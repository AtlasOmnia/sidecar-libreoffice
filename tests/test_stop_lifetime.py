# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""How long the endpoint's own stop work may live, over real loopback sockets.

A user Stop is sent by a short-lived thread of its own (docs/web-protocol.md 6.5). That thread is
registered before it starts, its whole call (connect, send, answer) has ONE deadline, and close()
cuts it off and joins it: after close() returns it neither runs nor sends, also when the API
server stalls the answer or trickles it byte by byte, or when the thread had not even connected
yet (critic2 R3). close() asks every known run to stop before it waits for the listener to shut
down, and that wait is bounded by the same budget (critic2 R4).

The API server here is written at the socket level so its stop answer can stall or trickle; the
key is a test value. Pauses are events inside the code under test, never sleeps.
"""
from __future__ import annotations

import asyncio
import json
import select
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

STOP_THREAD = "HermesLibreOfficeChatUserStop"
# Timed waits can return late (macOS coalesces timers of background processes by 0.1-0.2 s, and a
# loaded container more): close() has two timed waits here, so its return time gets this margin.
SLACK = 0.5


def stop_threads():
    return [thread for thread in threading.enumerate() if thread.name == STOP_THREAD and thread.is_alive()]


class RawApi:
    """``POST /v1/runs`` answers ``run_1``; the FIRST status poll is held until ``get_release``
    (then answers ``get_answer``), later ones answer ``running`` at once. A stop request is
    answered as ``stop_mode`` says: ``stall`` (never answered), ``trickle`` (status line and
    headers with ``Connection: close``, then one body byte every 40 ms), ``trickle_head`` (the
    status line, then one header byte every 40 ms) or ``ok`` (cancelled).
    ``log`` records ``(event, time.monotonic())``: ``stop`` when a stop request has arrived whole,
    ``stop_peer_gone`` when the endpoint ended that connection, ``stop_bytes_after`` for bytes it
    sent on it after its request."""

    def __init__(self, stop_mode):
        self.stop_mode = stop_mode
        self.lock = threading.Condition()
        self.log = []
        self.get_release, self.done = threading.Event(), threading.Event()
        self.get_answer, self.gets = "running", 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.threads = []
        threading.Thread(target=self._accept, daemon=True).start()

    def note(self, event):
        with self.lock:
            self.log.append((event, time.monotonic()))
            self.lock.notify_all()

    def events(self, name):
        with self.lock:
            return [at for event, at in self.log if event == name]

    def wait_for(self, name, seconds=10.0):
        with self.lock:
            return self.lock.wait_for(lambda: any(event == name for event, _at in self.log), timeout=seconds)

    def _accept(self):
        while not self.done.is_set():
            try:
                conn, _addr = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            with self.lock:
                self.threads.append(thread)

    @staticmethod
    def _read_request(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None, None
            data += chunk
        head, _sep, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        length = 0
        for line in lines[1:]:
            name, _colon, value = line.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        while len(body) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        method, path = lines[0].split(" ")[:2]
        return method, path

    @staticmethod
    def _answer(conn, status, value):
        data = json.dumps(value).encode()
        conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)

    def _serve(self, conn):
        try:
            method, path = self._read_request(conn)
            if method == "POST" and path.endswith("/v1/runs"):
                self._answer(conn, 202, {"run_id": "run_1", "status": "started"})
            elif method == "GET":
                with self.lock:
                    self.gets += 1
                    first = self.gets == 1
                if first:
                    self.note("get_held")
                    self.get_release.wait(30)
                    self._answer(conn, 200, {"run_id": "run_1", "status": self.get_answer,
                                             "output": "Done."})
                else:
                    self._answer(conn, 200, {"run_id": "run_1", "status": "running"})
            elif method == "POST" and path.endswith("/stop"):
                self.note("stop")
                self._stop(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def _stop(self, conn):
        if self.stop_mode == "ok":
            self._answer(conn, 200, {"run_id": "run_1", "status": "cancelled"})
            return
        if self.stop_mode == "stall":
            conn.settimeout(30)
            try:
                extra = conn.recv(1)   # b"" once the endpoint ends the connection
            except OSError:
                extra = b""
            self.note("stop_bytes_after" if extra else "stop_peer_gone")
            return
        if self.stop_mode == "trickle_head":   # the status line, then the headers one byte at a time
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            filler = b"X-Filler: " + b"a" * 390
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n"
                         b"Connection: close\r\n\r\n")
            filler = b" " * 400
        for index in range(400):   # trickle: one byte every 40 ms, far longer than any budget here
            if self.done.wait(0.04):
                return
            try:
                readable, _w, _x = select.select([conn], [], [], 0)
                if readable and not conn.recv(1):   # EOF: the endpoint ended the connection
                    raise ConnectionResetError
                conn.sendall(filler[index:index + 1])
            except OSError:
                self.note("stop_peer_gone")
                return
        self.note("stop_trickle_finished")

    def close(self):
        self.done.set()
        self.get_release.set()
        try:
            self.listener.close()
        except OSError:
            pass
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(5)


class Outcomes:
    def __init__(self):
        self.lock = threading.Condition()
        self.items = []

    def __call__(self, run, outcome):
        with self.lock:
            self.items.append(outcome)
            self.lock.notify_all()

    def wait(self, seconds=10.0):
        with self.lock:
            assert self.lock.wait_for(lambda: self.items, timeout=seconds), "no outcome arrived"
            return self.items[0]


class UserStopLifetimeTests(unittest.TestCase):
    """critic2 R3, with ``RECONCILE_SECONDS`` 2 s (a user Stop's own bound) and a 0.5 s close."""

    BUDGET = 0.5

    def setUp(self):
        patch = mock.patch.object(chat_bridge, "RECONCILE_SECONDS", 2.0)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, stop_mode):
        self.api = RawApi(stop_mode)
        self.addCleanup(self.api.close)
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             api_port=self.api.port, timeout=100.0, poll_interval=0.01,
                                             web_routes=False)
        self.addCleanup(self.bridge.close, 0.0)
        self.outcomes = Outcomes()
        run = self.bridge.begin_turn(SESSION, chat_bridge.compose_message("Bold row 1", None), self.outcomes)
        self.assertTrue(self.api.wait_for("get_held"), "the run's first status poll is held")
        return run

    def close_now(self):
        began = time.monotonic()
        remaining = self.bridge.close(self.BUDGET)
        returned = time.monotonic()
        alive = stop_threads()
        return remaining, returned - began, returned, alive

    def assert_bounded(self, elapsed, alive, returned):
        self.assertLessEqual(elapsed, self.BUDGET + getattr(chat_bridge, "STOP_JOIN_SECONDS", 0.25) + SLACK, "close() kept its budget")
        self.assertEqual(alive, [], "no user Stop thread outlives close()")
        gone = self.api.events("stop_peer_gone")
        self.assertTrue(gone, "the endpoint ended the stop's connection")
        self.assertLessEqual(gone[0], returned + SLACK, "it ended it by the time close() returned (the fake looks every 40 ms)")
        self.assertEqual(self.api.events("stop_bytes_after"), [], "nothing more was sent on it")

    def test_a_stop_whose_answer_never_comes_is_cut_off_and_joined_by_close(self):
        run = self.start("stall")
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.api.wait_for("stop"))
        remaining, elapsed, returned, alive = self.close_now()
        self.assertEqual(remaining, 1, "never seen to settle: unresolved")
        self.assertTrue(self.api.wait_for("stop_peer_gone", 5))
        self.assert_bounded(elapsed, alive, returned)

    def test_a_stop_answer_trickled_after_connection_close_headers_is_cut_off_and_joined(self):
        # http.client drops its socket once headers with Connection: close are read; the body
        # is still read from it. The cut-off must reach that socket.
        run = self.start("trickle")
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.api.wait_for("stop"))
        remaining, elapsed, returned, alive = self.close_now()
        self.assertEqual(remaining, 1)
        self.assertTrue(self.api.wait_for("stop_peer_gone", 5), "the trickled answer is no longer read")
        self.assert_bounded(elapsed, alive, returned)
        self.assertEqual(self.api.events("stop_trickle_finished"), [])

    def test_a_stop_whose_headers_are_trickled_is_cut_off_and_joined(self):
        run = self.start("trickle_head")
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.api.wait_for("stop"))
        remaining, elapsed, returned, alive = self.close_now()
        self.assertEqual(remaining, 1)
        self.assertTrue(self.api.wait_for("stop_peer_gone", 5))
        self.assert_bounded(elapsed, alive, returned)

    def test_a_stop_still_connecting_when_close_begins_never_sends_after_close_returned(self):
        entered, release = threading.Event(), threading.Event()
        real = socket.create_connection

        def held_connect(*args, **kwargs):
            if threading.current_thread().name == STOP_THREAD:
                entered.set()
                release.wait(15)
            return real(*args, **kwargs)
        run = self.start("ok")
        with mock.patch.object(socket, "create_connection", held_connect):
            self.assertTrue(self.bridge.request_stop(run))
            self.assertTrue(entered.wait(10), "the stop's thread is connecting")
            remaining, elapsed, returned, _alive = self.close_now()
            senders = stop_threads()
            release.set()
            for thread in senders:
                thread.join(5)
        self.assertEqual(remaining, 1)
        self.assertLessEqual(elapsed, self.BUDGET + getattr(chat_bridge, "STOP_JOIN_SECONDS", 0.25) + SLACK)
        self.assertEqual(stop_threads(), [], "the thread ends once its connect returns")
        late = [at for at in self.api.events("stop") if at > returned]
        self.assertEqual(late, [], "no stop request reaches the API server after close() returned")

    def test_a_run_seen_to_settle_does_not_hold_close_for_its_stalled_stop(self):
        run = self.start("stall")
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.api.wait_for("stop"))
        self.api.get_answer = "completed"
        self.api.get_release.set()   # the driving thread sees the run complete meanwhile
        self.assertEqual(self.outcomes.wait().kind, "completed")
        self.bridge.close(2.0)
        self.assertEqual(stop_threads(), [], "the stop of a settled run is cut off and joined")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_stop_thread_that_cannot_start_leaves_the_stop_to_the_driving_thread(self):
        run = self.start("ok")
        real_start = threading.Thread.start

        def failing_start(thread):
            if thread.name == STOP_THREAD:
                raise RuntimeError("can't start new thread")
            return real_start(thread)
        with mock.patch.object(threading.Thread, "start", failing_start):
            self.assertTrue(self.bridge.request_stop(run))
        self.assertFalse(run.stopping, "the stop slot is free again")
        self.assertEqual(self.bridge._stop_senders, set())
        self.api.get_release.set()
        self.assertEqual(self.outcomes.wait().kind, "user", "the driving thread sent the stop")
        self.assertTrue(self.api.events("stop"))
        self.assertEqual(self.bridge.owned_runs(), 0)


