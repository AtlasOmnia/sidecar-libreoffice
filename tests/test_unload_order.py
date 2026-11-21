# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The order of the chat endpoint's unload, over a real listener.

close() asks every run whose id it knows to stop before it waits for the listener to shut down,
and that wait is bounded by the same budget: the listener's accept loop is woken at once instead
of waiting out its 0.5 s poll, and on an event loop close() does not wait for it at all. Before
0.4.1 the listener was shut down first, which took the poll interval (or longer, when the accept
thread was busy) out of the budget before any stop was sent.

The API server is the scripted ``_call`` of test_run_ownership on the real clock. Pauses are
events inside the code under test, never sleeps.
"""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
from test_chat_bridge import API_KEY, SESSION  # noqa: E402
from test_run_ownership import REAL_CLOCK, FakeRuns  # noqa: E402

from plugin import chat_bridge  # noqa: E402

# Timed waits can return late (macOS coalesces timers of background processes by 0.1-0.2 s, and a
# loaded container more): close() has two timed waits here, so its return time gets this margin.
SLACK = 0.5
# Time the code deliberately allocates is checked apart from OS scheduling: a recorded wait's end
# (when it was entered plus the timeout it was given) may pass the deadline only by the few steps
# between the code reading the clock and entering the wait.
ALLOCATION_EPS = 0.05


class CloseOrderTests(unittest.TestCase):
    """Stops for known runs go out before close() waits for the listener, and that wait is
    bounded. The accept thread is held in ``process_request`` (a connection it is handing over)
    until the API server has received a stop, or for longer than the budget."""

    BUDGET = 1.0

    def setUp(self):
        self.api = FakeRuns(REAL_CLOCK)
        self.api.cost, self.api.stop = 0.0, "refuse"
        self.api.get_gate = threading.Event()
        self.stop_seen = threading.Event()
        self.addCleanup(self.stop_seen.set)
        self.api.on_stop = self.stop_seen.set
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             timeout=100.0, poll_interval=0.01)
        self.bridge._call = self.api
        port = self.bridge.start()
        self.addCleanup(self.bridge.close, 0.0)
        thread = threading.Thread(target=lambda: self.assertRaises(
            chat_bridge.ChatError, self.bridge.chat, {"message": "Bold row 1", "session_id": SESSION, "document": None}),
            daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.api.get_gate.set)   # runs before the join above
        with self.api.lock:
            self.assertTrue(self.api.lock.wait_for(lambda: self.api.get_held, timeout=10))
        server, entered = self.bridge._server, threading.Event()
        original = server.process_request

        def held(request, client_address):
            entered.set()
            self.stop_seen.wait(self.BUDGET + 1.5)
            original(request, client_address)
        server.process_request = held
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        self.assertTrue(entered.wait(5), "the accept thread is held")

    def test_known_runs_are_asked_to_stop_before_the_listener_is_waited_for(self):
        began = time.monotonic()
        remaining = self.bridge.close(self.BUDGET)
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(len(self.api.stops()), 1, "the run was asked to stop before close() returned")
        self.assertLessEqual(elapsed, self.BUDGET + SLACK, "the listener wait is inside the budget")
        self.assertEqual(remaining, 1)

    def test_close_on_the_event_loop_does_not_wait_for_a_held_listener(self):
        async def unload():
            return self.bridge.close(self.BUDGET)
        began = time.monotonic()
        self.assertEqual(asyncio.run(unload()), 1)
        self.assertLess(time.monotonic() - began, 0.5, "the event loop is not blocked by the listener")
        self.assertTrue(self.stop_seen.wait(self.BUDGET), "the stop still goes out in the background")


class ListenerDeadlineTests(unittest.TestCase):
    """``stop_serving(timeout)`` ends by ONE deadline taken when it is called: waking the accept
    loop and waiting for it share it, and with no time (close() on an event loop) nothing blocks.
    ``hold_blocking_connects`` holds a connect to the listener in blocking mode for its whole
    timeout (as when the listener's queue is full); a non-blocking one goes through. The accept
    loop's own tests use real connections."""

    def setUp(self):
        self.server = chat_bridge.BoundedHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler,
                                                    max_handlers=2, refusal=b"")
        self.addCleanup(self.server.server_close)
        self.blocking, self.waits = [], []
        real_wait = self.server._served.wait

        def wait(timeout=None):
            self.waits.append((time.monotonic(), timeout))
            return real_wait(timeout)
        self.server._served.wait = wait

    def hold_blocking_connects(self):
        address, never = self.server.server_address, threading.Event()

        def held(real):
            def connect(sock, target):
                timeout = sock.gettimeout()
                if tuple(target[:2]) == tuple(address[:2]) and timeout != 0.0:
                    self.blocking.append((time.monotonic(), timeout))
                    never.wait(5.0 if timeout is None else timeout)
                    raise socket.timeout("timed out")
                return real(sock, target)
            return connect
        for name in ("connect", "connect_ex"):
            patch = mock.patch.object(socket.socket, name, held(getattr(socket.socket, name)))
            patch.start()
            self.addCleanup(patch.stop)

    def stop(self, timeout):
        began = time.monotonic()
        ended = self.server.stop_serving(timeout)
        return ended, began, time.monotonic() - began

    def test_a_held_wake_and_the_wait_for_a_held_accept_loop_share_one_deadline(self):
        self.hold_blocking_connects()
        ended, began, elapsed = self.stop(0.3)
        self.assertFalse(ended, "the accept loop never ran, so it did not end")
        self.assertTrue(self.waits)
        for at, timeout in self.blocking + self.waits:
            self.assertIsNotNone(timeout)
            self.assertLessEqual(at + timeout, began + 0.3 + ALLOCATION_EPS, "nothing gets time past the deadline")
        self.assertLessEqual(elapsed, 0.3 + SLACK)

    def test_with_no_time_left_the_wake_never_blocks(self):
        self.hold_blocking_connects()
        _ended, began, elapsed = self.stop(0.0)
        self.assertEqual(self.blocking, [], "no blocking connect, not even a short one")
        for at, timeout in self.waits:
            self.assertLessEqual(at + timeout, began + ALLOCATION_EPS)
        self.assertLessEqual(elapsed, SLACK)

    def serving(self):
        """Run the accept loop with a 30 s poll and return once it waits in select()."""
        selecting = threading.Event()

        class Selector(chat_bridge._ServerSelector):
            def select(self, timeout=None):
                selecting.set()
                return super().select(timeout)
        patch = mock.patch.object(chat_bridge, "_ServerSelector", Selector)
        patch.start()
        self.addCleanup(patch.stop)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 30.0}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server._stop.set)
        self.assertTrue(selecting.wait(5), "the accept loop waits in select()")
        return thread

    def test_with_no_time_left_the_wake_still_ends_an_accept_loop_waiting_in_select(self):
        thread = self.serving()
        _ended, began, _elapsed = self.stop(0.0)
        thread.join(10)
        self.assertFalse(thread.is_alive(), "woken: it did not wait out its 30 s poll")
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(self.server.socket.fileno(), -1, "the loop closed the listener")

    def test_with_time_left_the_wake_ends_the_accept_loop_and_the_wait_sees_it(self):
        self.serving()
        ended, _began, elapsed = self.stop(10.0)
        self.assertTrue(ended)
        self.assertLess(elapsed, 5, "woken: it did not wait out its 30 s poll")
        self.assertEqual(self.server.socket.fileno(), -1)

    def test_the_standard_shutdown_still_ends_the_loop(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        began = time.monotonic()
        self.server.shutdown()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 5)


if __name__ == "__main__":
    unittest.main()
