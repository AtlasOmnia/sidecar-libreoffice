# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Calc charts: validation before writing, creation readback and listing (fakes; native checks separate)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

OPS = FAKES.OPS


class Struct:
    pass


class Diagram:
    def __init__(self, kind):
        self.kind, self.Vertical = kind, False

    def getDiagramType(self):
        return "com.sun.star.chart." + self.kind


class Model:
    def __init__(self):
        self.diagram, self.HasMainTitle, self.HasLegend = Diagram("BarDiagram"), False, True
        self.title = Struct()
        self.title.String = ""

    def createInstance(self, service):
        return Diagram(service.rsplit(".", 1)[-1])

    def setDiagram(self, diagram):
        self.diagram = diagram

    def getDiagram(self):
        return self.diagram

    def getTitle(self):
        return self.title


class Chart:
    def __init__(self, address):
        self.model, self.address = Model(), address

    def getEmbeddedObject(self):
        return self.model

    def getRanges(self):
        return (self.address,)


class Charts:
    def __init__(self, sheet):
        self.sheet, self.items = sheet, {}

    def getElementNames(self):
        return tuple(self.items)

    def getByName(self, name):
        return self.items[name]

    def addNewByName(self, name, rect, ranges, _headers, _labels):
        assert self.sheet.doc.undo.open is not None
        self.sheet.doc.undo.touched = True
        self.items[name] = Chart(ranges[0])
        shape = Struct()
        shape.PersistName = name
        shape.getPosition = lambda: type("P", (), {"X": rect.X, "Y": rect.Y})()
        shape.getSize = lambda: type("S", (), {"Width": rect.Width, "Height": rect.Height})()
        self.sheet.shapes.append(shape)


class Cell:
    def __init__(self, kind, col, row):
        self.kind = kind
        self.Position = type("P", (), {"X": col * 2000, "Y": row * 500})()

    def getType(self):
        return self.kind


class Sheet:
    def __init__(self, doc, numeric=True):
        self.doc, self.numeric, self.shapes = doc, numeric, []
        self.charts = Charts(self)

    def getName(self):
        return "Data"

    def getCharts(self):
        return self.charts

    def getCellByPosition(self, col, row):
        return Cell("VALUE", col, row)

    def getCellRangeByPosition(self, c1, r1, c2, r2):
        sheet = self
        address = Struct()
        address.Sheet, address.StartColumn, address.StartRow, address.EndColumn, address.EndRow = 0, c1, r1, c2, r2

        class Range:
            def getCellByPosition(self, c, r):
                return Cell("VALUE" if sheet.numeric and c and r else "TEXT", c, r)

            def getRangeAddress(self):
                return address
        return Range()

    def getDrawPage(self):
        shapes = self.shapes
        return type("Page", (), {"getCount": lambda _s: len(shapes), "getByIndex": lambda _s, i: shapes[i]})()


class Undo:
    def __init__(self):
        self.open, self.steps, self.touched = None, [], False

    def enterUndoContext(self, title):
        self.open, self.touched = title, False

    def leaveUndoContext(self):
        if self.touched:
            self.steps.append(self.open)
        self.open = None

    def isUndoPossible(self):
        return bool(self.steps)

    def getCurrentUndoActionTitle(self):
        return self.steps[-1]

    def getAllUndoActionTitles(self):
        return tuple(reversed(self.steps))


def make_doc(numeric=True):
    doc = FAKES.Calc(("Data",))
    doc.undo = Undo()
    doc.getUndoManager = lambda: doc.undo
    sheet = Sheet(doc, numeric)
    doc.getSheets = lambda: type("Sheets", (), {"hasByName": lambda _s, n: n == "Data", "getByName": lambda _s, n: sheet,
                                                 "getByIndex": lambda _s, i: sheet, "getCount": lambda _s: 1})()
    doc.sheet = sheet
    return doc


class ChartTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def setUp(self):
        patch = mock.patch.object(OPS.uno, "createUnoStruct", lambda _n: Struct(), create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_chart_is_created_read_back_and_listed(self):
        doc = make_doc()
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_create_chart", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C5",
                                                    "type": "bar", "anchor_cell": "E2", "title": "Sales vs costs"})
        self.assertTrue(data["verified"] and data["undoable"])
        self.assertEqual({k: data["chart"][k] for k in ("name", "type", "title", "ranges", "x_cm", "width_cm")},
                         {"name": "Chart 1", "type": "bar", "title": "Sales vs costs", "ranges": ["Data.A1:C5"], "x_cm": 8.0,
                          "width_cm": 16.0})
        runtime.invoke("calc_create_chart", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C5",
                                             "type": "pie", "anchor_cell": "E20", "legend": False})
        listed = runtime.invoke("calc_list_charts", {"document_id": self.document_id, "sheet": "Data"})
        self.assertEqual([(c["name"], c["type"], c["has_legend"]) for c in listed["charts"]],
                         [("Chart 1", "bar", True), ("Chart 2", "pie", False)])

    def test_invalid_requests_and_ranges_without_numbers_create_nothing(self):
        doc = make_doc()
        runtime = self.runtime(doc)
        for extra in ({"type": "radar"}, {"anchor_cell": "E2:F3"}, {"address": "A1:A5"}, {"width_cm": 1},
                      {"title": "x" * 256}, {"legend": "yes"}, {"if_match": "abc"}):
            args = {"sheet": "Data", "address": "A1:C5", "type": "line", "anchor_cell": "E2", **extra}
            self.assertEqual(self.fault(runtime, "calc_create_chart", **args).category, "Validation", extra)
        runtime = self.runtime(make_doc(numeric=False))
        self.assertEqual(self.fault(runtime, "calc_create_chart", sheet="Data", address="A1:C5", type="line",
                                    anchor_cell="E2").category, "Validation")
        self.assertEqual(doc.sheet.charts.items, {})


if __name__ == "__main__":
    unittest.main()
