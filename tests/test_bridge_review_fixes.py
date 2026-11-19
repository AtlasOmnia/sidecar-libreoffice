# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Bridge fixes from a code review of the 0.4.0 tools, against in-memory fakes.

Each fake models one LibreOffice behaviour measured natively on LibreOffice 26.8:
getAllUndoActionTitles lists the newest step first and an
empty undo context records nothing; a conditional-format entry without SourcePosition is anchored
at A1 of the first sheet; a chart name already used on any sheet makes addNewByName throw.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_charts as CHARTS  # noqa: E402  (module imports: their TestCases are not re-collected)
import test_impress_editing as IMPRESS  # noqa: E402
import test_office_ops as FAKES  # noqa: E402
import test_sort_filter as SORT  # noqa: E402
import test_writer_paging as PAGING  # noqa: E402

BRIDGE, OPS = FAKES.BRIDGE, FAKES.OPS
WIRE_LIMIT = int(1.5 * 1024 * 1024)  # well below the plugin's 2 MiB MAX_RESPONSE_BYTES


def wire_size(data):
    """Bytes the bridge sends for a successful reply (handler_for.send_json)."""
    return len(json.dumps({"ok": True, "data": data}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class TitledUndo(FAKES.UndoManager):
    """The shared fake plus getAllUndoActionTitles (newest first) and LibreOffice's step cap."""

    def __init__(self, cap=100):
        super().__init__()
        self.native_records, self.cap = True, cap

    def getAllUndoActionTitles(self):
        return tuple(title for title, _fn in reversed(self.steps))

    def leaveUndoContext(self):
        super().leaveUndoContext()
        del self.steps[:-self.cap]

    def addUndoAction(self, action):
        super().addUndoAction(action)
        del self.steps[:-self.cap]


class Case(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def patch_uno(self):
        for name, value in (("createUnoStruct", lambda _n: types.SimpleNamespace()), ("Enum", lambda _t, v: v)):
            patch = mock.patch.object(OPS.uno, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)


# -- P3-1 / P3-2: undo bookkeeping ---------------------------------------------------------

class UndoBookkeepingTests(Case):
    def test_undoable_comes_from_the_titles_list_not_from_the_top_title(self):
        doc = FAKES.Calc()
        doc.undo = TitledUndo(cap=3)
        runtime = self.runtime(doc)
        title = "Hermes: clear comments in A1:B2"
        doc.undo.steps.append((title, None))
        empty = runtime._in_undo(doc, title, lambda: None, lambda: {})  # LibreOffice records nothing
        self.assertFalse(empty["undoable"])
        doc.undo.steps[:] = [("a", None), ("b", None), ("c", None)]  # at the cap: the oldest step drops
        full = runtime._in_undo(doc, "Hermes: x", lambda: None, lambda: doc.undo.note_native_change() or {})
        self.assertTrue(full["undoable"])
        self.assertEqual(doc.undo.getAllUndoActionTitles(), ("Hermes: x", "c", "b"))

    def test_a_shape_build_that_fails_part_way_is_compensated_and_says_so(self):
        doc = FAKES.Impress()
        doc.undo = TitledUndo()
        runtime = self.runtime(doc)
        page = doc.pages.getByIndex(0)
        before = page.getCount()
        args = {"slide": 1, "kind": "rectangle", "x_cm": 1, "y_cm": 1, "width_cm": 2, "height_cm": 2, "fill_color": "#FF0000"}
        fault = self.fault(runtime, "impress_add_shape", **args)  # the fake shape has no setPropertyValue
        self.assertEqual((fault.category, fault.state), ("ExecutionUncertain", "started"))
        self.assertIn("compensated: true", str(fault))
        self.assertEqual((page.getCount(), doc.undo.steps), (before, []))
        page.remove = lambda _shape: (_ for _ in ()).throw(RuntimeError("cannot remove"))
        fault = self.fault(runtime, "impress_add_shape", **args)
        self.assertIn("compensated: false", str(fault))
        self.assertEqual(page.getCount(), before + 1)


# -- P1-3: conditional formats are anchored at the range's top-left cell -------------------

class Entries:
    def __init__(self, keep_source=True):
        self.items, self.sent, self.keep_source = [], [], keep_source

    def getCount(self):
        return len(self.items)

    def addNew(self, props):
        values = {p.Name: p.Value for p in props}
        self.sent.append(values)
        if not self.keep_source:  # LibreOffice before the fix: default ScAddress (sheet 0, A1)
            values = {k: v for k, v in values.items() if k != "SourcePosition"}
        self.items.append(values)

    def getByIndex(self, index):
        values = self.items[index]
        origin = values.get("SourcePosition", types.SimpleNamespace(Sheet=0, Column=0, Row=0))
        return types.SimpleNamespace(getOperator=lambda: values["Operator"], getFormula1=lambda: values["Formula1"],
                                     getFormula2=lambda: values.get("Formula2", ""), getStyleName=lambda: values["StyleName"],
                                     getSourcePosition=lambda: origin)


class FormatTarget:
    def __init__(self, doc, sheet_index, bounds, keep_source=True):
        self.doc, self._cf = doc, Entries(keep_source)
        self.address = types.SimpleNamespace(Sheet=sheet_index, StartColumn=bounds[0], StartRow=bounds[1],
                                             EndColumn=bounds[2], EndRow=bounds[3])

    def getRangeAddress(self):
        return self.address

    @property
    def ConditionalFormat(self):
        return self._cf

    @ConditionalFormat.setter
    def ConditionalFormat(self, value):
        self.doc.undo.note_native_change()
        self._cf = value


def rules_doc(keep_source=True):
    doc = FAKES.Calc(("Data", "Rules"))
    doc.undo = TitledUndo()
    doc.target = None

    def sheet_range(*bounds):
        doc.target = doc.target or FormatTarget(doc, 1, bounds, keep_source)
        return doc.target
    sheet = types.SimpleNamespace(getCellRangeByPosition=sheet_range)
    doc.sheets.getByName = lambda _name: sheet
    return doc


class ConditionalFormatAnchorTests(Case):
    def setUp(self):
        self.patch_uno()

    def add(self, doc):
        return self.runtime(doc).invoke("calc_add_conditional_format", {
            "document_id": self.document_id, "sheet": "Rules", "address": "B2:B10", "operator": "formula",
            "value1": "B2>C2", "style": "Bad"})

    def test_formula_condition_is_anchored_at_the_top_left_cell_of_its_sheet(self):
        doc = rules_doc()
        data = self.add(doc)
        origin = doc.target.ConditionalFormat.sent[0]["SourcePosition"]
        self.assertEqual((origin.Sheet, origin.Column, origin.Row), (1, 1, 1))
        self.assertTrue(data["verified"])
        self.assertEqual(data["conditional_formats"][-1]["anchor"], "B2")

    def test_a_rule_anchored_anywhere_else_is_not_verified(self):
        data = self.add(rules_doc(keep_source=False))
        self.assertFalse(data["verified"])
        self.assertEqual(data["conditional_formats"][-1]["anchor"], "A1")


# -- P3-5: validation readback --------------------------------------------------------------

class Rule(types.SimpleNamespace):
    pass


class ValidationTarget:
    def __init__(self, doc, readback):
        self.doc, self.readback = doc, readback  # attributes LibreOffice reads back differently
        self.rule = Rule(Type="ANY", Operator="NONE", Formula1="", Formula2="", ShowErrorMessage=False,
                         ErrorAlertStyle="STOP", ErrorMessage="", IgnoreBlankCells=True, ShowList=0)

    @property
    def Validation(self):
        return Rule(**vars(self.rule))

    @Validation.setter
    def Validation(self, value):
        self.doc.undo.note_native_change()
        self.rule = Rule(**{**vars(value), **self.readback})


class ValidationReadbackTests(Case):
    def setUp(self):
        self.patch_uno()

    def test_verified_compares_limits_operator_and_rejection_with_the_request(self):
        for readback, expected in (({}, True), ({"Formula2": ""}, False), ({"Formula1": "1"}, False),
                                   ({"Operator": "GREATER"}, False), ({"ShowErrorMessage": False}, False)):
            doc = FAKES.Calc(("Data",))
            doc.undo = TitledUndo()
            target = ValidationTarget(doc, readback)
            doc.sheets.getByName = lambda _n, t=target: types.SimpleNamespace(getCellRangeByPosition=lambda *_b: t)
            runtime = self.runtime(doc)
            data = runtime.invoke("calc_set_validation", {"document_id": self.document_id, "sheet": "Data", "address": "B2:B5",
                                                          "type": "whole_number", "operator": "between", "value1": 0, "value2": 100})
            self.assertEqual(data["verified"], expected, readback)


# -- P3-6: protection errors ------------------------------------------------------------------

class ProtectionErrorTests(Case):
    def test_a_failing_protect_is_not_reported_as_a_password_refusal(self):
        sheet = types.SimpleNamespace(protected=False, isProtected=lambda: sheet.protected,
                                      protect=lambda _p: (_ for _ in ()).throw(RuntimeError("protect failed")))
        doc = FAKES.Calc(("Data",))
        doc.undo = TitledUndo()
        doc.sheets.getByName = lambda _n: sheet
        runtime = self.runtime(doc)
        fault = self.fault(runtime, "calc_protect_sheet", sheet="Data", protect=True)
        self.assertEqual(fault.category, "ExecutionUncertain")
        self.assertNotIn("password", str(fault))


# -- P3-3 / P3-4: tables, AutoFormats and full-range fingerprints ------------------------------

class Grid:
    """A1:B5000 with a distinct header row; getDataArray/getFormulaArray over any sub-range."""

    def __init__(self, doc):
        self.doc, self.cells = doc, {(c, r): (f"H{c}" if r == 0 else float(r * 10 + c)) for c in range(2) for r in range(5000)}

    def getCellRangeByPosition(self, c1, r1, c2, r2):
        grid = self

        class Range:
            def getDataArray(self):
                return tuple(tuple(grid.cells.get((c, r), "") for c in range(c1, c2 + 1)) for r in range(r1, r2 + 1))

            def getFormulaArray(self):
                return tuple(tuple(str(v) for v in row) for row in self.getDataArray())

            def getCellByPosition(self, c, r):
                return types.SimpleNamespace(NumberFormat=0, getString=lambda: str(grid.cells.get((c1 + c, r1 + r), "")))

            def getRangeAddress(self):
                return types.SimpleNamespace(Sheet=0, StartColumn=c1, StartRow=r1, EndColumn=c2, EndRow=r2)

            def autoFormat(self, style):
                assert grid.doc.undo.open is not None
                if style not in grid.doc.autoformats:
                    raise RuntimeError("IllegalArgumentException")
        return Range()


class DatabaseRanges:
    def __init__(self, doc):
        self.doc, self.items = doc, {}

    def getElementNames(self):
        return tuple(self.items)

    def hasByName(self, name):
        return name in self.items

    def addNewByName(self, name, address):
        self.doc.undo.note_native_change()
        self.items[name] = types.SimpleNamespace(DataArea=address, AutoFilter=False)

    def getByName(self, name):
        return self.items[name]


def table_doc(autoformats=("Academic",)):
    doc = FAKES.Calc(("Data",))
    doc.undo = TitledUndo()
    doc.autoformats = autoformats
    grid = Grid(doc)
    doc.grid = grid
    doc.sheets.getByName = lambda _n: grid
    doc.DatabaseRanges = DatabaseRanges(doc)
    doc.getNumberFormats = lambda: types.SimpleNamespace(getByKey=lambda _k: types.SimpleNamespace(FormatString="General"))
    return doc


def service_context(autoformats):
    formats = types.SimpleNamespace(hasByName=lambda name: name in autoformats)
    manager = types.SimpleNamespace(createInstanceWithContext=lambda name, _ctx: formats if name == "com.sun.star.sheet.TableAutoFormats" else None)
    return types.SimpleNamespace(getServiceManager=lambda: manager)


class TableTests(Case):
    def test_a_missing_autoformat_is_refused_before_anything_is_written(self):
        doc = table_doc(autoformats=())
        runtime = self.runtime(doc)
        runtime.ctx = service_context(())
        fault = self.fault(runtime, "calc_insert_table", sheet="Data", address="A1:B4", style="Academic")
        self.assertEqual((fault.category, fault.state), ("Validation", "not_started"))
        self.assertEqual((doc.DatabaseRanges.items, doc.undo.steps), ({}, []))
        runtime.ctx = service_context(("Academic",))
        doc.autoformats = ("Academic",)
        self.assertTrue(runtime.invoke("calc_insert_table", {"document_id": self.document_id, "sheet": "Data",
                                                             "address": "A1:B4", "style": "Academic"})["verified"])

    def test_if_match_covers_cells_a_truncated_read_does_not_show(self):
        doc = table_doc()
        runtime = self.runtime(doc)
        runtime.ctx = service_context(("Academic",))
        read = runtime.invoke("get_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:B5000"})
        self.assertTrue(read["truncated"])
        doc.grid.cells[(1, 3999)] = "changed by the user"  # B4000, beyond the 5,000 cells returned
        fault = self.fault(runtime, "calc_insert_table", sheet="Data", address="A1:B5000", if_match=read["fingerprint"])
        self.assertEqual((fault.category, fault.state), ("Stale", "not_started"))
        self.assertEqual(doc.DatabaseRanges.items, {})


# -- P2-1: chart names are unique across the document ----------------------------------------

class SharedCharts(CHARTS.Charts):
    def addNewByName(self, name, rect, ranges, headers, labels):
        if any(name in sheet.charts.items for sheet in self.sheet.doc.chart_sheets):
            raise RuntimeError("RuntimeException")  # ScChartsObj::addNewByName, any sheet
        super().addNewByName(name, rect, ranges, headers, labels)


class NamedSheet(CHARTS.Sheet):
    def __init__(self, doc, name, index):
        super().__init__(doc)
        self.name, self.index, self.charts = name, index, SharedCharts(self)

    def getName(self):
        return self.name

    def getCellRangeByPosition(self, c1, r1, c2, r2):
        found = super().getCellRangeByPosition(c1, r1, c2, r2)
        found.getRangeAddress().Sheet = self.index
        return found


class ChartUndo(CHARTS.Undo):
    def getAllUndoActionTitles(self):
        return tuple(reversed(self.steps))


class ChartNameTests(Case):
    def setUp(self):
        patch = mock.patch.object(OPS.uno, "createUnoStruct", lambda _n: CHARTS.Struct(), create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_charts_on_two_sheets_get_document_unique_names(self):
        doc = FAKES.Calc(("Data", "Other"))
        doc.undo = ChartUndo()
        doc.getUndoManager = lambda: doc.undo
        doc.chart_sheets = [NamedSheet(doc, "Data", 0), NamedSheet(doc, "Other", 1)]
        named = {s.name: s for s in doc.chart_sheets}
        doc.getSheets = lambda: types.SimpleNamespace(hasByName=lambda n: n in named, getByName=lambda n: named[n],
                                                      getByIndex=lambda i: doc.chart_sheets[i], getCount=lambda: 2)
        runtime = self.runtime(doc)
        names = []
        for sheet in ("Data", "Other"):
            data = runtime.invoke("calc_create_chart", {"document_id": self.document_id, "sheet": sheet, "address": "A1:B3",
                                                        "type": "column", "anchor_cell": "D2"})
            self.assertTrue(data["verified"], sheet)
            names.append(data["chart"]["name"])
        self.assertEqual(names, ["Chart 1", "Chart 2"])


# -- P2-2: Writer reads stay well below the plugin's response limit --------------------------

class Comment:
    def __init__(self, text):
        self.Author, self.Content, self.text = "作者" * 100, text, text

    def supportsService(self, service):
        return service == "com.sun.star.text.textfield.Annotation"

    def getAnchor(self):
        return types.SimpleNamespace(getString=lambda: "錨" * 300)


class WriterBudgetTests(Case):
    def big_document(self, char):
        doc = FAKES.Writer([])
        rows = [[f"{char}{r:03d}{c:02d}" + char * 2100 for c in range(20)] for r in range(100)]
        doc.paragraphs = [PAGING.Table("Big", rows), FAKES.Paragraph("after the table")]
        return doc, rows

    def test_a_page_that_starts_with_a_huge_table_is_bounded_and_every_cell_is_read_once(self):
        for char in ("x", "東"):
            doc, rows = self.big_document(char)
            runtime = self.runtime(doc)
            seen, paragraphs, args = [], [], {"document_id": self.document_id}
            while True:
                page = runtime.invoke("writer_get_text", args)
                self.assertLess(wire_size(page), WIRE_LIMIT, char)
                paragraphs += [p for p in page["paragraphs"] if p]
                for table in page["tables"]:
                    seen += [cell["cell"] for cell in table["cells"]]
                    offset = table["next_cell_offset"]
                    while offset is not None:
                        rest = runtime.invoke("writer_get_table", {"document_id": self.document_id, "block": table["block"],
                                                                   "cell_offset": offset})
                        self.assertLess(wire_size(rest), WIRE_LIMIT, char)
                        self.assertTrue(rest["table"]["cells"], "every table page makes progress")
                        seen += [cell["cell"] for cell in rest["table"]["cells"]]
                        offset = rest["table"]["next_cell_offset"]
                if page["next_offset"] is None:
                    break
                args = {"document_id": self.document_id, "continuation": page["continuation"]}
            self.assertEqual(seen, list(doc.paragraphs[0].getCellNames()), char)
            self.assertEqual(paragraphs, ["after the table"])

    def test_comments_are_bounded_in_bytes_and_say_they_are_truncated(self):
        doc = FAKES.Writer(["x"])
        fields = [Comment("評" * 2000) for _ in range(500)]
        doc.getTextFields = lambda: types.SimpleNamespace(createEnumeration=lambda: iter_enum(fields))
        data = self.runtime(doc).invoke("writer_get_comments", {"document_id": self.document_id})
        self.assertLess(wire_size(data), WIRE_LIMIT)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["count"], 500)
        self.assertEqual(data["returned"], len(data["comments"]))
        self.assertLess(data["returned"], 500)


def iter_enum(items):
    pending = list(items)
    return types.SimpleNamespace(hasMoreElements=lambda: bool(pending), nextElement=lambda: pending.pop(0))


# -- P2-3: the sort's automatic revert uses the focused-window guard ---------------------------

class SortUndo(SORT.Undo):
    def __init__(self, sheet):
        super().__init__(sheet)
        self.undo_calls = 0

    def getAllUndoActionTitles(self):
        return tuple(step[0] for step in reversed(self.steps))

    def undo(self):
        self.undo_calls += 1
        super().undo()


class SortRevertTests(Case):
    setUp, grid = SORT.SortFilterTests.setUp, SORT.SortFilterTests.grid

    def run_sort(self, focused, focus_on_activate):
        doc = SORT.make_doc(self.grid())
        doc.undo = SortUndo(doc.sheet)
        doc.sheet.break_other_row_formula = True
        runtime = self.runtime(doc)
        state = {"focused": focused}
        runtime._is_active = lambda _doc: state["focused"]
        runtime._activate = lambda _doc: state.update(focused=focus_on_activate)
        with mock.patch.object(BRIDGE, "ACTIVATION_WAIT_SECONDS", 0.3):
            data = runtime.invoke("calc_sort_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C4",
                                                      "keys": [{"column": "A"}], "has_header": True})
        return doc, data

    def test_revert_needs_the_document_focused_and_never_undoes_blind(self):
        doc, data = self.run_sort(focused=False, focus_on_activate=False)
        self.assertEqual(doc.undo.undo_calls, 0)
        self.assertEqual((data["sorted"], data["reverted"], data["undoable"]), (False, False, True))
        self.assertIn("focused window", data["reason"])
        self.assertNotEqual(doc.sheet.grid, self.grid())
        doc, data = self.run_sort(focused=False, focus_on_activate=True)
        self.assertEqual((doc.undo.undo_calls, data["reverted"], data["activated_document"]), (1, True, True))
        self.assertEqual(doc.sheet.grid, self.grid())

    def test_a_failing_focus_check_still_reports_the_applied_sort(self):
        doc = SORT.make_doc(self.grid())
        doc.undo = SortUndo(doc.sheet)
        doc.sheet.break_other_row_formula = True
        runtime = self.runtime(doc)
        runtime._is_active = lambda _doc: (_ for _ in ()).throw(BRIDGE.BridgeFault("NotFound", "window gone", 404))
        data = runtime.invoke("calc_sort_range", {"document_id": self.document_id, "sheet": "Data", "address": "A1:C4",
                                                  "keys": [{"column": "A"}], "has_header": True})
        self.assertEqual((doc.undo.undo_calls, data["reverted"], data["undoable"]), (0, False, True))
        self.assertIn("STILL APPLIED", data["reason"])


# -- P3-7: Impress stale-target checks ---------------------------------------------------------

class ImpressStaleTargetTests(Case):
    setUp = IMPRESS.ImpressEditingTests.setUp

    def test_stale_text_or_title_is_refused_before_any_change(self):
        doc = IMPRESS.Deck()
        runtime = self.runtime(doc)
        page = doc.pages.pages[0]
        shapes = list(page.shapes)
        stale = [("impress_format_text", {"slide": 1, "placeholder": "body", "format": {"bold": True}, "expected_text": "Old body"}),
                 ("impress_format_text", {"slide": 1, "shape": 2, "format": {"bold": True}, "expected_title": "Old title"}),
                 ("impress_set_slide_layout", {"slide": 1, "layout": "title_only", "expected_title": "Old title"}),
                 ("impress_add_shape", {"slide": 1, "kind": "ellipse", "x_cm": 1, "y_cm": 1, "width_cm": 2, "height_cm": 2,
                                        "expected_title": "Old title"})]
        for op, args in stale:
            fault = self.fault(runtime, op, **args)
            self.assertEqual((fault.category, fault.status, fault.state), ("Stale", 409, "not_started"), op)
        self.assertEqual((page.Layout, page.shapes, doc.undo.steps), (1, shapes, []))
        data = runtime.invoke("impress_set_slide_layout", {"document_id": self.document_id, "slide": 1, "layout": "title_only",
                                                           "expected_title": "Title"})
        self.assertEqual((data["changed"], page.Layout), (True, 19))


if __name__ == "__main__":
    unittest.main()
