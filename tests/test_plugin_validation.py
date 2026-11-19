# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The plugin's argument check, as Hermes reaches it: through each registered tool's handler.

One known-valid call per registered tool must reach the bridge exactly once with the right
operation and mutation flag; invalid calls must come back as a Validation result with no bridge
call; and for the operator-dependent tools the plugin must accept exactly what the bridge accepts.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

import plugin  # noqa: E402

BRIDGE = FAKES.BRIDGE
D = "lo:1"
FP64 = "0" * 64
CF_OPERATORS = ("greater", "greater_equal", "less", "less_equal", "equal", "not_equal", "between", "not_between", "formula")
FILTER_OPERATORS = ("equals", "not_equals", "greater", "greater_equal", "less", "less_equal", "contains", "not_contains",
                    "begins_with", "ends_with", "not_empty")


class Recording:
    calls: list[tuple[str, dict, bool]] = []

    def __init__(self, _port, _token):
        pass

    def invoke(self, op, args, *, mutation=False):
        Recording.calls.append((op, args, mutation))
        return {"ok": True}


def call(name, args):
    Recording.calls = []
    with mock.patch.object(plugin, "BridgeClient", Recording), mock.patch.object(plugin, "_secret", lambda: "t" * 48):
        return json.loads(plugin._handler_for(name, 8660)(args))


def cf(operator, value1, value2=None, **extra):
    args = {"document_id": D, "sheet": "S", "address": "B2:B5", "operator": operator, "value1": value1, "style": "Good", **extra}
    if value2 is not None:
        args["value2"] = value2
    return args


def flt(*conditions, **extra):
    return {"document_id": D, "sheet": "S", "address": "A1:C9", "has_header": True, "conditions": list(conditions), **extra}


def val(**rule):
    return {"document_id": D, "sheet": "S", "address": "B2:B5", **rule}


PLAN_EXECUTE = {"version": 1, "outcome": "execute", "summary": "edit",
                "operations": [{"kind": "write_values", "sheet": "Sheet1", "address": "A1:B1", "values": [[1, 2.5]]}],
                "verification": [{"kind": "range_values_equal", "sheet": "Sheet1", "address": "A1:B1", "expectedValues": [[1, 2.5]]}]}

