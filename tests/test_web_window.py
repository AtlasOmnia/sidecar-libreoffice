# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Revision-5 G1/G2 against the real listener, session tables and MAC implementation.

No UNO, browser, gateway or model. The 30-second load uses synthetic HTTP readers and a
fake Runs API; this is plugin transport qualification, not installed/native acceptance.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import (LauncherClient, PageClient, WebCase, code_of,
                                   eventually, raw_request)
from plugin import chat_bridge, web_api, web_auth

RECT = {"x": 1240, "y": 102, "width": 400, "height": 848}
DOCUMENT = {"document_id": "lo:5f0c:1", "title": "sales.ods", "type": "calc"}


class WindowCase(WebCase):
    def attachment(self, document_id="lo:5f0c:1", follow=True, mode="app"):
        launcher = self.launcher()
        status, launch, signature = launcher.launch(dict(DOCUMENT, document_id=document_id),
                                                    mode=mode, follow=True if follow else None)
        self.assertEqual(status, 201)
        self.assertEqual(launch["follow"], follow)
        self.assertIsNotNone(signature)
        page = PageClient(self.port)
        status, exchange = page.exchange(code_of(launch["url"]))
        self.assertEqual(status, 200)
        self.assertEqual(exchange["follow"], follow)
        self.assertNotIn("launch_id", exchange)
        return launcher, launch["launch_id"], page

    @staticmethod
    def snapshot(launch_id, seq=1, state="normal", bounds=RECT):
        return {"launch_id": launch_id, "seq": seq, "state": state, "bounds": bounds}

    def bounds(self, launcher, launch_id, seq=1, state="normal", bounds=RECT):
        return launcher.call("POST", "/launcher/bounds", self.snapshot(launch_id, seq, state, bounds))

    def background(self, fn, *args):
        results, errors = [], []

        def run():
            try:
                results.append(fn(*args))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # Cleanup always wakes parked requests before joining; no leaked reader on assertion failure.
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.web.close_begin)
        return thread, results, errors

    def answered(self, job):
        thread, results, errors = job
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        return results[0]


