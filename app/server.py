#!/usr/bin/env python3
"""Demo API used as the workload under test in the reliability lab.

Deliberately dependency-free (standard library only) so the image stays tiny and
so the interesting behaviour is the *lifecycle* behaviour, not the framework:

* ``/healthz``  liveness  - 200 while the process can still serve.
* ``/readyz``   readiness - 503 until startup finished, 503 as soon as shutdown
  starts, 503 when ``FAIL_READY=true`` (the readiness-failure switch used by the
  kind E2E and by the chaos experiments).
* ``/metrics``  Prometheus text exposition.
* ``/work``     bounded CPU burn, used as the HPA load generator and by the
  CPU-stress chaos experiment.
* ``POST /admin/readiness?value=true|false`` - flips the same flag as ``FAIL_READY``
  at runtime, so a single pod can be taken out of the Service endpoints and put back
  without a rollout. Off unless ``ENABLE_CHAOS_SWITCH=true``.

Shutdown contract: on SIGTERM the process flips readiness to 503 immediately
(the endpoint controller then removes the pod from the Service endpoints) and
keeps serving in-flight requests until either they finish or
``SHUTDOWN_GRACE_SECONDS`` elapses, whichever comes first.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
READY_DELAY_SECONDS_DEFAULT = 3.0
SHUTDOWN_GRACE_SECONDS_DEFAULT = 30.0
MAX_WORK_MILLISECONDS = 2000


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class State:
    """Process state shared by every request handler thread."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.ready_delay = env_float("READY_DELAY_SECONDS", READY_DELAY_SECONDS_DEFAULT)
        self.shutdown_grace = env_float(
            "SHUTDOWN_GRACE_SECONDS", SHUTDOWN_GRACE_SECONDS_DEFAULT
        )
        # FAIL_READY is baked in at start; the chaos switch flips the same flag at
        # runtime so the kind E2E can prove endpoint removal without waiting for a
        # new ReplicaSet to roll out.
        self.forced_unready = env_bool("FAIL_READY")
        self.chaos_switch_enabled = env_bool("ENABLE_CHAOS_SWITCH", False)
        self.initialised = False
        self.terminating = False
        self.in_flight = 0
        self.requests_total: dict = {}
        self.ready_failures_total = 0
        self.lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def initialise(self) -> None:
        # Stands in for "warm the caches / open the connection pool" work.
        time.sleep(self.ready_delay)
        with self.lock:
            self.initialised = True

    def begin_shutdown(self) -> None:
        with self.lock:
            self.terminating = True

    # -- readiness ---------------------------------------------------------
    def readiness(self) -> tuple:
        if env_bool("FAIL_READY"):
            return False, "fail_ready_switch"
        with self.lock:
            if self.forced_unready:
                return False, "fail_ready_switch"
            if self.terminating:
                return False, "terminating"
            if not self.initialised:
                return False, "initialising"
        return True, "ready"

    # -- chaos switch ------------------------------------------------------
    def set_forced_unready(self, value: bool) -> bool:
        with self.lock:
            self.forced_unready = value
            return self.forced_unready

    # -- bookkeeping -------------------------------------------------------
    def enter(self, path: str, method: str) -> None:
        with self.lock:
            self.in_flight += 1
            key = (path, method)
            self.requests_total[key] = self.requests_total.get(key, 0) + 1

    def leave(self) -> None:
        with self.lock:
            self.in_flight -= 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "uptime_seconds": time.time() - self.started_at,
                "in_flight": self.in_flight,
                "requests_total": dict(self.requests_total),
                "ready_failures_total": self.ready_failures_total,
                "terminating": self.terminating,
                "initialised": self.initialised,
            }

    def drain(self) -> None:
        """Block until in-flight requests are done or the grace period expires."""
        deadline = time.time() + self.shutdown_grace
        while time.time() < deadline:
            with self.lock:
                if self.in_flight == 0:
                    return
            time.sleep(0.1)


STATE = State()


