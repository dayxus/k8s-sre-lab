#!/usr/bin/env python3
"""Render every deployment shape of the lab into build/ as plain multi-document YAML.

Both delivery paths are rendered so the validators (kubeconform, kube-linter) and the
invariant suite inspect exactly the same artefacts:

    kustomize: manifests/base, manifests/overlays/dev, manifests/overlays/prod
    helm:      chart/ with values.yaml, values-dev.yaml, values-prod.yaml

Nothing here needs a cluster.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invariants import (  # noqa: E402  (import after sys.path tweak on purpose)
    REPO_ROOT,
    helm_template,
    kustomize_build,
)

BUILD_DIR = REPO_ROOT / "build"

TARGETS = (
    ("kustomize-base", "kustomize", REPO_ROOT / "manifests" / "base"),
    ("kustomize-dev", "kustomize", REPO_ROOT / "manifests" / "overlays" / "dev"),
    ("kustomize-prod", "kustomize", REPO_ROOT / "manifests" / "overlays" / "prod"),
)


def render_kustomize(path: Path) -> str:
    return kustomize_build(path)


def render_helm(values: Path, namespace: str) -> str:
    return helm_template(values, namespace)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(BUILD_DIR),
        help="directory for the rendered manifests (default: build/)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for name, _kind, path in TARGETS:
        rendered = render_kustomize(path)
        target = out_dir / ("%s.yaml" % name)
        target.write_text(rendered, encoding="utf-8")
        written.append((name, target, len(rendered.splitlines())))

    helm_targets = (
        ("helm-default", REPO_ROOT / "chart" / "values.yaml", "k8s-sre-lab"),
        ("helm-dev", REPO_ROOT / "chart" / "values-dev.yaml", "k8s-sre-lab-dev"),
        ("helm-prod", REPO_ROOT / "chart" / "values-prod.yaml", "k8s-sre-lab-prod"),
    )
    for name, values, namespace in helm_targets:
        rendered = render_helm(values, namespace)
        target = out_dir / ("%s.yaml" % name)
        target.write_text(rendered, encoding="utf-8")
        written.append((name, target, len(rendered.splitlines())))

    for name, target, lines in written:
        print("%-16s %4d lines  %s" % (name, lines, target.relative_to(REPO_ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
