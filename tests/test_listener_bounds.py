# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Resource bounds of both loopback listeners: the plugin's sidebar chat endpoint and the
LibreOffice extension's bridge.

Any local process can connect without a token, so neither listener may let a client hold a
thread by sending nothing, trickling bytes or declaring a body it never sends, and a flood of
connections must be refused at once instead of spawning a thread each. A request gets ONE
deadline for headers and body together; the chat's long upstream wait is separate from it.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import logging
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_chat_bridge import API_KEY, SESSION, TOKEN, FakeApiServer  # noqa: E402
from test_extension_bridge import load_bridge_module  # noqa: E402

from plugin import chat_bridge  # noqa: E402

DEADLINE = 0.5
BRIDGE_TOKEN = "b" * 43


def read_until_closed(conn: socket.socket, limit: float = 3.0) -> tuple[bytes, float]:
    """Everything the server sends until it closes, and how long that took."""
    start, data = time.monotonic(), b""
    conn.settimeout(limit)
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, ConnectionResetError, ConnectionAbortedError):
        pass  # Windows reports a peer's close after unread bytes as a reset or an abort
    return data, time.monotonic() - start


def status_of(reply: bytes) -> int:
    try:
        return int(reply.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return 0


class ListenerBoundsMixin:
    """The same four abuse cases, run against each listener by the subclasses below."""

    port: int

    def health_request(self) -> bytes:  # pragma: no cover - provided by subclasses
        raise NotImplementedError

    def truncated_post(self, declared: int, sent: bytes) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def test_a_silent_connection_is_closed_after_one_read_deadline(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            _reply, elapsed = read_until_closed(conn)
        self.assertLess(elapsed, DEADLINE + 1.0)

    def test_trickled_headers_cannot_extend_the_deadline(self):
        stop = threading.Event()
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(b"GET /health HTTP/1.1\r\n")

            def trickle():
                while not stop.wait(0.1):   # one byte well inside any per-recv timeout
                    try:
                        conn.sendall(b"X")
                    except OSError:
                        return
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            _reply, elapsed = read_until_closed(conn)
            stop.set()
            sender.join(2)
        self.assertLess(elapsed, DEADLINE + 1.0)

    def test_a_body_that_never_arrives_is_refused_within_the_deadline(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(self.truncated_post(100, b'{"op":'))
            reply, elapsed = read_until_closed(conn)
        self.assertEqual(status_of(reply), 408, reply[:200])
        self.assertLess(elapsed, DEADLINE + 1.5)

    def test_a_connection_flood_is_refused_at_once_and_the_listener_recovers(self):
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(2)]
        try:
            time.sleep(0.1)                  # both accepted and holding a handler slot
            start = time.monotonic()
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=5) as extra:
                    extra.sendall(self.health_request())
                    reply, _elapsed = read_until_closed(extra)
            except (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError):
                reply = b""   # the refusal closes without lingering, so a reset can overtake the 503
            elapsed = time.monotonic() - start
            if reply or sys.platform != "win32":
                self.assertEqual(status_of(reply), 503, reply[:200])
            # Windows drops received data when a reset arrives; there a reset is the refusal.
            self.assertLess(elapsed, 0.4)
        finally:
            for conn in idle:
                conn.close()
        time.sleep(DEADLINE + 0.3)          # the slots are released when their deadline passes
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(self.health_request())
            reply, _elapsed = read_until_closed(conn)
        self.assertEqual(status_of(reply), 200, reply[:200])


class ChatEndpointBounds(ListenerBoundsMixin, unittest.TestCase):
    def setUp(self):
        for name, value in (("READ_DEADLINE_SECONDS", DEADLINE), ("MAX_HANDLERS", 2)):
            patcher = mock.patch.object(chat_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = FakeApiServer()
        self.addCleanup(self.api.close)
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: API_KEY,
                                             api_port=self.api.port)
        self.port = self.bridge.start()
        self.addCleanup(self.bridge.stop)

    def health_request(self) -> bytes:
        return (f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{chat_bridge.TOKEN_HEADER}: {TOKEN}\r\n"
                "Connection: close\r\n\r\n").encode()

    def truncated_post(self, declared: int, sent: bytes) -> bytes:
        return (f"POST /chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                f"{chat_bridge.TOKEN_HEADER}: {TOKEN}\r\nContent-Length: {declared}\r\n\r\n").encode() + sent

    def test_the_read_deadline_does_not_cut_a_long_chat(self):
        """The upstream wait is not a read: a reply that takes longer than the deadline arrives."""
        self.api.hold = threading.Event()
        threading.Timer(DEADLINE * 3, self.api.hold.set).start()
        body = json.dumps({"message": "hi", "session_id": SESSION}).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as conn:
            conn.sendall(self.truncated_post(len(body), body))
            reply, elapsed = read_until_closed(conn, limit=10)
        self.assertEqual(status_of(reply), 200, reply[:200])
        self.assertGreater(elapsed, DEADLINE * 2)


class ExtensionBridgeBounds(ListenerBoundsMixin, unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge_module()
        for name, value in (("READ_DEADLINE_SECONDS", DEADLINE), ("MAX_HANDLERS", 2)):
            patcher = mock.patch.object(self.bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = types.SimpleNamespace(token=BRIDGE_TOKEN, port=0, logger=logging.getLogger("test-bridge"),
                                             invoke=lambda op, args: {"op": op})
        self.server = self.bridge.make_server(self.runtime, 0)
        self.port = self.runtime.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, declared: int, sent: bytes) -> bytes:
        return (f"POST /invoke HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                f"X-Hermes-Plugin-Key: {BRIDGE_TOKEN}\r\nContent-Length: {declared}\r\n\r\n").encode() + sent

    def health_request(self) -> bytes:
        body = b'{"op":"get_overview","args":{}}'
        return self._post(len(body), body)

    def truncated_post(self, declared: int, sent: bytes) -> bytes:
        return self._post(declared, sent)


class ExtensionBridgeLogLines(unittest.TestCase):
    """bridge.log names a fixed route label and the status, never the path a client sent."""

    def test_log_lines_carry_route_labels_not_request_paths(self):
        bridge = load_bridge_module()
        logger = logging.getLogger("test-bridge-log-lines")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        lines: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        capture = Capture()
        logger.addHandler(capture)
        self.addCleanup(logger.removeHandler, capture)
        runtime = types.SimpleNamespace(token=BRIDGE_TOKEN, port=0, logger=logger,
                                        invoke=lambda op, args: {"op": op})
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.handler_for(runtime))
        runtime.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        marker = "secret-path-MARKER-7f3a"
        body = b'{"op":"get_overview","args":{}}'
        requests = [
            f"POST /{marker}?q={marker} HTTP/1.0\r\nHost: 127.0.0.1:{runtime.port}\r\n\r\n".encode(),
            f"GET /{marker} HTTP/1.0\r\nHost: 127.0.0.1:{runtime.port}\r\n\r\n".encode(),
            f"BOGUS /{marker} HTTP/1.0\r\n\r\n".encode(),
            f"garbage {marker}\r\n\r\n".encode(),
            (f"POST /invoke HTTP/1.0\r\nHost: 127.0.0.1:{runtime.port}\r\nContent-Type: application/json\r\n"
             f"X-Hermes-Plugin-Key: {BRIDGE_TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body,
        ]
        for raw in requests:
            with socket.create_connection(("127.0.0.1", runtime.port), timeout=5) as conn:
                conn.sendall(raw)
                read_until_closed(conn)
        deadline = time.monotonic() + 2
        while len(lines) < len(requests) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn(marker, line)
            self.assertNotIn(BRIDGE_TOKEN, line)
        self.assertIn("http POST invoke -> 200", lines)
        self.assertIn("http POST unknown -> 404", lines)


class ExtensionTokenComparison(unittest.TestCase):
    """The pairing token is compared as bytes: a header with non-ASCII bytes is a plain 401."""

    def test_a_non_ascii_token_header_is_refused_with_401_not_500(self):
        bridge = load_bridge_module()
        runtime = types.SimpleNamespace(token=BRIDGE_TOKEN, port=0, logger=logging.getLogger("test-bridge"),
                                        invoke=lambda op, args: {"op": op})
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.handler_for(runtime))
        runtime.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = b'{"op":"get_overview","args":{}}'
        for supplied in (b"\xff\xfe" + b"b" * 41, "b\u00e9".encode("latin-1") * 20, BRIDGE_TOKEN.encode()):
            head = (f"POST /invoke HTTP/1.0\r\nHost: 127.0.0.1:{runtime.port}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n").encode() + b"X-Hermes-Plugin-Key: " + supplied + b"\r\n\r\n"
            with socket.create_connection(("127.0.0.1", runtime.port), timeout=5) as conn:
                conn.sendall(head + body)
                reply, _elapsed = read_until_closed(conn)
            expected = 200 if supplied == BRIDGE_TOKEN.encode() else 401
            self.assertEqual(status_of(reply), expected, supplied)


if __name__ == "__main__":
    unittest.main()
