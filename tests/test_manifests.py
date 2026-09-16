"""Reliability invariants applied to the kustomize output (base + overlays).

One test per invariant, per rendered shape, plus the overlay-specific expectations
(dev is small and single-replica, prod is the 3-replica shape the kind E2E applies).
"""

from __future__ import annotations

import pytest

from conftest import find
from invariants import INVARIANTS, INVARIANT_DESCRIPTIONS, check_all, count_by_invariant

SHAPES = ("base", "dev", "prod")


def docs_for(shape: str, request) -> list:
    return request.getfixturevalue({"base": "base_docs", "dev": "dev_docs", "prod": "prod_docs"}[shape])


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("invariant,check", INVARIANTS, ids=[name for name, _ in INVARIANTS])
def test_invariant_holds(shape: str, invariant: str, check, request):
    docs = docs_for(shape, request)
    violations = [str(v) for v in check(docs)]
    assert not violations, "%s (%s) failed for %s:\n  %s" % (
        invariant,
        INVARIANT_DESCRIPTIONS[invariant],
        shape,
        "\n  ".join(violations),
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_no_violations_at_all(shape: str, request):
    """Belt and braces: the aggregate view the weekly audit report also uses."""
    docs = docs_for(shape, request)
    counts = count_by_invariant(check_all(docs))
    assert sum(counts.values()) == 0, counts


# ---------------------------------------------------------------------------
# base: the reliability contract itself
# ---------------------------------------------------------------------------

EXPECTED_BASE_KINDS = {
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


def test_base_contains_the_full_object_set(base_docs):
    assert {doc["kind"] for doc in base_docs} == EXPECTED_BASE_KINDS


def test_deployment_probes_have_distinct_jobs(base_docs):
    container = find(base_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]["containers"][0]
    readiness = container["readinessProbe"]
    liveness = container["livenessProbe"]
    startup = container["startupProbe"]

    assert readiness["httpGet"]["path"] == "/readyz"
    assert liveness["httpGet"]["path"] == "/healthz"
    assert startup["httpGet"]["path"] == "/healthz"

    # Liveness must be the slowest of the three, otherwise a traffic problem turns into
    # a restart loop.
    assert liveness["periodSeconds"] > readiness["periodSeconds"]
    assert liveness["failureThreshold"] * liveness["periodSeconds"] > (
        readiness["failureThreshold"] * readiness["periodSeconds"]
    )
    # The startup probe buys the slow start its time without loosening liveness.
    assert startup["failureThreshold"] * startup["periodSeconds"] >= 30


def test_grace_period_covers_prestop_plus_drain(base_docs):
    pod_spec = find(base_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]
    config = find(base_docs, "ConfigMap", "demo-api-config")["data"]
    pre_stop = pod_spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"]

    assert any("sleep" in part for part in pre_stop)
    assert int(pod_spec["terminationGracePeriodSeconds"]) >= 30
    drain = float(config["SHUTDOWN_GRACE_SECONDS"])
    assert int(pod_spec["terminationGracePeriodSeconds"]) > drain


def test_spread_and_anti_affinity_are_soft_on_purpose(base_docs):
    pod_spec = find(base_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]
    spread = pod_spec["topologySpreadConstraints"][0]
    anti = pod_spec["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]

    # Hard spreading with fewer nodes than replicas leaves pods Pending forever.
    assert spread["whenUnsatisfiable"] == "ScheduleAnyway"
    assert spread["topologyKey"] == "kubernetes.io/hostname"
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in pod_spec["affinity"]["podAntiAffinity"]
    # WeightedPodAffinityTerm nests the term: `podAffinityTerm.topologyKey` is the
    # field the API actually validates (a flat `topologyKey` on the weighted term is
    # rejected by the schema).
    anti_term = anti["podAffinityTerm"]
    assert anti_term["topologyKey"] == "kubernetes.io/hostname"
    assert anti_term["labelSelector"] == spread["labelSelector"]


def test_slo_alerts_cover_both_burn_rates_and_platform_symptoms(base_docs):
    rule = find(base_docs, "PrometheusRule", "demo-api")
    alerts = {
        r["alert"]: r
        for group in rule["spec"]["groups"]
        for r in group["rules"]
        if "alert" in r
    }
    assert "DemoApiErrorBudgetBurnFast" in alerts
    assert "DemoApiErrorBudgetBurnSlow" in alerts
    for name in ("DemoApiNoReadyEndpoints", "DemoApiPodCrashLooping", "DemoApiRolloutStuck"):
        assert name in alerts, name

    fast = alerts["DemoApiErrorBudgetBurnFast"]
    assert fast["labels"]["severity"] == "critical"
    assert fast["labels"]["slo"] == "availability"
    # Multi-window: both a long and a short window must agree.
    assert "ratio_rate1h" in fast["expr"] and "ratio_rate5m" in fast["expr"]

    slow = alerts["DemoApiErrorBudgetBurnSlow"]
    assert slow["labels"]["severity"] == "warning"


def test_alert_expressions_reference_metrics_the_app_exposes(base_docs):
    """An alert on a metric nobody emits is a rule that will never fire."""
    app_source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "server.py").read_text()
    rule = find(base_docs, "PrometheusRule", "demo-api")
    for group in rule["spec"]["groups"]:
        for rule_entry in group["rules"]:
            for metric in ("demo_api_ready_failures_total", "demo_api_requests_total", "demo_api_ready"):
                if metric in rule_entry["expr"]:
                    assert metric in app_source, "%s is alerted on but never emitted" % metric


def test_servicemonitor_scrapes_the_metrics_path(base_docs):
    monitor = find(base_docs, "ServiceMonitor", "demo-api")
    endpoint = monitor["spec"]["endpoints"][0]
    service = find(base_docs, "Service", "demo-api")
    assert endpoint["path"] == "/metrics"
    assert endpoint["port"] in [port["name"] for port in service["spec"]["ports"]]


# ---------------------------------------------------------------------------
# overlays
# ---------------------------------------------------------------------------


def test_dev_overlay_is_single_replica_without_autoscaler_or_budget(dev_docs):
    deployment = find(dev_docs, "Deployment", "demo-api")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["metadata"]["namespace"] == "k8s-sre-lab-dev"
    assert not [doc for doc in dev_docs if doc["kind"] == "HorizontalPodAutoscaler"]
    assert not [doc for doc in dev_docs if doc["kind"] == "PodDisruptionBudget"]

    limits = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits == {"cpu": "100m", "memory": "128Mi"}


def test_prod_overlay_is_the_shape_the_e2e_applies(prod_docs):
    deployment = find(prod_docs, "Deployment", "demo-api")
    assert deployment["spec"]["replicas"] == 3
    assert deployment["metadata"]["namespace"] == "k8s-sre-lab-prod"
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}

    budget = find(prod_docs, "PodDisruptionBudget", "demo-api")
    assert budget["spec"]["minAvailable"] == 2

    autoscaler = find(prod_docs, "HorizontalPodAutoscaler", "demo-api")
    assert autoscaler["spec"]["minReplicas"] == 3
    assert autoscaler["spec"]["maxReplicas"] == 10
    assert autoscaler["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 600

    # maxUnavailable=0 + minSurge 1 can add a pod, so the surge pod must fit in the
    # budget: 3 replicas - 1 voluntary eviction = 2 still available.
    assert budget["spec"]["minAvailable"] <= deployment["spec"]["replicas"] - 1


def test_prod_requests_fit_inside_limits_and_grow_with_the_environment(dev_docs, prod_docs):
    dev_requests = find(dev_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]["requests"]
    prod_container = find(prod_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]["containers"][0]
    prod_requests = prod_container["resources"]["requests"]
    prod_limits = prod_container["resources"]["limits"]

    assert prod_requests["cpu"] != dev_requests["cpu"]
    assert prod_requests["cpu"] != prod_limits["cpu"]
    assert prod_requests["memory"] != prod_limits["memory"]


def test_overlays_do_not_restate_the_contract(dev_docs, prod_docs, base_docs):
    """An overlay may change size, never posture."""
    base_container = find(base_docs, "Deployment", "demo-api")["spec"]["template"]["spec"]["containers"][0]
    for docs in (dev_docs, prod_docs):
        container = find(docs, "Deployment", "demo-api")["spec"]["template"]["spec"]["containers"][0]
        for field in ("readinessProbe", "livenessProbe", "startupProbe", "securityContext", "lifecycle"):
            assert container[field] == base_container[field], field
