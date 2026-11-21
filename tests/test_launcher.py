# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Sidecar launcher (extension/python/sidecar_launcher.py): the R18 channel against the
contract's test vectors and a loopback fake plugin, the bootstrap file, bounds, the browser
command line, the launch state machine, release on close and quit, and the R10 rule that only the
panel button (or the development hook) can start a browser."""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "python"
sys.path.insert(0, str(EXTENSION))
import sidecar_launcher as L  # noqa: E402

TOKEN = "example-pairing-token-0123456789abcdefghijklmnopq"
VECTOR_BODY = (b'{"document":{"document_id":"lo:5f0c:1","title":"sales.ods","type":"calc"},'
               b'"host_scheme":"light","mode":"app","bounds":{"x":1240,"y":102,"width":400,"height":848}}')
DOCUMENT = {"document_id": "lo:5f0c:1", "title": "sales.ods", "type": "calc"}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class ContractVectors(unittest.TestCase):
    """docs/web-protocol.md section 3.3, reproduced exactly."""
    client_nonce = b64u(bytes(range(32)))
    server_nonce = b64u(bytes(range(32, 64)))

    def test_nonces_match_the_document(self):
        self.assertEqual(self.client_nonce, "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
        self.assertEqual(self.server_nonce, "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8")

    def test_hello_proof_and_channel_key(self):
        self.assertEqual(L.hello_proof(TOKEN, 8661, self.client_nonce, self.server_nonce),
                         "6gAIEWKHsH5HilYgR4IFZrAzSBZsyxB9kUYyFBW3DZk")
        self.assertEqual(L.channel_key(TOKEN, 8661, self.client_nonce, self.server_nonce).hex(),
                         "49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1")

    def test_request_and_response_macs(self):
        key = L.channel_key(TOKEN, 8661, self.client_nonce, self.server_nonce)
        self.assertEqual(hashlib.sha256(VECTOR_BODY).hexdigest(),
                         "3220ea4ed994980c359a102c7b2d2d604bb604de9885a619bedc17373995bbba")
        self.assertEqual(L.request_mac(key, "POST", "/launcher/launch", 1, VECTOR_BODY),
                         "PmCfNapRjDIVbyHqzSrGnkU_mqgxbEr4XLWu0iQocX8")
        self.assertEqual(L.response_mac(key, 1, 201, b'{"ok":true}'),
                         "ZgH8usVq9byBTZB7tp2Fi7PfEpJk8YBIw-EfdEN6FRc")
        self.assertEqual(L.EMPTY_SHA256, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(L.request_mac(key, "GET", "/launcher/launch/AAAAAAAAAAAAAAAAAAAAAA", 2, b""),
                         "pz9hSDdfVxWMTMKc772QPcntSZE8IV0T3jHUv836CuI")

    def test_the_launch_body_is_serialized_as_in_the_vector(self):
        body = L.encode_body({"document": DOCUMENT, "host_scheme": "light", "mode": "app",
                              "bounds": {"x": 1240, "y": 102, "width": 400, "height": 848}})
        self.assertEqual(body, VECTOR_BODY)


class FakePlugin:
    """A loopback stand-in for the plugin's /launcher routes, written from the contract text
    (independently of sidecar_launcher). It records every raw request it receives."""

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.channels: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.states: list[str] = ["exchanged"]
        self.window_live = False
        self.hello_override = None
        self.launch_override = None
        self.forge_response_mac = False
        self.url_port = None
        self.released: list[dict] = []
        self.busy_paths: list[str] = []  # answered once each with the accept-thread busy refusal
        self.busy_answer = {"ok": False, "code": "endpoint_busy", "error": "the chat endpoint is busy; try again shortly"}
        self.trickle: dict[str, float] = {}  # path prefix -> seconds between answer bytes
        self.trickle_headers_fast = False
        plugin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return None

            def _answer(self, status, payload, n=None, key=None, raw=None):
                body = raw if raw is not None else json.dumps(payload).encode()
                delay = next((d for prefix, d in plugin.trickle.items() if self.path.startswith(prefix)), None)
                if delay is not None:
                    return self._trickle(status, body, n, key, delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if key is not None:
                    mac = L.b64u(hmac.new(key, "\n".join(["sidecar-launcher/1 response", str(n), str(status),
                                                          hashlib.sha256(body).hexdigest()]).encode(),
                                          hashlib.sha256).digest())
                    self.send_header("X-Sidecar-MAC", "A" * 43 if plugin.forge_response_mac else mac)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _trickle(self, status, body, n, key, delay):
                """A valid answer written one byte at a time (headers, then body)."""
                head = f"HTTP/1.0 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                if key is not None:
                    mac = L.b64u(hmac.new(key, "\n".join(["sidecar-launcher/1 response", str(n), str(status),
                                                          hashlib.sha256(body).hexdigest()]).encode(),
                                          hashlib.sha256).digest())
                    head += f"X-Sidecar-MAC: {mac}\r\n"
                head = (head + "Connection: close\r\n\r\n").encode()
                try:
                    if plugin.trickle_headers_fast:
                        self.wfile.write(head)
                        self.wfile.flush()
                        head = b""
                    for i in range(len(head + body)):
                        self.wfile.write((head + body)[i:i + 1])
                        self.wfile.flush()
                        time.sleep(delay)
                except OSError:
                    pass
                self.close_connection = True

            def _route(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                plugin.requests.append({"method": self.command, "path": self.path,
                                        "headers": dict(self.headers.items()), "body": body})
                if self.path in plugin.busy_paths:  # like the accept thread: before any check
                    plugin.busy_paths.remove(self.path)
                    return self._answer(503, plugin.busy_answer)
                port = self.server.server_address[1]
                if self.path == "/launcher/hello":
                    if plugin.hello_override is not None:
                        return self._answer(*plugin.hello_override)
                    cn = json.loads(body)["client_nonce"]
                    sn = L.b64u(secrets.token_bytes(32))
                    key = hmac.new(plugin.token.encode(), "\n".join(["sidecar-launcher/1 channel", str(port), cn, sn]).encode(),
                                   hashlib.sha256).digest()
                    proof = L.b64u(hmac.new(plugin.token.encode(), "\n".join(["sidecar-launcher/1 hello", str(port), cn, sn]).encode(),
                                            hashlib.sha256).digest())
                    plugin.channels[sn] = {"key": key, "next": 1}
                    return self._answer(200, {"ok": True, "v": 1, "server_nonce": sn, "server_proof": proof,
                                              "expires_in": 120, "chat": {"ready": True, "reason": None}})
                channel = plugin.channels.get(self.headers.get("X-Sidecar-Channel", ""))
                n = self.headers.get("X-Sidecar-Counter", "")
                if channel is None or n != str(channel["next"]):
                    return self._answer(401, {"ok": False, "code": "launcher_auth", "error": "x"})
                expected = L.b64u(hmac.new(channel["key"], "\n".join(["sidecar-launcher/1 request", self.command, self.path, n,
                                                                       hashlib.sha256(body).hexdigest()]).encode(),
                                           hashlib.sha256).digest())
                if not hmac.compare_digest(expected, self.headers.get("X-Sidecar-MAC", "")):
                    plugin.channels.pop(self.headers.get("X-Sidecar-Channel", ""), None)
                    return self._answer(401, {"ok": False, "code": "launcher_auth", "error": "x"})
                channel["next"] += 1
                key, n = channel["key"], int(n)
                if self.path == "/launcher/launch" and self.command == "POST":
                    if plugin.launch_override is not None:
                        return self._answer(*plugin.launch_override, n=n, key=key)
                    if plugin.window_live:
                        return self._answer(200, {"ok": True, "launch_id": None, "url": None, "window_live": True,
                                                  "expires_in": 0}, n, key)
                    code = L.b64u(secrets.token_bytes(32))
                    url = f"http://127.0.0.1:{plugin.url_port or port}/ui/index.html#launch={code}"
                    return self._answer(201, {"ok": True, "launch_id": L.b64u(secrets.token_bytes(16)), "url": url,
                                              "window_live": False, "expires_in": 30}, n, key)
                if self.path.startswith("/launcher/launch/") and self.command == "GET":
                    state = plugin.states.pop(0) if len(plugin.states) > 1 else plugin.states[0]
                    return self._answer(200, {"ok": True, "state": state}, n, key)
                if self.path == "/launcher/release" and self.command == "POST":
                    plugin.released.append(json.loads(body))
                    return self._answer(200, {"ok": True, "released": 1}, n, key)
                return self._answer(404, {"ok": False, "code": "not_found", "error": "x"}, n, key)

            do_GET = _route
            do_POST = _route

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ChannelClient(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.addCleanup(self.plugin.close)
        self.client = L.LauncherClient(self.plugin.port, lambda: TOKEN)

    def test_hello_launch_status_release_over_one_channel(self):
        self.assertEqual(self.client.hello(), {"ready": True, "reason": None})
        answer = self.client.launch(DOCUMENT, "dark", "app", {"x": 1, "y": 2, "width": 400, "height": 600})
        self.assertFalse(answer["window_live"])
        self.assertRegex(answer["url"], rf"^http://127\.0\.0\.1:{self.plugin.port}/ui/index\.html#launch=[A-Za-z0-9_-]{{43}}$")
        self.assertEqual(self.client.status(answer["launch_id"]), "exchanged")
        self.assertEqual(self.client.release(DOCUMENT["document_id"], "document_closed"), 1)
        counters = [r["headers"].get("X-Sidecar-Counter") for r in self.plugin.requests]
        self.assertEqual(counters, [None, "1", "2", "3"])

    def test_the_pairing_token_never_leaves_the_launcher(self):
        self.client.hello()
        self.client.launch(DOCUMENT, None, "tab", None)
        for request in self.plugin.requests:
            raw = json.dumps(request["headers"]).encode() + request["path"].encode() + request["body"]
            self.assertNotIn(TOKEN.encode(), raw)
            names = {name.lower() for name in request["headers"]}
            self.assertNotIn("x-hermes-plugin-key", names)
            self.assertNotIn("origin", names)
            self.assertFalse(any(name.startswith("sec-fetch-") for name in names))
            self.assertNotIn("x-sidecar-session", names)
            self.assertEqual(request["headers"]["Host"], f"127.0.0.1:{self.plugin.port}")
        self.assertEqual(json.loads(self.plugin.requests[1]["body"])["bounds"], None)

    def test_a_wrong_proof_means_not_your_hermes_and_nothing_more_is_sent(self):
        self.plugin.token = "another-token-" + "x" * 40
        with self.assertRaises(L.LauncherError) as caught:
            self.client.hello()
        self.assertEqual(str(caught.exception), L.NOT_YOURS.format(port=self.plugin.port))
        with self.assertRaises(L.LauncherError):
            self.client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(len(self.plugin.requests), 1)

    def test_hello_failures_map_to_the_panel_texts(self):
        cases = [
            ((503, {"ok": False, "code": "no_pairing", "error": "x"}), L.NO_PAIRING),
            ((401, {"ok": False, "error": "the pairing token does not match: pair LibreOffice with Hermes again"}),
             L.OLD_PLUGIN),
            ((404, {"ok": False, "error": "unknown route"}), L.OLD_PLUGIN),
            ((200, {"ok": True, "v": 1, "server_nonce": "short", "server_proof": "x"}),
             L.NOT_YOURS.format(port=self.plugin.port)),
            ((500, {"ok": False, "code": "internal", "error": "x"}), L.ENDPOINT_FAILED),
            ((503, {"ok": False, "code": "turned_off", "error": "x"}), L.TURNED_OFF),
            ((403, {"ok": False, "code": "bad_host", "error": "x"}), L.NOT_YOURS.format(port=self.plugin.port)),
            ((200, None, None, None, b"not json"), L.NOT_YOURS.format(port=self.plugin.port)),
        ]
        for override, text in cases:
            self.plugin.hello_override = override
            with self.assertRaises(L.LauncherError, msg=override) as caught:
                L.LauncherClient(self.plugin.port, lambda: TOKEN).hello()
            self.assertEqual(str(caught.exception), text)

    def test_connection_refused_means_chat_is_off(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(L.LauncherError) as caught:
            L.LauncherClient(port, lambda: TOKEN).hello()
        self.assertEqual(str(caught.exception), L.CHAT_OFF)
        self.assertEqual(L.CHAT_OFF.splitlines(), ["Chat is off in Hermes.", "See README ▸ Sidebar chat."])

    def test_a_missing_or_forged_response_mac_is_refused(self):
        self.client.hello()
        self.plugin.forge_response_mac = True
        with self.assertRaises(L.LauncherError) as caught:
            self.client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), L.NOT_YOURS.format(port=self.plugin.port))
        with self.assertRaises(L.LauncherError):  # the channel is dropped after a bad answer
            self.client.status("A" * 22)

    def test_a_url_for_another_port_or_shape_is_refused(self):
        self.plugin.url_port = self.plugin.port + 1
        self.client.hello()
        with self.assertRaises(L.LauncherError) as caught:
            self.client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), L.NOT_YOURS.format(port=self.plugin.port))

    def test_an_authenticated_refusal_shows_the_plugins_sentence(self):
        self.plugin.launch_override = (429, {"ok": False, "code": "session_cap",
                                             "error": "Too many Sidecar windows are open."})
        self.client.hello()
        with self.assertRaises(L.LauncherError) as caught:
            self.client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), "Too many Sidecar windows are open.")

    def test_window_live_answer(self):
        self.plugin.window_live = True
        self.client.hello()
        self.assertEqual(self.client.launch(DOCUMENT, None, "app", {"x": 0, "y": 0, "width": 400, "height": 500}),
                         {"window_live": True, "launch_id": None, "url": None})

    def test_token_file_problems_are_reported_before_anything_is_sent(self):
        def unsafe():
            raise L.LauncherError(L.TOKEN_UNSAFE)
        with self.assertRaises(L.LauncherError) as caught:
            L.LauncherClient(self.plugin.port, unsafe).hello()
        self.assertEqual(str(caught.exception), L.TOKEN_UNSAFE)
        self.assertEqual(self.plugin.requests, [])

    def test_read_pairing_token_uses_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            with self.assertRaises(L.LauncherError) as caught:
                L.read_pairing_token(path)
            self.assertEqual(str(caught.exception), L.TOKEN_MISSING)
            path.write_text(TOKEN + "\n")
            if os.name != "nt":  # Windows judges the DACL; chmod there does not open the file to others
                os.chmod(path, 0o644)
                with self.assertRaises(L.LauncherError) as caught:
                    L.read_pairing_token(path)
                self.assertEqual(str(caught.exception), L.TOKEN_UNSAFE)
                os.chmod(path, 0o600)
            self.assertEqual(L.read_pairing_token(path), TOKEN)


URL = "http://127.0.0.1:8661/ui/index.html#launch=" + "A" * 43


class BootstrapFile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def test_exact_bytes_private_modes_and_name(self):
        path = L.write_bootstrap(URL, self.cache)
        self.assertEqual(path.parent, self.cache / "Sidecar for LibreOffice" / "launch")
        self.assertRegex(path.name, r"^launch-[0-9a-f]{32}\.html$")
        self.assertEqual(path.read_bytes(), (
            '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f'<meta http-equiv="refresh" content="0;url={URL}"><title>Opening Sidecar…</title>').encode("utf-8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)

    def test_refuses_unexpected_url_characters(self):
        for bad in (URL + '"><script>', URL.replace("http", "https"), "file:///etc/passwd", URL + " x"):
            with self.assertRaises(ValueError):
                L.write_bootstrap(bad, self.cache)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_refuses_a_launch_folder_others_can_read(self):
        folder = self.cache / "Sidecar for LibreOffice" / "launch"
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o755)
        with self.assertRaises(OSError):
            L.write_bootstrap(URL, self.cache)
        self.assertEqual(list(folder.iterdir()), [])

    def test_sweep_removes_only_old_launch_files(self):
        old = L.write_bootstrap(URL, self.cache)
        new = L.write_bootstrap(URL, self.cache)
        other = old.parent / "notes.html"
        other.write_text("x")
        past = time.time() - 120
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.assertEqual(L.sweep(self.cache), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(other.exists())
        self.assertEqual(L.sweep(self.cache / "missing"), 0)


class BoundsAndArgv(unittest.TestCase):
    def test_right_of_the_frame_when_400_points_fit(self):
        self.assertEqual(L.compute_bounds((100, 80, 1000, 700), (0, 0, 2560, 1080)),
                         {"x": 1100, "y": 80, "width": 400, "height": 700})

    def test_against_the_work_area_edge_when_it_does_not_fit(self):
        self.assertEqual(L.compute_bounds((600, 240, 1820, 560), (0, 0, 2560, 1080)),
                         {"x": 2160, "y": 240, "width": 400, "height": 560})

    def test_clamped_into_the_contract_rectangle(self):
        bounds = L.compute_bounds((0, -40000, 200, 100), (0, 0, 1440, 900))
        self.assertEqual(bounds, {"x": 200, "y": -32768, "width": 400, "height": 240})
        self.assertEqual(L.compute_bounds(None, None)["width"], 400)

    def test_title_bar_guess(self):
        bounds = {"x": 1240, "y": 102, "width": 400, "height": 848}
        self.assertEqual(L.window_geometry(bounds, "darwin"), ((1240, 70), (400, 880)))
        self.assertEqual(L.window_geometry(bounds, "win32"), ((1240, 102), (400, 848)))

    def test_frozen_flag_list(self):
        profile = PurePosixPath("/cache/Sidecar for LibreOffice/browser")  # the macOS argv, on any test host
        argv = L.browser_argv("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", profile,
                              "file:///cache/Sidecar%20for%20LibreOffice/launch/launch-" + "0" * 32 + ".html",
                              (1240, 70), (400, 880), "darwin")
        self.assertEqual(argv, [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "--user-data-dir=/cache/Sidecar for LibreOffice/browser",
            "--app=file:///cache/Sidecar%20for%20LibreOffice/launch/launch-" + "0" * 32 + ".html",
            "--window-position=1240,70", "--window-size=400,880",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--use-mock-keychain"])
        self.assertNotIn("--use-mock-keychain", L.browser_argv("c", profile, "file:///x.html", (0, 0), (1, 1), "win32"))

    def test_each_flag_name_is_tested(self):
        for flag in ("--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                     "--disable-background-networking", "--disable-component-update", "--disable-default-apps"):
            self.assertIn(flag, L.CHROMIUM_FLAGS)
        self.assertEqual(L.MACOS_ONLY_FLAGS, ("--use-mock-keychain",))

    def test_the_launch_url_never_goes_on_the_command_line(self):
        for target in (URL, "http://127.0.0.1:8661/ui/index.html", "file:///x.html#launch=" + "A" * 43):
            with self.assertRaises(ValueError):
                L.browser_argv("c", Path("/p"), target, (0, 0), (1, 1), "darwin")


class FakeAdapter:
    def __init__(self, cache: Path, browsers=(("Google Chrome", "/Apps/Chrome"),)) -> None:
        self.cache = cache
        self.browsers = list(browsers)
        self.spawned: list[list[str]] = []
        self.bootstrap_at_spawn: list[bytes] = []
        self.opened: list[Path] = []
        self.raised: list[int] = []
        self.terminated: list[int] = []
        self.alive: set[int] = set()
        self.raise_ok = True
        self.spawn_error = None
        self.handoff = False
        self.next_pid = 500

    def find_browsers(self):
        return list(self.browsers)

    def cache_dir(self):
        return self.cache

    def spawn(self, argv):
        if self.spawn_error:
            raise self.spawn_error
        self.spawned.append(argv)
        target = next(a for a in argv if a.startswith("--app="))[len("--app="):]
        self.bootstrap_at_spawn.append(Path(urllib.request.url2pathname(urllib.parse.urlsplit(target).path)).read_bytes())
        self.next_pid += 1
        if not (self.handoff and self.alive):
            self.alive.add(self.next_pid)
        return self.next_pid

    def running(self, pid):
        return pid in self.alive

    def open_default(self, path):
        self.opened.append(Path(path))
        self.bootstrap_at_spawn.append(Path(path).read_bytes())

    def raise_pid(self, pid):
        self.raised.append(pid)
        return self.raise_ok

    def terminate(self, pid, wait=None):
        self.terminated.append(pid)
        self.alive.discard(pid)


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, script: dict) -> None:
        self.script = script
        self.calls: list[tuple] = []

    port = 8661
    deadline = None

    def factory(self):
        def make(port, read_token, timeout=L.CALL_TIMEOUT_SECONDS, **_keywords):
            self.calls.append(("new", port, timeout))
            return self
        return make

    def hello(self):
        self.calls.append(("hello",))
        if self.script.get("hello_error"):
            raise L.LauncherError(self.script["hello_error"])
        return {"ready": True, "reason": None}

    def launch(self, document, host_scheme, mode, bounds):
        self.calls.append(("launch", document, host_scheme, mode, bounds))
        if self.script.get("window_live"):
            return {"window_live": True, "launch_id": None, "url": None}
        return {"window_live": False, "launch_id": "B" * 22, "url": URL}

    def status(self, launch_id):
        self.calls.append(("status", launch_id))
        states = self.script.setdefault("states", ["exchanged"])
        state = states.pop(0) if len(states) > 1 else states[0]
        if state == "busy":
            raise L.LauncherBusy()
        return state

    def release(self, document_id, reason):
        self.calls.append(("release", document_id, reason))
        return 1


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class StateMachine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.adapter = FakeAdapter(self.cache)
        self.clock = Clock()
        self.reports: list[tuple[str, bool]] = []
        self.threads: list[str] = []
        self.deferred: list = []

    def launcher(self, script=None, platform="darwin"):
        self.client = FakeClient(script or {})

        def start(fn, name):
            self.threads.append(name)
            if name == "SidecarLaunchCleanup":
                self.deferred.append(fn)
            else:
                fn()
        launcher = L.Launcher(adapter=self.adapter, platform=platform, port=lambda: 8661,
                              client=self.client.factory(), sleep=self.clock.sleep, clock=self.clock, start=start)
        launcher.configure(lambda: TOKEN)
        return launcher

    def report(self, text, busy):
        self.reports.append((text, busy))

    def open(self, launcher, frame=(100, 80, 1000, 700)):
        return launcher.open(dict(DOCUMENT), "dark", frame, (0, 0, 2560, 1080), self.report)

    def launch_files(self):
        folder = self.cache / "Sidecar for LibreOffice" / "launch"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_app_mode_spawns_with_the_bootstrap_file_and_reports_open(self):
        launcher = self.launcher({"states": ["pending", "pending", "exchanged"]})
        self.assertTrue(self.open(launcher))
        self.assertEqual(self.reports, [(L.OPENING, True), (L.OPEN, False)])
        self.assertEqual(self.threads, ["SidecarLauncher", "SidecarLaunchCleanup"])
        launch = [c for c in self.client.calls if c[0] == "launch"][0]
        self.assertEqual(launch[1:], (DOCUMENT, "dark", "app", {"x": 1100, "y": 80, "width": 400, "height": 700}))
        argv = self.adapter.spawned[0]
        self.assertEqual(argv[0], "/Apps/Chrome")
        self.assertIn("--window-position=1100,48", argv)
        self.assertIn("--window-size=400,732", argv)
        self.assertIn(f"--user-data-dir={self.cache / 'Sidecar for LibreOffice' / 'browser'}", argv)
        self.assertNotIn("launch=", " ".join(argv))
        self.assertIn(URL.encode(), self.adapter.bootstrap_at_spawn[0])
        self.assertEqual([c[0] for c in self.client.calls].count("status"), 3)
        self.assertEqual(self.clock.sleeps, [0.5, 0.5, 0.5])
        self.assertEqual(self.launch_files(), [])  # deleted at the first non-pending state
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.cache / "Sidecar for LibreOffice" / "browser").stat().st_mode), 0o700)

    def test_tab_mode_without_a_chromium_browser(self):
        self.adapter.browsers = []
        launcher = self.launcher()
        self.open(launcher)
        launch = [c for c in self.client.calls if c[0] == "launch"][0]
        self.assertEqual(launch[3:], ("tab", None))
        self.assertEqual(self.adapter.spawned, [])
        self.assertEqual(len(self.adapter.opened), 1)
        self.assertRegex(self.adapter.opened[0].name, r"^launch-[0-9a-f]{32}\.html$")
        self.assertEqual(self.reports[-1], (L.OPEN, False))
        self.assertEqual(self.launch_files(), [])

    def test_still_pending_after_20_s_reports_did_not_connect_and_keeps_the_file_until_30_s(self):
        launcher = self.launcher({"states": ["pending"]})
        self.open(launcher)
        self.assertEqual(self.reports[-1], (L.DID_NOT_CONNECT, False))
        self.assertEqual([c[0] for c in self.client.calls].count("status"), 40)
        self.assertEqual(len(self.launch_files()), 1)
        self.assertEqual(len(self.deferred), 1)
        self.deferred.pop()()
        self.assertEqual(self.clock.now - 1000.0, 30.0)
        self.assertEqual(self.launch_files(), [])

    def test_expired_and_double_use(self):
        for state, text in (("expired", L.DID_NOT_CONNECT), ("double_use", L.DOUBLE_USE)):
            self.reports.clear()
            self.open(self.launcher({"states": [state]}))
            self.assertEqual(self.reports[-1], (text, False))
            self.assertEqual(self.launch_files(), [])

    def test_window_live_raises_the_browser_this_libreoffice_started(self):
        launcher = self.launcher()
        self.open(launcher)
        pid = next(iter(self.adapter.alive))
        self.client.script["window_live"] = True
        self.open(launcher)
        self.assertEqual(self.adapter.raised, [pid])
        self.assertEqual(self.reports[-1], (L.OPEN, False))
        self.assertEqual(len(self.adapter.spawned), 1)  # nothing new was started
        self.adapter.raise_ok = False
        self.open(launcher)
        self.assertEqual(self.reports[-1], (L.ALREADY_OPEN, False))

    def test_window_live_without_a_known_browser_or_in_tab_mode_says_already_open(self):
        launcher = self.launcher({"window_live": True})
        self.open(launcher)
        self.assertEqual(self.reports[-1], (L.ALREADY_OPEN, False))
        self.assertEqual(self.adapter.raised, [])
        self.adapter.browsers = []
        self.open(launcher)
        self.assertEqual(self.reports[-1], (L.ALREADY_OPEN, False))
        self.assertEqual(self.adapter.spawned + self.adapter.opened, [])

    def test_a_handoff_to_the_running_instance_keeps_the_first_pid(self):
        launcher = self.launcher()
        self.open(launcher)
        first = next(iter(self.adapter.alive))
        self.adapter.handoff = True  # the second process exits at once and is not alive
        second = dict(DOCUMENT, document_id="lo:5f0c:2")
        launcher.open(second, None, (0, 0, 800, 600), (0, 0, 2560, 1080), self.report)
        self.assertEqual(len(self.adapter.spawned), 2)
        self.assertEqual(launcher._live_browser(), first)

    def test_hello_failures_start_nothing(self):
        for text in (L.CHAT_OFF, L.NO_PAIRING, L.NOT_YOURS.format(port=8661), L.OLD_PLUGIN):
            self.reports.clear()
            self.open(self.launcher({"hello_error": text}))
            self.assertEqual(self.reports, [(L.OPENING, True), (text, False)])
        self.assertEqual(self.adapter.spawned + self.adapter.opened, [])
        self.assertEqual(self.launch_files(), [])

    def test_a_spawn_failure_removes_the_file(self):
        self.adapter.spawn_error = FileNotFoundError("gone")
        self.open(self.launcher())
        self.assertEqual(self.reports[-1], (L.COULD_NOT_START, False))
        self.assertEqual(self.launch_files(), [])

    def test_one_launch_per_document_at_a_time(self):
        launcher = self.launcher()
        held = []
        launcher._start = lambda fn, name: held.append(fn)
        self.assertTrue(self.open(launcher))
        self.assertFalse(self.open(launcher))
        held.pop()()
        launcher._start = lambda fn, name: fn()
        self.assertTrue(self.open(launcher))

    def test_documents_without_a_supported_type_and_unsupported_systems(self):
        launcher = self.launcher()
        self.assertFalse(launcher.open(None, None, None, None, self.report))
        self.assertFalse(launcher.open({"document_id": "x", "type": "draw"}, None, None, None, self.report))
        self.assertEqual(self.reports, [(L.NO_DOCUMENT, False)] * 2)
        unsupported = L.Launcher(platform="linux", port=lambda: 8661, start=lambda fn, name: fn())
        self.assertIsNone(unsupported.adapter)
        self.assertFalse(unsupported.open(dict(DOCUMENT), None, None, None, self.report))
        self.assertEqual(self.reports[-1], (L.UNSUPPORTED, False))

    def test_host_scheme_is_light_dark_or_null(self):
        launcher = self.launcher()
        launcher.open(dict(DOCUMENT), "sepia", None, None, self.report)
        self.assertIsNone([c for c in self.client.calls if c[0] == "launch"][0][2])

    def test_probe_reports_idle_or_the_problem_and_starts_nothing(self):
        self.launcher().probe(self.report)
        self.launcher({"hello_error": L.CHAT_OFF}).probe(self.report)
        self.assertEqual(self.reports, [(L.IDLE, False), (L.CHAT_OFF, False)])
        self.assertEqual(self.adapter.spawned + self.adapter.opened, [])
        self.assertEqual(self.threads, ["SidecarLauncherProbe", "SidecarLauncherProbe"])

    def test_document_closed_releases_only_documents_this_libreoffice_opened(self):
        launcher = self.launcher()
        launcher.on_document_closed("lo:other:1")
        self.assertEqual(self.client.calls, [])
        self.open(launcher)
        self.client.calls.clear()
        launcher.on_document_closed(DOCUMENT["document_id"])
        self.assertEqual([c[0] for c in self.client.calls], ["new", "hello", "release"])
        self.assertEqual(self.client.calls[-1], ("release", DOCUMENT["document_id"], "document_closed"))
        self.client.calls.clear()
        launcher.on_document_closed(DOCUMENT["document_id"])
        self.assertEqual(self.client.calls, [])

    def test_terminate_releases_everything_quits_the_browser_and_sweeps_once(self):
        launcher = self.launcher()
        self.open(launcher)
        pid = next(iter(self.adapter.alive))
        stale = L.write_bootstrap(URL, self.cache)
        past = time.time() - 120
        os.utime(stale, (past, past))
        self.client.calls.clear()
        launcher.on_terminate()
        self.assertEqual(self.client.calls[0], ("new", 8661, L.QUIT_TIMEOUT_SECONDS))
        self.assertEqual(self.client.calls[-1], ("release", None, "host_quit"))
        self.assertEqual(self.adapter.terminated, [pid])
        self.assertFalse(stale.exists())
        launcher.on_terminate()
        self.assertEqual(self.adapter.terminated, [pid])
        self.assertFalse(self.open(launcher))  # nothing starts while LibreOffice quits

    def test_terminate_without_any_window_sends_nothing(self):
        launcher = self.launcher()
        launcher.on_terminate()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.adapter.terminated, [])

    def test_terminate_after_the_documents_already_closed_still_sends_host_quit(self):
        # Observed natively: LibreOffice unloads every document (OnUnload) before it reports
        # termination, so the per-document releases run on workers that may not finish in time.
        launcher = self.launcher()
        self.open(launcher)
        launcher.on_document_closed(DOCUMENT["document_id"])
        self.client.calls.clear()
        launcher.on_terminate()
        self.assertEqual(self.client.calls[-1], ("release", None, "host_quit"))

    def test_terminate_never_raises(self):
        launcher = self.launcher({"hello_error": L.CHAT_OFF})
        launcher._opened.add("x")
        self.adapter.terminate = lambda pid: (_ for _ in ()).throw(RuntimeError("boom"))
        launcher.on_terminate()


class BusyAnswers(unittest.TestCase):
    """The plugin's busy refusal (every request slot in use) gets its own panel text. A 503
    without a code means busy only from a port that already proved to be a 0.5.0 plugin."""

    def setUp(self):
        self.plugin = FakePlugin()
        self.addCleanup(self.plugin.close)

    def hello_text(self, answer, current=False):
        self.plugin.hello_override = (503, answer)
        with self.assertRaises(L.LauncherError) as caught:
            L.LauncherClient(self.plugin.port, lambda: TOKEN, current=current).hello()
        return str(caught.exception)

    def test_the_busy_code_at_hello(self):
        self.assertEqual(L.BUSY, "Sidecar is busy, try again in a moment.")
        self.assertEqual(self.hello_text({"ok": False, "code": "endpoint_busy", "error": "x"}), L.BUSY)
        self.assertEqual(self.hello_text({"ok": False, "code": "busy", "error": "x"}), L.BUSY)
        self.assertEqual(self.hello_text({"ok": False, "code": "internal", "error": "x"}, current=True),
                         L.NOT_YOURS.format(port=self.plugin.port))  # other coded 503s are unchanged

    def test_the_0_5_0_busy_sentence_without_a_code(self):
        self.assertEqual(self.hello_text({"ok": False, "error": "the chat endpoint is busy; try again shortly"}),
                         L.BUSY)

    def test_a_503_without_a_code_is_busy_only_from_a_known_0_5_0_plugin(self):
        answer = {"ok": False, "error": "the plugin could not answer"}
        self.assertEqual(self.hello_text(answer, current=False), L.OLD_PLUGIN)
        self.assertEqual(self.hello_text(answer, current=True), L.BUSY)
        self.assertEqual(self.hello_text({"ok": False, "code": "no_pairing", "error": "x"}, current=True),
                         L.NO_PAIRING)

    def test_the_launcher_remembers_a_port_that_answered_as_0_5_0(self):
        launcher = L.Launcher(adapter=FakeAdapter(Path(".")), platform="darwin", port=lambda: self.plugin.port,
                              start=lambda fn, name: fn())
        launcher.configure(lambda: TOKEN)
        reports = []
        launcher.probe(lambda text, busy: reports.append(text))
        self.plugin.hello_override = (503, {"ok": False, "error": "the plugin could not answer"})
        launcher.probe(lambda text, busy: reports.append(text))
        fresh = L.Launcher(adapter=FakeAdapter(Path(".")), platform="darwin", port=lambda: self.plugin.port,
                           start=lambda fn, name: fn())
        fresh.configure(lambda: TOKEN)
        fresh.probe(lambda text, busy: reports.append(text))
        self.assertEqual(reports, [L.IDLE, L.BUSY, L.OLD_PLUGIN])

    def test_a_busy_refusal_on_the_channel_does_not_use_up_the_counter(self):
        client = L.LauncherClient(self.plugin.port, lambda: TOKEN)
        client.hello()
        self.plugin.busy_paths.append("/launcher/launch")
        with self.assertRaises(L.LauncherBusy) as caught:
            client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), L.BUSY)
        answer = client.launch(DOCUMENT, None, "tab", None)  # same channel, same counter
        self.assertFalse(answer["window_live"])
        counters = [r["headers"].get("X-Sidecar-Counter") for r in self.plugin.requests]
        self.assertEqual(counters, [None, "1", "1"])

    def test_a_busy_status_poll_keeps_waiting(self):
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(cache, ignore_errors=True))
        clock = Clock()
        client = FakeClient({"states": ["busy", "busy", "exchanged"]})
        launcher = L.Launcher(adapter=FakeAdapter(cache), platform="darwin", port=lambda: 8661,
                              client=client.factory(), sleep=clock.sleep, clock=clock,
                              start=lambda fn, name: None if name == "SidecarLaunchCleanup" else fn())
        launcher.configure(lambda: TOKEN)
        reports = []
        launcher.open(dict(DOCUMENT), None, (0, 0, 800, 600), (0, 0, 2560, 1080),
                      lambda text, busy: reports.append(text))
        self.assertEqual(reports, [L.OPENING, L.OPEN])


class Trickler:
    """A program on the chat port that answers one byte at a time, each byte well inside any
    per-recv timeout, so only a whole-call deadline can end the call."""

    def __init__(self, delay: float = 0.05, headers_fast: bool = False) -> None:
        body = b'{"ok": true, "v": 1}' + b" " * 400
        head = (f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode()
        self.answer, self.head, self.delay, self.headers_fast = head + body, head, delay, headers_fast
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.drip, args=(conn,), daemon=True).start()

    def drip(self, conn):
        with conn:
            try:
                conn.recv(65536)
                data = self.answer
                if self.headers_fast:
                    conn.sendall(self.head)
                    data = self.answer[len(self.head):]
                for i in range(len(data)):
                    if self.stopped.is_set():
                        return
                    conn.sendall(data[i:i + 1])
                    time.sleep(self.delay)
            except OSError:
                return

    def close(self):
        self.stopped.set()
        self.listener.close()


class WholeCallDeadline(unittest.TestCase):
    """critic2 B4: a socket timeout bounds one recv, not a call. Every launcher call, and quit as
    a whole, must end on time against an answer trickled faster than any per-recv timeout."""

    def trickler(self, **kwargs):
        trickler = Trickler(**kwargs)
        self.addCleanup(trickler.close)
        return trickler

    def timed(self, fn):
        started = time.monotonic()
        try:
            fn()
            error = None
        except L.LauncherError as exc:
            error = str(exc)
        return time.monotonic() - started, error

    def test_a_trickled_status_line_and_headers_end_the_hello_on_time(self):
        trickler = self.trickler()
        elapsed, error = self.timed(lambda: L.LauncherClient(trickler.port, lambda: TOKEN, timeout=0.5).hello())
        self.assertLess(elapsed, 1.5)
        self.assertEqual(error, L.NO_ANSWER.format(port=trickler.port))

    def test_a_trickled_body_ends_the_hello_on_time(self):
        trickler = self.trickler(headers_fast=True)
        elapsed, error = self.timed(lambda: L.LauncherClient(trickler.port, lambda: TOKEN, timeout=0.5).hello())
        self.assertLess(elapsed, 1.5)
        self.assertEqual(error, L.NO_ANSWER.format(port=trickler.port))

    def test_a_shared_deadline_bounds_every_call_on_the_client(self):
        trickler = self.trickler()
        client = L.LauncherClient(trickler.port, lambda: TOKEN, timeout=30, deadline=time.monotonic() + 0.4)
        elapsed, error = self.timed(client.hello)
        self.assertLess(elapsed, 1.4)
        self.assertEqual(error, L.NO_ANSWER.format(port=trickler.port))
        elapsed, error = self.timed(client.hello)  # past the deadline: nothing is even sent
        self.assertLess(elapsed, 0.2)
        self.assertEqual(error, L.NO_ANSWER.format(port=trickler.port))

    def test_a_trickled_status_answer_ends_on_time(self):
        plugin = FakePlugin()
        self.addCleanup(plugin.close)
        client = L.LauncherClient(plugin.port, lambda: TOKEN, timeout=0.5)
        client.hello()
        launch_id = client.launch(DOCUMENT, None, "tab", None)["launch_id"]
        plugin.trickle["/launcher/launch/"] = 0.05
        elapsed, error = self.timed(lambda: client.status(launch_id))
        self.assertLess(elapsed, 1.5)
        self.assertEqual(error, L.NO_ANSWER.format(port=plugin.port))

    def quit_launcher(self, port):
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(cache, ignore_errors=True))
        launcher = L.Launcher(adapter=FakeAdapter(cache), platform="darwin", port=lambda: port,
                              start=lambda fn, name: fn())
        launcher.configure(lambda: TOKEN)
        launcher._ever_opened = True  # a window was opened earlier, so quit sends host_quit
        return launcher

    def test_quit_ends_on_time_against_a_trickled_hello(self):
        trickler = self.trickler()
        launcher = self.quit_launcher(trickler.port)
        elapsed, _ = self.timed(launcher.on_terminate)
        self.assertLess(elapsed, L.QUIT_TIMEOUT_SECONDS + 1.0)

    def test_quit_hello_and_release_share_one_deadline(self):
        plugin = FakePlugin()
        self.addCleanup(plugin.close)
        plugin.trickle["/launcher/release"] = 0.05
        launcher = self.quit_launcher(plugin.port)
        elapsed, _ = self.timed(launcher.on_terminate)
        self.assertLess(elapsed, L.QUIT_TIMEOUT_SECONDS + 1.0)
        self.assertEqual([r["path"] for r in plugin.requests], ["/launcher/hello", "/launcher/release"])
        self.assertLessEqual(L.QUIT_TIMEOUT_SECONDS, L.QUIT_BUDGET_SECONDS)

    def test_the_bootstrap_cleanup_is_scheduled_when_the_file_is_written(self):
        """Deleted 30 s after creation even while a status call hangs (not only after _follow)."""
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(cache, ignore_errors=True))
        adapter = FakeAdapter(cache)
        scheduled = []
        seen_at_spawn = []
        spawn = adapter.spawn
        adapter.spawn = lambda argv: (seen_at_spawn.append(list(scheduled)), spawn(argv))[1]
        clock = Clock()
        client = FakeClient({})
        launcher = L.Launcher(adapter=adapter, platform="darwin", port=lambda: 8661, client=client.factory(),
                              sleep=clock.sleep, clock=clock,
                              start=lambda fn, name: scheduled.append(name) if name == "SidecarLaunchCleanup" else fn())
        launcher.configure(lambda: TOKEN)
        launcher.open(dict(DOCUMENT), None, (0, 0, 800, 600), (0, 0, 2560, 1080), lambda *a: None)
        self.assertEqual(seen_at_spawn, [["SidecarLaunchCleanup"]])


class Gate:
    """A barrier a fake step waits at until the test opens it."""

    def __init__(self) -> None:
        self.arrived = threading.Event()
        self.opened = threading.Event()

    def wait(self) -> None:
        self.arrived.set()
        if not self.opened.wait(10):
            raise RuntimeError("gate never opened")


class GatedClient(FakeClient):
    """The launch worker's client stops at the gated step; later clients (release on close,
    host_quit) are ungated and log into the same list."""

    def __init__(self, gates: dict[str, Gate], log: list, script: dict) -> None:
        super().__init__(script)
        self.gates = gates
        self.calls = log

    def hello(self):
        result = super().hello()
        if "hello" in self.gates:
            self.gates.pop("hello").wait()
        return result

    def launch(self, *args):
        if "launch" in self.gates:  # the plugin is minting the code when the document closes
            self.gates.pop("launch").wait()
        return super().launch(*args)

    def status(self, launch_id):
        if "status" in self.gates:
            self.gates.pop("status").wait()
        return super().status(launch_id)


class CancelInFlight(unittest.TestCase):
    """critic2 B1: closing the document or quitting LibreOffice while a launch is in flight. The
    worker is held at each step in turn: before hello returns, while the plugin mints, after the
    mint, while the browser starts, and while the outcome is polled; then the document closes or
    LibreOffice quits; then the step finishes. App and tab adapters."""

    STEPS = ("hello", "launch", "after_mint", "spawn", "status")

    def run_case(self, step: str, event: str, mode: str):
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(cache, ignore_errors=True))
        adapter = FakeAdapter(cache, browsers=((("Google Chrome", "/Apps/Chrome"),) if mode == "app" else ()))
        gate = Gate()
        log: list = []
        gates = {step: gate} if step in ("hello", "launch", "status") else {}
        made: list = []

        def factory(port, read_token, timeout=L.CALL_TIMEOUT_SECONDS, **_keywords):
            client = GatedClient(gates if not made else {}, log, {"states": ["pending", "exchanged"]})
            log.append(("new", port, timeout))
            made.append(client)
            return client

        if step == "after_mint":  # the worker first asks for cache_dir right after the 201
            cache_dir = adapter.cache_dir

            def gated_cache_dir():
                if threading.current_thread().name == "SidecarLauncher" and not gate.opened.is_set():
                    gate.wait()
                return cache_dir()
            adapter.cache_dir = gated_cache_dir
        if step == "spawn":
            original = adapter.spawn if mode == "app" else adapter.open_default
            hold = lambda *a: (gate.wait(), original(*a))[1]
            if mode == "app":
                adapter.spawn = hold
            else:
                adapter.open_default = hold
        workers: list[threading.Thread] = []

        def start(fn, name):
            if name == "SidecarLaunchCleanup":
                return  # the 30 s timer is not under test here
            thread = threading.Thread(target=fn, name=name, daemon=True)
            workers.append(thread)
            thread.start()

        clock = Clock()
        launcher = L.Launcher(adapter=adapter, platform="darwin", port=lambda: 8661, client=factory,
                              sleep=clock.sleep, clock=clock, start=start)
        launcher.configure(lambda: TOKEN)
        reports: list = []
        self.assertTrue(launcher.open(dict(DOCUMENT), "light", (0, 0, 800, 600), (0, 0, 2560, 1080),
                                      lambda text, busy: reports.append(text)))
        self.assertTrue(gate.arrived.wait(5), f"{step}: the worker never reached the step")
        if event == "close":
            launcher.on_document_closed(DOCUMENT["document_id"])
        else:
            if step == "spawn":  # quit waits (bounded) for a browser being started
                threading.Timer(0.2, gate.opened.set).start()
            started = time.monotonic()
            launcher.on_terminate()
            self.assertLess(time.monotonic() - started, L.QUIT_BUDGET_SECONDS + 0.5)
        gate.opened.set()
        for thread in list(workers):
            thread.join(5)
            self.assertFalse(thread.is_alive())
        return launcher, adapter, log, reports, cache

    def check(self, step, event, mode):
        launcher, adapter, log, reports, cache = self.run_case(step, event, mode)
        label = f"{step}/{event}/{mode}"
        self.assertNotIn(L.OPEN, reports, label)
        self.assertEqual(reports, [L.OPENING], label)  # nothing stale reported after the cancel
        started = adapter.spawned + adapter.opened
        if step in ("hello", "launch", "after_mint"):
            self.assertEqual(started, [], f"{label}: a browser started after the cancel")
        launched = [c for c in log if c[0] == "launch"]
        released = [c for c in log if c[0] == "release"]
        if step == "hello":
            self.assertEqual(launched, [], f"{label}: a code was minted for a closed document")
        expected = ("release", None, "host_quit") if event == "quit" else ("release", DOCUMENT["document_id"],
                                                                         "document_closed")
        if launched:  # a minted code is released, after the launch call, whatever the step
            self.assertIn(expected, released, label)
            last = max(i for i, call in enumerate(log) if call == expected)
            self.assertGreater(last, log.index(launched[0]), label)
        if step == "spawn" and mode == "app":  # started while the cancel happened: quit again
            self.assertTrue(adapter.alive <= set(adapter.terminated) or not adapter.alive,
                            f"{label}: the browser started for a cancelled launch is still running")
        folder = cache / "Sidecar for LibreOffice" / "launch"
        self.assertEqual(sorted(folder.iterdir()) if folder.exists() else [], [], f"{label}: bootstrap left")
        self.assertEqual(getattr(launcher, "_launches", {}), {}, label)
        self.assertEqual(launcher._opened, set(), label)

    def test_close_at_every_step_app_mode(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.check(step, "close", "app")

    def test_close_at_every_step_tab_mode(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.check(step, "close", "tab")

    def test_quit_at_every_step_app_mode(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.check(step, "quit", "app")

    def test_quit_at_every_step_tab_mode(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.check(step, "quit", "tab")

    def test_after_a_cancelled_launch_the_document_can_open_again(self):
        launcher, adapter, log, reports, cache = self.run_case("launch", "close", "app")
        reports.clear()
        done = threading.Event()
        launcher._start = lambda fn, name: None if name == "SidecarLaunchCleanup" else (fn(), done.set())
        self.assertTrue(launcher.open(dict(DOCUMENT), None, (0, 0, 800, 600), (0, 0, 2560, 1080),
                                      lambda text, busy: reports.append(text)))
        self.assertEqual(reports, [L.OPENING, L.OPEN])


class SpawnOnlyFromTheButton(unittest.TestCase):
    """R10 by construction: a browser starts only from Launcher.open (the panel button), which
    only the panel's open_sidecar and the HERMES_LO_DEV hook call."""

    @staticmethod
    def callers(path: Path, name: str) -> set[str]:
        """Names of the innermost functions that contain a call ``<x>.name(...)``."""
        found: set[str] = set()

        def visit(node: ast.AST, current: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == name):
                    found.add(str(current))
                visit(child, inner)
        visit(ast.parse(path.read_text(encoding="utf-8")), None)
        return found

    def test_spawn_and_open_default_are_called_only_by_the_launch_step(self):
        self.assertEqual(self.callers(EXTENSION / "sidecar_launcher.py", "spawn"), {"_launch"})
        self.assertEqual(self.callers(EXTENSION / "sidecar_launcher.py", "open_default"), {"_launch"})
        self.assertEqual(self.callers(EXTENSION / "sidecar_launcher.py", "_launch"), {"run"})

    def test_only_the_button_and_the_dev_hook_open_sidecar(self):
        self.assertEqual(self.callers(EXTENSION / "hermes_sidebar.py", "open"), {"open_sidecar"})
        self.assertEqual(self.callers(EXTENSION / "hermes_sidebar.py", "open_sidecar"), {"actionPerformed"})
        self.assertEqual(self.callers(EXTENSION / "hermes_bridge.py", "open_sidecar"), {"run"})
        source = (EXTENSION / "hermes_bridge.py").read_text(encoding="utf-8")
        hook = source[source.index("    def dev_open_sidecar"):source.index("    def dev_select_text")]
        self.assertIn('os.environ.get("HERMES_LO_DEV") != "1"', hook)
        self.assertIn("panel.open_sidecar()", hook)
        self.assertEqual(source.count("open_sidecar()"), 1)
        show = source[source.index("    def show_chat_panel"):source.index("    def dev_open_sidecar")]
        self.assertNotIn("launcher", show.lower())

    def test_the_bridge_reaches_the_launcher_only_for_release(self):
        source = (EXTENSION / "hermes_bridge.py").read_text(encoding="utf-8")
        used = set(re.findall(r"sidecar_launcher\.(\w+)", source))
        self.assertEqual(used, {"on_document_closed", "on_terminate", "on_start"})


