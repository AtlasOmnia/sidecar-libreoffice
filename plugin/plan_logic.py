# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Pure validation for the bounded Calc WorkbookPlan V1 subset."""
from __future__ import annotations

from typing import Any

from .range_logic import RangeValidationError, parse_a1, validate_matrix

FORMAT_KEYS = {
    "bold", "italic", "fontColor", "fillColor", "numberFormat", "borders",
    "horizontalAlignment", "verticalAlignment", "fontName", "fontSize",
    "underline", "wrapText", "columnAutofit",
}


# WorkbookPlan V1 text limits (mirrors the Excel contract).
TEXT_LIMITS = {"reason": 1024, "summary": 2048, "message": 2048}
# Kinds this LibreOffice subset accepts. All exist in WorkbookPlan V1 except
# clear_range, a documented LibreOffice extension.
SUPPORTED_READ_KINDS = ("read_range", "summarize_range_stats")
SUPPORTED_OPERATION_KINDS = ("write_values", "write_formula", "format_range", "create_sheet", "clear_range")
SUPPORTED_VERIFICATION_KINDS = (
    "range_values_equal", "range_formulas_equal", "range_number_format_equal", "sheet_exists", "sentinel_unchanged",
)
LIBREOFFICE_EXTENSION_KINDS = ("clear_range",)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def validate_document_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise RangeValidationError("document_id must be a non-empty bounded string")
    return value


def validate_sheet(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise RangeValidationError("sheet must be a non-empty bounded string")
    return value


def validate_format(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict) or not spec:
        raise RangeValidationError("format must be a non-empty object")
    unknown = sorted(set(spec) - FORMAT_KEYS)
    if unknown:
        raise RangeValidationError(f"unsupported format keys: {', '.join(unknown)}")
    for key in ("bold", "italic", "borders", "wrapText", "columnAutofit"):
        if key in spec and not isinstance(spec[key], bool):
            raise RangeValidationError(f"{key} must be boolean")
    for key in ("fontColor", "fillColor"):
        value = spec.get(key)
        if value is not None and (
            not isinstance(value, str) or len(value) != 7 or not value.startswith("#")
            or any(char not in "0123456789abcdefABCDEF" for char in value[1:])
        ):
            raise RangeValidationError("colors must use #RRGGBB")
    for key in ("numberFormat", "fontName"):
        if key in spec and (not isinstance(spec[key], str) or not spec[key]):
            raise RangeValidationError(f"{key} must be a non-empty string")
    if "fontSize" in spec and (
        not isinstance(spec["fontSize"], (int, float)) or isinstance(spec["fontSize"], bool)
        or not 0 < spec["fontSize"] <= 409
    ):
        raise RangeValidationError("fontSize must be between 0 and 409")
    if spec.get("horizontalAlignment") not in (None, "General", "Left", "Center", "Right", "Fill", "Justify"):
        raise RangeValidationError("unsupported horizontalAlignment")
    if spec.get("verticalAlignment") not in (None, "Top", "Center", "Bottom", "Standard"):
        raise RangeValidationError("unsupported verticalAlignment")
    if spec.get("underline") not in (None, "None", "Single", "Double"):
        raise RangeValidationError("unsupported underline")
    return spec


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise RangeValidationError("plan version must be 1")
    outcome = plan.get("outcome")
    required = {
        "inspect": {"version", "outcome", "reason", "reads"},
        "execute": {"version", "outcome", "summary", "operations", "verification"},
        "ask": {"version", "outcome", "message"},
        "refuse": {"version", "outcome", "message"},
    }
    if outcome not in required or set(plan) != required[outcome]:
        raise RangeValidationError("plan fields do not match a supported outcome")
    text_key = "reason" if outcome == "inspect" else "summary" if outcome == "execute" else "message"
    if not isinstance(plan.get(text_key), str) or not plan[text_key]:
        raise RangeValidationError(f"plan {text_key} is required")
    if len(plan[text_key]) > TEXT_LIMITS[text_key]:
        raise RangeValidationError(f"plan {text_key} exceeds {TEXT_LIMITS[text_key]} characters")
    if outcome == "inspect":
        reads = plan["reads"]
        if not isinstance(reads, list) or not 1 <= len(reads) <= 50:
            raise RangeValidationError("inspect plan needs 1-50 reads")
        for read in reads:
            if not isinstance(read, dict) or set(read) != {"kind", "sheet", "address"} or read.get("kind") not in ("read_range", "summarize_range_stats"):
                raise RangeValidationError("inspection operation is invalid")
            validate_sheet(read["sheet"])
            parse_a1(read["address"])
    elif outcome == "execute":
        operations, verification = plan["operations"], plan["verification"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 30:
            raise RangeValidationError("execute plan needs 1-30 operations")
        if not isinstance(verification, list) or not 1 <= len(verification) <= 30:
            raise RangeValidationError("execute plan needs 1-30 verification rules")
        fields = {
            "write_values": {"kind", "sheet", "address", "values"},
            "write_formula": {"kind", "sheet", "address", "formula"},
            "format_range": {"kind", "sheet", "address", "format"},
            "clear_range": {"kind", "sheet", "address"},
            "create_sheet": {"kind", "name"},
        }
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("kind") not in fields or set(operation) != fields[operation["kind"]]:
                raise RangeValidationError("workbook operation is unsupported or malformed")
            kind = operation["kind"]
            if kind == "create_sheet":
                validate_sheet(operation["name"])
                continue
            validate_sheet(operation["sheet"])
            if kind == "write_values":
                validate_matrix(operation["address"], operation["values"])
            else:
                bounds = parse_a1(operation["address"])
                if kind == "write_formula" and (
                    bounds[0] != bounds[2] or bounds[1] != bounds[3]
                    or not isinstance(operation["formula"], str) or not operation["formula"].startswith("=")
                ):
                    raise RangeValidationError("write_formula requires one cell and an = formula")
                if kind == "format_range":
                    validate_format(operation["format"])
        expected = {
            "range_values_equal": "expectedValues",
            "range_formulas_equal": "expectedFormulas",
            "sheet_exists": "name",
            "sentinel_unchanged": "expected",
            "range_number_format_equal": "expectedNumberFormat",
        }
        for rule in verification:
            if not isinstance(rule, dict) or rule.get("kind") not in expected:
                raise RangeValidationError("verification rule is invalid")
            kind = rule["kind"]
            if kind == "sheet_exists":
                if set(rule) != {"kind", "name"}:
                    raise RangeValidationError("sheet_exists verification is invalid")
                validate_sheet(rule["name"])
            else:
                key = expected[kind]
                if set(rule) != {"kind", "sheet", "address", key}:
                    raise RangeValidationError(f"{kind} verification is invalid")
                validate_sheet(rule["sheet"])
                bounds = parse_a1(rule["address"])
                if kind == "sentinel_unchanged" and (
                    bounds[0] != bounds[2] or bounds[1] != bounds[3] or not _is_scalar(rule["expected"])
                ):
                    raise RangeValidationError("sentinel_unchanged needs one cell and a scalar expected value")
    return plan