class WindowBoundaryTests(WindowCase):
    def test_capability_and_old_shaped_launch_are_independent_of_page_preference(self):
        status, hello = self.launcher().hello()
        self.assertEqual((status, hello["caps"]), (200, ["window_follow", "attachment_release"]))
        launcher, launch_id, page = self.attachment(follow=False)
        self.assertEqual(page.window()[0:2], (404, {"ok": False,
                         "error": web_api.ERRORS["no_follow"][1], "code": "no_follow"}))
        self.assertEqual(self.bounds(launcher, launch_id)[0:2],
                         (409, {"ok": False, "error": web_api.ERRORS["attachment_stale"][1],
                                "code": "attachment_stale"}))
        # A negotiated capability requires no plugin setting and no browser preference input.
        launcher, launch_id, page = self.attachment("follow-capable")
        self.assertEqual(self.bounds(launcher, launch_id)[0], 200)
        self.assertEqual(page.window()[1]["window"], {"seq": 1, "state": "normal", "bounds": RECT})
        status, live, _sig = launcher.launch(dict(DOCUMENT, document_id="follow-capable"), follow=True)
        self.assertEqual((status, live["follow"], live["launch_id"], live["url"]), (200, False, None, None))

    def test_follow_key_is_true_only_and_only_with_app(self):
        launcher = self.launcher()
        body = {"document": DOCUMENT, "host_scheme": None, "mode": "app", "bounds": RECT}
        for follow in (False, 1, "true", None, [], {}):
            with self.subTest(follow=follow):
                status, value, sig = launcher.call("POST", "/launcher/launch", dict(body, follow=follow))
                self.assertEqual((status, value["code"]), (400, "bad_request"))
                self.assertIsNotNone(sig)
        status, value, sig = launcher.call("POST", "/launcher/launch",
                                           dict(body, mode="tab", bounds=None, follow=True))
        self.assertEqual((status, value["code"]), (400, "bad_request"))
        self.assertIsNotNone(sig)
        self.attachment("tab", follow=False, mode="tab")

    def test_bounds_shapes_are_signed_refusals_and_do_not_mutate_snapshot(self):
        launcher, launch_id, page = self.attachment()
        body = self.snapshot(launch_id)
        bad = [dict(body, seq=x) for x in (True, False, 0, -1, 2147483648, 1.0, "1", None)]
        bad += [dict(body, launch_id=x) for x in (None, True, "short", "A" * 22 + "\n")]
        bad += [dict(body, state=x) for x in (None, True, [], "other")]
        bad += [dict(body, bounds=x) for x in (None, [], dict(RECT, width=True), dict(RECT, x=-32769),
                 dict(RECT, y=32768), dict(RECT, width=239), dict(RECT, height=8193), dict(RECT, height=2.5),
                 dict(RECT, extra=1))]
        bad += [dict(body, state=s) for s in ("hidden", "minimized", "fullscreen", "window_closed")]
        bad += [dict(body, extra=1), {k: v for k, v in body.items() if k != "state"}]
        for value in bad:
            with self.subTest(value=value):
                # Renew before the channel's bounded call count, also under future test expansion.
                if launcher.counter >= 48:
                    launcher.hello()
                status, refusal, signature = launcher.call("POST", "/launcher/bounds", value)
                self.assertEqual((status, refusal["code"]), (400, "bad_request"))
                self.assertIsNotNone(signature)
                self.assertEqual(self.web.sessions.all()[0].window, {"seq": 0, "state": "hidden", "bounds": None})
        self.assertEqual(self.bounds(launcher, launch_id)[0], 200)
        self.assertEqual(page.window()[1]["window"]["seq"], 1)

    def test_bounds_authority_monotonicity_state_and_lifetime(self):
        launcher, launch_id, page = self.attachment()
        for seq, state, bounds in ((1, "normal", RECT), (2, "minimized", None), (3, "hidden", None),
                                   (4, "fullscreen", None), (5, "window_closed", None),
                                   (2147483647, "normal", dict(RECT, x=-32768, y=32767, width=240, height=8192))):
            self.assertEqual(self.bounds(launcher, launch_id, seq, state, bounds)[0:2],
                             (200, {"ok": True, "seq": seq}))
            status, value = page.window(0)
            self.assertEqual((status, value["window"]),
                             (200, {"seq": seq, "state": state, "bounds": bounds}))
            self.assertNotIn("launch_id", json.dumps(value))
        for seq in (2147483647, 1):
            self.assertEqual(self.bounds(launcher, launch_id, seq)[1]["code"], "attachment_stale")
        self.assertEqual(self.bounds(launcher, "A" * 22)[1]["code"], "attachment_stale")
        session = self.web.sessions.all()[0]
        page.close()
        self.assertIsNone(session.window)
        status, refusal, sig = self.bounds(launcher, launch_id, 10)
        self.assertEqual((status, refusal["code"]), (409, "attachment_stale"))
        self.assertIsNotNone(sig)
        self.assertEqual(page.window()[1]["ended"], "closed")
        self.assertEqual(len(self.web.sessions), 0)

    def test_pending_launch_is_not_a_live_attachment(self):
        launcher = self.launcher()
        status, launch, _sig = launcher.launch(follow=True)
        self.assertEqual(status, 201)
        self.assertEqual(self.bounds(launcher, launch["launch_id"])[1]["code"], "attachment_stale")

    def test_bounds_mac_counter_replay_and_foreign_credentials(self):
        launcher, launch_id, page = self.attachment()
        body = self.snapshot(launch_id)
        raw = json.dumps(body).encode()
        headers = [("Host", self.web.host), ("Content-Type", "application/json")]
        status, hdr, data = raw_request(self.port, "POST", "/launcher/bounds", raw, headers)
        self.assertEqual((status, json.loads(data)["code"]), (401, "launcher_auth"))
        self.assertNotIn("X-Sidecar-MAC", dict(hdr))
        for kwargs in ({"tamper": True}, {"counter": 2}, {"counter": 0}):
            launcher.hello()
            status, answer, sig = launcher.call("POST", "/launcher/bounds", body, **kwargs)
            self.assertEqual((status, answer["code"], sig), (401, "launcher_auth", None))
        launcher.hello()
        self.assertEqual(self.bounds(launcher, launch_id)[0], 200)
        replay = launcher.call("POST", "/launcher/bounds", body, counter=1)
        self.assertEqual((replay[0], replay[1]["code"], replay[2]), (401, "launcher_auth", None))
        for name in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "X-Sidecar-Session", "X-Hermes-Plugin-Key"):
            launcher.hello()
            status, value, sig = launcher.call("POST", "/launcher/bounds", body,
                                               extra=[(name, "foreign")], check=False)
            self.assertEqual((status, value["code"], sig), (403, "forbidden_credential", None))
        launcher.hello()
        self.assertEqual(self.bounds(launcher, launch_id, 2)[0], 200)
        self.assertEqual(page.window()[1]["window"]["seq"], 2)

    def test_window_query_and_browser_boundaries(self):
        launcher, launch_id, page = self.attachment()
        self.bounds(launcher, launch_id)
        for query in ("", "seq=", "seq=00", "seq=01", "seq=-1", "seq=1.0", "seq=true", "seq=2147483648",
                      "seq=0&seq=0", "seq=0&have=0", "seq=0&", "seq=%30", "s%65q=0", "seq=+0"):
            with self.subTest(query=query):
                status, value = page.request("GET", "/api/window?" + query)
                self.assertEqual((status, value["code"]), (400, "bad_request"))
        self.assertEqual(PageClient(self.port).window()[1]["code"], "no_session")
        base = page.headers("GET")
        for headers, code in (([(k, "cross-site" if k == "Sec-Fetch-Site" else v) for k, v in base], "cross_site"),
                              (base + [("X-Sidecar-MAC", "foreign")], "forbidden_credential"),
                              (base + [("Origin", "http://evil.example")], "bad_origin"),
                              (base + [("X-Sidecar-Session", page.session)], "no_session"),
                              ([(k, "evil" if k == "Host" else v) for k, v in base], "bad_host")):
            status, value = page.request("GET", "/api/window?seq=0", headers=headers)
            self.assertEqual(value["code"], code)
        for method, path, allow in (("POST", "/api/window?seq=0", "GET"),
                                     ("GET", "/launcher/bounds", "POST"),
                                     ("OPTIONS", "/api/window?seq=0", "GET")):
            status, hdr, _raw = raw_request(self.port, method, path, headers=[("Host", self.web.host)])
            self.assertEqual((status, dict(hdr)["Allow"]), (405, allow))
        self.assertEqual(page.request("POST", "/api/bounds", self.snapshot(launch_id))[0], 404)
        self.assertEqual(self.web.sessions.all()[0].window["seq"], 1)

    def test_bounds_body_limit_and_host_gate_precede_mac(self):
        body = b"x" * (web_api.MAX_POST + 1)
        headers = [("Host", self.web.host), ("Content-Type", "application/json")]
        status, _hdr, raw = raw_request(self.port, "POST", "/launcher/bounds", body, headers)
        self.assertEqual((status, json.loads(raw)["code"]), (413, "too_large"))
        headers[0] = ("Host", "evil.example")
        status, _hdr, raw = raw_request(self.port, "POST", "/launcher/bounds", b"{}", headers)
        self.assertEqual((status, json.loads(raw)["code"]), (403, "bad_host"))

    def test_successful_new_routes_are_debug_and_do_not_log_geometry_or_ids(self):
        launcher, launch_id, page = self.attachment()
        with self.assertLogs(chat_bridge.log, level="DEBUG") as captured:
            self.bounds(launcher, launch_id)
            page.window(0)
            self.bounds(launcher, launch_id)  # stale refusal is INFO
            self.assertTrue(eventually(lambda: all(any(f" {label} -> {status} " in r.getMessage()
                                                       for r in captured.records)
                                                   for label, status in (("launcher-bounds", 200),
                                                                         ("api-window", 200),
                                                                         ("launcher-bounds", 409)))))
        wanted = {("launcher-bounds", 200): "DEBUG", ("api-window", 200): "DEBUG",
                  ("launcher-bounds", 409): "INFO"}
        for (label, status), level in wanted.items():
            records = [r for r in captured.records if f" {label} -> {status} " in r.getMessage()]
            self.assertEqual([r.levelname for r in records], [level])
        text = "\n".join(captured.output)
        for secret in (launch_id, page.session, launcher.channel, "sales.ods", str(RECT), '"width"',
                       "/launcher/bounds", "/api/window"):
            self.assertNotIn(secret, text)


