#!/usr/bin/env bash
#
# Weekly maintenance: refresh the pinned versions, re-validate everything with them,
# write reports/weekly-audit.md and commit - but only if something really changed.
#
# Rules:
#   * a version bump is only kept if the whole gate (rendering, kubeconform,
#     kube-linter, invariants, pytest, link check) still passes with it;
#   * if the candidate pins fail, they are reverted, the failure is recorded in the
#     report with the literal tool output, and an issue is opened;
#   * nothing is committed when `git diff --quiet` is true.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${PYTHON:-python3}"
REPORT_DIR="${REPO_ROOT}/reports"
CANDIDATE_REPORT="$(mktemp -t weekly-audit-candidate.XXXXXX.md)"

log() { printf '\n== %s\n' "$*"; }

mkdir -p "${REPORT_DIR}"

log "1/5 refresh the pinned versions from upstream"
"${PYTHON}" scripts/update_versions.py --fetch | tee /tmp/version-bump.txt

pins_changed=0
if ! git diff --quiet -- tools/versions.env docs/versions.md; then
  pins_changed=1
fi
echo "pins changed: ${pins_changed}"

log "2/5 full gate with the candidate pins"
# audit.py never exits non-zero for a failing tool: the status is in the report and on
# stdout, which is what this script keys off.
"${PYTHON}" scripts/audit.py --report "${CANDIDATE_REPORT}" | tee /tmp/audit-candidate.txt
candidate_status="$(grep -m1 '^AUDIT_STATUS=' /tmp/audit-candidate.txt | cut -d= -f2)"

if [ "${candidate_status}" = "ok" ]; then
  log "3/5 candidate pins are green: publishing the report"
  cp "${CANDIDATE_REPORT}" "${REPORT_DIR}/weekly-audit.md"
else
  log "3/5 candidate pins failed the gate"
  if [ "${pins_changed}" = "1" ]; then
    log "4/5 reverting the version bump and re-validating with the known-good pins"
    git checkout -- tools/versions.env docs/versions.md
    "${PYTHON}" scripts/audit.py --report "${REPORT_DIR}/weekly-audit.md" | tee /tmp/audit-reverted.txt
    reverted_status="$(grep -m1 '^AUDIT_STATUS=' /tmp/audit-reverted.txt | cut -d= -f2)"
    echo "status with the previous pins: ${reverted_status}"
  else
    cp "${CANDIDATE_REPORT}" "${REPORT_DIR}/weekly-audit.md"
  fi
  {
    echo ""
    echo "## Rejected version bump"
    echo ""
    echo "The candidate pins in this run failed the gate; they were not kept. Literal output:"
    echo ""
    echo '```'
    sed -n '/## kubeconform/,$p' "${CANDIDATE_REPORT}" | head -n 120
    echo '```'
    echo ""
  } >>"${REPORT_DIR}/weekly-audit.md"
fi

log "5/5 commit only if something really changed"
if git diff --quiet -- reports/weekly-audit.md docs/versions.md tools/versions.env; then
  echo "no diff: nothing to commit"
else
  git add reports/weekly-audit.md docs/versions.md tools/versions.env
  if [ "${pins_changed}" = "1" ] && [ "${candidate_status}" = "ok" ]; then
    git commit -m "chore(deps): bump validation tooling"
  else
    git commit -m "chore(reports): weekly reliability audit"
  fi
  git push
fi

if [ "${candidate_status}" != "ok" ]; then
  log "opening an issue for the failing gate"
  body="$(mktemp -t maintenance-issue.XXXXXX.md)"
  {
    echo "The weekly maintenance run could not keep its validation gate green."
    echo ""
    echo "- Workflow run: \`${GITHUB_RUN_URL:-local}\`"
    echo "- Pins changed in this run: ${pins_changed}"
    echo "- Status with the previous pins: ${reverted_status:-not re-run}"
    echo ""
    echo "Literal output of the candidate run:"
    echo '```'
    sed -n '/## kubeconform/,$p' "${CANDIDATE_REPORT}" | head -n 150
    echo '```'
  } >"${body}"
  if command -v gh >/dev/null 2>&1 && [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    gh issue create \
      --title "Weekly audit: validation gate failed ($(date -u +%Y-%m-%d))" \
      --body-file "${body}" \
      --label maintenance 2>/dev/null \
      || gh issue create --title "Weekly audit: validation gate failed ($(date -u +%Y-%m-%d))" --body-file "${body}"
  else
    echo "gh is not available or unauthenticated; issue body written to ${body}"
  fi
fi

log "maintenance finished (candidate status: ${candidate_status})"
