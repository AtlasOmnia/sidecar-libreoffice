# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Conditional formats, validation, freeze panes and protection: pure checks and refusals (fakes)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

OPS = FAKES.OPS


class Undo:
    def __init__(self):
        self.open, self.steps, self.added = None, [], []

    def enterUndoContext(self, title):
        self.open, self.added = title, []

    def leaveUndoContext(self):
        if self.added:
            self.steps.append((self.open, self.added))
        self.open = None

    def addUndoAction(self, action):
        self.added.append(action)

    def isUndoPossible(self):
        return bool(self.steps)

    def getCurrentUndoActionTitle(self):
        return self.steps[-1][0]

    def getAllUndoActionTitles(self):
        return tuple(step[0] for step in reversed(self.steps))


class Sheet:
    def __init__(self, protected=False, password=False):
        self.protected, self.password = protected, password

    def isProtected(self):
        return self.protected

    def protect(self, password):
        assert password == ""
        self.protected = True

    def unprotect(self, password):
        if self.password and password != "secret":
            raise ValueError("IllegalArgumentException")
        self.protected = False


def make_doc(sheet):
    doc = FAKES.Calc(("Data",))
    doc.undo = Undo()
    doc.getUndoManager = lambda: doc.undo
    doc.getSheets = lambda: type("Sheets", (), {"hasByName": lambda _s, n: n == "Data", "getByName": lambda _s, n: sheet})()
    return doc


class ViewSettingsTests(unittest.TestCase):
    def test_freeze_state_is_parsed_per_sheet_and_unknown_formats_fail_closed(self):
        data = "100/60/0;1;tw:1857;0/0/2/2/1/2/0/0/1/0/2;3+4+2+0+3+0+1+0+3+0+0;0/0/0/0/0/0/0/0/0/0/0"
        self.assertEqual(OPS.parse_view_freeze(data, 0), {"frozen": True, "columns": 1, "rows": 2})
        self.assertEqual(OPS.parse_view_freeze(data, 1), {"frozen": True, "columns": 3, "rows": 0})
        self.assertEqual(OPS.parse_view_freeze(data, 2), {"frozen": False, "columns": 0, "rows": 0})
        for bad, index in ((data, 3), ("100/60/0;0;0/0/2/2/1/2", 0), ("100;0;tw:1;x/y/2/2/1/2", 0), ("100;0;tw:1;0/0/2", 0)):
            with self.assertRaises(ValueError):
                OPS.parse_view_freeze(bad, index)


class RuleTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def test_protection_never_takes_a_password_and_never_lifts_a_users_password(self):
        sheet = Sheet(protected=True, password=True)
        doc = make_doc(sheet)
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_protect_sheet", {"document_id": self.document_id, "sheet": "Data", "protect": False})
        self.assertEqual((data["changed"], data["protected"], data["undoable"]), (False, True, False))
        self.assertIn("password", data["reason"])
        self.assertEqual(doc.undo.steps, [])
        self.assertEqual(self.fault(runtime, "calc_protect_sheet", sheet="Data", protect=True, password="x").category, "Validation")

    def test_protect_is_one_step_whose_bridge_action_reverses_it(self):
        sheet = Sheet()
        doc = make_doc(sheet)
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_protect_sheet", {"document_id": self.document_id, "sheet": "Data", "protect": True})
        self.assertEqual((data["changed"], data["verified"], data["undoable"]), (True, True, True))
        title, actions = doc.undo.steps[-1]
        self.assertEqual(title, "Hermes: protect Data")
        actions[0].undo()
        self.assertFalse(sheet.protected)
        again = runtime.invoke("calc_protect_sheet", {"document_id": self.document_id, "sheet": "Data", "protect": False})
        self.assertEqual(again["changed"], False)

    def test_invalid_rule_requests_are_refused_before_anything_is_read(self):
        runtime = self.runtime(make_doc(Sheet()))
        base = {"sheet": "Data", "address": "B2:B5"}
        for extra in ({"operator": "between", "value1": 1, "style": "Good"}, {"operator": "greater", "value1": 1, "value2": 2, "style": "Good"},
                      {"operator": "formula", "value1": "=B2>1", "style": "Good"}, {"operator": "greater", "value1": True, "style": "Good"},
                      {"operator": "greater", "value1": 1, "style": "Pink"}, {"operator": "regex", "value1": 1, "style": "Good"}):
            self.assertEqual(self.fault(runtime, "calc_add_conditional_format", **base, **extra).category, "Validation", extra)
        for extra in ({"type": "list"}, {"type": "list", "items": []}, {"type": "list", "items": ["a"] * 101},
                      {"type": "list", "items": ["a"], "operator": "equal"}, {"type": "whole_number", "operator": "between", "value1": 0.5, "value2": 2},
                      {"type": "decimal", "operator": "greater"}, {"type": "none", "items": ["a"]}, {"type": "date"},
                      {"type": "decimal", "operator": "greater", "value1": 1, "error_message": "x" * 256}):
            self.assertEqual(self.fault(runtime, "calc_set_validation", **base, **extra).category, "Validation", extra)
        for extra in ({"columns": -1, "rows": 0}, {"columns": 1, "rows": 101}, {"columns": True, "rows": 1}):
            self.assertEqual(self.fault(runtime, "calc_freeze_panes", sheet="Data", **extra).category, "Validation", extra)


if __name__ == "__main__":
    unittest.main()