class WindowPoolTests(WindowCase):
    def bridge_options(self):
        return {"long_poll_seconds": 5.0}

    def test_initial_snapshot_timeout_and_poll_only_fallback(self):
        launcher, launch_id, page = self.attachment()
        self.web.long_poll_seconds = 0.08
        started = time.monotonic()
        status, value = page.window()
        self.assertGreaterEqual(time.monotonic() - started, 0.07)
        self.assertEqual((status, value), (200, {"ok": True, "superseded": False, "retry_ms": 0,
                         "window": {"seq": 0, "state": "hidden", "bounds": None}}))
        with mock.patch.object(web_api, "WINDOW_POLL_ONLY", True):
            self.assertEqual(page.window()[1]["retry_ms"], 100)
            self.bounds(launcher, launch_id)
            self.assertEqual(page.window(0)[1]["retry_ms"], 0, "new data is always immediate")
            self.assertEqual(page.window(2147483647)[1]["retry_ms"], 100)
        self.assertEqual(self.web._parked, 0)

    def test_latest_only_snapshot_wakes_own_reader(self):
        launcher, launch_id, page = self.attachment()
        job = self.background(page.window)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        self.bounds(launcher, launch_id)
        self.assertEqual(self.answered(job)[1]["window"], {"seq": 1, "state": "normal", "bounds": RECT})
        self.bounds(launcher, launch_id, 5, "hidden", None)
        self.bounds(launcher, launch_id, 9)
        self.assertEqual(page.window()[1]["window"]["seq"], 9)
        _other_launcher, _id, other_page = self.attachment("other")
        other = self.background(other_page.window)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        self.bounds(launcher, launch_id, 10)
        self.assertTrue(other[0].is_alive(), "another session cannot read this snapshot")
        self.web.close_begin()
        self.assertEqual(self.answered(other)[0], 503)

    def test_supersession_transfers_slot_without_double_decrement(self):
        launcher, launch_id, page = self.attachment()
        older = self.background(page.window)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        newer = self.background(page.window)
        self.assertTrue(self.answered(older)[1]["superseded"])
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        self.assertEqual(len(self.web._window_waiters), 1)
        self.bounds(launcher, launch_id)
        self.assertFalse(self.answered(newer)[1]["superseded"])
        self.assertEqual((self.web._parked, len(self.web._window_waiters)), (0, 0))

    def test_four_window_slots_fifth_retry_and_turn_preemption(self):
        attachments = [self.attachment(f"d{i}") for i in range(5)]
        jobs = [self.background(page.window) for _l, _id, page in attachments[:4]]
        self.assertTrue(eventually(lambda: self.web._parked == 4))
        self.assertEqual(attachments[4][2].window()[1]["retry_ms"], 250)
        page = attachments[4][2]
        turn = self.start_turn(page)
        seq = self.followed_seq(page, turn)
        turn_job = self.background(page.turn, turn, seq)
        self.assertTrue(eventually(lambda: len(self.web._window_waiters) == 3 and self.web._parked == 4))
        self.assertTrue(eventually(lambda: sum(bool(job[1]) for job in jobs) == 1))
        preempted = next(job for job in jobs if job[1])
        self.assertEqual(self.answered(preempted)[1]["retry_ms"], 250)
        self.assertEqual(page.window()[1]["retry_ms"], 250)
        self.assertEqual(page.stop(turn)[0], 202)
        self.assertEqual(self.answered(turn_job)[0], 200)
        self.web.close_begin()
        for job in jobs:
            if job is not preempted:
                self.assertEqual(self.answered(job)[0], 503)
        self.assertEqual(self.web._parked, 0)

    def test_session_end_wakes_poll_and_drops_snapshot(self):
        _launcher, _id, page = self.attachment()
        session = self.web.sessions.all()[0]
        job = self.background(page.window)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        page.close()
        status, value = self.answered(job)
        self.assertEqual((status, value["ended"]), (401, "closed"))
        self.assertIsNone(session.window)
        self.assertEqual(self.web._parked, 0)

    def test_unload_four_window_polls_once_and_no_late_writes(self):
        attachments = [self.attachment(f"d{i}") for i in range(4)]
        jobs = [self.background(page.window) for _l, _id, page in attachments]
        self.assertTrue(eventually(lambda: self.web._parked == 4))
        sessions = self.web.sessions.all()
        started = time.monotonic()
        self.bridge.close(2.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.25)
        for job in jobs:
            status, value = self.answered(job)
            self.assertEqual((status, value["code"], value["ended"]), (503, "turned_off", "turned_off"))
        self.assertTrue(self.web._finished)
        self.assertEqual((self.web._in_flight, self.web._parked, len(self.web._requests)), (0, 0, 0))
        self.assertTrue(all(s.window is None for s in sessions))
        self.web.close_begin()
        self.web.close_end(0)
        for job in jobs:
            self.assertEqual(len(job[1]), 1)

    def test_call_after_unload_has_host_first_and_no_unsigned_authority(self):
        from email.message import Message
        launcher, launch_id, page = self.attachment()
        self.web.close_begin()

        class Request:
            command = "POST"
            path = "/launcher/bounds"
            def __init__(self, host):
                self.headers = Message()
                self.headers["Host"] = host
                self.answers = []
            def respond(self, status, raw, _start, **kwargs):
                self.answers.append((status, json.loads(raw), kwargs))
        request = Request(self.web.host)
        self.web.handle(request)
        self.assertEqual((request.answers[0][0], request.answers[0][1]["code"]), (503, "turned_off"))
        self.assertNotIn("X-Sidecar-MAC", dict(request.answers[0][2]["headers"]))
        bad = Request("evil")
        self.web.handle(bad)
        self.assertEqual(bad.answers[0][1]["code"], "bad_host")
        browser = Request(self.web.host)
        browser.path, browser.command = "/api/window?seq=0", "GET"
        self.web.handle(browser)
        self.assertEqual(browser.answers[0][1]["ended"], "turned_off")


