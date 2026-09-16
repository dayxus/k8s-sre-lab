# Reliability patterns

Each section below is one pattern, in the order the failures usually show up:
first the process that will not die cleanly, then the capacity that disappears, then
the alarm nobody wired up. Every entry names the manifest that carries the pattern and
says plainly whether it is *executed* in CI or *reviewed* only.

The distinction matters. A pattern that is linted but never run is documentation with
extra steps, so the summary table at the end marks which ones the kind E2E actually
exercises.

---

## 1. Three probes, three different questions

**Problem.** A single health endpoint wired to both `livenessProbe` and
`readinessProbe` couples two unrelated decisions: "restart this process" and "send
this pod traffic". A slow start then becomes a restart loop, and a dependency slowdown
becomes an outage even though the pod could still serve.

**Symptom.** Pods restart during deploys; `CrashLoopBackOff` right after a rollout
starts; replicas that are running but never receive traffic.

**How the manifests solve it.**

| Probe | Endpoint | Question it answers | Tuning |
| --- | --- | --- | --- |
| `startupProbe` | `/healthz` | "has the process finished starting?" | 15 × 2s = 30s of grace, so liveness can stay strict |
| `readinessProbe` | `/readyz` | "should this pod be in the Service endpoints?" | 2 failures × 5s = removal after ~10s |
| `livenessProbe` | `/healthz` | "is this process wedged?" | 3 failures × 10s = restart after ~30s |

The application side is deliberate too: `/readyz` returns 503 while shutting down,
while `/healthz` keeps returning 200, so a draining pod leaves the endpoints without
being killed. `tests/test_app.py` asserts exactly that split.

**Where:** `manifests/base/deployment.yaml`, `chart/templates/deployment.yaml`,
`app/server.py`.
**Executed in CI:** yes - the E2E curls `/healthz` and `/readyz` and asserts 200.

---

## 2. Graceful shutdown: `terminationGracePeriodSeconds` + `preStop`

**Problem.** Kubernetes removes a pod from the Service endpoints and sends SIGTERM at
almost the same moment. In-flight requests lose their connection, and the endpoint
controller needs a beat to notice the pod is gone.

**Symptom.** A handful of 502s on every deploy; client errors that never reproduce in
a test environment because only production has traffic during rollout.

**How the manifests solve it.** Three budgets that have to fit inside each other:

```
preStop sleep 10s  ->  app drain <= 30s  ->  terminationGracePeriodSeconds 45s
```

`preStop` keeps the container alive while the endpoints drain; the app then stops
accepting new work but finishes in-flight requests (`SHUTDOWN_GRACE_SECONDS`); the
pod-level grace period is the hard ceiling and is deliberately larger than the other
two, otherwise the kubelet sends SIGKILL in the middle of a request.
`INV2_graceful_shutdown` in `scripts/invariants.py` parses the preStop command, the
ConfigMap and the pod grace period and fails if that ordering is broken.

**Where:** `manifests/base/deployment.yaml` (`lifecycle.preStop`),
`manifests/base/configmap.yaml` (`SHUTDOWN_GRACE_SECONDS`).
**Executed in CI:** partly - the E2E observes the pod leaving the endpoints via
`endpointslices`; the SIGTERM path itself is covered by `tests/test_app.py`.

---

## 3. `maxUnavailable: 0` + PodDisruptionBudget

**Problem.** Two different mechanisms can remove a pod at the same time: a rolling
update (voluntary, controller-driven) and a node drain or cluster upgrade (voluntary,
operator-driven). With `maxUnavailable: 1` and no budget, a deploy that coincides with
a drain can take the last replica.

**Symptom.** Capacity dips that are visible in a deployment timeline but are never
attributed to anything in a postmortem, because each mechanism looks fine on its own.

**How the manifests solve it.** The two settings guard different things and are only
safe together:

- `strategy.rollingUpdate.maxUnavailable: 0` with `maxSurge: 1` means a rollout adds a
  pod before it removes one. Capacity never goes down during a deploy.
- `PodDisruptionBudget minAvailable: 2` (prod, 3 replicas) means the eviction API
  refuses to take a second pod while one is already gone. A node drain therefore
  proceeds one pod at a time.

Together: 3 replicas - 1 voluntary eviction = 2 available, which is exactly what a
surge rollout needs. If the budget were `minAvailable: 3`, the drain would block
forever; `maxUnavailable: 0` also means a rollout that can never become Ready never
finishes - which is why `progressDeadlineSeconds: 120` is set.

`unhealthyPodEvictionPolicy: AlwaysAllow` closes the other trap: a pod that is already
failing does not block a node drain indefinitely.

