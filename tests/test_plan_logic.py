# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import unittest

from plugin.plan_logic import validate_format, validate_plan
from plugin.range_logic import RangeValidationError


class PlanLogicTests(unittest.TestCase):
    def test_accepts_all_outcomes_and_supported_operations(self):
        plans = [
            {"version": 1, "outcome": "inspect", "reason": "inspect", "reads": [
                {"kind": "read_range", "sheet": "Sheet1", "address": "A1:B2"},
                {"kind": "summarize_range_stats", "sheet": "Sheet1", "address": "A1:B2"},
            ]},
            {"version": 1, "outcome": "execute", "summary": "edit", "operations": [
                {"kind": "write_values", "sheet": "Sheet1", "address": "A1:B1", "values": [[1, 2]]},
                {"kind": "write_formula", "sheet": "Sheet1", "address": "C1", "formula": "=SUM(A1:B1)"},
                {"kind": "format_range", "sheet": "Sheet1", "address": "A1:C1", "format": {"bold": True}},
                {"kind": "clear_range", "sheet": "Sheet1", "address": "D1"},
                {"kind": "create_sheet", "name": "New"},
            ], "verification": [
                {"kind": "range_values_equal", "sheet": "Sheet1", "address": "A1:B1", "expectedValues": [[1, 2]]},
                {"kind": "range_formulas_equal", "sheet": "Sheet1", "address": "C1", "expectedFormulas": [["=SUM(A1:B1)"]]},
                {"kind": "sheet_exists", "name": "New"},
                {"kind": "sentinel_unchanged", "sheet": "Sheet1", "address": "Z1", "expected": None},
                {"kind": "range_number_format_equal", "sheet": "Sheet1", "address": "A1:B1", "expectedNumberFormat": "0.00"},
            ]},
            {"version": 1, "outcome": "ask", "message": "question"},
            {"version": 1, "outcome": "refuse", "message": "no"},
        ]
        for plan in plans:
            with self.subTest(outcome=plan["outcome"]):
                self.assertIs(validate_plan(plan), plan)

    def test_whole_plan_rejects_unsupported_operation_before_execution(self):
        plan = {"version": 1, "outcome": "execute", "summary": "bad", "operations": [
            {"kind": "delete_sheet", "name": "Sheet1"},
        ], "verification": [{"kind": "sheet_exists", "name": "Sheet1"}]}
        with self.assertRaisesRegex(RangeValidationError, "unsupported or malformed"):
            validate_plan(plan)

    def test_sentinel_unchanged_uses_v1_scalar_shape(self):
        for rule in (
            {"kind": "sentinel_unchanged", "sheet": "S", "address": "A1:B1", "expected": 1},
            {"kind": "sentinel_unchanged", "sheet": "S", "address": "A1", "expected": [[1]]},
            {"kind": "sentinel_unchanged", "sheet": "S", "address": "A1", "expectedValues": [[1]]},
        ):
            plan = {"version": 1, "outcome": "execute", "summary": "s", "operations": [
                {"kind": "create_sheet", "name": "N"}], "verification": [rule]}
            with self.subTest(rule=rule), self.assertRaises(RangeValidationError):
                validate_plan(plan)

    def test_text_fields_respect_v1_limits(self):
        for plan in (
            {"version": 1, "outcome": "inspect", "reason": "r" * 1025, "reads": [
                {"kind": "read_range", "sheet": "S", "address": "A1"}]},
            {"version": 1, "outcome": "ask", "message": "m" * 2049},
        ):
            with self.subTest(outcome=plan["outcome"]), self.assertRaises(RangeValidationError):
                validate_plan(plan)

    def test_supported_kinds_are_a_subset_of_workbook_plan_v1(self):
        # Embedded copy of the Excel WorkbookPlan V1 enums (hermes-office-excel tool_schemas.json).
        v1_reads = {"read_range", "summarize_range_stats"}
        v1_operations = {
            "write_values", "write_formula", "fill_formula", "format_range", "create_table", "sort_table",
            "filter_table", "reset_table_filter", "create_chart", "set_validation_list", "conditional_format",
            "freeze_panes", "create_sheet", "rename_sheet", "copy_sheet", "add_comment", "calculate",
        }
        v1_verification = {
            "range_values_equal", "range_formulas_equal", "range_formula_pattern", "range_number_format_equal",
            "table_exists", "table_row_order", "table_filter_state", "sheet_exists", "chart_exists",
            "comment_exists", "sentinel_unchanged", "calculation_completed",
        }
        from plugin import plan_logic
        self.assertLessEqual(set(plan_logic.SUPPORTED_READ_KINDS), v1_reads)
        self.assertEqual(set(plan_logic.SUPPORTED_OPERATION_KINDS) - v1_operations, set(plan_logic.LIBREOFFICE_EXTENSION_KINDS))
        self.assertLessEqual(set(plan_logic.SUPPORTED_VERIFICATION_KINDS), v1_verification)

    def test_tool_schema_matches_supported_kinds(self):
        import json
        from pathlib import Path
        from plugin import plan_logic
        schemas = json.loads((Path(__file__).resolve().parents[1] / "plugin" / "tool_schemas.json").read_text())
        plan = [s for s in schemas if s["name"] == "calc_execute_plan"][0]["parameters"]["properties"]["plan"]
        branches = {b["properties"]["outcome"].get("const", "ask/refuse"): b for b in plan["oneOf"]}
        execute = branches["execute"]["properties"]
        ops = {o["properties"]["kind"]["const"] for o in execute["operations"]["items"]["oneOf"]}
        rules = {r["properties"]["kind"]["const"] for r in execute["verification"]["items"]["oneOf"]}
        self.assertEqual(ops, set(plan_logic.SUPPORTED_OPERATION_KINDS))
        self.assertEqual(rules, set(plan_logic.SUPPORTED_VERIFICATION_KINDS))
        sentinel = [r for r in execute["verification"]["items"]["oneOf"] if r["properties"]["kind"]["const"] == "sentinel_unchanged"][0]
        self.assertEqual(set(sentinel["required"]), {"kind", "sheet", "address", "expected"})
        self.assertEqual(branches["inspect"]["properties"]["reason"]["maxLength"], 1024)
        self.assertEqual(execute["summary"]["maxLength"], 2048)

    def test_rejects_unknown_format_key(self):
        with self.assertRaisesRegex(RangeValidationError, "unsupported format keys"):
            validate_format({"mergeCells": True})

    def test_rejects_bad_formula_and_matrix_before_execution(self):
        for operation in (
            {"kind": "write_formula", "sheet": "S", "address": "A1:B1", "formula": "=1"},
            {"kind": "write_values", "sheet": "S", "address": "A1:B1", "values": [[1]]},
        ):
            plan = {"version": 1, "outcome": "execute", "summary": "bad", "operations": [operation],
                    "verification": [{"kind": "sheet_exists", "name": "S"}]}
            with self.subTest(kind=operation["kind"]), self.assertRaises(RangeValidationError):
                validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