class UnsignedAnswersAfterHello(unittest.TestCase):
    """Answers nobody signed only choose among fixed panel texts: the plugin's own coded hello
    refusals, and an unsigned 401 on the first call after a hello that proved the plugin."""

    def setUp(self):
        self.plugin = FakePlugin()
        self.addCleanup(self.plugin.close)

    def test_coded_hello_refusals_show_fixed_texts_never_the_answers_own_words(self):
        for status, code, text in ((503, "turned_off", L.TURNED_OFF), (500, "internal", L.ENDPOINT_FAILED)):
            self.plugin.hello_override = (status, {"ok": False, "code": code, "error": "Visit http://example.invalid now"})
            with self.assertRaises(L.LauncherError, msg=code) as caught:
                L.LauncherClient(self.plugin.port, lambda: TOKEN).hello()
            self.assertEqual(str(caught.exception), text)
        self.assertEqual(len(self.plugin.requests), 2)  # nothing is sent after either hello

    def test_an_unsigned_launcher_auth_on_the_first_call_means_try_again(self):
        client = L.LauncherClient(self.plugin.port, lambda: TOKEN)
        client.hello()
        self.plugin.channels.clear()  # displaced between the hello and the first call
        with self.assertRaises(L.LauncherError) as caught:
            client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), L.NO_ANSWER.format(port=self.plugin.port))
        with self.assertRaises(L.LauncherError):  # the channel is dropped all the same
            client.status("A" * 22)
        self.assertEqual(len(self.plugin.requests), 2)

    def test_a_later_unsigned_401_is_still_not_your_hermes(self):
        client = L.LauncherClient(self.plugin.port, lambda: TOKEN)
        client.hello()
        client.launch(DOCUMENT, None, "tab", None)
        self.plugin.channels.clear()
        with self.assertRaises(L.LauncherError) as caught:
            client.status("A" * 22)
        self.assertEqual(str(caught.exception), L.NOT_YOURS.format(port=self.plugin.port))

    def test_the_texts_are_the_plugins_sentences(self):
        sys.path.insert(0, str(ROOT))
        from plugin import web_api
        self.assertEqual(L.TURNED_OFF, web_api.ERRORS["turned_off"][1])
        self.assertTrue(L.ENDPOINT_FAILED.lower().startswith(web_api.ERRORS["internal"][1]))


