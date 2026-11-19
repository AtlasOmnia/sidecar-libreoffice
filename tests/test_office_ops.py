# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Sheet, find-and-replace, style, selection and slide operations against in-memory fakes.

The fakes model the one LibreOffice behaviour these operations are built around: an undo manager
that records nothing for slides and shapes added through the API, so the bridge's own undo action
has to remove and rebuild them.
"""
from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension_bridge import BRIDGE, ImmediateCallback  # noqa: E402

import plugin  # noqa: E402

OPS = sys.modules["office_ops"]


class UndoManager:
    """Keeps real steps; a context is recorded only when something was added inside it."""

    def __init__(self):
        self.steps = []      # (title, undo callable or None)
        self.open = None
        self.locked = 0
        self.native_records = False

    def enterUndoContext(self, title):
        self.open = [title, False]

    def note_native_change(self, undo_fn=None):
        if self.open and not self.locked and self.native_records:
            self.open[1] = True
            self.open.append(undo_fn)

    def leaveUndoContext(self):
        title, changed, *rest = self.open
        self.open = None
        if changed:
            self.steps.append((title, rest[0] if rest else None))

    def lock(self):
        self.locked += 1

    def unlock(self):
        self.locked -= 1

    def addUndoAction(self, action):
        assert not self.locked
        self.steps.append((action.Title, action.undo))
        self.last_action = action

    def isUndoPossible(self):
        return bool(self.steps)

    def getCurrentUndoActionTitle(self):
        return self.steps[-1][0]

    def getAllUndoActionTitles(self):  # newest first, as LibreOffice lists them
        return tuple(step[0] for step in reversed(self.steps))

    def undo(self):
        _title, undo_fn = self.steps.pop()
        if undo_fn:
            undo_fn()


class Shape:
    def __init__(self, kind, text=""):
        self.ShapeType, self.Name, self.text = kind, "", text
        self.position, self.size = None, None

    def getString(self):
        return self.text

    def setString(self, value):
        self.text = value

    def setPosition(self, value):
        self.position = value

    def setSize(self, value):
        self.size = value

    def getPosition(self):
        return self.position

    def getSize(self):
        return self.size


class Page:
    def __init__(self):
        self.shapes, self.Name, self.Layout, self.Width, self.Height = [], "", 20, 28000, 15750

    def getCount(self):
        return len(self.shapes)

    def getByIndex(self, index):
        return self.shapes[index]

    def setPropertyValue(self, name, value):
        assert name == "Layout"
        self.Layout = value
        self.shapes = {
            0: [Shape(OPS.TITLE_SHAPE), Shape("com.sun.star.presentation.SubtitleShape")],
            1: [Shape(OPS.TITLE_SHAPE), Shape("com.sun.star.presentation.OutlinerShape")],
            19: [Shape(OPS.TITLE_SHAPE)], 20: [],
        }[value]

    def add(self, shape):
        self.shapes.append(shape)

    def remove(self, shape):
        self.shapes.remove(shape)

    def createReplaceDescriptor(self):
        values = {}
        return type("Descriptor", (), {
            "setSearchString": lambda _s, v: values.__setitem__("search", v),
            "setReplaceString": lambda _s, v: values.__setitem__("replace", v),
            "setPropertyValue": lambda _s, k, v: values.__setitem__(k, v),
            "values": values,
        })()

    def replaceAll(self, descriptor):
        count = 0
        for shape in self.shapes:
            count += shape.text.count(descriptor.values["search"])
            shape.text = shape.text.replace(descriptor.values["search"], descriptor.values["replace"])
        return count


class Pages:
    def __init__(self, pages):
        self.pages = pages

    def getCount(self):
        return len(self.pages)

    def getByIndex(self, index):
        return self.pages[index]

    def insertNewByIndex(self, index):
        page = Page()
        self.pages.insert(index + 1, page)
        return page

    def remove(self, page):
        self.pages.remove(page)


class Document:
    def __init__(self, kind):
        self.kind, self.RuntimeUID, self.Title, self.URL = kind, f"{kind}-1", f"{kind}.doc", ""
        self.undo = UndoManager()

    def getPropertyValue(self, name):
        return self.RuntimeUID

    def supportsService(self, service):
        return service == {"calc": "com.sun.star.sheet.SpreadsheetDocument", "writer": "com.sun.star.text.TextDocument",
                           "impress": "com.sun.star.presentation.PresentationDocument"}[self.kind]

    def getUndoManager(self):
        return self.undo


class Impress(Document):
    def __init__(self):
        super().__init__("impress")
        first = Page()
        first.setPropertyValue("Layout", 0)
        first.shapes[0].text = "Deck title"
        self.pages = Pages([first])

    def getDrawPages(self):
        return self.pages

    def createInstance(self, _service):
        return Shape("com.sun.star.drawing.TextShape")


class Paragraph:
    def __init__(self, text, style="Standard"):
        self.String, self.ParaStyleName, self.OutlineLevel = text, style, 0

    def supportsService(self, _service):
        return False

    def setPropertyValue(self, name, value):
        setattr(self, name, value)


class Writer(Document):
    def __init__(self, paragraphs):
        super().__init__("writer")
        self.undo.native_records = True
        self.paragraphs = [Paragraph(text) for text in paragraphs]

    def getText(self):
        items = list(self.paragraphs)
        return type("Text", (), {"createEnumeration": lambda _s: type("Enum", (), {
            "hasMoreElements": lambda _e: bool(items), "nextElement": lambda _e: items.pop(0)})()})()

    def getStyleFamilies(self):
        shown = {"Standard": "Default Paragraph Style", "Heading 1": "Heading 1", "Text body": "Body Text"}
        styles = type("Styles", (), {
            "hasByName": lambda _s, name: name in shown,
            "getElementNames": lambda _s: tuple(shown),
            "getByName": lambda _s, name: type("Style", (), {"DisplayName": shown[name]})(),
        })()
        return type("Families", (), {"getByName": lambda _s, _name: styles})()


class Sheets:
    def __init__(self, names):
        self.names = list(names)

    def hasByName(self, name):
        return name in self.names

    def getElementNames(self):
        return tuple(self.names)

    def getByName(self, name):
        sheets = self
        return type("Sheet", (), {"setName": lambda _s, new: sheets.names.__setitem__(sheets.names.index(name), new)})()

    def removeByName(self, name):
        self.names.remove(name)

    def copyByName(self, _name, new, _index):
        self.names.append(new)


class Calc(Document):
    def __init__(self, names=("Sheet1",)):
        super().__init__("calc")
        self.undo.native_records = True
        self.sheets = Sheets(names)

    def getSheets(self):
        return self.sheets


class OfficeOpsTests(unittest.TestCase):
    def runtime(self, doc):
        runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)
        runtime.async_callback = ImmediateCallback()
        runtime.logger = logging.getLogger("office-ops-test")
        runtime.session_nonce = "feedbeef"
        runtime._documents = {}
        runtime._enumerate_documents = lambda: [doc]
        self.document_id = runtime._id_for_document(doc)
        return runtime

    def fault(self, runtime, op, **args):
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.invoke(op, {"document_id": self.document_id, **args})
        return caught.exception

    # -- Impress: the bridge's own undo step --------------------------------

    def test_new_slide_is_one_bridge_undo_step_that_removes_and_rebuilds_it(self):
        doc = Impress()
        runtime = self.runtime(doc)
        data = runtime.invoke("impress_create_slide", {"document_id": self.document_id, "title": "Plan", "body": "One\nTwo"})
        self.assertEqual((data["slide"], data["layout"], data["title_set"], data["body_set"]), (2, "title_content", True, True))
        self.assertTrue(data["undoable"])
        self.assertEqual(doc.undo.steps[-1][0], "Hermes: create slide 2")
        self.assertEqual([shape.text for shape in doc.pages.pages[1].shapes], ["Plan", "One\nTwo"])
        doc.undo.undo()
        self.assertEqual(doc.pages.getCount(), 1)
        doc.undo.last_action.redo()
        self.assertEqual([shape.text for shape in doc.pages.pages[1].shapes], ["Plan", "One\nTwo"])
        self.assertEqual(doc.undo.locked, 0)

    def test_slide_goes_after_the_named_slide_and_layout_must_fit_the_text(self):
        doc = Impress()
        runtime = self.runtime(doc)
        runtime.invoke("impress_create_slide", {"document_id": self.document_id, "title": "Last"})
        data = runtime.invoke("impress_create_slide", {"document_id": self.document_id, "title": "Middle", "after_slide": 1})
        self.assertEqual(data["slide"], 2)
        self.assertEqual([page.shapes[0].text for page in doc.pages.pages], ["Deck title", "Middle", "Last"])
        self.assertEqual(self.fault(runtime, "impress_create_slide", body="x", layout="title_only").category, "Validation")
        self.assertEqual(self.fault(runtime, "impress_create_slide", after_slide=9).category, "NotFound")
        self.assertEqual(doc.pages.getCount(), 3)

    def test_shape_text_undo_restores_the_previous_text(self):
        doc = Impress()
        runtime = self.runtime(doc)
        data = runtime.invoke("impress_set_shape_text", {"document_id": self.document_id, "slide": 1, "placeholder": "title", "text": "New"})
        self.assertEqual((data["previous_text"], data["text"], data["undoable"]), ("Deck title", "New", True))
        doc.undo.undo()
        self.assertEqual(doc.pages.pages[0].shapes[0].text, "Deck title")
        self.assertEqual(self.fault(runtime, "impress_set_shape_text", slide=1, text="x").category, "Validation")
        self.assertEqual(self.fault(runtime, "impress_set_shape_text", slide=1, text="x", shape=1, placeholder="title").category, "Validation")
        self.assertEqual(self.fault(runtime, "impress_set_shape_text", slide=1, text="x", shape=7).category, "NotFound")

    def test_text_box_is_added_in_hundredths_of_a_millimetre_and_undo_removes_it(self):
        doc = Impress()
        runtime = self.runtime(doc)
        data = runtime.invoke("impress_add_text_box", {"document_id": self.document_id, "slide": 1, "text": "Note", "x_cm": 1.5, "width_cm": 8})
        box = doc.pages.pages[0].shapes[-1]
        self.assertEqual((data["shape"], box.text, box.position.X, box.position.Y, box.size.Width, box.size.Height),
                         (3, "Note", 1500, 4000, 8000, 2000))
        doc.undo.undo()
        self.assertEqual(doc.pages.pages[0].getCount(), 2)

    def test_presentation_replace_restores_every_changed_shape_on_undo(self):
        doc = Impress()
        runtime = self.runtime(doc)
        runtime.invoke("impress_create_slide", {"document_id": self.document_id, "title": "Deck two", "body": "deck deck"})
        data = runtime.invoke("impress_find_replace", {"document_id": self.document_id, "search": "deck", "replace": "show"})
        self.assertEqual((data["replaced"], data["slides_changed"]), (2, 2))  # the fake's replaceAll is case-sensitive
        doc.undo.undo()
        self.assertEqual([shape.text for page in doc.pages.pages for shape in page.shapes], ["Deck title", "", "Deck two", "deck deck"])
        none = runtime.invoke("impress_find_replace", {"document_id": self.document_id, "search": "absent", "replace": "x"})
        self.assertEqual((none["replaced"], none["undoable"]), (0, False))

    def test_only_slide_cannot_be_deleted_and_slide_read_names_placeholders(self):
        doc = Impress()
        runtime = self.runtime(doc)
        self.assertEqual(self.fault(runtime, "impress_delete_slide", slide=1).category, "Validation")
        self.assertEqual(self.fault(runtime, "impress_get_slide", slide=4).category, "NotFound")
        read = runtime.invoke("impress_get_slide", {"document_id": self.document_id, "slide": 1})
        self.assertEqual([(shape["shape"], shape.get("placeholder"), shape["text"]) for shape in read["shapes"]],
                         [(1, "title", "Deck title"), (2, "body", "")])

    def test_step_that_libreoffice_does_not_record_is_reported_as_not_undoable(self):
        doc = Impress()  # this fake's undo manager records nothing for a plain context
        runtime = self.runtime(doc)
        runtime.invoke("impress_create_slide", {"document_id": self.document_id, "title": "Extra"})
        data = runtime.invoke("impress_delete_slide", {"document_id": self.document_id, "slide": 2})
        self.assertEqual((data["deleted"], data["slide_count"], data["undoable"]), (True, 1, False))

    # -- Writer ---------------------------------------------------------------

    def test_structure_lists_styles_and_style_change_checks_text_and_style(self):
        doc = Writer(["Title line", "Body line"])
        runtime = self.runtime(doc)
        structure = runtime.invoke("writer_get_structure", {"document_id": self.document_id})
        self.assertEqual([(p["paragraph"], p["style"], p["preview"]) for p in structure["paragraphs"]],
                         [(1, "Standard", "Title line"), (2, "Standard", "Body line")])
        self.assertEqual(self.fault(runtime, "writer_set_paragraph_style", paragraph=1, style="Heading 1", expected_text="other").category, "Stale")
        self.assertEqual(self.fault(runtime, "writer_set_paragraph_style", paragraph=1, style="Missing").category, "Validation")
        self.assertEqual(self.fault(runtime, "writer_set_paragraph_style", paragraph=5, style="Heading 1").category, "NotFound")
        self.assertEqual(doc.paragraphs[0].ParaStyleName, "Standard")
        data = runtime.invoke("writer_set_paragraph_style", {"document_id": self.document_id, "paragraph": 1, "style": "Heading 1", "expected_text": "Title line"})
        self.assertEqual((data["previous_style"], data["style"], doc.paragraphs[0].ParaStyleName), ("Standard", "Heading 1", "Heading 1"))

    def test_paragraph_style_is_accepted_by_its_displayed_name(self):
        doc = Writer(["Body line"])
        runtime = self.runtime(doc)
        data = runtime.invoke("writer_set_paragraph_style", {"document_id": self.document_id, "paragraph": 1, "style": "Body Text"})
        self.assertEqual((data["style"], doc.paragraphs[0].ParaStyleName), ("Text body", "Text body"))
        runtime.invoke("writer_set_paragraph_style", {"document_id": self.document_id, "paragraph": 1, "style": "Default Paragraph Style"})
        self.assertEqual(doc.paragraphs[0].ParaStyleName, "Standard")

    def test_wrong_document_type_and_unknown_arguments_are_refused_before_any_change(self):
        doc = Writer(["Text"])
        runtime = self.runtime(doc)
        self.assertEqual(self.fault(runtime, "impress_get_slide", slide=1).category, "Validation")
        self.assertEqual(self.fault(runtime, "writer_find_replace", search="a", replace="b", extra=True).category, "Validation")
        self.assertEqual(self.fault(runtime, "writer_find_replace", search="", replace="b").category, "Validation")
        self.assertEqual(doc.undo.steps, [])

    # -- Calc -----------------------------------------------------------------

    def test_sheet_rename_copy_and_delete_guard_names_and_the_last_sheet(self):
        doc = Calc()
        runtime = self.runtime(doc)
        self.assertEqual(self.fault(runtime, "calc_delete_sheet", sheet="Sheet1").category, "Validation")
        self.assertEqual(self.fault(runtime, "calc_rename_sheet", sheet="Nope", new_name="X").category, "NotFound")
        runtime.invoke("calc_copy_sheet", {"document_id": self.document_id, "sheet": "Sheet1", "new_name": "Copy"})
        self.assertEqual(self.fault(runtime, "calc_rename_sheet", sheet="Sheet1", new_name="Copy").category, "Validation")
        runtime.invoke("calc_rename_sheet", {"document_id": self.document_id, "sheet": "Copy", "new_name": "Plan"})
        runtime.invoke("calc_delete_sheet", {"document_id": self.document_id, "sheet": "Sheet1"})
        self.assertEqual(doc.sheets.names, ["Plan"])

    def test_autofill_and_comment_arguments_are_checked_before_dispatch(self):
        runtime = self.runtime(Calc())
        self.assertEqual(self.fault(runtime, "calc_autofill", sheet="Sheet1", address="A1:A2", direction="down", source_count=2).category, "Validation")
        self.assertEqual(self.fault(runtime, "calc_autofill", sheet="Sheet1", address="A1:A5", direction="up").category, "Validation")
        self.assertEqual(self.fault(runtime, "calc_add_comment", sheet="Sheet1", cell="A1:B2", text="x").category, "Validation")
        with self.assertRaises(BRIDGE.RangeValidationError):
            runtime.invoke("calc_range_stats", {"document_id": self.document_id, "sheet": "Sheet1", "address": "not-a-range"})


class RecordingClient:
    calls: list[tuple[str, dict, bool]] = []

    def __init__(self, _port, _token):
        pass

    def invoke(self, op, args, *, mutation=False):
        RecordingClient.calls.append((op, args, mutation))
        return {"ok": True}


class PluginDirectToolTests(unittest.TestCase):
    def call(self, name, args):
        RecordingClient.calls = []
        with mock.patch.object(plugin, "BridgeClient", RecordingClient), mock.patch.object(plugin, "_secret", lambda: "t" * 48):
            return json.loads(plugin._handler_for(name, 8660)(args))

    def test_every_direct_tool_has_a_schema_and_a_bridge_operation_of_the_same_name(self):
        self.assertEqual(sorted(plugin._DIRECT_TOOLS), sorted(OPS.EXTRA_OPERATIONS))
        for name in plugin._DIRECT_TOOLS:
            self.assertIn(name, plugin._SCHEMA_BY_NAME)
            self.assertTrue(callable(getattr(BRIDGE.BridgeRuntime, name)))
        for name in OPS.INTERNAL_OPERATIONS:
            self.assertNotIn(name, plugin._SCHEMA_BY_NAME)  # never offered to the model directly
            self.assertTrue(callable(getattr(BRIDGE.BridgeRuntime, name)))

    def test_reads_are_sent_as_reads_and_changes_as_mutations(self):
        self.call("impress_get_slide", {"document_id": "lo:1", "slide": 2})
        self.assertEqual(RecordingClient.calls, [("impress_get_slide", {"document_id": "lo:1", "slide": 2}, False)])
        self.call("writer_find_replace", {"document_id": "lo:1", "search": "a", "replace": ""})
        self.assertEqual(RecordingClient.calls[0][2], True)
        read_only = {name for name, mutation in plugin._DIRECT_TOOLS.items() if not mutation}
        self.assertEqual(read_only, {"calc_range_stats", "calc_get_comments", "calc_list_charts", "calc_get_rules",
                                     "impress_get_current_slide", "impress_list_layouts", "writer_get_structure", "writer_get_table",
                                     "writer_get_selection", "writer_get_comments", "impress_get_slide"})

    def test_invalid_arguments_never_reach_the_bridge(self):
        bad = [
            ("impress_get_slide", {"document_id": "lo:1", "slide": 0}),
            ("impress_get_slide", {"document_id": "lo:1", "slide": True}),
            ("impress_get_slide", {"document_id": "lo:1"}),
            ("impress_create_slide", {"document_id": "lo:1", "layout": "fancy"}),
            ("impress_add_text_box", {"document_id": "lo:1", "slide": 1, "text": "x", "x_cm": float("nan")}),
            ("writer_find_replace", {"document_id": "lo:1", "search": "", "replace": "x"}),
            ("writer_find_replace", {"document_id": "lo:1", "search": "a", "replace": "x", "match_case": "yes"}),
            ("calc_autofill", {"document_id": "lo:1", "sheet": "S", "address": "A1:A9", "direction": "left"}),
            ("calc_add_comment", {"document_id": "lo:1", "sheet": "S", "cell": "nope", "text": "x"}),
            ("calc_rename_sheet", {"document_id": "lo:1", "sheet": "S", "new_name": "T", "extra": 1}),
        ]
        for name, args in bad:
            result = self.call(name, args)
            self.assertEqual(result["error"]["category"], "Validation", (name, args))
            self.assertEqual(RecordingClient.calls, [], (name, args))


if __name__ == "__main__":
    unittest.main()
