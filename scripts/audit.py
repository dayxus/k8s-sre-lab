#!/usr/bin/env python3
"""Weekly audit: re-validate the lab with the current tooling and write a report.

Run by .github/workflows/maintenance.yml (and runnable locally). It answers the
question the lab exists to ask - "do the manifests still satisfy the contract, with
today's Kubernetes and today's linters?" - and writes the answer to
reports/weekly-audit.md:

  * the pinned versions in play,
  * kubeconform against the current stable Kubernetes version (API deprecations),
  * kube-linter,
  * the reliability invariant counts, per invariant,
  * a link check over the documentation.

Exit code is 0 unless the report could not be written; the tool results are in the
report itself, and the workflow decides what to do with them.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from invariants import (  # noqa: E402
    INVARIANT_DESCRIPTIONS,
    INVARIANTS,
    check_all,
    count_by_invariant,
    helm_template,
    kustomize_build,
    tool_path,
)

REPORTS_DIR = REPO_ROOT / "reports"
CRD_SCHEMA_LOCATION = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
LINK_RE = re.compile(r"(?<![\w\"'(])(https?://[^\s<>)\]]+)")
BADGE_HOSTS = ("img.shields.io", "shields.io", "badge")


def display_path(path: Path) -> str:
    """Render a path relative to the repository, or absolute when it lives outside it.

    The weekly maintenance workflow writes the candidate report to a ``mktemp`` file
    outside the checkout, so a bare ``relative_to(REPO_ROOT)`` would raise there.
    """
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


SHAPES = (
    (
        "kustomize/overlays/dev",
        lambda: kustomize_build(REPO_ROOT / "manifests" / "overlays" / "dev"),
    ),
    (
        "kustomize/overlays/prod",
        lambda: kustomize_build(REPO_ROOT / "manifests" / "overlays" / "prod"),
    ),
    ("kustomize/base", lambda: kustomize_build(REPO_ROOT / "manifests" / "base")),
    (
        "helm/values-dev.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values-dev.yaml", "k8s-sre-lab-dev"),
    ),
    (
        "helm/values-prod.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values-prod.yaml", "k8s-sre-lab-prod"),
    ),
    (
        "helm/values.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values.yaml", "k8s-sre-lab"),
    ),
)


def read_versions() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in (REPO_ROOT / "tools" / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def capture(cmd: Sequence[str]) -> Tuple[int, str]:
    completed = subprocess.run(
        list(cmd), cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace")


def audit_invariants() -> Tuple[Dict[str, Dict[str, int]], List[str]]:
    counts: Dict[str, Dict[str, int]] = {}
    details: List[str] = []
    for name, render in SHAPES:
        docs = [doc for doc in yaml.safe_load_all(render()) if doc]
        violations = check_all(docs)
        counts[name] = count_by_invariant(violations)
        for violation in violations:
            details.append("%s %s" % (name, violation))
    return counts, details


def check_links() -> List[Tuple[str, str, int, str]]:
    """Return (severity, url, status, source file) for every absolute link in docs."""
    results: List[Tuple[str, str, int, str]] = []
    seen = set()
    files = [REPO_ROOT / "README.md", REPO_ROOT / "README.pt-BR.md"]
    files += sorted((REPO_ROOT / "docs").glob("*.md"))
    files += sorted((REPO_ROOT / "chaos").glob("*.md"))

    for path in files:
        if not path.exists():
            continue
        for url in LINK_RE.findall(path.read_text(encoding="utf-8")):
            url = url.rstrip(".,;:")
            if url in seen:
                continue
            seen.add(url)
            badge = any(host in url for host in BADGE_HOSTS)
            status = 0
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "k8s-sre-lab-audit"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    status = response.status
            except urllib.error.HTTPError as error:
                status = error.code
            except Exception:
                status = 0
            if status == 0 or status >= 400:
                severity = "warning" if badge else "failure"
                results.append((severity, url, status, str(path.relative_to(REPO_ROOT))))
    return results


def write_report(
    versions: Dict[str, str],
    kubeconform_exit: int,
    kubeconform_out: str,
    linter_exit: int,
    linter_out: str,
    counts: Dict[str, Dict[str, int]],
    details: List[str],
    links: List[Tuple[str, str, int, str]],
    extended: Tuple[int, str],
) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_violations = sum(sum(shape.values()) for shape in counts.values())
    broken = [link for link in links if link[0] == "failure"]
    status = (
        "ok"
        if (
            kubeconform_exit == 0
            and linter_exit == 0
            and extended[0] == 0
            and total_violations == 0
            and not broken
        )
        else "attention"
    )

    lines = [
        "# Weekly audit",
        "",
        "Generated by `scripts/audit.py` (workflow: `.github/workflows/maintenance.yml`).",
        "",
        "- **Run:** %s" % now,
        "- **Status:** `%s`" % status,
        "- **Kubernetes version used for schema checks:** `%s`"
        % versions.get("KUBERNETES_VERSION", "?"),
        "- **kind:** `%s`" % versions.get("KIND_VERSION", "?"),
        "- **kubeconform:** `%s` (exit %d)"
        % (versions.get("KUBECONFORM_VERSION", "?"), kubeconform_exit),
        "- **kube-linter:** `%s` (exit %d)"
        % (versions.get("KUBE_LINTER_VERSION", "?"), linter_exit),
        "- **pytest suite:** exit %d" % extended[0],
        "- **Invariant violations:** %d" % total_violations,
        "- **Broken document links:** %d" % len(broken),
        "",
        "## Invariant counts per rendered shape",
        "",
        "| Invariant | " + " | ".join(name for name, _ in SHAPES) + " |",
        "| --- | " + " | ".join("---" for _ in SHAPES) + " |",
    ]
    for invariant, _fn in INVARIANTS:
        row = [str(counts[name].get(invariant, 0)) for name, _ in SHAPES]
        lines.append("| `%s` | %s |" % (invariant, " | ".join(row)))
    total_row = [str(sum(counts[name].values())) for name, _ in SHAPES]
    lines.append("| **total** | %s |" % " | ".join(total_row))
    lines += [
        "",
        "Each column is a fully rendered deployment shape: `kustomize build` for the",
        "three kustomize targets, `helm template` for the three value sets.",
        "",
    ]

    if details:
        lines += ["### Violations", "", "```"]
        lines += details
        lines += ["```", ""]

    lines += ["## Invariant descriptions", ""]
    for invariant, _fn in INVARIANTS:
        lines.append("- `%s` - %s" % (invariant, INVARIANT_DESCRIPTIONS[invariant]))
    lines.append("")

    lines += [
        "## kubeconform (`-strict`, Kubernetes %s)" % versions.get("KUBERNETES_VERSION", "?"),
        "",
    ]
    lines += ["```", kubeconform_out.strip() or "(no output)", "```", ""]
    lines += ["## kube-linter", "", "```", linter_out.strip() or "(no output)", "```", ""]
    lines += ["## pytest", "", "```", extended[1].strip() or "(no output)", "```", ""]

    lines += ["## Documentation links", ""]
    if not links:
        lines.append("Every absolute link in `README.md`, `README.pt-BR.md` and `docs/` answered.")
    else:
        lines += ["| Severity | Link | Status | File |", "| --- | --- | --- | --- |"]
        for severity, url, status_code, source in links:
            lines.append(
                "| %s | %s | %s | `%s` |" % (severity, url, status_code or "unreachable", source)
            )
    lines += [
        "",
        "---",
        "",
        "This file is committed by the scheduled workflow only when it changes.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(REPORTS_DIR / "weekly-audit.md"))
    parser.add_argument("--skip-network", action="store_true", help="skip the link check")
    parser.add_argument("--skip-suite", action="store_true", help="skip the pytest run")
    args = parser.parse_args()

    versions = read_versions()
    build_dir = REPO_ROOT / "build"
    render_exit, render_out = capture(
        [sys.executable, "scripts/render.py", "--out", str(build_dir)]
    )
    if render_exit != 0:
        print(render_out, file=sys.stderr)
        return 1

    targets = [
        str(build_dir / "kustomize-dev.yaml"),
        str(build_dir / "kustomize-prod.yaml"),
        str(build_dir / "kustomize-base.yaml"),
        str(build_dir / "helm-dev.yaml"),
        str(build_dir / "helm-prod.yaml"),
        str(build_dir / "helm-default.yaml"),
    ]

    kubeconform_args = [
        tool_path("kubeconform"),
        "-strict",
        "-summary",
        "-kubernetes-version",
        # kubeconform expects a bare x.y.z version, the pins carry the `v` prefix.
        str(versions.get("KUBERNETES_VERSION", "v1.37.0")).lstrip("v"),
        "-schema-location",
        "default",
        "-schema-location",
        CRD_SCHEMA_LOCATION,
    ]
    kubeconform_exit, kubeconform_out = capture(kubeconform_args + targets)
    linter_exit, linter_out = capture(
        [tool_path("kube-linter"), "lint", "--config", ".kube-linter.yaml", *targets]
    )
    counts, details = audit_invariants()
    links = [] if args.skip_network else check_links()
    if args.skip_suite:
        extended = (0, "skipped")
    else:
        extended = capture([sys.executable, "-m", "pytest", "-q"])

    report = write_report(
        versions,
        kubeconform_exit,
        kubeconform_out,
        linter_exit,
        linter_out,
        counts,
        details,
        links,
        extended,
    )
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")

    broken = len([link for link in links if link[0] == "failure"])
    total_violations = sum(sum(shape.values()) for shape in counts.values())
    failed = (
        kubeconform_exit != 0 or linter_exit != 0 or extended[0] != 0 or total_violations or broken
    )
    print("AUDIT_STATUS=%s" % ("failed" if failed else "ok"))
    print("report: %s" % display_path(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
