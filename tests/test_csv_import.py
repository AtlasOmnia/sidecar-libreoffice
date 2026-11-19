# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""CSV import: parsing/typing policy in the plugin, literal empty-destination writes in the bridge."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin.csv_import import parse_csv, target_address  # noqa: E402
from plugin.range_logic import RangeValidationError  # noqa: E402
import test_office_ops as FAKES  # noqa: E402

TRICKY = 'name,amount,code,note\r\n"Smith, J",12.50,007,"line one\nline two"\r\nZoë ✓,-3,1e3,=SUM(A1:A9)\r\n+1,1.234,5%,@x\r\n,,,\r\nshort\r\n'


class ParseTests(unittest.TestCase):
    def test_quoted_newline_unicode_and_formula_like_fields_stay_literal(self):
        rows = parse_csv(TRICKY)
        self.assertEqual(rows[1], ["Smith, J", "12.50", "007", "line one\nline two"])
        self.assertEqual(rows[2], ["Zoë ✓", "-3", "1e3", "=SUM(A1:A9)"])
        self.assertEqual(rows[4], [None, None, None, None])  # inner blank row kept
        self.assertEqual(rows[5], ["short", None, None, None])  # padded
        self.assertTrue(all(isinstance(v, str) or v is None for row in rows for v in row))

    def test_numbers_policy_converts_only_plain_dot_decimals(self):
        rows = parse_csv(TRICKY, typing="numbers")
        self.assertEqual(rows[1][1:3], [12.5, "007"])
        self.assertEqual(rows[2][1:4], [-3.0, 1000.0, "=SUM(A1:A9)"])
        self.assertEqual(rows[3], ["+1", 1.234, "5%", "@x"])
        self.assertEqual(parse_csv("1,5;2\n", delimiter=";", typing="numbers"), [["1,5", 2.0]])
        self.assertEqual(parse_csv("1234567890123456,0.5,.5,1.\n", typing="numbers"), [["1234567890123456", 0.5, ".5", "1."]])

    def test_malformed_oversized_and_invalid_options_are_refused(self):
        for bad in ('a,"b\n', "", "  \n", "a\x00b", "x" * 1_000_001):
            with self.assertRaises(RangeValidationError):
                parse_csv(bad)
        with self.assertRaises(RangeValidationError):
            parse_csv("a,b\n" * 5001)
        with self.assertRaises(RangeValidationError):
            parse_csv("a", delimiter=":")
        with self.assertRaises(RangeValidationError):
            parse_csv("a", typing="auto")

    def test_target_address(self):
        self.assertEqual(target_address("B2", 3, 28), "B2:AC4")
        for bad in ("A1:B2", "1A", None):
            with self.assertRaises(RangeValidationError):
                target_address(bad, 1, 1)
        with self.assertRaises(RangeValidationError):
            target_address("XFD1", 1, 2)


TEXT_FORMAT = 100  # stands for the "@" number-format key


class Cell:
    """LibreOffice-like cell: setString of text that would otherwise read as a number or formula
    switches NumberFormat to Text ("@"), and LibreOffice's own undo does not restore it."""

    def __init__(self, doc):
        self.doc, self.kind, self.text, self.value = doc, "EMPTY", "", 0.0
        self.NumberFormat = 0

    def _record(self):
        self.doc.undo.events.append(("content", self, (self.kind, self.text, self.value)))

    def setPropertyValue(self, name, value):
        """An explicit API format change IS part of LibreOffice's undo record."""
        assert name == "NumberFormat" and self.doc.undo.open is not None
        self.doc.undo.touched = True
        self.doc.undo.events.append(("format", self, self.NumberFormat))
        self.NumberFormat = value

    def getType(self):
        return self.kind

    def getString(self):
        return self.text

    def getValue(self):
        return self.value

    def setString(self, text):
        assert self.doc.undo.open is not None
        self.doc.undo.touched = True
        self._record()
        self.kind, self.text = "TEXT", text
        if text[:1] in "=+-@" or text.replace(".", "", 1).isdigit():
            self.NumberFormat = TEXT_FORMAT  # implicit, and not part of the native undo record

    def setValue(self, value):
        assert self.doc.undo.open is not None
        self.doc.undo.touched = True
        self._record()
        self.kind, self.value, self.text = "VALUE", value, repr(value)

    def setFormula(self, _formula):
        raise AssertionError("an import must never write a formula")


class Undo:
    def __init__(self):
        self.open, self.steps, self.touched, self.events, self.recorded = None, [], False, [], []

    def enterUndoContext(self, title):
        self.open, self.touched, self.events = title, False, []

    def addUndoAction(self, action):
        assert self.open is not None
        self.touched = True
        self.events.append(("action", action, None))

    def leaveUndoContext(self):
        if self.touched:
            self.steps.append(self.open)
            self.recorded.append(self.events)
        self.open = None

    def undo(self):
        """Native content undo plus Hermes actions, newest first; formats are NOT restored natively."""
        self.steps.pop()
        for kind, target, prev in reversed(self.recorded.pop()):
            if kind == "content":
                target.kind, target.text, target.value = prev
            elif kind == "format":
                target.NumberFormat = prev
            else:
                target.undo()

    def isUndoPossible(self):
        return bool(self.steps)

    def getCurrentUndoActionTitle(self):
        return self.steps[-1]

    def getAllUndoActionTitles(self):
        return tuple(reversed(self.steps))