class HelloFloodAgainstThePlugin(unittest.TestCase):
    """The real plugin endpoint: a flood of hellos from a program without the token, landing
    between the launcher's hello and its first call, makes the panel say try again, not that
    something other than Hermes answered (contract 3.1)."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from support import fake_runs_api as support
        self.support = support
        api = support.FakeRunsApi()
        self.addCleanup(api.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        bridge, self.port, self.web = support.make_bridge(api, root=support.make_fixture(folder.name))
        self.addCleanup(bridge.close, 3.0)

    def test_a_hello_flood_before_the_first_call_says_did_not_answer(self):
        client = L.LauncherClient(self.port, lambda: self.support.PAIRING_TOKEN)
        client.hello()
        for _ in range(17):  # one more than the unused pool holds
            stranger = self.support.LauncherClient(self.port, token="not-the-pairing-token-" + "x" * 30)
            try:
                stranger.hello()
            except AssertionError:  # its proof cannot verify without the token
                pass
        with self.assertRaises(L.LauncherError) as caught:
            client.launch(DOCUMENT, None, "tab", None)
        self.assertEqual(str(caught.exception), L.NO_ANSWER.format(port=self.port))

    def test_without_a_flood_the_same_launch_succeeds(self):
        client = L.LauncherClient(self.port, lambda: self.support.PAIRING_TOKEN)
        client.hello()
        self.assertFalse(client.launch(DOCUMENT, None, "tab", None)["window_live"])


if __name__ == "__main__":
    unittest.main()
