# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Minimal Chrome DevTools Protocol client over ``--remote-debugging-pipe`` (no port, no
websocket, stdlib only). Starts the installed Google Chrome headless with a throwaway profile."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))


def find_chrome() -> Optional[str]:
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    return None


class CDPError(Exception):
    pass


class Chrome:
    def __init__(self, *, headless: bool = True, window: str = "400,880", extra: tuple[str, ...] = ()) -> None:
        exe = find_chrome()
        if exe is None:
            raise CDPError("Google Chrome is not installed")
        self.profile = Path(tempfile.mkdtemp(prefix="web-smoke-chrome-"))
        os.chmod(self.profile, 0o700)
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        args = [exe, "--remote-debugging-pipe", f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                "--use-mock-keychain", f"--window-size={window}", *extra, "about:blank"]
        if headless:
            args.insert(1, "--headless")

        def child_fds() -> None:
            # Chrome reads commands on fd 3 and writes on fd 4. Move both out of the way first so
            # neither dup2 clobbers the other; os.pipe() fds are non-inheritable (PEP 446), so
            # with close_fds=False only stdio and the dup2'd 3 and 4 reach Chrome.
            import fcntl
            high_r = fcntl.fcntl(to_chrome_r, getattr(fcntl, "F_DUPFD_CLOEXEC", fcntl.F_DUPFD), 10)
            high_w = fcntl.fcntl(from_chrome_w, getattr(fcntl, "F_DUPFD_CLOEXEC", fcntl.F_DUPFD), 10)
            os.dup2(high_r, 3)
            os.dup2(high_w, 4)

        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, preexec_fn=child_fds, close_fds=False)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._out = os.fdopen(to_chrome_w, "wb", buffering=0)
        self._in = os.fdopen(from_chrome_r, "rb", buffering=0)
        self._next = 0
        self._lock = threading.Lock()
        self._pending: dict[int, "queue.Queue[dict]"] = {}
        self.events: list[dict] = []
        self._event_cv = threading.Condition()
        self.listeners: list[Callable[[dict], None]] = []
        threading.Thread(target=self._reader, name="cdp-reader", daemon=True).start()
        self.version = self.send("Browser.getVersion")
        # New pages pause until their preload script is installed (Page attaches, then resumes).
        self.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

    def _reader(self) -> None:
        buf = b""
        while True:
            chunk = self._in.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw.decode("utf-8"))
                if "id" in msg and msg["id"] in self._pending:
                    self._pending[msg["id"]].put(msg)
                else:
                    with self._event_cv:
                        self.events.append(msg)
                        self._event_cv.notify_all()
                    for listener in list(self.listeners):
                        listener(msg)

    def send(self, method: str, params: Optional[dict] = None, session: Optional[str] = None,
             timeout: float = 30.0) -> dict:
        with self._lock:
            self._next += 1
            mid = self._next
            box: "queue.Queue[dict]" = queue.Queue()
            self._pending[mid] = box
            msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            self._out.write(json.dumps(msg).encode("utf-8") + b"\0")
        try:
            reply = box.get(timeout=timeout)
        except queue.Empty:
            raise CDPError(f"{method} timed out") from None
        finally:
            self._pending.pop(mid, None)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def wait_event(self, predicate: Callable[[dict], bool], timeout: float = 10.0, start: int = 0) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        with self._event_cv:
            while True:
                for msg in self.events[start:]:
                    if predicate(msg):
                        return msg
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._event_cv.wait(left)

    def close(self) -> None:
        socket_link = self.profile / "SingletonSocket"
        socket_directory = socket_link.resolve(strict=False).parent if socket_link.is_symlink() else None
        try:
            self.send("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001 - closing anyway
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        shutil.rmtree(self.profile, ignore_errors=True)
        # Only the outside-profile socket directory referenced by THIS owned profile.
        if socket_directory and str(socket_directory).startswith(("/var/folders/", "/private/var/folders/")) and socket_directory.name.startswith(("org.chromium.", ".com.google.Chrome.")):
            shutil.rmtree(socket_directory, ignore_errors=True)


class Page:
    """One target (tab/window) attached with a flat session."""

    def __init__(self, chrome: Chrome, url: str = "about:blank", *, preload: Optional[str] = None,
                 width: int = 400, height: int = 880) -> None:
        """Open ``url`` in a new window. The target starts paused (auto-attach), so ``preload``
        runs before the first document's scripts and the window's history starts at ``url``."""
        self.chrome = chrome
        mark = len(chrome.events)
        self.target = chrome.send("Target.createTarget", {"url": url, "newWindow": True,
                                                           "width": width, "height": height})["targetId"]
        attached = chrome.wait_event(lambda m: m.get("method") == "Target.attachedToTarget"
                                     and m["params"]["targetInfo"]["targetId"] == self.target, 10, mark)
        if attached is None:
            raise CDPError("new target did not auto-attach")
        self.session = attached["params"]["sessionId"]
        for domain in ("Page", "Runtime", "Network", "Log"):
            self.send(f"{domain}.enable")
        self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                         "mobile": False})
        if preload:
            self.send("Page.addScriptToEvaluateOnNewDocument", {"source": preload})
        load_mark = len(chrome.events)
        self.send("Runtime.runIfWaitingForDebugger")
        if url != "about:blank":
            chrome.wait_event(lambda m: m.get("sessionId") == self.session and m.get("method") == "Page.loadEventFired",
                              10, load_mark)

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        return self.chrome.send(method, params, self.session, timeout)

    def goto(self, url: str, wait: float = 10.0) -> None:
        mark = len(self.chrome.events)
        self.send("Page.navigate", {"url": url})
        self.chrome.wait_event(lambda m: m.get("sessionId") == self.session and m.get("method") == "Page.loadEventFired",
                               wait, mark)

    def eval(self, expression: str, timeout: float = 30.0) -> Any:
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True,
                                             "userGesture": True}, timeout)
        if "exceptionDetails" in res:
            raise CDPError("page exception: " + json.dumps(res["exceptionDetails"].get("exception", {}).get(
                "description") or res["exceptionDetails"].get("text"))[:400])
        return res.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 10.0, interval: float = 0.1) -> Any:
        """Poll ``expression`` until truthy; a throwing expression (document mid-navigation) is falsy."""
        guarded = f"(() => {{ try {{ return ({expression}); }} catch (e) {{ return null; }} }})()"
        deadline = time.monotonic() + timeout
        while True:
            value = self.eval(guarded)
            if value:
                return value
            if time.monotonic() >= deadline:
                return value
            time.sleep(interval)

    def key(self, key: str, *, shift: bool = False, text: Optional[str] = None) -> None:
        codes = {"Tab": 9, "Enter": 13, "Escape": 27}
        modifiers = 8 if shift else 0
        base = {"key": key, "code": key, "windowsVirtualKeyCode": codes.get(key, 0), "modifiers": modifiers}
        down = dict(base, type="keyDown")
        if key == "Enter":
            down["text"] = "\r"
        if text is not None:
            down["text"] = text
        self.send("Input.dispatchKeyEvent", down)
        self.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def type_text(self, text: str) -> None:
        self.send("Input.insertText", {"text": text})

    def screenshot(self, path: Path) -> Path:
        import base64
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        Path(path).write_bytes(base64.b64decode(data))
        return Path(path)

    def close(self) -> None:
        try:
            self.chrome.send("Target.closeTarget", {"targetId": self.target}, timeout=5)
        except CDPError:
            pass
