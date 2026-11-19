# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""scripts/check_guidance_tools.py: the guidance names only real tools, arguments, plan kinds and result fields."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_guidance_tools as audit  # noqa: E402


class GuidanceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tools = audit.load_tools()
        self.plan = audit.plan_vocabulary()
        self.results = audit.result_fields()

    def test_bundled_guidance_passes_and_covers_every_registered_tool(self):
        files = audit.guidance_files()
        self.assertEqual(len(files), 7)
        report = audit.audit(files, self.tools, self.plan, self.results)
        self.assertEqual(report["unknown"], [])
        self.assertEqual(report["unmentioned_tools"], [])
        self.assertEqual(report["mentioned_tools"], report["registered_tools"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(audit.main([]), 0)
        self.assertTrue(output.getvalue().rstrip().endswith("OK"))

    def test_absent_internal_and_development_names_fail(self):
        text = ("Use `calc_set_range` with `if_match`, a `write_values` plan step and check `title_set`.\n"
                "Never `calc_write_literal` (internal), `dev_select_text` (development only),\n"
                "`calc_create_pivot` (absent) or `made_up_field`.\n"
                "```\nimpress_reorder_slide(slide=2)\n```\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_text(text, encoding="utf-8")
            report = audit.audit([path], self.tools, self.plan, self.results)
        unknown = {item["name"]: item["reason"] for item in report["unknown"]}
        self.assertEqual(set(unknown), {"calc_write_literal", "dev_select_text", "calc_create_pivot", "made_up_field",
                                        "impress_reorder_slide"})
        self.assertEqual(unknown["calc_write_literal"], "not a registered tool")
        self.assertIn("calc_get_overview", report["unmentioned_tools"])
        self.assertNotIn("calc_set_range", report["unmentioned_tools"])

    def test_a_result_field_must_exist_in_the_bridge_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "ops.py"
            source.write_text('return {"title_set": True}\n', encoding="utf-8")
            self.assertEqual(audit.result_fields([source]), {"title_set"})

    def test_schema_enum_values_and_nested_properties_count_as_documented(self):
        names = audit.documented_arguments(self.tools)
        for name in ("title_two_content", "rounded_rectangle", "not_empty", "font_size", "whole_number"):
            self.assertIn(name, names)
        self.assertIn("summarize_range_stats", self.plan)
        self.assertIn("sentinel_unchanged", self.plan)


if __name__ == "__main__":
    unittest.main()