# One valid call per registered tool: (tool, arguments, bridge operation, mutation).
VALID = [
    ("calc_get_overview", {}, "get_overview", False),
    ("calc_get_range", {"document_id": D, "sheet": "S", "address": "A1:B2"}, "get_range", False),
    ("calc_set_range", {"document_id": D, "sheet": "S", "address": "A1:D1", "values": [["a", 1.5, True, None]], "if_match": FP64},
     "set_range", True),
    ("calc_import_csv", {"document_id": D, "sheet": "S", "start_cell": "A1", "csv_text": "a,b\n1,2", "typing": "numbers"},
     "calc_write_literal", True),
    ("calc_sort_range", {"document_id": D, "sheet": "S", "address": "A1:C9", "has_header": True, "keys": [{"column": "B", "descending": True}]},
     "calc_sort_range", True),
    ("calc_filter_range", flt({"column": "B", "operator": "greater", "value": 1}), "calc_filter_range", True),
    ("calc_create_chart", {"document_id": D, "sheet": "S", "address": "A1:C5", "type": "column", "anchor_cell": "E2", "width_cm": 12.5},
     "calc_create_chart", True),
    ("calc_list_charts", {"document_id": D, "sheet": "S"}, "calc_list_charts", False),
    ("calc_get_rules", {"document_id": D, "sheet": "S", "address": "B2:B5"}, "calc_get_rules", False),
    ("calc_add_conditional_format", cf("greater", 10), "calc_add_conditional_format", True),
    ("calc_set_validation", val(type="whole_number", operator="between", value1=0, value2=100), "calc_set_validation", True),
    ("calc_freeze_panes", {"document_id": D, "sheet": "S", "columns": 0, "rows": 1}, "calc_freeze_panes", True),
    ("calc_protect_sheet", {"document_id": D, "sheet": "S", "protect": True}, "calc_protect_sheet", True),
    ("calc_insert_table", {"document_id": D, "sheet": "S", "address": "A1:C5", "name": "Sales", "style": "Academic"}, "calc_insert_table", True),
    ("calc_activate_sheet", {"document_id": D, "sheet": "S"}, "calc_activate_sheet", True),
    ("calc_recalculate", {"document_id": D, "all": False}, "calc_recalculate", True),
    ("calc_format_range", {"document_id": D, "sheet": "S", "address": "A1:B2", "format": {"bold": True, "fontSize": 11.5}}, "format_range", True),
    ("calc_clear_range", {"document_id": D, "sheet": "S", "address": "A1:B2"}, "clear_range", True),
    ("calc_create_sheet", {"document_id": D, "name": "New"}, "create_sheet", True),
    ("calc_execute_plan", {"document_id": D, "plan": PLAN_EXECUTE}, "execute_plan", True),
    ("writer_get_text", {"document_id": D, "offset": 1, "limit": 50}, "writer_get_text", False),
    ("writer_insert_text", {"document_id": D, "text": "Hello", "position": "end", "as_paragraph": True}, "writer_insert_text", True),
    ("writer_insert_paragraphs", {"document_id": D, "position": "after", "after_paragraph": 2, "expected_text": "",
                                  "paragraphs": [{"text": "a", "style": "Heading 1"}, {"text": ""}]}, "writer_insert_paragraphs", True),
    ("impress_list_slides", {"document_id": D}, "impress_list_slides", False),
    ("libreoffice_undo", {"document_id": D, "steps": 1}, "undo", True),
    ("calc_rename_sheet", {"document_id": D, "sheet": "S", "new_name": "T"}, "calc_rename_sheet", True),
    ("calc_copy_sheet", {"document_id": D, "sheet": "S", "new_name": "T"}, "calc_copy_sheet", True),
    ("calc_delete_sheet", {"document_id": D, "sheet": "S"}, "calc_delete_sheet", True),
    ("calc_find_replace", {"document_id": D, "sheet": "S", "search": "a", "replace": "", "whole_cell": True}, "calc_find_replace", True),
    ("calc_autofill", {"document_id": D, "sheet": "S", "address": "A1:A9", "direction": "down", "source_count": 2}, "calc_autofill", True),
    ("calc_range_stats", {"document_id": D, "sheet": "S", "address": "A1:B9"}, "calc_range_stats", False),
    ("calc_add_comment", {"document_id": D, "sheet": "S", "cell": "B2", "text": "note"}, "calc_add_comment", True),
    ("calc_get_comments", {"document_id": D, "sheet": "S"}, "calc_get_comments", False),
    ("calc_clear_comments", {"document_id": D, "sheet": "S", "address": "A1:C9"}, "calc_clear_comments", True),
    ("writer_get_structure", {"document_id": D, "limit": 100}, "writer_get_structure", False),
    ("writer_get_table", {"document_id": D, "table": "T1", "cell_offset": 2001}, "writer_get_table", False),
    ("writer_set_paragraph_style", {"document_id": D, "paragraph": 3, "style": "Heading 2", "expected_text": "Intro"},
     "writer_set_paragraph_style", True),
    ("writer_find_replace", {"document_id": D, "search": "a", "replace": "b", "whole_words": True}, "writer_find_replace", True),
    ("writer_apply_edits", {"document_id": D, "edits": [{"find": "a", "replace": "", "occurrence": 2, "match_case": False}],
                            "if_match": "0" * 32}, "writer_apply_edits", True),
    ("writer_get_selection", {"document_id": D}, "writer_get_selection", False),
    ("writer_replace_selection", {"document_id": D, "text": "new", "expected_text": "old"}, "writer_replace_selection", True),
    ("writer_format_selection", {"document_id": D, "expected_text": "abc", "format": {"bold": True, "font_size": 11.5, "color": "#112233"}},
     "writer_format_selection", True),
    ("writer_get_comments", {"document_id": D}, "writer_get_comments", False),
    ("writer_add_comment", {"document_id": D, "expected_text": "abc", "text": "note", "author": "Me"}, "writer_add_comment", True),
    ("impress_get_slide", {"document_id": D, "slide": 2}, "impress_get_slide", False),
    ("impress_create_slide", {"document_id": D, "title": "Plan", "body": "One", "layout": "title_content", "after_slide": 1},
     "impress_create_slide", True),
    ("impress_delete_slide", {"document_id": D, "slide": 2}, "impress_delete_slide", True),
    ("impress_set_shape_text", {"document_id": D, "slide": 1, "placeholder": "title", "text": "T"}, "impress_set_shape_text", True),
    ("impress_add_text_box", {"document_id": D, "slide": 1, "text": "x", "x_cm": 1, "y_cm": 2.5}, "impress_add_text_box", True),
    ("impress_find_replace", {"document_id": D, "search": "a", "replace": "b", "slide": 1}, "impress_find_replace", True),
    ("impress_set_slide_layout", {"document_id": D, "slide": 1, "layout": "title_two_content", "expected_title": "Plan"},
     "impress_set_slide_layout", True),
    ("impress_format_text", {"document_id": D, "slide": 1, "shape": 2, "format": {"italic": True, "font_size": 20},
                             "expected_text": "Body", "expected_title": ""}, "impress_format_text", True),
    ("impress_add_shape", {"document_id": D, "slide": 1, "kind": "line", "x_cm": 1, "y_cm": 1, "width_cm": 5, "height_cm": 0,
                           "line_color": "#000000", "expected_title": "Plan"}, "impress_add_shape", True),
    ("impress_get_current_slide", {"document_id": D}, "impress_get_current_slide", False),
    ("impress_list_layouts", {"document_id": D}, "impress_list_layouts", False),
]

