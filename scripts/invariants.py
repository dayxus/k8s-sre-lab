"""Reliability invariants for the k8s-sre-lab manifests and chart.

This module is the single definition of "the lab's reliability contract". Three
consumers share it, so a manifest cannot pass one gate and fail another:

* ``tests/test_manifests.py`` - kustomize output (base + overlays)
* ``tests/test_chart.py``     - ``helm template`` output
* ``scripts/audit.py``        - the weekly audit report produced by maintenance.yml

Each invariant is a function that receives a rendered document set and yields
:class:`Violation` records. Nothing here talks to a cluster: the E2E in
``scripts/e2e-kind.sh`` is what proves the contract holds at runtime.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / ".tools"

# The five labels recommended by the Kubernetes API conventions, plus the
# managed-by label used to tell kustomize and Helm output apart.
LABELS_REQUIRED: Tuple[str, ...] = (
    "app.kubernetes.io/name",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/version",
    "app.kubernetes.io/component",
    "app.kubernetes.io/part-of",
)
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_ALLOWED = ("kustomize", "Helm")

# Workloads and policies that the invariants apply to.
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
POLICY_KINDS = ("PodDisruptionBudget", "HorizontalPodAutoscaler", "NetworkPolicy")
LABELLED_KINDS = (
    WORKLOAD_KINDS
    + POLICY_KINDS
    + (
        "Service",
        "ConfigMap",
        "Namespace",
        "PriorityClass",
        "ServiceMonitor",
        "PrometheusRule",
    )
)

FLOATING_TAGS = ("latest", "edge", "stable", "master", "main", "dev", "develop")

# Prevents a floating tag from sneaking in as `repo:latest` with a digest-like suffix.
IMAGE_RE = re.compile(
    r"^(?P<repo>[^:]+)(?::(?P<tag>[^:@]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)

MIN_TERMINATION_GRACE_SECONDS = 30
MIN_SCALE_DOWN_STABILIZATION_SECONDS = 300
MIN_HPA_REPLICAS = 3


@dataclass(frozen=True)
class Violation:
    """A single failed invariant, with enough context to fix it without guessing."""

    invariant: str
    resource: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - formatting helper
        return "[%s] %s: %s" % (self.invariant, self.resource, self.message)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def resource_id(doc: Dict[str, Any]) -> str:
    return "%s/%s" % (doc.get("kind", "?"), doc.get("metadata", {}).get("name", "?"))


def iter_containers(doc: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(container name, container spec)`` for every container in a workload."""
    template = doc.get("spec", {}).get("template", {})
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    for container in pod_spec.get("containers", []) or []:
        yield container.get("name", "?"), container
    for container in pod_spec.get("initContainers", []) or []:
        yield container.get("name", "? (init)"), container


def pod_spec_of(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    template = doc.get("spec", {}).get("template")
    if isinstance(template, dict):
        return template.get("spec", {})
    return None


def walk(node: Any, key: str) -> Iterator[Any]:
    """Yield every value stored under ``key`` anywhere in a nested structure."""
    if isinstance(node, dict):
        for node_key, value in node.items():
            if node_key == key:
                yield value
            yield from walk(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, key)


def parse_cpu_quantity(value: Any) -> float:
    """CPU quantity in cores. Only the units this repo uses are supported."""
    text = str(value)
    if text.endswith("m"):
        return float(text[:-1]) / 1000.0
    return float(text)


def parse_memory_quantity(value: Any) -> float:
    """Memory quantity in mebibytes."""
    text = str(value)
    units = {"Ki": 1.0 / 1024.0, "Mi": 1.0, "Gi": 1024.0}
    for suffix, factor in units.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text) / (1024.0 * 1024.0)


# ---------------------------------------------------------------------------
# invariant 1: probes and resource requirements
# ---------------------------------------------------------------------------


