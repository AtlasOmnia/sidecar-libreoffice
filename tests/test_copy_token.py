# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Tools > Copy Sidecar pairing token must never raise into LibreOffice's event loop."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension_bridge import BRIDGE  # noqa: E402


class Clipboard:
    def __init__(self, fail=False):
        self.fail = fail
        self.contents = None

    def setContents(self, transferable, _owner):
        if self.fail:
            raise RuntimeError("clipboard unavailable")
        self.contents = transferable


class Toolkit:
    def __init__(self, fail=False):
        self.fail = fail
        self.boxes = []

    def createMessageBox(self, parent, kind, buttons, title, text):
        if self.fail:
            raise RuntimeError("no message box")
        self.boxes.append((parent, kind, text))
        return types.SimpleNamespace(execute=lambda: 1)


class Context:
    def __init__(self, clipboard, toolkit, frame=True):
        window = object()
        frame_obj = types.SimpleNamespace(getContainerWindow=lambda: window) if frame else None
        self.window = window
        desktop = types.SimpleNamespace(getCurrentFrame=lambda: frame_obj)
        services = {
            "com.sun.star.datatransfer.clipboard.SystemClipboard": clipboard,
            "com.sun.star.awt.Toolkit": toolkit,
            "com.sun.star.frame.Desktop": desktop,
        }
        self.manager = types.SimpleNamespace(createInstanceWithContext=lambda name, _ctx: services[name])

    def getServiceManager(self):
        return self.manager


class CopyTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._load, self._dir = BRIDGE.load_startup_config, BRIDGE.profile_bridge_dir
        BRIDGE.load_startup_config = lambda _ctx: (self.dir, "test-token-value", 8660, None)
        BRIDGE.profile_bridge_dir = lambda _ctx: self.dir
        self.url = types.SimpleNamespace(Complete=BRIDGE.COPY_TOKEN_URL)

    def tearDown(self):
        BRIDGE.load_startup_config, BRIDGE.profile_bridge_dir = self._load, self._dir
        self.tmp.cleanup()

    def job(self, ctx):
        job = BRIDGE.HermesBridgeJob.__new__(BRIDGE.HermesBridgeJob)
        job.ctx = ctx
        return job

    def test_copies_token_and_shows_info_box_on_document_window(self):
        clipboard, toolkit = Clipboard(), Toolkit()
        ctx = Context(clipboard, toolkit)
        self.job(ctx).dispatch(self.url, ())
        self.assertEqual(clipboard.contents.text, "test-token-value")
        self.assertEqual(toolkit.boxes, [(ctx.window, ("com.sun.star.awt.MessageBoxType", "INFOBOX"),
                                          "Pairing token copied to the clipboard.")])
        self.assertNotIn("test-token-value", toolkit.boxes[0][2])

    def test_failures_never_escape(self):
        for clipboard, toolkit, frame in ((Clipboard(fail=True), Toolkit(), True),
                                          (Clipboard(), Toolkit(fail=True), True),
                                          (Clipboard(), Toolkit(), False)):
            self.job(Context(clipboard, toolkit, frame)).dispatch(self.url, ())  # must not raise
        log = (self.dir / "bridge.log").read_text(encoding="utf-8")
        self.assertIn("copy pairing token failed: RuntimeError", log)
        self.assertIn("pairing token notice failed: RuntimeError", log)
        self.assertNotIn("test-token-value", log)

    def test_other_urls_ignored(self):
        clipboard = Clipboard()
        self.job(Context(clipboard, Toolkit())).dispatch(types.SimpleNamespace(Complete="vnd.other:x"), ())
        self.assertIsNone(clipboard.contents)


if __name__ == "__main__":
    unittest.main()
