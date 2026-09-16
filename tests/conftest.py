"""Shared pytest fixtures: render the manifests once and reuse the parsed documents."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import invariants


def _load(rendered: str) -> List[Dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _kustomize(name: str) -> List[Dict[str, Any]]:
    return _load(invariants.kustomize_build(REPO_ROOT / "manifests" / name))


@pytest.fixture(scope="session")
def base_docs() -> List[Dict[str, Any]]:
    return _kustomize("base")


@pytest.fixture(scope="session")
def dev_docs() -> List[Dict[str, Any]]:
    return _kustomize("overlays/dev")


@pytest.fixture(scope="session")
def prod_docs() -> List[Dict[str, Any]]:
    return _kustomize("overlays/prod")


@pytest.fixture(scope="session")
def helm_default_docs() -> List[Dict[str, Any]]:
    return _load(invariants.helm_template(REPO_ROOT / "chart" / "values.yaml", "k8s-sre-lab"))


@pytest.fixture(scope="session")
def helm_dev_docs() -> List[Dict[str, Any]]:
    return _load(
        invariants.helm_template(REPO_ROOT / "chart" / "values-dev.yaml", "k8s-sre-lab-dev")
    )


@pytest.fixture(scope="session")
def helm_prod_docs() -> List[Dict[str, Any]]:
    return _load(
        invariants.helm_template(REPO_ROOT / "chart" / "values-prod.yaml", "k8s-sre-lab-prod")
    )


def find(docs: List[Dict[str, Any]], kind: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Return the single document of a kind (optionally by name)."""
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind and (name is None or doc["metadata"]["name"] == name)
    ]
    assert matches, "no %s named %r in the rendered set" % (kind, name)
    assert len(matches) == 1, "expected exactly one %s named %r, found %d" % (
        kind,
        name,
        len(matches),
    )
    return matches[0]