class Sheet:
    def __init__(self, doc):
        self.cells = {}
        self.doc = doc

    def getCellByPosition(self, col, row):
        return self.cells.setdefault((col, row), Cell(self.doc))


class LiteralWriteTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def doc(self):
        doc = FAKES.Calc()
        doc.undo = Undo()
        doc.getUndoManager = lambda: doc.undo
        sheet = Sheet(doc)
        doc.getSheets = lambda: type("Sheets", (), {"hasByName": lambda _s, n: n == "Data",
                                                     "getByName": lambda _s, n: sheet})()
        doc.sheet = sheet
        doc.getNumberFormats = lambda: type("Formats", (), {"queryKey": lambda _s, code, _l, _b: TEXT_FORMAT if code == "@" else -1,
                                                            "addNew": lambda _s, _c, _l: TEXT_FORMAT})()
        return doc

    def test_writes_text_literally_numbers_as_values_and_reads_back(self):
        doc = self.doc()
        runtime = self.runtime(doc)
        values = [["=SUM(A1)", 12.5], [None, "007"]]
        data = runtime.invoke("calc_write_literal", {"document_id": self.document_id, "sheet": "Data",
                                                     "address": "B2:C3", "values": values})
        self.assertTrue(data["verified"] and data["undoable"])
        self.assertEqual(data["cells"], {"text": 2, "number": 1, "empty": 1})
        self.assertEqual((doc.sheet.cells[(1, 1)].kind, doc.sheet.cells[(1, 1)].text), ("TEXT", "=SUM(A1)"))
        self.assertEqual(doc.undo.steps, ["Hermes: import into B2:C3"])

    def test_one_undo_restores_values_and_the_number_formats_libreoffice_changed(self):
        doc = self.doc()
        doc.sheet.getCellByPosition(1, 1).NumberFormat = 7  # a user format on an empty cell survives
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_write_literal", {"document_id": self.document_id, "sheet": "Data", "address": "B2:D2",
                                                     "values": [["007", "=1+1", 3]]})
        self.assertTrue(data["verified"] and data["undoable"])
        cells = [doc.sheet.cells[(c, 1)] for c in (1, 2, 3)]
        self.assertEqual([c.NumberFormat for c in cells], [TEXT_FORMAT, TEXT_FORMAT, 0])  # text cells are "@"
        doc.undo.undo()
        self.assertEqual([(c.kind, c.NumberFormat) for c in cells], [("EMPTY", 7), ("EMPTY", 0), ("EMPTY", 0)])
        self.assertEqual(doc.undo.steps, [])

    def test_non_empty_destination_bad_cells_and_unknown_sheet_change_nothing(self):
        doc = self.doc()
        runtime = self.runtime(doc)
        doc.sheet.getCellByPosition(2, 2).kind = "VALUE"
        self.assertEqual(self.fault(runtime, "calc_write_literal", sheet="Data", address="B2:C3",
                                    values=[["a", "b"], ["c", "d"]]).category, "Validation")
        self.assertEqual([c.kind for c in doc.sheet.cells.values()].count("TEXT"), 0)
        for values in ([[True]], [[""]], [[{"x": 1}]], [["a", "b"]]):
            self.assertEqual(self.fault(runtime, "calc_write_literal", sheet="Data", address="A1",
                                        values=values).category, "Validation", values)
        self.assertEqual(self.fault(runtime, "calc_write_literal", sheet="Nope", address="A1", values=[["a"]]).category, "NotFound")
        self.assertEqual(doc.undo.steps, [])


class PluginRoutingTests(unittest.TestCase):
    call = FAKES.PluginDirectToolTests.call

    def test_import_parses_in_the_plugin_and_sends_one_literal_mutation(self):
        self.call("calc_import_csv", {"document_id": "lo:1", "sheet": "Data", "start_cell": "C3",
                                      "csv_text": "a,=1+1\n2,3\n", "typing": "numbers"})
        self.assertEqual(FAKES.RecordingClient.calls,
                         [("calc_write_literal", {"document_id": "lo:1", "sheet": "Data", "address": "C3:D4",
                                                  "values": [["a", "=1+1"], [2.0, 3.0]]}, True)])

    def test_bad_input_never_reaches_the_bridge(self):
        for args in ({"csv_text": 'a,"b'}, {"start_cell": "A1:B2"}, {"typing": "auto"}, {"extra": 1}):
            full = {"document_id": "lo:1", "sheet": "Data", "start_cell": "A1", "csv_text": "a,b\n", **args}
            result = self.call("calc_import_csv", full)
            self.assertFalse(result.get("ok", True), args)
            self.assertEqual(FAKES.RecordingClient.calls, [], args)


if __name__ == "__main__":
    unittest.main()
