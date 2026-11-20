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
   matching windows; on quit the browser this LibreOffice started is asked to quit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
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
CALL_TIMEOUT_SECONDS = 5.0
QUIT_TIMEOUT_SECONDS = 2.0
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
UNSUPPORTED = "Sidecar windows are not supported on this system."  # 8.1 names it, no exact text
NO_DOCUMENT = "Sidecar opens for a Calc, Writer or Impress document."  # not in 8.2
COULD_NOT_START = "Sidecar could not start the browser. Click Open Sidecar to try again."  # not in 8.2
TOKEN_UNSAFE = "The pairing token file has unsafe permissions; restart LibreOffice."
TOKEN_MISSING = "The pairing token is missing; restart LibreOffice."
TOKEN_INVALID = "The pairing token is invalid; restart LibreOffice."


class LauncherError(Exception):
    """A failure whose message is the panel text to show."""


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


class LauncherClient:
    """One channel to the plugin's /launcher routes (contract sections 3 and 4).

    Open a new client for each user action. Any failure drops the channel; nothing is retried.
    """

    def __init__(self, port: int, read_token: Callable[[], str], *, timeout: float = CALL_TIMEOUT_SECONDS,
                 connect: Callable[..., Any] = http.client.HTTPConnection) -> None:
        self.port = port
        self._read_token = read_token
        self._timeout = timeout
        self._connect = connect
        self._key: bytes | None = None
        self._channel = ""
        self._counter = 0
        self.chat: dict[str, Any] | None = None

    def _exchange(self, method: str, path: str, body: bytes | None,
                  headers: dict[str, str]) -> tuple[int, bytes, list[str]]:
        connection = self._connect("127.0.0.1", self.port, timeout=self._timeout)
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
        if status in (401, 404, 503) and value.get("ok") is False and "code" not in value:
            # A 0.4.0 plugin checks its bearer token before the path and has no error codes.
            raise LauncherError(OLD_PLUGIN)
        server_nonce = value.get("server_nonce")
        proof = value.get("server_proof")
        if (status != 200 or value.get("ok") is not True or value.get("v") != 1
                or not isinstance(server_nonce, str) or not _NONCE.match(server_nonce)
                or not isinstance(proof, str)
                or not hmac.compare_digest(proof, hello_proof(token, self.port, client_nonce, server_nonce))):
            raise LauncherError(NOT_YOURS.format(port=self.port))
        self._key = channel_key(token, self.port, client_nonce, server_nonce)
        self._channel = server_nonce
        self._counter = 0
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
        if len(macs) != 1 or not hmac.compare_digest(macs[0], expected) or value is None:
            self._key = None  # a refused or forged answer ends the channel
            raise LauncherError(NOT_YOURS.format(port=self.port))
        return status, value

    def launch(self, document: dict[str, Any], host_scheme: str | None, mode: str,
               bounds: dict[str, int] | None) -> dict[str, Any]:
        status, value = self.call("POST", "/launcher/launch", {
            "document": document, "host_scheme": host_scheme, "mode": mode, "bounds": bounds})
        if status == 200 and value.get("ok") is True and value.get("window_live") is True:
            return {"window_live": True, "launch_id": None, "url": None}
        url, launch_id = value.get("url"), value.get("launch_id")
        if status == 201 and value.get("ok") is True and value.get("window_live") is False:
            pattern = rf"^http://127\.0\.0\.1:{self.port}/ui/index\.html#launch=[A-Za-z0-9_-]{{43}}$"
            if (isinstance(url, str) and re.match(pattern, url) and isinstance(launch_id, str)
                    and _LAUNCH_ID.match(launch_id)):
                return {"window_live": False, "launch_id": launch_id, "url": url}
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
    if not _URL_CHARS.match(url) or not url.startswith("http://127.0.0.1:"):
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
        if not _BOOTSTRAP_NAME.match(name):
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
    x = fx + fw if fx + fw + WINDOW_WIDTH <= ax + aw else ax + aw - WINDOW_WIDTH
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


