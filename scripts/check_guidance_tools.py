# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Audit the bundled guidance against what the plugin really offers.

Every backticked snake_case name in the skill files must be one of:
- a registered tool (``plugin/tool_schemas.json``);
- a documented argument: a parameter name at any depth of a tool schema, or one of its enum values;
- a known WorkbookPlan operation, read or verification kind, or plan field (``plugin/plan_logic.py``);
- a documented result field: listed in RESULT_FIELDS below and returned by the bridge source.

A name that starts like a tool (``calc_``, ``writer_``, ``impress_``, ``libreoffice_``) but is not
registered always fails, even if the bridge has such an operation: internal and development
operations are not tools. The report also lists registered tools the guidance never mentions.

Usage: python3 scripts/check_guidance_tools.py [--json]   (exit 1 when an unknown name is found)
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugin" / "skills"
SCHEMAS = ROOT / "plugin" / "tool_schemas.json"
PLAN_LOGIC = ROOT / "plugin" / "plan_logic.py"
BRIDGE_SOURCES = (ROOT / "extension" / "python" / "office_ops.py", ROOT / "extension" / "python" / "hermes_bridge.py")
TOOL_PREFIXES = ("calc_", "writer_", "impress_", "libreoffice_")

# Result fields the guidance may name. Each must also appear as a quoted key in the bridge source,
# so a renamed or removed field fails the audit instead of lingering in the guidance.
RESULT_FIELDS = frozenset({
    "automatic_calculation", "body_set", "clipped_blocks", "complete_document", "hidden_rows",
    "next_offset", "number_formats", "previous_text", "text_preserved", "title_set", "visible_rows", "view_change_only",
    "values_unchanged",
})

SPAN = re.compile(r"`([^`\n]+)`")
NAME = re.compile(r"(?<![A-Za-z0-9_$.])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![A-Za-z0-9_])")


def load_tools(path: Path = SCHEMAS) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk_schema(spec: Any, names: set[str]) -> None:
    if isinstance(spec, dict):
        for key, value in spec.get("properties", {}).items():
            names.add(key)
            _walk_schema(value, names)
        for value in spec.get("enum", []):
            if isinstance(value, str):
                names.add(value)
        if "items" in spec:
            _walk_schema(spec["items"], names)


def documented_arguments(tools: Iterable[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        _walk_schema(tool.get("parameters", {}), names)
    return names


def plan_vocabulary(path: Path = PLAN_LOGIC) -> set[str]:
    """Kinds and field names quoted in the plan validator (operation, read and verification kinds)."""
    return set(re.findall(r'"([a-z][a-z0-9]*(?:_[a-z0-9]+)+)"', path.read_text(encoding="utf-8")))


def result_fields(sources: Iterable[Path] = BRIDGE_SOURCES) -> set[str]:
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources if p.is_file())
    return {name for name in RESULT_FIELDS if f'"{name}"' in text}


def guidance_files(root: Path = SKILLS) -> list[Path]:
    return sorted(root.rglob("*.md"))


def mentioned_names(text: str) -> list[str]:
    """Backticked snake_case names, in order, including those inside fenced code blocks."""
    names: list[str] = []
    fenced = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)
    inline = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.S)
    for chunk in [*SPAN.findall(inline), *fenced]:
        names += NAME.findall(chunk)
    return names


def audit(files: Iterable[Path], tools: list[dict[str, Any]], plan_names: set[str], results: set[str]) -> dict[str, Any]:
    registered = {tool["name"] for tool in tools}
    arguments = documented_arguments(tools)
    unknown: list[dict[str, str]] = []
    mentioned: set[str] = set()
    for path in files:
        for name in mentioned_names(path.read_text(encoding="utf-8")):
            if name in registered:
                mentioned.add(name)
                continue
            if name.startswith(TOOL_PREFIXES):
                reason = "not a registered tool"
            elif name in arguments or name in plan_names or name in results:
                continue
            else:
                reason = "not a tool, documented argument, plan kind or result field"
            unknown.append({"file": path.name, "name": name, "reason": reason})
    return {"unknown": unknown, "unmentioned_tools": sorted(registered - mentioned),
            "mentioned_tools": len(mentioned), "registered_tools": len(registered)}


def main(argv: list[str]) -> int:
    report = audit(guidance_files(), load_tools(), plan_vocabulary(), result_fields())
    if "--json" in argv:
        print(json.dumps(report, indent=1))
    else:
        print(f"registered tools: {report['registered_tools']}, mentioned in guidance: {report['mentioned_tools']}")
        for item in report["unknown"]:
            print(f"UNKNOWN {item['file']}: `{item['name']}` ({item['reason']})")
        for name in report["unmentioned_tools"]:
            print(f"NOT MENTIONED: {name}")
        print("OK" if not report["unknown"] else f"FAIL: {len(report['unknown'])} unknown name(s)")
    return 1 if report["unknown"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
