# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Calc sort and filter: validation before writing, row preservation, auto-revert, filter readback (fakes)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

OPS = FAKES.OPS


class Undo:
    def __init__(self, sheet):
        self.sheet, self.open, self.steps = sheet, None, []

    def enterUndoContext(self, title):
        self.open = (title, [row[:] for row in self.sheet.grid], [r.IsVisible for r in self.sheet.row_objects])

    def leaveUndoContext(self):
        self.steps.append(self.open)
        self.open = None

    def isUndoPossible(self):
        return bool(self.steps)

    def getCurrentUndoActionTitle(self):
        return self.steps[-1][0]

    def getAllUndoActionTitles(self):
        return tuple(step[0] for step in reversed(self.steps))

    def undo(self):
        _title, grid, visible = self.steps.pop()
        self.sheet.grid = grid
        for row, shown in zip(self.sheet.row_objects, visible):
            row.IsVisible = shown


class Row:
    IsVisible = True


class Cell:
    def __init__(self, sheet, c, r):
        self.sheet, self.c, self.r = sheet, c, r

    def getString(self):
        return str(self.sheet.grid[self.r][self.c])


class Prop:
    def __init__(self, name):
        self.Name, self.Value = name, None


class Descriptor:
    def __init__(self):
        self.fields, self.ContainsHeader = (), False

    def setFilterFields2(self, fields):
        self.fields = fields


class Range:
    def __init__(self, sheet, bounds):
        self.sheet, self.bounds = sheet, bounds

    def getCellByPosition(self, c, r):
        return Cell(self.sheet, self.bounds[0] + c, self.bounds[1] + r)

    def createSortDescriptor(self):
        return tuple(Prop(n) for n in ("IsSortColumns", "ContainsHeader", "SortFields", "BindFormatsToContent"))

    def createFilterDescriptor(self, _empty):
        return Descriptor()

    def filter(self, descriptor):
        assert self.sheet.doc.undo.open is not None
        first = self.bounds[1] + (1 if descriptor.ContainsHeader else 0)
        for r in range(first, self.bounds[3] + 1):
            ok = all(self.sheet.grid[r][self.bounds[0] + f.Field] > f.NumericValue for f in descriptor.fields)
            self.sheet.row_objects[r].IsVisible = ok


class Sheet:
    def __init__(self, doc, grid):
        self.doc, self.grid = doc, grid
        self.row_objects = [Row() for _ in grid]
        for row in self.row_objects:
            row.IsVisible = True

    def getCellRangeByPosition(self, c1, r1, c2, r2):
        return Range(self, (c1, r1, c2, r2))

    def getRows(self):
        rows = self.row_objects
        return type("Rows", (), {"getByIndex": lambda _s, i: rows[i]})()


def make_doc(grid):
    doc = FAKES.Calc(("Data",))
    sheet = Sheet(doc, grid)
    doc.undo = Undo(sheet)
    doc.getUndoManager = lambda: doc.undo
    doc.getSheets = lambda: type("Sheets", (), {"hasByName": lambda _s, n: n == "Data", "getByName": lambda _s, n: sheet})()
    doc.sheet = sheet
    return doc


def fake_sort(target, _name, args):
    props = {p.Name: p.Value for p in args[0]}
    sheet, (c1, r1, c2, r2) = target.sheet, target.bounds
    assert sheet.doc.undo.open is not None
    first = r1 + (1 if props["ContainsHeader"] else 0)
    rows = [sheet.grid[r][c1:c2 + 1] for r in range(first, r2 + 1)]
    for field in reversed(props["SortFields"]):
        rows.sort(key=lambda row: row[field.Field], reverse=not field.IsAscending)
    for offset, row in enumerate(rows):
        sheet.grid[first + offset][c1:c2 + 1] = row
    if getattr(sheet, "break_other_row_formula", False):
        sheet.grid[first][c2] = "changed"


class SortFilterTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def setUp(self):
        struct = type("Struct", (), {})
        patches = [mock.patch.object(OPS.uno, "createUnoStruct", lambda _n: struct(), create=True),
                   mock.patch.object(OPS.uno, "Any", lambda _t, value: value, create=True),
                   mock.patch.object(OPS.uno, "invoke", fake_sort, create=True),
                   mock.patch.object(OPS.uno, "getConstantByName", lambda name: name.rsplit(".", 1)[-1], create=True),
                   mock.patch.object(OPS.uno, "Enum", lambda _t, value: value, create=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def grid(self):
        return [["Name", "Score", "Note"], ["b", 2, "x"], ["a", 1, "y"], ["c", 3, "z"]]

    def test_sort_keeps_rows_together_and_header_in_place(self):
        doc = make_doc(self.grid())
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_sort_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C4",
                                                  "keys": [{"column": "B", "descending": True}], "has_header": True})
        self.assertEqual((data["sorted"], data["rows_preserved"], data["undoable"], data["first_key_values"]),
                         (True, True, True, ["3", "2", "1"]))
        self.assertEqual(doc.sheet.grid[1], ["c", 3, "z"])
        self.assertEqual(doc.sheet.grid[0], ["Name", "Score", "Note"])

    def test_sort_that_changes_row_content_is_undone_and_reported(self):
        doc = make_doc(self.grid())
        doc.sheet.break_other_row_formula = True
        runtime = self.runtime(doc)
        runtime._is_active = lambda _doc: True  # the revert needs the focused-window guard of libreoffice_undo
        data = runtime.invoke("calc_sort_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C4",
                                                  "keys": [{"column": "A"}], "has_header": True})
        self.assertEqual((data["sorted"], data["reverted"], data["undoable"]), (False, True, False))
        self.assertEqual(doc.sheet.grid, self.grid())

    def test_invalid_sort_and_filter_requests_change_nothing(self):
        doc = make_doc(self.grid())
        runtime = self.runtime(doc)
        for keys in ([], [{"column": "D"}], [{"column": "A"}, {"column": "A"}], [{"column": "A", "descending": "yes"}],
                     [{"column": "A"}] * 4, [{"col": "A"}]):
            self.assertEqual(self.fault(runtime, "calc_sort_range", sheet="Data", address="A1:C4", keys=keys,
                                        has_header=True).category, "Validation", keys)
        self.assertEqual(self.fault(runtime, "calc_sort_range", sheet="Data", address="A1:C1", keys=[{"column": "A"}],
                                    has_header=True).category, "Validation")
        for condition in ({"column": "B", "operator": "greater", "value": "1"}, {"column": "B", "operator": "like", "value": 1},
                          {"column": "B", "operator": "empty"}, {"column": "B", "operator": "not_empty", "value": 1}, {"column": "B", "operator": "contains"},
                          {"column": "Z", "operator": "not_empty"}):
            self.assertEqual(self.fault(runtime, "calc_filter_range", sheet="Data", address="A1:C4", conditions=[condition],
                                        has_header=True).category, "Validation", condition)
        self.assertEqual(self.fault(runtime, "calc_sort_range", sheet="Nope", address="A1:C4", keys=[{"column": "A"}],
                                    has_header=True).category, "NotFound")
        self.assertEqual((doc.sheet.grid, doc.undo.steps), (self.grid(), []))

    def test_filter_hides_rows_reads_them_back_and_undo_shows_them(self):
        doc = make_doc(self.grid())
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_filter_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C4",
                                                    "conditions": [{"column": "B", "operator": "greater", "value": 1}],
                                                    "has_header": True})
        self.assertEqual((data["hidden_rows"], data["visible_rows"], data["values_unchanged"], data["undoable"]), ([3], 2, True, True))
        doc.undo.undo()
        self.assertTrue(all(row.IsVisible for row in doc.sheet.row_objects))


if __name__ == "__main__":
    unittest.main()