class Launcher:
    """One per LibreOffice process. ``open`` is called on the main thread from the button;
    ``report(text, busy)`` is called from worker threads (the panel marshals it)."""

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
        self._inflight: set[str] = set()
        self._opened: set[str] = set()  # documents with a window this LibreOffice opened
        self._ever_opened = False
        self._browser_pid: int | None = None
        self._read_token: Callable[[], str] | None = None
        self._terminated = False

    @property
    def adapter(self) -> Any:
        if not self._adapter_loaded:
            self._adapter = platform_adapter(self._platform)
            self._adapter_loaded = True
        return self._adapter

    def configure(self, read_token: Callable[[], str]) -> None:
        """Where the pairing token is read from (the bridge folder of this profile)."""
        self._read_token = read_token

    def _new_client(self, timeout: float = CALL_TIMEOUT_SECONDS) -> LauncherClient:
        if self._read_token is None:
            raise LauncherError(TOKEN_MISSING)
        return self._client(self._port(), self._read_token, timeout=timeout)

    def probe(self, report: Callable[[str, bool], None]) -> None:
        """Panel opened: one hello on a worker, to show chat-off or pairing problems early.
        It starts no process and mints no launch code."""
        def run() -> None:
            try:
                self._new_client().hello()
                text = IDLE if self.adapter is not None else UNSUPPORTED
            except LauncherError as exc:
                text = str(exc)
            except Exception:
                text = NO_ANSWER.format(port=self._port())
            report(text, False)
        self._start(run, "SidecarLauncherProbe")

    def open(self, document: dict[str, Any] | None, host_scheme: str | None,
             frame: tuple[int, int, int, int] | None, work_area: tuple[int, int, int, int] | None,
             report: Callable[[str, bool], None]) -> bool:
        """The Open Sidecar button. Returns False when nothing was started (a launch for this
        document is already in progress, or the document is not supported)."""
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
            if key in self._inflight or self._terminated:
                return False
            self._inflight.add(key)
        report(OPENING, True)
        doc = {"document_id": key, "title": str(document.get("title", ""))[:1000], "type": document["type"]}

        def run() -> None:
            try:
                text = self._launch(doc, host_scheme, frame, work_area)
            except LauncherError as exc:
                text = str(exc)
            except Exception:
                text = COULD_NOT_START
            finally:
                with self._lock:
                    self._inflight.discard(key)
            report(text, False)
        self._start(run, "SidecarLauncher")
        return True

    def _launch(self, document: dict[str, Any], host_scheme: str | None,
                frame: tuple[int, int, int, int] | None, work_area: tuple[int, int, int, int] | None) -> str:
        adapter = self.adapter
        client = self._new_client()
        client.hello()
        browsers = list(adapter.find_browsers())
        mode = "app" if browsers else "tab"
        bounds = compute_bounds(frame, work_area) if mode == "app" else None
        answer = client.launch(document, host_scheme, mode, bounds)
        if answer["window_live"]:
            pid = self._live_browser()
            if mode == "app" and pid is not None and adapter.raise_pid(pid):
                return OPEN
            return ALREADY_OPEN
        cache = Path(adapter.cache_dir())
        written = self._clock()
        path = write_bootstrap(answer["url"], cache)
        keep = False
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
            with self._lock:
                self._opened.add(document["document_id"])
                self._ever_opened = True
            state = self._follow(client, answer["launch_id"], written)
            if state == "pending":
                # Still unexchanged after 20 s: report now, keep the file until 30 s.
                keep = True
                self._start(lambda: self._remove_later(path, written), "SidecarLaunchCleanup")
        finally:
            if not keep:
                remove_bootstrap(path)
        return {"exchanged": OPEN, "double_use": DOUBLE_USE}.get(state, DID_NOT_CONNECT)

    def _follow(self, client: LauncherClient, launch_id: str, written: float) -> str:
        """Poll the launch state every 0.5 s for up to 20 s (R11); the first state other than
        pending ends the wait (and the caller then deletes the bootstrap file)."""
        state = "pending"
        while self._clock() - written < POLL_LIMIT_SECONDS:
            self._sleep(POLL_INTERVAL_SECONDS)
            try:
                state = client.status(launch_id)
            except LauncherError:
                state = "expired"
            if state != "pending":
                return state
        return state

    def _remove_later(self, path: Path, written: float) -> None:
        remaining = BOOTSTRAP_LIFETIME_SECONDS - (self._clock() - written)
        if remaining > 0:
            self._sleep(remaining)
        remove_bootstrap(path)

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

    def on_document_closed(self, document_id: str) -> None:
        """A document closed: disconnect its window (contract 4.3). Only for documents this
        LibreOffice opened a window for; on a worker, never blocking the close."""
        with self._lock:
            if document_id not in self._opened or self._terminated:
                return
            self._opened.discard(document_id)

        def run() -> None:
            try:
                client = self._new_client()
                client.hello()
                client.release(document_id, "document_closed")
            except Exception:
                return
        self._start(run, "SidecarRelease")

    def on_terminate(self) -> None:
        """LibreOffice is quitting: release every window, ask the browser this instance started
        to quit, and delete leftover bootstrap files. Bounded and synchronous; never raises.

        LibreOffice unloads each document before it reports termination, so the per-document
        releases are already on their way (on worker threads that may not finish before the
        process exits); one synchronous ``host_quit`` covers them whenever this LibreOffice
        opened any Sidecar window."""
        with self._lock:
            if self._terminated:
                return
            self._terminated = True
            opened = self._ever_opened
            self._opened.clear()
        adapter = self.adapter
        if adapter is None:
            return
        if opened:
            try:
                client = self._new_client(timeout=QUIT_TIMEOUT_SECONDS)
                client.hello()
                client.release(None, "host_quit")
            except Exception:
                pass
        pid = self._live_browser()
        if pid is not None:
            try:
                adapter.terminate(pid)
            except Exception:
                pass
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
