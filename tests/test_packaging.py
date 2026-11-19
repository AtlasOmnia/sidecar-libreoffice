# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""What the distributed packages contain: the .oxt build and the installed plugin directory."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_oxt", ROOT / "scripts" / "build_oxt.py")
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)


def build(out: Path) -> bytes:
    with mock.patch.object(BUILD, "OUTPUT", out), mock.patch("builtins.print"):
        BUILD.main()
    return out.read_bytes()


class OxtPackageTests(unittest.TestCase):
    def test_the_archive_carries_license_and_notice_and_only_the_listed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "sidecar-libreoffice.oxt"
            build(out)
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                self.assertEqual(names, BUILD.FILES + ["LICENSE", "NOTICE"])
                self.assertEqual(archive.read("LICENSE"), (ROOT / "LICENSE").read_bytes())
                self.assertEqual(archive.read("NOTICE"), (ROOT / "NOTICE").read_bytes())

    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            first = build(Path(directory) / "a.oxt")
            second = build(Path(directory) / "b.oxt")
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())

    def test_members_are_stored_so_the_bytes_do_not_depend_on_zlib_or_the_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            normal = build(Path(directory) / "a.oxt")
            with mock.patch.object(zipfile, "zlib", None):  # any deflate would now raise
                without_zlib = build(Path(directory) / "b.oxt")
            with mock.patch.object(sys, "platform", "win32"):  # ZipInfo's Windows defaults
                as_windows = build(Path(directory) / "c.oxt")
            with zipfile.ZipFile(Path(directory) / "a.oxt") as archive:
                infos = archive.infolist()
        self.assertEqual(without_zlib, normal)
        self.assertEqual(as_windows, normal)
        self.assertEqual(len(infos), len(BUILD.FILES) + 2)
        for info in infos:
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED, info.filename)
            self.assertEqual(info.compress_size, info.file_size, info.filename)
            self.assertEqual(info.create_system, 3, info.filename)
            self.assertEqual(info.date_time, (2026, 1, 1, 0, 0, 0), info.filename)
            self.assertEqual(info.external_attr, 0o100644 << 16, info.filename)

    def test_the_build_matches_the_sha256_documented_in_both_readmes(self):
        documented = BUILD.documented_sha256()
        with tempfile.TemporaryDirectory() as directory:
            digest = hashlib.sha256(build(Path(directory) / "a.oxt")).hexdigest()
        self.assertEqual(digest, documented, "a change to extension/, LICENSE or NOTICE needs the new "
                                             "SHA-256 in README.md and plugin/README.md")
        self.assertIn(f"`{documented}`", (ROOT / "plugin" / "README.md").read_text(encoding="utf-8"))

    def test_the_built_file_and_extension_identifier_use_atlas_omnia_names(self):
        self.assertEqual(BUILD.OUTPUT.name, "sidecar-libreoffice.oxt")
        description = (ROOT / "extension" / "description.xml").read_text(encoding="utf-8")
        self.assertIn('<identifier value="com.atlasomnia.sidecar.libreoffice"/>', description)
        for path in (ROOT / "extension").rglob("*"):
            if path.is_file() and path.suffix in {".xml", ".xcu", ".xdl", ".py", ".txt"}:
                text = path.read_text(encoding="utf-8")
                for old in ("dev.hermes.", "vnd.hermes.", "HermesDeck", "HermesChatPanel", "HermesSidebar"):
                    self.assertNotIn(old, text, path)


class PluginPackageTests(unittest.TestCase):
    """A catalog install copies only plugin/, so the license, README and after-install note live there."""

    def test_plugin_directory_carries_license_notice_readme_and_after_install(self):
        self.assertEqual((ROOT / "plugin" / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertEqual((ROOT / "plugin" / "NOTICE").read_bytes(), (ROOT / "NOTICE").read_bytes())
        readme = (ROOT / "plugin" / "README.md").read_text(encoding="utf-8")
        after = (ROOT / "plugin" / "after-install.md").read_text(encoding="utf-8")
        for text in (readme, after):
            self.assertIn("--branch v0.4.0", text)
            self.assertIn("Copy Sidecar pairing token", text)
            self.assertNotIn("Copy Hermes pairing token", text)
        self.assertIn("## Risk disclosures", readme)
        for fact in ("127.0.0.1:8660", "127.0.0.1:8661", "API_SERVER_KEY", "codex_app_server"):
            self.assertIn(fact, readme)


if __name__ == "__main__":
    unittest.main()
