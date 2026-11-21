# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Loopback chat endpoint for the LibreOffice sidebar panel.

It runs inside the Hermes process that serves the API server (the gateway). The panel posts one
message at a time; this endpoint checks the pairing token the two sides already share, starts a
Hermes API-server run for the panel's session with ``API_SERVER_KEY`` (``POST /v1/runs``), waits
for it and returns the reply.

Security model:
- binds 127.0.0.1 only; ``Host`` must be exactly ``127.0.0.1:<port>``; any ``Origin`` header is
  refused, so a web page cannot use it;
- every request needs the pairing token (constant-time compare); without a configured token the
  endpoint answers 503 and forwards nothing;
- ``API_SERVER_KEY`` is read through Hermes's secret store, held in memory only, checked to be a
  valid header value before use, sent only to the loopback API server and never returned to the
  panel or logged;
- at most ``MAX_HANDLERS`` connections are served at once (checked before a thread is started;
  more are refused at once) and each request must arrive, headers and body, within ONE
  ``READ_DEADLINE_SECONDS`` deadline, so slow or silent local clients cannot pin threads;
- bodies are bounded and strictly shaped; logs carry a fixed route label (health, chat or
  unknown), status and elapsed time only, never paths, messages, document names, tokens or keys;
- at most ``MAX_ACTIVE`` chats run at once and one per session;
- the endpoint forwards only while the plugin's tool fence is registered (``fence_active``);
  ``close()`` refuses new work, asks every run whose id it knows to stop through the documented
  ``POST /v1/runs/{id}/stop`` control (even one whose chat thread is blocked in a status poll)
  and waits, within ONE time budget, until they have settled.

