# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Ownership of the runs the chat endpoint starts, when Hermes's answers go missing.

A run Hermes may have admitted must stay owned (its session busy, its ``MAX_ACTIVE`` slot taken,
its stop attempted on unload) until a terminal state is seen. These tests replace only the
endpoint's one call to the API server (``ChatBridge._call``) with a scripted Runs API and the
module's clock with a fake one, so lost answers, timeouts, refusals and key revocation are exact
and need no model, network or real waiting.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time as real_time
import types
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
from test_chat_bridge import API_KEY, SESSION  # noqa: E402

import plugin  # noqa: E402
from plugin import chat_bridge  # noqa: E402

NEW_KEY = "api-key-" + "n" * 40
BUDGET = 10.0   # chat_bridge's close budget, in fake seconds


class FakeClock:
    """``time.monotonic``/``time.sleep`` for chat_bridge: time moves only when a call costs it."""

    def __init__(self):
        self.now, self.lock = 1000.0, threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def advance_to(self, when):
        with self.lock:
            self.now = max(self.now, when)

    def sleep(self, seconds):
        self.advance_to(self.monotonic() + max(0.0, seconds))
        real_time.sleep(0.0005)


class FakeRuns:
    """The Runs API as the endpoint sees it, one ``_call`` at a time.

    ``post``: ``ok``, ``lost_once`` (admits the run, then the answer is lost once) or
    ``lost_always``. ``get``: ``ok``, ``timeout``, ``503``, ``malformed``, ``strange`` (a state
    Hermes never reports), ``401``, ``shape`` (200 with ``status`` = ``shape``); ``get_failures``
    limits a failure mode to that many answers. ``stop``: ``ok`` (the run settles as cancelled),
    ``refuse`` (409), ``timeout``, ``unconfirmed`` (acknowledged but never seen to settle),
    ``shape`` (200 with ``status`` = ``stop_shape``; ``stop_failures`` limits it, then ``ok``).
    Keys in ``revoked`` get 401 everywhere. Every call costs ``cost`` fake seconds; a ``timeout``
    costs the whole timeout it was given. ``get_gate``: the first status poll waits on this
    event (``get_held`` tells it has begun) and then answers as ``get_after_gate`` says;
    ``on_stop`` is called as each stop request arrives.
    """

    def __init__(self, clock, events=None):
        self.clock, self.events = clock, events if events is not None else []
        self.calls, self.runs, self.by_key = [], {}, {}
        self.post, self.get, self.stop = "ok", "ok", "ok"
        self.get_failures, self.state, self.reply, self.cost = None, "running", "done", 0.5
        self.revoked, self.post_gate, self.stop_together = set(), None, 0
        self.shape, self.stop_shape, self.stop_failures = None, None, None
        self.get_gate, self.get_held, self.get_after_gate, self.on_stop = None, False, "ok", None
        self.lock = threading.Condition()

    # helpers for assertions
    def made(self, method, suffix=""):
        return [c for c in self.calls if c["method"] == method and c["path"].endswith(suffix)]

    def creations(self):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("/v1/runs")]

    def stops(self):
        return self.made("POST", "/stop")

    def __call__(self, key, method, path, payload=None, *, timeout=30.0, headers=None):
        at = self.clock.monotonic()
        call = {"method": method, "path": path, "key": key, "timeout": timeout, "at": at,
                "idempotency_key": (headers or {}).get("Idempotency-Key")}
        with self.lock:
            self.calls.append(call)
            self.lock.notify_all()
        if method == "GET" and self.get_gate is not None and not self.get_held:
            with self.lock:
                self.get_held = True
                self.lock.notify_all()
            self.get_gate.wait(30)
            if self.get_after_gate == "401" or key in self.revoked:
                return (401, {"error": {"message": "invalid API key"}})
            if self.get_after_gate == "shape":
                return (200, {"run_id": path.rsplit("/", 1)[-1], "status": self.shape})
            return (200, {"run_id": path.rsplit("/", 1)[-1], "status": "running"})
        if method == "POST" and path.endswith("/stop") and self.on_stop is not None:
            self.on_stop()
        if key in self.revoked:
            return self._cost(at, (401, {"error": {"message": "invalid API key"}}))
        if method == "POST" and path.endswith("/v1/runs"):
            return self._create(key, call, timeout, at)
        if method == "POST" and path.endswith("/stop"):
            return self._stop(path.rsplit("/", 2)[-2], timeout, at)
        return self._status(path.rsplit("/", 1)[-1], timeout, at)

    def _cost(self, at, answer):
        self.clock.advance_to(at + self.cost)
        return answer

    def _lost(self, at, timeout):
        self.clock.advance_to(at + min(timeout, 30.0))   # the socket gives up after its timeout
        raise chat_bridge.ChatError(502, "the Hermes API server did not answer")

    def _create(self, key, call, timeout, at):
        if self.post_gate is not None:
            self.post_gate.wait(10)
        known = self.by_key.get(call["idempotency_key"]) if call["idempotency_key"] else None
        if known is None:
            known = f"run_{len(self.runs):032x}"
            self.runs[known] = {"key": key, "stopped": False}
            if call["idempotency_key"]:
                self.by_key[call["idempotency_key"]] = known
            self.events.append(("admitted", known))
        if self.post == "lost_always" or self.post == "lost_once":
            if self.post == "lost_once":
                self.post = "ok"
            self._lost(at, timeout)
        return self._cost(at, (202, {"run_id": known, "status": "started"}))

    def _status(self, run_id, timeout, at):
        run = self.runs.get(run_id)
        mode = self.get
        if self.get_failures is not None:
            if self.get_failures <= 0:
                mode = "ok"
            else:
                self.get_failures -= 1
        if mode == "timeout":
            self._lost(at, timeout)
        answers = {"503": (503, {"error": {"message": "busy"}}), "malformed": (200, None),
                   "strange": (200, {"run_id": run_id, "status": "paused_forever"}),
                   "401": (401, {"error": {"message": "invalid API key"}}),
                   "shape": (200, {"run_id": run_id, "status": self.shape})}
        if mode in answers:
            return self._cost(at, answers[mode])
        if run is None:
            return self._cost(at, (404, {"error": {"message": "not found"}}))
        if run["stopped"]:
            self.events.append(("settled", run_id))
            return self._cost(at, (200, {"run_id": run_id, "status": "cancelled"}))
        return self._cost(at, (200, {"run_id": run_id, "status": self.state, "output": self.reply}))

    def _stop(self, run_id, timeout, at):
        self.events.append(("stop", run_id))
        if self.stop == "timeout":
            if self.stop_together:   # hang together, like unresponsive endpoints do, then time out
                with self.lock:
                    self.lock.wait_for(lambda: len(self.stops()) >= self.stop_together, timeout=1.0)
            self._lost(at, timeout)
        if self.stop == "refuse":
            return self._cost(at, (409, {"error": {"message": "Run is not active in this gateway process"}}))
        if self.stop == "shape":
            if self.stop_failures is None or self.stop_failures > 0:
                if self.stop_failures is not None:
                    self.stop_failures -= 1
                return self._cost(at, (200, {"run_id": run_id, "status": self.stop_shape}))
        if self.stop in ("ok", "shape"):
            self.runs[run_id]["stopped"] = True
            return self._cost(at, (200, {"run_id": run_id, "status": "stopping"}))
        if self.stop == "ok":
            self.runs[run_id]["stopped"] = True
        return self._cost(at, (200, {"run_id": run_id, "status": "stopping"}))


class OwnershipCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        clock_patch = mock.patch.object(chat_bridge, "time",
                                        types.SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.api = FakeRuns(self.clock)
        self.key = API_KEY
        self.bridge = self.make_bridge()

    def make_bridge(self, timeout=100.0):
        bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: self.key, timeout=timeout,
                                        poll_interval=0.001)
        bridge._call = self.api   # the one seam: the endpoint's request to the API server
        self.addCleanup(bridge.close, 0.0)
        return bridge

    def ask(self, session=SESSION, bridge=None):
        try:
            reply = (bridge or self.bridge).chat({"message": "Bold row 1", "session_id": session, "document": None})
        except chat_bridge.ChatError as exc:
            return exc.status, str(exc)
        return 200, reply["reply"]

    def ask_in_background(self, session=SESSION, bridge=None):
        results = []
        thread = threading.Thread(target=lambda: results.append(self.ask(session, bridge)), daemon=True)
        thread.start()
        return thread, results

    def wait_for(self, predicate, seconds=10.0):
        with self.api.lock:
            self.assertTrue(self.api.lock.wait_for(predicate, timeout=seconds), "the endpoint never got there")

    def assert_uncertain(self, result):
        self.assertEqual(result[0], 502, result)
        self.assertIn("not known", result[1])
        self.assertIn("do not resend edits blindly", result[1])