def render_metrics(state: dict) -> str:
    ready, _reason = STATE.readiness()
    lines = [
        "# HELP demo_api_build_info Build information for the demo API.",
        "# TYPE demo_api_build_info gauge",
        'demo_api_build_info{version="%s"} 1' % VERSION,
        "# HELP demo_api_ready Whether the instance currently passes readiness.",
        "# TYPE demo_api_ready gauge",
        "demo_api_ready %d" % (1 if ready else 0),
        "# HELP demo_api_uptime_seconds Seconds since process start.",
        "# TYPE demo_api_uptime_seconds gauge",
        "demo_api_uptime_seconds %.3f" % state["uptime_seconds"],
        "# HELP demo_api_inflight_requests Requests currently being served.",
        "# TYPE demo_api_inflight_requests gauge",
        "demo_api_inflight_requests %d" % state["in_flight"],
        "# HELP demo_api_ready_failures_total Readiness checks answered with 503.",
        "# TYPE demo_api_ready_failures_total counter",
        "demo_api_ready_failures_total %d" % state["ready_failures_total"],
        "# HELP demo_api_requests_total Requests handled, by path and method.",
        "# TYPE demo_api_requests_total counter",
    ]
    for (path, method), count in sorted(state["requests_total"].items()):
        lines.append(
            'demo_api_requests_total{path="%s",method="%s"} %d' % (path, method, count)
        )
    lines.append("")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "demo-api/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        if env_bool("ACCESS_LOG", True):
            sys.stderr.write(
                "%s - %s\n" % (self.address_string(), fmt % args)
            )

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        STATE.enter(path, self.command)
        try:
            if path == "/healthz":
                self._json(200, {"status": "ok", "version": VERSION})
            elif path == "/readyz":
                ready, reason = STATE.readiness()
                if ready:
                    self._json(200, {"status": "ready", "reason": reason})
                else:
                    with STATE.lock:
                        STATE.ready_failures_total += 1
                    self._json(503, {"status": "not-ready", "reason": reason})
            elif path == "/metrics":
                self._send(
                    200,
                    render_metrics(STATE.snapshot()).encode("utf-8"),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
            elif path == "/work":
                self._handle_work()
            elif path == "/":
                self._json(
                    200,
                    {
                        "service": "demo-api",
                        "version": VERSION,
                        "endpoints": ["/healthz", "/readyz", "/metrics", "/work"],
                        "write_endpoints": ["/admin/readiness"],
                    },
                )
            else:
                self._json(404, {"error": "not found", "path": path})
        finally:
            STATE.leave()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        """The chaos switch: flip readiness at runtime, on one pod, deterministically.

        Disabled unless ENABLE_CHAOS_SWITCH=true, because a service that can be told
        to stop serving is exactly the kind of endpoint an attacker looks for.
        """
        path = self.path.split("?", 1)[0]
        STATE.enter(path, self.command)
        try:
            if path != "/admin/readiness":
                self._json(404, {"error": "not found", "path": path})
                return
            if not STATE.chaos_switch_enabled:
                self._json(
                    403,
                    {"error": "chaos switch disabled", "hint": "set ENABLE_CHAOS_SWITCH=true"},
                )
                return
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            raw = ""
            for pair in query.split("&"):
                if pair.startswith("value="):
                    raw = pair[6:]
            value = raw.strip().lower() in {"1", "true", "yes", "on"}
            applied = STATE.set_forced_unready(value)
            sys.stderr.write("[demo-api] chaos switch: forced_unready=%s\n" % applied)
            self._json(200, {"forced_unready": applied})
        finally:
            STATE.leave()

    def _handle_work(self) -> None:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        millis = 50
        for pair in query.split("&"):
            if pair.startswith("ms="):
                try:
                    millis = max(0, min(MAX_WORK_MILLISECONDS, int(pair[3:])))
                except ValueError:
                    millis = 50
        deadline = time.time() + millis / 1000.0
        spins = 0
        while time.time() < deadline:
            spins += 1
        self._json(200, {"worked_milliseconds": millis, "spins": spins})


def build_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    port = int(env_float("PORT", 8080))
    server = build_server(port)

    def on_signal(signum, _frame):
        sys.stderr.write(
            "[demo-api] received signal %d, failing readiness and draining\n" % signum
        )
        STATE.begin_shutdown()
        threading.Thread(target=_drain_and_stop, args=(server,), daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threading.Thread(target=STATE.initialise, daemon=True).start()
    sys.stderr.write("[demo-api] %s listening on :%d\n" % (VERSION, port))
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


def _drain_and_stop(server: ThreadingHTTPServer) -> None:
    STATE.drain()
    server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