Run ownership: a run Hermes may have admitted stays owned by this endpoint (it keeps its
session busy and counts against ``MAX_ACTIVE``) until a terminal state is observed. Run creation
carries an ``Idempotency-Key``, so a lost acknowledgement is reconciled by resending the SAME
keyed request (Hermes replays the admitted run instead of starting another), never by an unkeyed
resend. When polling fails after admission the run is asked to stop with the key it was started
with and watched for a bounded time; a run that cannot be confirmed stopped stays owned as
unresolved and is never reported as drained. Only an observed settled state releases a run, once,
whichever thread observes it first.
"""
from __future__ import annotations

import asyncio
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import sys
import logging
import re
import selectors
import threading
import time
from typing import Any, Callable, Optional
import uuid

_WINDOWS = sys.platform == "win32"
_ServerSelector = getattr(selectors, "PollSelector", selectors.SelectSelector)   # as socketserver picks it

log = logging.getLogger(__name__)

# The sidebar chat is OFF unless the user sets ``chat_port`` (the panel calls 8661). This constant
# is the one code default, also used for a missing or invalid setting; plugin.yaml's
# ``config_schema.chat_port.default`` must equal it (tests/test_manifest.py).
DEFAULT_CHAT_PORT = 0
PANEL_CHAT_PORT = 8661
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8642
MAX_BODY = 64 * 1024
MAX_MESSAGE = 8000
MAX_REPLY = 200_000
MAX_UPSTREAM_BODY = 8 * 1024 * 1024
MAX_ACTIVE = 4
MAX_HANDLERS = 16
READ_DEADLINE_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 10.0
UPSTREAM_TIMEOUT_SECONDS = 600.0      # one whole chat turn, from the run's start to its reply
UPSTREAM_CALL_SECONDS = 30.0          # one call to the API server (start, poll, stop), at most
RECONCILE_SECONDS = 10.0              # after a stop request or a lost answer: time to confirm the run's state
CLOSE_SECONDS = 10.0                  # close(): ONE budget for the whole shutdown, stops included
LATE_STOP_SECONDS = 5.0               # one stop for a run whose id became known only after that budget
TOKEN_HEADER = "X-Hermes-Plugin-Key"
SESSION_RE = re.compile(r"^lo-[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DOCUMENT_TYPES = ("calc", "writer", "impress")
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
# Hermes's Runs API states of a run that may still execute (gateway/platforms/api_server_runs.py
# and api_server_authority_runs.py, 0.23.1 and 0.23.3). ``unknown`` means Hermes lost the run's
# owner (no live execution) and its outcome is not known. Any other value is a protocol failure.
LIVE_RUN_STATUSES = frozenset({"queued", "started", "running", "stopping", "waiting_for_approval"})
_SETTLED_STATES = TERMINAL_RUN_STATUSES | {"unknown"}
_MAX_KEY_LENGTH = 4096
OUTCOME_UNCERTAIN = ("It is not known whether Hermes carried out this request or which changes it made. "
                     "Check the document before you send it again; do not resend edits blindly.")
PLUGIN_TURNED_OFF = "the reply was stopped because the LibreOffice plugin was turned off"


class SecretUnavailable(Exception):
    """The secret store cannot be read in this context (an unscoped read in a multiplexed
    gateway's worker thread). This is the only failure that keeps a cached value."""


class SecretProvider:
    """A secret read through Hermes's secret store and kept in memory only.

    Hermes scopes secret reads to the owning profile; the endpoint's worker threads are not in
    that scope, so they reuse the value read at a scoped moment (plugin load, a tool call). A
    successful read is authoritative: an empty or missing value clears the cache, so a removed
    pairing token or API key stops working at the next scoped read. Only ``SecretUnavailable``
    keeps the last value; any other read failure clears it (fail closed).
    """

    def __init__(self, reader: Callable[[], Optional[str]]) -> None:
        self._reader = reader
        self._lock = threading.Lock()
        self._value: Optional[str] = None

    def refresh(self) -> None:
        try:
            value = self._reader()
        except SecretUnavailable:
            return
        except Exception:  # noqa: BLE001 - an unknown failure is not a reason to keep a secret
            value = None
        value = value.strip() if isinstance(value, str) else ""
        with self._lock:
            self._value = value or None

    def clear(self) -> None:
        with self._lock:
            self._value = None

    def __call__(self) -> Optional[str]:
        self.refresh()
        with self._lock:
            return self._value

    def __repr__(self) -> str:
        return "SecretProvider(<redacted>)"


class ChatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _NotSent(ChatError):
    """The API server could not be reached at all: the request was never sent."""


class _Run:
    """One run this endpoint asked Hermes for, owned from before its creation request is sent
    until a terminal state is observed (or it was never admitted).

    ``driven`` is True while a thread owns its waiting (the chat thread, or a cleanup thread);
    ``key`` is the API key it was started with (stop and status use only that key, never a newer
    one); ``run_id`` is None until Hermes's acknowledgement arrives. ``settled`` is set once, when
    the run is released; ``stopping`` is True while a stop request is in flight, so concurrent
    threads never send two at once; ``close_seen`` marks a run close() has already given a stop
    thread."""

    __slots__ = ("session_id", "key", "idempotency_key", "body", "run_id", "deadline", "driven", "sent",
                 "stop_acked", "stop_tried", "stopping", "settled", "close_seen")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.key = ""
        self.idempotency_key = "lo-run-" + uuid.uuid4().hex
        self.body: dict[str, Any] = {}
        self.run_id: Optional[str] = None
        self.deadline = 0.0
        self.driven = True
        self.sent = False
        self.stop_acked = False
        self.stop_tried = False
        self.stopping = False
        self.settled = False
        self.close_seen = False


def _known_state(value: Any) -> Optional[str]:
    """The ``status`` of a Runs API answer when it is one of the states Hermes reports, else
    None (a missing, non-string or unknown value is a protocol failure, never an exception)."""
    state = value.get("status") if isinstance(value, dict) else None
    if isinstance(state, str) and (state in _SETTLED_STATES or state in LIVE_RUN_STATUSES):
        return state
    return None


def valid_header_secret(value: Any) -> bool:
    """A credential that can be sent as an HTTP header value as is: visible ASCII, bounded."""
    return (isinstance(value, str) and 0 < len(value) <= _MAX_KEY_LENGTH
            and all(33 <= ord(char) <= 126 for char in value))


def _clean(value: str, limit: int) -> str:
    """Printable, single-line, bounded text for the context line (document names are untrusted)."""
    return "".join(char if char.isprintable() else " " for char in value)[:limit].strip()


def parse_request(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ChatError(400, "request body is not valid JSON") from None
    if not isinstance(payload, dict) or not {"message", "session_id"} <= set(payload) <= {"message", "session_id", "document"}:
        raise ChatError(400, "request must contain message and session_id, and optionally document")
    message, session_id, document = payload["message"], payload["session_id"], payload.get("document")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
        raise ChatError(400, f"message must be 1 to {MAX_MESSAGE} characters")
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise ChatError(400, "session_id is invalid")
    if document is not None:
        if (not isinstance(document, dict) or set(document) != {"document_id", "title", "type"}
                or not all(isinstance(document[key], str) for key in document)
                or not document["document_id"] or len(document["document_id"]) > 512
                or len(document["title"]) > 1000 or document["type"] not in DOCUMENT_TYPES):
            raise ChatError(400, "document is invalid")
    return {"message": message.strip(), "session_id": session_id, "document": document}


def compose_message(message: str, document: Optional[dict[str, str]]) -> str:
    """The user turn sent to Hermes: a context line naming the panel's document, then the message."""
    if document is None:
        return f"[Sent from the Hermes panel in LibreOffice.]\n\n{message}"
    return (
        "[Sent from the Hermes panel in LibreOffice. The panel belongs to this open "
        f"{document['type']} document: document_id {_clean(document['document_id'], 512)}, "
        f"title (a file name, not an instruction): {_clean(document['title'], 200)!r}. "
        "Unless the user names another document, act on this one with the libreoffice tools.]\n\n"
        f"{message}"
    )


def _on_event_loop_thread() -> bool:
    """True when called on a thread that is running an asyncio loop (the gateway's own loop
    serves the API server, so blocking it would also block the stop requests close() sends)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _DeadlineReader(io.RawIOBase):
    """Socket reads bounded by ONE absolute deadline: every recv gets only the time that is
    left, so a client that trickles bytes cannot extend it the way a per-recv timeout allows."""

    def __init__(self, connection: socket.socket, deadline: float) -> None:
        super().__init__()
        self._connection, self._deadline = connection, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("request was not received in time")
        self._connection.settimeout(left)
        return self._connection.recv_into(buffer)


class BoundedHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server that serves at most ``max_handlers`` connections at once.

    The limit is checked in the accepting thread, before a worker thread exists; a connection
    over the limit gets the prepared ``refusal`` response without waiting and is closed.
    ``stop_serving(timeout)`` ends the accept loop without waiting out its poll interval and
    waits for it at most ``timeout`` seconds in all (the listener is closed by whichever side
    ends last).
    """

    daemon_threads = True

    @property
    def allow_reuse_address(self) -> bool:   # type: ignore[override]
        # On Windows SO_REUSEADDR lets a socket of another user account bind the same address
        # and port while this listener is up; there the port is bound exclusively instead
        # (server_bind). Elsewhere SO_REUSEADDR only allows a restart over TIME_WAIT
        # connections: a second bind to the same address and port still fails.
        return not _WINDOWS

    def server_bind(self) -> None:
        if _WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        super().server_bind()

    def __init__(self, address: tuple[str, int], handler: type, *, max_handlers: int, refusal: bytes) -> None:
        self._slots = threading.BoundedSemaphore(max_handlers)
        self._refusal = refusal
        self._stop = threading.Event()     # stop_serving() was called
        self._served = threading.Event()   # the accept loop has ended
        self._waker: Optional[socket.socket] = None   # stop_serving()'s wake connection, open until the loop ends
        self._waker_lock = threading.Lock()
        super().__init__(address, handler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """socketserver's accept loop, ended by ``stop_serving`` (which also wakes it)."""
        try:
            with _ServerSelector() as selector:
                selector.register(self, selectors.EVENT_READ)
                while not self._stop.is_set():
                    ready = selector.select(poll_interval)
                    if self._stop.is_set():
                        break
                    if ready:
                        self._handle_request_noblock()
                    self.service_actions()
        finally:
            self._served.set()
            self._drop_waker()
            if self._stop.is_set():
                self.server_close()   # stop_serving() stopped waiting for this loop: close here

    def stop_serving(self, timeout: Optional[float]) -> bool:
        """End the accept loop and wait for it at most ``timeout`` seconds (None: no limit),
        counted from this call: waking the loop takes none of it (a non-blocking loopback
        connection makes a waiting select() return at once, so this does not wait out the poll
        interval), and the wait gets what is left. True when the loop has ended; the listener is
        then closed here, else the loop closes it when it ends."""
        until = None if timeout is None else time.monotonic() + max(0.0, timeout)
        self._stop.set()
        if not self._served.is_set():
            self._wake()
        ended = self._served.wait(None if until is None else max(0.0, until - time.monotonic()))
        if ended:
            self._drop_waker()
            self.server_close()
        return ended

    def _wake(self) -> None:
        """Connect to the listener without waiting: the kernel completes a loopback handshake on
        its own, which makes the listener readable. The socket stays open until the loop ends
        (closing it at once could withdraw the connection before the loop sees it)."""
        with self._waker_lock:
            if self._waker is not None or self._served.is_set():
                return
            try:
                waker = socket.socket(self.address_family, socket.SOCK_STREAM)
            except OSError:
                return
            self._waker = waker
            try:
                waker.setblocking(False)
                waker.connect_ex(self.server_address)
            except OSError:
                pass

    def _drop_waker(self) -> None:
        with self._waker_lock:
            waker, self._waker = self._waker, None
        if waker is not None:
            waker.close()

    def shutdown(self) -> None:
        self.stop_serving(None)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.setblocking(False)   # the accepting thread never waits on this client
                request.send(self._refusal)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _refusal_response(protocol: str, status: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    head = (f"{protocol} {status}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
    return head.encode("ascii") + data


class ChatBridge:
    def __init__(self, *, port: int, token: Callable[[], Optional[str]], api_key: Callable[[], Optional[str]],
                 api_host: str = DEFAULT_API_HOST, api_port: int = DEFAULT_API_PORT, api_prefix: str = "",
                 timeout: float = UPSTREAM_TIMEOUT_SECONDS, fence_active: Callable[[], bool] = lambda: True,
                 max_handlers: Optional[int] = None, read_deadline: Optional[float] = None,
                 poll_interval: float = 0.25) -> None:
        if api_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the Hermes API server must be a loopback address")
        self.port, self.token, self.api_key = port, token, api_key
        self.api_host, self.api_port, self.timeout = api_host, api_port, float(timeout)
        self.runs_path = api_prefix + "/v1/runs"
        self.fence_active = fence_active
        self.max_handlers = int(MAX_HANDLERS if max_handlers is None else max_handlers)
        self.read_deadline = float(READ_DEADLINE_SECONDS if read_deadline is None else read_deadline)
        self.poll_interval = float(poll_interval)
        self._server: Optional[BoundedHTTPServer] = None
        self._lock = threading.Condition()
        self._runs: dict[str, _Run] = {}   # session_id -> the run it owns (live or unresolved)
        self._closed = False
        self._close_deadline: Optional[float] = None
        self._closing = threading.Event()
        self._close_workers = 0                                # close()'s stop threads still running
        self._polls: set[http.client.HTTPConnection] = set()   # status polls in flight

    # -- lifecycle --
    def start(self) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("a closed chat endpoint cannot be started again")
            if self._server is None:
                refusal = _refusal_response("HTTP/1.1", "503 Service Unavailable",
                                            {"ok": False, "error": "the chat endpoint is busy; try again shortly"})
                server = BoundedHTTPServer(("127.0.0.1", self.port), _handler_for(self),
                                           max_handlers=self.max_handlers, refusal=refusal)
                self.port = server.server_address[1]
                threading.Thread(target=server.serve_forever, name="HermesLibreOfficeChat", daemon=True).start()
                self._server = server
            return self.port

    @property
    def closed(self) -> bool:
        return self._closed

    def owned_runs(self) -> int:
        """How many runs this endpoint still owns: in flight, or unresolved."""
        with self._lock:
            return len(self._runs)

    def close(self, budget: Optional[float] = None) -> int:
        """Close ingress, then stop the runs this endpoint owns, all within ONE time budget.

        Ordered for the plugin's unload: new requests are refused first, then every owned run
        whose id is known is asked to stop through the API server's documented stop control (with
        the key it was started with) by a thread of its own, also when its chat thread is blocked
        in a status poll, and watched until it settles; only then is the listener shut down, and
        that waits at most what is left of the budget (on an event loop, at most 0.05 s). Every call
        gets only the time left, so unresponsive endpoints cannot add up; close() tries to cut off
        status polls started before it when it returns (a poll already reading a response body
        may stay blocked until that read completes or times out). Returns how many runs are still owned when it
        returns: 0 means every run was seen to settle; anything else is unresolved (they may still
        be running). It returns as soon as nothing is left to wait for, which can be before the
        budget ends (a run whose creation was never answered has no id and cannot be stopped).
        On an asyncio event-loop thread it cannot wait (that would block the API server answering
        the stops): the work continues in the background and the count of runs not yet confirmed
        is returned. Idempotent; a second call keeps the first call's budget.
        """
        with self._lock:
            if self._close_deadline is None:
                self._close_deadline = time.monotonic() + max(0.0, CLOSE_SECONDS if budget is None else budget)
            deadline = self._close_deadline
            self._closed = True
            server, self._server = self._server, None
            workers = []
            for run in self._runs.values():
                if run.run_id and not run.close_seen:
                    run.close_seen = True
                    run.stop_acked = False                  # unload asks again, whatever was asked before
                    workers.append((run, not run.driven))   # an idle run: its stop thread drives it
                    run.driven = True
            self._close_workers += len(workers)
            polls = list(self._polls)                         # started before close: not budget-bound
            self._lock.notify_all()
        self._closing.set()
        for run, drive in workers:   # one thread per run, whoever else is waiting on it, before the listener
            threading.Thread(target=self._close_worker, args=(run, deadline, drive),
                             name="HermesLibreOfficeChatStop", daemon=True).start()
        on_loop = _on_event_loop_thread()
        if server is not None:   # the accept loop ends at once (woken); waiting for it is bounded too
            left = max(0.0, deadline - time.monotonic())
            server.stop_serving(min(0.05, left) if on_loop else left)
        with self._lock:
            while not on_loop and (self._close_workers or any(run.driven for run in self._runs.values())):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._lock.wait(left)
            remaining = len(self._runs)
        if polls:
            if on_loop:
                timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._cut_off, args=(polls,))
                timer.daemon = True
                timer.start()
            else:
                self._cut_off(polls)
        if remaining:
            log.warning("libreoffice chat endpoint closed with %d run(s) not confirmed stopped; they may still "
                        "be running%s", remaining,
                        " (closed on an event loop, so it could not wait)" if on_loop else "")
        return remaining

    def _close_worker(self, run: _Run, deadline: float, drive: bool) -> None:
        try:
            self._reconcile(run, deadline, drive=drive)
        finally:
            with self._lock:
                self._close_workers -= 1
                self._lock.notify_all()

    @staticmethod
    def _cut_off(polls: list) -> None:
        """Try to end status polls that were already waiting when close() began: their socket
        timeout was chosen before the close budget existed. Best effort: a poll whose response
        body is being read may have no connection socket left to shut down and then finishes on
        its own read timeout. A cut-off poll is a lost observation."""
        for connection in polls:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if _WINDOWS:
                    # On Windows shutdown() does not wake a recv() already waiting in another
                    # thread; closing the OS socket does. socket.socket.close() defers that while
                    # the response file still references the socket, so close the base socket.
                    try:
                        super(socket.socket, sock).close()
                    except OSError:
                        pass

    def stop(self) -> None:
        """Close the endpoint (kept for callers of the earlier API)."""
        self.close()

    # -- ownership --
    def _claim(self, session_id: str) -> _Run:
        """Reserve the session and one of ``MAX_ACTIVE`` slots before anything is sent. A slot
        held by an unresolved run is checked again (stop, then status, bounded) before refusing."""
        for attempt in (0, 1):
            with self._lock:
                if self._closed or not self.fence_active():
                    raise ChatError(503, "the LibreOffice plugin is not active in Hermes: enable it and restart the gateway")
                held = self._runs.get(session_id)
                if held is None and len(self._runs) < MAX_ACTIVE:
                    run = self._runs[session_id] = _Run(session_id)
                    return run
                if held is not None and held.driven:
                    raise ChatError(409, "this chat is still waiting for a reply")
                pending = [run for run in ([held] if held is not None else list(self._runs.values()))
                           if not run.driven and run.run_id]
                if attempt or not pending:
                    if held is not None:
                        raise ChatError(409, "an earlier message in this chat is still unresolved in Hermes. "
                                             + OUTCOME_UNCERTAIN + " Start a New chat to continue.")
                    raise ChatError(429, "too many chats are running or unresolved; try again shortly")
                for run in pending:
                    run.driven = True
            deadline = time.monotonic() + RECONCILE_SECONDS
            threads = [threading.Thread(target=self._reconcile, args=(run, deadline), daemon=True) for run in pending]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()) + 1.0)
        raise AssertionError("unreachable")

    def _release(self, run: _Run) -> None:
        """The run settled, or was never admitted: free its session and slot (once; a second
        release of the same run, from another thread that saw it settle too, changes nothing)."""
        with self._lock:
            if self._runs.get(run.session_id) is run:
                del self._runs[run.session_id]
            run.settled = True
            run.driven = False
            self._lock.notify_all()

    def _abandon(self, run: _Run) -> None:
        """No thread watches the run any more and its state is unknown: it stays owned."""
        with self._lock:
            if run.settled:   # another thread saw it settle: nothing is left unresolved
                return
            run.driven = False
            self._lock.notify_all()
        log.warning("libreoffice chat: a panel run could not be confirmed finished or stopped; it stays tracked")

    def _limit(self, deadline: float) -> float:
        """``deadline``, or the close budget's end when that comes first."""
        with self._lock:
            close = self._close_deadline
        return deadline if close is None else min(deadline, close)

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            if self._closed:
                time.sleep(seconds)
            else:
                self._closing.wait(seconds)

    # -- one chat turn --
    def chat(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = request["session_id"]
        run = self._claim(session_id)
        try:
            return {"reply": self._ask(compose_message(request["message"], request["document"]), run),
                    "session_id": session_id}
        finally:
            if run.driven:   # an unexpected failure: release only what was certainly never sent
                (self._abandon if run.sent else self._release)(run)

    def _call(self, key: str, method: str, path: str, payload: Optional[dict[str, Any]] = None,
              *, timeout: float = UPSTREAM_CALL_SECONDS, headers: Optional[dict[str, str]] = None) -> tuple[int, Any]:
        """One request to the loopback API server -> ``(status, parsed JSON or None)``. Raises
        ``_NotSent`` when no connection could be made (nothing was sent) and ``ChatError`` when
        the request may have arrived but no answer was read. Errors never carry exception text:
        it can echo a header, and the key is a header."""
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        send = {"Authorization": f"Bearer {key}", "Accept": "application/json", "Connection": "close"}
        if body is not None:
            send.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        send.update(headers or {})
        connection = http.client.HTTPConnection(self.api_host, self.api_port, timeout=max(0.1, timeout))
        poll = method == "GET"
        try:
            try:
                connection.connect()
            except OSError:
                raise _NotSent(502, "the Hermes API server did not answer: enable it and start the Hermes gateway") from None
            if poll:   # close() may cut it off (see _cut_off)
                with self._lock:
                    self._polls.add(connection)
            try:
                connection.request(method, path, body=body, headers=send)
                response = connection.getresponse()
                raw = response.read(MAX_UPSTREAM_BODY + 1)
            except (OSError, http.client.HTTPException, ValueError, UnicodeError):
                raise ChatError(502, "the Hermes API server did not answer: enable it and start the Hermes gateway") from None
        finally:
            if poll:
                with self._lock:
                    self._polls.discard(connection)
            connection.close()
        if len(raw) > MAX_UPSTREAM_BODY:
            raise ChatError(502, "the Hermes API server sent a reply this plugin could not read")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        return response.status, value

    def _ask(self, content: str, run: _Run) -> str:
        key = self.api_key()
        if not key:
            self._release(run)
            raise ChatError(503, "Hermes's API server key is not available: enable the API server and set API_SERVER_KEY")
        if not valid_header_secret(key):
            self._release(run)
            raise ChatError(503, "Hermes's API server key cannot be used as a header value: set API_SERVER_KEY again")
        run.key, run.body = key, {"input": content, "session_id": run.session_id}
        run.deadline = time.monotonic() + self.timeout
        self._create(run)
        return self._follow(run)

    def _create(self, run: _Run) -> None:
        """``POST /v1/runs`` with the run's ``Idempotency-Key``. A definite refusal of the first
        request frees the run. Once a request may have been admitted without an answer, only a
        run id ends this: the SAME keyed request is resent (Hermes replays the admitted run and
        starts nothing new), within ``RECONCILE_SECONDS``; then the run stays unresolved."""
        headers = {"Idempotency-Key": run.idempotency_key}
        uncertain_until: Optional[float] = None
        delay = self.poll_interval
        while True:
            if self._closed and not run.sent:   # closed before anything was sent: nothing to stop
                self._release(run)
                raise ChatError(503, PLUGIN_TURNED_OFF)
            limit = self._limit(run.deadline if uncertain_until is None else min(run.deadline, uncertain_until))
            left = limit - time.monotonic()
            if left <= 0:
                self._abandon(run)
                raise ChatError(503 if self._closed else 502, OUTCOME_UNCERTAIN)
            run.sent = True
            try:
                status, value = self._call(run.key, "POST", self.runs_path, run.body,
                                           timeout=min(UPSTREAM_CALL_SECONDS, left), headers=headers)
            except _NotSent:
                if uncertain_until is None:   # nothing was ever delivered: nothing was admitted
                    self._release(run)
                    raise
                status, value = 0, None
            except ChatError:
                status, value = 0, None
                if uncertain_until is None:
                    uncertain_until = time.monotonic() + RECONCILE_SECONDS
            run_id = value.get("run_id") if isinstance(value, dict) else None
            if status in (200, 202) and isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id):
                with self._lock:
                    run.run_id = run_id
                return
            if uncertain_until is None:
                if status not in (0, 200, 202):   # the API server answered with an error: it admitted nothing
                    self._release(run)
                    if status in (401, 403):
                        raise ChatError(502, "the Hermes API server refused the key (check API_SERVER_KEY)")
                    if status == 429:
                        raise ChatError(429, "Hermes is busy with other runs; try again shortly")
                    raise ChatError(502, f"the Hermes API server answered with an error (HTTP {status})")
                uncertain_until = time.monotonic() + RECONCILE_SECONDS   # an answer without a run id
            # A replay that is refused does not prove the first request was not admitted.
            self._pause(min(delay, max(0.0, limit - time.monotonic())))
            delay = min(delay * 2, 4 * self.poll_interval)

    def _observe(self, run: _Run, timeout: float) -> tuple[str, dict[str, Any]]:
        """``GET /v1/runs/{id}`` with the run's own key -> ``(state, body)``; anything but a
        well-formed known state raises ``ChatError`` (the run's state is then not known)."""
        status, value = self._call(run.key, "GET", f"{self.runs_path}/{run.run_id}", timeout=timeout)
        state = _known_state(value) if status == 200 else None
        if state is None:
            raise ChatError(502, "the Hermes API server lost track of this reply")
        return state, value

    def _request_stop(self, run: _Run, timeout: float) -> Optional[tuple[str, dict[str, Any]]]:
        """``POST /v1/runs/{id}/stop`` with the run's own key. Returns ``(state, body)`` when the
        answer already reports a settled run. Only an answer naming a known state acknowledges
        the stop; a refused, unanswered or malformed one leaves ``stop_acked`` False, so it is
        requested again. One stop per run is in flight at a time: a concurrent caller gets None."""
        with self._lock:
            if run.stopping or run.settled:
                return None
            run.stopping = run.stop_tried = True
        try:
            status, value = self._call(run.key, "POST", f"{self.runs_path}/{run.run_id}/stop", {}, timeout=timeout)
        except ChatError:
            return None
        finally:
            with self._lock:
                run.stopping = False
        state = _known_state(value) if status == 200 else None
        if state is None:
            return None
        run.stop_acked = True
        if state in _SETTLED_STATES:
            return state, value
        return None

    def _late_stop(self, run: _Run) -> None:
        """A run whose id became known only after close()'s budget ended (a late creation
        answer) and that was never asked to stop: ask once, best effort. It stays owned unless
        the answer reports it settled. close() has already returned and the plugin's tool fence
        may already be gone, so this is never counted as a stop made under the fence."""
        if run.run_id is None or run.stop_tried or run.settled:
            return
        log.warning("libreoffice chat: a panel run became known after the endpoint closed; asking it to stop "
                    "(the tool fence may already be removed)")
        answer = self._request_stop(run, LATE_STOP_SECONDS)
        if answer is not None:
            self._release(run)

    def _follow(self, run: _Run, stop_reason: Optional[str] = None, settle_by: float = 0.0,
                drive: bool = True) -> str:
        """Watch the run until it settles. A stop is requested when the turn's deadline passes
        (``timeout``), when its state can no longer be read (``lost``) or when the endpoint
        closes (``closed``); after that the run has ``RECONCILE_SECONDS`` (or what is left of
        the close budget) to be seen settled, otherwise it stays owned as unresolved.

        ``drive`` is False for close()'s helper on a run another thread drives (one blocked in a
        call): it stops and observes the same way, but leaves abandoning to that thread."""
        delay = self.poll_interval
        while True:
            if run.settled:   # another thread saw it settle and released it
                raise ChatError(503, PLUGIN_TURNED_OFF) if self._closed else ChatError(502, OUTCOME_UNCERTAIN)
            now = time.monotonic()
            if stop_reason is None and (self._closed or now >= run.deadline):
                stop_reason, settle_by = ("closed" if self._closed else "timeout"), now + RECONCILE_SECONDS
            limit = self._limit(settle_by if stop_reason else run.deadline)
            if stop_reason and not run.stop_acked and limit > time.monotonic():
                answer = self._request_stop(run, min(UPSTREAM_CALL_SECONDS, limit - time.monotonic()))
                if answer is not None:
                    return self._settled(run, answer[0], answer[1], stop_reason)
            left = limit - time.monotonic()
            if left <= 0:
                if stop_reason is None:
                    continue   # the turn's deadline: ask for a stop first
                if not drive:
                    raise ChatError(503, OUTCOME_UNCERTAIN)
                self._abandon(run)
                if self._closed:
                    self._late_stop(run)
                raise ChatError(503 if self._closed else 502, OUTCOME_UNCERTAIN)
            try:
                state, value = self._observe(run, min(UPSTREAM_CALL_SECONDS, left))
            except ChatError:
                if stop_reason is None:
                    stop_reason, settle_by = "lost", time.monotonic() + RECONCILE_SECONDS
                self._pause(min(delay, max(0.0, self._limit(settle_by) - time.monotonic())))
                delay = min(delay * 2, 4 * self.poll_interval)
                continue
            if state in _SETTLED_STATES:
                return self._settled(run, state, value, stop_reason)
            if stop_reason or self._closed:
                self._pause(min(self.poll_interval, max(0.0, self._limit(settle_by if stop_reason else run.deadline)
                                                        - time.monotonic())))
            else:
                self._closing.wait(min(delay, max(0.0, run.deadline - time.monotonic())))
                delay = min(delay * 2, 4 * self.poll_interval)

    def _settled(self, run: _Run, state: str, value: dict[str, Any], stop_reason: Optional[str]) -> str:
        self._release(run)
        if state == "completed":
            reply = value.get("output")
            if not isinstance(reply, str) or not reply.strip():
                return "(Hermes finished without a text reply.)"
            return reply[:MAX_REPLY]
        if stop_reason == "closed":
            raise ChatError(503, PLUGIN_TURNED_OFF)
        if stop_reason == "lost" or state == "unknown":
            raise ChatError(502, OUTCOME_UNCERTAIN)
        if stop_reason in ("timeout", "recheck"):
            raise ChatError(504, "Hermes did not finish this reply in time and stopped it; check the document for "
                                 "changes it made before stopping")
        if state == "failed":
            raise ChatError(502, "Hermes could not finish this reply; see the Hermes logs")
        raise ChatError(502, "Hermes stopped before finishing this reply")

    def _reconcile(self, run: _Run, deadline: float, drive: bool = True) -> None:
        """Stop and watch a run until it settles or ``deadline``: one no chat thread waits for
        any more (close, or a new message held back by it), or, with ``drive`` False, one whose
        chat thread is blocked in a call when close() begins."""
        try:
            self._follow(run, stop_reason="closed" if self._closed else "recheck", settle_by=deadline, drive=drive)
        except ChatError:
            pass
        except Exception:  # noqa: BLE001 - a cleanup thread must not die with the run still marked as driven
            log.error("libreoffice chat: stop cleanup failed")
            if drive and run.driven:
                self._abandon(run)


LINGER_SECONDS = 0.5


def _linger_close(connection: Any, budget: int = 2 * MAX_BODY, seconds: float = LINGER_SECONDS) -> int:
    """After a refusal has been sent, stop sending, then read and discard what the client still
    sends until EOF, ``budget`` bytes or ONE overall deadline, whichever comes first.

    Closing a socket with unread request bytes makes Windows answer with a reset that destroys
    the reply (found on a Windows 11 host). The request body is never read before the reply, so
    a client that declares more than it sends cannot delay the refusal, and the deadline bounds
    every recv by the time left, so a trickling client holds the thread for ``seconds`` at most.
    Returns the bytes discarded.
    """
    total = 0
    deadline = time.monotonic() + seconds
    try:
        connection.shutdown(socket.SHUT_WR)
        while total < budget:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            connection.settimeout(left)
            chunk = connection.recv(65536)
            if not chunk:
                break
            total += len(chunk)
    except OSError:
        pass
    return total


_ROUTES = {("GET", "/health"): "health", ("POST", "/chat"): "chat"}


def _handler_for(bridge: ChatBridge) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesLibreOfficeChat/0.1"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            # ONE deadline for the whole request, headers and body: see _DeadlineReader.
            plain, self.rfile = self.rfile, io.BufferedReader(
                _DeadlineReader(self.connection, time.monotonic() + bridge.read_deadline))
            plain.close()

        def log_message(self, _format: str, *_args: Any) -> None:
            return None  # replaced by the metadata-only line in _finish

        def _route(self) -> str:
            return _ROUTES.get((self.command, self.path), "unknown")

        def _finish(self, status: int, payload: dict[str, Any], start: float) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.close_connection = True
            try:
                self.connection.settimeout(WRITE_TIMEOUT_SECONDS)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            except OSError:
                pass  # the panel went away
            if status >= 400:
                _linger_close(self.connection)
            log.info("libreoffice chat %s %s -> %d in %.1fs", "GET" if self.command == "GET" else "POST",
                     self._route(), status, time.monotonic() - start)

        def _authorize(self) -> None:
            if self.headers.get("Origin") is not None:
                raise ChatError(403, "requests with an Origin are refused")
            if self.headers.get("Host") != f"127.0.0.1:{bridge.port}":
                raise ChatError(403, "Host must be the loopback chat endpoint")
            expected = bridge.token()
            if not expected:
                raise ChatError(503, "Hermes has no LibreOffice pairing token yet: pair first (see the README)")
            supplied = self.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                raise ChatError(401, "the pairing token does not match: pair LibreOffice with Hermes again")

        def _run(self, work: Callable[[], dict[str, Any]]) -> None:
            start = time.monotonic()
            try:
                self._authorize()
                self._finish(200, {"ok": True, **work()}, start)
            except ChatError as exc:
                self._finish(exc.status, {"ok": False, "error": str(exc)}, start)
            except Exception as exc:  # noqa: BLE001 - never let exception text (it can hold a secret) out
                log.error("libreoffice chat: unhandled failure (%s)", type(exc).__name__)
                self._finish(500, {"ok": False, "error": "the chat endpoint failed"}, start)

        def do_GET(self) -> None:
            def health() -> dict[str, Any]:
                if self.path != "/health":
                    raise ChatError(404, "unknown route")
                return {"status": "ok"}
            self._run(health)

        def do_POST(self) -> None:
            def chat() -> dict[str, Any]:
                if self.path != "/chat":
                    raise ChatError(404, "unknown route")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ChatError(415, "Content-Type must be application/json")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    raise ChatError(411, "Content-Length is required") from None
                if not 0 < length <= MAX_BODY:
                    raise ChatError(413, "request body is empty or too large")
                try:
                    raw = self.rfile.read(length)
                except (socket.timeout, OSError):
                    raise ChatError(408, "the request body did not arrive in time") from None
                if len(raw) != length:
                    raise ChatError(400, "request body is truncated")
                return bridge.chat(parse_request(raw))
            self._run(chat)

    return Handler
