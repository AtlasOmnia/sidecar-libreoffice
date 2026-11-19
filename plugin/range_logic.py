# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Pure A1 parsing and matrix-shape validation for Calc tools."""
from __future__ import annotations

import math
import re
from typing import Any

_A1 = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]*))?$")
MAX_CELLS = 10000


class RangeValidationError(ValueError):
    pass


def column_index(label: str) -> int:
    value = 0
    for char in label.upper():
        value = value * 26 + ord(char) - 64
    return value - 1


def parse_a1(address: Any) -> tuple[int, int, int, int]:
    if not isinstance(address, str) or len(address) > 64:
        raise RangeValidationError("address must be a bounded A1 range")
    match = _A1.fullmatch(address.strip())
    if not match:
        raise RangeValidationError("address must be an A1 cell or range")
    c1, r1, c2, r2 = match.groups()
    start_col, start_row = column_index(c1), int(r1) - 1
    end_col, end_row = column_index(c2 or c1), int(r2 or r1) - 1
    if end_col < start_col or end_row < start_row:
        raise RangeValidationError("range endpoints must be top-left to bottom-right")
    rows, cols = end_row - start_row + 1, end_col - start_col + 1
    if rows * cols > MAX_CELLS:
        raise RangeValidationError("range exceeds the 10000-cell limit")
    return start_col, start_row, end_col, end_row


def validate_matrix(address: Any, values: Any) -> tuple[int, int, int, int]:
    bounds = parse_a1(address)
    rows, cols = bounds[3] - bounds[1] + 1, bounds[2] - bounds[0] + 1
    if not isinstance(values, list) or len(values) != rows:
        raise RangeValidationError("values row count does not match address")
    for row in values:
        if not isinstance(row, list) or len(row) != cols:
            raise RangeValidationError("values column count does not match address")
        for item in row:
            if item is not None and not isinstance(item, (str, int, float, bool)):
                raise RangeValidationError("cell values must be JSON scalars")
            if isinstance(item, float) and not math.isfinite(item):
                raise RangeValidationError("cell numbers must be finite")
    return bounds
