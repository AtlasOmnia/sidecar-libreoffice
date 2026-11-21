# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Admission of one Send (docs/web-protocol.md 6.3): a ``send_id`` starts at most one run, however
its requests interleave. A request that repeats a Send while that Send is still being admitted
waits for it and answers with the same turn; one that arrives between the bridge's answer and the
turn's publication cannot see "nothing yet"; an id whose turn has been replaced is refused for
good. A loopback fake Runs API stands in for Hermes; no model runs. Pauses are made with events
inside the code under test, never with sleeps."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Optional  # noqa: F401 - used in a type comment
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import WebCase, eventually  # noqa: E402

from plugin import chat_bridge  # noqa: E402


def in_background(function, *args, **kwargs):
    results = []
    thread = threading.Thread(target=lambda: results.append(function(*args, **kwargs)), daemon=True)
    thread.start()
    return thread, results


class GapLock(threading.Condition):
    """The web routes' lock with one pause: after ``arm(thread)``, the first time that thread
    RELEASES the lock it waits for ``gate``. Placed just after the bridge has answered, this holds
    the handler between one critical section and the next, whatever the code does there."""

    def __init__(self):
        super().__init__()
        self.armed = None   # type: Optional[threading.Thread]
        self.paused = threading.Event()
        self.gate = threading.Event()

    def __exit__(self, *exc):
        result = super().__exit__(*exc)
        if self.armed is not None and self.armed is threading.current_thread():
            self.armed = None
            self.paused.set()
            self.gate.wait(15)
        return result