# More valid shapes of the tools whose arguments vary with an operator or a type.
VALID_VARIANTS = (
    [("calc_add_conditional_format", cf(op, 1, 5) if op.endswith("between") else cf(op, 1)) for op in CF_OPERATORS if op != "formula"]
    + [("calc_add_conditional_format", cf(op, "a", "z") if op.endswith("between") else cf(op, "Open")) for op in CF_OPERATORS if op != "formula"]
    + [("calc_add_conditional_format", cf("between", -10.5, 0)), ("calc_add_conditional_format", cf("formula", "B2>C2")),
       ("calc_add_conditional_format", cf("formula", "AND($B2>0;ISNUMBER(C$1))")), ("calc_add_conditional_format", cf("equal", "x" * 255))]
    + [("calc_filter_range", flt({"column": "B", "operator": op, "value": 1.5})) for op in ("equals", "not_equals", "greater", "greater_equal", "less", "less_equal")]
    + [("calc_filter_range", flt({"column": "C", "operator": op, "value": "Open"})) for op in ("equals", "not_equals", "contains", "not_contains", "begins_with", "ends_with")]
    + [("calc_filter_range", flt({"column": "C", "operator": "equals", "value": ""})), ("calc_filter_range", flt({"column": "B", "operator": "not_empty"})),
       ("calc_filter_range", flt()), ("calc_filter_range", flt({"column": "C", "operator": "contains", "value": "x" * 1000})),
       ("calc_filter_range", flt(*[{"column": "B", "operator": "greater", "value": i} for i in range(8)], match="any"))]
    + [("calc_set_validation", val(type="whole_number", operator=op, value1=1, value2=9) if op.endswith("between") else val(type="whole_number", operator=op, value1=1))
       for op in ("between", "not_between", "greater", "greater_equal", "less", "less_equal", "equal")]
    + [("calc_set_validation", val(type="decimal", operator="greater", value1=0.5, error_message="too small", reject_invalid=False)),
       ("calc_set_validation", val(type="decimal", operator="between", value1=-1e15, value2=1e15)),
       ("calc_set_validation", val(type="list", items=["Open", "Closed"])), ("calc_set_validation", val(type="none")),
       ("calc_execute_plan", {"document_id": D, "plan": {"version": 1, "outcome": "ask", "message": "Which sheet?"}})]
)

INVALID = [
    ("calc_add_conditional_format", cf("greater", True)),
    ("calc_add_conditional_format", cf("greater", [1])),
    ("calc_add_conditional_format", cf("greater", "")),
    ("calc_add_conditional_format", cf("greater", float("nan"))),
    ("calc_add_conditional_format", cf("greater", float("inf"))),
    ("calc_add_conditional_format", cf("greater", 10 ** 400)),
    ("calc_add_conditional_format", cf("equal", "x" * 256)),
    ("calc_add_conditional_format", cf("formula", "x" * 1001)),
    ("calc_add_conditional_format", cf("formula", "=B2>C2")),
    ("calc_add_conditional_format", cf("formula", 3)),
    ("calc_add_conditional_format", cf("formula", "B2>1", "B3")),
    ("calc_add_conditional_format", cf("between", 1)),
    ("calc_add_conditional_format", cf("greater", 1, 2)),
    ("calc_add_conditional_format", cf("greater", None)),
    ("calc_filter_range", flt({"column": "B", "operator": "greater", "value": "1"})),
    ("calc_filter_range", flt({"column": "B", "operator": "contains"})),
    ("calc_filter_range", flt({"column": "B", "operator": "contains", "value": ""})),
    ("calc_filter_range", flt({"column": "B", "operator": "contains", "value": 3})),
    ("calc_filter_range", flt({"column": "B", "operator": "not_empty", "value": 1})),
    ("calc_filter_range", flt({"column": "B", "operator": "equals", "value": {"x": 1}})),
    ("calc_filter_range", flt({"column": "B", "operator": "equals", "value": "x" * 1001})),
    ("calc_filter_range", flt({"column": "B", "operator": "equals"})),
    ("calc_filter_range", flt({"column": "B", "operator": "empty"})),
    ("calc_filter_range", flt(*[{"column": "B", "operator": "not_empty"}] * 9)),
    ("calc_set_validation", val(type="whole_number", operator="between", value1="0", value2=100)),
    ("calc_set_validation", val(type="decimal", operator="greater", value1=True)),
    ("calc_set_validation", val(type="decimal", operator="greater", value1=float("inf"))),
    ("calc_set_validation", val(type="date")),
    ("impress_format_text", {"document_id": D, "slide": 1, "shape": 1, "format": {}, "expected_text": "x" * 10001}),
    ("impress_format_text", {"document_id": D, "slide": 1, "shape": 1, "format": {}, "expected_title": 5}),
    ("impress_set_slide_layout", {"document_id": D, "slide": 1, "layout": "blank", "expected_text": "x"}),
    ("impress_add_shape", {"document_id": D, "slide": 1, "kind": "line", "x_cm": 1, "y_cm": 1, "width_cm": 5, "height_cm": 0,
                           "expected_text": "x"}),
    ("impress_set_shape_text", {"document_id": D, "slide": 1, "placeholder": "title", "text": "T", "expected_title": "x"}),
    ("impress_add_text_box", {"document_id": D, "slide": 1, "text": "x", "x_cm": -1}),
    ("impress_get_slide", {"document_id": D, "slide": 0}),
    ("calc_create_chart", {"document_id": D, "sheet": "S", "address": "A1:C5", "type": "pie", "anchor_cell": "E2", "width_cm": 1}),
]


