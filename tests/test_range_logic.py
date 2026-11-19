# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import unittest

from plugin.range_logic import RangeValidationError, parse_a1, validate_matrix


class RangeLogicTests(unittest.TestCase):
    def test_parses_single_and_absolute_range(self):
        self.assertEqual(parse_a1("A1"), (0, 0, 0, 0))
        self.assertEqual(parse_a1("$B$2:$D$4"), (1, 1, 3, 3))
        self.assertEqual(parse_a1("AA10:AB11"), (26, 9, 27, 10))

    def test_rejects_invalid_or_reversed_addresses(self):
        for address in ("", "A0", "1A", "B2:A1", "A1:B", "A1:C4000"):
            with self.subTest(address=address), self.assertRaises(RangeValidationError):
                parse_a1(address)

    def test_matrix_shape_must_match(self):
        self.assertEqual(validate_matrix("A1:B2", [[1, 2], [3, 4]]), (0, 0, 1, 1))
        for values in ([[1, 2]], [[1], [2]], [1, 2], [[1, 2], [3, float("nan")]]):
            with self.subTest(values=values), self.assertRaises(RangeValidationError):
                validate_matrix("A1:B2", values)


if __name__ == "__main__":
    unittest.main()