class AdmissionTests(WebCase):
    fake_clock = True   # no housekeeping thread: the test owns every use of the lock

    def bridge_options(self):
        return {"long_poll_seconds": 5.0}

    def test_a_retry_in_the_gap_before_publication_replays_the_turn_and_starts_no_second_run(self):
        # critic2 R1: the first request's run has already completed; its handler is held after
        # the bridge answered and before it published the turn. A retry of the same Send must
        # not find "nothing" and start another run.
        lock = GapLock()
        self.web._cond = lock
        page = self.page()
        self.api.finish.set()
        body = {"message": "Add a total row", "send_id": "V" * 22}
        original, calls = self.bridge.begin_turn, []

        def answered_then_held(*args, **kwargs):
            run = original(*args, **kwargs)
            calls.append(run)
            if len(calls) == 1:
                with self.bridge._lock:   # its run completes before the handler goes on
                    self.assertTrue(self.bridge._lock.wait_for(lambda: run.settled, 10))
                lock.armed = threading.current_thread()
            return run
        with mock.patch.object(self.bridge, "begin_turn", answered_then_held):
            first = in_background(page.request, "POST", "/api/chat", body)
            self.assertTrue(lock.paused.wait(10), "the first handler reached the gap")
            retry = page.request("POST", "/api/chat", body)
            other = page.request("POST", "/api/chat", {"message": "Something else", "send_id": "V" * 22})
            lock.gate.set()
            first[0].join(10)
        status, value = first[1][0]
        self.assertEqual((status, value["replayed"]), (202, False))
        self.assertEqual((retry[0], retry[1].get("replayed"), retry[1].get("turn", {}).get("id")),
                         (200, True, value["turn"]["id"]), "the retry answers with the same turn")
        self.assertEqual((other[0], other[1]["code"]), (400, "bad_request"))
        self.assertEqual(len(self.api.creations()), 1, "one run for one send_id")
        self.assertEqual(len(calls), 1, "the retry claimed nothing")
        self.assertEqual(page.state()[1]["turn"]["id"], value["turn"]["id"], "the published turn is the first")
        done = page.wait_done(value["turn"]["id"])
        self.assertEqual((done["outcome"]["kind"], done["text"]), ("completed", self.api.reply))

    def hold_admission(self):
        claimed, release = threading.Event(), threading.Event()
        original = self.bridge.begin_turn

        def held(*args, **kwargs):
            claimed.set()
            release.wait(15)
            return original(*args, **kwargs)
        patch = mock.patch.object(self.bridge, "begin_turn", held)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release.set)
        return claimed, release

    def test_a_retry_during_admission_waits_and_answers_with_the_same_turn(self):
        page = self.page()
        claimed, release = self.hold_admission()
        body = {"message": "Add a total row", "send_id": "W" * 22}
        first = in_background(page.request, "POST", "/api/chat", body)
        self.assertTrue(claimed.wait(10))
        retry = in_background(page.request, "POST", "/api/chat", body)
        self.assertTrue(eventually(lambda: self.web._in_flight == 2), "the retry is being answered")
        status, value = page.request("POST", "/api/chat", {"message": "Other", "send_id": "W" * 22})
        self.assertEqual((status, value["code"]), (400, "bad_request"), "another message under the same id")
        status, value = page.request("POST", "/api/chat", {"message": "Other", "send_id": "X" * 22})
        self.assertEqual((status, value["code"]), (409, "busy"), "another Send while one is admitted")
        self.assertFalse(retry[1], "the retry waits while the Send is admitted")
        release.set()
        first[0].join(10)
        retry[0].join(10)
        self.assertEqual(first[1][0][0], 202)
        turn_id = first[1][0][1]["turn"]["id"]
        self.assertEqual((retry[1][0][0], retry[1][0][1]["replayed"], retry[1][0][1]["turn"]["id"]),
                         (200, True, turn_id))
        self.api.finish.set()
        page.wait_done(turn_id)
        self.assertEqual(len(self.api.creations()), 1)

    def test_a_refused_admission_refuses_its_waiting_retry_and_leaves_no_reservation(self):
        page = self.page()
        claimed, release = self.hold_admission()
        body = {"message": "Add a total row", "send_id": "Y" * 22}
        refusal = chat_bridge.ChatError(429, "too many chats are running or unresolved; try again shortly")
        with mock.patch.object(self.bridge, "_claim", side_effect=refusal):
            first = in_background(page.request, "POST", "/api/chat", body)
            self.assertTrue(claimed.wait(10))
            retry = in_background(page.request, "POST", "/api/chat", body)
            self.assertTrue(eventually(lambda: self.web._in_flight == 2))
            release.set()
            first[0].join(10)
            retry[0].join(10)
        self.assertEqual([(r[0], r[1]["code"]) for r in (first[1][0], retry[1][0])],
                         [(429, "too_many"), (429, "too_many")])
        session = self.web.sessions.all()[0]
        self.assertIsNone(session.starting, "no reservation is left behind")
        status, value = page.request("POST", "/api/chat", body)   # nothing was sent: the same Send may go now
        self.assertEqual((status, value["replayed"]), (202, False))
        self.assertTrue(self.api.wait_for(lambda: len(self.api.creations()) == 1))

    def test_a_session_ended_during_admission_leaves_no_reservation_and_stops_the_run(self):
        page = self.page()
        claimed, release = self.hold_admission()
        body = {"message": "Add a total row [hang]", "send_id": "Z" * 22}
        first = in_background(page.request, "POST", "/api/chat", body)
        self.assertTrue(claimed.wait(10))
        session = self.web.sessions.all()[0]
        self.assertEqual(page.close()[0], 200)
        release.set()
        first[0].join(10)
        self.assertEqual((first[1][0][0], first[1][0][1]["code"]), (401, "no_session"))
        self.assertIsNone(session.starting)
        self.assertTrue(eventually(lambda: self.bridge.owned_runs() == 0), "the claimed run was stopped")

    def test_an_earlier_send_id_never_starts_a_run_again(self):
        page = self.page()
        self.api.finish.set()
        first = {"message": "Add a total row", "send_id": "A" * 22}
        second = {"message": "Now bold it", "send_id": "B" * 22}
        status, value = page.request("POST", "/api/chat", first)
        self.assertEqual(status, 202)
        page.wait_done(value["turn"]["id"])
        status, value = page.request("POST", "/api/chat", second)
        self.assertEqual(status, 202)
        page.wait_done(value["turn"]["id"])
        status, value = page.request("POST", "/api/chat", first)   # a late copy of the first Send
        self.assertEqual((status, value["code"]), (409, "already_sent"))
        self.assertEqual(page.new_chat()[0], 200)
        status, value = page.request("POST", "/api/chat", second)  # New chat does not forget it
        self.assertEqual((status, value["code"]), (409, "already_sent"))
        self.assertEqual(len(self.api.creations()), 2, "one run per send_id")
        status, value = page.request("POST", "/api/chat", {"message": "Rev 2 body"})
        self.assertEqual(status, 202, "a body without send_id still starts a turn")


if __name__ == "__main__":
    unittest.main()