class WindowClockTests(WindowCase):
    fake_clock = True

    def test_real_session_idle_boundary_refresh_expiry_and_tombstone_forgetting(self):
        launcher, launch_id, page = self.attachment()
        self.bounds(launcher, launch_id)
        session = self.web.sessions.all()[0]
        self.clock.advance(web_auth.SESSION_IDLE_SECONDS - 1)
        self.web.sweep()
        self.assertIsNone(session.ended)
        page.window(0)  # real authenticated request refreshes the real session
        self.clock.advance(web_auth.SESSION_IDLE_SECONDS - 1)
        self.web.sweep()
        self.assertIsNone(session.ended)
        self.clock.advance(1)
        self.web.sweep()
        self.assertEqual((session.ended, session.window), ("idle", None))
        self.assertEqual(page.window()[1]["ended"], "idle")
        launcher.hello()
        self.assertEqual(self.bounds(launcher, launch_id, 2)[1]["code"], "attachment_stale")
        self.clock.advance(web_auth.ENDED_SECONDS - 1)
        self.assertEqual(page.state()[1]["ended"], "idle")
        self.clock.advance(1)
        self.assertIsNone(page.state()[1]["ended"])

    def test_window_poll_keeps_window_live_but_does_not_keep_turn_alive(self):
        launcher, launch_id, page = self.attachment()
        turn = self.start_turn(page)
        self.followed_seq(page, turn)
        self.web.long_poll_seconds = 5
        job = self.background(page.window)
        self.assertTrue(eventually(lambda: self.web._parked == 1))
        self.clock.advance(web_api.WINDOW_GONE_SECONDS)
        self.web.sweep()
        self.assertTrue(self.api.wait_for(lambda: self.api.stops))
        launcher.hello()
        status, live, _sig = launcher.launch(DOCUMENT, follow=True)
        self.assertEqual((status, live["window_live"], live["follow"]), (200, True, False))
        self.bounds(launcher, launch_id)
        self.assertEqual(self.answered(job)[0], 200)


