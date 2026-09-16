"""The Helm chart must reproduce the kustomize contract, not just resemble it.

`helm template` output is fed through the exact same invariant suite as the kustomize
output (scripts/invariants.py), and the reliability-critical fields of the pod spec are
compared field by field between the two delivery paths. If the chart drifts from the
manifests, one of these fails.
"""

from __future__ import annotations

import pytest
from conftest import REPO_ROOT, find

from invariants import (
    INVARIANT_DESCRIPTIONS,
    INVARIANTS,
    check_all,
    count_by_invariant,
    run,
    tool_path,
)

SHAPES = ("helm_default", "helm_dev", "helm_prod")


def docs_for(shape: str, request) -> list:
    return request.getfixturevalue(shape + "_docs")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("invariant,check", INVARIANTS, ids=[name for name, _ in INVARIANTS])
def test_chart_invariant_holds(shape: str, invariant: str, check, request):
    docs = docs_for(shape, request)
    violations = [str(v) for v in check(docs)]
    assert not violations, "%s (%s) failed for %s:\n  %s" % (
        invariant,
        INVARIANT_DESCRIPTIONS[invariant],
        shape,
        "\n  ".join(violations),
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_chart_has_no_violations_at_all(shape: str, request):
    counts = count_by_invariant(check_all(docs_for(shape, request)))
    assert sum(counts.values()) == 0, counts


@pytest.mark.parametrize("values", ["values.yaml", "values-dev.yaml", "values-prod.yaml"])
def test_helm_lint_is_clean(values: str):
    output = run([tool_path("helm"), "lint", "chart", "-f", "chart/%s" % values])
    assert "0 chart(s) failed" in output, output


def test_chart_metadata_matches_the_pinned_image_tag():
    """Chart.yaml appVersion and the image tag come from tools/versions.env."""
    app_tag = None
    for line in (REPO_ROOT / "tools" / "versions.env").read_text().splitlines():
        if line.startswith("APP_IMAGE_TAG="):
            app_tag = line.split("=", 1)[1].strip()
    assert app_tag, "APP_IMAGE_TAG is missing from tools/versions.env"

    chart = (REPO_ROOT / "chart" / "Chart.yaml").read_text()
    assert 'appVersion: "%s"' % app_tag in chart
    assert 'tag: "%s"' % app_tag in (REPO_ROOT / "chart" / "values.yaml").read_text()


def test_helm_prod_mirrors_the_prod_overlay(helm_prod_docs, prod_docs):
    """The two delivery paths must agree on the fields that carry the contract."""
    helm_deployment = find(helm_prod_docs, "Deployment", "k8s-sre-lab")
    kustomize_deployment = find(prod_docs, "Deployment", "demo-api")

    assert helm_deployment["spec"]["replicas"] == kustomize_deployment["spec"]["replicas"] == 3
    assert (
        helm_deployment["spec"]["strategy"]["rollingUpdate"]
        == kustomize_deployment["spec"]["strategy"]["rollingUpdate"]
    )

    helm_container = helm_deployment["spec"]["template"]["spec"]["containers"][0]
    kustomize_container = kustomize_deployment["spec"]["template"]["spec"]["containers"][0]
    # Names differ (k8s-sre-lab-* vs demo-api-*) because Helm prefixes with the release
    # name; everything that carries reliability semantics must be identical.
    for field in (
        "image",
        "imagePullPolicy",
        "readinessProbe",
        "livenessProbe",
        "startupProbe",
        "securityContext",
        "lifecycle",
        "resources",
    ):
        assert helm_container[field] == kustomize_container[field], "%s differs" % field

    helm_pod = helm_deployment["spec"]["template"]["spec"]
    kustomize_pod = kustomize_deployment["spec"]["template"]["spec"]
    for field in (
        "terminationGracePeriodSeconds",
        "securityContext",
        "automountServiceAccountToken",
        "topologySpreadConstraints",
        "affinity",
        "volumes",
    ):
        assert helm_pod[field] == kustomize_pod[field], "%s differs" % field

    helm_budget = find(helm_prod_docs, "PodDisruptionBudget", "k8s-sre-lab")
    assert (
        helm_budget["spec"]["minAvailable"]
        == find(prod_docs, "PodDisruptionBudget", "demo-api")["spec"]["minAvailable"]
    )


def test_helm_dev_mirrors_the_dev_overlay(helm_dev_docs, dev_docs):
    helm_deployment = find(helm_dev_docs, "Deployment", "k8s-sre-lab")
    kustomize_deployment = find(dev_docs, "Deployment", "demo-api")

    assert helm_deployment["spec"]["replicas"] == kustomize_deployment["spec"]["replicas"] == 1
    assert not [doc for doc in helm_dev_docs if doc["kind"] == "HorizontalPodAutoscaler"]
    assert not [doc for doc in helm_dev_docs if doc["kind"] == "PodDisruptionBudget"]
    assert (
        helm_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
        == kustomize_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
    )


def test_autoscaling_is_off_by_default_and_on_in_prod(helm_default_docs, helm_prod_docs):
    """A single-replica default must not ship an autoscaler that fights the replica count."""
    assert not [doc for doc in helm_default_docs if doc["kind"] == "HorizontalPodAutoscaler"]
    autoscaler = find(helm_prod_docs, "HorizontalPodAutoscaler", "k8s-sre-lab")
    assert autoscaler["spec"]["minReplicas"] == 3
    assert autoscaler["spec"]["maxReplicas"] == 10
    assert autoscaler["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 600


def test_chart_templates_reference_every_policy_object(helm_prod_docs):
    kinds = {doc["kind"] for doc in helm_prod_docs}
    assert kinds == {
        "Namespace",
        "ConfigMap",
        "PriorityClass",
        "Deployment",
        "Service",
        "PodDisruptionBudget",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "ServiceMonitor",
        "PrometheusRule",
    }
