# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".xcu", ".xml", ".yaml", ".sh"}
EXCLUDED_PARTS = {".git", "dist", "__pycache__"}
SPDX = "SPDX-License-Identifier: Apache-2.0"
COPYRIGHT = "Copyright 2026 Atlas Omnia"


def tracked_files() -> list[Path]:
    """The files git tracks. Untracked directories (a .venv, a scratch folder) are never walked."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT, check=True, capture_output=True).stdout
    return [ROOT / name for name in sorted(set(listed.decode("utf-8").split("\0"))) if name]


class LicenseHeaderTests(unittest.TestCase):
    def test_commentable_source_files_have_spdx_headers(self):
        try:
            files = tracked_files()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        missing = []
        for path in files:
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts):
                continue
            prefix = path.read_text(encoding="utf-8")[:512]
            if SPDX not in prefix or COPYRIGHT not in prefix:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [])

    def test_license_is_canonical_apache_2(self):
        import hashlib

        digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
        self.assertEqual(digest, "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")

    def test_notice_contains_attribution(self):
        self.assertIn(
            "Sidecar for LibreOffice — Copyright 2026 Atlas Omnia",
            (ROOT / "NOTICE").read_text(encoding="utf-8"),
        )


class UntrackedDirectoryTests(unittest.TestCase):
    def test_an_untracked_tool_directory_is_not_walked(self):
        """A local .venv or scratch folder full of third-party .py files must not fail the suite."""
        import shutil
        import tempfile

        probe = Path(tempfile.mkdtemp(prefix=".venv-spdx-probe-", dir=ROOT))
        try:
            (probe / "lib").mkdir()
            (probe / "lib" / "third_party.py").write_text("print('no header here')\n", encoding="utf-8")
            result = unittest.TestResult()
            LicenseHeaderTests("test_commentable_source_files_have_spdx_headers").run(result)
            self.assertEqual((len(result.failures), len(result.errors)), (0, 0),
                             [text[-300:] for _test, text in result.failures + result.errors])
        finally:
            shutil.rmtree(probe)


if __name__ == "__main__":
    unittest.main()
