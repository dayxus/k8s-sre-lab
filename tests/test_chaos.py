"""The chaos experiments are configuration, so they get configuration tests.

They cannot be executed in CI without a Chaos Mesh installation (see
docs/chaos-experiments.md), but they can be checked for the mistakes that make an
experiment silently do nothing: no duration, a selector that matches nothing, a missing
label set, or a documented hypothesis that is not written down anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT
from invariants import LABELS_REQUIRED

CHAOS_DIR = REPO_ROOT / "chaos"
EXPERIMENTS = ("pod-delete.yaml", "cpu-stress.yaml", "network-latency.yaml")
EXPECTED_KINDS = {"pod-delete.yaml": "PodChaos", "cpu-stress.yaml": "StressChaos", "network-latency.yaml": "NetworkChaos"}
TARGET_LABELS = {
    "app.kubernetes.io/name": "demo-api",
    "app.kubernetes.io/instance": "k8s-sre-lab",
}


def load(name: str) -> dict:
    documents = [doc for doc in yaml.safe_load_all((CHAOS_DIR / name).read_text()) if doc]
    assert len(documents) == 1, "%s must hold exactly one document" % name
    return documents[0]


@pytest.mark.parametrize("name", EXPERIMENTS)
def test_experiment_exists_and_uses_one_style(name: str):
    document = load(name)
    assert document["kind"] == EXPECTED_KINDS[name]
    # One style only: Chaos Mesh CRs, never a mix with Litmus.
    assert document["apiVersion"] == "chaos-mesh.org/v1alpha1"


@pytest.mark.parametrize("name", EXPERIMENTS)
def test_experiment_targets_the_prod_workload(name: str):
    document = load(name)
    selector = document["spec"]["selector"]
    assert selector["namespaces"] == ["k8s-sre-lab-prod"]
    for label, value in TARGET_LABELS.items():
        assert selector["labelSelectors"][label] == value, "%s selector label %s" % (name, label)
    assert document["metadata"]["namespace"] == "k8s-sre-lab-prod"


@pytest.mark.parametrize("name", EXPERIMENTS)
def test_experiment_carries_the_standard_labels(name: str):
    labels = load(name)["metadata"]["labels"]
    missing = [label for label in LABELS_REQUIRED if label not in labels]
    assert not missing, "%s is missing %s" % (name, missing)


def test_pod_delete_is_bounded_and_reversible():
    spec = load("pod-delete.yaml")["spec"]
    assert spec["action"] == "pod-kill"
    assert spec["mode"] == "one"
    # A grace period below the 10s preStop sleep would measure SIGKILL, not shutdown.
    assert spec["gracePeriod"] >= 10


def test_cpu_stress_is_bounded_in_time_and_load():
    spec = load("cpu-stress.yaml")["spec"]
    assert spec["duration"].endswith("m")
    assert spec["mode"] == "one"
    cpu = spec["stressors"]["cpu"]
    assert cpu["workers"] >= 1
    assert 0 < cpu["load"] <= 100


def test_network_latency_is_bounded_and_has_a_target():
    spec = load("network-latency.yaml")["spec"]
    assert spec["action"] == "delay"
    assert spec["direction"] in ("to", "from", "both")
    assert spec["duration"].endswith("m")
    assert spec["delay"]["latency"].endswith("ms")
    # netem needs a target; a delay experiment without one applies to nothing.
    assert spec["target"]["selector"]["labelSelectors"]["app.kubernetes.io/name"] == "demo-api"


def test_every_experiment_is_documented_with_a_rollback():
    readme = (CHAOS_DIR / "README.md").read_text()
    for name in EXPERIMENTS:
        assert name in readme, "%s is not referenced from chaos/README.md" % name
    for required_phrase in ("Hypothesis", "Success criterion", "Rollback", "measure"):
        assert required_phrase.lower() in readme.lower(), "chaos/README.md never mentions %s" % required_phrase


def test_chaos_directory_has_no_stray_files():
    files = sorted(path.name for path in CHAOS_DIR.iterdir() if path.is_file())
    assert files == sorted(list(EXPERIMENTS) + ["README.md"]), files


def test_no_chaos_manifest_is_a_kustomize_resource():
    """Chaos is applied by an operator, never by `kubectl apply -k manifests/overlays/prod`."""
    for kustomization in (REPO_ROOT / "manifests").rglob("kustomization.yaml"):
        text = kustomization.read_text()
        assert "chaos/" not in text, "%s pulls in a chaos manifest" % kustomization
        assert "chaos-" not in text
    chaos_names = {str(Path(name).stem) for name in EXPERIMENTS}
    rendered = (REPO_ROOT / "manifests" / "base" / "kustomization.yaml").read_text()
    for name in chaos_names:
        assert name not in rendered
