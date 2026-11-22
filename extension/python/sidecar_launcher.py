# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""What the Open Sidecar button does: ask the Hermes plugin for a one-time launch, then open it.

Pure Python (no UNO), so every step is unit-tested. The sidebar panel calls :data:`LAUNCHER`
from LibreOffice's main thread; network and process work runs on worker threads.

1. **Channel** (docs/web-protocol.md section 3, R18). The launcher never sends the pairing token
   to the chat port. It proves knowledge of it with HMAC-SHA256: ``POST /launcher/hello`` checks
   the plugin's proof, and every later call and answer carries a MAC under a per-click key.
2. **Launch** (section 4). ``POST /launcher/launch`` answers a one-time URL, or ``window_live``
   when the document already has a Sidecar window.
3. **Bootstrap file** (section 5). The URL is written to a private ``launch-<hex>.html`` that
   only redirects; the browser is given that file, so the launch code is never on a command line.
   App mode starts an installed Chromium browser with its own empty profile; tab mode (no
   Chromium browser, owner decision D1, and Windows in 0.5.0) opens the file in the default
   browser.
4. **Outcome** (R11). The launcher polls the launch state for up to 20 s and reports it; there is
   no automatic second attempt.
5. **Release**. Closing a document or quitting LibreOffice tells the plugin, which disconnects the
   matching windows; on quit the browser this LibreOffice started is asked to quit. Closing or
   quitting also cancels a launch still in flight for that document: no browser starts after it,
   and a code the plugin already minted is released (revoked) and its bootstrap file deleted.

Every launcher request is bounded by ONE deadline for the whole call (connect, send, status line,
headers and body), so a program on the chat port that trickles bytes cannot stretch a call, and
quitting LibreOffice spends at most ``QUIT_BUDGET_SECONDS`` here.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from typing import Any, Callable

_COMPONENT_DIR = str(Path(__file__).resolve().parent)
if _COMPONENT_DIR not in sys.path:
    sys.path.insert(0, _COMPONENT_DIR)
import private_files  # noqa: E402

DEFAULT_CHAT_PORT = 8661
PROTOCOL = "sidecar-launcher/1"
CALL_TIMEOUT_SECONDS = 5.0      # whole-call deadline of one launcher request
QUIT_TIMEOUT_SECONDS = 2.0      # all launcher requests made at quit (hello + host_quit), together
QUIT_BUDGET_SECONDS = 4.0       # everything on_terminate does, waiting for the browser included
QUIT_RESERVE_SECONDS = 1.0      # of that budget, kept for asking the browser to quit
TERMINATE_AFTER_QUIT_SECONDS = 1.0  # a browser that started after quit began: how long to wait for it
MAX_ANSWER_BYTES = 64 * 1024
POLL_INTERVAL_SECONDS = 0.5
POLL_LIMIT_SECONDS = 20.0
BOOTSTRAP_LIFETIME_SECONDS = 30.0
SWEEP_AGE_SECONDS = 60.0
WINDOW_WIDTH = 400
# Expected title-bar height added above the client rectangle (contract 8.3; S2 measured 32 on
# macOS). The page corrects the remainder itself.
TITLE_BAR_GUESS = {"darwin": 32}
FOLDER_NAME = "Sidecar for LibreOffice"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHROMIUM_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
)
MACOS_ONLY_FLAGS = ("--use-mock-keychain",)
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LAUNCH_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
_BOOTSTRAP_NAME = re.compile(r"^launch-[0-9a-f]{32}\.html$")
_URL_CHARS = re.compile(r"^[A-Za-z0-9:/._#=-]+$")
BOOTSTRAP_TEMPLATE = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                      '<meta http-equiv="refresh" content="0;url={url}"><title>Opening Sidecar…</title>')

# Panel texts (docs/web-protocol.md section 8.2). Texts marked "not in 8.2" are requested as a
# contract addition in the lane report.
IDLE = "Open Sidecar to chat with Hermes about this document."  # not in 8.2
CHECKING = "Checking Hermes…"  # not in 8.2
CHAT_OFF = "Chat is off in Hermes.\nSee README ▸ Sidebar chat."
NO_PAIRING = "Hermes is not paired with LibreOffice yet. See README ▸ Pairing."
NOT_YOURS = "Something other than your Hermes answered on port {port}. Sidecar did not open."
OPENING = "Opening Sidecar…"
OPEN = "Sidecar is open."
DID_NOT_CONNECT = ("The Sidecar window did not connect. Your browser may be managed by your "
                   "organization; see README ▸ Which browser.")
DOUBLE_USE = "Another program used this launch. Click Open Sidecar again."
ALREADY_OPEN = "Sidecar is already open. Switch to it from the Dock or taskbar."
OLD_PLUGIN = ("Your Hermes plugin is older than this extension. Update atlas-sidecar-libreoffice "
              "in Hermes, then click Open Sidecar again.")  # not in 8.2