def walk(spec, path):
    """Yield (path, spec) for every schema node, through properties, items and oneOf/anyOf/allOf."""
    if not isinstance(spec, dict):
        return
    yield path, spec
    for key, child in spec.get("properties", {}).items():
        yield from walk(child, f"{path}.{key}")
    if isinstance(spec.get("items"), dict):
        yield from walk(spec["items"], f"{path}[]")
    for keyword in ("oneOf", "anyOf", "allOf"):
        for number, child in enumerate(spec.get(keyword, [])):
            yield from walk(child, f"{path}/{keyword}{number}")


class SchemaBoundsTests(unittest.TestCase):
    def test_every_number_and_integer_declares_both_bounds(self):
        missing = []
        for schema in plugin._SCHEMAS:
            for path, spec in walk(schema["parameters"], schema["name"]):
                kinds = spec.get("type") if isinstance(spec.get("type"), list) else [spec.get("type")]
                if {"number", "integer"} & set(kinds) and not (
                        ("minimum" in spec or "exclusiveMinimum" in spec) and ("maximum" in spec or "exclusiveMaximum" in spec)):
                    missing.append(path)
        self.assertEqual(missing, [])

    def test_stale_target_arguments_are_exactly_the_agreed_contract(self):
        title = {"type": "string", "minLength": 0, "maxLength": 10000,
                 "description": "the slide's title text as last read; if given and different, the call is refused as Stale"}
        text = {"type": "string", "minLength": 0, "maxLength": 10000,
                "description": "the shape's full text as last read with impress_get_slide; if given and different, the call is refused as Stale"}
        expected = {
            "impress_format_text": {"document_id", "slide", "shape", "placeholder", "format", "expected_text", "expected_title"},
            "impress_set_slide_layout": {"document_id", "slide", "layout", "expected_title"},
            "impress_add_shape": {"document_id", "slide", "kind", "x_cm", "y_cm", "width_cm", "height_cm", "fill_color", "line_color",
                                  "text", "expected_title"},
        }
        for name, properties in expected.items():
            parameters = plugin._SCHEMA_BY_NAME[name]["parameters"]
            self.assertEqual(set(parameters["properties"]), properties, name)
            self.assertEqual(parameters["properties"]["expected_title"], title, name)
            self.assertNotIn("expected_title", parameters["required"])
        self.assertEqual(plugin._SCHEMA_BY_NAME["impress_format_text"]["parameters"]["properties"]["expected_text"], text)


