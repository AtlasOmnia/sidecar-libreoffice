#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Verification stub of the 0.5.0 Sidecar window endpoint (docs/web-protocol.md, frozen C0).

It imitates the plugin for page and launcher work: every ``/launcher/*``, ``/ui/*`` and ``/api/*``
route with the contract's gates, headers, error codes, sessions, launch codes, R18 channel and the
``/api/turn`` cursor long-poll. Turns run through the real A0 seam (``ChatBridge.begin_turn``,
``request_stop``, ``session_state``, ``close``) loaded from ``plugin/chat_bridge.py`` of the
checkout, against an in-process fake Runs API (``fake_runs_api.py``), so busy / unresolved /
too-many / Stop / unload behave as the plugin's seam does. The web layer here is written from
the contract text only; it shares no code with the plugin's own web modules. Stdlib only.

Usage (all ports ephemeral; 8642, 8660 and 8661 are refused):

    python3 scripts/web_smoke/stub_server.py serve --ui-dir plugin/sidebar --state-dir <dir>
    python3 scripts/web_smoke/stub_server.py open  --state-dir <dir> [--mode tab] [--chrome]
    python3 scripts/web_smoke/stub_server.py ctl   --state-dir <dir> unload | ready <reason> |
                                                   release <document_id|all> | heal | token <on|off>

``serve`` writes ``state.json`` and ``pairing-token`` (mode 0600, folder 0700) into the state
folder and never prints the token. ``open`` acts as the launcher: hello, launch, bootstrap file
(section 5), and with ``--chrome`` starts Google Chrome in app mode with a throwaway profile in
the state folder. Send messages containing ``[md]``, ``[inject]``, ``[hang]``, ``[slow:S]``,
``[fail]``, ``[unknown]``, ``[unresolved]``, ``[approval]``, ``[refuse]``, ``[long:N]`` or
``[noreply]`` to pick a scripted reply (see fake_runs_api.py).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract  # noqa: E402
from fake_runs_api import FakeRunsAPI  # noqa: E402

RESERVED_PORTS = {8642, 8660, 8661}
log = logging.getLogger("web_smoke.stub")


def load_chat_bridge(repo: Optional[Path] = None) -> Any:
    """``plugin/chat_bridge.py`` of the checkout, loaded by path (``plugin/__init__.py`` needs Hermes)."""
    root = Path(repo) if repo else HERE.parents[1]
    path = root / "plugin" / "chat_bridge.py"
    if not path.is_file():
        raise SystemExit(f"plugin/chat_bridge.py not found under {root}; pass --repo <checkout>")
    name = "web_smoke_chat_bridge"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    for attr in ("begin_turn", "request_stop", "session_state", "add_route_handler"):
        if not hasattr(module.ChatBridge, attr):
            raise SystemExit(f"chat_bridge.py lacks ChatBridge.{attr}: the A0 seam (5db8792 or later) is required")
    return module


class Timing:
    """Contract timings (seconds and counts); the harness may shorten them on the stub only."""

    def __init__(self, **kw: float) -> None:
        self.code_ttl = 30.0
        self.launch_record_ttl = 120.0
        self.channel_ttl = 120.0
        self.channel_calls = 64
        self.max_channels = 16
        self.max_codes = 16
        self.max_sessions = 8
        self.idle = 43200.0
        self.ended_keep = 600.0
        self.ended_max = 64
        self.window_live = 90.0
        self.window_gone = 90.0
        self.longpoll = 25.0
        for key, value in kw.items():
            if not hasattr(self, key):
                raise ValueError(f"unknown timing {key}")
            setattr(self, key, value)

    def as_dict(self) -> dict[str, float]:
        return dict(self.__dict__)


class WebError(Exception):
    def __init__(self, status: int, code: str, message: str, *, ended: Any = "absent",
                 headers: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(message)
        self.status, self.code, self.ended, self.headers = status, code, ended, headers


class Turn:
    def __init__(self, session: "Session") -> None:
        self.id = contract.b64u(secrets.token_bytes(16))
        self.session = session
        self.seq = 1
        self.state = "working"
        self.hermes_state: Optional[str] = None
        self.text = ""
        self.outcome: Optional[dict[str, Any]] = None
        self.run: Any = None
        self.poller: Optional[object] = None
        self.inflight = 0
        self.last_poll = time.monotonic()
        self.gone_requested = False

    def view(self, have: Optional[int] = None) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "seq": self.seq, "state": self.state, "hermes_state": self.hermes_state}
        if have is not None:
            start = min(have, len(self.text))
            out["text_from"] = start
            out["text"] = self.text[start:]
        out.update({"text_length": len(self.text), "activity": [], "outcome": self.outcome})
        return out


class Session:
    def __init__(self, digest: str, rec: "CodeRecord") -> None:
        self.digest = digest
        self.document, self.host_scheme, self.mode, self.bounds = rec.document, rec.host_scheme, rec.mode, rec.bounds
        self.chat_id = "lo-" + secrets.token_hex(16)
        self.last_seen = time.monotonic()
        self.turn: Optional[Turn] = None
        self.ended: Optional[str] = None

    @property
    def chat_label(self) -> str:
        return self.chat_id[:11]


class CodeRecord:
    def __init__(self, launch_id: str, document: dict, host_scheme: Optional[str], mode: str, bounds: Any) -> None:
        self.launch_id, self.document, self.host_scheme, self.mode, self.bounds = launch_id, document, host_scheme, mode, bounds
        self.minted = time.monotonic()
        self.used = False
        self.session_digest: Optional[str] = None


class Channel:
    def __init__(self, key: bytes, ttl: float) -> None:
        self.key = key
        self.next_counter = 1
        self.calls = 0
        self.expires = time.monotonic() + ttl


ROUTES = {
    "/launcher/hello": ("launcher", "POST", "launcher-hello"),
    "/launcher/launch": ("launcher", "POST", "launcher-launch"),
    "/launcher/release": ("launcher", "POST", "launcher-release"),
    "/api/session": ("api", "POST", "api-session"),
    "/api/state": ("api", "GET", "api-state"),
    "/api/chat": ("api", "POST", "api-chat"),
    "/api/turn/stop": ("api", "POST", "api-stop"),
    "/api/new-chat": ("api", "POST", "api-new-chat"),
    "/api/session/close": ("api", "POST", "api-close"),
}
BODY_LIMIT = {"/api/chat": 64 * 1024}
BODY_KEYS = {
    "/launcher/hello": {"v", "client_nonce"},
    "/launcher/launch": {"document", "host_scheme", "mode", "bounds"},
    "/launcher/release": {"document_id", "reason"},
    "/api/session": {"launch"},
    "/api/chat": {"message"},
    "/api/turn/stop": {"turn"},
    "/api/new-chat": set(),
    "/api/session/close": set(),
}
READ_ERRORS = {411: "length_required", 413: "too_large", 408: "body_timeout", 400: "bad_request"}


def classify(target: str) -> Optional[tuple[str, str, str, str]]:
    """-> (family, route, method, label); None for an unknown path. The legacy pair is not here."""
    if target in ROUTES:
        family, method, label = ROUTES[target]
        return family, target, method, label
    if target == "/api/turn" or target.startswith("/api/turn?"):
        return "api", "/api/turn", "GET", "api-turn"
    if target.startswith("/launcher/launch/") and "/" not in target[len("/launcher/launch/"):]:
        return "launcher", "/launcher/launch/<id>", "GET", "launcher-status"
    if target.startswith("/ui/") and len(target) > 4:
        return "ui", "/ui/<file>", "GET", "ui-static"
    return None


class Stub:
    def __init__(self, ui_dir: Path, *, port: int = 0, repo: Optional[Path] = None, timing: Optional[Timing] = None,
                 token: Optional[str] = None, api_key: Optional[str] = None, poll_interval: float = 0.1,
                 fake_delay: float = 0.6) -> None:
        if port in RESERVED_PORTS:
            raise SystemExit(f"port {port} is reserved for the real listeners; use 0 (ephemeral)")
        self.cb = load_chat_bridge(repo)
        self.ui_dir = Path(ui_dir).resolve()
        self.timing = timing or Timing()
        self.token: Optional[str] = token if token is not None else secrets.token_urlsafe(32)
        self.fake = FakeRunsAPI(api_key or secrets.token_urlsafe(24), delay=fake_delay)
        self.api_key_value: Optional[str] = self.fake.api_key
        self.fence = True
        self.cv = threading.Condition()
        self.channels: dict[str, Channel] = {}
        self.codes: dict[str, CodeRecord] = {}          # sha256(code) -> record (held code_ttl from minting)
        self.launches: dict[str, tuple[CodeRecord, str]] = {}   # launch_id -> (record, state)
        self.sessions: dict[str, Session] = {}          # sha256(token) -> session
        self.ended: dict[str, tuple[str, float]] = {}   # sha256(token) -> (reason, when)
        self.parked = 0
        self.closing = False
        self.warnings: list[str] = []
        self._requested_port = port
        self._poll_interval = poll_interval
        self.bridge: Any = None
        self.port = 0
        self._sweeper_stop = threading.Event()

    # -- lifecycle --
    def start(self) -> int:
        self.fake.start()
        self.fake.observers.append(self._observe)
        self.bridge = self.cb.ChatBridge(port=self._requested_port, token=lambda: self.token,
                                         api_key=lambda: self.api_key_value, api_port=self.fake.port,
                                         fence_active=lambda: self.fence, poll_interval=self._poll_interval)
        self.bridge.add_route_handler(self.handle)
        self.port = self.bridge.start()
        if self.port in RESERVED_PORTS:   # cannot happen with port 0, but never serve on them
            self.bridge.close(0)
            raise SystemExit("the OS handed out a reserved port; start again")
        threading.Thread(target=self._sweeper, name="WebSmokeStubSweeper", daemon=True).start()
        return self.port

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self, budget: Optional[float] = None) -> int:
        """Plugin unload (R12): ingress first, parked long-polls answered once, then the bridge."""
        runs = []
        with self.cv:
            self.closing = True
            for session in list(self.sessions.values()):
                runs.extend(self._end_locked(session, "turned_off"))
            self.codes.clear()
            self.channels.clear()
            self.launches.clear()
            self.cv.notify_all()
            deadline = time.monotonic() + 2.0
            while self.parked and time.monotonic() < deadline:
                self.cv.wait(deadline - time.monotonic())
        remaining = self.bridge.close(budget)
        self._sweeper_stop.set()
        self.fake.close()
        return remaining

    # -- readiness --
    def readiness(self) -> dict[str, Any]:
        if self.bridge is None or self.bridge.closed or not self.fence:
            return {"ready": False, "reason": "plugin_inactive"}
        if not self.api_key_value:
            return {"ready": False, "reason": "no_api_key"}
        if self.api_key_value != self.fake.api_key:
            return {"ready": False, "reason": "bad_api_key"}
        return {"ready": True, "reason": None}

    def set_ready(self, reason: Optional[str]) -> None:
        self.fence = reason != "plugin_inactive"
        self.api_key_value = {"no_api_key": None, "bad_api_key": "wrong-" + self.fake.api_key}.get(
            reason or "", self.fake.api_key)

    # -- background --
    def _sweeper(self) -> None:
        while not self._sweeper_stop.wait(0.25):
            stops = []
            now = time.monotonic()
            with self.cv:
                t = self.timing
                for digest, rec in list(self.codes.items()):
                    if now - rec.minted >= t.code_ttl:
                        del self.codes[digest]
                        record = self.launches.get(rec.launch_id)
                        if record and record[1] == "pending":
                            self.launches[rec.launch_id] = (rec, "expired")
                for launch_id, (rec, _state) in list(self.launches.items()):
                    if now - rec.minted >= t.launch_record_ttl:
                        del self.launches[launch_id]
                for nonce, channel in list(self.channels.items()):
                    if now >= channel.expires:
                        del self.channels[nonce]
                for digest, (_reason, when) in list(self.ended.items()):
                    if now - when >= t.ended_keep:
                        del self.ended[digest]
                for session in list(self.sessions.values()):
                    if now - session.last_seen >= t.idle and not (session.turn and session.turn.inflight):
                        stops.extend(self._end_locked(session, "idle"))
                        continue
                    turn = session.turn
                    if (turn and turn.state != "done" and not turn.gone_requested and turn.inflight == 0
                            and now - turn.last_poll >= t.window_gone):
                        turn.gone_requested = True
                        if turn.run is not None:
                            stops.append(turn.run)
            for run in stops:
                self._stop_run(run)

    def _stop_run(self, run: Any) -> None:
        if run is not None and self.bridge.request_stop(run):
            with self.cv:
                for session in self.sessions.values():
                    turn = session.turn
                    if turn and turn.run is run and turn.state == "working":
                        turn.state = "stopping"
                        turn.seq += 1
                self.cv.notify_all()

    def _observe(self, chat_id: str, status: str) -> None:
        mapped = {"started": "running"}.get(status, status)
        if mapped not in ("queued", "running", "stopping", "waiting_for_approval"):
            return
        with self.cv:
            for session in self.sessions.values():
                turn = session.turn
                if session.chat_id == chat_id and turn and turn.state != "done" and turn.hermes_state != mapped:
                    turn.hermes_state = mapped
                    turn.seq += 1
                    self.cv.notify_all()

    def _end_locked(self, session: Session, reason: str) -> list[Any]:
        """End a session (caller holds cv); returns the runs to ask to stop (outside the lock)."""
        if self.sessions.get(session.digest) is not session:
            return []
        del self.sessions[session.digest]
        session.ended = reason
        self.ended[session.digest] = (reason, time.monotonic())
        while len(self.ended) > self.timing.ended_max:
            oldest = min(self.ended, key=lambda d: self.ended[d][1])
            del self.ended[oldest]
        self.cv.notify_all()
        turn = session.turn
        return [turn.run] if turn and turn.state != "done" and turn.run is not None else []

    def end_session(self, session: Session, reason: str) -> None:
        with self.cv:
            runs = self._end_locked(session, reason)
        for run in runs:
            self._stop_run(run)

    # -- request entry (ChatBridge.add_route_handler) --
    def handle(self, req: Any) -> bool:
        start = time.monotonic()
        target = req.path
        if target in ("/health", "/chat"):
            return self._legacy(req, start)
        try:
            if req.headers.get_all("Host", []) != [f"127.0.0.1:{self.port}"]:
                route = classify(target)
                req.route_label = route[3] if route else "unknown"
                raise WebError(403, "bad_host", "Host must be the loopback chat endpoint")
            route = classify(target)
            if route is None:
                req.route_label = "unknown"
                raise WebError(404, "not_found", "unknown route")
            family, name, method, label = route
            if req.command != method:
                req.route_label = "method"
                raise WebError(405, "method_not_allowed", "method not allowed", headers=(("Allow", method),))
            req.route_label = label
            self._role_gate(req, family)
            if family == "ui":
                return self._static(req, target, start)
            if family == "api":
                self._fetch_gate(req)
            raw = self._body_gate(req, name)
            if family == "launcher":
                return self._launcher(req, name, target, raw, start)
            return self._api(req, name, target, raw, start)
        except WebError as exc:
            payload: dict[str, Any] = {"ok": False, "error": str(exc), "code": exc.code}
            if exc.ended != "absent":
                payload["ended"] = exc.ended
            self._json(req, exc.status, payload, start, extra=exc.headers)
            return True

    # -- gates --
    def _legacy(self, req: Any, start: float) -> bool:
        if req.command not in ("GET", "POST") or req.headers.get("Origin") is not None:
            return False   # 0.4.0's own answers: 501 for other methods, 403 for an Origin
        names = {k.lower() for k in req.headers.keys()}
        if any(n.startswith("sec-fetch-") for n in names) or names & set(contract.FORBIDDEN["legacy"]):
            req.route_label = "health" if req.path == "/health" else "chat"
            self._json(req, 403, {"ok": False, "error": "this credential is not accepted on this route",
                                  "code": "forbidden_credential"}, start)
            return True
        return False

    def _role_gate(self, req: Any, family: str) -> None:
        names = {k.lower() for k in req.headers.keys()}
        bad = names & set(contract.FORBIDDEN[family])
        if family == "launcher" and any(n.startswith("sec-fetch-") for n in names):
            bad.add("sec-fetch")
        if bad:
            raise WebError(403, "forbidden_credential", "this credential is not accepted on this route")

    def _fetch_gate(self, req: Any) -> None:
        sites = req.headers.get_all("Sec-Fetch-Site", [])
        if sites and sites != ["same-origin"]:
            raise WebError(403, "cross_site", "cross-site requests are refused")
        origins = req.headers.get_all("Origin", [])
        if req.command == "POST":
            if origins != [self.origin]:
                raise WebError(403, "bad_origin", "Origin is missing or not this endpoint")
        elif origins:
            if origins != [self.origin]:
                raise WebError(403, "bad_origin", "Origin is not this endpoint")
        elif sites != ["same-origin"]:
            raise WebError(403, "bad_origin", "a browser request without Origin must be same-origin")

    def _body_gate(self, req: Any, name: str) -> Optional[bytes]:
        if req.headers.get("Transfer-Encoding") is not None:
            raise WebError(400, "bad_request", "chunked bodies are not accepted")
        if req.command == "GET":
            if req.headers.get("Content-Length") not in (None, "0"):
                raise WebError(400, "bad_request", "GET requests carry no body")
            return None
        types = req.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise WebError(415, "unsupported_media_type", "Content-Type must be application/json")
        if len(req.headers.get_all("Content-Length", [])) > 1:
            raise WebError(400, "bad_request", "one Content-Length is required")
        try:
            return req.read_body(BODY_LIMIT.get(name, 16 * 1024))
        except self.cb.ChatError as exc:
            raise WebError(exc.status, READ_ERRORS.get(exc.status, "bad_request"), str(exc)) from None

    @staticmethod
    def _parse(raw: bytes, name: str) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise WebError(400, "bad_request", "the request body is not valid JSON") from None
        if not isinstance(data, dict) or set(data) != BODY_KEYS[name]:
            raise WebError(400, "bad_request", "the request body does not have the expected keys")
        return data

    # -- answers --
    def _json(self, req: Any, status: int, payload: dict[str, Any], start: float,
              extra: tuple[tuple[str, str], ...] = ()) -> bytes:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req.respond(status, data, start, content_type="application/json", headers=contract.API_HEADERS + tuple(extra))
        return data

    # -- launcher (R18) --
    def _launcher(self, req: Any, name: str, target: str, raw: Optional[bytes], start: float) -> bool:
        if name == "/launcher/hello":
            data = self._parse(raw or b"", name)
            if data["v"] != 1 or not contract.is_int(data["v"]) or not (
                    isinstance(data["client_nonce"], str) and contract.CODE_RE.fullmatch(data["client_nonce"])):
                raise WebError(400, "bad_request", "hello needs v 1 and a 43-character client_nonce")
            token = self.token
            if not token:
                raise WebError(503, "no_pairing", "Hermes has no LibreOffice pairing token yet: pair first")
            nonce = contract.b64u(secrets.token_bytes(32))
            with self.cv:
                if self.closing:
                    raise WebError(503, "turned_off", "the plugin is unloading")
                while len(self.channels) >= self.timing.max_channels:
                    oldest = min(self.channels, key=lambda n: self.channels[n].expires)
                    del self.channels[oldest]
                self.channels[nonce] = Channel(contract.channel_key(token, self.port, data["client_nonce"], nonce),
                                               self.timing.channel_ttl)
            self._json(req, 200, {"ok": True, "v": 1, "server_nonce": nonce,
                                  "server_proof": contract.server_proof(token, self.port, data["client_nonce"], nonce),
                                  "expires_in": int(self.timing.channel_ttl), "chat": self.readiness()}, start)
            return True
        channel, counter = self._channel(req, target, raw or b"")

        def answer(status: int, payload: dict[str, Any]) -> bool:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            mac = contract.response_mac(channel.key, counter, status, data)
            req.respond(status, data, start, content_type="application/json",
                        headers=contract.API_HEADERS + (("X-Sidecar-MAC", mac),))
            return True

        def refuse(status: int, code: str, message: str) -> bool:
            return answer(status, {"ok": False, "error": message, "code": code})

        if name == "/launcher/launch/<id>":
            launch_id = target[len("/launcher/launch/"):]
            with self.cv:
                record = self.launches.get(launch_id) if contract.ID22_RE.fullmatch(launch_id) else None
            if record is None:
                return refuse(404, "not_found", "unknown launch")
            return answer(200, {"ok": True, "state": record[1]})
        try:
            data = self._parse(raw or b"", name)
        except WebError as exc:
            return refuse(exc.status, exc.code, str(exc))
        if name == "/launcher/launch":
            doc, scheme, mode, bounds = data["document"], data["host_scheme"], data["mode"], data["bounds"]
            if not (contract.valid_document(doc) and scheme in ("light", "dark", None) and mode in ("app", "tab")
                    and ((mode == "app" and contract.valid_rectangle(bounds)) or (mode == "tab" and bounds is None))):
                return refuse(400, "bad_request", "the launch request is not valid")
            now = time.monotonic()
            with self.cv:
                if self.closing:
                    return refuse(503, "turned_off", "the plugin is unloading")
                for session in self.sessions.values():
                    if session.document["document_id"] == doc["document_id"] and (
                            now - session.last_seen <= self.timing.window_live
                            or (session.turn is not None and session.turn.inflight > 0)):
                        return answer(200, {"ok": True, "launch_id": None, "url": None, "window_live": True,
                                            "expires_in": 0})
                if sum(1 for r in self.codes.values() if not r.used) >= self.timing.max_codes:
                    return refuse(429, "too_many_launches", "too many launches are waiting; try again shortly")
                if len(self.sessions) >= self.timing.max_sessions and not self._evictable_locked(doc["document_id"]):
                    return refuse(429, "session_cap", "too many Sidecar windows are open")
                code = contract.b64u(secrets.token_bytes(32))
                launch_id = contract.b64u(secrets.token_bytes(16))
                rec = CodeRecord(launch_id, doc, scheme, mode, bounds)
                self.codes[hashlib.sha256(code.encode()).hexdigest()] = rec
                self.launches[launch_id] = (rec, "pending")
            return answer(201, {"ok": True, "launch_id": launch_id, "url": f"{self.origin}/ui/index.html#launch={code}",
                                "window_live": False, "expires_in": int(self.timing.code_ttl)})
        # /launcher/release
        document_id, reason = data["document_id"], data["reason"]
        if not ((reason == "document_closed" and isinstance(document_id, str) and 1 <= len(document_id) <= 512)
                or (reason == "host_quit" and document_id is None)):
            return refuse(400, "bad_request", "the release request is not valid")
        stops = []
        with self.cv:
            matching = [s for s in self.sessions.values()
                        if document_id is None or s.document["document_id"] == document_id]
            for session in matching:
                stops.extend(self._end_locked(session, "host_gone"))
        for run in stops:
            self._stop_run(run)
        return answer(200, {"ok": True, "released": len(matching)})

    def _channel(self, req: Any, target: str, body: bytes) -> tuple[Channel, int]:
        names = req.headers.get_all("X-Sidecar-Channel", [])
        counters = req.headers.get_all("X-Sidecar-Counter", [])
        macs = req.headers.get_all("X-Sidecar-MAC", [])
        with self.cv:
            channel = self.channels.get(names[0]) if len(names) == 1 else None
            ok = (channel is not None and len(counters) == 1 and len(macs) == 1
                  and contract.COUNTER_RE.fullmatch(counters[0]) is not None
                  and time.monotonic() < channel.expires and channel.calls < self.timing.channel_calls
                  and int(counters[0]) == channel.next_counter
                  and hmac.compare_digest(macs[0], contract.request_mac(channel.key, req.command, target,
                                                                        int(counters[0]), body)))
            if not ok:
                if channel is not None:
                    self.channels.pop(names[0], None)
                raise WebError(401, "launcher_auth", "the launcher channel is not valid")
            channel.next_counter += 1
            channel.calls += 1
            return channel, int(counters[0])

    def _evictable_locked(self, document_id: str) -> Optional[Session]:
        """The session a new one for ``document_id`` would replace or evict, if any."""
        for session in self.sessions.values():
            if session.document["document_id"] == document_id:
                return session
        candidates = [s for s in self.sessions.values() if self.bridge.session_state(s.chat_id) != "driven"]
        return min(candidates, key=lambda s: s.last_seen) if candidates else None

    # -- static (section 7) --
    def _static(self, req: Any, target: str, start: float) -> bool:
        rel = target[len("/ui/"):]
        if any(ch in target for ch in "%?#\\") or ".." in target or not contract.STATIC_REL_RE.fullmatch(rel):
            raise WebError(404, "not_found", "not found")
        site = req.headers.get_all("Sec-Fetch-Site", [])
        mode = req.headers.get_all("Sec-Fetch-Mode", [])
        dest = req.headers.get_all("Sec-Fetch-Dest", [])
        navigation = rel == "index.html" and mode in ([], ["navigate"]) and dest in ([], ["document"])
        if not navigation and site not in ([], ["same-origin"]):
            raise WebError(403, "cross_site", "cross-site requests are refused")
        suffix = os.path.splitext(rel)[1]
        if suffix not in contract.STATIC_TYPES:
            raise WebError(404, "not_found", "not found")
        try:
            sums = contract.parse_sha256sums((self.ui_dir / "SHA256SUMS").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            sums = None
        digest = (sums or {}).get(rel)
        if digest is None:
            raise WebError(404, "not_found", "not found")
        path = self.ui_dir
        for part in rel.split("/"):
            path = path / part
            try:
                if stat.S_ISLNK(os.lstat(path).st_mode):
                    raise WebError(404, "not_found", "not found")
            except OSError:
                raise WebError(404, "not_found", "not found") from None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                data = f.read(8 * 1024 * 1024)
        except OSError:
            raise WebError(404, "not_found", "not found") from None
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise WebError(404, "not_found", "not found")
        req.respond(200, data, start, content_type=contract.STATIC_TYPES[suffix], headers=contract.STATIC_HEADERS)
        return True

    # -- page API (section 6) --
    def _session(self, req: Any) -> Session:
        values = req.headers.get_all("X-Sidecar-Session", [])
        token = values[0] if len(values) == 1 else ""
        digest = hashlib.sha256(token.encode()).hexdigest() if contract.CODE_RE.fullmatch(token) else ""
        with self.cv:
            session = self.sessions.get(digest) if digest else None
            if session is None:
                ended = self.ended.get(digest, (None,))[0] if digest else None
                if self.closing:
                    ended = ended or "turned_off"
                raise WebError(401, "no_session", "This window is no longer connected. "
                               "Click Open Sidecar in LibreOffice again.", ended=ended)
            session.last_seen = time.monotonic()
            return session

    def _api(self, req: Any, name: str, target: str, raw: Optional[bytes], start: float) -> bool:
        if name == "/api/session":
            return self._exchange(req, self._parse(raw or b"", name), start)
        session = self._session(req)
        if name == "/api/state":
            with self.cv:
                view = session.turn.view() if session.turn else None
            self._json(req, 200, {"ok": True, "document": session.document, "chat": self.readiness(),
                                  "chat_label": session.chat_label, "turn": view}, start)
            return True
        if name == "/api/turn":
            return self._turn(req, session, target, start)
        data = self._parse(raw or b"", name)
        if name == "/api/chat":
            return self._chat(req, session, data["message"], start)
        if name == "/api/turn/stop":
            turn_id = data["turn"]
            if not isinstance(turn_id, str):
                raise WebError(400, "bad_request", "turn must be a string")
            with self.cv:
                turn = session.turn
                if turn is None or turn.id != turn_id:
                    raise WebError(404, "unknown_turn", "this reply is no longer known")
                if turn.state == "done":
                    self._json(req, 200, {"ok": True, "turn": turn.view()}, start)
                    return True
                run = turn.run
            if not self.bridge.request_stop(run):
                with self.cv:
                    view = turn.view()
                self._json(req, 200, {"ok": True, "turn": view}, start)
                return True
            with self.cv:
                if turn.state == "working":
                    turn.state = "stopping"
                    turn.seq += 1
                    self.cv.notify_all()
                view = turn.view()
            self._json(req, 202 if view["state"] != "done" else 200, {"ok": True, "turn": view}, start)
            return True
        if name == "/api/new-chat":
            if self.bridge.session_state(session.chat_id) == "driven":
                raise WebError(409, "turn_running", "Wait for the reply, or press Stop first.")
            with self.cv:
                session.chat_id = "lo-" + secrets.token_hex(16)
                session.turn = None
                self.cv.notify_all()
            self._json(req, 200, {"ok": True, "chat_label": session.chat_label}, start)
            return True
        # /api/session/close
        self.end_session(session, "closed")
        self._json(req, 200, {"ok": True}, start)
        return True

    def _exchange(self, req: Any, data: dict[str, Any], start: float) -> bool:
        code = data["launch"]
        if not isinstance(code, str):
            raise WebError(400, "bad_request", "launch must be a string")
        digest = hashlib.sha256(code.encode()).hexdigest() if contract.CODE_RE.fullmatch(code) else ""
        stops: list[Any] = []
        with self.cv:
            rec = self.codes.get(digest) if digest else None
            now = time.monotonic()
            if rec is None or now - rec.minted >= self.timing.code_ttl:
                raise WebError(401, "launch_expired", contract.LAUNCH_EXPIRED_TEXT)
            if rec.used:
                victim = self.sessions.get(rec.session_digest or "")
                if victim is not None:
                    stops.extend(self._end_locked(victim, "double_use"))
                self.launches[rec.launch_id] = (rec, "double_use")
                self.warnings.append("launch code presented twice; its session was revoked")
                log.warning("sidecar launch code presented twice; its session was revoked")
                exc = WebError(401, "launch_double_use", contract.LAUNCH_EXPIRED_TEXT)
            else:
                exc = None
                old = self._evictable_locked(rec.document["document_id"])
                if old is not None and old.document["document_id"] == rec.document["document_id"]:
                    stops.extend(self._end_locked(old, "replaced"))
                elif len(self.sessions) >= self.timing.max_sessions:
                    if old is None:
                        raise WebError(429, "session_cap", "too many Sidecar windows are open")
                    stops.extend(self._end_locked(old, "evicted"))
                token = contract.b64u(secrets.token_bytes(32))
                session = Session(hashlib.sha256(token.encode()).hexdigest(), rec)
                self.sessions[session.digest] = session
                rec.used = True
                rec.session_digest = session.digest
                self.launches[rec.launch_id] = (rec, "exchanged")
        for run in stops:
            self._stop_run(run)
        if exc is not None:
            raise exc
        self._json(req, 200, {"ok": True, "session": token, "idle_timeout": int(self.timing.idle),
                              "document": session.document, "host_scheme": session.host_scheme,
                              "mode": session.mode, "bounds": session.bounds, "chat": self.readiness(),
                              "chat_label": session.chat_label, "endpoint": self.origin,
                              "limits": {"max_message": self.cb.MAX_MESSAGE}}, start)
        return True

    def _chat(self, req: Any, session: Session, message: Any, start: float) -> bool:
        if not isinstance(message, str) or not message.strip() or len(message) > self.cb.MAX_MESSAGE:
            raise WebError(400, "message_invalid", f"A message must be 1 to {self.cb.MAX_MESSAGE} characters.")
        turn = Turn(session)

        def on_done(_run: Any, outcome: Any) -> None:
            with self.cv:
                turn.state = "done"
                turn.text = outcome.text[: self.cb.MAX_REPLY] if outcome.kind == "completed" else ""
                turn.outcome = {"kind": outcome.kind, "message": None if outcome.kind == "completed" else outcome.text}
                turn.seq += 1
                self.cv.notify_all()

        content = self.cb.compose_message(message.strip(), session.document)
        try:
            run = self.bridge.begin_turn(session.chat_id, content, on_done)
        except self.cb.ChatError as exc:
            text = str(exc)
            if exc.status == 409:
                code = "busy" if "still waiting" in text else "unresolved"
            elif exc.status == 429:
                code = "too_many"
            elif text == self.cb.TURN_WORKER_FAILED:
                code = "worker_failed"
            else:
                code = "plugin_inactive"
            raise WebError(exc.status, code, text) from None
        with self.cv:
            turn.run = run
            if session.ended is None:
                session.turn = turn
            view = turn.view()
        if session.ended is not None:
            self._stop_run(run)
        self._json(req, 202, {"ok": True, "turn": view}, start)
        return True

    def _turn(self, req: Any, session: Session, target: str, start: float) -> bool:
        query = urllib.parse.urlsplit(target).query
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=False)
        keys = [k for k, _ in pairs]
        values = dict(pairs)
        number = re.compile(r"^(0|[1-9][0-9]{0,5})$")
        if (sorted(keys) != ["have", "id", "seq"] or not contract.ID22_RE.fullmatch(values["id"])
                or not number.fullmatch(values["seq"]) or not number.fullmatch(values["have"])
                or int(values["seq"]) > 200000 or int(values["have"]) > 200000):
            raise WebError(400, "bad_request", "the turn query is not valid")
        seq, have = int(values["seq"]), int(values["have"])
        me = object()
        with self.cv:
            turn = session.turn
            if turn is None or turn.id != values["id"]:
                raise WebError(404, "unknown_turn", "this reply is no longer known")
            turn.last_poll = time.monotonic()
            if turn.poller is not None:
                self.cv.notify_all()          # the older long-poll sees it was superseded
            turn.poller = me
            turn.inflight += 1
            self.parked += 1
            deadline = time.monotonic() + self.timing.longpoll
            try:
                while True:
                    if self.closing:
                        raise WebError(503, "turned_off", "The LibreOffice plugin was turned off in Hermes.",
                                       ended="turned_off")
                    if session.ended is not None:
                        raise WebError(401, "no_session", "This window is no longer connected. "
                                       "Click Open Sidecar in LibreOffice again.", ended=session.ended)
                    if turn.poller is not me:
                        payload = {"ok": True, "superseded": True, "turn": turn.view(have)}
                        break
                    left = deadline - time.monotonic()
                    if turn.seq > seq or turn.state == "done" or left <= 0:
                        payload = {"ok": True, "superseded": False, "turn": turn.view(have)}
                        break
                    self.cv.wait(left)
            finally:
                turn.inflight -= 1
                turn.last_poll = time.monotonic()
                if turn.poller is me:
                    turn.poller = None
                self.parked -= 1
                self.cv.notify_all()
        self._json(req, 200, payload, start)
        return True


# ------------------------------------------------------------------------------- command line
def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _control_routes(stub: Stub, token: str) -> Any:
    def handler(req: Any) -> bool:
        if not req.path.startswith("/_control/"):
            return False
        supplied = req.headers.get("X-Stub-Control", "")
        if req.command != "POST" or not hmac.compare_digest(supplied.encode(), token.encode()):
            req._send(403, {"ok": False})
            return True
        length = int(req.headers.get("Content-Length") or 0)
        args = json.loads(req.rfile.read(length) or b"{}") if length else {}
        cmd = req.path[len("/_control/"):]
        result: Any = None
        if cmd == "ready":
            stub.set_ready(args.get("reason"))
            result = stub.readiness()
        elif cmd == "release":
            doc_id = args.get("document_id")
            stops = []
            with stub.cv:
                for s in list(stub.sessions.values()):
                    if doc_id is None or s.document["document_id"] == doc_id:
                        stops.extend(stub._end_locked(s, "host_gone"))
            for run in stops:
                stub._stop_run(run)
            result = len(stops)
        elif cmd == "heal":
            stub.fake.heal_all()
        elif cmd == "token":
            stub.token = args.get("token") if args.get("on") else None
        elif cmd == "unload":
            req._send(200, {"ok": True, "result": "unloading"})
            threading.Thread(target=stub.close, daemon=True).start()
            return True
        else:
            req._send(404, {"ok": False})
            return True
        req._send(200, {"ok": True, "result": result})
        return True
    return handler


def cmd_serve(args: argparse.Namespace) -> None:
    state = _private_dir(Path(args.state_dir).resolve())
    timing = Timing(**{k: float(v) for k, v in (t.split("=", 1) for t in args.timing)})
    stub = Stub(Path(args.ui_dir), port=args.port, repo=Path(args.repo) if args.repo else None, timing=timing)
    if args.ready != "ready":
        stub.set_ready(args.ready)
    control = secrets.token_urlsafe(24)
    stub.fake.extra_routes = _control_routes(stub, control)
    port = stub.start()
    if args.ready != "ready":
        stub.set_ready(args.ready)
    _write_private(state / "pairing-token", stub.token or "")
    _write_private(state / "state.json", json.dumps({"port": port, "origin": stub.origin, "api_port": stub.fake.port,
                                                     "control": control, "pid": os.getpid()}))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    print(f"stub serving {stub.origin}/ui/ (fake Runs API on 127.0.0.1:{stub.fake.port}); state in {state}", flush=True)
    try:
        while not stub.bridge.closed:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stub.close()


def _state(args: argparse.Namespace) -> tuple[dict, str, Path]:
    state = Path(args.state_dir).resolve()
    info = json.loads((state / "state.json").read_text())
    return info, (state / "pairing-token").read_text().strip(), state


def cmd_open(args: argparse.Namespace) -> None:
    import subprocess
    from httpraw import open_launch
    info, token, state = _state(args)
    document = {"document_id": args.document_id, "title": args.title, "type": args.type}
    bounds = None if args.mode == "tab" else {"x": args.x, "y": args.y, "width": 400, "height": 848}
    launch_id, url = open_launch(info["port"], token, document, mode=args.mode, bounds=bounds)
    folder = _private_dir(state / "launch")
    path = folder / f"launch-{secrets.token_hex(16)}.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(contract.bootstrap_html(url))
    print(f"launch {launch_id[:4]}… bootstrap file {path} (valid {int(30)} s, single use)")
    if args.chrome:
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile = _private_dir(state / "browser")
        subprocess.Popen([chrome, f"--app={path.as_uri()}", f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                          "--disable-background-networking", "--disable-component-update",
                          "--disable-default-apps", "--use-mock-keychain",
                          f"--window-position={args.x},{args.y - 32}", "--window-size=400,880"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_ctl(args: argparse.Namespace) -> None:
    import http.client
    info, _token, _state_dir = _state(args)
    command, rest = args.command, args.rest
    body: dict[str, Any] = {}
    if command == "ready":
        body = {"reason": None if not rest or rest[0] == "ready" else rest[0]}
    elif command == "release":
        body = {"document_id": None if not rest or rest[0] == "all" else rest[0]}
    elif command == "token":
        body = {"on": bool(rest) and rest[0] == "on", "token": _token}
    conn = http.client.HTTPConnection("127.0.0.1", info["api_port"], timeout=15)
    data = json.dumps(body).encode()
    conn.request("POST", f"/_control/{command}", body=data,
                 headers={"X-Stub-Control": info["control"], "Content-Type": "application/json"})
    print(conn.getresponse().read().decode())


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--ui-dir", required=True, help="folder with SHA256SUMS (plugin/sidebar or a fixture)")
    s.add_argument("--state-dir", required=True)
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--repo", help="checkout whose plugin/chat_bridge.py to use (default: this one)")
    s.add_argument("--ready", default="ready", choices=["ready", "plugin_inactive", "no_api_key", "bad_api_key"])
    s.add_argument("--timing", action="append", default=[], metavar="NAME=VALUE",
                   help="override a contract timing on the stub, e.g. code_ttl=5 (see Timing)")
    o = sub.add_parser("open")
    o.add_argument("--state-dir", required=True)
    o.add_argument("--document-id", default="lo:stub:1")
    o.add_argument("--title", default="sales.ods")
    o.add_argument("--type", default="calc", choices=list(contract.DOCUMENT_TYPES))
    o.add_argument("--mode", default="app", choices=["app", "tab"])
    o.add_argument("--x", type=int, default=1240)
    o.add_argument("--y", type=int, default=102)
    o.add_argument("--chrome", action="store_true")
    c = sub.add_parser("ctl")
    c.add_argument("--state-dir", required=True)
    c.add_argument("command", choices=["unload", "ready", "release", "heal", "token"])
    c.add_argument("rest", nargs="*")
    args = ap.parse_args(argv)
    {"serve": cmd_serve, "open": cmd_open, "ctl": cmd_ctl}[args.cmd](args)


if __name__ == "__main__":
    main()