NO_ANSWER = "Hermes did not answer on port {port}. Click Open Sidecar to try again."  # not in 8.2
BUSY = "Sidecar is busy, try again in a moment."  # not in 8.2
# The plugin's own sentences for its coded hello refusals (contract 8.2, revision 3). The answer
# to a hello is unsigned, so the panel shows these fixed texts, never the answer's own words.
TURNED_OFF = "The LibreOffice plugin was turned off in Hermes."
ENDPOINT_FAILED = "The chat endpoint failed. Click Open Sidecar to try again."
HELLO_REFUSALS = {(503, "turned_off"): TURNED_OFF, (500, "internal"): ENDPOINT_FAILED}
UNSUPPORTED = "Sidecar windows are not supported on this system."  # 8.1 names it, no exact text
NO_DOCUMENT = "Sidecar opens for a Calc, Writer or Impress document."  # not in 8.2
COULD_NOT_START = "Sidecar could not start the browser. Click Open Sidecar to try again."  # not in 8.2
CACHE_UNSAFE = "The Sidecar cache folder is unsafe. See README ▸ Which browser."
TOKEN_UNSAFE = "The pairing token file has unsafe permissions; restart LibreOffice."
TOKEN_MISSING = "The pairing token is missing; restart LibreOffice."
TOKEN_INVALID = "The pairing token is invalid; restart LibreOffice."


class LauncherError(Exception):
    """A failure whose message is the panel text to show."""


class LauncherBusy(LauncherError):
    """The plugin refused the call because all its request slots were in use (nothing happened)."""

    def __init__(self) -> None:
        super().__init__(BUSY)


# The plugin's busy refusal, sent before any handler runs: code ``endpoint_busy`` (``busy`` is
# accepted too); the first 0.5.0 endpoint answered it without a code but with this sentence.
BUSY_CODES = ("endpoint_busy", "busy")
BUSY_SENTENCE = "the chat endpoint is busy; try again shortly"


def _busy(status: int, value: dict[str, Any], current: bool) -> bool:
    """Is this unsigned answer the plugin's busy refusal? A 503 without any ``code`` counts as
    busy only from a port that already proved to be a 0.5.0 plugin; from an unknown one it stays
    the older-plugin answer. It only chooses a panel text, never authorises anything."""
    if status != 503 or value.get("ok") is not False:
        return False
    if value.get("code") in BUSY_CODES or value.get("error") == BUSY_SENTENCE:
        return True
    return current and "code" not in value


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hmac(key: bytes, *parts: str) -> bytes:
    return hmac.new(key, "\n".join(parts).encode("utf-8"), hashlib.sha256).digest()


def hello_proof(token: str, port: int, client_nonce: str, server_nonce: str) -> str:
    return b64u(_hmac(token.encode("utf-8"), PROTOCOL + " hello", str(port), client_nonce, server_nonce))


def channel_key(token: str, port: int, client_nonce: str, server_nonce: str) -> bytes:
    return _hmac(token.encode("utf-8"), PROTOCOL + " channel", str(port), client_nonce, server_nonce)


def request_mac(key: bytes, method: str, path: str, counter: int, body: bytes) -> str:
    return b64u(_hmac(key, PROTOCOL + " request", method.upper(), path, str(counter),
                      hashlib.sha256(body).hexdigest()))


def response_mac(key: bytes, counter: int, status: int, body: bytes) -> str:
    return b64u(_hmac(key, PROTOCOL + " response", str(counter), str(status),
                      hashlib.sha256(body).hexdigest()))


