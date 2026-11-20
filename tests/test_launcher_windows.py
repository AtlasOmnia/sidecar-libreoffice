# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Windows launcher adapter (extension/python/launcher_windows.py). In 0.5.0 Windows has no
app window: Open Sidecar opens the bootstrap file in the default browser (os.startfile), and the
launch code is never on a command line. Runs on every system with os.startfile mocked."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "python"
sys.path.insert(0, str(EXTENSION))
import launcher_windows as W  # noqa: E402
import sidecar_launcher as L  # noqa: E402

TOKEN = "t" * 43
URL = "http://127.0.0.1:8661/ui/index.html#launch=" + "A" * 43


class Adapter(unittest.TestCase):
    def test_no_app_window_browser_in_this_version(self):
        self.assertEqual(W.find_browsers(), [])
        with self.assertRaises(OSError):
            W.spawn(["msedge.exe", "--app=file:///x.html"])
        self.assertFalse(W.running(1))
        self.assertFalse(W.raise_pid(1))
        self.assertIsNone(W.terminate(1))

    def test_cache_is_local_app_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(W.cache_dir(), Path(folder))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            with self.assertRaises(OSError):
                W.cache_dir()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "relative\\path"}):
            with self.assertRaises(OSError):
                W.cache_dir()

    def test_open_default_hands_the_file_to_the_shell_association(self):
        target = Path("X:/cache/Sidecar for LibreOffice/launch/launch-x.html")
        with mock.patch.object(W.os, "startfile", create=True) as startfile:
            W.open_default(target)
        startfile.assert_called_once_with(str(target))

    def test_no_native_code(self):
        tree = ast.parse((EXTENSION / "launcher_windows.py").read_text(encoding="utf-8"))
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("ctypes", names)
        self.assertNotIn("subprocess", names)


class WindowsLaunch(unittest.TestCase):
    """The launcher with the real Windows adapter: tab mode, no bounds, the file is opened."""

    def test_tab_mode_with_the_bootstrap_file(self):
        calls, seen, reports = [], [], []

        class Client:
            def __init__(self, port, read_token, timeout=5.0):
                pass

            def hello(self):
                calls.append("hello")
                return {}

            def launch(self, document, host_scheme, mode, bounds):
                calls.append(("launch", mode, bounds))
                return {"window_live": False, "launch_id": "B" * 22, "url": URL}

            def status(self, launch_id):
                calls.append("status")
                return "exchanged"

        def startfile(path):
            seen.append(Path(path).read_bytes())

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
                mock.patch.object(W.os, "startfile", side_effect=startfile, create=True):
            launcher = L.Launcher(adapter=W, platform="win32", port=lambda: 8661, client=Client,
                                  sleep=lambda s: None, start=lambda fn, name: fn())
            launcher.configure(lambda: TOKEN)
            launcher.open({"document_id": "lo:1:2", "title": "a.ods", "type": "calc"}, "light",
                          (0, 0, 1920, 1000), (0, 0, 1920, 1040), lambda text, busy: reports.append(text))
            leftovers = list((Path(folder) / "Sidecar for LibreOffice" / "launch").iterdir())
        self.assertEqual(calls, ["hello", ("launch", "tab", None), "status"])
        self.assertEqual(len(seen), 1)
        self.assertIn(URL.encode(), seen[0])
        self.assertEqual(reports, [L.OPENING, L.OPEN])
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