class CloseOrderTests(unittest.TestCase):
    """critic2 R4: stops for known runs go out before close() waits for the listener, and that
    wait is bounded. The accept thread is held in ``process_request`` (a connection it is
    handing over) until the API server has received a stop, or for longer than the budget."""

    BUDGET = 1.0

    def setUp(self):
        self.api = FakeRuns(REAL_CLOCK)
        self.api.cost, self.api.stop = 0.0, "refuse"
        self.api.get_gate = threading.Event()
        self.addCleanup(self.api.get_gate.set)
        self.stop_seen = threading.Event()
        self.addCleanup(self.stop_seen.set)
        self.api.on_stop = self.stop_seen.set
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             timeout=100.0, poll_interval=0.01, web_routes=False)
        self.bridge._call = self.api
        port = self.bridge.start()
        self.addCleanup(self.bridge.close, 0.0)
        thread = threading.Thread(target=lambda: self.assertRaises(
            chat_bridge.ChatError, self.bridge.chat, {"message": "Bold row 1", "session_id": SESSION, "document": None}),
            daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
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
        stops = len(self.api.stops())
        self.assertGreaterEqual(stops, 1, "the run was asked to stop before close() returned")
        self.assertLessEqual(elapsed, self.BUDGET + SLACK, "the listener wait is inside the budget")
        self.assertEqual(remaining, 1)

    def test_close_on_the_event_loop_does_not_wait_for_a_held_listener(self):
        async def unload():
            return self.bridge.close(self.BUDGET)
        began = time.monotonic()
        self.assertEqual(asyncio.run(unload()), 1)
        self.assertLess(time.monotonic() - began, 0.5, "the event loop is not blocked by the listener")
        self.assertTrue(self.stop_seen.wait(self.BUDGET), "the stop still goes out in the background")


if __name__ == "__main__":
    unittest.main()
