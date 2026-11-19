# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Defects found in native acceptance on a Windows 11 host.

1. A refused chat request must close gracefully: Windows resets a connection closed with unread
request bytes, and the panel then reports a lost connection instead of the refusal. The graceful
close is bounded by ONE deadline, so a client that declares more than it sends, or trickles bytes,
cannot delay the refusal or hold a server thread.
2. The development bridge client must judge token privacy with private_files (DACL on Windows),
not POSIX mode bits, which Windows always reports as 0o666.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from plugin import chat_bridge  # noqa: E402
import live_client  # noqa: E402

TOKEN = "t" * 43


class GracefulRefusalClose(unittest.TestCase):
    def setUp(self):
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: "k", api_port=9)
        self.port = self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.lingered: list[tuple[int, float]] = []
        original = chat_bridge._linger_close

        def spy(connection, *args, **kwargs):
            start = time.monotonic()
            result = original(connection, *args, **kwargs)
            self.lingered.append((result, time.monotonic() - start))
            return result
        patcher = mock.patch.object(chat_bridge, "_linger_close", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _head(self, token: str, length: int) -> bytes:
        return (f"POST /chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                f"{chat_bridge.TOKEN_HEADER}: {token}\r\nContent-Length: {length}\r\n\r\n").encode()

    @staticmethod
    def _read_until_eof(conn: socket.socket) -> bytes:
        """The server half-closes after its reply, so EOF arrives even while this side stays open."""
        reply = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                reply += data
        except socket.timeout:
            pass
        return reply

    def _wait_for_linger(self):
        for _ in range(150):
            if self.lingered:
                return
            threading.Event().wait(0.02)

    def test_a_full_refused_body_still_gets_the_refusal(self):
        body = b'{"message":"' + b"x" * 30000 + b'","session_id":"s"}'
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(self._head("wrong" * 9, len(body)) + body)
            reply = self._read_until_eof(conn)
        self.assertIn(b" 401 ", reply.split(b"\r\n", 1)[0])
        self.assertIn(b"pair LibreOffice with Hermes again", reply)

    def test_declaring_more_than_is_sent_cannot_delay_the_refusal_or_hold_the_thread(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            start = time.monotonic()
            conn.sendall(self._head("wrong" * 9, 1000) + b"0123456789")  # 990 bytes never come
            reply = self._read_until_eof(conn)
            elapsed = time.monotonic() - start
            self._wait_for_linger()
        self.assertIn(b" 401 ", reply.split(b"\r\n", 1)[0] if reply else b"no reply")
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(self.lingered), 1)
        self.assertLess(self.lingered[0][1], 0.75)

    def test_a_trickling_client_holds_the_linger_for_one_deadline_only(self):
        stop = threading.Event()
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(self._head(TOKEN, chat_bridge.MAX_BODY + 1) + b"{")

            def trickle():
                while not stop.wait(0.4):  # each byte lands just before a per-recv timeout would
                    try:
                        conn.sendall(b"x")
                    except OSError:
                        return
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            reply = self._read_until_eof(conn)
            self._wait_for_linger()
            stop.set()
            sender.join(2)
        self.assertIn(b" 413 ", reply.split(b"\r\n", 1)[0] if reply else b"no reply")
        self.assertEqual(len(self.lingered), 1)
        self.assertLess(self.lingered[0][1], 0.75)

    def test_there_is_no_unbounded_body_drain(self):
        self.assertFalse(hasattr(chat_bridge, "_drain_body"))


class DevClientTokenCheck(unittest.TestCase):
    def test_token_privacy_is_judged_by_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            path.write_text(TOKEN)
            with mock.patch.object(live_client.private_files, "read_private_bytes", return_value=TOKEN.encode()) as reader:
                with mock.patch.object(live_client.os, "stat", side_effect=AssertionError("mode bits must not decide")):
                    self.assertEqual(live_client.read_token(str(path)), TOKEN)
            reader.assert_called_once()
            with mock.patch.object(live_client.private_files, "read_private_bytes", side_effect=RuntimeError("token is a link")):
                with self.assertRaisesRegex(live_client.ClientError, "missing or insecure"):
                    live_client.read_token(str(path))


if __name__ == "__main__":
    unittest.main()
