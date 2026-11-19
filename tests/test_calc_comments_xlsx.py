# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""calc_get_comments returns the text of comments in a workbook just loaded from .xlsx.

Found natively in the format round-trip check: after an
.xlsx was opened, every comment's getString() was "" although the file and LibreOffice held the
text; the text appears once LibreOffice builds the comment's caption (getAnnotationShape). The
empty readback could lead to calc_add_comment replacing a comment the user still has.
"""
from __future__ import annotations

import types
import unittest

import test_office_ops as FAKES


class Annotation:
    def __init__(self, column, row, text, caption_text):
        self._text, self._caption = text, caption_text
        self.position = types.SimpleNamespace(Column=column, Row=row)

    def getPosition(self):
        return self.position

    def getString(self):
        return self._text

    def getAnnotationShape(self):
        return types.SimpleNamespace(getString=lambda: self._caption)


class Annotations:
    def __init__(self, items):
        self.items = items

    def getCount(self):
        return len(self.items)

    def getByIndex(self, index):
        return self.items[index]


class CommentSheets(FAKES.Sheets):
    def __init__(self, annotations):
        super().__init__(["T"])
        self.annotations = annotations

    def getByName(self, name):
        return types.SimpleNamespace(getAnnotations=lambda: self.annotations)


class XlsxCommentReadbackTests(FAKES.OfficeOpsTests):
    def test_comment_text_comes_from_the_caption_when_the_cell_reports_none(self):
        doc = FAKES.Calc(("T",))
        doc.sheets = CommentSheets(Annotations([Annotation(7, 1, "", "SUMIFS over Inputs ✓"),
                                                Annotation(0, 0, "Typed here", "Typed here"),
                                                Annotation(1, 2, "", "")]))
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_get_comments", {"document_id": self.document_id, "sheet": "T"})
        self.assertEqual(data["comments"], [{"cell": "H2", "text": "SUMIFS over Inputs ✓"},
                                            {"cell": "A1", "text": "Typed here"},
                                            {"cell": "B3", "text": ""}])


# Run only the new test from this module, not the inherited OfficeOpsTests again.
def load_tests(loader, _tests, _pattern):
    return loader.loadTestsFromNames(
        ["test_calc_comments_xlsx.XlsxCommentReadbackTests.test_comment_text_comes_from_the_caption_when_the_cell_reports_none"])


if __name__ == "__main__":
    unittest.main()
