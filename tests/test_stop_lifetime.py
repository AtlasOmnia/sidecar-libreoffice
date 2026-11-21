# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""How long the endpoint's own stop work may live, over real loopback sockets.

A user Stop is sent by a short-lived thread of its own (docs/web-protocol.md 6.5). That thread is
registered before it starts, its whole call (connect, send, answer) has ONE deadline, and close()
cuts it off: after the cut-off it sends no Stop payload, also when the API server stalls the
answer or trickles it byte by byte, or when the thread had not even connected yet (critic2 R3).
That is a promise about what is sent, not that every thread has ended: a thread that has not
started, or is still inside an operating-system connect, can outlive close() and then sends
nothing; on an event loop close() returns at once and the cut-off comes at the end of the budget,
so a Stop not yet connected can still be sent before then (6.5). close() asks every known run to
stop before it waits for the listener to shut
down, and that wait is bounded by the same budget (critic2 R4). A close() that runs while a
registered Stop's thread has not started yet neither fails nor joins it (critic2 R5), and every
wait of close() and of the listener's shutdown ends by ONE deadline: no sender and no phase gets
a fresh allowance (critic2 R6).

The API server here is written at the socket level so its stop answer can stall or trickle; the
key is a test value. Pauses are events inside the code under test, never sleeps.
"""
from __future__ import annotations

import asyncio
import http.server
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


# Time the code deliberately allocates is checked apart from OS scheduling: a recorded wait's end
# (when it was entered plus the timeout it was given) may pass the cut-off only by the few steps
# between the code reading the clock and entering the wait. A fresh 0.25 s grace is far above it.
ALLOCATION_EPS = 0.05


class HeldStart:
    """``Thread.start`` of a user Stop's thread, held after the Stop was registered and before
    the real start (critic2 R5). ``paused`` is set once a start is held; ``resume()`` lets it
    go on, ``resume(fail=True)`` makes it fail like a thread the system cannot start."""

    def __init__(self, test):
        self.paused, self._go, self.fail = threading.Event(), threading.Event(), False
        real = threading.Thread.start

        def start(thread):
            if thread.name == STOP_THREAD:
                self.paused.set()
                self._go.wait(30)
                if self.fail:
                    raise RuntimeError("can't start new thread")
            return real(thread)
        patch = mock.patch.object(threading.Thread, "start", start)
        patch.start()
        test.addCleanup(patch.stop)
        test.addCleanup(self._go.set)

    def resume(self, fail=False):
        self.fail = fail
        self._go.set()


class StopStartRaceTests(unittest.TestCase):
    """critic2 R5: request_stop registers a user Stop, then starts its thread. close() runs in
    between: for a run still unresolved (short budget) and for a run seen to settle meanwhile
    (the normal 10 s budget), on an ordinary thread and on an event loop, once and twice, with
    the start then succeeding or failing. close() never raises, its end hook runs, it returns
    the true count, nothing stays registered, and the cut-off Stop never connects or sends."""

    def setUp(self):
        patch = mock.patch.object(chat_bridge, "RECONCILE_SECONDS", 2.0)
        patch.start()
        self.addCleanup(patch.stop)
        self.api = RawApi("ok")
        self.addCleanup(self.api.close)
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             api_port=self.api.port, timeout=100.0, poll_interval=0.01,
                                             web_routes=False)
        self.addCleanup(self.bridge.close, 0.0)
        self.ends = []
        self.bridge.add_close_hook(lambda: None, self.ends.append)
        self.outcomes = Outcomes()
        self.chat_run = self.bridge.begin_turn(SESSION, chat_bridge.compose_message("Bold row 1", None), self.outcomes)
        self.assertTrue(self.api.wait_for("get_held"), "the run's first status poll is held")
        self.stop_connects = []
        real_connect = socket.create_connection

        def create_connection(*args, **kwargs):
            if threading.current_thread().name == STOP_THREAD:
                self.stop_connects.append(time.monotonic())
            return real_connect(*args, **kwargs)
        patch = mock.patch.object(socket, "create_connection", create_connection)
        patch.start()
        self.addCleanup(patch.stop)
        self.cut = threading.Event()
        real_cut = self.bridge._cut_off_all

        def cut_off_all():
            real_cut()
            self.cut.set()
        self.bridge._cut_off_all = cut_off_all
        self.held = HeldStart(self)
        self.answers = []
        self.stopper = threading.Thread(target=lambda: self.answers.append(self.bridge.request_stop(self.chat_run)),
                                        daemon=True)
        self.stopper.start()
        self.addCleanup(self.stopper.join, 10)
        self.assertTrue(self.held.paused.wait(10), "the Stop is registered and its start is held")
        self.assertEqual(len(self.bridge._stop_senders), 1)

    def settle(self):
        """Another thread observes the run's end while the Stop's start is held."""
        self.api.get_answer = "completed"
        self.api.get_release.set()
        self.assertEqual(self.outcomes.wait().kind, "completed")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def close(self, budget, on_loop=False):
        async def unload():
            return self.bridge.close(budget)
        began = time.monotonic()
        try:
            remaining = asyncio.run(unload()) if on_loop else self.bridge.close(budget)
        except Exception as exc:  # noqa: BLE001 - the failure under test
            self.fail(f"close() raised {type(exc).__name__}: {exc}")
        return remaining, time.monotonic() - began

    def finish(self, fail):
        """Let the held start go on (or fail), then wait for every thread involved to end."""
        self.held.resume(fail)
        self.stopper.join(10)
        self.assertEqual(self.answers, [True])
        for thread in stop_threads():
            thread.join(5)
        self.outcomes.wait()
        self.assertEqual(stop_threads(), [])

    def assert_clean(self, owned):
        self.assertEqual(self.bridge.owned_runs(), owned, "the count close() returned is still true")
        self.assertEqual(self.stop_connects, [], "the cut-off Stop never connected")
        self.assertEqual(self.api.events("stop"), [], "no stop request reached the API server")
        self.assertFalse(self.chat_run.stopping, "the run's stop slot is free again")
        with self.bridge._lock:
            self.assertEqual(self.bridge._stop_senders, set(), "no sender left registered")
            self.assertEqual(self.bridge._polls, set(), "no call left registered")

    def unresolved(self, fail, on_loop=False):
        remaining, elapsed = self.close(0.3, on_loop)
        self.assertEqual(remaining, 1, "never seen to settle: unresolved")
        self.assertEqual(len(self.ends), 1, "close()'s end hook ran")
        if on_loop:
            self.assertEqual(self.ends, [0.0])
            self.assertTrue(self.cut.wait(5), "the budget's timer cut the calls off")
        else:
            self.assertLessEqual(elapsed, 0.3 + chat_bridge.STOP_JOIN_SECONDS + SLACK, "close() kept its budget")
        self.assertEqual(self.close(0.3, on_loop)[0], 1, "a second close() agrees")
        self.finish(fail)
        self.assert_clean(owned=1)

    def test_close_while_the_start_is_held_for_an_unresolved_run_then_the_start_succeeds(self):
        self.unresolved(fail=False)

    def test_close_while_the_start_is_held_for_an_unresolved_run_then_the_start_fails(self):
        self.unresolved(fail=True)

    def test_close_on_the_event_loop_while_the_start_is_held_then_the_start_succeeds(self):
        self.unresolved(fail=False, on_loop=True)

    def test_close_on_the_event_loop_while_the_start_is_held_then_the_start_fails(self):
        self.unresolved(fail=True, on_loop=True)

    def settled(self, fail):
        self.settle()
        remaining, elapsed = self.close(chat_bridge.CLOSE_SECONDS)
        self.assertEqual(remaining, 0)
        self.assertEqual(len(self.ends), 1, "close()'s end hook ran")
        self.assertGreater(self.ends[0], chat_bridge.CLOSE_SECONDS / 2, "with the budget it had left")
        self.assertLessEqual(elapsed, chat_bridge.STOP_JOIN_SECONDS + SLACK,
                             "a settled run's sender holds close() for one cut-off grace at most")
        self.assertEqual(self.close(chat_bridge.CLOSE_SECONDS)[0], 0, "a second close() agrees")
        self.assertEqual(len(self.ends), 2)
        self.finish(fail)
        self.assert_clean(owned=0)

    def test_a_run_settled_while_the_start_is_held_then_close_with_the_normal_budget_and_the_start_succeeds(self):
        self.settled(fail=False)

    def test_a_run_settled_while_the_start_is_held_then_close_with_the_normal_budget_and_the_start_fails(self):
        self.settled(fail=True)

    def test_a_start_that_happens_while_close_waits_is_joined_before_close_returns(self):
        self.settle()
        resumer = threading.Thread(target=lambda: self.cut.wait(10) and self.held.resume(), daemon=True)
        resumer.start()
        self.addCleanup(resumer.join, 10)
        remaining, _elapsed = self.close(chat_bridge.CLOSE_SECONDS)
        alive = stop_threads()
        self.assertEqual(remaining, 0)
        self.assertEqual(len(self.ends), 1)
        self.assertEqual(alive, [], "the late-started, cut-off thread was joined")
        self.finish(fail=False)
        self.assert_clean(owned=0)


class SharedCutoffTests(unittest.TestCase):
    """critic2 R6(a): four user Stops, two of them for runs already seen to settle, all held
    inside their connect, and a listener whose accept loop is held; close() gets 0.1 s. Every
    wait close() enters ends by ONE cut-off, at most ``STOP_JOIN_SECONDS`` after the budget,
    measured both as the time each wait was given and on the wall clock; the senders still in
    their connect are reported, and once released they send nothing."""

    BUDGET = 0.1

    def setUp(self):
        patch = mock.patch.object(chat_bridge, "RECONCILE_SECONDS", 2.0)
        patch.start()
        self.addCleanup(patch.stop)
        self.api = RawApi("ok")
        self.addCleanup(self.api.close)
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             api_port=self.api.port, timeout=100.0, poll_interval=0.01,
                                             web_routes=False)
        port = self.bridge.start()
        self.addCleanup(self.bridge.close, 0.0)
        self.server, accepting, self.hold = self.bridge._server, threading.Event(), threading.Event()
        hold = self.hold
        self.addCleanup(hold.set)
        original = self.server.process_request

        def held_request(request, client_address):
            accepting.set()
            hold.wait(15)
            original(request, client_address)
        self.server.process_request = held_request
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        self.assertTrue(accepting.wait(5), "the accept loop is held")
        self.release, self.connecting = threading.Event(), threading.Semaphore(0)
        self.addCleanup(self.release.set)
        real_connect = socket.create_connection

        def held_connect(*args, **kwargs):
            if threading.current_thread().name == STOP_THREAD:
                self.connecting.release()
                self.release.wait(15)
            return real_connect(*args, **kwargs)
        patch = mock.patch.object(socket, "create_connection", held_connect)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_sender_and_phase_shares_one_cutoff(self):
        runs = []
        for index in range(4):
            run = self.bridge._claim(f"lo-{index + 1:032x}")
            run.key, run.run_id, run.sent = API_KEY, f"run_{index + 1}", True
            runs.append(run)
        for run in runs[:2]:
            self.bridge._abandon(run)   # unresolved, idle: close()'s own stop thread drives it
        for run in runs:
            self.assertTrue(self.bridge.request_stop(run))
        for _run in runs:
            self.assertTrue(self.connecting.acquire(timeout=10), "each user Stop is inside its connect")
        for run in runs[2:]:
            self.bridge._release(run)   # seen to settle by another thread meanwhile
        closer, allocated = threading.current_thread(), []
        real_wait, real_served = self.bridge._lock.wait, self.server._served.wait
        real_join = threading.Thread.join

        def recorded(what, real):
            def wait(timeout=None):
                if threading.current_thread() is closer:
                    allocated.append((what, time.monotonic(), timeout))
                return real(timeout)
            return wait

        def join(thread, timeout=None):
            if threading.current_thread() is closer and thread.name == STOP_THREAD:
                allocated.append(("join", time.monotonic(), timeout))
            return real_join(thread, timeout)
        self.bridge._lock.wait = recorded("wait", real_wait)
        self.server._served.wait = recorded("listener", real_served)
        with mock.patch.object(threading.Thread, "join", join), \
                self.assertLogs(chat_bridge.log, "WARNING") as logs:
            began = time.monotonic()
            remaining = self.bridge.close(self.BUDGET)
            returned = time.monotonic()
        alive = stop_threads()
        cutoff = self.bridge._close_deadline + chat_bridge.STOP_JOIN_SECONDS
        self.assertEqual(remaining, 2, "the two idle runs were never seen to settle")
        self.assertTrue(allocated, "close() waited")
        for what, at, timeout in allocated:
            self.assertIsNotNone(timeout, f"{what}: every wait is bounded")
            self.assertLessEqual(at + timeout, cutoff + ALLOCATION_EPS,
                                 f"{what}: no wait is given time past the one cut-off")
        self.assertLessEqual(returned - began, self.BUDGET + chat_bridge.STOP_JOIN_SECONDS + SLACK,
                             "close() kept its budget plus one grace")
        self.assertEqual(len(alive), 4, "the senders still in their connect outlive close() ...")
        self.assertTrue(any("4 user Stop" in line for line in logs.output), "... and close() says so")
        self.release.set()
        for thread in alive:
            thread.join(5)
        for thread in threading.enumerate():
            if thread.name == "HermesLibreOfficeChatStop":
                thread.join(5)
        self.assertEqual(stop_threads(), [])
        self.assertEqual(self.api.events("stop"), [], "released after the cut-off, they sent nothing")
        self.assertEqual(self.bridge.owned_runs(), 2)
        self.assertFalse(any(run.stopping for run in runs))
        with self.bridge._lock:
            self.assertEqual(self.bridge._stop_senders, set())
            self.assertEqual(self.bridge._polls, set())


    def test_a_settled_runs_stop_stuck_in_connect_holds_close_one_grace_at_most(self):
        # dev-critic round 3, N1/T12: the run is seen to settle while its Stop is stuck inside its
        # connect; close() with a 3 s budget owes nothing and must not wait out that budget.
        self.hold.set()   # the listener is not what is measured here
        run = self.bridge._claim(f"lo-{1:032x}")
        run.key, run.run_id, run.sent = API_KEY, "run_1", True
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.connecting.acquire(timeout=10), "the user Stop is inside its connect")
        self.bridge._release(run)
        began = time.monotonic()
        remaining = self.bridge.close(3.0)
        elapsed = time.monotonic() - began
        alive = stop_threads()
        self.assertEqual(remaining, 0)
        self.assertLessEqual(elapsed, chat_bridge.STOP_JOIN_SECONDS + SLACK,
                             "a settled run's stuck Stop holds close() for one grace at most")
        self.assertEqual(len(alive), 1, "the thread stuck in its connect is left behind")
        self.release.set()
        for thread in alive:
            thread.join(5)
        self.assertEqual(stop_threads(), [])
        self.assertEqual(self.api.events("stop"), [], "released after the cut-off, it sent nothing")
        with self.bridge._lock:
            self.assertEqual(self.bridge._stop_senders, set())


class ListenerDeadlineTests(unittest.TestCase):
    """critic2 R6(b): ``stop_serving(timeout)`` ends by ONE deadline taken when it is called:
    waking the accept loop and waiting for it share it, and with no time (close() on an event
    loop) nothing blocks. ``hold_blocking_connects`` holds a connect to the listener in blocking
    mode for its whole timeout (as when the listener's queue is full); a non-blocking one goes
    through. The accept loop's own tests use real connections."""

    def setUp(self):
        self.server = chat_bridge.BoundedHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler,
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


if __name__ == "__main__":
    unittest.main()