**Where:** `manifests/base/deployment.yaml` (`spec.strategy`),
`manifests/base/pdb.yaml`, `manifests/overlays/prod/patch-pdb.yaml`.
**Executed in CI:** yes - the E2E asserts `minAvailable=2`, `maxUnavailable=0`, DELETES
a replica and requires the Deployment to converge back to 3 ready endpoints, and
separately proves that a pod failing readiness keeps the ready count at 3.

---

## 4. Resource requests, limits and an autoscaler with an opinion

**Problem.** Requests are what the scheduler and the HPA arithmetic use; limits are
what the kubelet enforces. Setting only limits makes pods unboundedly greedy at
scheduling time and makes CPU utilisation a meaningless HPA input.

**Symptom.** The HPA scales up and down in the same minute; pods get OOMKilled at the
limit; nodes look over-committed while most containers idle.

**How the manifests solve it.**

- Requests and limits on every container, and `INV1` fails a manifest where a limit is
  below its request.
- HPA target: 70% CPU utilisation of the *request*, `minReplicas: 3` so a single
  disruption cannot drop the service below two.
- `scaleUp` reacts immediately (0s window, max +100%/30s or +2 pods/30s).
- `scaleDown` waits 10 minutes and removes at most 1 pod or 25% per minute, so a spike
  that has already passed does not cause a scale-in that has to be undone.

**Where:** `manifests/base/deployment.yaml`, `manifests/base/hpa.yaml`,
`manifests/overlays/prod/patch-resources.yaml`.
**Executed in CI:** partly - the E2E asserts the HPA exists with `minReplicas=3` /
`maxReplicas=10`. No load is generated, so the HPA is not observed scaling (see
`docs/chaos-experiments.md` for what is not covered).

---

## 5. Spreading replicas: topology constraints and anti-affinity

**Problem.** By default the scheduler is free to put every replica of a Deployment on
one node. Losing that node loses the service, and a single noisy neighbour affects all
replicas at once.

**Symptom.** "The service is down" traced to one node; replicas that always restart
together.

**How the manifests solve it.** `topologySpreadConstraints` (maxSkew 1 over
`kubernetes.io/hostname`) plus a `preferredDuringScheduling...` pod anti-affinity — both
**soft**:

- `whenUnsatisfiable: ScheduleAnyway` instead of `DoNotSchedule`. With 3 replicas and
  two schedulable workers, a hard constraint leaves one pod Pending forever, turning a
  reliability feature into an outage of its own.
- Preferred anti-affinity rather than required, for the same reason.

The trade-off is explicit: spreading is a preference, not a guarantee. A cluster with
fewer nodes than replicas is a capacity problem, and the manifests refuse to pretend
otherwise.

**Where:** `manifests/base/topology-spread.yaml` (applied as a patch from
`manifests/base/kustomization.yaml`), `chart/templates/deployment.yaml`.
**Executed in CI:** yes - the E2E prints the pod-to-node mapping
(`artifacts/spread.txt`) on a 3-node kind cluster.

---

## 6. NetworkPolicy: default deny, DNS by exception

**Problem.** In Kubernetes every pod can reach every pod. A compromised container has
lateral movement for free: it can query the cloud metadata endpoint, scan the cluster,
or talk to a database it never needed.

**Symptom.** None, until an incident. This is the pattern that has no observable
symptom before it matters.

**How the manifests solve it.** One policy, `podSelector` matching the workload, with
both `Ingress` and `Egress` in `policyTypes`:

- ingress: only the lab namespace, on the container port.
- ingress: the node network CIDR, because the kubelet originates both the probe
  connections and the `kubectl port-forward` relay used by an on-call engineer. Dropping
  this rule is the classic mistake that makes a default-deny policy look broken.
- egress: DNS to `kube-system` only. Nothing else leaves the pod, including the
  metadata service.

`EXTRA_network_isolation` in `scripts/invariants.py` fails any workload that is not
selected by a policy with an egress rule, so the isolation cannot be lost silently.

**Where:** `manifests/base/networkpolicy.yaml`, `chart/templates/networkpolicy.yaml`.
**Executed in CI:** yes - the E2E's curls go through the policy (they originate from the
node network), so a wrong CIDR or a missing rule fails the job.

---

## 7. Security context that the namespace enforces

**Problem.** Pod security settings written by hand drift. A new manifest that forgets
`runAsNonRoot` is merged because the linter was not run.

**Symptom.** Discovered during an audit, months later, usually in a different namespace
than the one that was reviewed.

**How the manifests solve it.** Two layers:

1. The pod and container security contexts (`runAsNonRoot`, fixed uid/gid,
   `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`,
   `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`).