class LostCreationTests(OwnershipCase):
    def test_a_lost_creation_answer_is_reconciled_with_the_same_key_not_resent(self):
        self.api.post, self.api.state = "lost_once", "completed"
        self.assertEqual(self.ask(), (200, "done"))
        creations = self.api.creations()
        self.assertEqual(len(self.api.runs), 1, "the request must reach Hermes as ONE run")
        self.assertEqual(len(creations), 2)
        keys = {call["idempotency_key"] for call in creations}
        self.assertEqual(len(keys), 1)
        self.assertIsNotNone(keys.pop(), "run creation must carry an Idempotency-Key")

    def test_a_creation_that_stays_unanswered_keeps_its_session_and_slot(self):
        self.api.post = "lost_always"
        first = self.ask()
        self.assertEqual(len(self.api.runs), 1)
        second = self.ask()
        self.assertEqual(len(self.api.runs), 1, "the same chat is held; nothing is resent")
        self.assertEqual(second[0], 409)
        self.assertTrue(all(call["idempotency_key"] for call in self.api.creations()),
                        "no unkeyed resend of a creation that may have been admitted")
        self.assertEqual(self.bridge.close(BUDGET), 1)   # without a run id it cannot be stopped: not drained
        self.assert_uncertain(first)


class PollFailureTests(OwnershipCase):
    MODES = ("timeout", "503", "malformed", "strange")

    def test_a_poll_failure_stops_the_run_with_its_key_and_waits_until_it_has_settled(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.get_failures = mode, 1   # one unreadable answer, then the truth
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)
                run_id = next(iter(self.api.runs))
                stops = self.api.stops()
                self.assertEqual([call["path"].rsplit("/", 2)[-2] for call in stops], [run_id],
                                 "the run must be asked to stop")
                self.assertEqual(stops[0]["key"], API_KEY)
                self.assertIn(("settled", run_id), self.api.events)
                self.assert_uncertain(result)
                self.api.state = "completed"
                self.assertEqual(self.ask(bridge=bridge), (200, "done"))   # settled: the chat is free again
                self.assertEqual(bridge.close(BUDGET), 0)

    def test_a_run_whose_state_stays_unreadable_stays_owned_and_is_not_drained(self):
        for mode in ("timeout", "503", "malformed"):
            with self.subTest(mode=mode):
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.stop = mode, "unconfirmed"
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)
                self.assertEqual(len(self.api.stops()), 1, "the run must be asked to stop")
                posts = len(self.api.creations())
                again = self.ask(bridge=bridge)
                self.assertEqual(len(self.api.creations()), posts, "nothing may be resubmitted")
                self.assertEqual(again[0], 409)   # session still owned
                start = self.clock.monotonic()
                self.assertEqual(bridge.close(BUDGET), 1)
                self.assertLessEqual(self.clock.monotonic() - start, BUDGET + 1.0)
                self.assert_uncertain(result)

    def test_unresolved_runs_keep_their_capacity(self):
        self.api.get, self.api.stop = "503", "unconfirmed"
        sessions = [f"lo-{index:032x}" for index in range(chat_bridge.MAX_ACTIVE)]
        results = [self.ask(session) for session in sessions]
        posts = len(self.api.creations())
        extra = self.ask("lo-" + "f" * 32)
        self.assertEqual(len(self.api.creations()), posts, "an unresolved run keeps its slot")
        self.assertEqual(extra[0], 429)
        for result in results:
            self.assert_uncertain(result)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class MalformedStatusTests(OwnershipCase):
    """A status field Hermes never sends (any JSON type, or an unknown word) is a lost
    observation on a poll and no acknowledgement on a stop: never an exception."""

    SHAPES = ([], {}, None, 7, 1.5, ["running"], {"status": "running"}, "paused_forever")

    def capture_logs(self):
        handler = LogCapture()
        chat_bridge.log.addHandler(handler)
        self.addCleanup(chat_bridge.log.removeHandler, handler)
        return handler

    def assert_clean_logs(self, handler):
        for line in handler.lines:
            self.assertNotIn(API_KEY, line)
            self.assertNotIn("TypeError", line)

    def test_a_poll_with_a_malformed_status_stops_the_run_and_reports_uncertainty(self):
        for shape in self.SHAPES:
            with self.subTest(status=shape):
                logs = self.capture_logs()
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.get_failures, self.api.shape = "shape", 1, shape
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)   # a non-ChatError exception would escape here
                run_id = next(iter(self.api.runs))
                stops = self.api.stops()
                self.assertEqual([call["path"].rsplit("/", 2)[-2] for call in stops], [run_id],
                                 "the run must be asked to stop")
                self.assertEqual({call["key"] for call in stops}, {API_KEY})
                self.assertIn(("settled", run_id), self.api.events)
                self.assert_uncertain(result)
                self.assertEqual(bridge.owned_runs(), 0, "released once it was seen settled")
                self.assertEqual(bridge.close(BUDGET), 0)
                self.assert_clean_logs(logs)

    def test_a_poll_status_that_stays_malformed_keeps_the_run_owned(self):
        for shape in self.SHAPES:
            with self.subTest(status=shape):
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.shape, self.api.stop = "shape", shape, "unconfirmed"
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)
                self.assertEqual(len(self.api.stops()), 1)
                self.assertEqual(self.ask(bridge=bridge)[0], 409)
                self.assertEqual(len(self.api.creations()), 1, "nothing may be resubmitted")
                self.assertEqual(bridge.close(BUDGET), 1)
                self.assert_uncertain(result)

    def test_a_stop_answer_with_a_malformed_status_is_not_an_acknowledgement(self):
        for shape in self.SHAPES:
            with self.subTest(status=shape):
                logs = self.capture_logs()
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.get_failures = "503", 1           # one lost poll: stop
                self.api.stop, self.api.stop_shape, self.api.stop_failures = "shape", shape, 1
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)
                stops = self.api.stops()
                self.assertEqual(len(stops), 2, "a malformed stop answer is asked again")
                self.assertEqual({call["key"] for call in stops}, {API_KEY})
                self.assert_uncertain(result)
                self.assertEqual(bridge.owned_runs(), 0)
                self.assertEqual(bridge.close(BUDGET), 0)
                self.assert_clean_logs(logs)

    def test_a_stop_that_is_only_ever_answered_malformed_stays_unresolved_and_retried_at_unload(self):
        for shape in self.SHAPES:
            with self.subTest(status=shape):
                self.api = FakeRuns(self.clock)
                self.api.get, self.api.stop, self.api.stop_shape = "503", "shape", shape
                bridge = self.make_bridge()
                result = self.ask(bridge=bridge)
                before = len(self.api.stops())
                self.assertGreaterEqual(before, 1)
                self.assertEqual(bridge.close(BUDGET), 1, "an unacknowledged stop is not drained")
                self.assertGreater(len(self.api.stops()), before, "unload asks again")
                self.assert_uncertain(result)


