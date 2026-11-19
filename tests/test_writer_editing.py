# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Structured Writer editing against an in-memory text model (no LibreOffice).

The model keeps paragraphs as objects with text and a paragraph style, a cursor that moves by
paragraph, a paragraph break that splits the paragraph at the cursor (both halves keep the
style), and an undo manager whose context snapshots the whole body, like LibreOffice's list
actions. Native behaviour is checked separately on real documents.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402

STYLES = {"Standard": "Default Paragraph Style", "Heading 1": "Heading 1", "Text body": "Body Text", "Title": "Title"}


class Para:
    def __init__(self, text, style="Standard", doc=None):
        self.String, self.ParaStyleName, self.OutlineLevel, self.doc = text, style, 0, doc

    def supportsService(self, _service):
        return False

    def setPropertyValue(self, name, value):
        setattr(self, name, value)

    def getStart(self):
        return (self, 0)

    def getEnd(self):
        return (self, len(self.String))


class Cursor:
    def __init__(self, doc, para, offset):
        self.doc, self.para, self.offset = doc, para, offset

    def index(self):
        return self.doc.paragraphs.index(self.para)

    def gotoStart(self, _expand):
        self.para, self.offset = self.doc.paragraphs[0], 0

    def gotoPreviousParagraph(self, _expand):
        self.para, self.offset = self.doc.paragraphs[self.index() - 1], 0
        return True

    def gotoNextParagraph(self, _expand):
        self.para, self.offset = self.doc.paragraphs[self.index() + 1], 0
        return True

    def setPropertyValue(self, name, value):
        assert name == "ParaStyleName"
        if self.doc.fail_on_style == value:
            raise RuntimeError("injected failure")
        assert self.doc.undo.open is not None, "changes happen inside the undo context"
        self.para.ParaStyleName = value


class Undo(FAKES.UndoManager):
    def __init__(self, doc):
        super().__init__()
        self.doc = doc

    def enterUndoContext(self, title):
        self.open = [title, [(p.String, p.ParaStyleName) for p in self.doc.paragraphs]]
        self.touched = False

    def leaveUndoContext(self):
        title, snapshot = self.open
        self.open = None
        if self.touched or [(p.String, p.ParaStyleName) for p in self.doc.paragraphs] != snapshot:
            self.steps.append((title, lambda: self.doc.restore(snapshot)))


class TextDoc(FAKES.Document):
    def __init__(self, paragraphs):
        super().__init__("writer")
        self.undo = Undo(self)
        self.fail_on_style = None
        self.paragraphs = [Para(t, s) if isinstance(t, str) else t for t, s in
                           ((p, "Standard") if isinstance(p, str) else p for p in paragraphs)]

    def restore(self, snapshot):
        self.paragraphs = [Para(text, style) for text, style in snapshot]

    def state(self):
        return [(p.String, p.ParaStyleName) for p in self.paragraphs]

    def getStyleFamilies(self):
        styles = type("Styles", (), {
            "hasByName": lambda _s, name: name in STYLES,
            "getElementNames": lambda _s: tuple(STYLES),
            "getByName": lambda _s, name: type("Style", (), {"DisplayName": STYLES[name]})(),
        })()
        return type("Families", (), {"getByName": lambda _s, _name: styles})()

    def getText(self):
        doc = self

        class Text:
            def createEnumeration(self):
                items = list(doc.paragraphs)
                return type("Enum", (), {"hasMoreElements": lambda _e: bool(items), "nextElement": lambda _e: items.pop(0)})()

            def createTextCursor(self):
                return Cursor(doc, doc.paragraphs[0], 0)

            def createTextCursorByRange(self, where):
                return Cursor(doc, where[0], where[1])

            def insertString(self, cursor, text, _absorb):
                assert doc.undo.open is not None
                p = cursor.para
                p.String = p.String[:cursor.offset] + text + p.String[cursor.offset:]
                cursor.offset += len(text)

            def insertControlCharacter(self, cursor, character, _absorb):
                assert character == 0 and doc.undo.open is not None
                p = cursor.para
                tail = Para(p.String[cursor.offset:], p.ParaStyleName)
                p.String = p.String[:cursor.offset]
                doc.paragraphs.insert(doc.paragraphs.index(p) + 1, tail)
                cursor.para, cursor.offset = tail, 0
        return Text()