2. The namespace label
   `pod-security.kubernetes.io/enforce: restricted`, which makes the API server reject
   anything that does not comply. The manifests are written to pass admission rather
   than to be exempt from it, and the E2E proves it: a pod that violated the standard
   would never reach Ready.

**Where:** `manifests/base/namespace.yaml`, `manifests/base/deployment.yaml`,
`manifests/base/kustomization.yaml`.
**Executed in CI:** yes - by admission, at apply time. Also enforced statically by
`INV4_security_context` and by kube-linter.

---

## 8. PriorityClass and preemption

**Problem.** Under node pressure the kubelet evicts by QoS class and priority. Without
a priority class, a user-facing API competes on equal terms with a batch job.

**Symptom.** The API is evicted during a busy night while a best-effort cron job keeps
running.

**How the manifests solve it.** A dedicated `PriorityClass` at value 100000 -
above ordinary batch work, far below the reserved `system-*` classes - and explicitly
not the cluster default.

**Where:** `manifests/base/priorityclass.yaml`.
**Executed in CI:** reviewed only; the kind cluster has no resource pressure.

---

## 9. Alerts on symptoms, not on causes

**Problem.** Alerting on the immediate cause (`CPU > 80%`) pages for things that do not
affect users and misses the failures that do.

**Symptom.** Alert fatigue, then ignored alerts, then an outage nobody noticed.

**How the manifests solve it.** Two groups in one `PrometheusRule`:

- `demo-api.slo.availability`: a 99.9%/30d SLI computed from metrics the app really
  exposes, with multi-window multi-burn-rate alerts. 14.4x burn (long 1h + short 5m
  window both breached) pages; 6x burn (30m + 5m) files a ticket. Requiring both windows
  is what stops a scrape gap from paging someone at 03:00.
- `demo-api.platform-symptoms`: the Kubernetes-specific failures - no available
  replicas, crash looping, a rollout that stopped progressing, a PDB that blocks every
  drain, an HPA pinned at `maxReplicas`.

`tests/test_manifests.py` asserts both groups and checks that each alert expression only
references metrics `app/server.py` actually emits, because an alert on a metric nobody
emits is a rule that will never fire and will never be noticed.

**Where:** `manifests/base/prometheusrule.yaml`,
`manifests/base/servicemonitor.yaml`.
**Executed in CI:** reviewed only - no Prometheus runs in the kind cluster, so the
rules are schema-validated and reviewed, never observed firing. `docs/chaos-experiments.md`
lists this under what the lab does not cover.

---

## 10. Pin everything, including the tools that check the pins

**Problem.** `image: app:latest` and a floating linter version both mean the artefact
that passed CI is not necessarily the artefact that runs, and a green build can turn red
overnight for reasons unrelated to the change.

**Symptom.** "It works on my machine"; a deploy that cannot state which build is live;
a diff that only touches a comment failing because upstream shipped a new linter rule.

**How the manifests solve it.** `imagePullPolicy: IfNotPresent` with a fixed tag, and
`tools/versions.env` as the single source of truth for Kubernetes, kind, kustomize,
kubeconform, kube-linter, Helm and shellcheck. `docs/versions.md` is generated from it,
`make lint` fails if the two disagree, and the weekly maintenance workflow is the only
thing allowed to move the pins - and only when the whole gate still passes with them.

**Where:** `tools/versions.env`, `scripts/install-tools.sh`,
`scripts/update_versions.py`, `.github/workflows/maintenance.yml`.
**Executed in CI:** yes (`docs/versions.md` consistency check).

---

## Which patterns are actually executed

| Pattern | Static checks | kind E2E | Notes |
| --- | --- | --- | --- |
| Three probes | INV1 | yes | `/healthz` and `/readyz` curled; FAIL_READY gate observed |
| Graceful shutdown | INV2 | partly | endpoint removal observed; SIGTERM drain covered by unit tests |
| `maxUnavailable: 0` + PDB | INV3, INV6 | yes | replica deleted, ready count observed, PDB minAvailable asserted |
| Requests, limits, HPA | INV1, INV6 | partly | HPA asserted present; no load generated |
| Topology spread | INV7 (labels) | yes | pod-to-node mapping printed from the 3-node cluster |
| NetworkPolicy | EXTRA | yes | traffic only reaches the app through the policy |
| Security context | INV4 | yes | restricted Pod Security admission at apply time |
| PriorityClass | INV8 | no | reviewed only |
| SLO burn-rate alerts | test_manifests | no | schema-validated, never observed firing |
| Pinned versions | versions check | yes | generated doc compared in CI |
