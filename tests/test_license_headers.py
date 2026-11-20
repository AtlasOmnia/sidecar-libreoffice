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
# The Sidecar page's own files carry the header too. Vendored libraries and the three Omnia
# stylesheets pinned in plugin/sidebar/UPSTREAM.json stay byte-identical to upstream (no header).
SIDEBAR = ("plugin", "sidebar")
SIDEBAR_SUFFIXES = {".js", ".css", ".html", ".svg"}
UPSTREAM_COPIES = {"omnia-tokens.css", "omnia-pane.css", "taskpane.css"}


def needs_header(relative: Path) -> bool:
    if relative.suffix in SOURCE_SUFFIXES:
        return True
    parts = relative.parts
    return (parts[:2] == SIDEBAR and relative.suffix in SIDEBAR_SUFFIXES and "vendor" not in parts[2:]
            and relative.name not in UPSTREAM_COPIES)


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
            if not path.is_file() or not needs_header(path.relative_to(ROOT)):
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

    def test_notice_names_the_vendored_libraries_with_their_licences(self):
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for line in ("marked 15.0.12", "Copyright (c) 2011-2018, Christopher Jeffrey", "MIT License",
                     "plugin/sidebar/vendor/marked.LICENSE.md", "DOMPurify 3.4.12", "Cure53",
                     "used here under the Apache License 2.0", "plugin/sidebar/vendor/dompurify.LICENSE"):
            self.assertIn(line, notice)

    def test_header_rule_covers_the_pages_own_files_but_not_upstream_copies(self):
        self.assertTrue(needs_header(Path("plugin/sidebar/app.js")))
        self.assertTrue(needs_header(Path("plugin/sidebar/index.html")))
        self.assertTrue(needs_header(Path("plugin/sidebar/sidecar-lo.css")))
        self.assertTrue(needs_header(Path("plugin/sidebar/assets/icon.svg")))
        self.assertFalse(needs_header(Path("plugin/sidebar/vendor/purify.js")))
        self.assertFalse(needs_header(Path("plugin/sidebar/omnia-pane.css")))
        self.assertFalse(needs_header(Path("docs/assets/screenshots/x.css")))


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
