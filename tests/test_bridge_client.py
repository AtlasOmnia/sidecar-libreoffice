# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest

from plugin.bridge_client import BridgeClient, BridgeError

TOKEN = "t" * 48


class FakeBridge:
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.requests = 0
        self.auth_headers: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                outer.requests += 1
                outer.auth_headers.append(self.headers.get("X-Hermes-Plugin-Key"))
                if outer.behavior == "timeout":
                    time.sleep(0.25)
                    return
                if outer.behavior == "garbage_500":
                    data = b"<html>oops</html>"
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if outer.behavior == "drop":
                    self.close_connection = True
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if outer.behavior == "unauthorized":
                    status = 401
                    value = {"ok": False, "category": "Auth", "error": "bridge authentication failed"}
                elif outer.behavior == "not_found":
                    status = 404
                    value = {"ok": False, "category": "NotFound", "error": "document missing"}
                elif outer.behavior == "validation":
                    status = 400
                    value = {"ok": False, "category": "Validation", "error": "bad input"}
                elif outer.behavior == "not_started":
                    status = 504
                    value = {"ok": False, "category": "Timeout", "state": "not_started", "error": "busy"}
                elif outer.behavior == "uncategorized_500":
                    status = 500
                    value = {"ok": False, "error": "boom"}
                elif outer.behavior == "unavailable_started":
                    status = 503
                    value = {"ok": False, "category": "Unavailable", "error": "after write?"}
                elif outer.behavior == "ok_no_data":
                    status = 200
                    value = {"ok": True}
                elif outer.behavior == "uncertain":
                    status = 500
                    value = {"ok": False, "category": "ExecutionUncertain", "state": "started", "error": "unknown"}
                elif outer.behavior == "stale":
                    status = 409
                    value = {"ok": False, "category": "Stale", "error": "Calc range changed since it was read"}
                else:
                    status = 200
                    value = {"ok": True, "data": {"echo": payload["op"]}}
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BridgeClientTests(unittest.TestCase):
    def test_auth_header_is_sent(self):
        fake = FakeBridge()
        self.addCleanup(fake.close)
        result = BridgeClient(fake.port, TOKEN).invoke("get_range", {"sheet": "S", "address": "A1"})
        self.assertEqual(result, {"echo": "get_range"})
        self.assertEqual(fake.auth_headers, [TOKEN])

    def test_401_is_auth_error(self):
        fake = FakeBridge("unauthorized")
        self.addCleanup(fake.close)
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(fake.port, TOKEN).invoke("get_range", {})
        self.assertEqual(caught.exception.category, "Auth")

    def test_validation_and_not_found_categories_propagate(self):
        for behavior, category in (("validation", "Validation"), ("not_found", "NotFound")):
            with self.subTest(category=category):
                fake = FakeBridge(behavior)
                self.addCleanup(fake.close)
                with self.assertRaises(BridgeError) as caught:
                    BridgeClient(fake.port, TOKEN).invoke("get_range", {})
                self.assertEqual(caught.exception.category, category)

    def test_connection_refused_is_unavailable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(port, TOKEN, timeout=0.1).invoke("get_range", {})
        self.assertEqual(caught.exception.category, "Unavailable")

    def test_timeout_after_set_send_is_uncertain_and_not_retried(self):
        fake = FakeBridge("timeout")
        self.addCleanup(fake.close)
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(fake.port, TOKEN, timeout=0.05).invoke("set_range", {"x": 1}, mutation=True)
        self.assertEqual(caught.exception.category, "ExecutionUncertain")
        time.sleep(0.3)
        self.assertEqual(fake.requests, 1)

    def test_stale_category_propagates(self):
        fake = FakeBridge("stale")
        self.addCleanup(fake.close)
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(fake.port, TOKEN).invoke("set_range", {}, mutation=True)
        self.assertEqual(caught.exception.category, "Stale")

    def _invoke(self, behavior, *, mutation, timeout=1.0):
        fake = FakeBridge(behavior)
        self.addCleanup(fake.close)
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(fake.port, TOKEN, timeout=timeout).invoke("op", {}, mutation=mutation)
        return caught.exception.category, fake

    def test_mutation_never_downgrades_unknown_outcomes(self):
        for behavior in ("garbage_500", "drop", "uncategorized_500", "unavailable_started", "ok_no_data", "uncertain"):
            with self.subTest(behavior=behavior):
                category, fake = self._invoke(behavior, mutation=True)
                self.assertEqual(category, "ExecutionUncertain")
                self.assertEqual(fake.requests, 1, "mutations are never retried")

    def test_mutation_explicit_not_started_timeout_stays_timeout(self):
        category, fake = self._invoke("not_started", mutation=True)
        self.assertEqual(category, "Timeout")
        self.assertEqual(fake.requests, 1)

    def test_read_failures_are_never_uncertain(self):
        expected = {
            "garbage_500": "Protocol", "drop": "Unavailable", "uncategorized_500": "Unavailable",
            "unavailable_started": "Unavailable", "ok_no_data": "Protocol", "not_started": "Timeout",
        }
        for behavior, category in expected.items():
            with self.subTest(behavior=behavior):
                actual, _fake = self._invoke(behavior, mutation=False)
                self.assertEqual(actual, category)

    def test_missing_token_fails_fast(self):
        with self.assertRaises(BridgeError) as caught:
            BridgeClient(8660, "")
        self.assertEqual(caught.exception.category, "Auth")


if __name__ == "__main__":
    unittest.main()
