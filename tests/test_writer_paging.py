# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Paged Writer reads: every block exactly once, table cells included, stale continuations refused."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402  (module import: its TestCase is not re-collected here)

Paragraph, Writer = FAKES.Paragraph, FAKES.Writer


class Harness(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

UNICODE = "Ünïcödé — Zürich, 東京, naïve café ✓"


class Table:
    def __init__(self, name, rows):
        self.Name, self.rows = name, rows

    def supportsService(self, service):
        return service == "com.sun.star.text.TextTable"

    def getCellNames(self):
        return tuple(f"{chr(65 + c)}{r + 1}" for r, row in enumerate(self.rows) for c in range(len(row)))

    def getCellByName(self, name):
        col, row = ord(name[0]) - 65, int(name[1:]) - 1
        text = self.rows[row][col]
        return type("Cell", (), {"getString": lambda _s: text})()

    def getRows(self):
        return type("Rows", (), {"getCount": lambda _s: len(self.rows)})()

    def getColumns(self):
        return type("Cols", (), {"getCount": lambda _s: len(self.rows[0])})()


def long_document():
    doc = Writer([f"Paragraph {n:03d}. " + (UNICODE if n % 50 == 0 else "filler") for n in range(1, 601)])
    doc.paragraphs.insert(300, Table("KeyFigures", [["Metric", "Value"], ["Widgets", "Key figure: 4,321 widgets"]]))
    return doc


class PagingTests(Harness):
    def read_all(self, runtime, op, **first):
        pages, args = [], {"document_id": self.document_id, **first}
        while True:
            page = runtime.invoke(op, args)
            pages.append(page)
            if page["next_offset"] is None:
                return pages
            args = {"document_id": self.document_id, "continuation": page["continuation"], **{k: v for k, v in first.items() if k == "limit"}}

    def test_a_600_paragraph_document_with_a_table_reads_to_the_end_without_omission_or_repeat(self):
        doc = long_document()
        runtime = self.runtime(doc)
        pages = self.read_all(runtime, "writer_get_text")
        self.assertEqual(len(pages), 2)
        self.assertFalse(pages[0]["complete_document"])
        texts = [t for page in pages for t in page["paragraphs"]]
        expected = [p.String if isinstance(p, Paragraph) else "" for p in doc.paragraphs]
        self.assertEqual(texts, expected)
        tables = [t for page in pages for t in page["tables"]]
        self.assertEqual(len(tables), 1)
        self.assertEqual((tables[0]["block"], tables[0]["name"], tables[0]["rows"], tables[0]["columns"]), (301, "KeyFigures", 2, 2))
        self.assertIn({"cell": "B2", "text": "Key figure: 4,321 widgets"}, tables[0]["cells"])
        self.assertEqual(sum(p["returned"] for p in pages), 601)
        self.assertEqual(texts.count(f"Paragraph 050. {UNICODE}"), 1)

    def test_small_pages_add_up_exactly_and_structure_pages_too(self):
        runtime = self.runtime(long_document())
        pages = self.read_all(runtime, "writer_get_text", limit=97)
        self.assertEqual([p["start"] for p in pages], [1 + 97 * i for i in range(7)])
        self.assertEqual(sum(p["returned"] for p in pages), 601)
        structure = self.read_all(runtime, "writer_get_structure", limit=250)
        items = [i for page in structure for i in page["paragraphs"]]
        self.assertEqual([i["paragraph"] for i in items], list(range(1, 602)))
        self.assertEqual(items[300]["type"], "table")
        self.assertEqual(items[300]["cell_count"], 4)

    def test_an_edit_between_pages_makes_the_continuation_stale(self):
        for change in ("insert", "delete", "edit", "table"):
            doc = long_document()
            runtime = self.runtime(doc)
            first = runtime.invoke("writer_get_text", {"document_id": self.document_id, "limit": 100})
            if change == "insert":
                doc.paragraphs.insert(10, Paragraph("new"))
            elif change == "delete":
                del doc.paragraphs[150]
            elif change == "edit":
                doc.paragraphs[400].String += "!"
            else:
                doc.paragraphs[300].rows[1][1] = "Key figure: 9,999 widgets"
            fault = self.fault(runtime, "writer_get_text", continuation=first["continuation"])
            self.assertEqual(fault.category, "Stale", change)
        # unchanged: the same continuation is still accepted
        doc = long_document()
        runtime = self.runtime(doc)
        first = runtime.invoke("writer_get_text", {"document_id": self.document_id, "limit": 100})
        self.assertEqual(runtime.invoke("writer_get_text", {"document_id": self.document_id, "continuation": first["continuation"]})["start"], 101)

    def test_default_call_keeps_the_old_shape_and_reports_completeness(self):
        doc = Writer(["One", "Two"])
        runtime = self.runtime(doc)
        data = runtime.invoke("writer_get_text", {"document_id": self.document_id})
        self.assertEqual((data["paragraphs"], data["text"], data["truncated"]), (["One", "Two"], "One\nTwo", False))
        self.assertTrue(data["complete_document"])
        self.assertIsNone(data["continuation"])

    def test_tables_only_empty_and_huge_paragraph_documents(self):
        only = Writer([])
        only.paragraphs = [Table("OnlyTable", [["A1 text", "B1 text"], ["A2 ✓", "B2"]])]
        runtime = self.runtime(only)
        data = runtime.invoke("writer_get_text", {"document_id": self.document_id})
        self.assertEqual((data["paragraphs"], data["complete_document"]), ([""], True))
        self.assertEqual([c["text"] for c in data["tables"][0]["cells"]], ["A1 text", "B1 text", "A2 ✓", "B2"])
        table = runtime.invoke("writer_get_table", {"document_id": self.document_id, "table": "OnlyTable", "cell_offset": 3})
        self.assertEqual(([c["cell"] for c in table["table"]["cells"]], table["table"]["next_cell_offset"]), (["A2", "B2"], None))
        self.assertEqual(self.fault(runtime, "writer_get_table", table="Missing").category, "NotFound")
        self.assertEqual(self.fault(runtime, "writer_get_table", table="OnlyTable", block=1).category, "Validation")
        empty = Writer([""])
        runtime = self.runtime(empty)
        data = runtime.invoke("writer_get_text", {"document_id": self.document_id})
        self.assertEqual((data["paragraphs"], data["complete_document"], data["next_offset"]), ([""], True, None))
        huge = Writer(["x" * 60000, "after"])
        runtime = self.runtime(huge)
        data = runtime.invoke("writer_get_text", {"document_id": self.document_id})
        self.assertEqual((len(data["paragraphs"][0]), data["clipped_blocks"], data["next_offset"]), (50000, [1], 2))
        self.assertTrue(data["truncated"])
        self.assertFalse(data["complete_document"])
        rest = runtime.invoke("writer_get_text", {"document_id": self.document_id, "continuation": data["continuation"]})
        self.assertEqual(rest["paragraphs"], ["after"])

    def test_paging_arguments_are_validated_before_dispatch(self):
        runtime = self.runtime(Writer(["a"]))
        for bad in ({"limit": 0}, {"limit": 501}, {"offset": 0}, {"continuation": "nope"},
                    {"continuation": "2." + "0" * 32, "offset": 2}, {"offset": True}):
            self.assertEqual(self.fault(runtime, "writer_get_text", **bad).category, "Validation", bad)
        self.assertEqual(self.fault(runtime, "writer_get_text", offset=5).category, "NotFound")
        self.assertEqual(self.fault(runtime, "writer_get_text", continuation="1." + "0" * 32).category, "Stale")


if __name__ == "__main__":
    unittest.main()
