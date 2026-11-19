# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""docs/demo.md stays inside the time budget and its Writer prompt stays grounded in the sheet."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

DEMO = (Path(__file__).resolve().parents[1] / "docs" / "demo.md").read_text(encoding="utf-8")


def seconds(stamp: str) -> int:
    minutes, secs = stamp.split(":")
    return int(minutes) * 60 + int(secs)


class DemoScriptTests(unittest.TestCase):
    def test_shot_list_is_contiguous_and_at_most_85_seconds(self):
        spans = [(seconds(a), seconds(b)) for a, b in re.findall(r"^\| (\d:\d\d)–(\d:\d\d) \|", DEMO, re.M)]
        self.assertGreaterEqual(len(spans), 5)
        self.assertEqual(spans[0][0], 0)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertEqual(end, start)
        self.assertLessEqual(spans[-1][1], 85)

    def test_writer_prompt_names_no_figures_and_asks_for_a_paragraph(self):
        prompt = DEMO.split("Writer (new;", 1)[1].split("\n> ", 1)[1].split("\n", 1)[0]
        self.assertNotRegex(prompt, r"\d{2,}")  # no 446, 143, ... : the numbers must come from the sheet
        self.assertIn("D1:E4", prompt)
        self.assertIn("as_paragraph", prompt)

    def test_undo_beat_uses_the_tool(self):
        self.assertIn("`libreoffice_undo` runs", DEMO)


if __name__ == "__main__":
    unittest.main()
