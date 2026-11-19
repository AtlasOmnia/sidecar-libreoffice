# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Undo targeting and plan fixes found in live testing (fakes only, no LibreOffice)."""
from __future__ import annotations

import logging
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension_bridge import BRIDGE, ImmediateCallback  # noqa: E402


class Undo:
    def __init__(self, titles):
        self.titles = list(titles)
        self.undone = []
        self.contexts = []

    def isUndoPossible(self):
        return bool(self.titles)

    def getCurrentUndoActionTitle(self):
        return self.titles[-1]

    def undo(self):
        self.undone.append(self.titles.pop())

    def enterUndoContext(self, title):
        self.contexts.append(title)

    def leaveUndoContext(self):
        return None


class Window:
    def __init__(self, doc):
        self.doc = doc

    def toFront(self):
        return None

    def setFocus(self):
        if self.doc.focus_on_activate:
            self.doc.runtime_focus.on_event("OnFocus", self.doc)


class Doc:
    def __init__(self, uid, titles, desktop, focus):
        self.RuntimeUID = uid
        self.Title = uid
        self.URL = ""
        self.undo = Undo(titles)
        self.focus_on_activate = True
        self.activations = 0
        self.runtime_focus = focus
        outer = self

        class Frame:
            def activate(self):
                outer.activations += 1
                desktop.current = outer

            def getContainerWindow(self):
                return Window(outer)

            def getComponentWindow(self):
                return Window(outer)

        self.controller = types.SimpleNamespace(getFrame=lambda: Frame())

    def getPropertyValue(self, name):
        return self.RuntimeUID

    def supportsService(self, service):
        return service == "com.sun.star.sheet.SpreadsheetDocument"

    def getUndoManager(self):
        return self.undo

    def getCurrentController(self):
        return self.controller


class Desktop:
    def __init__(self):
        self.current = None

    def getCurrentComponent(self):
        return self.current


class FocusStateTests(unittest.TestCase):
    def test_tracks_focus_and_unfocus(self):
        state = BRIDGE.FocusState()
        a, b = object(), object()
        self.assertFalse(state.is_focused(a))
        state.on_event("OnFocus", a)
        self.assertTrue(state.is_focused(a))
        state.on_event("OnUnfocus", b)  # another document's unfocus does not clear a
        self.assertTrue(state.is_focused(a))
        state.on_event("OnFocus", b)
        self.assertFalse(state.is_focused(a))
        state.on_event("OnUnload", b)
        self.assertFalse(state.is_focused(b))


class UndoTargetingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)
        self.runtime.async_callback = ImmediateCallback()
        self.runtime.logger = logging.getLogger("undo-test")
        self.runtime.session_nonce = "feedbeef"
        self.runtime._documents = {}
        self.runtime.focus = BRIDGE.FocusState()
        self.desktop = Desktop()
        self.runtime.desktop = self.desktop
        self.a = Doc("a", [], self.desktop, self.runtime.focus)
        self.b = Doc("b", [], self.desktop, self.runtime.focus)
        self.desktop.current = self.b
        self.runtime.focus.on_event("OnFocus", self.b)
        self.runtime._enumerate_documents = lambda: [self.a, self.b]
        self.a_id = self.runtime._id_for_document(self.a)
        self._wait = BRIDGE.ACTIVATION_WAIT_SECONDS
        BRIDGE.ACTIVATION_WAIT_SECONDS = 0.3

    def tearDown(self):
        BRIDGE.ACTIVATION_WAIT_SECONDS = self._wait

    def test_focused_document_undoes_in_place(self):
        self.desktop.current = self.a
        self.runtime.focus.on_event("OnFocus", self.a)
        self.a.undo.titles = ["Hermes: set E1:F2"]
        result = self.runtime.undo({"document_id": self.a_id})
        self.assertEqual(self.a.undo.undone, ["Hermes: set E1:F2"])
        self.assertFalse(result["activated_document"])
        self.assertEqual(self.a.activations, 0)

    def test_background_document_is_activated_before_any_undo(self):
        for title in ("Hermes: set E1:F2", "Hermes: create sheet HermesSweep", BRIDGE.PLAN_UNDO_TITLE):
            self.desktop.current = self.b
            self.runtime.focus.on_event("OnFocus", self.b)
            self.a.undo.titles = [title]
            result = self.runtime.undo({"document_id": self.a_id})
            self.assertEqual(self.a.undo.undone[-1], title)
            self.assertTrue(result["activated_document"])
        self.assertEqual(self.a.activations, 3)
        self.assertEqual(self.b.undo.undone, [])

    def test_refused_when_focus_cannot_be_confirmed(self):
        self.a.undo.titles = ["Hermes: create sheet HermesSweep"]
        self.a.focus_on_activate = False  # desktop says current, but LibreOffice never reports OnFocus
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            self.runtime.undo({"document_id": self.a_id})
        self.assertEqual(caught.exception.category, "Validation")
        self.assertEqual(caught.exception.state, "not_started")
        self.assertEqual(self.a.undo.undone, [])
        self.assertIs(self.desktop.current, self.a)

    def test_unknown_focus_fails_closed(self):
        self.runtime.focus = BRIDGE.FocusState()
        self.a.runtime_focus = self.runtime.focus
        self.a.focus_on_activate = False
        self.desktop.current = self.a
        self.a.undo.titles = ["Hermes: set A1"]
        with self.assertRaises(BRIDGE.BridgeFault):
            self.runtime.undo({"document_id": self.a_id})
        self.assertEqual(self.a.undo.undone, [])

    def test_undo_stack_change_between_check_and_undo_is_stale(self):
        self.desktop.current = self.a
        self.runtime.focus.on_event("OnFocus", self.a)
        self.a.undo.titles = ["Hermes: set A1"]
        original = self.runtime.dispatch

        def dispatch(fn, *, mutation=False):
            if mutation:
                self.a.undo.titles.append("Typing: user")
            return original(fn, mutation=mutation)

        self.runtime.dispatch = dispatch
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            self.runtime.undo({"document_id": self.a_id})
        self.assertEqual(caught.exception.category, "Stale")
        self.assertEqual(caught.exception.state, "not_started")
        self.assertEqual(self.a.undo.undone, [])


class PlanFixTests(unittest.TestCase):
    def test_write_formula_targets_the_cell_and_plan_title_marks_new_sheets(self):
        runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)
        runtime.async_callback = ImmediateCallback()
        runtime.logger = logging.getLogger("plan-test")
        formulas = []
        undo = Undo([])

        class Cell:
            def setFormula(self, value):
                formulas.append(value)

        class OneCellRange:  # like LibreOffice's range object: no setFormula
            def getCellByPosition(self, column, row):
                assert (column, row) == (0, 0)
                return Cell()

        names = ["Sheet1"]
        sheets = types.SimpleNamespace(
            getElementNames=lambda: list(names),
            insertNewByName=lambda name, _pos: names.append(name),
            hasByName=lambda name: name in names,
        )
        doc = types.SimpleNamespace(getSheets=lambda: sheets, getUndoManager=lambda: undo)
        runtime.resolve_document = lambda _id, _kind=None: doc
        runtime._sheet_and_range = lambda _doc, _sheet, _address: (None, OneCellRange())
        runtime._read_range = lambda _doc, _sheet, _address: {"values": [[""]], "formulas": [["=B2*2"]]}
        for operations, title in (
            ([{"kind": "write_formula", "sheet": "Sheet1", "address": "B3", "formula": "=B2*2"}], BRIDGE.PLAN_UNDO_TITLE),
            ([{"kind": "create_sheet", "name": "New"},
              {"kind": "write_formula", "sheet": "New", "address": "B3", "formula": "=B2*2"}], BRIDGE.PLAN_WITH_SHEETS_UNDO_TITLE),
        ):
            plan = {"version": 1, "outcome": "execute", "summary": "s", "operations": operations,
                    "verification": [{"kind": "range_formulas_equal", "sheet": "Sheet1", "address": "B3",
                                      "expectedFormulas": [["=B2*2"]]}]}
            result = runtime.execute_plan({"document_id": "lo:x:y", "plan": plan})
            self.assertTrue(result["verified"])
            self.assertEqual(undo.contexts[-1], title)
        self.assertEqual(formulas, ["=B2*2", "=B2*2"])


if __name__ == "__main__":
    unittest.main()
