# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Native findings in calc_format_range (hermes_bridge._apply_format), measured on LibreOffice 26.8.

1. `italic` always fails: every `{"italic": ...}` returned ExecutionUncertain (`state: started`)
   with `pyuno.getConstantByName: com.sun.star.awt.FontSlant.ITALICis not a constant`
   (format round-trip check, format_italic). FontSlant is a UNO enum and needs uno.Enum,
   as the bridge's other enums already use. The shared fake `uno` hid this: its getConstantByName
   accepts any name.
2. `numberFormat` codes are parsed in the office locale: with de-DE, "0.00" became "000" and showed
   1234.5 as "1.235", and "#,##0.00" became "#,##000" (decimal-comma locale check). The code
   is passed with an empty Locale. Written in English notation and converted with
   addNewConverted(code, en-US, document locale), the same codes show "1234,50" and "1.234,50"
   (checked in-process in the same run).

Both are fixed in hermes_bridge.py; natively, addNewConverted returned the same key when a code was
applied twice.
"""
from __future__ import annotations

import types
import unittest

from test_extension_bridge import BRIDGE


def strict_constants(name: str) -> int:
    # Real pyuno: enum values are not constants.
    if name.startswith("com.sun.star.awt.FontSlant."):
        raise RuntimeError(f"pyuno.getConstantByName: {name}is not a constant")
    return 1


class Formats:
    """Records how a number format code is registered."""

    def __init__(self):
        self.calls = []

    def queryKey(self, code, locale, _scan):
        self.calls.append(("queryKey", code, vars(locale)))
        return -1

    def addNew(self, code, locale):
        self.calls.append(("addNew", code, vars(locale)))
        return 7

    def addNewConverted(self, code, source, target):
        self.calls.append(("addNewConverted", code, vars(source), target))
        return 9


class CalcFormatFindingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)

    def test_italic_sets_the_font_slant_enum(self):
        old = BRIDGE.uno.getConstantByName
        BRIDGE.uno.getConstantByName = strict_constants
        try:
            for value, expected in ((True, "ITALIC"), (False, "NONE")):
                cells = types.SimpleNamespace()
                self.runtime._apply_format(None, cells, {"italic": value})
                self.assertEqual(cells.CharPosture, ("com.sun.star.awt.FontSlant", expected))
        finally:
            BRIDGE.uno.getConstantByName = old

    def test_number_format_codes_are_english_and_converted_to_the_document_locale(self):
        formats = Formats()
        german = types.SimpleNamespace(Language="de", Country="DE", Variant="")
        doc = types.SimpleNamespace(getNumberFormats=lambda: formats, CharLocale=german)
        cells = types.SimpleNamespace()
        self.runtime._apply_format(doc, cells, {"numberFormat": "#,##0.00"})
        self.assertEqual(cells.NumberFormat, 9)
        kind, code, source, target = formats.calls[-1]
        self.assertEqual((kind, code, source["Language"], source["Country"], target),
                         ("addNewConverted", "#,##0.00", "en", "US", german))


if __name__ == "__main__":
    unittest.main()
