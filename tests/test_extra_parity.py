# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Tables, active sheet, recalculation and Impress current-slide/layout reads (fakes; native checks separate)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_office_ops as FAKES  # noqa: E402


class Controllers:
    def __init__(self, count):
        self.left = count

    def hasMoreElements(self):
        return self.left > 0

    def nextElement(self):
        self.left -= 1


class ExtraTests(unittest.TestCase):
    runtime = FAKES.OfficeOpsTests.runtime
    fault = FAKES.OfficeOpsTests.fault

    def test_table_arguments_are_checked_before_the_document_is_touched(self):
        runtime = self.runtime(FAKES.Calc(("Data",)))
        for extra in ({"address": "A1:C1"}, {"name": "1bad"}, {"name": "has space"}, {"style": "Neon"},
                      {"filter_buttons": "yes"}, {"if_match": "abc"}):
            args = {"sheet": "Data", "address": "A1:C4", **extra}
            self.assertEqual(self.fault(runtime, "calc_insert_table", **args).category, "Validation", extra)

    def test_view_operations_need_exactly_one_window(self):
        doc = FAKES.Calc(("Data",))
        doc.getControllers = lambda: Controllers(2)
        runtime = self.runtime(doc)
        self.assertEqual(self.fault(runtime, "calc_activate_sheet", sheet="Data").category, "Validation")
        self.assertEqual(self.fault(runtime, "calc_freeze_panes", sheet="Data", columns=1, rows=1).category, "Validation")

    def test_recalculate_reports_mode_and_is_not_claimed_undoable(self):
        doc = FAKES.Calc(("Data",))
        calls = []
        doc.calculateAll = lambda: calls.append("all")
        doc.calculate = lambda: calls.append("dirty")
        doc.isAutomaticCalculationEnabled = lambda: False
        runtime = self.runtime(doc)
        data = runtime.invoke("calc_recalculate", {"document_id": self.document_id, "all": False})
        self.assertEqual((calls, data["automatic_calculation"], data["undoable"]), (["dirty"], False, False))
        self.assertEqual(self.fault(runtime, "calc_recalculate", all="yes").category, "Validation")


if __name__ == "__main__":
    unittest.main()
