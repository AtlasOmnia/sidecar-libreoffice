# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Test support for the Sidecar window routes: a loopback fake of Hermes's Runs API, a static
fixture tree, and clients that speak the launcher channel and the page protocol exactly as
docs/web-protocol.md describes them. Nothing here talks to a real gateway or model.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Optional
import unittest

API_KEY = "api-key-" + "w" * 40
PAIRING_TOKEN = "pairing-token-" + "q" * 40


class FakeRunsApi:
    """``POST /v1/runs`` (an ``Idempotency-Key`` replays the admitted run), ``GET /v1/runs/{id}``
    and ``POST /v1/runs/{id}/stop`` on 127.0.0.1, with the API key checked on every call.

    A run reports ``live_state`` (``running`` by default) until ``finish`` is set, then
    ``completed`` with ``reply``. ``stop_mode``: ``ok`` (the run settles as ``cancelled`` at the
    next poll), ``settle`` (the stop answer itself reports ``cancelled``), ``unconfirmed``
    (acknowledged, never settles) or ``refuse`` (409). ``create_gate``: run creation waits on this
    event before answering. ``get_hold``: ``headers`` (a status poll sends nothing until
    ``get_release`` is set) or ``body`` (it sends its status line and headers, then holds the
    body); ``held_gets`` counts the polls being held. ``stops`` holds ``(run_id,
    time.monotonic())`` for every stop request; ``calls`` every request.
    """

    def __init__(self, key: str = API_KEY) -> None:
        self.key = key
        self.lock = threading.Condition()
        self.runs: dict = {}
        self.by_idempotency: dict = {}
        self.calls: list = []
        self.stops: list = []
        self.live_state, self.reply, self.stop_mode = "running", "**Done.** Total is 446.", "ok"
        self.finish = threading.Event()
        self.create_gate: Optional[threading.Event] = None
        self.get_hold: Optional[str] = None
        self.get_release = threading.Event()
        self.held_gets = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return None

            def _send(self, status: int, value: Any) -> None:
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {outer.key}"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with outer.lock:
                    outer.calls.append(("POST", self.path, body))
                    outer.lock.notify_all()
                if not self._authorized():
                    return self._send(401, {"error": {"message": "invalid API key"}})
                match = re.fullmatch(r"(?:/p/[a-z0-9_-]+)?/v1/runs/([A-Za-z0-9_-]+)/stop", self.path)
                if match:
                    return self._stop(match.group(1))
                if re.fullmatch(r"(?:/p/[a-z0-9_-]+)?/v1/runs", self.path):
                    if outer.create_gate is not None:
                        outer.create_gate.wait(30)
                    with outer.lock:
                        key = self.headers.get("Idempotency-Key")
                        run_id = outer.by_idempotency.get(key) if key else None
                        if run_id is None:
                            run_id = "run_" + secrets.token_hex(8)
                            outer.runs[run_id] = {"stopped": False, "input": body.get("input"),
                                                  "session_id": body.get("session_id")}
                            if key:
                                outer.by_idempotency[key] = run_id
                        outer.lock.notify_all()
                    return self._send(202, {"run_id": run_id, "status": "started"})
                return self._send(404, {"error": {"message": "not found"}})

            def _stop(self, run_id: str) -> None:
                with outer.lock:
                    outer.stops.append((run_id, time.monotonic()))
                    run = outer.runs.get(run_id)
                    mode = outer.stop_mode
                    if run is not None and mode in ("ok", "settle"):
                        run["stopped"] = True
                    outer.lock.notify_all()
                if run is None:
                    return self._send(404, {"error": {"message": "not found"}})
                if mode == "refuse":
                    return self._send(409, {"error": {"message": "Run is not active"}})
                if mode == "settle":
                    return self._send(200, {"run_id": run_id, "status": "cancelled"})
                return self._send(200, {"run_id": run_id, "status": "stopping"})

            def do_GET(self) -> None:
                with outer.lock:
                    outer.calls.append(("GET", self.path, None))
                    hold = outer.get_hold
                    if hold:
                        outer.held_gets += 1
                    outer.lock.notify_all()
                if hold == "headers":
                    outer.get_release.wait(30)
                if not self._authorized():
                    return self._send(401, {"error": {"message": "invalid API key"}})
                run_id = self.path.rsplit("/", 1)[-1]
                if hold == "body":   # the status line and headers now, the body once released
                    with outer.lock:
                        state = outer.live_state
                    data = json.dumps({"run_id": run_id, "status": state}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    outer.get_release.wait(30)
                    self.wfile.write(data)
                    self.close_connection = True
                    return None
                with outer.lock:
                    run = outer.runs.get(run_id)
                    state = outer.live_state
                if run is None:
                    return self._send(404, {"error": {"message": "not found"}})
                if run["stopped"]:
                    return self._send(200, {"run_id": run_id, "status": "cancelled"})
                if outer.finish.is_set():
                    return self._send(200, {"run_id": run_id, "status": "completed", "output": outer.reply})
                return self._send(200, {"run_id": run_id, "status": state})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, predicate: Any, seconds: float = 10.0) -> bool:
        with self.lock:
            return self.lock.wait_for(predicate, timeout=seconds)

    def creations(self) -> list:
        return [call for call in self.calls if call[0] == "POST" and call[1].endswith("/v1/runs")]

    def polls(self) -> list:
        return [call for call in self.calls if call[0] == "GET"]

    def close(self) -> None:
        self.get_release.set()   # a held poll answers instead of holding a server thread
        self.server.shutdown()
        self.server.server_close()


