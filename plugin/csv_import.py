# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Parse bounded CSV text into a literal Calc matrix (stdlib only).

Typing policy, explicit and locale-independent:

- ``text`` (default): every non-empty field is written as text, exactly as given.
- ``numbers``: a field that is a plain decimal number written with a dot (``-12``, ``3.50``,
  ``1e3``) becomes a number; everything else stays text. Leading zeros (``007``), more than 15
  significant digits, thousands separators, decimal commas, percentages, currency, dates and
  booleans stay text, so identifiers and ambiguous values are never silently converted.

Empty fields become empty cells. Formula-like fields (``=``, ``+``, ``-``, ``@``) are data:
the bridge writes strings with ``setString``, which never evaluates them.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from .range_logic import RangeValidationError, parse_a1

MAX_CSV_CHARS = 1_000_000
MAX_IMPORT_CELLS = 10000
MAX_FIELD_CHARS = 32767  # Calc's cell text limit
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?")
DELIMITERS = {",": ",", ";": ";", "tab": "\t", "|": "|"}


def _number(field: str) -> float | None:
    if not NUMBER.fullmatch(field):
        return None
    mantissa = re.split(r"[eE]", field)[0].lstrip("-").replace(".", "").lstrip("0")
    if len(mantissa) > 15:
        return None
    value = float(field)
    return value if value == value and abs(value) < 1e300 else None


def parse_csv(text: Any, delimiter: Any = ",", typing: Any = "text") -> list[list[Any]]:
    if not isinstance(text, str) or not text.strip():
        raise RangeValidationError("csv_text must be non-empty text")
    if len(text) > MAX_CSV_CHARS:
        raise RangeValidationError(f"csv_text is limited to {MAX_CSV_CHARS} characters")
    if "\x00" in text:
        raise RangeValidationError("csv_text contains a NUL character")
    if delimiter not in DELIMITERS or typing not in ("text", "numbers"):
        raise RangeValidationError("delimiter must be , ; tab or |, and typing text or numbers")
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), delimiter=DELIMITERS[delimiter], strict=True)
    rows: list[list[str]] = []
    cells = 0
    try:
        for row in reader:
            cells += len(row)
            if cells > MAX_IMPORT_CELLS:
                raise RangeValidationError(f"the CSV has more than {MAX_IMPORT_CELLS} cells")
            rows.append(row)
    except csv.Error as exc:
        raise RangeValidationError(f"malformed CSV on line {reader.line_num}: {exc}") from exc
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        raise RangeValidationError("the CSV holds no rows")
    width = max(len(row) for row in rows)
    if width == 0 or len(rows) * width > MAX_IMPORT_CELLS:
        raise RangeValidationError(f"the CSV is limited to {MAX_IMPORT_CELLS} cells")
    matrix: list[list[Any]] = []
    for row in rows:
        out: list[Any] = []
        for field in row + [""] * (width - len(row)):
            if len(field) > MAX_FIELD_CHARS:
                raise RangeValidationError(f"a CSV field exceeds {MAX_FIELD_CHARS} characters")
            if field == "":
                out.append(None)
            elif typing == "numbers" and (number := _number(field)) is not None:
                out.append(number)
            else:
                out.append(field)
        matrix.append(out)
    return matrix


def column_label(index: int) -> str:
    label = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        label = chr(65 + rest) + label
    return label


def target_address(start_cell: Any, rows: int, cols: int) -> str:
    if not isinstance(start_cell, str):
        raise RangeValidationError("start_cell must be a single cell such as A1")
    col, row, end_col, end_row = parse_a1(start_cell)
    if (col, row) != (end_col, end_row):
        raise RangeValidationError("start_cell must be a single cell such as A1")
    last_col, last_row = col + cols - 1, row + rows - 1
    if last_col > 16383 or last_row > 1048575:
        raise RangeValidationError("the CSV does not fit on the sheet from that cell")
    return f"{column_label(col)}{row + 1}:{column_label(last_col)}{last_row + 1}"
