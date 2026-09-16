"""Behavioural tests for the demo API that every manifest in this repo describes.

These run without a cluster, so they are the part of the suite that can prove the
*application* side of the reliability contract on a laptop: readiness is a separate
signal from liveness, the metrics endpoint is valid Prometheus text, and shutdown
drains instead of dropping traffic.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "app" / "server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location("demo_api_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("ACCESS_LOG", "false")
    monkeypatch.setenv("READY_DELAY_SECONDS", "0")
    monkeypatch.setenv("SHUTDOWN_GRACE_SECONDS", "1")
    monkeypatch.delenv("FAIL_READY", raising=False)

    module = load_server_module()
    module.STATE = module.State()
    server = module.build_server(0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        yield module, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url: str):
    """Return (status, body, headers) without raising on 4xx/5xx."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8"), dict(error.headers)


def test_healthz_is_liveness_only(api):
    _module, base = api
    status, body, _ = get(base + "/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_readyz_is_503_until_startup_finishes(api):
    module, base = api
    # Fresh state: the initialisation thread has not completed yet.
    module.STATE.initialised = False
    status, body, _ = get(base + "/readyz")
    assert status == 503
    assert json.loads(body)["reason"] == "initialising"

    module.STATE.initialised = True
    status, body, _ = get(base + "/readyz")
    assert status == 200
    assert json.loads(body)["status"] == "ready"


def test_fail_ready_closes_the_traffic_gate_without_killing_liveness(api, monkeypatch):
    module, base = api
    module.STATE.initialised = True

    monkeypatch.setenv("FAIL_READY", "true")
    status, body, _ = get(base + "/readyz")
    assert status == 503
    assert json.loads(body)["reason"] == "fail_ready_switch"

    # Liveness must stay green: a pod that removes itself from the endpoints is not a
    # pod that should be restarted.
    status, _body, _ = get(base + "/healthz")
    assert status == 200


def test_shutdown_flips_readiness_before_the_process_exits(api):
    module, base = api
    module.STATE.initialised = True
    module.STATE.begin_shutdown()

    status, body, _ = get(base + "/readyz")
    assert status == 503
    assert json.loads(body)["reason"] == "terminating"
    assert get(base + "/healthz")[0] == 200


def test_metrics_endpoint_is_prometheus_text(api):
    module, base = api
    module.STATE.initialised = True
    get(base + "/readyz")

    status, body, headers = get(base + "/metrics")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")

    for expected in (
        "# TYPE demo_api_ready gauge",
        "# TYPE demo_api_requests_total counter",
        "demo_api_ready 1",
        'demo_api_build_info{version="',
    ):
        assert expected in body, "metrics output is missing %r" % expected

    # Every non-comment line must be a valid `name[labels] value` sample.
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        assert line.rsplit(" ", 1)[-1].replace(".", "").isdigit(), "bad sample: %r" % line


def test_readyz_failures_are_counted(api, monkeypatch):
    module, base = api
    module.STATE.initialised = True
    monkeypatch.setenv("FAIL_READY", "true")
    get(base + "/readyz")

    _status, body, _ = get(base + "/metrics")
    assert "demo_api_ready_failures_total 1" in body


def test_work_endpoint_is_bounded(api):
    module, base = api
    status, body, _ = get(base + "/work?ms=10")
    assert status == 200
    assert json.loads(body)["worked_milliseconds"] == 10

    status, body, _ = get(base + "/work?ms=999999")
    assert status == 200
    assert json.loads(body)["worked_milliseconds"] == module.MAX_WORK_MILLISECONDS


def test_unknown_path_is_404_and_counted(api):
    _module, base = api
    status, body, _ = get(base + "/nope")
    assert status == 404
    assert json.loads(body)["error"] == "not found"

    _status, metrics, _ = get(base + "/metrics")
    assert 'demo_api_requests_total{path="/nope",method="GET"} 1' in metrics