def eventually(predicate: Any, seconds: float = 10.0) -> bool:
    """Poll ``predicate`` until it is true or ``seconds`` pass (for state other threads change)."""
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


class FakeClock:
    """A settable clock for the web tables (TTLs, idle, window gone); long-polls use real time."""

    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_bridge(api: Optional[FakeRunsApi] = None, *, clock: Any = None, root: Optional[Path] = None,
                token: Any = PAIRING_TOKEN, key: Any = API_KEY, fence: Any = None, poll_interval: float = 0.05,
                long_poll_seconds: float = 2.0, max_handlers: Optional[int] = None) -> tuple:
    """A real ChatBridge on an ephemeral port with the Sidecar routes attached to a fixture tree
    -> ``(bridge, port, web)``. ``clock`` (a FakeClock) drives the web tables; no janitor thread
    runs unless ``clock`` is None. ``token``/``key``/``fence`` may be values or callables."""
    from plugin import chat_bridge, web_api   # imported late: the caller sets sys.path first

    def provider(value: Any) -> Any:
        return value if callable(value) else (lambda: value)
    bridge = chat_bridge.ChatBridge(port=0, token=provider(token), api_key=provider(key),
                                    api_port=api.port if api is not None else 9, poll_interval=poll_interval,
                                    fence_active=provider(True if fence is None else fence),
                                    max_handlers=max_handlers, web_routes=False)
    options: dict = {"root": root if root is not None else make_fixture(), "long_poll_seconds": long_poll_seconds}
    if clock is not None:
        options.update(clock=clock, janitor=False)
    web = web_api.attach(bridge, **options)
    bridge.web = web
    port = bridge.start()
    return bridge, port, web


# -- static fixture (stands in for lane B's plugin/sidebar/) --

FIXTURE_FILES = {
    "index.html": b"<!doctype html><title>Sidecar</title><script src=\"app.js\"></script>",
    "app.js": b"\"use strict\";\n",
    "omnia-pane.css": b"body{margin:0}\n",
    "assets/icon.svg": b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
    "vendor/purify.js": b"/* vendored */\n",
    "vendor/VENDOR.json": b"{}\n",            # present but not listed: never served
    "notes.txt": b"unlisted suffix\n",          # listed below, but .txt is not a served suffix
}
LISTED = ("app.js", "assets/icon.svg", "index.html", "notes.txt", "omnia-pane.css", "vendor/purify.js")


def make_fixture(directory: Optional[str] = None) -> Path:
    root = Path(directory or tempfile.mkdtemp(prefix="sidecar-ui-"))
    for relpath, data in FIXTURE_FILES.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    write_sums(root, LISTED)
    return root


def write_sums(root: Path, listed: tuple) -> None:
    lines = [f"{hashlib.sha256((root / relpath).read_bytes()).hexdigest()}  {relpath}\n" for relpath in sorted(listed)]
    # Bytes, not text: write_text would turn each LF into CRLF on Windows, and the plugin reads
    # SHA256SUMS strictly (LF lines, as git checks it out with this repository's .gitattributes).
    (root / "SHA256SUMS").write_bytes("".join(lines).encode("ascii"))


# -- clients --

