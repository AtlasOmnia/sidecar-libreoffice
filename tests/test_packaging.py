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
PLACEHOLDER = "D-FINAL-OXT-SHA256"


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
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        plugin_readme = (ROOT / "plugin" / "README.md").read_text(encoding="utf-8")
        if PLACEHOLDER in readme or PLACEHOLDER in plugin_readme:
            # The draft keeps one placeholder in each README until the integrated build is frozen.
            self.assertEqual((readme.count(PLACEHOLDER), plugin_readme.count(PLACEHOLDER)), (1, 1))
            self.assertIn(f"Expected SHA-256 of `sidecar-libreoffice.oxt` for v0.5.1: `{PLACEHOLDER}`", readme)
            self.assertTrue("## [0.5.1] - Unreleased" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                            "fill the .oxt SHA-256 in README.md and plugin/README.md before dating 0.5.1")
            self.skipTest("the .oxt SHA-256 is a D-final placeholder")
        documented = BUILD.documented_sha256()
        with tempfile.TemporaryDirectory() as directory:
            digest = hashlib.sha256(build(Path(directory) / "a.oxt")).hexdigest()
        self.assertEqual(digest, documented, "a change to extension/, LICENSE or NOTICE needs the new "
                                             "SHA-256 in README.md and plugin/README.md")
        self.assertIn(f"`{documented}`", plugin_readme)

    @unittest.skipUnless((ROOT / "extension" / "python" / "sidecar_launcher.py").is_file(),
                         "the launcher files land with the extension lane")
    def test_the_archive_holds_exactly_the_files_the_protocol_lists(self):
        """docs/web-protocol.md section 8.1 is the frozen .oxt file list for 0.5.1."""
        protocol = (ROOT / "docs" / "web-protocol.md").read_text(encoding="utf-8")
        section = protocol[protocol.index("### 8.1"):protocol.index("### 8.2")]
        block = section[section.index("```\n") + 4:section.index("\n```", section.index("```\n") + 4)]
        listed = {line.split()[0] for line in block.splitlines() if line.strip()}
        self.assertEqual(set(BUILD.FILES + BUILD.ROOT_FILES), listed)

    def test_check_allows_only_a_consistent_unreleased_draft(self):
        marker = "D-FINAL-OXT-SHA256"
        expected = f"Expected SHA-256 of `sidecar-libreoffice.oxt` for v0.5.1: `{marker}`"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin").mkdir()
            readme = root / "README.md"
            readme.write_text(expected, encoding="utf-8")
            (root / "plugin/README.md").write_text(expected, encoding="utf-8")
            (root / "CHANGELOG.md").write_text("## [0.5.1] - Unreleased", encoding="utf-8")
            with mock.patch.object(BUILD, "ROOT", root), mock.patch.object(BUILD, "README", readme), \
                    mock.patch.object(BUILD, "SOURCE", ROOT / "extension"), \
                    mock.patch.object(BUILD, "ROOT_FILES", []), \
                    mock.patch.object(BUILD, "OUTPUT", root / "draft.oxt"), mock.patch("builtins.print"):
                BUILD.main(["--check"])
                (root / "CHANGELOG.md").write_text("## [0.5.1] - 2026-11-21", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    BUILD.main(["--check"])
                (root / "CHANGELOG.md").write_text("## [0.5.1] - Unreleased", encoding="utf-8")
                (root / "plugin/README.md").write_text("missing placeholder", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    BUILD.main(["--check"])

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
            self.assertIn("--branch v0.5.0", text)
            self.assertIn("Copy Sidecar pairing token", text)
            self.assertNotIn("Copy Hermes pairing token", text)
            self.assertIn("Open Sidecar", text)
            self.assertNotIn("Hermes is not answering on 127.0.0.1:8661", text)  # the 0.4.0 panel's chat-off reply
        self.assertIn("## Risk disclosures", readme)
        for fact in ("127.0.0.1:8660", "127.0.0.1:8661", "API_SERVER_KEY", "codex_app_server",
                     "/launcher/", "/ui/", "/api/", "`/chat`", "Sidecar for LibreOffice/browser",
                     "marked", "DOMPurify", "VENDOR.json"):
            self.assertIn(fact, readme)

    @unittest.skipUnless((ROOT / "plugin" / "sidebar" / "vendor").is_dir(), "the page lands with the web UI lane")
    def test_the_vendored_libraries_ship_with_their_licences_and_match_notice(self):
        vendor = ROOT / "plugin" / "sidebar" / "vendor"
        for name in ("marked.umd.js", "marked.LICENSE.md", "purify.js", "dompurify.LICENSE", "VENDOR.json"):
            self.assertTrue((vendor / name).is_file(), name)
        self.assertIn("Christopher Jeffrey", (vendor / "marked.LICENSE.md").read_text(encoding="utf-8"))
        self.assertIn("Apache License", (vendor / "dompurify.LICENSE").read_text(encoding="utf-8"))
        record = (vendor / "VENDOR.json").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for package, version in (("marked", "15.0.12"), ("DOMPurify", "3.4.12")):
            self.assertIn(f'"{version}"', record)
            self.assertIn(f"{package} {version}", notice)


if __name__ == "__main__":
    unittest.main()
