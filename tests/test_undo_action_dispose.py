# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Bridge undo steps let go of document objects when LibreOffice disposes them.

Found natively in the format round-trip check: closing a deck after
impress_add_text_box crashed LibreOffice 26.8 every time. pyuno's GC thread released the step's
closures after the document model was gone, and the text shape they held aborted in
~SdrObject. LibreOffice's undo manager disposes a UNO undo action it drops, so the step must
implement XComponent and release its callables (and with them every UNO reference) in dispose().
"""
from __future__ import annotations

import gc
import sys
import unittest
import weakref

import test_office_ops  # noqa: F401  (loads office_ops with the fake uno modules)

OPS = sys.modules["office_ops"]


class Held:
    """Stands in for a UNO shape proxy captured by a step's closures."""


class UndoActionDisposeTests(unittest.TestCase):
    def test_dispose_releases_everything_the_step_holds(self):
        def make():  # the closures capture the shape the way impress_add_text_box's do
            made = {"shape": Held()}
            step = OPS.HermesUndoAction("Hermes: add text box on slide 1", lambda: made["shape"], lambda: made["shape"])
            return step, weakref.ref(made["shape"])
        action, ref = make()
        gc.collect()
        self.assertIsNotNone(ref(), "the step keeps its target while it can still be undone")
        action.dispose()
        gc.collect()
        self.assertIsNone(ref(), "dispose must drop the callables and the objects they capture")
        # The listener half of XComponent is accepted and ignored.
        action.addEventListener(object())
        action.removeEventListener(object())

    def test_steps_are_still_undoable_before_dispose(self):
        calls = []
        action = OPS.HermesUndoAction("Hermes: t", lambda: calls.append("undo"), lambda: calls.append("redo"))
        action.undo()
        action.redo()
        self.assertEqual(calls, ["undo", "redo"])
        self.assertEqual(action.Title, "Hermes: t")


if __name__ == "__main__":
    unittest.main()
