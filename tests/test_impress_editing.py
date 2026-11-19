# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Impress layout change, whole-shape text formatting and basic shapes (fakes; native checks separate)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

OPS = FAKES.OPS


class State:
    def __init__(self, value):
        self.value = value


class TextCursor:
    def __init__(self, shape):
        self.shape = shape

    def gotoStart(self, _expand):
        return None

    def gotoEnd(self, _expand):
        return None

    def getPropertyState(self, name):
        if name in self.shape.mixed:
            return State("AMBIGUOUS_VALUE")
        return State("DIRECT_VALUE" if name in self.shape.direct else "DEFAULT_VALUE")

    def getPropertyValue(self, name):
        return self.shape.direct.get(name, self.shape.defaults.get(name))

    def setPropertyValue(self, name, value):
        self.shape.direct[name] = value

    def setPropertyToDefault(self, name):
        self.shape.direct.pop(name, None)


class TextShape(FAKES.Shape):
    def __init__(self, kind, text=""):
        super().__init__(kind, text)
        self.direct, self.mixed = {}, set()
        self.defaults = {"CharWeight": 100.0, "CharHeight": 32.0, "CharPosture": "NONE", "CharUnderline": 0,
                         "CharFontName": "Liberation Sans", "CharColor": -1, "CharStrikeout": 0, "CharBackColor": -1}
        self.props = {}

    def getText(self):
        shape = self
        return type("Text", (), {"createTextCursor": lambda _s: TextCursor(shape)})()

    def setPropertyValue(self, name, value):
        self.props[name] = value

    def getPropertyValue(self, name):
        return self.props.get(name, 0)


class LayoutPage(FAKES.Page):
    def setPropertyValue(self, name, value):
        assert name == "Layout"
        self.Layout = value  # LibreOffice keeps placeholders that hold text (probed live)


class Deck(FAKES.Impress):
    def __init__(self):
        super().__init__()
        page = LayoutPage()
        page.Layout = 1
        page.shapes = [TextShape(OPS.TITLE_SHAPE, "Title"), TextShape("com.sun.star.presentation.OutlinerShape", "Body")]
        self.pages = FAKES.Pages([page])

    def createInstance(self, service):
        return TextShape(service)


class ImpressEditingTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def setUp(self):
        original = OPS.uno.Enum
        OPS.uno.Enum = lambda _type, value: value
        self.addCleanup(setattr, OPS.uno, "Enum", original)
        struct = OPS.uno.createUnoStruct
        OPS.uno.createUnoStruct = lambda _name: type("Struct", (), {})()
        self.addCleanup(setattr, OPS.uno, "createUnoStruct", struct)

    def test_layout_change_is_one_bridge_step_that_restores_the_previous_layout(self):
        doc = Deck()
        runtime = self.runtime(doc)
        data = runtime.invoke("impress_set_slide_layout", {"document_id": self.document_id, "slide": 1, "layout": "title_only"})
        self.assertEqual((data["previous_layout"], data["layout"], data["text_preserved"], data["undoable"]),
                         ("title_content", "title_only", True, True))
        self.assertEqual(doc.pages.pages[0].Layout, 19)
        self.assertEqual(doc.undo.steps[-1][0], "Hermes: layout of slide 1")
        doc.undo.undo()
        self.assertEqual(doc.pages.pages[0].Layout, 1)
        self.assertEqual(self.fault(runtime, "impress_set_slide_layout", slide=1, layout="fancy").category, "Validation")
        self.assertEqual(self.fault(runtime, "impress_set_slide_layout", slide=4, layout="blank").category, "NotFound")

    def test_layout_changes_undo_cannot_restore_exactly_are_refused_before_writing(self):
        doc = Deck()
        runtime = self.runtime(doc)
        self.assertEqual(self.fault(runtime, "impress_set_slide_layout", slide=1, layout="title").category, "Validation")
        page = doc.pages.pages[0]
        self.assertEqual((page.Layout, len(doc.undo.steps)), (1, 0))
        page.Layout = 0  # placeholders no longer match the layout number (as in many saved decks)
        self.assertEqual(self.fault(runtime, "impress_set_slide_layout", slide=1, layout="title_only").category, "Validation")
        self.assertEqual((page.Layout, len(doc.undo.steps)), (0, 0))
        page.Layout = 1
        data = runtime.invoke("impress_set_slide_layout", {"document_id": self.document_id, "slide": 1, "layout": "title_content"})
        self.assertEqual((data["changed"], data["undoable"]), (False, False))

    def test_formatting_refuses_mixed_text_and_undo_restores_direct_and_default_values(self):
        doc = Deck()
        runtime = self.runtime(doc)
        title = doc.pages.pages[0].shapes[0]
        title.direct["CharHeight"] = 40.0
        data = runtime.invoke("impress_format_text", {"document_id": self.document_id, "slide": 1, "placeholder": "title",
                                                      "format": {"bold": True, "font_size": 54, "color": "#1B7563"}})
        self.assertEqual(data["applied"], {"bold": True, "font_size": 54.0, "color": "#1B7563"})
        doc.undo.undo()
        self.assertEqual(title.direct, {"CharHeight": 40.0})  # weight and colour back to the style default
        title.mixed.add("CharWeight")
        self.assertEqual(self.fault(runtime, "impress_format_text", slide=1, placeholder="title", format={"bold": True}).category, "Validation")
        self.assertEqual(title.direct, {"CharHeight": 40.0})
        for bad in ({}, {"highlight": "#FFFFFF"}, {"bold": 1}):
            self.assertEqual(self.fault(runtime, "impress_format_text", slide=1, shape=1, format=bad).category, "Validation", bad)

    def test_shapes_are_added_with_geometry_and_colours_and_removed_by_undo(self):
        doc = Deck()
        runtime = self.runtime(doc)
        data = runtime.invoke("impress_add_shape", {"document_id": self.document_id, "slide": 1, "kind": "rounded_rectangle",
                                                    "x_cm": 2, "y_cm": 3, "width_cm": 6, "height_cm": 3,
                                                    "fill_color": "#1B7563", "line_color": "#0C1117", "text": "Step 1"})
        shape = doc.pages.pages[0].shapes[-1]
        self.assertEqual((data["shape"], shape.text, shape.props["FillColor"], shape.props["CornerRadius"]), (3, "Step 1", 0x1B7563, 500))
        self.assertEqual((shape.position.X, shape.size.Width), (2000, 6000))
        doc.undo.undo()
        self.assertEqual(doc.pages.pages[0].getCount(), 2)
        for bad in ({"kind": "star"}, {"kind": "line", "fill_color": "#000000"}, {"kind": "line", "text": "x"},
                    {"kind": "ellipse", "width_cm": 0}, {"kind": "ellipse", "fill_color": "green"}):
            args = {"slide": 1, "kind": "ellipse", "x_cm": 1, "y_cm": 1, "width_cm": 2, "height_cm": 2, **bad}
            self.assertEqual(self.fault(runtime, "impress_add_shape", **args).category, "Validation", bad)


if __name__ == "__main__":
    unittest.main()
