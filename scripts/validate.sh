#!/usr/bin/env bash
#
# Renders every deployment shape and runs the schema + policy gates on the result.
#
#   kustomize build   manifests/base, overlays/dev, overlays/prod
#   helm lint         chart/ against values.yaml, values-dev.yaml, values-prod.yaml
#   helm template     the same three value sets
#   kubeconform       every rendered document against the pinned Kubernetes version
#   kube-linter       the same documents against the project policy in .kube-linter.yaml
#
# Exit code is non-zero if any gate fails. Run `make tools` first.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=../tools/versions.env
source "${REPO_ROOT}/tools/versions.env"

TOOLS_DIR="${REPO_ROOT}/.tools"
PATH="${TOOLS_DIR}:${PATH}"

PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="$(command -v python3)"
fi

for bin in kustomize kubeconform kube-linter helm; do
  if [ ! -x "${TOOLS_DIR}/${bin}" ] && ! command -v "${bin}" >/dev/null 2>&1; then
    echo "missing ${bin}; run scripts/install-tools.sh (or make tools)" >&2
    exit 1
  fi
done

BUILD_DIR="${REPO_ROOT}/build"
mkdir -p "${BUILD_DIR}"

CRD_SCHEMA_LOCATION='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

hr() { printf '\n=== %s ===\n' "$1"; }

hr "render: kustomize + helm"
"${PYTHON}" scripts/render.py --out "${BUILD_DIR}"

hr "helm lint"
for values in values values-dev values-prod; do
  echo "-- chart/${values}.yaml"
  helm lint chart -f "chart/${values}.yaml"
done

hr "kubeconform ${KUBECONFORM_VERSION} against Kubernetes ${KUBERNETES_VERSION}"
# -strict rejects unknown fields; CRs without an upstream schema (Prometheus operator)
# are resolved from the datree CRDs catalog, and anything still unknown is reported
# rather than silently skipped (-ignore-missing-schemas is NOT used).
kubeconform \
  -strict \
  -summary \
  -kubernetes-version "${KUBERNETES_VERSION}" \
  -schema-location default \
  -schema-location "${CRD_SCHEMA_LOCATION}" \
  "${BUILD_DIR}"/kustomize-dev.yaml \
  "${BUILD_DIR}"/kustomize-prod.yaml \
  "${BUILD_DIR}"/helm-dev.yaml \
  "${BUILD_DIR}"/helm-prod.yaml

hr "kube-linter ${KUBE_LINTER_VERSION}"
kube-linter lint \
  --config .kube-linter.yaml \
  "${BUILD_DIR}"/kustomize-dev.yaml \
  "${BUILD_DIR}"/kustomize-prod.yaml \
  "${BUILD_DIR}"/helm-dev.yaml \
  "${BUILD_DIR}"/helm-prod.yaml

hr "reliability invariants"
"${PYTHON}" scripts/invariants_report.py

hr "validate.sh: OK"
