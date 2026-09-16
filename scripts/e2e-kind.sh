#!/usr/bin/env bash
#
# End-to-end proof of the reliability contract on a real cluster.
#
# Requires docker, kind and kubectl; the CI job `e2e-kind` is where this runs, because
# the machine this repo was authored on has no container runtime. Everything it prints
# is the literal evidence quoted in the README.
#
#   1. create a 3-node kind cluster
#   2. build the demo image and load it into the cluster
#   3. kubectl apply -k manifests/overlays/prod, then wait for the rollout
#   4. capture the applied PodDisruptionBudget, HPA and probes
#   5. port-forward the Service and curl /healthz, /readyz and /metrics
#   6. delete one replica: the Deployment must converge back with no unexpected
#      Warning events
#   7. FAIL_READY=true on the prod Deployment: the new pod must never enter the ready
#      endpoints, and the three old replicas must keep serving (zero downtime)
#   8. the runtime chaos switch on the single-replica dev namespace: Ready 1 -> 0 -> 1
#      in the Service endpoint slice
#   9. write describe/events/metrics evidence to artifacts/
#
# Usage: scripts/e2e-kind.sh [--no-cluster]   (--no-cluster reuses an existing cluster)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=../tools/versions.env
source "${REPO_ROOT}/tools/versions.env"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-k8s-sre-lab}"
PROD_NS="k8s-sre-lab-prod"
DEV_NS="k8s-sre-lab-dev"
IMAGE="k8s-sre-lab/demo-api:${APP_IMAGE_TAG}"
ARTIFACTS="${REPO_ROOT}/artifacts"
SELECTOR="app.kubernetes.io/name=demo-api"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
LOCAL_PORT=18080
LOCAL_ADMIN_PORT=18081

REUSE_CLUSTER=0
[ "${1:-}" = "--no-cluster" ] && REUSE_CLUSTER=1