class BoundsVectorTests(unittest.TestCase):
    def test_three_revision_five_vectors_use_real_auth_functions_and_channel(self):
        key = bytes.fromhex("49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1")
        normal = (b'{"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":1,"state":"normal",'
                  b'"bounds":{"x":1240,"y":102,"width":400,"height":848}}')
        minimized = b'{"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":2,"state":"minimized","bounds":null}'
        response = b'{"ok":true,"seq":1}'
        self.assertEqual(hashlib.sha256(normal).hexdigest(),
                         "22f9e39c4973f6720ea42b8e720a5f5993d2b3d578682559edf6d4ae654ac807")
        self.assertEqual(hashlib.sha256(minimized).hexdigest(),
                         "cda42e6aeb2b78e01ce7ef5c07e3515c219f8a2161bff327b77662c9a9d7b254")
        self.assertEqual(hashlib.sha256(response).hexdigest(),
                         "c87ef1c0ab45dc8b9e285d32cd005ac8e66110a4dde2e68dfaa64991dcc2570c")
        channels = web_auth.Channels(lambda: 0)
        nonce = "A" * 43
        channel = web_auth.Channel(nonce, key, 120)
        channel.next_counter, channel.calls = 3, 2
        channels._live[nonce] = channel
        for counter, raw, expected in ((3, normal, "v30WcmVm1K7joHamsLRFyRhnU4E5g6y47-bhQB8TPKE"),
                                       (4, minimized, "zekmULHpr-_hN52XDlsmycbEFggZ24vGwBNHTRKBHv8")):
            self.assertEqual(web_auth.request_mac(key, "POST", "/launcher/bounds", counter, raw), expected)
            self.assertEqual(channels.verify([nonce], [str(counter)], [expected], "POST", "/launcher/bounds", raw),
                             (key, counter))
        self.assertEqual(web_auth.response_mac(key, 3, 200, response),
                         "uB4aMcV9-01j7t7iVhTqS1q74kMyjkur7kCGft1U-dQ")


