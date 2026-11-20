# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""A fake Hermes Runs API (``/v1/runs``) on an ephemeral loopback port, with scripted runs.

It answers the three calls the plugin makes (``POST /v1/runs`` with an ``Idempotency-Key``,
``GET /v1/runs/{id}``, ``POST /v1/runs/{id}/stop``) with the shapes of Hermes 0.23.1, and picks a
script from markers in the run's input text (the user's message is at its end):

    (none)          completes after ``delay`` seconds with a short Markdown echo
    [md]            completes with a Markdown reply (table, code, list, quote, link)
    [inject]        completes with the R1 injection corpus
    [long:N]        completes with a reply of N characters
    [slow:S]        completes after S seconds
    [hang]          runs until stopped (stop -> cancelled after ``stop_delay``)
    [approval]      waiting_for_approval until stopped
    [fail]          ends ``failed``
    [unknown]       ends ``unknown`` (outcome not known; the run is released)
    [unresolved]    status and stop answer 500 until healed (the plugin keeps the run unresolved)
    [refuse]        the creation request answers 500 (nothing admitted)
    [noreply]       completes with an empty output

Every call is recorded (method, run id, session id, time) for the harness. Stdlib only.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

try:
    from . import corpus  # type: ignore
except ImportError:  # run as a plain script directory
    import corpus  # type: ignore


class FakeRun:
    def __init__(self, run_id: str, session_id: str, text: str, delay: float) -> None:
        self.run_id, self.session_id, self.text = run_id, session_id, text
        self.created = time.monotonic()
        self.stop_at: Optional[float] = None
        self.healed = False
        self.script, self.duration, self.output = self._pick(text, delay)

    @staticmethod
    def _pick(text: str, delay: float) -> tuple[str, float, str]:
        tail = text.split("\n\n", 1)[-1]   # compose_message: one context line, a blank line, the message
        m = re.search(r"\[(md|inject|hang|approval|fail|unknown|unresolved|refuse|noreply)\]", tail)
        long_m = re.search(r"\[long:(\d{1,6})\]", tail)
        slow_m = re.search(r"\[slow:(\d+(?:\.\d+)?)\]", tail)
        duration = float(slow_m.group(1)) if slow_m else delay
        if long_m:
            return "long", duration, corpus.long_reply(int(long_m.group(1)))
        script = m.group(1) if m else "echo"
        if script == "md":
            return script, duration, corpus.MARKDOWN_REPLY
        if script == "inject":
            return script, duration, corpus.INJECTION_REPLY
        if script == "noreply":
            return script, duration, ""
        return script, duration, f"You said: **{tail.strip()[:200]}**"

    def status(self, now: float, stop_delay: float) -> str:
        if self.stop_at is not None and self.script != "unresolved":
            return "cancelled" if now >= self.stop_at + stop_delay else "stopping"
        if self.script == "unresolved" and self.healed:
            return "cancelled" if self.stop_at is not None else "completed"
        elapsed = now - self.created
        if elapsed < 0.15:
            return "queued"
        if self.script in ("hang", "unresolved"):
            return "running"
        if self.script == "approval":
            return "waiting_for_approval"
        if elapsed < self.duration:
            return "running"
        return {"fail": "failed", "unknown": "unknown"}.get(self.script, "completed")