def inv1_probes_and_resources(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        containers = list(iter_containers(doc))
        if not containers:
            out.append(Violation("INV1_probes_and_resources", rid, "workload has no containers"))
            continue
        for name, container in containers:
            for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
                if not container.get(probe):
                    out.append(
                        Violation(
                            "INV1_probes_and_resources",
                            rid,
                            "container %r has no %s (the app needs a start window, a traffic "
                            "gate and a restart signal - all three)" % (name, probe),
                        )
                    )
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            for field in ("cpu", "memory"):
                if field not in requests:
                    out.append(
                        Violation(
                            "INV1_probes_and_resources",
                            rid,
                            "container %r declares no resources.requests.%s" % (name, field),
                        )
                    )
                if field not in limits:
                    out.append(
                        Violation(
                            "INV1_probes_and_resources",
                            rid,
                            "container %r declares no resources.limits.%s" % (name, field),
                        )
                    )
            if (
                "cpu" in requests
                and "cpu" in limits
                and parse_cpu_quantity(limits["cpu"]) < parse_cpu_quantity(requests["cpu"])
            ):
                out.append(
                    Violation(
                        "INV1_probes_and_resources",
                        rid,
                        "container %r has a cpu limit below its request" % name,
                    )
                )
            if (
                "memory" in requests
                and "memory" in limits
                and parse_memory_quantity(limits["memory"])
                < parse_memory_quantity(requests["memory"])
            ):
                out.append(
                    Violation(
                        "INV1_probes_and_resources",
                        rid,
                        "container %r has a memory limit below its request" % name,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# invariant 2: graceful shutdown
# ---------------------------------------------------------------------------


def inv2_graceful_shutdown(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    config = {}
    for doc in docs:
        if doc.get("kind") == "ConfigMap":
            config = doc.get("data") or {}

    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        pod_spec = pod_spec_of(doc) or {}
        grace = pod_spec.get("terminationGracePeriodSeconds")
        if grace is None:
            out.append(
                Violation("INV2_graceful_shutdown", rid, "terminationGracePeriodSeconds is unset")
            )
        elif int(grace) < MIN_TERMINATION_GRACE_SECONDS:
            out.append(
                Violation(
                    "INV2_graceful_shutdown",
                    rid,
                    "terminationGracePeriodSeconds=%s is below the %ds floor"
                    % (grace, MIN_TERMINATION_GRACE_SECONDS),
                )
            )
        for name, container in iter_containers(doc):
            lifecycle = container.get("lifecycle") or {}
            if not lifecycle.get("preStop"):
                out.append(
                    Violation(
                        "INV2_graceful_shutdown",
                        rid,
                        "container %r has no preStop hook; the endpoint and the pod would "
                        "disappear at the same instant" % name,
                    )
                )

        # Cross-check the drain budget against the pod-level budget: a preStop sleep
        # plus an application drain longer than the grace period ends in SIGKILL.
        if grace is not None and "SHUTDOWN_GRACE_SECONDS" in config:
            pre_stop_sleep = 0
            for hook in walk(pod_spec, "preStop"):
                for command in walk(hook, "command"):
                    if isinstance(command, list):
                        for part in command:
                            match = re.search(r"sleep\s+(\d+)", str(part))
                            if match:
                                pre_stop_sleep = max(pre_stop_sleep, int(match.group(1)))
            drain = float(config["SHUTDOWN_GRACE_SECONDS"])
            if pre_stop_sleep + drain > int(grace):
                out.append(
                    Violation(
                        "INV2_graceful_shutdown",
                        rid,
                        "preStop sleep %ds + SHUTDOWN_GRACE_SECONDS %ds exceeds "
                        "terminationGracePeriodSeconds %ds: the drain would be SIGKILLed"
                        % (pre_stop_sleep, drain, int(grace)),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# invariant 3: rollout strategy
# ---------------------------------------------------------------------------


def inv3_rollout_safety(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        strategy = doc.get("spec", {}).get("strategy") or {}
        if strategy.get("type") != "RollingUpdate":
            out.append(
                Violation(
                    "INV3_rollout_safety",
                    rid,
                    "strategy.type is %r; a replace-style update is an outage"
                    % strategy.get("type"),
                )
            )
        rolling = strategy.get("rollingUpdate") or {}
        if rolling.get("maxUnavailable") != 0:
            out.append(
                Violation(
                    "INV3_rollout_safety",
                    rid,
                    "rollingUpdate.maxUnavailable=%r; must be 0 so a rollout never removes "
                    "capacity before the replacement is ready" % rolling.get("maxUnavailable"),
                )
            )
        deadline = doc.get("spec", {}).get("progressDeadlineSeconds")
        if deadline is None:
            out.append(
                Violation(
                    "INV3_rollout_safety",
                    rid,
                    "progressDeadlineSeconds is unset; a stuck rollout would never be reported",
                )
            )
    return out


# ---------------------------------------------------------------------------
# invariant 4: security context
# ---------------------------------------------------------------------------


def inv4_security_context(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        pod_spec = pod_spec_of(doc) or {}
        pod_security = pod_spec.get("securityContext") or {}
        if pod_security.get("runAsNonRoot") is not True:
            out.append(
                Violation(
                    "INV4_security_context", rid, "pod securityContext.runAsNonRoot is not true"
                )
            )
        seccomp = (pod_security.get("seccompProfile") or {}).get("type")
        if seccomp not in ("RuntimeDefault", "Localhost"):
            out.append(
                Violation(
                    "INV4_security_context",
                    rid,
                    "pod seccompProfile.type is %r; expected RuntimeDefault or Localhost" % seccomp,
                )
            )
        for name, container in iter_containers(doc):
            security = container.get("securityContext") or {}
            if security.get("allowPrivilegeEscalation") is not False:
                out.append(
                    Violation(
                        "INV4_security_context",
                        rid,
                        "container %r does not set allowPrivilegeEscalation: false" % name,
                    )
                )
            if security.get("readOnlyRootFilesystem") is not True:
                out.append(
                    Violation(
                        "INV4_security_context",
                        rid,
                        "container %r does not set readOnlyRootFilesystem: true" % name,
                    )
                )
            drops = (security.get("capabilities") or {}).get("drop") or []
            if "ALL" not in drops:
                out.append(
                    Violation(
                        "INV4_security_context",
                        rid,
                        "container %r does not drop ALL capabilities (drop=%r)" % (name, drops),
                    )
                )
            if security.get("privileged") is True:
                out.append(
                    Violation("INV4_security_context", rid, "container %r is privileged" % name)
                )
    return out


# ---------------------------------------------------------------------------
# invariant 5: image hygiene
# ---------------------------------------------------------------------------


def inv5_image_hygiene(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        for name, container in iter_containers(doc):
            image = container.get("image")
            if not image:
                out.append(Violation("INV5_image_hygiene", rid, "container %r has no image" % name))
                continue
            match = IMAGE_RE.match(str(image))
            tag = match.group("tag") if match else None
            digest = match.group("digest") if match else None
            if not tag and not digest:
                out.append(
                    Violation(
                        "INV5_image_hygiene",
                        rid,
                        "container %r image %r has neither a tag nor a digest" % (name, image),
                    )
                )
            if tag and tag.lower() in FLOATING_TAGS:
                out.append(
                    Violation(
                        "INV5_image_hygiene",
                        rid,
                        "container %r uses the floating tag %r" % (name, tag),
                    )
                )
            if container.get("imagePullPolicy") != "IfNotPresent":
                out.append(
                    Violation(
                        "INV5_image_hygiene",
                        rid,
                        "container %r has imagePullPolicy=%r; a pinned tag with an "
                        "always-pull policy is a registry outage waiting to happen"
                        % (name, container.get("imagePullPolicy")),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# invariant 6: disruption budget and autoscaler behaviour
# ---------------------------------------------------------------------------


def inv6_disruption_and_autoscaling(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    hpas = [d for d in docs if d.get("kind") == "HorizontalPodAutoscaler"]

    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        replicas = doc.get("spec", {}).get("replicas", 1)
        selector = ((doc.get("spec", {}).get("selector") or {}).get("matchLabels")) or {}
        if isinstance(replicas, int) and replicas > 1:
            match = [
                pdb
                for pdb in pdbs
                if (pdb.get("spec", {}).get("selector", {}).get("matchLabels") or {}) == selector
            ]
            if not match:
                out.append(
                    Violation(
                        "INV6_disruption_and_autoscaling",
                        rid,
                        "runs %d replicas but no PodDisruptionBudget selects them" % replicas,
                    )
                )

    for pdb in pdbs:
        rid = resource_id(pdb)
        spec = pdb.get("spec", {})
        if "minAvailable" not in spec and "maxUnavailable" not in spec:
            out.append(
                Violation(
                    "INV6_disruption_and_autoscaling",
                    rid,
                    "neither minAvailable nor maxUnavailable is set: the budget allows "
                    "evicting everything",
                )
            )
        if spec.get("maxUnavailable") == 0:
            out.append(
                Violation(
                    "INV6_disruption_and_autoscaling",
                    rid,
                    "maxUnavailable=0 means no voluntary disruption is ever allowed, "
                    "which blocks every node drain",
                )
            )
        if not spec.get("selector", {}).get("matchLabels"):
            out.append(Violation("INV6_disruption_and_autoscaling", rid, "no matchLabels selector"))

    for hpa in hpas:
        rid = resource_id(hpa)
        spec = hpa.get("spec", {})
        minimum = spec.get("minReplicas")
        maximum = spec.get("maxReplicas")
        if minimum is None or maximum is None:
            out.append(
                Violation("INV6_disruption_and_autoscaling", rid, "minReplicas/maxReplicas missing")
            )
        else:
            if minimum < MIN_HPA_REPLICAS:
                out.append(
                    Violation(
                        "INV6_disruption_and_autoscaling",
                        rid,
                        "minReplicas=%d is below %d: scale-in could drop the service below a "
                        "single voluntary disruption" % (minimum, MIN_HPA_REPLICAS),
                    )
                )
            if maximum <= minimum:
                out.append(
                    Violation(
                        "INV6_disruption_and_autoscaling",
                        rid,
                        "maxReplicas=%d is not above minReplicas=%d" % (maximum, minimum),
                    )
                )
        behavior = spec.get("behavior")
        if not behavior:
            out.append(
                Violation(
                    "INV6_disruption_and_autoscaling",
                    rid,
                    "no behavior block: default scale-down behaviour is immediate",
                )
            )
            continue
        scale_down = behavior.get("scaleDown") or {}
        window = scale_down.get("stabilizationWindowSeconds")
        if window is None or int(window) < MIN_SCALE_DOWN_STABILIZATION_SECONDS:
            out.append(
                Violation(
                    "INV6_disruption_and_autoscaling",
                    rid,
                    "scaleDown.stabilizationWindowSeconds=%r is below %ds; a short spike "
                    "would churn replicas" % (window, MIN_SCALE_DOWN_STABILIZATION_SECONDS),
                )
            )
        for direction in ("scaleUp", "scaleDown"):
            if not (behavior.get(direction) or {}).get("policies"):
                out.append(
                    Violation(
                        "INV6_disruption_and_autoscaling",
                        rid,
                        "%s has no explicit policies" % direction,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# invariant 7: standard labels
# ---------------------------------------------------------------------------


def inv7_standard_labels(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        kind = doc.get("kind")
        if kind not in LABELLED_KINDS:
            continue
        rid = resource_id(doc)
        labels = doc.get("metadata", {}).get("labels") or {}
        missing = [label for label in LABELS_REQUIRED if label not in labels]
        if missing:
            out.append(
                Violation(
                    "INV7_standard_labels",
                    rid,
                    "missing recommended labels: %s" % ", ".join(missing),
                )
            )
        managed_by = labels.get(MANAGED_BY_LABEL)
        if managed_by not in MANAGED_BY_ALLOWED:
            out.append(
                Violation(
                    "INV7_standard_labels",
                    rid,
                    "%s=%r is not one of %s" % (MANAGED_BY_LABEL, managed_by, MANAGED_BY_ALLOWED),
                )
            )
        for label in LABELS_REQUIRED:
            value = labels.get(label)
            if isinstance(value, str) and len(value) > 63:
                out.append(
                    Violation(
                        "INV7_standard_labels", rid, "label %s is longer than 63 characters" % label
                    )
                )
        # The pod template is what selectors and alerts actually match on.
        pod_template = doc.get("spec", {}).get("template", {}) if kind in WORKLOAD_KINDS else {}
        if pod_template:
            pod_labels = pod_template.get("metadata", {}).get("labels") or {}
            pod_missing = [label for label in LABELS_REQUIRED if label not in pod_labels]
            if pod_missing:
                out.append(
                    Violation(
                        "INV7_standard_labels",
                        rid,
                        "pod template is missing labels: %s" % ", ".join(pod_missing),
                    )
                )
            selector = ((doc.get("spec", {}).get("selector") or {}).get("matchLabels")) or {}
            for key, value in selector.items():
                if pod_labels.get(key) != value:
                    out.append(
                        Violation(
                            "INV7_standard_labels",
                            rid,
                            "selector %s=%s does not match the pod template labels" % (key, value),
                        )
                    )
    return out


# ---------------------------------------------------------------------------
# invariant 8: forbidden primitives
# ---------------------------------------------------------------------------


def inv8_forbidden_primitives(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    for doc in docs:
        rid = resource_id(doc)
        kind = doc.get("kind")

        if list(walk(doc, "hostPath")):
            out.append(Violation("INV8_forbidden_primitives", rid, "uses a hostPath volume"))
        if (
            list(walk(doc, "hostNetwork"))
            or list(walk(doc, "hostPID"))
            or list(walk(doc, "hostIPC"))
        ):
            out.append(
                Violation(
                    "INV8_forbidden_primitives",
                    rid,
                    "uses a host namespace (hostNetwork/hostPID/hostIPC)",
                )
            )
        for value in walk(doc, "privileged"):
            if value is True:
                out.append(
                    Violation("INV8_forbidden_primitives", rid, "runs a privileged container")
                )
        for value in walk(doc, "allowPrivilegeEscalation"):
            if value is True:
                out.append(
                    Violation(
                        "INV8_forbidden_primitives",
                        rid,
                        "allows privilege escalation",
                    )
                )
        for value in walk(doc, "add"):
            if isinstance(value, list) and value:
                out.append(
                    Violation(
                        "INV8_forbidden_primitives",
                        rid,
                        "adds Linux capabilities: %s" % ", ".join(str(v) for v in value),
                    )
                )
        if kind in ("ClusterRoleBinding", "RoleBinding"):
            role_ref = doc.get("roleRef") or {}
            if role_ref.get("name") == "cluster-admin":
                out.append(
                    Violation(
                        "INV8_forbidden_primitives",
                        rid,
                        "binds cluster-admin; the lab needs no API access at all",
                    )
                )
        if kind in ("ClusterRole", "ClusterRoleBinding"):
            out.append(
                Violation(
                    "INV8_forbidden_primitives",
                    rid,
                    "cluster-scoped RBAC is not needed by this lab",
                )
            )
        for value in walk(doc, "runAsUser"):
            if value == 0:
                out.append(Violation("INV8_forbidden_primitives", rid, "runs as uid 0"))
    return out


# ---------------------------------------------------------------------------
# extra: network isolation (not one of the eight, but part of the lab's claim)
# ---------------------------------------------------------------------------


def inv_network_isolation(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    out: List[Violation] = []
    policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        rid = resource_id(doc)
        selector = ((doc.get("spec", {}).get("selector") or {}).get("matchLabels")) or {}
        matching = [
            np
            for np in policies
            if np.get("spec", {}).get("podSelector", {}).get("matchLabels") == selector
        ]
        if not matching:
            out.append(
                Violation("EXTRA_network_isolation", rid, "no NetworkPolicy selects these pods")
            )
            continue
        for np in matching:
            policy_types = np.get("spec", {}).get("policyTypes") or []
            if "Egress" not in policy_types:
                out.append(
                    Violation(
                        "EXTRA_network_isolation",
                        resource_id(np),
                        "policyTypes does not include Egress",
                    )
                )
            if not np.get("spec", {}).get("egress"):
                out.append(
                    Violation(
                        "EXTRA_network_isolation",
                        resource_id(np),
                        "Egress is denied but nothing is allowed, not even DNS",
                    )
                )
    return out


INVARIANTS: Sequence[Tuple[str, Any]] = (
    ("INV1_probes_and_resources", inv1_probes_and_resources),
    ("INV2_graceful_shutdown", inv2_graceful_shutdown),
    ("INV3_rollout_safety", inv3_rollout_safety),
    ("INV4_security_context", inv4_security_context),
    ("INV5_image_hygiene", inv5_image_hygiene),
    ("INV6_disruption_and_autoscaling", inv6_disruption_and_autoscaling),
    ("INV7_standard_labels", inv7_standard_labels),
    ("INV8_forbidden_primitives", inv8_forbidden_primitives),
    ("EXTRA_network_isolation", inv_network_isolation),
)

INVARIANT_DESCRIPTIONS: Dict[str, str] = {
    "INV1_probes_and_resources": "every container: readiness + liveness + startup probes and cpu/memory requests and limits",
    "INV2_graceful_shutdown": "terminationGracePeriodSeconds >= 30, a preStop hook, and a drain budget that fits inside it",
    "INV3_rollout_safety": "RollingUpdate with maxUnavailable=0 and a progressDeadlineSeconds",
    "INV4_security_context": "runAsNonRoot, RuntimeDefault seccomp, no privilege escalation, read-only root filesystem, capabilities dropped",
    "INV5_image_hygiene": "fixed image tag (never latest) and imagePullPolicy IfNotPresent",
    "INV6_disruption_and_autoscaling": "PDB for every multi-replica workload, SLO-shaped scale-down behaviour",
    "INV7_standard_labels": "the five app.kubernetes.io/* labels plus managed-by on every object",
    "INV8_forbidden_primitives": "no hostPath, no host namespaces, no privileged, no added capabilities, no cluster-admin, no uid 0",
    "EXTRA_network_isolation": "every workload is selected by a NetworkPolicy that restricts egress and keeps DNS",
}


def check_all(docs: Sequence[Dict[str, Any]]) -> List[Violation]:
    """Run every invariant against a rendered document set."""
    violations: List[Violation] = []
    for _name, check in INVARIANTS:
        violations.extend(check(docs))
    return violations


def count_by_invariant(violations: Sequence[Violation]) -> Dict[str, int]:
    counts = {name: 0 for name, _ in INVARIANTS}
    for violation in violations:
        counts[violation.invariant] = counts.get(violation.invariant, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# tooling resolution
# ---------------------------------------------------------------------------


def tool_path(name: str) -> str:
    """Locate a validation binary: .tools/ first, then PATH."""
    local = TOOLS_DIR / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        "%s not found. Run `make tools` (scripts/install-tools.sh) or install it on PATH." % name
    )


def run(cmd: Sequence[str], cwd: Optional[Path] = None) -> str:
    """Run a command and return stdout, raising with stderr attached on failure."""
    completed = subprocess.run(
        list(cmd),
        cwd=str(cwd or REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed (%d): %s\n%s"
            % (completed.returncode, " ".join(cmd), completed.stderr.decode("utf-8", "replace"))
        )
    return completed.stdout.decode("utf-8")


def kustomize_build(path: Path) -> str:
    return run([tool_path("kustomize"), "build", str(path)])


def helm_template(values: Path, namespace: str) -> str:
    return run(
        [
            tool_path("helm"),
            "template",
            "k8s-sre-lab",
            str(REPO_ROOT / "chart"),
            "--namespace",
            namespace,
            "-f",
            str(values),
        ]
    )