class WindowStressTests(WindowCase):
    def bridge_options(self):
        return {"long_poll_seconds": 25.0, "poll_interval": 0.1}

    def test_g2_thirty_seconds_eight_readers_four_turns_bounds_and_all_stops(self):
        attachments = [self.attachment(f"stress-{i}") for i in range(8)]
        stop = threading.Event()
        errors, counts, max_parked, statuses = [], [0] * 8, [0], []
        threads = []
        self.addCleanup(lambda: [t.join(10) for t in threads])
        self.addCleanup(self.web.close_begin)
        self.addCleanup(stop.set)

        def reader(index, page):
            seq = 0
            try:
                while not stop.is_set():
                    status, value = page.window(seq)
                    if stop.is_set():
                        break
                    if status != 200:
                        raise AssertionError(("window", status, value.get("code")))
                    seq = value["window"]["seq"]
                    counts[index] += 1
                    stop.wait(value["retry_ms"] / 1000)
            except Exception as exc:
                errors.append(exc)

        def turn_reader(page, turn, seq):
            try:
                while not stop.is_set():
                    status, value = page.turn(turn, seq)
                    if stop.is_set():
                        break
                    if status != 200:
                        raise AssertionError(("turn", status, value.get("code")))
                    seq = value["turn"]["seq"]
                    if value["turn"]["state"] == "done":
                        break
            except Exception as exc:
                errors.append(exc)

        def start_group(group):
            turns = []
            for index in group:
                page = attachments[index][2]
                turn = self.start_turn(page)
                seq = self.followed_seq(page, turn)
                turns.append((page, turn))
                thread = threading.Thread(target=turn_reader, args=(page, turn, seq), daemon=True)
                threads.append(thread)
                thread.start()
            self.assertTrue(eventually(lambda: self.web._parked == 4 and not self.web._window_waiters))
            return turns

        current = start_group(range(4))
        for index, (_l, _id, page) in enumerate(attachments):
            thread = threading.Thread(target=reader, args=(index, page), daemon=True)
            threads.append(thread)
            thread.start()
        sender = self.launcher()
        started = time.monotonic()
        deadline, next_batch, sequence, calls = started + 30, started, 0, 0
        stop_times = []
        swapped = False
        page_loads = 0

        def stop_group(turns):
            for page, turn in turns:
                before = time.monotonic()
                status, _value = page.stop(turn)
                stop_times.append(time.monotonic() - before)
                self.assertEqual(status, 202)
                self.assertLess(stop_times[-1], 1.0)
            for page, turn in turns:
                self.assertEqual(page.wait_done(turn)["outcome"]["kind"], "user")

        while time.monotonic() < deadline:
            now = time.monotonic()
            if not swapped and now - started >= 15:
                stop_group(current)
                current = start_group(range(4, 8))
                swapped = True
            sequence += 1
            for _launcher, launch_id, _page in attachments:
                if sender.counter >= 47:
                    sender.hello()
                status, value, signature = self.bounds(sender, launch_id, sequence,
                                                        bounds=dict(RECT, x=1200 + sequence % 100))
                statuses.append(status)
                self.assertEqual((status, value["seq"]), (200, sequence))
                self.assertIsNotNone(signature)  # call() already verifies the exact response bytes
                calls += 1
                with self.web._cond:
                    max_parked[0] = max(max_parked[0], self.web._parked)
                    self.assertLessEqual(self.web._parked, 4)
            if page_loads == 0 or (page_loads == 1 and swapped):
                status, _hdr, raw = raw_request(self.port, "GET", "/ui/index.html",
                                                headers=[("Host", self.web.host)])
                self.assertEqual(status, 200)
                self.assertIn(b"Sidecar", raw)
                page_loads += 1
            next_batch += 0.1  # 10 Hz per session; stronger than the production 10/s total
            stop.wait(max(0, next_batch - time.monotonic()))
        elapsed = time.monotonic() - started
        stop_group(current)
        stop.set()
        self.web.close_begin()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(stop_times), 8)
        self.assertEqual(page_loads, 2)
        self.assertTrue(all(count > 0 for count in counts), counts)
        self.assertEqual(max_parked[0], 4)
        self.assertNotIn(503, statuses)
        # Aggregate evidence only: never session, launch id, code, token or rectangle.
        print("G2 plugin stress:", json.dumps({"seconds": round(elapsed, 3), "signed_bounds": calls,
              "parked_max": max_parked[0], "reader_responses": counts, "stops": len(stop_times),
              "stop_max_ms": round(max(stop_times) * 1000, 3), "page_loads": page_loads,
              "http_errors": len([s for s in statuses if s != 200])}))


if __name__ == "__main__":
    unittest.main()