class RevokedKeyTests(OwnershipCase):
    def test_a_revoked_key_leaves_the_run_unresolved_and_a_newer_key_never_touches_it(self):
        thread, results = self.ask_in_background()
        self.wait_for(lambda: self.api.made("GET"))
        self.api.revoked.add(API_KEY)    # the user rotates API_SERVER_KEY while the run is going
        self.key = NEW_KEY
        thread.join(10)
        run_id = next(iter(self.api.runs))
        for_run = [call for call in self.api.calls if run_id in call["path"]]
        self.assertTrue(self.api.stops(), "a stop must be attempted")
        self.assertEqual({call["key"] for call in for_run}, {API_KEY})
        again = self.ask()
        self.assertEqual(len(self.api.creations()), 1, "no resubmission with the new key")
        self.assertEqual(again[0], 409)
        self.assertEqual({call["key"] for call in self.api.calls if run_id in call["path"]}, {API_KEY})
        self.assertEqual(self.bridge.close(BUDGET), 1)
        self.assert_uncertain(results[0])


class StopFailureTests(OwnershipCase):
    def test_a_refused_or_unanswered_stop_is_never_reported_as_drained(self):
        for mode in ("refuse", "timeout"):
            with self.subTest(stop=mode):
                self.api = FakeRuns(self.clock)
                self.api.stop, self.api.cost = mode, 1.0
                bridge = self.make_bridge(timeout=5.0)   # the turn runs out, so a stop is requested
                result = self.ask(bridge=bridge)
                self.assertTrue(self.api.stops())
                stops_before = len(self.api.stops())
                start = self.clock.monotonic()
                self.assertEqual(bridge.close(BUDGET), 1, "a run not seen to stop is not drained")
                self.assertLessEqual(self.clock.monotonic() - start, BUDGET + 1.0)
                if mode == "refuse":
                    self.assertGreater(len(self.api.stops()), stops_before, "unload asks again")
                self.assertIn(result[0], (502, 504), result)
                self.assertIn("not known", result[1])


