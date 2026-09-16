# Chaos experiments

Three declarative experiments, one style (Chaos Mesh, `chaos-mesh.org/v1alpha1`), each
one written as a falsifiable statement about the manifests in `manifests/overlays/prod`.

They are **declarative on purpose**: an experiment that lives only in someone's shell
history cannot be reviewed, cannot be replayed and cannot be attached to an incident.
Each file below says what it assumes, what it measures, what would count as the
contract holding, and how to stop it.

| Experiment | Target | Injects | Success criterion |
| --- | --- | --- | --- |
| [`pod-delete.yaml`](pod-delete.yaml) | 1 of 3 replicas | `pod-kill` with a 15s grace period | Ready endpoints never drop below 2; the Deployment returns to 3/3 ready within the rollout timeout |
| [`cpu-stress.yaml`](cpu-stress.yaml) | 1 of 3 replicas | 2 stress-ng workers at 80% load, 5 minutes | The stressed pod fails readiness and leaves the endpoints; the other replicas keep answering 200 |
| [`network-latency.yaml`](network-latency.yaml) | all prod replicas | 300ms ±50ms netem delay, both directions, 5 minutes | Pods stay Ready (the readiness probe does not turn a slow dependency into a restart loop); client latency rises and then recovers |

## Hypothesis per experiment

Each experiment is a falsifiable statement; the "what we expected" column is what the
manifests claim, the "success criterion" column is what would count as the claim holding.

| Experiment | Hypothesis |
| --- | --- |
| [`pod-delete.yaml`](pod-delete.yaml) | Killing one replica mid-request costs no error: the two surviving replicas absorb the traffic, and the pod that returns is Ready before the next one could be touched. Tested by `chaos/pod-delete.yaml`'s grace period being shorter than the Service endpoint convergence window. |
| [`cpu-stress.yaml`](cpu-stress.yaml) | A single replica starved of CPU fails readiness and leaves the endpoints instead of being restarted: the readiness probe, not the liveness probe, is what removes a slow pod. |
| [`network-latency.yaml`](network-latency.yaml) | 300ms of injected latency does not turn into a restart loop: the readiness probe timeout tolerates a slow dependency, so pods stay Ready and only client latency moves. |

## How to run one

```bash
# Chaos Mesh must be installed in the cluster first:
#   helm repo add chaos-mesh https://charts.chaos-mesh.org
#   helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace \
#     --set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/run/containerd/containerd.sock
kubectl apply -f chaos/pod-delete.yaml
kubectl -n k8s-sre-lab-prod get pods -w          # watch the replacement come up
kubectl delete -f chaos/pod-delete.yaml          # always revert
```

`chaosDaemon.runtime=containerd` and the socket path are what kind clusters need; the
Chaos Mesh default targets Docker. That single flag is the usual reason a Chaos Mesh
install silently does nothing on kind.

## What is measured, and with what

| Question | Signal |
| --- | --- |
| Did traffic stop? | `kubectl -n k8s-sre-lab-prod get endpointslices -o jsonpath=...` ready address count |
| Did the app notice? | `demo_api_ready` and `demo_api_ready_failures_total` on `/metrics` |
| Did the probe fire correctly? | `kubectl -n k8s-sre-lab-prod get events --field-selector type=Warning` (`Unhealthy` events) |
| Did capacity recover? | `kubectl -n k8s-sre-lab-prod rollout status deployment/demo-api` |
| Would a node drain have been blocked? | `kubectl -n k8s-sre-lab-prod get pdb demo-api -o jsonpath='{.status}'` |

## Rollback

Every experiment is bounded by `duration` (or by a single action, for `pod-kill`), and
`kubectl delete -f <file>` removes it immediately. Nothing in this directory touches
the manifests, the Deployment spec or the image: if an experiment is deleted mid-flight,
the workload converges back to the state declared in `manifests/overlays/prod`.

If an experiment has to be stopped because it is causing real damage:

```bash
kubectl delete -f chaos/network-latency.yaml
kubectl -n k8s-sre-lab-prod rollout status deployment/demo-api --timeout=180s
kubectl -n k8s-sre-lab-prod get events --field-selector type=Warning   # confirm it stopped
```

## What these experiments do not cover

- **Node loss.** All three inject at the pod level. A node failure is a different
  failure mode (kubelet heartbeat timeouts, pod eviction, PV attachment) and needs a
  multi-node cluster that this lab's kind setup does not simulate faithfully.
- **Authentication pressure.** There is no ingress or auth layer in the lab, so
  "the dependency is slow" is approximated by netem rather than by a real upstream.
- **Anything measured over hours.** The burn-rate alerts in
  `manifests/base/prometheusrule.yaml` need a multi-window evaluation over hours to
  fire; these experiments run in minutes. `docs/reliability-patterns.md` is explicit
  about which parts of the lab are executed in CI and which are reviewed only.
