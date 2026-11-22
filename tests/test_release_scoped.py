# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Attachment release boundaries, retry policy and launcher retention."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_release_attachment import DOC, L
import test_launcher as T
from support.fake_runs_api import WebCase, LauncherClient, PageClient, PAIRING_TOKEN, code_of


class ScopedBoundaryTests(WebCase):
    fake_clock = True

    def test_shape_document_binding_idempotence_expired_record_and_legacy(self):
        client = LauncherClient(self.port)
        client.hello()
        _, launch, _ = client.launch(DOC)
        payload = {"document_id": DOC["document_id"], "reason": "document_closed",
                   "launch_id": launch["launch_id"]}
        for bad in (None, False, 1, "short", "A" * 22 + "\n"):
            self.assertEqual(client.call("POST", "/launcher/release", dict(payload, launch_id=bad))[0], 400)
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, document_id="wrong"))[0], 400)
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, document_id=None, reason="host_quit"))[0], 400)
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, extra=True))[0], 400)
        page = PageClient(self.port)
        self.assertEqual(page.exchange(code_of(launch["url"]))[0], 200, "refusals must not revoke")
        self.clock.advance(121)
        client.hello()
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, document_id="wrong"))[0], 400)
        self.assertEqual(page.state()[0], 200)
        self.assertEqual(client.call("POST", "/launcher/release", payload)[1]["released"], 1)
        self.assertEqual(client.call("POST", "/launcher/release", payload)[1]["released"], 0)
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, launch_id="Z" * 22))[1]["released"], 0)
        _, pending, _ = client.launch(DOC)
        self.assertEqual(client.call("POST", "/launcher/release", dict(payload, launch_id=pending["launch_id"]))[1]["released"], 0)
        self.assertEqual(PageClient(self.port).exchange(code_of(pending["url"]))[0], 401)
        _, pending, _ = client.launch(DOC)
        self.assertEqual(client.call("POST", "/launcher/release", {k: payload[k] for k in ("document_id", "reason")})[0], 200)
        self.assertEqual(PageClient(self.port).exchange(code_of(pending["url"]))[0], 401)

    def test_close_during_real_mint_and_exchange(self):
        import re
        import tempfile
        for phase in ("mint", "exchange"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as cache:
                entered, resume = threading.Event(), threading.Event()
                original = self.web._send
                def send(call, status, value, *args):
                    route = "/launcher/launch" if phase == "mint" else "/api/session"
                    if call.request.path == route and status in (200, 201):
                        entered.set()
                        self.assertTrue(resume.wait(5))
                    return original(call, status, value, *args)
                self.web._send = send
                adapter = T.FakeAdapter(Path(cache), browsers=())
                exchanges = []
                def open_default(path):
                    code = re.search(r"#launch=([A-Za-z0-9_-]{43})", Path(path).read_text()).group(1)
                    page = PageClient(self.port)
                    exchanges.append((page, page.exchange(code)))
                adapter.open_default = open_default
                threads = []
                def start(fn, name):
                    if name != "SidecarLaunchCleanup":
                        t = threading.Thread(target=fn, name=name)
                        threads.append(t)
                        t.start()
                worker = L.Launcher(adapter=adapter, platform="win32", port=lambda: self.port, start=start)
                worker.configure(lambda: PAIRING_TOKEN)
                try:
                    self.assertTrue(worker.open(DOC, None, None, None, lambda *a: None))
                    self.assertTrue(entered.wait(3))
                    worker.on_document_closed(DOC["document_id"])
                    resume.set()
                    for t in threads:
                        t.join(5)
                        self.assertFalse(t.is_alive())
                    self.assertEqual(self.web.sessions.for_document(DOC["document_id"]), [])
                    self.assertEqual(self.web.launches.outstanding(), 0)
                    self.assertEqual(worker._attachments, {})
                    self.assertEqual(worker._launches, {})
                    if phase == "mint":
                        self.assertEqual(exchanges, [], "no browser handoff after close during mint")
                    else:
                        self.assertEqual(exchanges[0][0].state()[0], 401)
                finally:
                    resume.set()
                    for t in threads:
                        t.join(5)
                    self.web._send = original

    def test_real_signed_refusal_is_final(self):
        client = LauncherClient(self.port)
        client.hello()
        _, launch, _ = client.launch(DOC)
        original = self.web._release
        calls = []
        def release(call, body):
            self.assertIsNotNone(call.mac)
            calls.append(body)
            return original(call, body)
        self.web._release = release
        try:
            worker = L.Launcher(adapter=object(), platform="win32", port=lambda: self.port)
            worker.configure(lambda: PAIRING_TOKEN)
            worker._release_closed("wrong-document", 0, time.monotonic(), launch["launch_id"], True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.web.launches.outstanding(), 1)
        finally:
            self.web._release = original

    def test_real_exhaustion_same_id_four_fresh_channels_and_whole_deadlines(self):
        self.held_worker(False)

    def test_quit_does_not_wait_for_held_release_and_admits_no_retry(self):
        self.held_worker(True)

    def held_worker(self, quit_while_held):
        client = LauncherClient(self.port)
        client.hello()
        _, launch, _ = client.launch(DOC)
        resume, entered = threading.Event(), threading.Event()
        original = self.web._release
        calls = []
        def release(call, body):
            if body["reason"] == "document_closed":
                self.assertIsNotNone(call.mac)
                calls.append((time.monotonic(), body.copy(), call.request.headers["X-Sidecar-Channel"]))
                entered.set()
                resume.wait(8)
            return original(call, body)
        self.web._release = release
        worker = L.Launcher(adapter=T.FakeAdapter(self.root), platform="win32", port=lambda: self.port)
        worker.configure(lambda: PAIRING_TOKEN)
        worker._generations[DOC["document_id"]] = 1
        started = time.monotonic()
        thread = threading.Thread(target=worker._release_closed,
                                  args=(DOC["document_id"], 1, started, launch["launch_id"], True))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            if quit_while_held:
                worker._ever_opened = True
                before = time.monotonic()
                worker.on_terminate()
                self.assertLess(time.monotonic() - before, 0.5)
                self.assertFalse(resume.is_set())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 4.3)
            self.assertEqual(len(calls), 1 if quit_while_held else 4)
            self.assertEqual(len({channel for _, _, channel in calls}), len(calls))
            self.assertTrue(all(body["launch_id"] == launch["launch_id"] for _, body, _ in calls))
            for (at, _, _), offset in zip(calls, (0, 0.5, 1.5, 3.5)):
                self.assertLess(abs(at - started - offset), 0.2)
        finally:
            resume.set()
            thread.join(5)
            self.web._release = original