def encode_body(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def chat_port() -> int:
    value = os.environ.get("HERMES_LO_CHAT_PORT", "")
    return int(value) if value.isdigit() and 1024 <= int(value) <= 65535 else DEFAULT_CHAT_PORT


def read_pairing_token(path: Path) -> str:
    """The pairing token the bridge wrote in this profile; refuses a file others can read."""
    try:
        token = private_files.read_private_bytes(path, 4096).decode("utf-8").strip()
    except private_files.UnsafePath:
        raise LauncherError(TOKEN_UNSAFE) from None
    except (OSError, UnicodeDecodeError):
        raise LauncherError(TOKEN_MISSING) from None
    if len(token) < 32:
        raise LauncherError(TOKEN_INVALID)
    return token


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("the launcher call ran out of time")
    return left


class _DeadlineReader(io.RawIOBase):
    """Socket reads bounded by ONE absolute deadline: every recv gets only the time that is left,
    so an answer trickled byte by byte cannot outlast the call the way a per-recv timeout allows."""

    def __init__(self, sock: Any, deadline: float, holder: Any) -> None:
        super().__init__()
        # ``holder`` is the stdlib's own socket file: it keeps the descriptor open after the
        # connection object closes its socket (as http.client does for "Connection: close").
        self._sock, self._deadline, self._holder = sock, deadline, holder

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self._sock.settimeout(_left(self._deadline))
        return self._sock.recv_into(buffer)

    def close(self) -> None:
        try:
            self._holder.close()
        finally:
            super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    def __init__(self, sock: Any, debuglevel: int = 0, method: str | None = None, url: str | None = None, *,
                 deadline: float) -> None:
        super().__init__(sock, debuglevel, method, url)
        self.fp = io.BufferedReader(_DeadlineReader(sock, deadline, self.fp))


class DeadlineConnection(http.client.HTTPConnection):
    """An HTTP connection whose connect, sends and reads share one absolute deadline
    (``time.monotonic()`` seconds); past it every step raises ``socket.timeout``."""

    def __init__(self, host: str, port: int, *, deadline: float) -> None:
        super().__init__(host, port, timeout=_left(deadline))
        self._deadline = deadline
        self.response_class = functools.partial(_DeadlineResponse, deadline=deadline)

    def connect(self) -> None:
        self.timeout = _left(self._deadline)
        super().connect()

    def send(self, data: Any) -> None:
        if self.sock is not None:
            self.sock.settimeout(_left(self._deadline))
        super().send(data)


class LauncherClient:
    """One channel to the plugin's /launcher routes (contract sections 3 and 4).

    Open a new client for each user action. Any failure drops the channel; nothing is retried.
    Each call is bounded by ``timeout`` seconds in total, and by ``deadline`` (an absolute
    ``time.monotonic()`` value shared by all calls on this client) when one is set. ``current``
    says this port already proved to be a 0.5.0 plugin (it chooses the busy text, see _busy).
    """

    def __init__(self, port: int, read_token: Callable[[], str], *, timeout: float = CALL_TIMEOUT_SECONDS,
                 deadline: float | None = None, current: bool = False,
                 connect: Callable[..., Any] = DeadlineConnection) -> None:
        self.port = port
        self._read_token = read_token
        self._timeout = timeout
        self.deadline = deadline
        self.current = current
        self._connect = connect
        self._key: bytes | None = None
        self._channel = ""
        self._counter = 0
        self.chat: dict[str, Any] | None = None
        self.caps: tuple[str, ...] = ()

    def _exchange(self, method: str, path: str, body: bytes | None,
                  headers: dict[str, str]) -> tuple[int, bytes, list[str]]:
        deadline = time.monotonic() + self._timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        try:
            connection = self._connect("127.0.0.1", self.port, deadline=deadline)
        except OSError:
            raise LauncherError(NO_ANSWER.format(port=self.port)) from None
        try:
            sent = {"Host": f"127.0.0.1:{self.port}", "Connection": "close", "Accept-Encoding": "identity"}
            if body is not None:
                sent["Content-Type"] = "application/json"
                sent["Content-Length"] = str(len(body))
            sent.update(headers)
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            raw = response.read(MAX_ANSWER_BYTES + 1)
            macs = response.msg.get_all("X-Sidecar-MAC") or []
            status = response.status
        except ConnectionRefusedError:
            raise LauncherError(CHAT_OFF) from None
        except (OSError, http.client.HTTPException):
            raise LauncherError(NO_ANSWER.format(port=self.port)) from None
        finally:
            connection.close()
        if len(raw) > MAX_ANSWER_BYTES:
            raise LauncherError(NOT_YOURS.format(port=self.port))
        return status, raw, macs

    def _json(self, raw: bytes) -> dict[str, Any] | None:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def hello(self) -> dict[str, Any]:
        """Open the channel; returns the plugin's chat readiness. Reads the token first, so a
        missing or unsafe token file is reported before anything is sent."""
        token = self._read_token()
        client_nonce = b64u(secrets.token_bytes(32))
        status, raw, _ = self._exchange("POST", "/launcher/hello",
                                        encode_body({"v": 1, "client_nonce": client_nonce}), {})
        value = self._json(raw) or {}
        if status == 503 and value.get("code") == "no_pairing":
            raise LauncherError(NO_PAIRING)
        if _busy(status, value, self.current):
            raise LauncherBusy()
        refusal = HELLO_REFUSALS.get((status, value.get("code"))) if value.get("ok") is False else None
        if refusal is not None:
            raise LauncherError(refusal)
        if status in (401, 404, 503) and value.get("ok") is False and "code" not in value:
            # A 0.4.0 plugin checks its bearer token before the path and has no error codes.
            raise LauncherError(OLD_PLUGIN)
        server_nonce = value.get("server_nonce")
        proof = value.get("server_proof")
        if (status != 200 or value.get("ok") is not True or value.get("v") != 1
                or not isinstance(server_nonce, str) or not _NONCE.fullmatch(server_nonce)
                or not isinstance(proof, str)
                or not hmac.compare_digest(proof, hello_proof(token, self.port, client_nonce, server_nonce))):
            raise LauncherError(NOT_YOURS.format(port=self.port))
        self._key = channel_key(token, self.port, client_nonce, server_nonce)
        self._channel = server_nonce
        self._counter = 0
        self.current = True
        caps = value.get("caps")
        self.caps = tuple(caps) if isinstance(caps, list) and all(isinstance(c, str) for c in caps) else ()
        chat = value.get("chat")
        self.chat = chat if isinstance(chat, dict) else None
        return self.chat or {}

    def call(self, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, Any]]:
        """One MAC'd call; returns (status, answer) only for an answer whose MAC is right."""
        if self._key is None:
            raise LauncherError(NOT_YOURS.format(port=self.port))
        self._counter += 1
        counter = self._counter
        body = encode_body(payload) if payload is not None else None
        status, raw, macs = self._exchange(method, path, body, {
            "X-Sidecar-Channel": self._channel,
            "X-Sidecar-Counter": str(counter),
            "X-Sidecar-MAC": request_mac(self._key, method, path, counter, body or b""),
        })
        expected = response_mac(self._key, counter, status, raw)
        value = self._json(raw)
        if not macs and _busy(status, value or {}, True):
            # Refused before any handler read the request, so the plugin did not count it.
            self._counter -= 1
            raise LauncherBusy()
        if len(macs) != 1 or not hmac.compare_digest(macs[0], expected) or value is None:
            self._key = None  # a refused or forged answer ends the channel
            if (not macs and counter == 1 and status == 401 and value is not None
                    and value.get("code") == "launcher_auth"):
                # The hello already proved this plugin; an unsigned refusal of the first call means
                # its channel was displaced (or expired) in between. Nothing ran: try again.
                raise LauncherError(NO_ANSWER.format(port=self.port))
            raise LauncherError(NOT_YOURS.format(port=self.port))
        return status, value

    def launch(self, document: dict[str, Any], host_scheme: str | None, mode: str,
               bounds: dict[str, int] | None, *, follow: bool = False) -> dict[str, Any]:
        payload = {"document": document, "host_scheme": host_scheme, "mode": mode, "bounds": bounds}
        if follow and mode == "app" and "window_follow" in self.caps:
            payload["follow"] = True
        status, value = self.call("POST", "/launcher/launch", payload)
        if status == 200 and value.get("ok") is True and value.get("window_live") is True:
            return {"window_live": True, "launch_id": None, "url": None}
        url, launch_id = value.get("url"), value.get("launch_id")
        if status == 201 and value.get("ok") is True and value.get("window_live") is False:
            pattern = rf"^http://127\.0\.0\.1:{self.port}/ui/index\.html#launch=[A-Za-z0-9_-]{{43}}$"
            if (isinstance(url, str) and re.fullmatch(pattern, url) and isinstance(launch_id, str)
                    and _LAUNCH_ID.fullmatch(launch_id)):
                return {"window_live": False, "launch_id": launch_id, "url": url,
                        "follow": payload.get("follow") is True and value.get("follow") is True}
            raise LauncherError(NOT_YOURS.format(port=self.port))
        raise LauncherError(_refusal(value, status))

    def status(self, launch_id: str) -> str:
        status, value = self.call("GET", f"/launcher/launch/{launch_id}")
        state = value.get("state")
        if status == 200 and state in ("pending", "exchanged", "expired", "double_use"):
            return state
        return "expired"

    def release(self, document_id: str | None, reason: str) -> int:
        status, value = self.call("POST", "/launcher/release",
                                  {"document_id": document_id, "reason": reason})
        released = value.get("released")
        return released if status == 200 and isinstance(released, int) else 0