class FakeRunsAPI:
    def __init__(self, api_key: str, *, delay: float = 0.6, stop_delay: float = 0.2) -> None:
        self.api_key = api_key
        self.delay, self.stop_delay = delay, stop_delay
        self.lock = threading.Lock()
        self.runs: dict[str, FakeRun] = {}
        self.by_key: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.observers: list[Callable[[str, str], None]] = []   # (session_id, status) on each answer
        self.control_token = secrets.token_urlsafe(24)
        self.extra_routes: Optional[Callable[[Any], bool]] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self.port = 0

    # -- harness helpers --
    def record(self, kind: str, run: Optional[FakeRun]) -> None:
        with self.lock:
            self.calls.append({"kind": kind, "run_id": run.run_id if run else None,
                               "session_id": run.session_id if run else None, "t": time.monotonic(),
                               "script": run.script if run else None})

    def calls_for(self, session_id: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
        with self.lock:
            return [c for c in self.calls if c["session_id"] == session_id and (kind is None or c["kind"] == kind)]

    def stops(self) -> list[dict[str, Any]]:
        with self.lock:
            return [c for c in self.calls if c["kind"] == "stop"]

    def heal_all(self) -> None:
        with self.lock:
            for run in self.runs.values():
                run.healed = True

    def _notify(self, run: FakeRun, status: str) -> None:
        for observer in list(self.observers):
            try:
                observer(run.session_id, status)
            except Exception:  # noqa: BLE001 - an observer must not break the fake
                pass

    # -- HTTP --
    def start(self) -> int:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "FakeRunsAPI/1"
            sys_version = ""

            def log_message(self, *_a: Any) -> None:
                return None

            def _send(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _authorized(self) -> bool:
                supplied = self.headers.get("Authorization", "")
                return hmac.compare_digest(supplied.encode(), f"Bearer {api.api_key}".encode())

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw or b"null")
                except ValueError:
                    return None

            def do_GET(self) -> None:
                if api.extra_routes and api.extra_routes(self):
                    return
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                m = re.fullmatch(r"/v1/runs/([A-Za-z0-9_-]+)", self.path)
                run = api.runs.get(m.group(1)) if m else None
                if run is None:
                    return self._send(404, {"error": "not found"})
                api.record("get", run)
                if run.script == "unresolved" and not run.healed:
                    return self._send(500, {"error": "internal"})
                status = run.status(time.monotonic(), api.stop_delay)
                payload: dict[str, Any] = {"run_id": run.run_id, "status": status}
                if status == "completed":
                    payload["output"] = run.output
                api._notify(run, status)
                self._send(200, payload)

            def do_POST(self) -> None:
                if api.extra_routes and api.extra_routes(self):
                    return
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                body = self._body()
                if self.path == "/v1/runs":
                    if not isinstance(body, dict) or not isinstance(body.get("input"), str):
                        return self._send(400, {"error": "bad request"})
                    key = self.headers.get("Idempotency-Key", "")
                    with api.lock:
                        existing = api.runs.get(api.by_key.get(key, ""))
                    if existing is not None:
                        api.record("create-replay", existing)
                        return self._send(202, {"run_id": existing.run_id, "status": "queued"})
                    run = FakeRun("run_" + secrets.token_hex(8), str(body.get("session_id")), body["input"], api.delay)
                    if run.script == "refuse":
                        api.record("create-refused", run)
                        return self._send(500, {"error": "internal"})
                    with api.lock:
                        api.runs[run.run_id] = run
                        if key:
                            api.by_key[key] = run.run_id
                    api.record("create", run)
                    api._notify(run, "queued")
                    return self._send(202, {"run_id": run.run_id, "status": "queued"})
                m = re.fullmatch(r"/v1/runs/([A-Za-z0-9_-]+)/stop", self.path)
                run = api.runs.get(m.group(1)) if m else None
                if run is None:
                    return self._send(404, {"error": "not found"})
                api.record("stop", run)
                if run.script == "unresolved" and not run.healed:
                    return self._send(500, {"error": "internal"})
                now = time.monotonic()
                status = run.status(now, api.stop_delay)
                if status in ("queued", "running", "waiting_for_approval"):
                    run.stop_at = now
                    status = run.status(now, api.stop_delay)
                elif run.script == "unresolved" and run.healed and run.stop_at is None:
                    run.stop_at = now
                api._notify(run, status)
                self._send(200, {"run_id": run.run_id, "status": status})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, name="FakeRunsAPI", daemon=True).start()
        return self.port

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
