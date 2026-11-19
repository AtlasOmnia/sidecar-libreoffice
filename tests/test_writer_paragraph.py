# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Paragraph-true Writer append and the Hermes-only undo guard (fakes only, no LibreOffice).

The fake Writer models paragraphs, a text cursor, ``insertControlCharacter`` and an undo manager
whose undo context groups every change made inside it, the way LibreOffice's list actions do.
"""
from __future__ import annotations

import logging
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension_bridge import BRIDGE, ImmediateCallback  # noqa: E402


class GroupingUndo:
    """Snapshot on enterUndoContext; one undo restores everything done inside the context."""

    def __init__(self, doc):
        self.doc = doc
        self.stack = []  # (title, snapshot)
        self.contexts = []
        self._open = None

    def enterUndoContext(self, title):
        self.contexts.append(title)
        self._open = (title, list(self.doc.paragraphs))

    def leaveUndoContext(self):
        self.stack.append(self._open)
        self._open = None

    def push_user_step(self, title):
        self.stack.append((title, list(self.doc.paragraphs)))

    def isUndoPossible(self):
        return bool(self.stack)

    def getCurrentUndoActionTitle(self):
        return self.stack[-1][0]

    def getAllUndoActionTitles(self):
        return tuple(step[0] for step in reversed(self.stack))

    def undo(self):
        _title, snapshot = self.stack.pop()
        self.doc.paragraphs = snapshot


class Cursor:
    def __init__(self, doc):
        self.doc = doc
        self.paragraph = 0
        self.offset = 0

    def gotoStart(self, _expand):
        self.paragraph, self.offset = 0, 0

    def gotoEnd(self, _expand):
        self.paragraph = len(self.doc.paragraphs) - 1
        self.offset = len(self.doc.paragraphs[-1])

    def isStartOfParagraph(self):
        return self.offset == 0

    def isEndOfParagraph(self):
        return self.offset == len(self.doc.paragraphs[self.paragraph])


class Writer:
    def __init__(self, uid, paragraphs, desktop, focus):
        self.RuntimeUID = uid
        self.Title = uid
        self.URL = ""
        self.paragraphs = list(paragraphs)
        self.undo_manager = GroupingUndo(self)
        self.calls = []
        outer = self

        class Text:
            def createTextCursor(self):
                return Cursor(outer)

            def getString(self):
                return "\n".join(outer.paragraphs)

            def insertString(self, cursor, text, _absorb):
                outer.calls.append(("string", text, outer.undo_manager._open is not None))
                line = outer.paragraphs[cursor.paragraph]
                outer.paragraphs[cursor.paragraph] = line[:cursor.offset] + text + line[cursor.offset:]
                cursor.offset += len(text)

            def insertControlCharacter(self, cursor, character, _absorb):
                outer.calls.append(("control", character, outer.undo_manager._open is not None))
                assert character == 0, "only PARAGRAPH_BREAK is used"
                line = outer.paragraphs[cursor.paragraph]
                outer.paragraphs[cursor.paragraph:cursor.paragraph + 1] = [line[:cursor.offset], line[cursor.offset:]]
                cursor.paragraph, cursor.offset = cursor.paragraph + 1, 0

            def createEnumeration(self):
                items = iter([types.SimpleNamespace(String=line) for line in outer.paragraphs])
                sentinel = object()
                state = {"next": next(items, sentinel)}

                class Enumeration:
                    def hasMoreElements(self):
                        return state["next"] is not sentinel

                    def nextElement(self):
                        value, state["next"] = state["next"], next(items, sentinel)
                        return value

                return Enumeration()

        self.text = Text()

        class Frame:
            def activate(self):
                desktop.current = outer

            def getContainerWindow(self):
                return types.SimpleNamespace(toFront=lambda: None, setFocus=lambda: focus.on_event("OnFocus", outer))

            def getComponentWindow(self):
                return self.getContainerWindow()

        self.controller = types.SimpleNamespace(getFrame=lambda: Frame())

    def getPropertyValue(self, name):
        return self.RuntimeUID

    def supportsService(self, service):
        return service == "com.sun.star.text.TextDocument"

    def getText(self):
        return self.text

    def getUndoManager(self):
        return self.undo_manager

    def getCurrentController(self):
        return self.controller


class WriterCase(unittest.TestCase):
    def setUp(self):
        self.runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)
        self.runtime.async_callback = ImmediateCallback()
        self.runtime.logger = logging.getLogger("writer-paragraph-test")
        self.runtime.session_nonce = "0badc0de"
        self.runtime._documents = {}
        self.runtime.focus = BRIDGE.FocusState()
        self.desktop = types.SimpleNamespace(current=None)
        self.desktop.getCurrentComponent = lambda: self.desktop.current
        self.runtime.desktop = self.desktop
        self.doc = Writer("w", ["Hermes Writer original text."], self.desktop, self.runtime.focus)
        self.desktop.current = self.doc
        self.runtime.focus.on_event("OnFocus", self.doc)
        self.runtime._enumerate_documents = lambda: [self.doc]
        self.doc_id = self.runtime._id_for_document(self.doc)

    def insert(self, text, position="end", **extra):
        return self.runtime.writer_insert_text({"document_id": self.doc_id, "text": text, "position": position, **extra})

    def read(self):
        return self.runtime.writer_get_text({"document_id": self.doc_id})["paragraphs"]


class WriterParagraphTests(WriterCase):
    def test_append_as_paragraph_adds_its_own_paragraph_in_one_undo_context(self):
        result = self.insert("Q4 summary.", as_paragraph=True)
        self.assertTrue(result["as_paragraph"])
        self.assertEqual(self.read(), ["Hermes Writer original text.", "Q4 summary."])
        self.assertEqual(self.doc.calls, [("control", 0, True), ("string", "Q4 summary.", True)])
        self.assertEqual(len(self.doc.undo_manager.stack), 1)

    def test_undo_of_paragraph_append_leaves_no_empty_paragraph(self):
        self.insert("Q4 summary.", as_paragraph=True)
        undone = self.runtime.undo({"document_id": self.doc_id, "hermes_only": True})
        self.assertEqual(undone["title"], "Hermes: insert Writer paragraph at end")
        self.assertEqual(self.read(), ["Hermes Writer original text."])

    def test_prepend_as_paragraph_puts_the_break_after_the_text(self):
        self.insert("Heading line", position="start", as_paragraph=True)
        self.assertEqual(self.read(), ["Heading line", "Hermes Writer original text."])
        self.assertEqual([call[0] for call in self.doc.calls], ["string", "control"])

    def test_paragraph_mode_strips_edge_newlines_and_splits_inner_lines(self):
        self.insert("\nFirst\nSecond\n", as_paragraph=True)
        self.assertEqual(self.read(), ["Hermes Writer original text.", "First", "Second"])
        self.assertNotIn("", self.read())

    def test_empty_document_gets_no_leading_empty_paragraph(self):
        self.doc.paragraphs = [""]
        self.insert("Only line", as_paragraph=True)
        self.assertEqual(self.read(), ["Only line"])

    def test_append_reuses_an_already_empty_last_paragraph(self):
        self.doc.paragraphs = ["Hermes Writer original text.", ""]
        self.insert("Q4 summary.", as_paragraph=True)
        self.assertEqual(self.read(), ["Hermes Writer original text.", "Q4 summary."])
        self.assertEqual(self.doc.calls, [("string", "Q4 summary.", True)])
        self.runtime.undo({"document_id": self.doc_id, "hermes_only": True})
        self.assertEqual(self.read(), ["Hermes Writer original text.", ""])

    def test_prepend_reuses_an_already_empty_first_paragraph(self):
        self.doc.paragraphs = ["", "Hermes Writer original text."]
        self.insert("Heading line", position="start", as_paragraph=True)
        self.assertEqual(self.read(), ["Heading line", "Hermes Writer original text."])
        self.runtime.undo({"document_id": self.doc_id, "hermes_only": True})
        self.assertEqual(self.read(), ["", "Hermes Writer original text."])

    def test_paragraph_mode_rejects_text_that_is_only_newlines(self):
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            self.insert("\n\n", as_paragraph=True)
        self.assertEqual(caught.exception.category, "Validation")
        self.assertEqual(self.doc.calls, [])

    def test_as_paragraph_must_be_boolean(self):
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            self.insert("x", as_paragraph="yes")
        self.assertEqual(caught.exception.category, "Validation")

    def test_default_insert_is_unchanged(self):
        result = self.insert(" tail")
        self.assertFalse(result["as_paragraph"])
        self.assertEqual(self.read(), ["Hermes Writer original text. tail"])
        self.assertEqual(self.doc.undo_manager.contexts, ["Hermes: insert Writer text at end"])


class HermesOnlyUndoTests(WriterCase):
    def test_hermes_only_refuses_a_user_step_and_undoes_nothing(self):
        self.insert("Q4 summary.", as_paragraph=True)
        self.doc.undo_manager.push_user_step("Typing: abc")
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            self.runtime.undo({"document_id": self.doc_id, "hermes_only": True})
        self.assertEqual(caught.exception.category, "Validation")
        self.assertEqual(caught.exception.state, "not_started")
        self.assertEqual(len(self.doc.undo_manager.stack), 2)

    def test_without_hermes_only_the_bridge_keeps_its_previous_behaviour(self):
        self.doc.undo_manager.push_user_step("Typing: abc")
        self.assertEqual(self.runtime.undo({"document_id": self.doc_id})["title"], "Typing: abc")

    def test_undo_rejects_unknown_arguments(self):
        self.insert("x", as_paragraph=True)
        for args in ({"document_id": self.doc_id, "steps": 2}, {"document_id": self.doc_id, "hermes_only": "true"}):
            with self.assertRaises(BRIDGE.BridgeFault) as caught:
                self.runtime.undo(args)
            self.assertEqual(caught.exception.category, "Validation")
        self.assertEqual(len(self.doc.undo_manager.stack), 1)


if __name__ == "__main__":
    unittest.main()