class UnloadTests(OwnershipCase):
    def test_a_run_id_that_arrives_during_unload_is_stopped_and_not_reported_drained(self):
        self.api.post_gate, self.api.stop, self.api.cost = threading.Event(), "refuse", 1.0
        thread, results = self.ask_in_background()
        self.wait_for(lambda: self.api.creations())
        closing = []
        closer = threading.Thread(target=lambda: closing.append(self.bridge.close(BUDGET)), daemon=True)
        closer.start()
        for _ in range(500):
            if self.bridge.closed:
                break
            real_time.sleep(0.01)
        self.api.post_gate.set()                    # the creation answer arrives after unload began
        closer.join(15)
        thread.join(15)
        run_id = next(iter(self.api.runs))
        self.assertIn(("stop", run_id), self.api.events)
        self.assertEqual(closing, [1])
        self.assertEqual(results[0][0], 503)

    def _call_outlives_the_budget(self, which):
        """A status poll (``get``) or the creation request (``post``) is
        already in flight when close() begins and answers 12 s later, inside its own 30 s call
        timeout but after the 10 s close budget. The run must still be asked to stop."""
        self.api.stop, self.api.cost = "unconfirmed", 0.5
        gate, began = threading.Event(), threading.Event()
        if which == "post":
            self.api.post_gate = gate
        else:
            self.api.get_gate, self.api.get_after_gate = gate, "running"
        thread, results = self.ask_in_background()
        self.wait_for(lambda: self.api.creations() if which == "post" else self.api.get_held)
        start = self.clock.monotonic()
        closing = []

        def unload():
            began.set()
            closing.append(self.bridge.close(BUDGET))
        closer = threading.Thread(target=unload, daemon=True)
        closer.start()
        began.wait(5)
        for _ in range(500):
            if self.bridge.closed:
                break
            real_time.sleep(0.01)
        with self.api.lock:   # give close() a moment to send its stop, then let the budget run out
            self.api.lock.wait_for(lambda: self.api.stops(), timeout=1.0)
        self.clock.advance_to(start + 12.0)
        with self.bridge._lock:
            self.bridge._lock.notify_all()
        closer.join(15)
        self.assertEqual(closing, [1], "a run not seen to stop is not drained")
        self.clock.advance_to(start + 12.0)
        gate.set()                                # the in-flight call answers after the budget
        thread.join(15)
        stops = self.api.stops()
        self.assertTrue(stops, "the run must be asked to stop although a call outlived the budget")
        self.assertEqual({call["key"] for call in stops}, {API_KEY})
        self.assertEqual(len(self.api.runs), 1)
        self.assertEqual(self.bridge.owned_runs(), 1, "still tracked")
        self.assertEqual(results[0][0], 503)
        self.assertIn("not known", results[0][1])
        return stops, start

    def test_a_poll_in_flight_past_the_budget_still_gets_a_stop_within_it(self):
        stops, start = self._call_outlives_the_budget("get")
        self.assertLessEqual(stops[0]["at"] - start, BUDGET, "sent while close() still waited")

    def test_a_creation_in_flight_past_the_budget_gets_one_late_stop(self):
        stops, start = self._call_outlives_the_budget("post")
        self.assertEqual(len(stops), 1, "one best-effort stop once the id is known")
        self.assertLessEqual(stops[0]["timeout"], chat_bridge.LATE_STOP_SECONDS)

    def _start_four(self):
        self.api.stop, self.api.cost, self.api.stop_together = "timeout", 0.0, chat_bridge.MAX_ACTIVE
        threads = []
        for index in range(chat_bridge.MAX_ACTIVE):
            threads.append(self.ask_in_background(f"lo-{index:032x}")[0])
        self.wait_for(lambda: len({c["path"] for c in self.api.made("GET")}) == chat_bridge.MAX_ACTIVE)
        return threads

    def _assert_one_budget(self, start):
        stops = self.api.stops()
        self.assertEqual(len({call["path"] for call in stops}), chat_bridge.MAX_ACTIVE, "every run gets a stop")
        for call in stops:
            self.assertLessEqual(call["at"] - start + call["timeout"], BUDGET + 0.01,
                                 "a stop may only use what is left of the budget")
        self.assertLessEqual(self.clock.monotonic() - start, BUDGET + 0.01)

    def test_close_spends_one_budget_however_many_stops_hang(self):
        threads = self._start_four()
        start = self.clock.monotonic()
        self.assertEqual(self.bridge.close(BUDGET), chat_bridge.MAX_ACTIVE)
        self._assert_one_budget(start)
        for thread in threads:
            thread.join(10)

    def test_close_on_the_event_loop_thread_reports_runs_unconfirmed_and_keeps_the_budget(self):
        threads = self._start_four()
        start = self.clock.monotonic()

        async def unload():
            return self.bridge.close(BUDGET)
        began = real_time.monotonic()
        self.assertEqual(asyncio.run(unload()), chat_bridge.MAX_ACTIVE)
        self.assertLess(real_time.monotonic() - began, 2.0, "the event loop must not be blocked")
        self.wait_for(lambda: len(self.api.stops()) >= chat_bridge.MAX_ACTIVE)
        for thread in threads:
            thread.join(10)
        self._assert_one_budget(start)


REAL_CLOCK = types.SimpleNamespace(monotonic=real_time.monotonic, advance_to=lambda _when: None)