def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mac(key: bytes, *parts: str) -> str:
    return b64u(hmac.new(key, "\n".join(parts).encode("ascii"), hashlib.sha256).digest())


def raw_request(port: int, method: str, path: str, body: Optional[bytes] = None, headers: Optional[list] = None,
                timeout: float = 30.0) -> tuple:
    """One request with exactly the given header list (duplicates allowed) -> (status, headers, body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers or []:
            connection.putheader(name, value)
        if body is not None and not any(name.lower() == "content-length" for name, _value in headers or []):
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


class LauncherClient:
    """The 0.5.0 launcher's side of section 3: hello, then MAC'd calls on one channel."""

    def __init__(self, port: int, token: str = PAIRING_TOKEN) -> None:
        self.port, self.key = port, token.encode("utf-8")
        self.channel: Optional[str] = None
        self.channel_key = b""
        self.counter = 0

    def base_headers(self) -> list:
        return [("Host", f"127.0.0.1:{self.port}")]

    def hello(self) -> tuple:
        client_nonce = b64u(secrets.token_bytes(32))
        body = json.dumps({"v": 1, "client_nonce": client_nonce}).encode()
        status, headers, raw = raw_request(self.port, "POST", "/launcher/hello", body,
                                           self.base_headers() + [("Content-Type", "application/json")])
        value = json.loads(raw)
        if status == 200:
            server_nonce = value["server_nonce"]
            expected = mac(self.key, "sidecar-launcher/1 hello", str(self.port), client_nonce, server_nonce)
            if not hmac.compare_digest(expected, value["server_proof"]):
                raise AssertionError("server proof does not verify")
            self.channel = server_nonce
            self.channel_key = hmac.new(self.key, "\n".join(["sidecar-launcher/1 channel", str(self.port),
                                                              client_nonce, server_nonce]).encode(),
                                        hashlib.sha256).digest()
            self.counter = 0
        return status, value

    def call(self, method: str, path: str, value: Any = None, *, counter: Optional[int] = None,
             tamper: bool = False, extra: Optional[list] = None, check: bool = True) -> tuple:
        body = b"" if value is None else json.dumps(value, separators=(",", ":")).encode()
        n = self.counter + 1 if counter is None else counter
        self.counter = max(self.counter, n)
        signature = mac(self.channel_key, "sidecar-launcher/1 request", method, path, str(n),
                        hashlib.sha256(body).hexdigest())
        if tamper:
            signature = signature[:-2] + ("AA" if not signature.endswith("AA") else "BB")
        headers = self.base_headers() + [("X-Sidecar-Channel", self.channel or ""), ("X-Sidecar-Counter", str(n)),
                                         ("X-Sidecar-MAC", signature)] + (extra or [])
        if method == "POST":
            headers.append(("Content-Type", "application/json"))
        status, answer_headers, raw = raw_request(self.port, method, path, body if method == "POST" else None, headers)
        answer_mac = dict(answer_headers).get("X-Sidecar-MAC")
        if check and status != 401:
            expected = mac(self.channel_key, "sidecar-launcher/1 response", str(n), str(status),
                           hashlib.sha256(raw).hexdigest())
            if answer_mac is None or not hmac.compare_digest(expected, answer_mac):
                raise AssertionError("response MAC missing or wrong")
        return status, json.loads(raw), answer_mac

    def launch(self, document: Optional[dict] = None, mode: str = "app", host_scheme: Optional[str] = "light",
               bounds: Any = "default", follow: Any = None) -> tuple:
        if bounds == "default":
            bounds = {"x": 1240, "y": 102, "width": 400, "height": 848} if mode == "app" else None
        if self.channel is None:
            self.hello()
        body = {
            "document": document or {"document_id": "lo:5f0c:1", "title": "sales.ods", "type": "calc"},
            "host_scheme": host_scheme, "mode": mode, "bounds": bounds}
        if follow is not None:
            body["follow"] = follow
        return self.call("POST", "/launcher/launch", body)


def code_of(url: str) -> str:
    return url.rsplit("#launch=", 1)[1]


class PageClient:
    """The page's side of section 6: same-origin fetches with the session header in memory."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.session: Optional[str] = None

    def headers(self, method: str) -> list:
        headers = [("Host", f"127.0.0.1:{self.port}"), ("Sec-Fetch-Site", "same-origin"),
                   ("Sec-Fetch-Mode", "cors"), ("Sec-Fetch-Dest", "empty")]
        if method == "POST":
            headers += [("Origin", self.origin), ("Content-Type", "application/json")]
        if self.session is not None:
            headers.append(("X-Sidecar-Session", self.session))
        return headers

    def request(self, method: str, path: str, value: Any = None, *, headers: Optional[list] = None,
                timeout: float = 30.0) -> tuple:
        body = None if method == "GET" else json.dumps({} if value is None else value).encode()
        status, _headers, raw = raw_request(self.port, method, path, body,
                                            self.headers(method) if headers is None else headers, timeout)
        return status, json.loads(raw)

    def exchange(self, code: str) -> tuple:
        status, value = self.request("POST", "/api/session", {"launch": code})
        if status == 200:
            self.session = value["session"]
        return status, value

    def state(self) -> tuple:
        return self.request("GET", "/api/state")

    def chat(self, message: str = "Add a total row") -> tuple:
        return self.request("POST", "/api/chat", {"message": message})

    def turn(self, turn_id: str, seq: int = 0, have: int = 0, timeout: float = 30.0) -> tuple:
        return self.request("GET", f"/api/turn?id={turn_id}&seq={seq}&have={have}", timeout=timeout)

    def window(self, seq: int = 0, timeout: float = 30.0) -> tuple:
        return self.request("GET", f"/api/window?seq={seq}", timeout=timeout)

    def stop(self, turn_id: str) -> tuple:
        return self.request("POST", "/api/turn/stop", {"turn": turn_id})

    def new_chat(self) -> tuple:
        return self.request("POST", "/api/new-chat", {})

    def close(self) -> tuple:
        return self.request("POST", "/api/session/close", {})

    def wait_done(self, turn_id: str, seconds: float = 20.0) -> dict:
        deadline = time.monotonic() + seconds
        seq = 0
        while time.monotonic() < deadline:
            status, value = self.turn(turn_id, seq)
            if status != 200:
                raise AssertionError(f"long-poll answered {status}: {value}")
            seq = value["turn"]["seq"]
            if value["turn"]["state"] == "done":
                return value["turn"]
        raise AssertionError("the turn never ended")


