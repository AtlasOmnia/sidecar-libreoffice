# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_isolated_install", ROOT / "scripts" / "verify_isolated_install.py")
VERIFY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VERIFY)


class VerifyInstallTests(unittest.TestCase):
    def _fixture(self, directory: Path, installed_text: str) -> tuple[Path, Path]:
        oxt = directory / "sidecar-libreoffice.oxt"
        with zipfile.ZipFile(oxt, "w") as archive:
            archive.writestr("python/hermes_bridge.py", "new")
            archive.writestr("description.xml", "<d/>")
        profile = directory / "profile"
        installed = profile / "user/uno_packages/cache/uno_packages/lu1.tmp_/sidecar-libreoffice.oxt"
        (installed / "python").mkdir(parents=True)
        (installed / "python/hermes_bridge.py").write_text(installed_text)
        (installed / "description.xml").write_text("<d/>")
        return oxt, profile

    def test_matching_install_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            oxt, profile = self._fixture(Path(directory), "new")
            dirs = VERIFY.installed_dirs(profile, oxt.name)
            self.assertEqual(len(dirs), 1)
            expected = VERIFY.member_hashes(oxt)
            self.assertEqual(set(expected), {"python/hermes_bridge.py", "description.xml"})

    def test_stale_install_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            oxt, profile = self._fixture(Path(directory), "old")
            import sys
            argv = sys.argv
            sys.argv = ["verify", str(oxt), "--profile", str(profile)]
            try:
                self.assertEqual(VERIFY.main(), 3)
                (profile / "user/uno_packages/cache/uno_packages/lu1.tmp_/sidecar-libreoffice.oxt/python/hermes_bridge.py").write_text("new")
                self.assertEqual(VERIFY.main(), 0)
            finally:
                sys.argv = argv


if __name__ == "__main__":
    unittest.main()
