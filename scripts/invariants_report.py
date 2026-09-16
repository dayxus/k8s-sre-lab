#!/usr/bin/env python3
"""Print the reliability-invariant status of every rendered deployment shape.

Same checks as tests/, but as a readable report for the terminal and for CI logs.
Exit code 1 if any invariant fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invariants import (
    INVARIANTS,
    REPO_ROOT,
    check_all,
    count_by_invariant,
    helm_template,
    kustomize_build,
)

RENDERERS = (
    ("kustomize/base", lambda: kustomize_build(REPO_ROOT / "manifests" / "base")),
    (
        "kustomize/overlays/dev",
        lambda: kustomize_build(REPO_ROOT / "manifests" / "overlays" / "dev"),
    ),
    (
        "kustomize/overlays/prod",
        lambda: kustomize_build(REPO_ROOT / "manifests" / "overlays" / "prod"),
    ),
    (
        "helm/values.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values.yaml", "k8s-sre-lab"),
    ),
    (
        "helm/values-dev.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values-dev.yaml", "k8s-sre-lab-dev"),
    ),
    (
        "helm/values-prod.yaml",
        lambda: helm_template(REPO_ROOT / "chart" / "values-prod.yaml", "k8s-sre-lab-prod"),
    ),
)


def main() -> int:
    total = 0
    failures = 0
    for name, render in RENDERERS:
        docs = [doc for doc in yaml.safe_load_all(render()) if doc]
        violations = check_all(docs)
        counts = count_by_invariant(violations)
        status = "PASS" if not violations else "FAIL"
        print("%-24s %-4s %d documents, %d violations" % (name, status, len(docs), len(violations)))
        for invariant, _fn in INVARIANTS:
            print("    %-32s %d" % (invariant, counts.get(invariant, 0)))
        for violation in violations:
            print("    %s" % violation)
        total += len(violations)
        if violations:
            failures += 1
    print("\ntotal violations: %d across %d/%d shapes" % (total, failures, len(RENDERERS)))
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