log()  { printf '\n== %s\n' "$*"; }
step() { printf '\n--- %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required but not on PATH"; }
require docker
require kind
require kubectl
require curl

mkdir -p "${ARTIFACTS}"

PF_PIDS=""
cleanup() {
  # shellcheck disable=SC2086
  [ -n "${PF_PIDS}" ] && kill ${PF_PIDS} 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# "ip ready" for every endpoint of the demo-api Service.
endpoint_states() {
  kubectl -n "$1" get endpointslices -l kubernetes.io/service-name=demo-api \
    -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{" "}{.conditions.ready}{"\n"}{end}' 2>/dev/null || true
}

ready_count() {
  endpoint_states "$1" | awk '$2 == "true"' | grep -c . || true
}

# "name ip ready" for every demo-api pod.
pod_states() {
  kubectl -n "$1" get pods -l "${SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.podIP}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null || true
}

wait_for_ready_count() { # ns expected [timeout]
  local ns="$1" expected="$2" timeout="${3:-150}" start count
  start="$(date +%s)"
  while [ $(( $(date +%s) - start )) -lt "${timeout}" ]; do
    count="$(ready_count "${ns}")"
    if [ "${count}" = "${expected}" ]; then
      echo "  ready endpoints in ${ns}: ${count} (expected ${expected})"
      return 0
    fi
    sleep 3
  done
  endpoint_states "${ns}" | sed 's/^/    /'
  return 1
}

wait_for_notready_pod_ip() { # ns [timeout]
  local ns="$1" timeout="${2:-90}" start ip
  start="$(date +%s)"
  while [ $(( $(date +%s) - start )) -lt "${timeout}" ]; do
    ip="$(pod_states "${ns}" | awk '$3 == "False" { print $2; exit }')"
    if [ -n "${ip}" ]; then
      echo "${ip}"
      return 0
    fi
    sleep 3
  done
  return 1
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1"; }

# ---------------------------------------------------------------------------
# 1. cluster
# ---------------------------------------------------------------------------

log "STEP 1/9 kind cluster with 3 nodes"
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  if [ "${REUSE_CLUSTER}" = "1" ]; then
    echo "reusing existing cluster ${CLUSTER_NAME}"
  else
    echo "cluster ${CLUSTER_NAME} already exists; deleting it"
    kind delete cluster --name "${CLUSTER_NAME}"
    kind create cluster --name "${CLUSTER_NAME}" --config scripts/kind-3nodes.yaml \
      --image "${KIND_NODE_IMAGE}" --wait 180s
  fi
else
  # The node image is pinned in tools/versions.env (tag + digest): the cluster the E2E
  # proves things on is the cluster the version audit talks about.
  kind create cluster --name "${CLUSTER_NAME}" --config scripts/kind-3nodes.yaml \
    --image "${KIND_NODE_IMAGE}" --wait 180s
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
kubectl get nodes -o wide | tee "${ARTIFACTS}/nodes.txt"

# ---------------------------------------------------------------------------
# 2. image
# ---------------------------------------------------------------------------

log "STEP 2/9 build and load ${IMAGE}"
docker build -t "${IMAGE}" app/
kind load docker-image "${IMAGE}" --name "${CLUSTER_NAME}"

# ---------------------------------------------------------------------------
# 3. apply the prod overlay and wait for the rollout
# ---------------------------------------------------------------------------

log "STEP 3/9 kubectl apply -k manifests/overlays/prod"
kubectl apply -k manifests/overlays/prod
kubectl -n "${PROD_NS}" rollout status deployment/demo-api --timeout="${ROLLOUT_TIMEOUT}" \
  | tee "${ARTIFACTS}/rollout-status.txt"
kubectl -n "${PROD_NS}" get pods -o wide | tee "${ARTIFACTS}/pods-after-rollout.txt"

step "topology spread across nodes (3 replicas, 2 schedulable workers)"
kubectl -n "${PROD_NS}" get pods -l "${SELECTOR}" \
  -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,READY:.status.conditions[?(@.type=="Ready")].status' \
  | tee "${ARTIFACTS}/spread.txt"

# ---------------------------------------------------------------------------
# 4. PDB / HPA / probes
# ---------------------------------------------------------------------------

log "STEP 4/9 disruption budget, autoscaler and probes as applied"
kubectl -n "${PROD_NS}" get pdb,hpa -o yaml | tee "${ARTIFACTS}/pdb-hpa.yaml"
kubectl -n "${PROD_NS}" get pdb,hpa | tee "${ARTIFACTS}/pdb-hpa.txt"

pdb_min="$(kubectl -n "${PROD_NS}" get pdb demo-api -o jsonpath='{.spec.minAvailable}')"
hpa_min="$(kubectl -n "${PROD_NS}" get hpa demo-api -o jsonpath='{.spec.minReplicas}')"
hpa_max="$(kubectl -n "${PROD_NS}" get hpa demo-api -o jsonpath='{.spec.maxReplicas}')"
ready_path="$(kubectl -n "${PROD_NS}" get deploy demo-api -o jsonpath='{.spec.template.spec.containers[0].readinessProbe.httpGet.path}')"
live_path="$(kubectl -n "${PROD_NS}" get deploy demo-api -o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet.path}')"
startup_path="$(kubectl -n "${PROD_NS}" get deploy demo-api -o jsonpath='{.spec.template.spec.containers[0].startupProbe.httpGet.path}')"
max_unavailable="$(kubectl -n "${PROD_NS}" get deploy demo-api -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}')"
grace="$(kubectl -n "${PROD_NS}" get deploy demo-api -o jsonpath='{.spec.template.spec.terminationGracePeriodSeconds}')"

echo "  pdb.minAvailable=${pdb_min} hpa=${hpa_min}-${hpa_max} maxUnavailable=${max_unavailable} terminationGracePeriodSeconds=${grace}"
echo "  probes: startup=${startup_path} readiness=${ready_path} liveness=${live_path}"

[ "${pdb_min}" = "2" ] || fail "PodDisruptionBudget minAvailable=${pdb_min}, expected 2"
[ "${hpa_min}" = "3" ] || fail "HPA minReplicas=${hpa_min}, expected 3"
[ "${hpa_max}" = "10" ] || fail "HPA maxReplicas=${hpa_max}, expected 10"
[ "${max_unavailable}" = "0" ] || fail "maxUnavailable=${max_unavailable}, expected 0"
[ "${grace}" -ge 30 ] || fail "terminationGracePeriodSeconds=${grace}, expected >= 30"
[ "${ready_path}" = "/readyz" ] || fail "readinessProbe path=${ready_path}, expected /readyz"
[ "${live_path}" = "/healthz" ] || fail "livenessProbe path=${live_path}, expected /healthz"
[ "${startup_path}" = "/healthz" ] || fail "startupProbe path=${startup_path}, expected /healthz"

wait_for_ready_count "${PROD_NS}" 3 || fail "expected 3 ready endpoints before the traffic tests"

# ---------------------------------------------------------------------------
# 5. port-forward and curl
# ---------------------------------------------------------------------------

log "STEP 5/9 port-forward the Service and curl the endpoints"
kubectl -n "${PROD_NS}" port-forward service/demo-api "${LOCAL_PORT}:80" >"${ARTIFACTS}/port-forward.log" 2>&1 &
PF_PIDS="${PF_PIDS} $!"

for _ in $(seq 1 30); do
  if [ "$(http_code "http://127.0.0.1:${LOCAL_PORT}/healthz")" = "200" ]; then
    break
  fi
  sleep 1
done

CURL_EVIDENCE="${ARTIFACTS}/curl-evidence.txt"
: >"${CURL_EVIDENCE}"
for path in /healthz /readyz /metrics; do
  code="$(http_code "http://127.0.0.1:${LOCAL_PORT}${path}")"
  {
    echo "\$ curl -s -w '\\n%{http_code}' http://127.0.0.1:${LOCAL_PORT}${path}"
    echo "HTTP ${code}"
  } >>"${CURL_EVIDENCE}"
  [ "${code}" = "200" ] || fail "GET ${path} returned ${code}, expected 200"
done

curl -s "http://127.0.0.1:${LOCAL_PORT}/metrics" >"${ARTIFACTS}/metrics.txt"
step "curl output"
cat "${CURL_EVIDENCE}"
step "metrics sample"
grep -E '^# TYPE demo_api_|^demo_api_(ready|uptime_seconds|build_info)' "${ARTIFACTS}/metrics.txt" \
  | tee "${ARTIFACTS}/metrics-sample.txt"

grep -q '^# TYPE demo_api_ready gauge' "${ARTIFACTS}/metrics.txt" \
  || fail "/metrics is not Prometheus text exposition (no '# TYPE demo_api_ready gauge')"
grep -q '^demo_api_ready 1' "${ARTIFACTS}/metrics.txt" \
  || fail "/metrics does not report demo_api_ready 1"
grep -q '^demo_api_build_info{version="' "${ARTIFACTS}/metrics.txt" \
  || fail "/metrics does not report the build info series"

# ---------------------------------------------------------------------------
# 6. resilience: delete one replica
# ---------------------------------------------------------------------------

log "STEP 6/9 delete one replica and wait for convergence"
victim="$(kubectl -n "${PROD_NS}" get pods -l "${SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
echo "  kubectl delete pod ${victim}"
kubectl -n "${PROD_NS}" delete pod "${victim}" --wait=false
kubectl -n "${PROD_NS}" rollout status deployment/demo-api --timeout="${ROLLOUT_TIMEOUT}"
wait_for_ready_count "${PROD_NS}" 3 || fail "ready endpoints did not return to 3 after deleting ${victim}"
kubectl -n "${PROD_NS}" get pods -l "${SELECTOR}" -o wide | tee "${ARTIFACTS}/pods-after-pod-delete.txt"

step "Warning events (probe failures during startup are expected and explained)"
kubectl -n "${PROD_NS}" get events --field-selector type=Warning -o wide | tee "${ARTIFACTS}/warning-events.txt"
kubectl -n "${PROD_NS}" get events --field-selector type=Warning -o json >"${ARTIFACTS}/warning-events.json"
reasons="$(kubectl -n "${PROD_NS}" get events --field-selector type=Warning \
  -o jsonpath='{range .items[*]}{.reason}{"\n"}{end}' | sort -u | grep -v '^$' || true)"
unexpected="$(printf '%s\n' "${reasons}" | grep -v -E '^(Unhealthy)$' || true)"
if [ -n "${unexpected}" ]; then
  fail "unexpected Warning event reasons: $(printf '%s' "${unexpected}" | tr '\n' ' ')"
fi
echo "  only 'Unhealthy' events present: the startup probe answering 503 while the app"
echo "  warms up. That is the probe working, not a failure."

# ---------------------------------------------------------------------------
# 7. FAIL_READY=true on prod: the new pod never enters the endpoints
# ---------------------------------------------------------------------------

log "STEP 7/9 FAIL_READY=true on the prod Deployment (zero-downtime rollout gate)"
kubectl -n "${PROD_NS}" set env deployment/demo-api FAIL_READY=true >/dev/null

barred_ip="$(wait_for_notready_pod_ip "${PROD_NS}" 90)" \
  || fail "no NotReady pod appeared after FAIL_READY=true"

step "pod states (name ip ready)"
pod_states "${PROD_NS}" | tee "${ARTIFACTS}/pod-states-fail-ready.txt"
step "endpoint slice states (ip ready)"
endpoint_states "${PROD_NS}" | tee "${ARTIFACTS}/endpoints-fail-ready.txt"

if endpoint_states "${PROD_NS}" | awk -v ip="${barred_ip}" '$1 == ip && $2 == "true" { found = 1 } END { exit !found }'; then
  fail "pod ${barred_ip} with FAIL_READY=true is in the ready endpoints"
fi
echo "  barred pod ${barred_ip} is absent from the ready endpoints (readiness is the traffic gate)"

kept="$(ready_count "${PROD_NS}")"
echo "  ready endpoints kept during the failed rollout: ${kept} (maxUnavailable=0 never removed capacity)"
[ "${kept}" = "3" ] || fail "ready endpoints dropped to ${kept} during the failed rollout"

step "revert FAIL_READY and let the rollout finish"
kubectl -n "${PROD_NS}" set env deployment/demo-api FAIL_READY=false >/dev/null
kubectl -n "${PROD_NS}" rollout status deployment/demo-api --timeout="${ROLLOUT_TIMEOUT}"
wait_for_ready_count "${PROD_NS}" 3 || fail "ready endpoints did not return to 3 after reverting FAIL_READY"

# ---------------------------------------------------------------------------
# 8. runtime chaos switch on the single-replica dev overlay
# ---------------------------------------------------------------------------

log "STEP 8/9 readiness gating on a single replica (the dev overlay)"
kubectl apply -k manifests/overlays/dev
kubectl -n "${DEV_NS}" rollout status deployment/demo-api --timeout="${ROLLOUT_TIMEOUT}"
wait_for_ready_count "${DEV_NS}" 1 || fail "dev namespace did not reach 1 ready endpoint"

dev_pod="$(kubectl -n "${DEV_NS}" get pods -l "${SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "${DEV_NS}" port-forward "pod/${dev_pod}" "${LOCAL_ADMIN_PORT}:8080" >"${ARTIFACTS}/port-forward-dev.log" 2>&1 &
PF_PIDS="${PF_PIDS} $!"
for _ in $(seq 1 30); do
  if [ "$(http_code "http://127.0.0.1:${LOCAL_ADMIN_PORT}/readyz")" = "200" ]; then
    break
  fi
  sleep 1
done
echo "  dev pod ${dev_pod}: /readyz HTTP $(http_code "http://127.0.0.1:${LOCAL_ADMIN_PORT}/readyz")"

SWITCH_EVIDENCE="${ARTIFACTS}/readiness-switch.txt"
{
  echo "\$ kubectl -n ${DEV_NS} get endpointslices -l kubernetes.io/service-name=demo-api -o jsonpath=..."
  echo "ready endpoints before the switch: $(ready_count "${DEV_NS}")"
} | tee "${SWITCH_EVIDENCE}"

curl -s -X POST "http://127.0.0.1:${LOCAL_ADMIN_PORT}/admin/readiness?value=true" \
  | tee "${ARTIFACTS}/chaos-switch-response.txt"
echo

fail_reason="readiness switch did not remove the pod from the endpoints within 60s"
start="$(date +%s)"
while [ $(( $(date +%s) - start )) -lt 60 ]; do
  if [ "$(ready_count "${DEV_NS}")" = "0" ]; then
    fail_reason=""
    break
  fi
  sleep 3
done
[ -z "${fail_reason}" ] || { endpoint_states "${DEV_NS}" | sed 's/^/    /'; fail "${fail_reason}"; }

echo "  ready endpoints with readiness failing: $(ready_count "${DEV_NS}")" | tee -a "${SWITCH_EVIDENCE}"
endpoint_states "${DEV_NS}" | tee -a "${SWITCH_EVIDENCE}"

curl -s -X POST "http://127.0.0.1:${LOCAL_ADMIN_PORT}/admin/readiness?value=false" >/dev/null
wait_for_ready_count "${DEV_NS}" 1 || fail "readiness did not recover after the switch was reverted"
echo "  ready endpoints after the revert: $(ready_count "${DEV_NS}")" | tee -a "${SWITCH_EVIDENCE}"

if [ "$(http_code "http://127.0.0.1:${LOCAL_ADMIN_PORT}/healthz")" != "200" ]; then
  fail "liveness must stay green while readiness is failing"
fi
echo "  /healthz stayed 200 throughout: a pod that stops serving is not a pod to restart"

# ---------------------------------------------------------------------------
# 9. evidence
# ---------------------------------------------------------------------------

log "STEP 9/9 collecting evidence into ${ARTIFACTS}"
kubectl -n "${PROD_NS}" describe deployment demo-api >"${ARTIFACTS}/describe-deployment.txt"
kubectl -n "${PROD_NS}" get all -o wide >"${ARTIFACTS}/all-prod.txt"
kubectl -n "${PROD_NS}" get networkpolicy,priorityclass -o yaml >"${ARTIFACTS}/networkpolicy-priorityclass.yaml" 2>/dev/null || true
kubectl -n "${DEV_NS}" describe deployment demo-api >"${ARTIFACTS}/describe-deployment-dev.txt"

step "rollout status (literal)"
cat "${ARTIFACTS}/rollout-status.txt"
step "kubectl get pdb,hpa (literal)"
cat "${ARTIFACTS}/pdb-hpa.txt"
step "curl evidence (literal)"
cat "${CURL_EVIDENCE}"
step "endpoint slice before/after the readiness switch (literal)"
cat "${SWITCH_EVIDENCE}"

log "e2e-kind.sh: OK - every assertion above ran against a real cluster"
