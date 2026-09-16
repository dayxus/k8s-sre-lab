# Chaos experiments: hypotheses, measurements and honest status

A chaos experiment is only worth running if you write down what would prove you wrong
first. This file states the hypothesis, the injection, the measurement and the success
criterion for the three experiments in [`chaos/`](../chaos/), and then says plainly
what this repository actually executes.

**Read this first:** the Chaos Mesh experiments below are *not* executed in CI. Running
them needs a Chaos Mesh installation (a `chaos-daemon` DaemonSet with privileged
access), which would make the E2E job both slower and dependent on a third-party
operator. What CI does execute is the pod-level equivalent of the first experiment -
`kubectl delete pod` on a real replica - plus the readiness-gating assertion that the
other two depend on. Everything else here is a hypothesis with a stated measurement
plan, which is what the files in `chaos/` encode.

---

## Experiment 1 - Pod deletion

| | |
| --- | --- |
| **Manifest** | [`chaos/pod-delete.yaml`](../chaos/pod-delete.yaml) (Chaos Mesh `PodChaos`, `action: pod-kill`) |
| **Hypothesis** | Losing one replica out of three costs no requests and no capacity, because readiness gating keeps the Service endpoints populated and `maxUnavailable: 0` means the replacement is added before the lost pod is accounted for. |
| **Injection** | One pod killed with a 15s grace period (the preStop sleep alone is 10s). |
| **Measurement** | Ready address count from `kubectl get endpointslices`; `kubectl rollout status`; `Warning` events; `demo_api_ready` on `/metrics`. |
| **Success criterion** | Ready endpoints never fall below 2, the Deployment returns to 3/3 Ready within the rollout timeout, and no `Warning` event other than the expected `Unhealthy` probe events during startup appears. |
| **Falsified by** | Any `Warning` reason other than `Unhealthy`; a ready count of 0 or 1; a rollout that does not complete. |

**Status: executed in CI, in its `kubectl delete pod` form.** The E2E deletes one
replica and requires three ready endpoints again (`STEP 6/9`), and the CI log carries
the literal `rollout status` output. The Chaos Mesh form adds repetition - churn instead
of a single deletion - and that repetition is what is not covered here.

## Experiment 2 - CPU saturation of one replica

| | |
| --- | --- |
| **Manifest** | [`chaos/cpu-stress.yaml`](../chaos/cpu-stress.yaml) (Chaos Mesh `StressChaos`) |
| **Hypothesis** | Saturating one replica makes that replica fail readiness and leave the endpoints, while the other replicas keep answering 200. Saturation degrades a pod out of the rotation instead of taking the service down. |
| **Injection** | 2 stress-ng workers at 80% load for 5 minutes, against a 500m CPU limit. The stressor is throttled by the cgroup, which is deliberate: the failure should be latency, not an OOM kill. |
| **Measurement** | `demo_api_ready` per pod; ready addresses in the endpoint slice; `/readyz` failure count; `kube_pod_container_status_restarts_total` (a restart here would mean the liveness probe fired, which is a tuning bug). |
| **Success criterion** | The stressed pod leaves the ready endpoints; the other two stay Ready; the ready count never reaches 0; the container does not restart. |
| **Falsified by** | The stressed pod staying Ready and serving 30s requests; or the container restarting, which would mean liveness is doing readiness' job. |

**Status: not executed here.** The measurement plan is real; the operator dependency is
what keeps it out of CI. The readiness-gating half of the hypothesis is asserted in CI
(`STEP 7/9` and `STEP 8/9`): a pod that fails readiness is absent from the ready
addresses, and the healthy replicas keep serving.

## Experiment 3 - Network latency

| | |
| --- | --- |
| **Manifest** | [`chaos/network-latency.yaml`](../chaos/network-latency.yaml) (Chaos Mesh `NetworkChaos`, `action: delay`) |
| **Hypothesis** | 300ms ±50ms of added latency does **not** make the pods fail readiness, because the readiness probe answers "can this pod serve?", not "is this pod fast?". Latency shows up in client timings and nowhere else. |
| **Injection** | netem delay on all prod replicas, both directions, 5 minutes. |
| **Measurement** | `curl -w '%{time_total}'` against `/healthz` before, during and after; `kubectl get events --field-selector type=Warning` for `Unhealthy` events; ready address count. |
| **Success criterion** | Pods stay Ready for the whole 5 minutes; `time_total` rises by roughly the injected latency and returns to baseline; no restart, no endpoint churn. |
| **Falsified by** | Readiness failing, which would mean the readiness `timeoutSeconds` is tuned to latency rather than to reachability, and a slow dependency would cascade into an endpoint flap and a restart loop. |

**Status: not executed here.** This is the experiment whose value is a *false positive
test*: it is most useful precisely when it fails, because that failure points at a
mis-tuned probe rather than at the network.

---

## What the lab writes down because it is not measured here

- **HPA behaviour under load.** The autoscaler is asserted present with the right
  min/max and scale-down behaviour, but no load generator runs in the E2E, so no
  scaling event is observed. Measuring it needs a workload driver and a metrics-server
  sized for the load; the HPA settings in `manifests/base/hpa.yaml` are reviewed, not
  proven.
- **Burn-rate alerts firing.** The `PrometheusRule` is schema-validated and its
  expressions are checked against the metrics the app exposes, but there is no
  Prometheus in the kind cluster, and multi-window burn-rate alerts need hours of data.
  A rule that has never fired is a rule that has never been tested.
- **The SIGTERM drain under real traffic.** The endpoint removal is observed; the
  in-flight-request drain is covered by `tests/test_app.py`, which exercises the
  readiness/shutdown state machine directly rather than through a load generator.
- **Node failure.** Kind nodes are containers; a node-level failure here would not
  reproduce kubelet heartbeat timeouts, pod eviction and volume detach. The
  `topologySpreadConstraints` and the PDB are the parts of the answer that a pod-level
  experiment can still exercise.

Listing these is the point. A lab whose README implies it validated everything it
describes is the same failure mode as a dashboard that only shows green panels.