class ValidatorTotalityTests(unittest.TestCase):
    def test_union_members_use_their_own_bounds(self):
        spec = {"type": ["string", "number"], "minLength": 1, "maxLength": 3, "minimum": 0, "maximum": 5}
        for value, ok in ((4, True), (6, False), ("abc", True), ("abcd", False), ("", False), (True, False), (None, False), ([], False)):
            self.assertIs(plugin._valid_value(spec, value), ok, value)
        self.assertTrue(plugin._valid_value({"type": ["string", "null"]}, None))

    def test_missing_keywords_mean_unbounded_never_an_exception(self):
        for spec, value, ok in (({"type": "number"}, 1e300, True), ({"type": "number"}, float("nan"), False),
                                ({"type": "number"}, float("inf"), False), ({"type": "integer"}, -10 ** 30, True),
                                ({"type": "integer", "minimum": 0}, -1, False), ({"type": "number", "exclusiveMinimum": 0}, 0, False),
                                ({"type": "array"}, [1, "a"], True), ({"type": "object"}, {}, True), ({"type": "object"}, {"a": 1}, False),
                                ({}, 1, False), ({"type": "mystery"}, 1, False)):
            self.assertIs(plugin._valid_value(spec, value), ok, (spec, value))

    def test_a_local_defect_in_checking_is_a_structured_error_not_a_crash(self):
        with mock.patch.object(plugin, "_valid_value", side_effect=KeyError("minimum")), self.assertLogs("plugin", "WARNING"):
            result = call("calc_set_validation", val(type="whole_number", operator="between", value1=0, value2=100))
        self.assertEqual(result, {"ok": False, "error": {"category": "Validation",
                                                         "message": "LibreOffice plugin rejected an unexpected local error"}})
        self.assertEqual(Recording.calls, [])


class HandlerBatteryTests(unittest.TestCase):
    def test_the_battery_covers_every_registered_tool(self):
        self.assertEqual(sorted({name for name, *_ in VALID}), sorted(plugin._SCHEMA_BY_NAME))
        self.assertEqual(len(plugin._SCHEMA_BY_NAME), 55)

    def test_one_valid_call_per_tool_reaches_the_bridge_once_as_hermes_sends_it(self):
        for name, args, op, mutation in VALID:
            with self.subTest(tool=name):
                result = call(name, args)
                self.assertNotIn("error", result, result)
                self.assertEqual(len(Recording.calls), 1)
                self.assertEqual(Recording.calls[0][0], op)
                self.assertIs(Recording.calls[0][2], mutation)
                if name in plugin._DIRECT_TOOLS:
                    self.assertIs(mutation, plugin._DIRECT_TOOLS[name])
                    self.assertEqual(Recording.calls[0][1], args)

    def test_every_operator_and_rule_shape_is_accepted(self):
        for name, args in VALID_VARIANTS:
            with self.subTest(tool=name, args=args):
                result = call(name, args)
                self.assertNotIn("error", result, result)
                self.assertEqual([c[0] for c in Recording.calls], [name if name in plugin._DIRECT_TOOLS else "execute_plan"])

    def test_invalid_calls_are_refused_before_the_bridge(self):
        for name, args in INVALID:
            with self.subTest(tool=name, args=args):
                result = call(name, args)
                self.assertEqual(result["error"]["category"], "Validation")
                self.assertEqual(Recording.calls, [])


class PluginMatchesBridgeTests(unittest.TestCase):
    """For calc_add_conditional_format and calc_filter_range the plugin accepts exactly what the
    bridge's own checks accept (both run here on the same arguments; nothing is dispatched)."""

    def bridge_accepts(self, name, args):
        runtime = FAKES.OfficeOpsTests.runtime(self, FAKES.Calc(("S",)))  # type: ignore[arg-type]
        runtime.dispatch = lambda _fn, mutation=False: {"accepted": True}
        try:
            return runtime.invoke(name, {**args, "document_id": self.document_id}) == {"accepted": True}
        except BRIDGE.BridgeFault as fault:
            self.assertEqual(fault.category, "Validation", (name, args))
            return False

    def test_same_verdict_for_every_shape(self):
        cases = [(name, args) for name, args in VALID_VARIANTS + INVALID if name in ("calc_add_conditional_format", "calc_filter_range")]
        self.assertGreater(len(cases), 40)
        compared = 0
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                plugin_ok = "error" not in call(name, args)
                values = [args.get("value1"), args.get("value2")] + [c.get("value") for c in args.get("conditions", [])]
                if any(not_a_double(v) for v in values):
                    # NaN, infinity or an integer beyond the double range: no tool call can carry
                    # these to Calc, so the plugin refuses them and the bridge is not consulted
                    self.assertFalse(plugin_ok)
                    continue
                self.assertEqual(plugin_ok, self.bridge_accepts(name, args))
                compared += 1
        self.assertGreater(compared, 40)


def not_a_double(value):
    return ((isinstance(value, float) and not math.isfinite(value))
            or (isinstance(value, int) and not isinstance(value, bool) and abs(value) > sys.float_info.max))

if __name__ == "__main__":
    unittest.main()