def _refusal(value: dict[str, Any], status: int) -> str:
    """A refusal the plugin authenticated: show its own sentence, bounded."""
    error = value.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()[:300]
    return f"Hermes refused to open Sidecar (HTTP {status})."


# ---------------------------------------------------------------- files, bounds, command line


def sidecar_cache(cache: Path) -> Path:
    return Path(cache) / FOLDER_NAME


def launch_dir(cache: Path) -> Path:
    return sidecar_cache(cache) / "launch"


def profile_dir(cache: Path) -> Path:
    return sidecar_cache(cache) / "browser"


def ensure_private_dirs(cache: Path, *names: str) -> None:
    """Create ``<cache>/Sidecar for LibreOffice`` and the named children owner-only, and check
    each with the private_files rules (refused, never repaired)."""
    private_files.ensure_private_dir(sidecar_cache(cache))
    for name in names:
        private_files.ensure_private_dir(sidecar_cache(cache) / name)


def bootstrap_bytes(url: str) -> bytes:
    if not _URL_CHARS.fullmatch(url) or not url.startswith("http://127.0.0.1:"):
        raise ValueError("unexpected launch URL")
    return BOOTSTRAP_TEMPLATE.format(url=url).encode("utf-8")


def write_bootstrap(url: str, cache: Path) -> Path:
    """Write the redirect page (0600, created exclusively) in the private launch folder."""
    data = bootstrap_bytes(url)
    ensure_private_dirs(cache, "launch")
    path = launch_dir(cache) / f"launch-{secrets.token_hex(16)}.html"
    private_files.create_private_file(path, data)
    return path


