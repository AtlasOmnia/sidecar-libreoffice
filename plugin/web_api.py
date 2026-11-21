# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Routes of the Sidecar window on the chat endpoint (docs/web-protocol.md).

``attach(bridge)`` registers one route handler on a ``ChatBridge`` (``add_route_handler``), one
close hook pair (``add_close_hook``) and the run-state listener. The handler answers only the
``/launcher/*``, ``/ui/*`` and ``/api/*`` families and the paths a browser probes on its own (``/``,
``/favicon.ico``, ``/apple-touch-icon*``); every other request goes on to the legacy ``/health``
and ``/chat`` routes unchanged.

Each request passes the gates of section 1.2 in order: Host, then the route and its method
(404/405), role separation, fetch metadata, body, credential. Answers are JSON with fixed
headers; static files are served only from ``plugin/sidebar/`` when listed in its
``SHA256SUMS`` and still matching it. Logs carry only the route label, method, status and time.

Turns: ``/api/chat`` claims synchronously and starts one worker through ``ChatBridge.begin_turn``;
this module keeps per session only the current turn record (reply text, outcome, a version
number ``seq``) that ``/api/turn`` long-polls read. User Stop, a window that went away, and every
end of a session only call ``ChatBridge.request_stop``: the worker that drives the run sends the
stop, so a run never leaves the 0.4.0 ownership rules.

Locks: this module's ``_cond`` may be held while calling into the bridge (``request_stop``,
``session_state``), never the other way round: the bridge calls back (outcome, run state, close
hooks) only without its own lock held. ``begin_turn`` is called without ``_cond``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl

from . import web_auth as auth
from .chat_bridge import (MAX_ACTIVE, MAX_MESSAGE, MAX_REPLY, TURN_WORKER_FAILED, ChatError, TurnOutcome,
                          compose_message, valid_header_secret)

log = logging.getLogger(__name__)

LONG_POLL_SECONDS = 25.0
WINDOW_GONE_SECONDS = 90.0
JANITOR_SECONDS = 1.0
MAX_POST = 16 * 1024
MAX_CHAT_POST = 64 * 1024
MAX_STATIC_BYTES = 4 * 1024 * 1024
MAX_SUMS_BYTES = 64 * 1024
MAX_QUERY_NUMBER = 200_000
SIDEBAR = Path(__file__).resolve().parent / "sidebar"

API_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Referrer-Policy", "no-referrer"),
)
STATIC_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; worker-src 'none'; "
              "manifest-src 'none'; require-trusted-types-for 'script'; trusted-types dompurify")
PERMISSIONS_POLICY = ("accelerometer=(), camera=(), display-capture=(), fullscreen=(), geolocation=(), gyroscope=(), "
                      "hid=(), magnetometer=(), microphone=(), midi=(), payment=(), serial=(), usb=(), "
                      "clipboard-read=(), clipboard-write=(self)")
STATIC_HEADERS = (
    ("Content-Security-Policy", STATIC_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Permissions-Policy", PERMISSIONS_POLICY),
    ("Cache-Control", "no-cache"),
)
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
RELPATH_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
SUMS_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*)$")
NUMBER_RE = re.compile(r"^(0|[1-9][0-9]{0,5})$")   # decimal, no leading zeros (6.4)
HERMES_STATES = {"queued": "queued", "started": "running", "running": "running", "stopping": "stopping",
                 "waiting_for_approval": "waiting_for_approval"}

ERRORS = {
    "bad_host": (403, "This request was not addressed to the Sidecar endpoint."),
    "forbidden_credential": (403, "This request carries a credential this route does not accept."),
    "cross_site": (403, "Requests from other sites are refused."),
    "bad_origin": (403, "Requests from other origins are refused."),
    "method_not_allowed": (405, "This method is not allowed here."),
    "unsupported_media_type": (415, "The request body must be JSON."),
    "length_required": (411, "The request needs a Content-Length."),
    "too_large": (413, "The request body is empty or too large."),
    "body_timeout": (408, "The request did not arrive in time."),
    "bad_request": (400, "The request is not in the expected form."),
    "not_found": (404, "Not found."),
    "no_pairing": (503, "Hermes is not paired with LibreOffice yet. See README ▸ Pairing."),
    "launcher_auth": (401, "The launcher channel was refused."),
    "too_many_launches": (429, "Too many Sidecar launches are waiting; try again shortly."),
    "session_cap": (429, "Too many Sidecar windows are open; close one and try again."),
    "launch_expired": (401, "This launch link was already used or has expired. Click Open Sidecar in LibreOffice "
                            "again."),
    "launch_double_use": (401, "This launch was used by another program; that connection was closed. Click Open "
                               "Sidecar again."),
    "no_session": (401, "Click Open Sidecar in LibreOffice to reconnect."),
    "busy": (409, "this chat is still waiting for a reply"),
    "unresolved": (409, "an earlier message in this chat is still unresolved in Hermes. Start a New chat to continue."),
    "too_many": (429, "too many chats are running or unresolved; try again shortly"),
    "plugin_inactive": (503, "the LibreOffice plugin is not active in Hermes: enable it and restart the gateway"),
    "worker_failed": (503, TURN_WORKER_FAILED),
    "message_invalid": (400, f"message must be 1 to {MAX_MESSAGE} characters"),
    "unknown_turn": (404, "This reply is not the window's current one."),
    "turn_running": (409, "Wait for the reply, or press Stop first."),
    "turned_off": (503, "The LibreOffice plugin was turned off in Hermes."),
    "internal": (500, "the chat endpoint failed"),
}
_BODY_ERRORS = {411: "length_required", 413: "too_large", 408: "body_timeout", 400: "bad_request"}

# (family, route) -> (name, method, label); /launcher/launch/<id> and /ui/<file> are matched apart.
_ROUTES = {
    "/launcher/hello": ("hello", "POST", "launcher-hello"),
    "/launcher/launch": ("launch", "POST", "launcher-launch"),
    "/launcher/release": ("release", "POST", "launcher-release"),
    "/api/session": ("session", "POST", "api-session"),
    "/api/state": ("state", "GET", "api-state"),
    "/api/chat": ("chat", "POST", "api-chat"),
    "/api/turn": ("turn", "GET", "api-turn"),
    "/api/turn/stop": ("stop", "POST", "api-stop"),
    "/api/new-chat": ("new_chat", "POST", "api-new-chat"),
    "/api/session/close": ("close", "POST", "api-close"),
}
_BODY_KEYS = {"hello": ("v", "client_nonce"), "launch": ("document", "host_scheme", "mode", "bounds"),
              "release": ("document_id", "reason"), "session": ("launch",), "chat": ("message",),
              "stop": ("turn",), "new_chat": (), "close": ()}


class WebError(Exception):
    """A refusal with a machine code (section 9.1); ``message`` overrides the table's sentence."""

    def __init__(self, code: str, message: Optional[str] = None, *, ended: Any = False,
                 headers: tuple = ()) -> None:
        super().__init__(code)
        self.code, self.status = code, ERRORS[code][0]
        self.message = message if message is not None else ERRORS[code][1]
        self.ended, self.headers = ended, headers


class Turn:
    """The current turn of one session: what ``/api/turn`` reports. ``seq`` grows on every
    change the page should see; ``reply`` is set once, when the turn completes."""

    __slots__ = ("id", "seq", "state", "run", "reply", "outcome", "last_poll", "in_flight", "poll_gen", "gone")

    def __init__(self, turn_id: str, now: float) -> None:
        self.id, self.seq, self.state = turn_id, 1, "working"
        self.run: Any = None
        self.reply = ""
        self.outcome: Optional[TurnOutcome] = None
        self.last_poll = now      # when a long-poll last arrived or ended (the turn's start before one)
        self.in_flight = 0        # long-polls of this turn being answered
        self.poll_gen = 0         # the newest long-poll's number: an older one is superseded
        self.gone = False         # the window-gone stop was requested


class _Call:
    __slots__ = ("request", "start", "mac")

    def __init__(self, request: Any, start: float) -> None:
        self.request, self.start = request, start
        self.mac: Optional[tuple] = None   # (channel key, counter) of an accepted launcher call


def _family(path: str) -> Optional[str]:
    route = path.split("?", 1)[0]
    for prefix, family in (("/launcher", "launcher"), ("/ui", "ui"), ("/api", "api")):
        if route == prefix or route.startswith(prefix + "/"):
            return family
    if route in ("/", "/favicon.ico") or route.startswith("/apple-touch-icon"):
        return "probe"
    return None


def _chat_label(chat_session_id: str) -> str:
    """The display-only ``chat_label`` (6.1): ``lo-`` and the first 8 hex digits of the chat session id."""
    return chat_session_id[:11]


def _cut(request: Any) -> None:
    """End a connection without writing to it (after close() has returned)."""
    request.close_connection = True
    try:
        request.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class StaticFiles:
    """Files of ``root`` listed in ``root/SHA256SUMS``, served only while their bytes match."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.realpath(root))

    def _regular(self, relpath: str, limit: int) -> Optional[bytes]:
        """The bytes of a regular file under root, reached without any symlink, else None."""
        path = self.root
        parts = relpath.split("/")
        try:
            for part in parts[:-1]:
                path = path / part
                if not stat.S_ISDIR(os.lstat(path).st_mode):   # lstat: a symlink is not a directory
                    return None
            path = path / parts[-1]
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(limit + 1)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        return data if len(data) <= limit else None

    def sums(self) -> dict:
        """``relpath -> sha256 hex`` from SHA256SUMS; any malformed line means nothing is served."""
        raw = self._regular("SHA256SUMS", MAX_SUMS_BYTES)
        if raw is None:
            return {}
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return {}
        if not text.endswith("\n"):
            return {}
        listed = {}
        for line in text[:-1].split("\n"):
            match = SUMS_LINE_RE.fullmatch(line)
            if match is None or match.group(2) in listed or ".." in match.group(2):
                return {}
            listed[match.group(2)] = match.group(1)
        return listed

    def load(self, relpath: str) -> Optional[tuple[bytes, str]]:
        suffix = os.path.splitext(relpath)[1]
        if suffix not in CONTENT_TYPES or not RELPATH_RE.fullmatch(relpath) or ".." in relpath:
            return None
        expected = self.sums().get(relpath)
        if expected is None:
            return None
        data = self._regular(relpath, MAX_STATIC_BYTES)
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            return None
        return data, CONTENT_TYPES[suffix]


class WebApi:
    def __init__(self, bridge: Any, *, root: Optional[Path] = None, clock: Callable[[], float] = time.monotonic,
                 long_poll_seconds: float = LONG_POLL_SECONDS, janitor_seconds: float = JANITOR_SECONDS,
                 janitor: bool = True) -> None:
        self.bridge = bridge
        self.static = StaticFiles(SIDEBAR if root is None else root)
        self._clock = clock
        self.long_poll_seconds, self.janitor_seconds, self._use_janitor = long_poll_seconds, janitor_seconds, janitor
        self._cond = threading.Condition()
        self.channels = auth.Channels(clock)
        self.launches = auth.Launches(clock)
        self.sessions = auth.Sessions(clock)
        self._closing = False     # close() began: everything here is dropped, requests get 503
        self._finished = False    # close() is returning: nothing here writes to a socket any more
        self._in_flight = 0
        self._requests: set = set()
        self._parked = 0
        self._janitor: Optional[threading.Thread] = None

    # -- wiring --
    def attach(self) -> "WebApi":
        self.bridge.add_route_handler(self.handle)
        self.bridge.add_close_hook(self.close_begin, self.close_end)
        self.bridge.run_state_listener = self.run_state_changed
        return self

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.bridge.port}"

    @property
    def origin(self) -> str:
        return "http://" + self.host

    # -- the handler --
    def handle(self, request: Any) -> bool:
        family = _family(request.path)
        if family is None:
            return False
        call = _Call(request, time.monotonic())
        request.route_label = "unknown"
        with self._cond:
            if self._finished:
                _cut(request)
                return True
            self._in_flight += 1
            self._requests.add(request)
            if not self._closing:
                self._ensure_janitor()
                self._sweep()
        try:
            try:
                self._serve(family, call)
            except WebError as exc:
                self._error(call, exc)
            except Exception as exc:  # noqa: BLE001 - never let exception text (it can hold a secret) out
                log.error("libreoffice sidecar: unhandled failure (%s)", type(exc).__name__)
                self._error(call, WebError("internal"))
        finally:
            with self._cond:
                self._in_flight -= 1
                self._requests.discard(request)
                self._cond.notify_all()
        return True

    def _send(self, call: _Call, status: int, payload: dict, headers: tuple = ()) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = API_HEADERS + tuple(headers)
        if call.mac is not None:
            key, counter = call.mac
            headers += (("X-Sidecar-MAC", auth.response_mac(key, counter, status, data)),)
        call.request.respond(status, data, call.start, headers=headers)

    def _error(self, call: _Call, exc: WebError) -> None:
        payload: dict = {"ok": False, "error": exc.message, "code": exc.code}
        if exc.ended is not False:
            payload["ended"] = exc.ended
        if exc.code == "method_not_allowed":
            call.request.route_label = "method"
        self._send(call, exc.status, payload, exc.headers)

    def _serve(self, family: str, call: _Call) -> None:
        request = call.request
        headers = request.headers
        if not auth.host_ok(headers, self.host):
            raise WebError("bad_host")
        if self._closing or self.bridge.closed:
            raise WebError("turned_off", ended="turned_off" if family == "api" else False)
        if family == "probe":
            raise WebError("not_found")
        name, method, label, extra = self._resolve(family, request.path)
        request.route_label = label
        if request.command != method:
            raise WebError("method_not_allowed", headers=(("Allow", method),))
        if auth.carries_foreign_credential(headers, family):
            raise WebError("forbidden_credential")
        if family == "api":
            refusal = auth.api_fetch_metadata(headers, method, self.origin)
            if refusal:
                raise WebError(refusal)
        elif family == "ui" and not auth.static_fetch_allowed(headers, extra == "index.html"):
            raise WebError("cross_site")
        raw = self._body(request, MAX_CHAT_POST if name == "chat" else MAX_POST) if method == "POST" else None
        if method == "GET":
            self._no_body(request)
        if family == "ui":
            return self._static(call, extra)
        if family == "launcher":
            return self._launcher(call, name, raw, extra)
        return self._api(call, name, raw)

    def _resolve(self, family: str, path: str) -> tuple:
        route, mark, query = path.partition("?")
        if "#" in path:
            raise WebError("not_found")
        if family == "ui":
            relpath = route[len("/ui/"):] if route.startswith("/ui/") else ""
            if mark or not relpath or "%" in path or "\\" in path or ".." in relpath or not RELPATH_RE.fullmatch(relpath):
                raise WebError("not_found")
            return "static", "GET", "ui-static", relpath
        if family == "launcher" and route.startswith("/launcher/launch/"):
            if mark:
                raise WebError("not_found")
            # A malformed id is checked after the channel MAC, so its 404 is signed like an unknown id's.
            return "status", "GET", "launcher-status", route[len("/launcher/launch/"):]
        found = _ROUTES.get(route)
        if found is None or (mark and found[0] != "turn"):
            raise WebError("not_found")
        return found + ((query if mark else None),)

    def _body(self, request: Any, limit: int) -> bytes:
        headers = request.headers
        if headers.get_all("Transfer-Encoding"):
            raise WebError("bad_request")
        if not auth.json_content_type(headers):
            raise WebError("unsupported_media_type")
        lengths = auth.header_values(headers, "Content-Length")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0].strip()):
            raise WebError("length_required")
        try:
            return request.read_body(limit)
        except ChatError as exc:
            raise WebError(_BODY_ERRORS.get(exc.status, "bad_request")) from None

    @staticmethod
    def _no_body(request: Any) -> None:
        lengths = auth.header_values(request.headers, "Content-Length")
        if request.headers.get_all("Transfer-Encoding") or lengths not in ([], ["0"]):
            raise WebError("bad_request")

    @staticmethod
    def _shape(raw: Optional[bytes], name: str) -> dict:
        value = auth.parse_object(raw or b"", _BODY_KEYS[name])
        if value is None:
            raise WebError("bad_request")
        return value

    # -- static files (7) --
    def _static(self, call: _Call, relpath: str) -> None:
        found = self.static.load(relpath)
        if found is None:
            raise WebError("not_found")
        data, content_type = found
        call.request.respond(200, data, call.start, content_type=content_type, headers=STATIC_HEADERS)

    # -- readiness --
    def readiness(self) -> dict:
        bridge = self.bridge
        try:
            active = not bridge.closed and bool(bridge.fence_active())
        except Exception:  # noqa: BLE001 - an unreadable fence is an inactive one
            active = False
        if not active:
            return {"ready": False, "reason": "plugin_inactive"}
        try:
            key = bridge.api_key()
        except Exception:  # noqa: BLE001
            key = None
        if not key:
            return {"ready": False, "reason": "no_api_key"}
        if not valid_header_secret(key):
            return {"ready": False, "reason": "bad_api_key"}
        return {"ready": True, "reason": None}

    # -- launcher channel (3, 4) --
    def _launcher(self, call: _Call, name: str, raw: Optional[bytes], extra: Any) -> None:
        request = call.request
        if name == "hello":
            body = self._shape(raw, "hello")
            nonce = body["client_nonce"]
            if (not isinstance(body["v"], int) or isinstance(body["v"], bool) or body["v"] != 1
                    or not isinstance(nonce, str) or not auth.TOKEN_RE.fullmatch(nonce)):
                raise WebError("bad_request")
            token = self.bridge.token()
            if not token:
                raise WebError("no_pairing")
            with self._cond:
                server_nonce, proof = self.channels.open(token.encode("utf-8"), self.bridge.port, nonce)
            self._send(call, 200, {"ok": True, "v": 1, "server_nonce": server_nonce, "server_proof": proof,
                                   "expires_in": int(auth.CHANNEL_SECONDS), "chat": self.readiness()})
            return
        headers = request.headers
        with self._cond:
            accepted = self.channels.verify(headers.get_all("X-Sidecar-Channel"), headers.get_all("X-Sidecar-Counter"),
                                            headers.get_all("X-Sidecar-MAC"), request.command, request.path,
                                            raw if raw is not None else b"")
        if accepted is None:
            raise WebError("launcher_auth")
        call.mac = accepted   # every answer from here on carries the response MAC
        if name == "status":
            with self._cond:
                launch = self.launches.get(extra) if auth.SHORT_ID_RE.fullmatch(extra) else None
                state = launch.state if launch is not None else None
            if state is None:
                raise WebError("not_found")
            self._send(call, 200, {"ok": True, "state": state})
        elif name == "launch":
            self._launch(call, self._shape(raw, "launch"))
        else:
            self._release(call, self._shape(raw, "release"))

    def _launch(self, call: _Call, body: dict) -> None:
        document, scheme, mode, bounds = body["document"], body["host_scheme"], body["mode"], body["bounds"]
        if (not auth.valid_document(document) or scheme not in ("light", "dark", None)
                or not ((mode == "app" and auth.valid_rectangle(bounds)) or (mode == "tab" and bounds is None))):
            raise WebError("bad_request")
        document = {"document_id": document["document_id"], "title": document["title"], "type": document["type"]}
        bounds = None if bounds is None else {key: bounds[key] for key in ("x", "y", "width", "height")}
        with self._cond:
            now = self._clock()
            mine = self.sessions.for_document(document["document_id"])
            if any(session.parked or now - session.last_seen < auth.WINDOW_LIVE_SECONDS for session in mine):
                answer = (200, {"ok": True, "launch_id": None, "url": None, "window_live": True, "expires_in": 0})
            else:
                if self.launches.outstanding() >= auth.MAX_CODES:
                    raise WebError("too_many_launches")
                if len(self.sessions) >= auth.MAX_SESSIONS and not mine:
                    victim = self.sessions.evictable(self._driven)
                    if victim is None:
                        raise WebError("session_cap")
                    self._end_session(victim, "evicted")
                code, launch = self.launches.mint(document, scheme, mode, bounds)
                answer = (201, {"ok": True, "launch_id": launch.launch_id,
                                "url": f"{self.origin}/ui/index.html#launch={code}", "window_live": False,
                                "expires_in": int(auth.CODE_SECONDS)})
        self._send(call, *answer)

    def _release(self, call: _Call, body: dict) -> None:
        document_id, reason = body["document_id"], body["reason"]
        if not ((reason == "document_closed" and isinstance(document_id, str) and 0 < len(document_id) <= 512)
                or (reason == "host_quit" and document_id is None)):
            raise WebError("bad_request")
        with self._cond:
            targets = self.sessions.for_document(document_id) if document_id is not None else self.sessions.all()
            for session in targets:
                self._end_session(session, "host_gone")
            self.launches.revoke_pending(document_id)
        self._send(call, 200, {"ok": True, "released": len(targets)})

    # -- page routes (6) --
    def _api(self, call: _Call, name: str, raw: Optional[bytes]) -> None:
        if name == "session":
            return self._exchange(call, self._shape(raw, "session"))
        if name == "turn":
            query = call.request.path.partition("?")[2]
            session = self._session(call)
            return self._long_poll(call, session, query)
        body = self._shape(raw, name) if raw is not None else None
        session = self._session(call)
        if name == "state":
            readiness = self.readiness()
            with self._cond:
                payload = {"ok": True, "document": session.document, "chat": readiness,
                           "chat_label": _chat_label(session.chat_session_id),
                           "turn": self._view(session.turn) if session.turn is not None else None}
            return self._send(call, 200, payload)
        if name == "chat":
            return self._chat(call, session, body["message"])
        if name == "stop":
            return self._stop(call, session, body["turn"])
        if name == "new_chat":
            with self._cond:
                if session.starting or self.bridge.session_state(session.chat_session_id) == "driven":
                    raise WebError("turn_running")
                session.chat_session_id = auth.new_chat_session_id()
                session.turn = None
                self._cond.notify_all()
                label = _chat_label(session.chat_session_id)
            return self._send(call, 200, {"ok": True, "chat_label": label})
        with self._cond:   # close
            self._end_session(session, "closed")
        self._send(call, 200, {"ok": True})

    def _session(self, call: _Call) -> auth.Session:
        with self._cond:
            session, ended = self.sessions.lookup(call.request.headers.get_all("X-Sidecar-Session"))
            if session is None:
                raise WebError("no_session", ended=ended)
            session.last_seen = self._clock()
            return session

    def _exchange(self, call: _Call, body: dict) -> None:
        code = body["launch"]
        if not isinstance(code, str) or not auth.TOKEN_RE.fullmatch(code):
            raise WebError("bad_request")
        readiness = self.readiness()
        with self._cond:
            launch = self.launches.find_code(code)
            if launch is None:
                raise WebError("launch_expired")
            if launch.state != "pending":
                victim = self.sessions.by_digest(launch.session_digest)
                if victim is not None:
                    self._end_session(victim, "double_use")
                launch.state = "double_use"
                log.warning("libreoffice sidecar: a launch code was presented twice; the window that used it "
                            "was disconnected")
                raise WebError("launch_double_use")
            for older in self.sessions.for_document(launch.document["document_id"]):
                self._end_session(older, "replaced")
            if len(self.sessions) >= auth.MAX_SESSIONS:
                victim = self.sessions.evictable(self._driven)
                if victim is None:
                    raise WebError("session_cap")
                self._end_session(victim, "evicted")
            token, session = self.sessions.mint(launch)
            launch.state, launch.session_digest = "exchanged", session.digest
            payload = {"ok": True, "session": token, "idle_timeout": int(auth.SESSION_IDLE_SECONDS),
                       "document": session.document, "host_scheme": session.host_scheme, "mode": session.mode,
                       "bounds": session.bounds, "chat": readiness, "chat_label": _chat_label(session.chat_session_id),
                       "endpoint": self.origin, "limits": {"max_message": MAX_MESSAGE}}
        self._send(call, 200, payload)

    def _chat(self, call: _Call, session: auth.Session, message: Any) -> None:
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise WebError("message_invalid")
        with self._cond:
            if session.ended is not None:
                raise WebError("no_session", ended=session.ended)
            if session.starting:
                raise WebError("busy")
            session.starting = True
            chat_session_id, document = session.chat_session_id, session.document
            turn = Turn(auth.new_short_id(), self._clock())
        try:
            run = self.bridge.begin_turn(chat_session_id, compose_message(message.strip(), document),
                                         lambda _run, outcome: self.turn_done(turn, outcome))
        except ChatError as exc:
            raise WebError(_claim_code(exc), str(exc)) from None
        finally:
            with self._cond:
                session.starting = False
        with self._cond:
            turn.run = run
            if session.ended is not None:   # the session ended while the run was claimed
                self.bridge.request_stop(run)
                raise WebError("no_session", ended=session.ended)
            session.turn = turn
            self._cond.notify_all()
            view = self._view(turn)
        self._send(call, 202, {"ok": True, "turn": view})

    def _stop(self, call: _Call, session: auth.Session, turn_id: Any) -> None:
        if not isinstance(turn_id, str) or not auth.SHORT_ID_RE.fullmatch(turn_id):
            raise WebError("bad_request")
        with self._cond:
            turn = session.turn
            if turn is None or turn.id != turn_id:
                raise WebError("unknown_turn")
            if turn.state == "done":
                answer = (200, {"ok": True, "turn": self._view(turn)})
            else:
                self._stop_turn(turn)
                answer = (202, {"ok": True, "turn": self._view(turn)})
        self._send(call, *answer)

    def _long_poll(self, call: _Call, session: auth.Session, query: str) -> None:
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise WebError("bad_request") from None
        params = dict(pairs)
        if (len(pairs) != 3 or set(params) != {"id", "seq", "have"} or not auth.SHORT_ID_RE.fullmatch(params["id"])
                or not all(NUMBER_RE.fullmatch(params[key]) and int(params[key]) <= MAX_QUERY_NUMBER
                           for key in ("seq", "have"))):
            raise WebError("bad_request")
        seen, have = int(params["seq"]), int(params["have"])
        with self._cond:
            turn = session.turn
            if turn is None or turn.id != params["id"]:
                raise WebError("unknown_turn")
            turn.poll_gen += 1
            generation = turn.poll_gen
            turn.in_flight += 1
            turn.last_poll = self._clock()
            session.parked += 1
            self._cond.notify_all()   # an older long-poll of this turn answers now (superseded)
            parked = False
            deadline = time.monotonic() + self.long_poll_seconds
            try:
                while True:
                    if self._closing:
                        raise WebError("turned_off", ended="turned_off")
                    if session.ended is not None:
                        raise WebError("no_session", ended=session.ended)
                    if session.turn is not turn:
                        raise WebError("unknown_turn")
                    if turn.poll_gen != generation:
                        payload = {"ok": True, "superseded": True, "turn": self._view(turn, have)}
                        break
                    left = deadline - time.monotonic()
                    if turn.seq > seen or turn.state == "done" or left <= 0:
                        payload = {"ok": True, "superseded": False, "turn": self._view(turn, have)}
                        break
                    if not parked:
                        if self._parked >= MAX_ACTIVE:   # the connection budget (12): never more parked
                            payload = {"ok": True, "superseded": False, "turn": self._view(turn, have)}
                            break
                        parked = True
                        self._parked += 1
                    self._cond.wait(left)
            finally:
                if parked:
                    self._parked -= 1
                turn.in_flight -= 1
                turn.last_poll = self._clock()
                session.parked -= 1
        self._send(call, 200, payload)

    # -- turn records --
    def _view(self, turn: Turn, have: Optional[int] = None) -> dict:
        run = turn.run
        hermes = None
        if turn.state != "done" and run is not None:
            hermes = HERMES_STATES.get(run.hermes_state)
        view: dict = {"id": turn.id, "seq": turn.seq, "state": turn.state, "hermes_state": hermes}
        if have is not None:
            start = min(have, len(turn.reply))
            view["text_from"], view["text"] = start, turn.reply[start:]
        view["text_length"] = len(turn.reply)
        view["activity"] = []
        outcome = turn.outcome
        view["outcome"] = None if outcome is None else {
            "kind": outcome.kind, "message": None if outcome.kind == "completed" else outcome.text}
        return view

    def turn_done(self, turn: Turn, outcome: TurnOutcome) -> None:
        """``on_done`` of ``begin_turn``: called once, on the turn's worker, without the bridge's lock."""
        with self._cond:
            turn.outcome = outcome
            turn.reply = outcome.text[:MAX_REPLY] if outcome.kind == "completed" else ""
            turn.state = "done"
            turn.seq += 1
            self._cond.notify_all()

    def run_state_changed(self, run: Any) -> None:
        """The bridge observed a new Runs API state for ``run``: wake that turn's long-poll."""
        with self._cond:
            for session in self.sessions.all():
                turn = session.turn
                if turn is not None and turn.run is run and turn.state != "done":
                    turn.seq += 1
                    self._cond.notify_all()

    def _stop_turn(self, turn: Turn) -> None:
        """Ask the bridge to stop a turn's run (lock held). The worker sends the stop at once."""
        if turn.run is not None and turn.state != "done" and self.bridge.request_stop(turn.run):
            if turn.state != "stopping":
                turn.state = "stopping"
                turn.seq += 1
            self._cond.notify_all()

    def _driven(self, session: auth.Session) -> bool:
        return session.starting or self.bridge.session_state(session.chat_session_id) == "driven"

    def _end_session(self, session: auth.Session, reason: str) -> None:
        """End a session (lock held) and ask its driven run to stop; parked long-polls answer."""
        if self.sessions.end(session, reason):
            if session.turn is not None and reason != "turned_off":   # at unload the bridge stops runs itself
                self._stop_turn(session.turn)
            self._cond.notify_all()

    # -- housekeeping --
    def _ensure_janitor(self) -> None:
        if self._janitor is None and self._use_janitor:
            thread = threading.Thread(target=self._janitor_loop, name="HermesLibreOfficeSidecarJanitor", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                log.error("libreoffice sidecar: the housekeeping thread could not start")
                return
            self._janitor = thread

    def _janitor_loop(self) -> None:
        with self._cond:
            while not self._closing:
                try:
                    self._sweep()
                except Exception as exc:  # noqa: BLE001 - housekeeping must keep running
                    log.error("libreoffice sidecar: housekeeping failed (%s)", type(exc).__name__)
                self._cond.wait(self.janitor_seconds)

    def sweep(self) -> None:
        with self._cond:
            self._sweep()

    def _sweep(self) -> None:
        """Expire channels, codes and idle sessions; ask for a stop when a driven turn's window has
        had no long-poll for ``WINDOW_GONE_SECONDS`` (lock held)."""
        if self._closing:
            return
        self.channels.expire()
        self.launches.expire()
        for session in self.sessions.idle():
            self._end_session(session, "idle")
        now = self._clock()
        for session in self.sessions.all():
            turn = session.turn
            if (turn is not None and turn.state == "working" and turn.run is not None and not turn.gone
                    and turn.in_flight == 0 and now - turn.last_poll >= WINDOW_GONE_SECONDS):
                turn.gone = True
                log.info("libreoffice sidecar: a window stopped asking for its reply; asking Hermes to stop it")
                self._stop_turn(turn)

    # -- unload (R12) --
    def close_begin(self) -> None:
        """Ingress is closed: drop every session, code and channel and wake parked long-polls,
        which answer 503 ``turned_off`` once. Runs are stopped by the bridge's close()."""
        with self._cond:
            self._closing = True
            for session in self.sessions.all():
                self._end_session(session, "turned_off")
            self.launches.clear()
            self.channels.clear()
            self._cond.notify_all()

    def close_end(self, seconds_left: float) -> None:
        """Before close() returns: let the requests in flight finish answering (within what is left
        of the budget), then cut off any still open, so no handler of these routes writes to a
        socket after close() has returned."""
        deadline = time.monotonic() + max(0.0, seconds_left)
        with self._cond:
            self._closing = True
            while self._in_flight:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            self._finished = True
            stragglers = list(self._requests)
            self._cond.notify_all()
        for request in stragglers:
            _cut(request)


def _claim_code(exc: ChatError) -> str:
    text = str(exc)
    if exc.status == 409:
        return "busy" if text == ERRORS["busy"][1] else "unresolved"
    if exc.status == 429:
        return "too_many"
    if exc.status == 503:
        return "worker_failed" if text == TURN_WORKER_FAILED else "plugin_inactive"
    return "internal"


def attach(bridge: Any, **options: Any) -> WebApi:
    """Register the Sidecar window routes on ``bridge`` (called by ``ChatBridge.start``)."""
    return WebApi(bridge, **options).attach()
