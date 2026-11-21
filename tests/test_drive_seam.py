# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The seam the 0.5.0 web routes use: ``_claim`` + ``_drive``, ``begin_turn`` (one worker per
run), a user Stop that only sets ``stop_requested``, the ``user`` outcome, ``session_state`` for
New chat, and ``add_route_handler`` for routes defined in other modules.

The Runs API and the clock are the scripted fakes from ``test_run_ownership`` (imported, not
changed), so lost answers and stop races are exact and need no model or network.
"""
from __future__ import annotations

import http.client
import json
import logging
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
from test_chat_bridge import API_KEY, SESSION  # noqa: E402
from test_run_ownership import BUDGET, FakeClock, FakeRuns  # noqa: E402

from plugin import chat_bridge  # noqa: E402

OTHER = "lo-" + "b" * 32
TOKEN = "t" * 44


class Outcomes:
    """Collects ``on_done(run, outcome)`` calls from turn workers."""

    def __init__(self):
        self.lock = threading.Condition()
        self.items = []

    def __call__(self, run, outcome):
        with self.lock:
            self.items.append((run, outcome))
            self.lock.notify_all()

    def wait(self, count=1, seconds=10.0):
        with self.lock:
            assert self.lock.wait_for(lambda: len(self.items) >= count, timeout=seconds), "no outcome arrived"
            return self.items[count - 1][1]


class SeamCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        clock_patch = mock.patch.object(chat_bridge, "time",
                                        types.SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.api = FakeRuns(self.clock)
        self.key_gate = None
        self.bridge = self.make_bridge()
        self.outcomes = Outcomes()

    def make_bridge(self):
        def key():
            if self.key_gate is not None:
                self.key_gate.wait(10)
            return API_KEY
        bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=key, timeout=100.0, poll_interval=0.001)
        bridge._call = self.api
        self.addCleanup(bridge.close, 0.0)
        return bridge

    def begin(self, session=SESSION, message="Bold row 1"):
        return self.bridge.begin_turn(session, chat_bridge.compose_message(message, None), self.outcomes)

    def wait_for(self, predicate, seconds=10.0):
        with self.api.lock:
            self.assertTrue(self.api.lock.wait_for(predicate, timeout=seconds), "the endpoint never got there")


class DriveSeamTests(SeamCase):
    def test_chat_is_claim_then_drive_with_the_same_reply(self):
        self.api.state = "completed"
        with mock.patch.object(self.bridge, "_drive", wraps=self.bridge._drive) as drive:
            reply = self.bridge.chat({"message": "Bold row 1", "session_id": SESSION, "document": None})
        self.assertEqual(reply, {"reply": "done", "session_id": SESSION})
        self.assertEqual(drive.call_count, 1)
        run, content = drive.call_args[0]
        self.assertEqual(run.session_id, SESSION)
        self.assertEqual(content, chat_bridge.compose_message("Bold row 1", None))
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_turn_runs_on_one_worker_and_reports_the_reply(self):
        self.api.state = "completed"
        run = self.begin()
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.status, outcome.text), ("completed", 200, "done"))
        self.assertIs(self.outcomes.items[0][0], run)
        self.assertTrue(run.settled)
        self.assertEqual(len(self.api.creations()), 1)
        self.assertEqual(self.bridge.owned_runs(), 0)
        self.assertIsNone(self.bridge.session_state(SESSION))

    def test_begin_turn_refuses_synchronously_through_the_same_claim_as_chat(self):
        run = self.begin()                       # running until stopped
        self.wait_for(lambda: self.api.made("GET"))
        self.assertEqual(self.bridge.session_state(SESSION), "driven")
        with self.assertRaises(chat_bridge.ChatError) as busy:
            self.begin()
        self.assertEqual(busy.exception.status, 409)
        with self.assertRaises(chat_bridge.ChatError) as legacy:
            self.bridge.chat({"message": "hi", "session_id": SESSION, "document": None})
        self.assertEqual(legacy.exception.status, 409)
        others = [f"lo-{index + 1:032x}" for index in range(chat_bridge.MAX_ACTIVE - 1)]
        for session in others:
            self.begin(session)
        with self.assertRaises(chat_bridge.ChatError) as full:
            self.begin(OTHER)
        self.assertEqual(full.exception.status, 429)
        self.assertEqual(len(self.api.creations()), chat_bridge.MAX_ACTIVE)
        self.assertEqual(self.bridge.close(BUDGET), 0)
        with self.assertRaises(chat_bridge.ChatError) as closed:
            self.begin(OTHER)
        self.assertEqual(closed.exception.status, 503)
        self.assertTrue(run.settled)

    def test_a_worker_that_cannot_start_releases_the_run_and_sends_nothing(self):
        with mock.patch.object(chat_bridge.threading.Thread, "start", side_effect=RuntimeError("can't start")):
            with self.assertRaises(chat_bridge.ChatError) as failed:
                self.begin()
        self.assertEqual((failed.exception.status, str(failed.exception)), (503, chat_bridge.TURN_WORKER_FAILED))
        self.assertEqual(self.bridge.owned_runs(), 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.outcomes.items, [])

    def test_an_outcome_callback_that_fails_does_not_leave_the_run_owned(self):
        self.api.state = "completed"
        stored = threading.Event()

        def broken(_run, _outcome):
            stored.set()
            raise RuntimeError("store failed")
        with self.assertLogs(chat_bridge.log, level=logging.ERROR):
            self.bridge.begin_turn(SESSION, "x", broken)
            self.assertTrue(stored.wait(10))
            for _ in range(500):
                if self.bridge.owned_runs() == 0:
                    break
                threading.Event().wait(0.01)
        self.assertEqual(self.bridge.owned_runs(), 0)


class UserStopTests(SeamCase):
    def test_stop_while_polling_stops_the_run_with_its_key_and_reports_the_user_outcome(self):
        run = self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.assertTrue(self.bridge.request_stop(run))
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.status, outcome.text), ("user", 200, chat_bridge.USER_STOPPED))
        stops = self.api.stops()
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["key"], API_KEY)
        self.assertEqual(self.bridge.owned_runs(), 0)
        self.assertFalse(self.bridge.request_stop(run), "a settled run cannot be stopped again")

    def test_stop_whose_answer_already_settles_the_run(self):
        self.api.stop, self.api.stop_shape = "shape", "cancelled"
        run = self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.bridge.request_stop(run)
        outcome = self.outcomes.wait()
        self.assertEqual(outcome.kind, "user")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_stop_before_creation_was_sent_sends_nothing(self):
        self.key_gate = threading.Event()
        run = self.begin()
        self.bridge.request_stop(run)
        self.key_gate.set()
        outcome = self.outcomes.wait()
        self.assertEqual(outcome.kind, "user")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_stop_while_creation_is_in_flight_stops_the_run_once_its_id_is_known(self):
        self.api.post_gate = threading.Event()
        run = self.begin()
        self.wait_for(lambda: self.api.creations())
        self.bridge.request_stop(run)
        self.api.post_gate.set()
        outcome = self.outcomes.wait()
        self.assertEqual(outcome.kind, "user")
        self.assertEqual(len(self.api.creations()), 1)
        self.assertEqual(len(self.api.stops()), 1)
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_reply_that_completes_before_the_stop_lands_is_still_the_reply(self):
        self.api.stop, self.api.stop_shape = "shape", "completed"
        run = self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.bridge.request_stop(run)
        outcome = self.outcomes.wait()
        self.assertEqual(outcome.kind, "completed")

    def test_a_stop_that_cannot_be_confirmed_stays_owned_and_reports_uncertain(self):
        self.api.stop = "unconfirmed"
        run = self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.bridge.request_stop(run)
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.text), ("uncertain", chat_bridge.OUTCOME_UNCERTAIN))
        self.assertEqual(self.bridge.owned_runs(), 1)
        self.assertEqual(self.bridge.session_state(SESSION), "unresolved")

    def test_new_chat_continues_beside_an_unresolved_run(self):
        self.api.stop = "unconfirmed"
        run = self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.bridge.request_stop(run)
        self.assertEqual(self.outcomes.wait().kind, "uncertain")
        # New chat rotates the session id; the unresolved run stays owned under the old one.
        self.api.state = "completed"
        self.begin(OTHER)
        self.assertEqual(self.outcomes.wait(2).kind, "completed")
        self.assertEqual(self.bridge.session_state(SESSION), "unresolved")
        self.assertIsNone(self.bridge.session_state(OTHER))
        self.assertEqual(self.bridge.owned_runs(), 1)

    def test_unload_during_a_turn_reports_turned_off_and_drains(self):
        self.begin()
        self.wait_for(lambda: self.api.made("GET"))
        self.assertEqual(self.bridge.close(BUDGET), 0)
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.text), ("turned_off", chat_bridge.PLUGIN_TURNED_OFF))

    def test_outcome_kinds_are_derived_from_the_existing_error_strings(self):
        cases = [(chat_bridge.ChatStopped(), "user"),
                 (chat_bridge.ChatError(502, chat_bridge.OUTCOME_UNCERTAIN), "uncertain"),
                 (chat_bridge.ChatError(503, chat_bridge.PLUGIN_TURNED_OFF), "turned_off"),
                 (chat_bridge.ChatError(504, "late"), "error")]
        for error, kind in cases:
            self.assertEqual(chat_bridge.TurnOutcome.from_error(error).kind, kind)
        self.assertEqual(set(chat_bridge.TurnOutcome.KINDS), {"completed", "user", "uncertain", "turned_off", "error"})


class ImmediateStopTests(SeamCase):
    """A user Stop is sent at once from its own thread with the run's key, even while the thread
    driving the run is blocked in a status poll (critic2 B3); the driving thread still settles,
    releases (once) and reports the outcome."""

    def hold_first_poll(self):
        self.api.get_gate = threading.Event()
        run = self.begin()
        self.wait_for(lambda: self.api.get_held)
        return run

    def test_a_stop_is_sent_while_the_worker_is_blocked_in_a_status_poll(self):
        run = self.hold_first_poll()
        self.assertTrue(self.bridge.request_stop(run))
        self.wait_for(lambda: self.api.stops(), 5)
        stops = self.api.stops()
        self.assertEqual([(call["key"], call["path"].rsplit("/", 2)[-2]) for call in stops], [(API_KEY, run.run_id)])
        self.assertFalse(run.settled, "a stop answer alone never releases the run")
        self.api.get_gate.set()
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.text), ("user", chat_bridge.USER_STOPPED))
        self.assertEqual(len(self.api.stops()), 1, "the driving thread does not send a second stop")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_stop_answer_that_settles_the_run_is_settled_once_by_the_driving_thread(self):
        self.api.stop, self.api.stop_shape = "shape", "cancelled"
        with mock.patch.object(self.bridge, "_release", wraps=self.bridge._release) as release:
            run = self.hold_first_poll()
            self.bridge.request_stop(run)
            self.wait_for(lambda: self.api.stops(), 5)
            self.assertEqual(release.call_count, 0, "the stop's thread never releases a driven run")
            self.api.get_gate.set()            # the held poll still says running
            outcome = self.outcomes.wait()
        self.assertEqual(outcome.kind, "user")
        self.assertEqual([call.args[0] for call in release.call_args_list], [run], "released once")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_reply_that_completed_before_the_stop_landed_is_still_the_reply(self):
        self.api.stop, self.api.stop_shape = "shape", "completed"
        run = self.hold_first_poll()
        self.bridge.request_stop(run)
        self.wait_for(lambda: self.api.stops(), 5)
        self.api.get_gate.set()
        self.assertEqual(self.outcomes.wait().kind, "completed")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_refused_stop_is_asked_again_and_an_unconfirmed_run_stays_owned(self):
        self.api.stop = "refuse"
        run = self.hold_first_poll()
        self.bridge.request_stop(run)
        self.wait_for(lambda: self.api.stops(), 5)
        self.api.get_gate.set()
        outcome = self.outcomes.wait()
        self.assertEqual((outcome.kind, outcome.text), ("uncertain", chat_bridge.OUTCOME_UNCERTAIN))
        self.assertGreater(len(self.api.stops()), 1, "a refused stop is asked again")
        self.assertEqual({call["key"] for call in self.api.stops()}, {API_KEY})
        self.assertEqual((self.bridge.owned_runs(), self.bridge.session_state(SESSION)), (1, "unresolved"))

    def test_an_unreadable_stop_answer_is_not_an_acknowledgement(self):
        self.api.stop, self.api.stop_shape, self.api.stop_failures = "shape", "paused_forever", 1
        run = self.hold_first_poll()
        self.bridge.request_stop(run)
        self.wait_for(lambda: self.api.stops(), 5)
        self.api.get_gate.set()
        self.assertEqual(self.outcomes.wait().kind, "user")
        self.assertEqual(len(self.api.stops()), 2, "asked again by the driving thread, then confirmed")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_second_stop_while_one_is_in_flight_sends_nothing(self):
        stop_gate = threading.Event()
        self.api.on_stop = lambda: stop_gate.wait(10)
        run = self.hold_first_poll()
        self.bridge.request_stop(run)
        self.wait_for(lambda: self.api.stops(), 5)
        self.assertTrue(self.bridge.request_stop(run))
        self.assertTrue(self.bridge.request_stop(run))
        self.assertEqual(len(self.api.stops()), 1)
        stop_gate.set()
        self.api.get_gate.set()
        self.assertEqual(self.outcomes.wait().kind, "user")
        self.assertEqual(len(self.api.stops()), 1)

    def test_a_stop_asked_before_the_run_id_is_known_is_the_first_call_after_creation(self):
        self.api.post_gate = threading.Event()
        run = self.begin()
        self.wait_for(lambda: self.api.creations())
        self.bridge.request_stop(run)
        self.api.post_gate.set()
        self.assertEqual(self.outcomes.wait().kind, "user")
        kinds = [(call["method"], call["path"].rsplit("/", 1)[-1]) for call in self.api.calls]
        self.assertEqual(kinds[:2], [("POST", "runs"), ("POST", "stop")])
        self.assertEqual(len(self.api.stops()), 1)

    def test_unload_while_a_user_stop_is_in_flight_keeps_the_one_budget(self):
        stop_gate = threading.Event()
        self.api.stop, self.api.on_stop = "unconfirmed", lambda: stop_gate.wait(10)
        run = self.hold_first_poll()
        self.bridge.request_stop(run)
        self.wait_for(lambda: self.api.stops(), 5)
        before_close = len(self.api.calls)
        start = self.clock.monotonic()
        began = threading.Event()
        closing = []

        def unload():
            began.set()
            closing.append(self.bridge.close(BUDGET))
        closer = threading.Thread(target=unload, daemon=True)
        closer.start()
        began.wait(5)
        closer.join(10)
        self.assertFalse(closer.is_alive(), "close() did not wait for the user stop in flight")
        self.assertEqual(closing, [1], "a run not seen to stop is not drained")
        for call in self.api.calls[before_close:]:   # every call made under close() fits its budget
            self.assertLessEqual(call["at"] - start + call["timeout"], BUDGET + 0.01, call)
        stop_gate.set()
        self.api.get_gate.set()
        outcome = self.outcomes.wait()
        self.assertIn(outcome.kind, ("uncertain", "turned_off"))
        self.assertEqual(self.bridge.owned_runs(), 1, "still owned: never seen to settle")


class RouteHookTests(unittest.TestCase):
    def setUp(self):
        self.bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: API_KEY, api_port=9)
        self.seen = []

        def routes(request):
            self.seen.append((request.command, request.path))
            if request.path == "/ui/hello.txt" and request.command in ("GET", "HEAD"):
                request.route_label = "ui-static"
                request.respond(200, b"hello", 0.0, content_type="text/plain; charset=utf-8",
                                headers=(("Cache-Control", "no-cache"), ("X-Frame-Options", "DENY")))
                return True
            if request.path == "/api/echo" and request.command == "POST":
                request.route_label = "api-echo"
                body = request.read_body(16)
                request.respond(200, body, 0.0)
                return True
            if request.path == "/api/refuse":
                request.route_label = "api-refuse"
                raise chat_bridge.ChatError(403, "refused")
            if request.path == "/api/broken":
                raise RuntimeError("secret-" + TOKEN)
            if request.command == "OPTIONS" and request.path.startswith("/api/"):
                request.route_label = "api-method"
                request.respond(405, b'{"ok":false}', 0.0)
                return True
            return False
        self.bridge.add_route_handler(routes)
        self.port = self.bridge.start()
        self.addCleanup(self.bridge.close, 0.0)

    def request(self, method, path, body=None, headers=None):
        send = {"Host": f"127.0.0.1:{self.port}"}
        send.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=send)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_added_routes_answer_with_their_own_headers_and_log_a_fixed_label(self):
        with self.assertLogs(chat_bridge.log, level=logging.DEBUG) as captured:
            status, headers, body = self.request("GET", "/ui/hello.txt")
            for _ in range(250):
                if any("ui-static" in line for line in captured.output):
                    break
                threading.Event().wait(0.02)
        self.assertEqual((status, body), (200, b"hello"))
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual((headers["X-Frame-Options"], headers["Connection"]), ("DENY", "close"))
        text = "\n".join(captured.output)
        self.assertIn("GET ui-static -> 200", text)
        self.assertNotIn("hello.txt", text)
        status, headers, body = self.request("HEAD", "/ui/hello.txt")
        self.assertEqual((status, body, headers["Content-Length"]), (200, b"", "5"))

    def test_bodies_are_read_within_the_bounds_the_route_gives(self):
        self.assertEqual(self.request("POST", "/api/echo", b"0123456789")[::2], (200, b"0123456789"))
        status, _headers, body = self.request("POST", "/api/echo", b"x" * 17)
        self.assertEqual((status, json.loads(body)["ok"]), (413, False))

    def test_route_errors_are_json_and_never_carry_exception_text(self):
        status, _headers, body = self.request("GET", "/api/refuse")
        self.assertEqual((status, json.loads(body)), (403, {"ok": False, "error": "refused"}))
        with self.assertLogs(chat_bridge.log, level=logging.INFO) as captured:
            status, _headers, body = self.request("GET", "/api/broken")
            for _ in range(250):
                if any("-> 500" in line for line in captured.output):
                    break
                threading.Event().wait(0.02)
        self.assertEqual((status, json.loads(body)), (500, {"ok": False, "error": "the chat endpoint failed"}))
        self.assertNotIn(TOKEN, "\n".join(captured.output))

    def test_built_in_routes_and_other_methods_are_unchanged_when_no_route_answers(self):
        status, _headers, body = self.request("GET", "/health", headers={chat_bridge.TOKEN_HEADER: TOKEN})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "status": "ok"}))
        self.assertEqual(self.request("GET", "/health")[0], 401)
        self.assertEqual(self.request("GET", "/nowhere", headers={chat_bridge.TOKEN_HEADER: TOKEN})[0], 404)
        self.assertEqual(self.request("OPTIONS", "/chat")[0], 501)
        self.assertEqual(self.request("PUT", "/chat", b"{}")[0], 501)
        self.assertEqual(self.request("OPTIONS", "/api/state")[0], 405)
        self.assertIn(("GET", "/health"), self.seen, "added routes see every request first")


if __name__ == "__main__":
    unittest.main()