class ScopedLauncherTests(unittest.TestCase):
    setUp = T.StateMachine.setUp
    launcher = T.StateMachine.launcher
    report = T.StateMachine.report

    def test_revision_51_vector_plugin_launcher_and_independent_stub(self):
        import hashlib
        from plugin import web_auth
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/web_smoke"))
        import contract
        self.assertEqual(contract.check_vectors(), [])
        key = bytes.fromhex(contract.VECTORS["channel_key"])
        body = L.encode_body({"document_id": "lo:5f0c:1", "reason": "document_closed", "launch_id": "A" * 22})
        self.assertEqual(body.decode(), contract.VECTORS["request5_body"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), contract.VECTORS["request5_body_sha256"])
        for module in (L, web_auth):
            self.assertEqual(module.request_mac(key, "POST", "/launcher/release", 5, body), contract.VECTORS["request5_mac"])
            self.assertEqual(module.response_mac(key, 5, 200, b'{"ok":true,"released":1}'), contract.VECTORS["response5_mac"])

    def test_all_signed_mints_retained_including_not_exchanged_and_single_worker_claim(self):
        launcher = self.launcher({"states": ["pending"]})
        self.client.caps = ["attachment_release"]
        ids = iter(("B" * 22, "C" * 22))
        original = self.client.launch
        self.client.launch = lambda *args: dict(original(*args), launch_id=next(ids))
        released = []
        self.client.release = lambda doc, reason, **kw: released.append((doc, reason, kw)) or 0
        for _ in range(2):
            launcher.open(DOC, None, None, None, self.report)
        self.assertEqual([a.launch_id for a in launcher._attachments[DOC["document_id"]]], ["B" * 22, "C" * 22])
        launcher.on_document_closed(DOC["document_id"])
        launcher.on_document_closed(DOC["document_id"])
        self.assertEqual([kw["launch_id"] for _, _, kw in released], ["B" * 22, "C" * 22])
        self.assertEqual(launcher._attachments, {})

    def test_scoped_retries_keep_capture_after_reopen_and_signed_refusal_is_final(self):
        for refusal in (False, True):
            self.clock.now = 1000
            launcher = self.launcher()
            self.client.caps = ["attachment_release"]
            calls = []
            def release(doc, reason, **kw):
                calls.append((self.clock(), kw["launch_id"]))
                launcher._generations[doc] = 99
                if not refusal:
                    raise L.LauncherBusy()
                return 0
            self.client.release = release
            launcher._release_closed(DOC["document_id"], 0, self.clock(), "B" * 22, True)
            self.assertEqual([lid for _, lid in calls], ["B" * 22] * (1 if refusal else 4))
            self.assertEqual([round(at - 1000, 3) for at, _ in calls], [0] if refusal else [0, 0.5, 1.5, 3.5])

    def test_new_launcher_old_and_no_capability_plugin_never_get_extra_key(self):
        for caps in (None, [], ["window_follow"]):
            with self.subTest(caps=caps):
                plugin = T.FakePlugin()
                try:
                    plugin.caps = caps
                    worker = L.Launcher(adapter=object(), platform="win32", port=lambda: plugin.port)
                    worker.configure(lambda: T.TOKEN)
                    worker._release_closed(DOC["document_id"], 0, time.monotonic())
                    requests = [r for r in plugin.requests if r["path"] == "/launcher/release"]
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(set(json.loads(requests[0]["body"])), {"document_id", "reason"})
                    before = len(plugin.requests)
                    worker._release_closed(DOC["document_id"], 0, time.monotonic(), "B" * 22, True)
                    self.assertEqual(len(plugin.requests), before + 1, "only hello, never unsafe downgrade")
                finally:
                    plugin.close()

    def test_scoped_close_during_mint_and_outcome_exchange_poll(self):
        scoped = []
        class Client(T.GatedClient):
            caps = ["attachment_release"]
            def release(self, document_id, reason, **kw):
                scoped.append((document_id, reason, kw))
                return super().release(document_id, reason)
        with patch("test_launcher.GatedClient", Client):
            helper = T.CancelInFlight()
            try:
                for step in ("launch", "after_mint", "spawn", "status"):
                    for mode in ("app", "tab"):
                        before = len(scoped)
                        helper.check(step, "close", mode)
                        calls = scoped[before:]
                        self.assertEqual(len(calls), 1, (step, mode, calls))
                        self.assertEqual(calls[0][2], {"launch_id": "B" * 22})
            finally:
                helper.doCleanups()