class BlockedCallUnloadTests(unittest.TestCase):
    """close() while the chat thread is blocked in a call close() cannot shorten: a status poll
    held behind an event, or a creation whose answer arrives after the budget. Real clock,
    short budget, scripted ``_call``."""

    BUDGET = 1.0

    def setUp(self):
        self.events = []
        self.api = FakeRuns(REAL_CLOCK, self.events)
        self.api.cost = 0.0
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                             timeout=100.0, poll_interval=0.01)
        self.bridge._call = self.api
        self.releases = []
        release = self.bridge._release

        def counted(run):
            with self.bridge._lock:
                self.releases.append(self.bridge._runs.get(run.session_id) is run)
            release(run)
        self.bridge._release = counted
        self.addCleanup(self.bridge.close, 0.0)
        self.closed = []
        self.at_stop = []
        self.api.on_stop = lambda: self.at_stop.append(
            {"gate_held": self.api.get_gate is not None and not self.api.get_gate.is_set(),
             "close_returned": bool(self.closed), "fence": not self.closed})
        self.addCleanup(lambda: self.api.get_gate and self.api.get_gate.set())
        self.addCleanup(lambda: self.api.post_gate and self.api.post_gate.set())

    def chat(self):
        results = []

        def ask():
            try:
                self.bridge.chat({"message": "Bold row 1", "session_id": SESSION, "document": None})
                results.append((200, ""))
            except chat_bridge.ChatError as exc:
                results.append((exc.status, str(exc)))
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                results.append((0, type(exc).__name__))
        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        return thread, results

    def unload(self):
        began = real_time.monotonic()
        closer = threading.Thread(target=lambda: self.closed.append(self.bridge.close(self.BUDGET)), daemon=True)
        closer.start()
        closer.join(self.BUDGET + 10)
        self.assertFalse(closer.is_alive(), "close() must return")
        return real_time.monotonic() - began

    def hold_the_poll(self, after):
        self.api.get_gate, self.api.get_after_gate = threading.Event(), after
        thread, results = self.chat()
        with self.api.lock:
            self.assertTrue(self.api.lock.wait_for(lambda: self.api.get_held, timeout=10))
        return thread, results

    def assert_stop_under_close(self, run_id):
        stops = self.api.stops()
        self.assertTrue(stops, "close() must ask the run to stop")
        self.assertEqual({call["path"].rsplit("/", 2)[-2] for call in stops}, {run_id})
        self.assertEqual({call["key"] for call in stops}, {API_KEY}, "only the run's own key")
        first = self.at_stop[0]
        self.assertTrue(first["gate_held"], "the stop must not wait for the blocked poll")
        self.assertFalse(first["close_returned"], "the stop must arrive before close() returns")
        self.assertTrue(first["fence"])
        for call in stops:
            self.assertLessEqual(call["timeout"], self.BUDGET + 0.01)

    def test_a_poll_blocked_at_unload_does_not_keep_the_run_from_being_stopped(self):
        thread, results = self.hold_the_poll("running")
        elapsed = self.unload()
        run_id = next(iter(self.api.runs))
        self.assert_stop_under_close(run_id)
        self.assertEqual(self.closed, [0], "the stop was confirmed: the run settled")
        self.assertLess(elapsed, self.BUDGET + 0.5)
        self.api.get_gate.set()
        thread.join(10)
        self.assertEqual(results[0][0], 503)
        self.assertEqual(self.bridge.owned_runs(), 0)
        self.assertEqual(self.releases.count(True), 1, "released exactly once")
        self.assertEqual(len(self.api.creations()), 1, "no new admission")

    def test_an_auth_refusal_at_unload_leaves_the_blocked_run_counted_unresolved(self):
        thread, results = self.hold_the_poll("401")
        self.api.revoked.add(API_KEY)   # the key stops working while the poll is held
        elapsed = self.unload()
        run_id = next(iter(self.api.runs))
        self.assert_stop_under_close(run_id)
        self.assertEqual(self.closed, [1], "a refused stop is not drained")
        self.assertLess(elapsed, self.BUDGET + 0.5)
        self.api.get_gate.set()
        thread.join(10)
        self.assertEqual(results[0][0], 503)
        self.assertIn("not known", results[0][1])
        self.assertEqual(self.bridge.owned_runs(), 1)
        self.assertNotIn(True, self.releases)
        self.assertEqual(len(self.api.creations()), 1)

    def test_a_malformed_answer_at_unload_leaves_the_blocked_run_counted_unresolved(self):
        self.api.shape, self.api.stop, self.api.stop_shape, self.api.get = [], "shape", {}, "shape"
        thread, results = self.hold_the_poll("shape")
        elapsed = self.unload()
        run_id = next(iter(self.api.runs))
        self.assert_stop_under_close(run_id)
        self.assertGreaterEqual(len(self.api.stops()), 2, "an unacknowledged stop is asked again")
        self.assertEqual(self.closed, [1])
        self.assertLess(elapsed, self.BUDGET + 0.5)
        self.api.get_gate.set()
        thread.join(10)
        self.assertEqual(results[0][0], 503, results)
        self.assertIn("not known", results[0][1])
        self.assertEqual(self.bridge.owned_runs(), 1)
        self.assertNotIn(True, self.releases)

    def test_a_creation_answer_after_the_budget_keeps_ownership_and_gets_a_late_stop(self):
        self.api.post_gate, self.api.stop = threading.Event(), "unconfirmed"
        thread, results = self.chat()
        with self.api.lock:
            self.assertTrue(self.api.lock.wait_for(lambda: self.api.creations(), timeout=10))
        logs = LogCapture()
        chat_bridge.log.addHandler(logs)
        self.addCleanup(chat_bridge.log.removeHandler, logs)
        elapsed = self.unload()
        self.assertEqual(self.closed, [1], "an unanswered creation is counted unresolved")
        self.assertLess(elapsed, self.BUDGET + 0.5)
        self.assertEqual(self.api.stops(), [], "no id yet: nothing could be stopped under close()")
        self.api.post_gate.set()   # the answer arrives after close() returned
        thread.join(10)
        run_id = next(iter(self.api.runs))
        stops = self.api.stops()
        self.assertEqual([call["path"].rsplit("/", 2)[-2] for call in stops], [run_id], "best-effort late stop")
        self.assertEqual(stops[0]["key"], API_KEY)
        self.assertTrue(self.at_stop[0]["close_returned"], "this stop is not made under the fence")
        self.assertEqual(self.bridge.owned_runs(), 1, "not seen settled: still owned")
        self.assertEqual(results[0][0], 503)
        self.assertIn("not known", results[0][1])
        self.assertTrue(any("after the endpoint closed" in line for line in logs.lines))
        self.assertEqual(len(self.api.creations()), 1)