def remove_bootstrap(path: Path | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def sweep(cache: Path, *, older_than: float = SWEEP_AGE_SECONDS, now: float | None = None) -> int:
    """Delete leftover ``launch-*.html`` files older than ``older_than`` seconds."""
    folder = launch_dir(cache)
    now = time.time() if now is None else now
    removed = 0
    try:
        private_files.check_private(folder, directory=True)
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if not _BOOTSTRAP_NAME.fullmatch(name):
            continue
        path = folder / name
        try:
            info = os.lstat(path)
            if now - info.st_mtime >= older_than:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def compute_bounds(frame: tuple[int, int, int, int] | None,
                   work_area: tuple[int, int, int, int] | None) -> dict[str, int]:
    """The client rectangle for the Sidecar window (contract 8.3), in screen points.

    Right of LibreOffice's frame when 400 points fit inside the work area, otherwise against the
    work area's right edge, overlapping the frame. The page clamps to the real screen.
    """
    if work_area is None or work_area[2] <= 0:
        work_area = (0, 0, 1440, 900)
    ax, ay, aw, ah = (int(v) for v in work_area)
    if frame is None or frame[2] <= 0 or frame[3] <= 0:
        frame = (ax, ay, max(aw - WINDOW_WIDTH, 0), ah)
    fx, fy, fw, fh = (int(v) for v in frame)
    centre_inside = ax <= fx + fw / 2 < ax + aw and ay <= fy + fh / 2 < ay + ah
    x = fx + fw if fx + fw + WINDOW_WIDTH <= ax + aw or not centre_inside else ax + aw - WINDOW_WIDTH
    return {"x": _clamp(x, -32768, 32767), "y": _clamp(fy, -32768, 32767),
            "width": WINDOW_WIDTH, "height": _clamp(fh, 240, 8192)}


def window_geometry(bounds: dict[str, int], platform: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """``--window-position`` and ``--window-size`` for the client rectangle (contract 8.3)."""
    guess = TITLE_BAR_GUESS.get(platform, 0)
    return ((bounds["x"], bounds["y"] - guess), (bounds["width"], bounds["height"] + guess))


def browser_argv(executable: str, profile: Path, target_uri: str, position: tuple[int, int],
                 size: tuple[int, int], platform: str) -> list[str]:
    """The frozen Chromium app-mode argument list (contract 5.3). ``target_uri`` must be the
    bootstrap file's URI: the launch URL itself never goes on a command line."""
    if not target_uri.startswith("file:///") or "#" in target_uri or "launch=" in target_uri:
        raise ValueError("the browser is only ever given the bootstrap file")
    argv = [executable, f"--user-data-dir={profile}", f"--app={target_uri}",
            f"--window-position={int(position[0])},{int(position[1])}",
            f"--window-size={int(size[0])},{int(size[1])}", *CHROMIUM_FLAGS]
    if platform == "darwin":
        argv.extend(MACOS_ONLY_FLAGS)
    return argv


def platform_adapter(platform: str | None = None) -> Any:
    """The per-OS adapter module, or ``None`` where Sidecar windows are not supported."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        import launcher_macos
        return launcher_macos
    if platform == "win32":
        import launcher_windows
        return launcher_windows
    return None


# ---------------------------------------------------------------- the state machine


class _Cancelled(Exception):
    """The document closed or LibreOffice is quitting: this launch stops where it is."""


class _Launch:
    """One Open Sidecar click for one document, from the click until its worker ends."""

    __slots__ = ("document_id", "cancelled", "spawning", "client", "follow", "generation", "closed_at")

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        self.cancelled = False   # set under the launcher lock by on_document_closed / on_terminate
        self.spawning = False    # the browser (or the default handler) is being started right now
        self.client: LauncherClient | None = None
        self.follow: Any = None
        self.generation = 0
        self.closed_at: float | None = None


class Launcher:
    """One per LibreOffice process. ``open`` is called on the main thread from the button;
    ``report(text, busy)`` is called from worker threads (the panel marshals it).

    Closing a document or quitting cancels that document's launch in flight (see ``_launch``):
    the worker checks after every blocking step, never starts a browser once cancelled, releases a
    code the plugin already minted, deletes its bootstrap file, and reports nothing."""

    def __init__(self, *, adapter: Any = None, platform: str | None = None,
                 port: Callable[[], int] = chat_port, client: Callable[..., LauncherClient] = LauncherClient,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 start: Callable[[Callable[[], None], str], None] | None = None) -> None:
        self._platform = sys.platform if platform is None else platform
        self._adapter = adapter
        self._adapter_loaded = adapter is not None
        self._port = port
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._start = start or _start_thread
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._launches: dict[str, _Launch] = {}  # in flight, by document
        self._opened: set[str] = set()  # documents with a window this LibreOffice opened
        self._ever_opened = False
        self._current: set[int] = set()  # ports whose hello proved a 0.5.0 plugin
        self._bootstraps: set[Path] = set()  # bootstrap files written and not yet deleted
        self._browser_pid: int | None = None
        self._read_token: Callable[[], str] | None = None
        self._terminated = False
        self._follower: Any = None
        self._follow_handles: dict[str, Any] = {}
        self._follow_listeners: dict[str, Callable[[], None]] = {}
        self._generations: dict[str, int] = {}
        # Worker-only I/O gates: never acquired from a UNO callback. A reopened document
        # mints only after any older release in flight has completed.
        self._release_gates: dict[str, Any] = {}

    @property
    def adapter(self) -> Any:
        if not self._adapter_loaded:
            self._adapter = platform_adapter(self._platform)
            self._adapter_loaded = True
        return self._adapter

    def configure(self, read_token: Callable[[], str]) -> None:
        """Where the pairing token is read from (the bridge folder of this profile)."""
        self._read_token = read_token

    def register_follow_listener(self, document_id: str, close: Callable[[], None]) -> None:
        """UNO main thread only, like document-close and termination hooks."""
        self._follow_listeners[document_id] = close

    def follow_handle(self, document_id: str) -> Any:
        """Main-thread click only. Windows and the support kill switch attach nothing."""
        if self._platform != "darwin" or os.environ.get("HERMES_LO_SIDECAR_NO_FOLLOW") == "1" or self._terminated:
            return None
        if self._follower is None:
            import window_follow
            self._follower = window_follow.Follower()
        handle = self._follow_handles.get(document_id)
        if handle is None or not handle.alive:
            handle = self._follow_handles[document_id] = self._follower.attach(document_id)
        return handle

    def _new_client(self, timeout: float = CALL_TIMEOUT_SECONDS, deadline: float | None = None) -> LauncherClient:
        if self._read_token is None:
            raise LauncherError(TOKEN_MISSING)
        port = self._port()
        with self._lock:
            current = port in self._current
        return self._client(port, self._read_token, timeout=timeout, deadline=deadline, current=current)

    def _hello(self, client: LauncherClient) -> dict[str, Any]:
        chat = client.hello()
        with self._lock:
            self._current.add(client.port)
        return chat

    def probe(self, report: Callable[[str, bool], None]) -> None:
        """Panel opened: one hello on a worker, to show chat-off or pairing problems early.
        It starts no process and mints no launch code."""
        def run() -> None:
            try:
                self._hello(self._new_client())
                text = IDLE if self.adapter is not None else UNSUPPORTED
            except LauncherError as exc:
                text = str(exc)
            except Exception:
                text = NO_ANSWER.format(port=self._port())
            report(text, False)
        self._start(run, "SidecarLauncherProbe")

    def open(self, document: dict[str, Any] | None, host_scheme: str | None,
             frame: tuple[int, int, int, int] | None, work_area: tuple[int, int, int, int] | None,
             report: Callable[[str, bool], None], *, follow: Any = None) -> bool:
        """The Open Sidecar button. Returns False when nothing was started (a launch for this
        document is already in progress, LibreOffice is quitting, or the document is not
        supported)."""
        if not isinstance(document, dict) or document.get("type") not in ("calc", "writer", "impress"):
            report(NO_DOCUMENT, False)
            return False
        if self.adapter is None:
            report(UNSUPPORTED, False)
            return False
        if host_scheme not in ("light", "dark"):
            host_scheme = None
        key = document["document_id"]
        with self._lock:
            running = self._launches.get(key)
            if self._terminated or (running is not None and not running.cancelled):
                return False
            launch = self._launches[key] = _Launch(key)
            launch.generation = self._generations[key] = self._generations.get(key, 0) + 1
            self._release_gates.setdefault(key, threading.Lock())
            if self._platform == "darwin" and os.environ.get("HERMES_LO_SIDECAR_NO_FOLLOW") != "1":
                launch.follow = follow
        report(OPENING, True)
        doc = {"document_id": key, "title": str(document.get("title", ""))[:1000], "type": document["type"]}

        def run() -> None:
            text: str | None = None
            try:
                text = self._launch(launch, doc, host_scheme, frame, work_area)
            except _Cancelled:
                text = None
            except private_files.UnsafePath:
                text = CACHE_UNSAFE
            except LauncherError as exc:
                text = str(exc)
            except Exception:
                text = COULD_NOT_START
            finally:
                with self._lock:
                    if self._launches.get(key) is launch:
                        del self._launches[key]
                    cancelled = launch.cancelled
                    self._settled.notify_all()
            if text is not None and not cancelled:  # the panel of a closed document is gone
                report(text, False)
        self._start(run, "SidecarLauncher")
        return True

    def _check(self, launch: _Launch) -> None:
        if launch.cancelled:
            raise _Cancelled()

    def _abandon(self, client: LauncherClient, launch: _Launch, path: Path | None) -> None:
        """A cancelled launch whose code was minted: delete the bootstrap file, then release, so
        the plugin revokes the code and ends any session it already started (contract 4.3)."""
        self._remove(path)
        with self._lock:
            terminated = self._terminated
        try:
            if terminated:
                client.release(None, "host_quit")
            else:
                self._release_closed(launch.document_id, launch.generation,
                                     launch.closed_at if launch.closed_at is not None else self._clock())
        except Exception:
            pass
        raise _Cancelled()

    def _launch(self, launch: _Launch, document: dict[str, Any], host_scheme: str | None,
                frame: tuple[int, int, int, int] | None, work_area: tuple[int, int, int, int] | None) -> str:
        adapter = self.adapter
        with self._release_gates[launch.document_id]:
            client = launch.client = self._new_client()
            self._hello(client)
            self._check(launch)  # closed during hello: nothing was minted
            browsers = list(adapter.find_browsers())
            mode = "app" if browsers else "tab"
            bounds = compute_bounds(frame, work_area) if mode == "app" else None
            eligible = (launch.follow is not None and launch.follow.alive and mode == "app"
                        and "window_follow" in getattr(client, "caps", ())
                        and os.environ.get("HERMES_LO_SIDECAR_NO_FOLLOW") != "1")
            answer = (client.launch(document, host_scheme, mode, bounds, follow=True) if eligible else
                      client.launch(document, host_scheme, mode, bounds))
        if answer["window_live"]:
            self._check(launch)  # nothing minted; the close hook releases a window it opened
            pid = self._live_browser()
            if mode == "app" and pid is not None and adapter.raise_pid(pid):
                return OPEN
            return ALREADY_OPEN
        if launch.cancelled:  # closed while the plugin minted the code
            self._abandon(client, launch, None)
        cache = Path(adapter.cache_dir())
        written = self._clock()
        path = write_bootstrap(answer["url"], cache)
        with self._lock:
            self._bootstraps.add(path)
        # Deleted 30 s after it was written whatever happens to this worker (B4: not only after
        # the outcome poll), and earlier at the first state other than pending.
        self._start(lambda: self._remove_later(path, written), "SidecarLaunchCleanup")
        keep = False
        try:
            with self._lock:
                cancelled = launch.cancelled
                launch.spawning = not cancelled
            if cancelled:  # never start a browser for a closed document or while quitting
                self._abandon(client, launch, path)
            pid: int | None = None
            started = False
            try:
                if bounds is not None:
                    ensure_private_dirs(cache, "browser")
                    position, size = window_geometry(bounds, self._platform)
                    argv = browser_argv(browsers[0][1], profile_dir(cache), path.as_uri(), position, size,
                                        self._platform)
                    try:
                        pid = adapter.spawn(argv)
                    except OSError:
                        raise LauncherError(COULD_NOT_START) from None
                    self._note_spawn(pid)
                else:
                    try:
                        adapter.open_default(path)
                    except OSError:
                        raise LauncherError(COULD_NOT_START) from None
                started = True
            finally:
                with self._lock:
                    launch.spawning = False
                    cancelled = launch.cancelled
                    terminated = self._terminated
                    alone = not self._opened  # no other document has a window this LibreOffice opened
                    if started and not cancelled:
                        self._opened.add(document["document_id"])
                        self._ever_opened = True
                    self._settled.notify_all()
            if cancelled:  # closed while the browser was starting: bounded cleanup
                if pid is not None and (terminated or alone):  # the browser holds only this window
                    self._terminate_quietly(pid, TERMINATE_AFTER_QUIT_SECONDS)
                self._abandon(client, launch, path)
            state = self._follow(launch, client, answer["launch_id"], written)
            if launch.cancelled:
                self._abandon(client, launch, path)
            if state == "exchanged" and eligible and answer.get("follow") is True:
                # C2 bind rechecks alive; a concurrent close must not resurrect the binding.
                self._check(launch)
                launch.follow.bind(answer["launch_id"], client.port)
            if state == "pending":
                keep = True  # still unexchanged after 20 s: report now, the cleanup removes it at 30 s
        finally:
            if not keep:
                self._remove(path)
        return {"exchanged": OPEN, "double_use": DOUBLE_USE}.get(state, DID_NOT_CONNECT)

    def _follow(self, launch: _Launch, client: LauncherClient, launch_id: str, written: float) -> str:
        """Poll the launch state every 0.5 s for up to 20 s (R11); the first state other than
        pending ends the wait (and the caller then deletes the bootstrap file). A status call
        never runs past the 20 s either, and a cancelled launch stops polling."""
        limit = time.monotonic() + max(0.0, POLL_LIMIT_SECONDS - (self._clock() - written))
        client.deadline = limit if client.deadline is None else min(client.deadline, limit)
        state = "pending"
        while self._clock() - written < POLL_LIMIT_SECONDS and not launch.cancelled:
            self._sleep(POLL_INTERVAL_SECONDS)
            if launch.cancelled:
                break
            try:
                state = client.status(launch_id)
            except LauncherBusy:
                state = "pending"
            except LauncherError:
                state = "expired"
            if state != "pending":
                return state
        return state

    def _remove(self, path: Path | None) -> None:
        remove_bootstrap(path)
        with self._lock:
            self._bootstraps.discard(path)  # type: ignore[arg-type]

    def _remove_later(self, path: Path, written: float) -> None:
        remaining = BOOTSTRAP_LIFETIME_SECONDS - (self._clock() - written)
        if remaining > 0:
            self._sleep(remaining)
        self._remove(path)

    def _note_spawn(self, pid: int) -> None:
        """A second Chromium started on the same profile hands its window to the running
        instance and exits at once (status 0), so the first live PID stays the browser."""
        with self._lock:
            if self._browser_pid is None or not self.adapter.running(self._browser_pid):
                self._browser_pid = pid

    def _live_browser(self) -> int | None:
        with self._lock:
            pid = self._browser_pid
        return pid if pid is not None and self.adapter.running(pid) else None

    def _terminate_quietly(self, pid: int, wait: float) -> None:
        try:
            self.adapter.terminate(pid, wait=max(0.0, wait))
        except Exception:
            pass

    def on_document_closed(self, document_id: str) -> None:
        """A document closed: cancel its launch in flight, and disconnect the window this
        LibreOffice opened for it (contract 4.3), on a worker, never blocking the close."""
        detach = self._follow_listeners.pop(document_id, None)
        if detach is not None:
            detach()
        handle = self._follow_handles.pop(document_id, None)
        if handle is not None:
            handle.close()
        with self._lock:
            if self._terminated:
                return
            launch = self._launches.get(document_id)
            if launch is not None:
                launch.cancelled = True
                launch.closed_at = self._clock()
            if document_id not in self._opened:
                return
            self._opened.discard(document_id)
            generation = self._generations.get(document_id, 0)
            closed_at = self._clock()

        self._start(lambda: self._release_closed(document_id, generation, closed_at), "SidecarRelease")

    def _release_closed(self, document_id: str, generation: int, closed_at: float) -> None:
        """E03: initial close plus three new-channel attempts, within four seconds.

        A signed release answer (including refusal) is final. Only no-answer/unsigned busy
        permits retry. The worker-only gate orders an in-flight release before a reopened
        document's mint; generations cancel every stale retry, including abandoned launches.
        """
        with self._lock:
            gate = self._release_gates.setdefault(document_id, threading.Lock())
        for offset in (0.0, 0.5, 1.5, 3.5):
            remaining = closed_at + offset - self._clock()
            if remaining > 0:
                self._sleep(remaining)
            with gate:
                with self._lock:
                    if self._terminated or self._generations.get(document_id, 0) != generation:
                        return
                left = closed_at + 4.0 - self._clock()
                if left <= 0:
                    return
                deadline = time.monotonic() + min(0.5, left)
                try:
                    client = self._new_client(timeout=0.5, deadline=deadline)
                    self._hello(client)
                    with self._lock:
                        if self._terminated or self._generations.get(document_id, 0) != generation:
                            return
                    client.release(document_id, "document_closed")
                    return  # every authenticated answer is final, whatever its status
                except LauncherBusy:
                    continue
                except LauncherError as exc:
                    if str(exc) not in (CHAT_OFF, NO_ANSWER.format(port=self._port())):
                        return
                except Exception:
                    return

    def on_terminate(self) -> None:
        """LibreOffice is quitting: cancel launches in flight, release every window, ask the
        browser this instance started to quit, and delete bootstrap files. Synchronous, never
        raises, and bounded as a whole by QUIT_BUDGET_SECONDS: the launcher requests share one
        QUIT_TIMEOUT_SECONDS deadline, whatever the program on the chat port does.

        LibreOffice unloads each document before it reports termination, so the per-document
        releases are already on their way (on worker threads that may not finish before the
        process exits); one synchronous ``host_quit`` covers them whenever this LibreOffice
        opened a Sidecar window or has a launch in flight."""
        started = time.monotonic()
        budget_end = started + QUIT_BUDGET_SECONDS
        http_end = started + QUIT_TIMEOUT_SECONDS
        with self._lock:
            if self._terminated:
                return
            self._terminated = True
            for launch in self._launches.values():
                launch.cancelled = True
                if launch.client is not None:  # its next request ends with quit's own deadline
                    launch.client.deadline = http_end
            release = self._ever_opened or bool(self._launches)
            self._opened.clear()
        for detach in list(self._follow_listeners.values()):
            detach()
        self._follow_listeners.clear()
        if self._follower is not None:
            self._follower.stop()
        for handle in list(self._follow_handles.values()):
            handle.close()
        self._follow_handles.clear()
        adapter = self.adapter
        if adapter is None:
            return
        if release:
            try:
                client = self._new_client(timeout=QUIT_TIMEOUT_SECONDS, deadline=http_end)
                client.hello()
                client.release(None, "host_quit")
            except Exception:
                pass
        with self._lock:  # a browser being started right now is quit too, once its PID is known
            self._settled.wait_for(lambda: not any(launch.spawning for launch in self._launches.values()),
                                   timeout=max(0.0, budget_end - QUIT_RESERVE_SECONDS - time.monotonic()))
        pid = self._live_browser()
        if pid is not None:
            self._terminate_quietly(pid, budget_end - time.monotonic())
        with self._lock:
            leftovers = list(self._bootstraps)
        for path in leftovers:
            self._remove(path)
        try:
            sweep(Path(adapter.cache_dir()))
        except Exception:
            pass

    def on_start(self) -> None:
        """LibreOffice started: delete leftover bootstrap files. Never raises."""
        try:
            adapter = self.adapter
            if adapter is not None:
                sweep(Path(adapter.cache_dir()))
        except Exception:
            pass


def _start_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


LAUNCHER = Launcher()


def on_document_closed(document_id: str) -> None:
    LAUNCHER.on_document_closed(document_id)


def on_terminate() -> None:
    LAUNCHER.on_terminate()


def on_start() -> None:
    LAUNCHER.on_start()
