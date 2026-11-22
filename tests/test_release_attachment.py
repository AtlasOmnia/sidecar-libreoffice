# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""B1: real listener/authentication + production close worker, no native apps."""
from __future__ import annotations

import inspect
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extension/python"))
import sidecar_launcher as L
from support.fake_runs_api import (WebCase, LauncherClient, PageClient, PAIRING_TOKEN,
                                   code_of, eventually)

DOC = {"document_id": "lo:b1", "title": "test.ods", "type": "calc"}


class DelayedReleaseTests(WebCase):
    fake_clock = True

    def test_delayed_initial_and_each_retry_before_and_after_authentication(self):
        for attempt in range(1, 5):
            for phase in ("before_auth", "after_auth"):
                with self.subTest(attempt=attempt, phase=phase):
                    self.race(attempt, phase)

    def race(self, attempt, phase):
        mint = LauncherClient(self.port)
        mint.hello()
        _, old, _ = mint.launch(DOC)
        old_page = PageClient(self.port)
        self.assertEqual(old_page.exchange(code_of(old["url"]))[0], 200)
        entered, resume, timed_out, applied = (threading.Event() for _ in range(4))
        count = [0]
        original_launcher, original_release, original_send = self.web._launcher, self.web._release, self.web._send
        held_calls = set()

        def launcher(call, name, raw, extra):
            if name == "release":
                count[0] += 1
                call_number = count[0]
                if call_number == attempt:
                    held_calls.add(id(call))
                    if phase == "before_auth":
                        entered.set()
                        self.assertTrue(resume.wait(12))
            return original_launcher(call, name, raw, extra)

        def release(call, body):
            if id(call) in held_calls and phase == "after_auth":
                self.assertIsNotNone(call.mac)
                entered.set()
                self.assertTrue(resume.wait(12))
            result = original_release(call, body)
            if id(call) in held_calls:
                applied.set()
            return result

        def send(call, status, value, *args):
            # Earlier attempts apply, but their acknowledgements are lost. The
            # selected attempt is held across the client's real whole-call timeout.
            if call.request.path == "/launcher/release" and id(call) not in held_calls:
                time.sleep(0.6)
                return
            return original_send(call, status, value, *args)

        class Client(L.LauncherClient):
            def _exchange(inner, method, path, body, headers):
                try:
                    return super()._exchange(method, path, body, headers)
                finally:
                    if path == "/launcher/release" and entered.is_set():
                        timed_out.set()

        self.web._launcher, self.web._release, self.web._send = launcher, release, send
        worker = L.Launcher(platform="win32", adapter=object(), port=lambda: self.port, client=Client)
        worker.configure(lambda: PAIRING_TOKEN)
        worker._generations[DOC["document_id"]] = 1
        args = [DOC["document_id"], 1, time.monotonic()]
        # The identical test runs on the frozen baseline, whose worker has no
        # attachment arguments; its actual document-wide payload reproduces B1.
        if "launch_id" in inspect.signature(worker._release_closed).parameters:
            args += [old["launch_id"], True]
        thread = threading.Thread(target=worker._release_closed, args=args)
        thread.start()
        try:
            self.assertTrue(entered.wait(6))
            self.assertTrue(timed_out.wait(2))
            worker._generations[DOC["document_id"]] = 2
            self.clock.advance(91)
            _, new, _ = mint.launch(DOC)
            page = PageClient(self.port)
            self.assertEqual(page.exchange(code_of(new["url"]))[0], 200)
            self.assertEqual(page.chat()[0], 202)
            self.assertTrue(self.api.wait_for(lambda: len(self.api.creations()) >= 1))
            with self.web._cond:
                session, _ = self.web.sessions.lookup([page.session])
                self.assertTrue(eventually(lambda: session.turn.run.run_id is not None))
                run_id = session.turn.run.run_id
            with self.web._cond:
                session.last_seen -= 91  # permit another mint without expiring the held channel
            _, pending, _ = mint.launch(DOC)
            resume.set()
            self.assertTrue(applied.wait(3))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(page.state()[0], 200, "late old close ended replacement")
            self.assertNotIn(run_id, [run for run, _ in self.api.stops], "late old close stopped replacement turn")
            pending_page = PageClient(self.port)
            self.assertEqual(pending_page.exchange(code_of(pending["url"]))[0], 200,
                             "late old close revoked replacement pending code")
            self.assertEqual(old_page.state()[0], 401)
        finally:
            resume.set()
            thread.join(6)
            self.web._launcher, self.web._release, self.web._send = original_launcher, original_release, original_send
            self.api.finish.set()
            # End the replacement between subcases without a document-wide release.
            for session in self.web.sessions.all():
                with self.web._cond:
                    self.web._end_session(session, "closed")
            self.api.finish.clear()


if __name__ == "__main__":
    unittest.main()