class WebCase(unittest.TestCase):
    """A real ChatBridge with the Sidecar routes, a fake Runs API and a fixture tree per test.
    ``fake_clock`` switches the web tables to a FakeClock (``self.clock``)."""

    fake_clock = False
    close_budget = 3.0

    def bridge_options(self) -> dict:
        return {}

    def setUp(self) -> None:
        self.api = FakeRunsApi()
        self.addCleanup(self.api.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = make_fixture(folder.name)
        self.clock = FakeClock() if self.fake_clock else None
        self.bridge, self.port, self.web = make_bridge(self.api, clock=self.clock, root=self.root,
                                                       **self.bridge_options())
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.api.stop_mode = "ok"
        self.bridge.close(self.close_budget)

    def launcher(self) -> LauncherClient:
        client = LauncherClient(self.port)
        client.hello()
        return client

    def page(self, document_id: str = "lo:5f0c:1", title: str = "sales.ods", mode: str = "app") -> PageClient:
        status, value, _mac = self.launcher().launch({"document_id": document_id, "title": title, "type": "calc"},
                                                     mode=mode)
        self.assertEqual(status, 201, value)
        page = PageClient(self.port)
        status, value = page.exchange(code_of(value["url"]))
        self.assertEqual(status, 200, value)
        return page

    def start_turn(self, page: PageClient, message: str = "Add a total row") -> str:
        status, value = page.chat(message)
        self.assertEqual(status, 202, value)
        return value["turn"]["id"]

    def followed_seq(self, page: PageClient, turn_id: str, state: str = "running") -> int:
        """The turn's ``seq`` once the bridge has NOTED the run's first status poll (its raw Runs
        API state is ``state``), not merely once the fake has received that poll: a long-poll sent
        with this ``seq`` is then answered only by a later change."""
        def noted() -> bool:
            with self.web._cond:
                for session in self.web.sessions.all():
                    turn = session.turn
                    if turn is not None and turn.id == turn_id and turn.run is not None:
                        return turn.run.hermes_state == state
            return False
        self.assertTrue(eventually(noted), "the bridge never noted the run's state")
        status, value = page.turn(turn_id, 0)
        self.assertEqual(status, 200, value)
        return value["turn"]["seq"]
