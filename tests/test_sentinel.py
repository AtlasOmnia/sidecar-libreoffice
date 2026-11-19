# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.real_profile_sentinel import differences, snapshot

ROOT = Path(__file__).resolve().parents[1]


class RealProfileSentinelTests(unittest.TestCase):
    def test_snapshot_records_files_directories_and_symlinks_without_following(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "folder").mkdir()
            (root / "folder" / "data.txt").write_text("hello", encoding="utf-8")
            (root / "outside").symlink_to(Path(directory).parent)
            value = snapshot(root)
            self.assertEqual(value["folder"]["type"], "directory")
            self.assertEqual(value["folder/data.txt"]["type"], "file")
            self.assertEqual(len(value["folder/data.txt"]["sha256"]), 64)
            self.assertEqual(value["outside"]["type"], "symlink")
            self.assertFalse(any(key.startswith("outside/") for key in value))

    def test_differences_detects_added_removed_and_changed(self):
        diff = differences({"a": {"size": 1}, "b": {}}, {"a": {"size": 2}, "c": {}})
        self.assertEqual([item["path"] for item in diff], ["a", "b", "c"])

    def test_cli_compare_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            after = root / "after.json"
            before.write_text(json.dumps({"x": 1}), encoding="utf-8")
            after.write_text(json.dumps({"x": 1}), encoding="utf-8")
            command = [sys.executable, str(ROOT / "scripts/real_profile_sentinel.py"), "compare", str(before), str(after)]
            self.assertEqual(subprocess.run(command, check=False, capture_output=True).returncode, 0)
            after.write_text(json.dumps({"x": 2}), encoding="utf-8")
            changed = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn('"status": "CHANGED"', changed.stdout)

    def test_guarded_run_preserves_child_status_when_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = [
                sys.executable,
                str(ROOT / "scripts/guarded_run.py"),
                "--root",
                str(root),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(7)",
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertIn('"status": "UNCHANGED"', result.stdout)


if __name__ == "__main__":
    unittest.main()
