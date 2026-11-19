# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""docs/fixtures/ matches scripts/build_demo_fixtures.py byte for byte and holds the data docs/demo.md describes."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "fixtures"
SCRIPT = ROOT / "scripts" / "build_demo_fixtures.py"
NAMES = ["demo-deck.odp", "demo-notes.odt", "demo-sales.ods"]


def content(name: str) -> str:
    with zipfile.ZipFile(FIXTURES / name) as archive:
        return archive.read("content.xml").decode("utf-8")


def texts(xml: str) -> list[str]:
    return re.findall(r"<text:p>([^<]*)</text:p>", xml)


class DemoFixtureTests(unittest.TestCase):
    def test_committed_fixtures_are_exactly_what_the_script_builds(self):
        self.assertEqual(sorted(p.name for p in FIXTURES.iterdir()), NAMES)
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(SCRIPT), "--out-dir", directory], check=True, capture_output=True)
            for name in NAMES:
                self.assertEqual((Path(directory) / name).read_bytes(), (FIXTURES / name).read_bytes(), name)

    def test_odf_packaging(self):
        for name in NAMES:
            with zipfile.ZipFile(FIXTURES / name) as archive:
                first = archive.infolist()[0]
                self.assertEqual(first.filename, "mimetype")
                self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
                self.assertTrue(archive.read("mimetype").startswith(b"application/vnd.oasis.opendocument."))
                self.assertIn("META-INF/manifest.xml", archive.namelist())

    def test_sales_sheet_is_region_units_a1_b5_and_nothing_else(self):
        xml = content("demo-sales.ods")
        self.assertIn('table:name="Sheet1"', xml)
        self.assertEqual(texts(xml), ["Region", "Units", "North", "120", "South", "95", "East", "143", "West", "88"])
        self.assertEqual(re.findall(r'office:value="(\d+)"', xml), ["120", "95", "143", "88"])
        self.assertEqual(xml.count("<table:table-row>"), 5)

    def test_notes_and_deck_match_the_demo_setup(self):
        self.assertEqual(texts(content("demo-notes.odt")), ["Hermes Writer original text."])
        deck = content("demo-deck.odp")
        self.assertEqual(deck.count("<draw:page "), 1)
        self.assertEqual(texts(deck), ["Hermes Impress Proof", "Original slide content"])
        self.assertIn('presentation:style-name="Default-title" presentation:class="title"', deck)

    def test_demo_setup_and_readme_point_at_the_fixtures(self):
        demo = (ROOT / "docs" / "demo.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (demo, readme):
            self.assertIn("scripts/build_demo_fixtures.py", text)
            self.assertIn("demo-sales.ods", text)


class ScenarioFixtureTests(unittest.TestCase):
    def test_scenario_fixtures_are_deterministic_and_never_go_to_docs_fixtures(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            for directory in (one, two):
                subprocess.run([sys.executable, str(SCRIPT), "--scenarios", "--out-dir", directory], check=True, capture_output=True)
            names = sorted(p.name for p in Path(one).iterdir())
            self.assertIn("long.odt", names)
            for name in names:
                self.assertEqual((Path(one) / name).read_bytes(), (Path(two) / name).read_bytes(), name)
        refused = subprocess.run([sys.executable, str(SCRIPT), "--scenarios"], capture_output=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(sorted(p.name for p in FIXTURES.iterdir()), NAMES)

    def test_long_document_hides_its_key_figure_in_a_table_after_paragraph_300(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(SCRIPT), "--scenarios", "--out-dir", directory], check=True, capture_output=True)
            with zipfile.ZipFile(Path(directory) / "long.odt") as archive:
                xml = archive.read("content.xml").decode("utf-8")
        self.assertEqual(xml.count("<text:p>Paragraph "), 600)
        self.assertLess(xml.index("Paragraph 300."), xml.index("Key figure: 4,321"))
        self.assertLess(xml.index("Key figure: 4,321"), xml.index("Paragraph 301."))
        self.assertIn("東京", xml)


if __name__ == "__main__":
    unittest.main()