class PollCutOffTests(unittest.TestCase):
    """A real socket: a status poll that began before close() is ended when close() returns,
    instead of holding its thread for the 30-second call timeout chosen before close()."""

    def test_a_poll_started_before_close_ends_when_close_returns(self):
        import http.server
        import json
        release, polled = threading.Event(), threading.Event()

        class Api(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return None

            def answer(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.endswith("/stop"):
                    self.answer(200, {"run_id": "run_1", "status": "cancelled"})
                else:
                    self.answer(202, {"run_id": "run_1", "status": "started"})

            def do_GET(self):
                polled.set()
                release.wait(30)   # never answers while the test runs
                self.answer(200, {"run_id": "run_1", "status": "running"})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        bridge = chat_bridge.ChatBridge(port=0, token=lambda: "t" * 44, api_key=lambda: API_KEY,
                                        api_port=server.server_address[1], timeout=100.0, poll_interval=0.01)
        results = []

        def ask():
            try:
                bridge.chat({"message": "Bold row 1", "session_id": SESSION, "document": None})
            except chat_bridge.ChatError as exc:
                results.append(exc.status)
        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        self.assertTrue(polled.wait(10))
        real_time.sleep(0.05)
        self.assertEqual(bridge.close(1.0), 0, "the stop was answered settled")
        thread.join(3.0)
        self.assertFalse(thread.is_alive(), "the poll must not outlive close() by its 30-second timeout")
        self.assertEqual(results, [503])


class PluginUnloadTests(unittest.TestCase):
    """Through the plugin's own unload cleanup, in Hermes's disposal order (see test_chat_lifecycle).

    The contract tested here is the one the documentation states (an accepted 0.4.0 limitation):
    unload asks every known run to stop before close() returns, counts and logs the runs it could
    not confirm stopped, and Hermes then removes the tool fence anyway, possibly before the close
    budget has run out. The fence is NOT kept until those runs settle; a plugin cannot do that.
    """

    def load(self, events, close_seconds=0.6):
        from test_chat_lifecycle import LedgerContext, SecretStore, free_port
        from test_chat_bridge import TOKEN
        store = SecretStore({"LIBREOFFICE_LIVE_BRIDGE_TOKEN": TOKEN, "API_SERVER_KEY": API_KEY})
        for patch in (store.patch, mock.patch.object(plugin, "_in_gateway_process", return_value=True),
                      mock.patch.object(chat_bridge, "RECONCILE_SECONDS", 0.3, create=True),
                      mock.patch.object(chat_bridge, "CLOSE_SECONDS", close_seconds, create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        ctx = LedgerContext({"chat_port": free_port(), "api_server_port": 9}, events)
        plugin.register(ctx)
        self.addCleanup(ctx.unload)
        bridge = plugin._CHAT
        api = FakeRuns(REAL_CLOCK, events)
        api.cost = 0.0
        bridge._call, bridge.poll_interval = api, 0.01
        reported = []
        close = bridge.close

        def recorded(*args):
            reported.append(close(*args))
            events.append(("close_returned", reported[-1]))
            return reported[-1]
        bridge.close = recorded
        return ctx, bridge, api, reported

    def assert_disclosed_hook_removal(self, events, logs):
        kinds = [kind for kind, _name in events]
        self.assertLess(kinds.index("close_returned"), kinds.index("hook_removed"))
        self.assertIn(("hook_removed", "pre_tool_call"), events, "Hermes removes the fence regardless")
        self.assertTrue(any("not confirmed stopped" in line for line in logs.output), "unresolved runs are counted")
        self.assertTrue(any("removes the panel tool fence" in line for line in logs.output),
                        "the log says the fence goes while runs are unresolved")

    def test_unload_asks_an_unresolved_run_to_stop_before_close_returns_and_logs_the_fence_removal(self):
        events = []
        ctx, bridge, api, reported = self.load(events)
        api.get, api.stop = "503", "unconfirmed"
        answer = ""
        try:
            bridge.chat({"message": "Bold row 1", "session_id": SESSION, "document": None})
        except chat_bridge.ChatError as exc:
            answer = str(exc)
        with self.assertLogs(level=logging.WARNING) as logs:
            ctx.unload()
        kinds = [kind for kind, _name in events]
        stops = [index for index, kind in enumerate(kinds) if kind == "stop"]
        self.assertGreaterEqual(len(stops), 2, "asked at the lost poll and again at unload")
        self.assertLess(stops[-1], kinds.index("close_returned"), "unload's stop comes before close() returns")
        self.assertEqual(reported, [1], "an unresolved run must not be reported as drained")
        self.assert_disclosed_hook_removal(events, logs)
        self.assertIn("not known", answer)

    def test_unload_while_a_poll_is_blocked_still_asks_the_run_to_stop_before_close_returns(self):
        events = []
        ctx, bridge, api, reported = self.load(events)
        api.get_gate, api.stop = threading.Event(), "refuse"
        self.addCleanup(api.get_gate.set)
        held = []
        api.on_stop = lambda: held.append(not api.get_gate.is_set())
        thread = threading.Thread(target=lambda: self.assertRaises(
            chat_bridge.ChatError, bridge.chat, {"message": "Bold row 1", "session_id": SESSION, "document": None}),
            daemon=True)
        thread.start()
        with api.lock:
            self.assertTrue(api.lock.wait_for(lambda: api.get_held, timeout=10))
        with self.assertLogs(level=logging.WARNING) as logs:
            ctx.unload()
        kinds = [kind for kind, _name in events]
        self.assertIn("stop", kinds, "the run must be asked to stop although its poll is blocked")
        self.assertLess(kinds.index("stop"), kinds.index("close_returned"))
        self.assertTrue(held and held[0], "the stop did not wait for the blocked poll")
        self.assertEqual(reported, [1])
        self.assert_disclosed_hook_removal(events, logs)
        api.get_gate.set()
        thread.join(10)

    def test_unload_can_return_before_its_budget_when_no_run_can_be_stopped(self):
        events = []
        ctx, bridge, api, reported = self.load(events, close_seconds=5.0)
        api.post = "lost_always"   # the creation is never answered: the run has no id to stop
        self.assertRaises(chat_bridge.ChatError, bridge.chat,
                          {"message": "Bold row 1", "session_id": SESSION, "document": None})
        began = real_time.monotonic()
        with self.assertLogs(level=logging.WARNING) as logs:
            ctx.unload()
        self.assertLess(real_time.monotonic() - began, 2.5, "nothing to wait for: the hook goes before the budget ends")
        self.assertEqual(reported, [1])
        self.assertNotIn("stop", [kind for kind, _name in events])
        self.assert_disclosed_hook_removal(events, logs)


class DisclosureTests(unittest.TestCase):
    """The accepted limitation is stated wherever the sidebar chat's risks are."""

    FILES = ("README.md", "plugin/README.md", "SECURITY.md", "CHANGELOG.md", "catalog/atlas-sidecar-libreoffice.yaml")

    def test_every_risk_disclosure_states_the_unload_limitation(self):
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        for name in self.FILES:
            text = " ".join((root / name).read_text(encoding="utf-8").split())
            with self.subTest(file=name):
                for phrase in ("as soon as the unload callback returns", "keeps running without the restriction",
                               "accepted", "per-run tool restriction"):
                    self.assertTrue(phrase in text, f"{name} must say {phrase!r}")
                for phrase in ("when the 10-second budget ends", "unload's 10-second budget ends"):
                    self.assertFalse(phrase in text, f"{name} must not say {phrase!r}")


if __name__ == "__main__":
    unittest.main()
