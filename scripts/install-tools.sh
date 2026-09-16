#!/usr/bin/env bash
#
# Downloads the official release binaries of the validation tooling into .tools/.
# No sudo, no package manager: the lab has to be reproducible on a laptop and on a
# CI runner alike, and the versions must match tools/versions.env exactly.
#
# Usage: scripts/install-tools.sh [kustomize|kubeconform|kube-linter|helm|all]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/.tools"
# shellcheck source=../tools/versions.env
source "${REPO_ROOT}/tools/versions.env"

requested="${1:-all}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64 | amd64) arch="amd64" ;;
  arm64 | aarch64) arch="arm64" ;;
  *)
    echo "unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "${TOOLS_DIR}"

log() { printf '[install-tools] %s\n' "$*"; }

# fetch <url> <destination>
fetch() {
  log "GET $1"
  curl --fail --location --silent --show-error "$1" -o "$2"
}

# wants <tool>
wants() {
  [ "${requested}" = "all" ] || [ "${requested}" = "$1" ]
}

install_kustomize() {
  [ -x "${TOOLS_DIR}/kustomize" ] && [ "${FORCE:-0}" != "1" ] && {
    log "kustomize already present"
    return 0
  }
  local tarball="${TOOLS_DIR}/kustomize.tar.gz"
  fetch "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_${os}_${arch}.tar.gz" "${tarball}"
  tar -xzf "${tarball}" -C "${TOOLS_DIR}" kustomize
  rm -f "${tarball}"
}

install_kubeconform() {
  [ -x "${TOOLS_DIR}/kubeconform" ] && [ "${FORCE:-0}" != "1" ] && {
    log "kubeconform already present"
    return 0
  }
  local tarball="${TOOLS_DIR}/kubeconform.tar.gz"
  fetch "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-${os}-${arch}.tar.gz" "${tarball}"
  tar -xzf "${tarball}" -C "${TOOLS_DIR}" kubeconform
  rm -f "${tarball}"
}

install_kube_linter() {
  [ -x "${TOOLS_DIR}/kube-linter" ] && [ "${FORCE:-0}" != "1" ] && {
    log "kube-linter already present"
    return 0
  }
  local tarball="${TOOLS_DIR}/kube-linter.tar.gz"
  fetch "https://github.com/stackrox/kube-linter/releases/download/${KUBE_LINTER_VERSION}/kube-linter-${os}_${arch}.tar.gz" "${tarball}"
  tar -xzf "${tarball}" -C "${TOOLS_DIR}" kube-linter
  rm -f "${tarball}"
}

install_helm() {
  [ -x "${TOOLS_DIR}/helm" ] && [ "${FORCE:-0}" != "1" ] && {
    log "helm already present"
    return 0
  }
  local tarball="${TOOLS_DIR}/helm.tar.gz"
  fetch "https://get.helm.sh/helm-${HELM_VERSION}-${os}-${arch}.tar.gz" "${tarball}"
  tar -xzf "${tarball}" -C "${TOOLS_DIR}" --strip-components=1 "${os}-${arch}/helm"
  rm -f "${tarball}"
}

install_shellcheck() {
  [ -x "${TOOLS_DIR}/shellcheck" ] && [ "${FORCE:-0}" != "1" ] && {
    log "shellcheck already present"
    return 0
  }
  # shellcheck publishes x86_64/aarch64, not amd64/arm64.
  local sc_arch
  case "${arch}" in
    amd64) sc_arch="x86_64" ;;
    arm64) sc_arch="aarch64" ;;
    *) sc_arch="${arch}" ;;
  esac
  local tarball="${TOOLS_DIR}/shellcheck.tar.gz"
  fetch "https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.${os}.${sc_arch}.tar.gz" "${tarball}"
  tar -xzf "${tarball}" -C "${TOOLS_DIR}" --strip-components=1 "shellcheck-${SHELLCHECK_VERSION}/shellcheck"
  rm -f "${tarball}"
}

wants kustomize && install_kustomize
wants kubeconform && install_kubeconform
wants kube-linter && install_kube_linter
wants helm && install_helm
wants shellcheck && install_shellcheck

chmod +x "${TOOLS_DIR}"/* 2>/dev/null || true

log "installed into ${TOOLS_DIR}:"
printf '  %-12s %s\n' kustomize "$("${TOOLS_DIR}/kustomize" version 2>/dev/null | head -n 1)"
printf '  %-12s %s\n' kubeconform "$("${TOOLS_DIR}/kubeconform" -v 2>/dev/null | head -n 1)"
printf '  %-12s %s\n' kube-linter "$("${TOOLS_DIR}/kube-linter" version 2>/dev/null | head -n 1)"
printf '  %-12s %s\n' helm "$("${TOOLS_DIR}/helm" version --short 2>/dev/null | head -n 1)"
printf '  %-12s %s\n' shellcheck "$("${TOOLS_DIR}/shellcheck" --version 2>/dev/null | tail -n 1)"