class Harness(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def insert(self, runtime, **args):
        return runtime.invoke("writer_insert_paragraphs", {"document_id": self.document_id, **args})


DRAFT = [{"text": "Quarterly review", "style": "Title"}, {"text": "Goals", "style": "Heading 1"},
         {"text": "Notes about goals.", "style": "Body Text"}, {"text": "Plain"}]


class InsertParagraphsTests(Harness):
    def test_empty_document_gets_a_styled_draft_in_one_undo_step(self):
        doc = TextDoc([""])
        runtime = self.runtime(doc)
        data = self.insert(runtime, paragraphs=DRAFT, position="end")
        self.assertEqual(doc.state(), [("Quarterly review", "Title"), ("Goals", "Heading 1"),
                                       ("Notes about goals.", "Text body"), ("Plain", "Text body")])
        self.assertEqual([p["paragraph"] for p in data["paragraphs"]], [1, 2, 3, 4])
        self.assertTrue(data["verified"] and data["undoable"])
        self.assertEqual(len(doc.undo.steps), 1)
        doc.undo.undo()
        self.assertEqual(doc.state(), [("", "Standard")])

    def test_after_a_paragraph_and_at_the_start_keep_neighbours_intact(self):
        doc = TextDoc(["First", ("Second", "Heading 1"), "Third"])
        runtime = self.runtime(doc)
        data = self.insert(runtime, paragraphs=[{"text": "A", "style": "Title"}, {"text": "B"}],
                           position="after", after_paragraph=2, expected_text="Second")
        self.assertEqual(doc.state(), [("First", "Standard"), ("Second", "Heading 1"), ("A", "Title"),
                                       ("B", "Title"), ("Third", "Standard")])
        self.assertEqual([p["paragraph"] for p in data["paragraphs"]], [3, 4])
        doc.undo.undo()
        self.insert(runtime, paragraphs=[{"text": "X", "style": "Heading 1"}, {"text": "Y", "style": "Body Text"}], position="start")
        self.assertEqual(doc.state()[:3], [("X", "Heading 1"), ("Y", "Text body"), ("First", "Standard")])
        doc.undo.undo()
        self.assertEqual(doc.state(), [("First", "Standard"), ("Second", "Heading 1"), ("Third", "Standard")])

    def test_invalid_style_moved_anchor_and_bad_items_change_nothing(self):
        doc = TextDoc(["First", "Second"])
        runtime = self.runtime(doc)
        before = doc.state()
        cases = [
            ({"paragraphs": [{"text": "a", "style": "Heading 1"}, {"text": "b", "style": "Heading1"}], "position": "end"}, "Validation"),
            ({"paragraphs": [{"text": "a"}], "position": "after", "after_paragraph": 2, "expected_text": "Changed"}, "Stale"),
            ({"paragraphs": [{"text": "a"}], "position": "after", "after_paragraph": 9, "expected_text": "x"}, "NotFound"),
            ({"paragraphs": [{"text": "a\nb"}], "position": "end"}, "Validation"),
            ({"paragraphs": [], "position": "end"}, "Validation"),
            ({"paragraphs": [{"text": "a"}] * 51, "position": "end"}, "Validation"),
            ({"paragraphs": [{"text": "a", "bold": True}], "position": "end"}, "Validation"),
            ({"paragraphs": [{"text": "a"}], "position": "end", "after_paragraph": 1}, "Validation"),
            ({"paragraphs": [{"text": "a"}], "position": "after"}, "Validation"),
        ]
        for args, category in cases:
            self.assertEqual(self.fault(runtime, "writer_insert_paragraphs", **args).category, category, args)
        self.assertEqual(doc.state(), before)
        self.assertEqual(doc.undo.steps, [])

    def test_failure_after_the_first_change_is_uncertain_not_success(self):
        doc = TextDoc(["First"])
        doc.fail_on_style = "Text body"
        runtime = self.runtime(doc)
        fault = self.fault(runtime, "writer_insert_paragraphs", paragraphs=DRAFT, position="end")
        self.assertEqual((fault.category, fault.state), ("ExecutionUncertain", "started"))
        # the partial change is one Hermes step that restores the baseline
        self.assertEqual(len(doc.undo.steps), 1)
        doc.undo.undo()
        self.assertEqual(doc.state(), [("First", "Standard")])


class Found:
    """A found text range that, like LibreOffice's, keeps its place when earlier text changes."""

    def __init__(self, doc, para, start, end):
        self.doc, self.para, self.start, self.end = doc, para, start, end
        doc.ranges.append(self)

    def getString(self):
        return self.para.String[self.start:self.end]

    def setString(self, value):
        assert self.doc.undo.open is not None
        if self.doc.fail_on_replace == value:
            raise RuntimeError("injected failure")
        delta = len(value) - (self.end - self.start)
        self.para.String = self.para.String[:self.start] + value + self.para.String[self.end:]
        for other in self.doc.ranges:
            if other is not self and other.para is self.para and other.start >= self.end:
                other.start += delta
                other.end += delta
        self.end = self.start + len(value)

    def getText(self):
        return self.doc.body

    def getEnd(self):
        return ("end", self)

    def key(self, which="start"):
        return (self.doc.paragraphs.index(self.para), self.start if which == "start" else self.end)


class SearchDoc(TextDoc):
    def __init__(self, paragraphs):
        super().__init__(paragraphs)
        self.ranges, self.fail_on_replace = [], None
        doc = self
        self.body = type("Body", (), {"compareRegionStarts": lambda _s, a, b: doc.compare(a, b)})()

    @staticmethod
    def position(item):
        return item[1].key("end") if isinstance(item, tuple) else item.key("start")

    def compare(self, a, b):
        pa, pb = self.position(a), self.position(b)
        return 1 if pa < pb else 0 if pa == pb else -1

    def createSearchDescriptor(self):
        return type("Descriptor", (), {})()

    def findAll(self, descriptor):
        found = []
        for para in self.paragraphs:
            haystack = para.String if descriptor.SearchCaseSensitive else para.String.lower()
            needle = descriptor.SearchString if descriptor.SearchCaseSensitive else descriptor.SearchString.lower()
            start = haystack.find(needle)
            while start != -1:
                found.append(Found(self, para, start, start + len(needle)))
                start = haystack.find(needle, start + len(needle))
        return type("Found", (), {"getCount": lambda _s: len(found), "getByIndex": lambda _s, i: found[i]})()


TYPOS = ["Please recieve the parcel and keep the seperate invoices apart.",
         "The outage occured twice; it occured again on Friday.", "We will definately wait untill noon.",
         "Zürich café ✓ teh end."]


class ApplyEditsTests(Harness):
    def edits(self, runtime, edits, **extra):
        return runtime.invoke("writer_apply_edits", {"document_id": self.document_id, "edits": edits, **extra})

    def test_an_ambiguous_or_missing_target_refuses_the_whole_batch(self):
        doc = SearchDoc(TYPOS)
        runtime = self.runtime(doc)
        before = doc.state()
        for edits in ([{"find": "recieve", "replace": "receive"}, {"find": "occured", "replace": "occurred"}],
                      [{"find": "recieve", "replace": "receive"}, {"find": "absent", "replace": "x"}],
                      [{"find": "occured", "replace": "occurred", "occurrence": 3}],
                      [{"find": "seperate", "replace": "separate"}, {"find": "seperate invoices", "replace": "x"}]):
            fault = self.fault(runtime, "writer_apply_edits", edits=edits)
            self.assertEqual((fault.category, fault.state), ("Validation", "not_started"), edits)
            self.assertIn("nothing was changed", str(fault))
        self.assertEqual(doc.state(), before)
        self.assertEqual(doc.undo.steps, [])

    def test_a_valid_batch_changes_only_the_intended_ranges_in_one_undo_step(self):
        doc = SearchDoc(TYPOS)
        runtime = self.runtime(doc)
        before = doc.state()
        data = self.edits(runtime, [
            {"find": "recieve", "replace": "receive"}, {"find": "seperate", "replace": "separate"},
            {"find": "occured", "replace": "occurred", "occurrence": 2}, {"find": "occured", "replace": "occurred", "occurrence": 1},
            {"find": "definately", "replace": "definitely"}, {"find": "untill", "replace": "until"},
            {"find": "teh", "replace": "the"}, {"find": "Zürich café", "replace": "Zürich Café"}])
        self.assertTrue(data["verified"] and data["undoable"])
        self.assertEqual(data["applied"], 8)
        self.assertEqual([p[0] for p in doc.state()], [
            "Please receive the parcel and keep the separate invoices apart.",
            "The outage occurred twice; it occurred again on Friday.", "We will definitely wait until noon.",
            "Zürich Café ✓ the end."])
        self.assertEqual(len(doc.undo.steps), 1)
        doc.undo.undo()
        self.assertEqual(doc.state(), before)

    def test_changed_document_and_mid_operation_failure(self):
        doc = SearchDoc(TYPOS)
        runtime = self.runtime(doc)
        fingerprint = runtime.invoke("writer_get_text", {"document_id": self.document_id})["fingerprint"]
        doc.paragraphs[3].String += "!"
        self.assertEqual(self.fault(runtime, "writer_apply_edits", edits=[{"find": "teh", "replace": "the"}], if_match=fingerprint).category, "Stale")
        fresh = runtime.invoke("writer_get_text", {"document_id": self.document_id})["fingerprint"]
        doc.fail_on_replace = "until"
        fault = self.fault(runtime, "writer_apply_edits", if_match=fresh,
                           edits=[{"find": "teh", "replace": "the"}, {"find": "untill", "replace": "until"}])
        self.assertEqual((fault.category, fault.state), ("ExecutionUncertain", "started"))
        for bad in ([], [{"find": "a", "replace": "a"}], [{"find": "", "replace": "x"}], [{"find": "a\nb", "replace": "x"}],
                    [{"find": "a", "replace": "b", "occurrence": 0}], [{"find": "a", "replace": "b", "extra": 1}]):
            self.assertEqual(self.fault(runtime, "writer_apply_edits", edits=bad).category, "Validation", bad)


class Selection:
    """One selected text range with character properties and a text that accepts a comment."""

    def __init__(self, doc, text):
        self.doc, self.text, self.props = doc, text, {"CharWeight": 100.0, "CharHeight": 12.0, "CharFontName": "Liberation Serif",
                                                       "CharColor": -1, "CharBackColor": -1, "CharUnderline": 0,
                                                       "CharStrikeout": 0, "CharPosture": "NONE"}

    def getString(self):
        return self.text

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        assert self.doc.undo.open is not None
        self.doc.undo.touched = True
        self.props[name] = value

    def getText(self):
        doc, sel = self.doc, self

        class Text:
            def insertTextContent(self, where, field, absorb):
                assert where is sel and absorb and doc.undo.open is not None
                doc.undo.touched = True
                field.anchor = sel.text
                doc.fields.append(field)
        return Text()


class Annotation:
    def __init__(self, author="", content="", anchor=""):
        self.Author, self.Content, self.anchor = author, content, anchor

    def supportsService(self, name):
        return name == "com.sun.star.text.textfield.Annotation"

    def getAnchor(self):
        anchor = self.anchor
        return type("Anchor", (), {"getString": lambda _s: anchor})()


class ReviewDoc(TextDoc):
    def __init__(self, selected):
        super().__init__(["The launch moved to May."])
        self.selected = [Selection(self, text) for text in selected]
        self.fields = [Annotation("Existing Reviewer", "Existing note", "")]
        doc = self

        class Controller:
            def getSelection(self):
                return type("Sel", (), {"getCount": lambda _s: len(doc.selected), "getByIndex": lambda _s, i: doc.selected[i]})()
        self.controller = Controller()

    def getCurrentController(self):
        return self.controller

    def createInstance(self, name):
        assert name == "com.sun.star.text.textfield.Annotation"
        return Annotation()

    def getTextFields(self):
        items = list(self.fields)
        return type("Fields", (), {"createEnumeration": lambda _s: type("E", (), {
            "hasMoreElements": lambda _e: bool(items), "nextElement": lambda _e: items.pop(0)})()})()


class FormatAndCommentTests(Harness):
    def setUp(self):
        original = FAKES.OPS.uno.Enum
        FAKES.OPS.uno.Enum = lambda _type, value: value  # the bridge loader's uno stub returns tuples
        self.addCleanup(setattr, FAKES.OPS.uno, "Enum", original)
        # The cursor walk in _model_range needs LibreOffice's text model; it is checked natively.
        patcher = unittest.mock.patch.object(FAKES.BRIDGE.BridgeRuntime, "_model_range", lambda _self, selection: selection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_formatting_applies_to_the_exact_selection_and_reads_back(self):
        doc = ReviewDoc(["4 percent"])
        runtime = self.runtime(doc)
        data = runtime.invoke("writer_format_selection", {"document_id": self.document_id, "expected_text": "4 percent",
                                                          "format": {"bold": True, "italic": True, "color": "#1b7563",
                                                                     "highlight": "#FFFF00", "font_size": 14}})
        self.assertTrue(data["verified"] and data["undoable"])
        self.assertEqual((data["previous"]["bold"], data["applied"]["bold"], data["applied"]["color"], data["applied"]["font_size"]),
                         (False, True, "#1B7563", 14.0))
        self.assertEqual(doc.selected[0].props["CharBackColor"], 0xFFFF00)

    def test_stale_empty_or_multiple_selections_and_bad_formats_are_refused(self):
        runtime = self.runtime(ReviewDoc(["4 percent"]))
        self.assertEqual(self.fault(runtime, "writer_format_selection", expected_text="other", format={"bold": True}).category, "Stale")
        for fmt in ({}, {"bold": "yes"}, {"color": "red"}, {"font_size": 0}, {"highlight": "#12345"}, {"shadow": True}):
            self.assertEqual(self.fault(runtime, "writer_format_selection", expected_text="4 percent", format=fmt).category, "Validation", fmt)
        runtime = self.runtime(ReviewDoc([""]))
        self.assertEqual(self.fault(runtime, "writer_add_comment", expected_text="", text="x").category, "Validation")
        runtime = self.runtime(ReviewDoc(["a", "b"]))
        self.assertEqual(self.fault(runtime, "writer_format_selection", expected_text="a", format={"bold": True}).category, "Validation")

    def test_comment_is_anchored_to_the_selection_and_listed_with_existing_ones(self):
        doc = ReviewDoc(["Costs rose by 4 percent."])
        runtime = self.runtime(doc)
        data = runtime.invoke("writer_add_comment", {"document_id": self.document_id, "expected_text": "Costs rose by 4 percent.",
                                                     "text": "Source for the 4 percent?"})
        self.assertTrue(data["verified"])
        self.assertEqual(data["comment_count"], 2)
        listed = runtime.invoke("writer_get_comments", {"document_id": self.document_id})
        self.assertEqual([(c["author"], c["text"], c["anchor_text"]) for c in listed["comments"]],
                         [("Existing Reviewer", "Existing note", ""), ("Hermes", "Source for the 4 percent?", "Costs rose by 4 percent.")])
        self.assertEqual(self.fault(runtime, "writer_add_comment", expected_text="changed", text="x").category, "Stale")


if __name__ == "__main__":
    unittest.main()
