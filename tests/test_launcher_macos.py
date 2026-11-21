# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The macOS launcher adapter (extension/python/launcher_macos.py): fixed browser paths, the
cleaned and detached spawn, the D1 default-browser path, raise by PID through osascript, and
terminate. No native code is loaded into LibreOffice."""
from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "python"
sys.path.insert(0, str(EXTENSION))
import launcher_macos as M  # noqa: E402


def make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    os.chmod(path, 0o755)


class FindBrowsers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.system = Path(self.temp.name) / "Applications"
        self.user = Path(self.temp.name) / "home" / "Applications"
        patcher = mock.patch.object(M, "roots", lambda: [self.system, self.user])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preference_order_and_fixed_paths(self):
        make_executable(self.user / "Chromium.app/Contents/MacOS/Chromium")
        make_executable(self.system / "Brave Browser.app/Contents/MacOS/Brave Browser")
        make_executable(self.system / "Google Chrome.app/Contents/MacOS/Google Chrome")
        make_executable(self.user / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        self.assertEqual(M.find_browsers(), [
            ("Google Chrome", str(self.system / "Google Chrome.app/Contents/MacOS/Google Chrome")),
            ("Microsoft Edge", str(self.user / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
            ("Brave", str(self.system / "Brave Browser.app/Contents/MacOS/Brave Browser")),
            ("Chromium", str(self.user / "Chromium.app/Contents/MacOS/Chromium")),
        ])

    @unittest.skipIf(os.name == "nt", "POSIX execute bits (the macOS adapter runs only on macOS)")
    def test_system_applications_win_and_non_executables_are_skipped(self):
        make_executable(self.system / "Google Chrome.app/Contents/MacOS/Google Chrome")
        make_executable(self.user / "Google Chrome.app/Contents/MacOS/Google Chrome")
        plain = self.system / "Chromium.app/Contents/MacOS/Chromium"
        plain.parent.mkdir(parents=True)
        plain.write_text("")
        os.chmod(plain, 0o644)
        self.assertEqual(M.find_browsers(), [("Google Chrome", str(self.system / "Google Chrome.app/Contents/MacOS/Google Chrome"))])

    def test_no_browser(self):
        self.assertEqual(M.find_browsers(), [])


class DefaultPaths(unittest.TestCase):
    def test_default_roots(self):
        self.assertEqual(M.roots(), [Path("/Applications"), Path.home() / "Applications"])
        self.assertEqual(M.cache_dir(), Path.home() / "Library" / "Caches")


class Processes(unittest.TestCase):
    def test_environment_drops_libreoffice_runtime_variables(self):
        env = {"PATH": "/bin", "PYTHONHOME": "x", "PYTHONPATH": "y", "DYLD_LIBRARY_PATH": "z",
               "URE_BOOTSTRAP": "u", "SAL_USE_VCLPLUGIN": "s", "HOME": "/h", "LANG": "en"}
        self.assertEqual(M.clean_environment(env), {"PATH": "/bin", "HOME": "/h", "LANG": "en"})

    def test_spawn_is_detached_without_a_shell(self):
        process = mock.Mock(pid=4242)
        with mock.patch.object(M.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.dict(os.environ, {"PYTHONPATH": "x", "SAL_X": "y"}):
            self.assertEqual(M.spawn(["/Apps/Chrome", "--app=file:///x.html"]), 4242)
        args, kwargs = popen.call_args
        self.assertEqual(args, (["/Apps/Chrome", "--app=file:///x.html"],))
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIs(kwargs["close_fds"], True)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("SAL_X", kwargs["env"])
        M._CHILDREN.pop(4242, None)

    @unittest.skipIf(os.name == "nt", "POSIX process")
    def test_running_and_terminate_track_only_spawned_children(self):
        pid = M.spawn(["/bin/sleep", "30"])
        self.addCleanup(lambda: M._CHILDREN.pop(pid, None))
        self.assertTrue(M.running(pid))
        self.assertNotEqual(os.getsid(pid), os.getsid(0))  # its own session
        self.assertFalse(M.running(os.getpid()))
        M.terminate(os.getpid())  # not ours: nothing happens
        M.terminate(pid)
        self.assertFalse(M.running(pid))
        self.assertEqual(M._CHILDREN[pid].returncode, -15)

    def test_open_default_uses_usr_bin_open(self):
        with mock.patch.object(M.subprocess, "run") as run:
            M.open_default(PurePosixPath("/c/launch-" + "0" * 32 + ".html"))  # a macOS path on any test host
        args, kwargs = run.call_args
        self.assertEqual(args, (["/usr/bin/open", "/c/launch-" + "0" * 32 + ".html"],))
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], True)
        self.assertEqual(kwargs["timeout"], M.OPEN_TIMEOUT_SECONDS)


class RaiseByPid(unittest.TestCase):
    def setUp(self):
        M._CHILDREN[777] = mock.Mock(poll=lambda: None)
        self.addCleanup(lambda: M._CHILDREN.pop(777, None))

    def run_with(self, stdout=b"raised", returncode=0, error=None):
        result = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b"")
        with mock.patch.object(M.subprocess, "run", side_effect=error, return_value=result) as run:
            value = M.raise_pid(777)
        return value, run

    def test_raises_through_osascript_with_the_pid_as_an_argument(self):
        value, run = self.run_with()
        self.assertTrue(value)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/osascript", "-l", "JavaScript", "-e", M.RAISE_SCRIPT, "777"])
        self.assertIs(kwargs["shell"], False)
        self.assertNotIn("777", M.RAISE_SCRIPT)
        self.assertIn("runningApplicationWithProcessIdentifier", M.RAISE_SCRIPT)

    def test_refused_missing_failed_or_timed_out_is_false(self):
        self.assertFalse(self.run_with(stdout=b"refused")[0])
        self.assertFalse(self.run_with(stdout=b"missing")[0])
        self.assertFalse(self.run_with(returncode=1)[0])
        self.assertFalse(self.run_with(error=subprocess.TimeoutExpired("osascript", 5))[0])
        self.assertFalse(self.run_with(error=FileNotFoundError())[0])

    def test_only_a_browser_this_libreoffice_started_and_the_native_switch(self):
        with mock.patch.object(M.subprocess, "run") as run:
            self.assertFalse(M.raise_pid(os.getpid()))
            with mock.patch.dict(os.environ, {"HERMES_LO_SIDECAR_NO_NATIVE": "1"}):
                self.assertFalse(M.raise_pid(777))
        run.assert_not_called()

    def test_no_native_code_in_libreoffice(self):
        tree = ast.parse((EXTENSION / "launcher_macos.py").read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree)
                                                if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("ctypes", imported)
        self.assertNotIn("objc", imported)
        self.assertNotIn("shell=True", (EXTENSION / "launcher_macos.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
