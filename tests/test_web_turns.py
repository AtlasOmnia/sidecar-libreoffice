# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Turns of the Sidecar window (docs/web-protocol.md 6.3-6.7, 12; plan gate G2): one worker per
run through ``ChatBridge.begin_turn``, the cursor long-poll, user Stop with an immediate wake-up,
New chat beside an unresolved run, the window-gone stop and the connection budget. A loopback
fake Runs API stands in for Hermes; no model runs."""
from __future__ import annotations

import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import (API_KEY, PAIRING_TOKEN, PageClient, WebCase, eventually,  # noqa: E402
                                   raw_request)

from plugin import chat_bridge, web_api, web_auth  # noqa: E402


def in_background(function, *args, **kwargs):
    results = []
    thread = threading.Thread(target=lambda: results.append(function(*args, **kwargs)), daemon=True)
    thread.start()
    return thread, results


class TurnTests(WebCase):
    def bridge_options(self):
        return {"long_poll_seconds": 0.6}

    def test_a_reply_arrives_whole_and_the_text_suffix_follows_have(self):
        page = self.page()
        turn = self.start_turn(page)
        self.api.finish.set()
        done = page.wait_done(turn)
        reply = self.api.reply
        self.assertEqual((done["text"], done["text_length"], done["outcome"]),
                         (reply, len(reply), {"kind": "completed", "message": None}))
        self.assertEqual((done["activity"], done["hermes_state"]), ([], None))
        status, value = page.turn(turn, done["seq"], 5)
        self.assertEqual((status, value["turn"]["text_from"], value["turn"]["text"]), (200, 5, reply[5:]))
        status, value = page.turn(turn, 0, 10 ** 5)
        self.assertEqual((value["turn"]["text_from"], value["turn"]["text"]), (len(reply), ""))
        sent = self.api.creations()[0][2]
        self.assertTrue(sent["input"].endswith("\n\nAdd a total row"))
        self.assertIn("document_id lo:5f0c:1", sent["input"])
        self.assertRegex(sent["session_id"], chat_bridge.SESSION_RE.pattern)
        self.assertEqual(page.state()[1]["chat_label"], sent["session_id"][:11])

    def test_the_outcome_is_kept_until_the_next_message_or_new_chat(self):
        page = self.page()
        self.api.finish.set()
        turn = self.start_turn(page)
        self.assertTrue(eventually(lambda: page.state()[1]["turn"]["state"] == "done"))
        state = page.state()[1]["turn"]
        self.assertNotIn("text", state)
        self.assertEqual(state["outcome"]["kind"], "completed")
        status, value = page.turn(turn)
        self.assertEqual((status, value["turn"]["text"]), (200, self.api.reply), "fetched after it ended")
        second = self.start_turn(page)
        self.assertEqual(page.turn(turn)[1]["code"], "unknown_turn", "a new message replaces the record")
        page.wait_done(second)
        self.assertEqual(page.new_chat()[0], 200)
        self.assertIsNone(page.state()[1]["turn"])
        self.assertEqual(page.turn(second)[1]["code"], "unknown_turn")

    def test_the_long_poll_parks_then_answers_the_unchanged_record(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        _status, first = page.turn(turn, 0)
        seq = first["turn"]["seq"]
        self.assertEqual(first["turn"]["hermes_state"], "running")
        started = time.monotonic()
        status, value = page.turn(turn, seq)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual((status, value["superseded"], value["turn"]["seq"], value["turn"]["state"]),
                         (200, False, seq, "working"))

    def test_a_newer_long_poll_supersedes_the_older_one(self):
        self.web.long_poll_seconds = 10.0
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        seq = page.turn(turn, 0)[1]["turn"]["seq"]
        thread, results = in_background(page.turn, turn, seq)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        started = time.monotonic()
        newer = in_background(page.turn, turn, seq)
        thread.join(5)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual((results[0][0], results[0][1]["superseded"]), (200, True))
        self.api.finish.set()
        newer[0].join(10)
        self.assertEqual(newer[1][0][1]["turn"]["state"], "done")

    def test_waiting_for_approval_is_reported(self):
        self.api.live_state = "waiting_for_approval"
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(eventually(lambda: page.turn(turn, 0)[1]["turn"]["hermes_state"] == "waiting_for_approval"))

    def test_an_error_outcome_carries_the_user_safe_text(self):
        self.api.live_state = "failed"
        page = self.page()
        done = page.wait_done(self.start_turn(page))
        self.assertEqual(done["outcome"], {"kind": "error",
                                           "message": "Hermes could not finish this reply; see the Hermes logs"})
        self.assertEqual(done["text"], "")

    def test_turn_queries_are_strict(self):
        page = self.page()
        turn = self.start_turn(page)
        for query in (f"id={turn}&seq=0", f"id={turn}&seq=0&have=0&x=1", f"id={turn}&id={turn}&seq=0&have=0",
                      f"id={turn}&seq=-1&have=0", f"id={turn}&seq=200001&have=0", f"id={turn}&seq=1.0&have=0",
                      "id=short&seq=0&have=0", f"id={turn}&seq=0&have=", f"id={turn}&seq=01&have=0",
                      f"id={turn}&seq=0&have=00", ""):
            status, value = page.request("GET", f"/api/turn?{query}")
            self.assertEqual((status, value["code"]), (400, "bad_request"), query)
        self.assertEqual(page.turn("A" * 22)[1]["code"], "unknown_turn")
        other = self.page(document_id="other")
        self.assertEqual(other.turn(turn)[1]["code"], "unknown_turn", "a turn belongs to its session")

    def test_messages_are_checked(self):
        page = self.page()
        for message in ("", "   \n", "x" * (chat_bridge.MAX_MESSAGE + 1), ["hi"], None):
            status, value = page.request("POST", "/api/chat", {"message": message})
            self.assertEqual((status, value["code"]), (400, "message_invalid"), repr(message)[:20])
        self.assertEqual(self.api.creations(), [])

    def test_busy_and_the_shared_claim_with_the_legacy_route(self):
        page = self.page()
        self.start_turn(page)
        status, value = page.chat("again")
        self.assertEqual((status, value["code"]), (409, "busy"))
        pages = [self.page(document_id=f"d{index}") for index in range(chat_bridge.MAX_ACTIVE - 2)]
        for other in pages:
            self.start_turn(other)
        legacy_body = json.dumps({"message": "hi", "session_id": "lo-" + "e" * 32}).encode()
        legacy_headers = [("Host", f"127.0.0.1:{self.port}"), (chat_bridge.TOKEN_HEADER, PAIRING_TOKEN),
                          ("Content-Type", "application/json")]
        legacy = in_background(raw_request, self.port, "POST", "/chat", legacy_body, legacy_headers)
        self.assertTrue(eventually(lambda: self.bridge.owned_runs() == chat_bridge.MAX_ACTIVE))
        late = self.page(document_id="late")
        status, value = late.chat()
        self.assertEqual((status, value["code"]), (429, "too_many"))
        other_legacy = json.dumps({"message": "hi", "session_id": "lo-" + "f" * 32}).encode()
        self.assertEqual(raw_request(self.port, "POST", "/chat", other_legacy, legacy_headers)[0], 429)
        self.api.finish.set()
        legacy[0].join(10)
        self.assertEqual(legacy[1][0][0], 200)

    def test_readiness_and_an_inactive_plugin(self):
        page = self.page()
        self.bridge.fence_active = lambda: False
        self.assertEqual(page.state()[1]["chat"], {"ready": False, "reason": "plugin_inactive"})
        status, value = page.chat()
        self.assertEqual((status, value["code"]), (503, "plugin_inactive"))
        self.bridge.fence_active = lambda: True
        self.bridge.api_key = lambda: None
        self.assertEqual(page.state()[1]["chat"]["reason"], "no_api_key")
        self.bridge.api_key = lambda: "bad key\n"
        self.assertEqual(page.state()[1]["chat"]["reason"], "bad_api_key")
        self.bridge.api_key = lambda: API_KEY
        self.assertEqual(page.state()[1]["chat"], {"ready": True, "reason": None})

    def test_a_worker_that_cannot_start_releases_the_claim_and_sends_nothing(self):
        real = chat_bridge.threading

        class Thread(real.Thread):
            def start(self):
                if self.name == "HermesLibreOfficeChatTurn":
                    raise RuntimeError("can't start new thread")
                return super().start()
        proxy = types.SimpleNamespace(**{name: getattr(real, name) for name in dir(real) if not name.startswith("__")})
        proxy.Thread = Thread
        page = self.page()
        with mock.patch.object(chat_bridge, "threading", proxy):
            status, value = page.chat()
        self.assertEqual((status, value["code"], value["error"]), (503, "worker_failed", chat_bridge.TURN_WORKER_FAILED))
        self.assertEqual((self.bridge.owned_runs(), self.api.creations()), (0, []))
        self.assertIsNone(page.state()[1]["turn"])

    def test_the_browser_never_sees_a_run_id_or_the_chat_session_id(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        seen = [json.dumps(page.state()[1]), json.dumps(page.turn(turn, 0)[1])]
        self.api.finish.set()
        seen.append(json.dumps(page.wait_done(turn)))
        session_id = self.api.creations()[0][2]["session_id"]
        for text in seen:
            self.assertNotIn("run_", text)
            self.assertNotIn(session_id, text)
            self.assertNotIn(API_KEY, text)
            self.assertNotIn(PAIRING_TOKEN, text)


class StopTests(WebCase):
    def bridge_options(self):
        return {"poll_interval": 2.0, "long_poll_seconds": 5.0}

    def test_user_stop_wakes_the_worker_and_is_sent_at_once(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        time.sleep(0.3)   # the worker is now waiting for its next status poll (2 s or more away)
        polls = len(self.api.polls())
        asked = time.monotonic()
        status, value = page.stop(turn)
        self.assertEqual((status, value["turn"]["state"]), (202, "stopping"))
        self.assertTrue(self.api.wait_for(lambda: self.api.stops, 5))
        self.assertLess(self.api.stops[0][1] - asked, 0.5, "the stop does not wait for the next poll")
        self.assertEqual(len(self.api.polls()), polls, "no poll came first")
        done = page.wait_done(turn)
        self.assertEqual(done["outcome"], {"kind": "user", "message": chat_bridge.USER_STOPPED})
        self.assertEqual(self.bridge.owned_runs(), 0)
        status, value = page.stop(turn)
        self.assertEqual((status, value["turn"]["state"]), (200, "done"), "a stop after the end is the final view")
        self.assertEqual(page.stop("A" * 22)[1]["code"], "unknown_turn")

    def test_stop_while_the_run_is_being_created(self):
        self.api.create_gate = threading.Event()
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.creations()))
        self.assertEqual(page.stop(turn)[0], 202)
        self.api.create_gate.set()
        done = page.wait_done(turn)
        self.assertEqual(done["outcome"]["kind"], "user")
        self.assertEqual((len(self.api.creations()), len(self.api.stops)), (1, 1))
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_a_stop_answer_that_already_settles_the_run(self):
        self.api.stop_mode = "settle"
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        page.stop(turn)
        self.assertEqual(page.wait_done(turn)["outcome"]["kind"], "user")
        self.assertEqual(self.bridge.owned_runs(), 0)

    def test_new_chat_is_refused_while_driven(self):
        page = self.page()
        self.start_turn(page)
        status, value = page.new_chat()
        self.assertEqual((status, value["code"], value["error"]), (409, "turn_running",
                                                                    "Wait for the reply, or press Stop first."))


class UnresolvedTests(WebCase):
    def bridge_options(self):
        return {"poll_interval": 0.05, "long_poll_seconds": 5.0}

    def test_an_unconfirmed_stop_is_uncertain_and_new_chat_continues_beside_it(self):
        self.api.stop_mode = "unconfirmed"
        with mock.patch.object(chat_bridge, "RECONCILE_SECONDS", 0.5), \
                self.assertLogs(chat_bridge.log, "WARNING"):
            page = self.page()
            turn = self.start_turn(page)
            self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
            page.stop(turn)
            done = page.wait_done(turn)
        self.assertEqual(done["outcome"], {"kind": "uncertain", "message": chat_bridge.OUTCOME_UNCERTAIN})
        self.assertEqual(self.bridge.owned_runs(), 1)
        old_label = page.state()[1]["chat_label"]
        status, value = page.chat("next")
        self.assertEqual((status, value["code"]), (409, "unresolved"))
        self.assertTrue(value["error"].endswith("Start a New chat to continue."))
        status, value = page.new_chat()
        self.assertEqual(status, 200)
        self.assertNotEqual(value["chat_label"], old_label)
        self.api.finish.set()
        self.api.stop_mode = "ok"
        second = self.start_turn(page)
        self.assertEqual(page.wait_done(second)["outcome"]["kind"], "completed")
        self.assertEqual(self.bridge.owned_runs(), 1, "the unresolved run stays owned under the old id")


class SessionEndStopTests(WebCase):
    fake_clock = True

    def bridge_options(self):
        return {"poll_interval": 0.05, "long_poll_seconds": 0.5}

    def test_closing_the_session_stops_its_run(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        self.assertEqual(page.close()[0], 200)
        self.assertTrue(self.api.wait_for(lambda: self.api.stops, 5))
        self.assertTrue(eventually(lambda: self.bridge.owned_runs() == 0))
        self.assertIsNotNone(turn)

    def test_ninety_seconds_without_a_long_poll_stops_a_driven_turn(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        self.clock.advance(web_api.WINDOW_GONE_SECONDS - 1)
        self.web.sweep()
        self.assertEqual(self.api.stops, [])
        page.turn(turn, 0)   # a long-poll arrived: the 90 s count starts again
        self.clock.advance(web_api.WINDOW_GONE_SECONDS - 1)
        self.web.sweep()
        self.assertEqual(self.api.stops, [])
        self.clock.advance(1)
        self.web.sweep()
        self.assertTrue(self.api.wait_for(lambda: self.api.stops, 5))
        self.assertTrue(eventually(lambda: self.bridge.owned_runs() == 0))
        self.assertEqual(page.state()[1]["turn"]["outcome"]["kind"], "user")

    def test_a_long_poll_in_flight_keeps_the_turn_alive(self):
        self.web.long_poll_seconds = 3.0
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        seq = page.turn(turn, 0)[1]["turn"]["seq"]
        thread, _results = in_background(page.turn, turn, seq)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        self.clock.advance(web_api.WINDOW_GONE_SECONDS * 2)
        self.web.sweep()
        self.assertEqual(self.api.stops, [])
        self.api.finish.set()
        thread.join(5)


class BudgetTests(WebCase):
    def bridge_options(self):
        return {"poll_interval": 0.05, "long_poll_seconds": 20.0}

    def test_four_active_turns_and_eight_polling_sessions_still_serve_a_page_load_and_a_stop(self):
        pages = [self.page(document_id=f"doc{index}") for index in range(web_auth.MAX_SESSIONS)]
        turns = [self.start_turn(page) for page in pages[:chat_bridge.MAX_ACTIVE]]
        self.assertTrue(self.api.wait_for(lambda: len({c[1] for c in self.api.polls()}) >= chat_bridge.MAX_ACTIVE))
        polls = []
        for page, turn in zip(pages, turns):
            seq = page.turn(turn, 0)[1]["turn"]["seq"]
            polls.append(in_background(page.turn, turn, seq, 0, 30.0))
        self.assertTrue(eventually(lambda: self.web._parked == chat_bridge.MAX_ACTIVE))
        states = [in_background(page.state) for page in pages]
        for thread, results in states:
            thread.join(10)
            self.assertEqual(results[0][0], 200)
        started = time.monotonic()
        status, _headers, _raw = raw_request(self.port, "GET", "/ui/index.html", None,
                                             [("Host", f"127.0.0.1:{self.port}")], timeout=5)
        self.assertEqual(status, 200)
        status, value = pages[0].stop(turns[0])
        self.assertEqual((status, value["turn"]["state"]), (202, "stopping"))
        self.assertLess(time.monotonic() - started, 2.0)
        polls[0][0].join(10)
        self.assertEqual(polls[0][1][0][1]["turn"]["state"], "stopping")
        self.api.finish.set()
        for thread, _results in polls[1:]:
            thread.join(10)

    def test_no_more_than_max_active_long_polls_are_ever_parked(self):
        page = self.page()
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        seq = page.turn(turn, 0)[1]["turn"]["seq"]
        with self.web._cond:
            self.web._parked = chat_bridge.MAX_ACTIVE
        try:
            started = time.monotonic()
            status, value = page.turn(turn, seq)
            self.assertLess(time.monotonic() - started, 2.0, "answered at once instead of parking")
            self.assertEqual((status, value["turn"]["seq"]), (200, seq))
        finally:
            with self.web._cond:
                self.web._parked = 0


if __name__ == "__main__":
    unittest.main()
