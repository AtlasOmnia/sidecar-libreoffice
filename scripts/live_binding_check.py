#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live proof that writes follow document_id, not the frontmost window."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_client import ClientError, invoke  # noqa: E402


def call(options, op, args):
    """Return the bridge envelope; bridge errors come back as {"ok": false, ...}."""
    try:
        return invoke(options.port, options.token_file, op, args)
    except ClientError as exc:
        try:
            return json.loads(str(exc))
        except json.JSONDecodeError:
            return {"ok": False, "category": "Unavailable", "error": str(exc)}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def options_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--token-file", required=True, help="test-harness read of the isolated profile token")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8660)
    return parser


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def main() -> int:
    options = options_parser(__doc__).parse_args()
    steps = []

    def step(name, op, args):
        response = call(options, op, args)
        steps.append({"name": name, "op": op, "args": args, "response": response})
        return response

    docs = step("list_documents", "list_documents", {})["data"]["documents"]
    calc = {item["title"]: item for item in docs if item["type"] == "calc"}
    writer = [item for item in docs if item["type"] == "writer"]
    expect("calc-a.ods" in calc and "calc-b.ods" in calc and writer, f"need calc-a, calc-b and a Writer doc: {docs}")
    a, b = calc["calc-a.ods"], calc["calc-b.ods"]
    expect(not a["frontmost"], "calc-a.ods must not be frontmost for this proof; bring another document to the front")
    overview = step("overview", "get_overview", {})["data"]["documents"]
    sheet_a = [d for d in overview if d["document_id"] == a["document_id"]][0]["sheets"][0]["name"]
    sheet_b = [d for d in overview if d["document_id"] == b["document_id"]][0]["sheets"][0]["name"]
    target = {"document_id": a["document_id"], "sheet": sheet_a, "address": "E1:F2"}
    witness = {"document_id": b["document_id"], "sheet": sheet_b, "address": "E1:F2"}
    a_before = step("get_a_before", "get_range", target)["data"]
    b_before = step("get_b_before", "get_range", witness)["data"]
    values = [["BOUND", "WRITE"], ["calc-a", "only"]]
    written = step("set_a_by_id_while_not_frontmost", "set_range", {**target, "values": values, "if_match": a_before["fingerprint"]})
    expect(written.get("ok") is True, f"bound write failed: {written}")
    a_after = step("get_a_after", "get_range", target)["data"]
    b_after = step("get_b_after", "get_range", witness)["data"]
    expect(a_after["values"] == values, "calc-a did not receive the bound write")
    expect(b_after["fingerprint"] == b_before["fingerprint"], "calc-b changed: write was retargeted")
    frontmost_after = [d["title"] for d in step("list_after", "list_documents", {})["data"]["documents"] if d["frontmost"]]
    stale = step("stale_if_match", "set_range", {**target, "values": values, "if_match": a_before["fingerprint"]})
    expect(stale.get("category") == "Stale", f"expected Stale: {stale}")
    bogus = step("bogus_document_id", "set_range", {**target, "document_id": "lo:00000000:bogus", "values": values})
    expect(bogus.get("category") == "NotFound", f"expected NotFound: {bogus}")
    wrong = step("calc_tool_on_writer", "get_range", {**target, "document_id": writer[0]["document_id"]})
    expect(wrong.get("category") == "Validation", f"expected Validation: {wrong}")
    undone = step("undo_a", "undo", {"document_id": a["document_id"]})
    expect(undone.get("ok") is True and undone["data"]["title"].startswith("Hermes:"), f"undo failed: {undone}")
    a_restored = step("get_a_restored", "get_range", target)["data"]
    expect(a_restored["fingerprint"] == a_before["fingerprint"], "calc-a was not restored by undo")
    b_final = step("get_b_final", "get_range", witness)["data"]
    b_sheets = [d for d in step("overview_final", "get_overview", {})["data"]["documents"]
                if d["document_id"] == b["document_id"]][0]["sheets"]
    b_sheets_before = [d for d in overview if d["document_id"] == b["document_id"]][0]["sheets"]
    expect(b_final["fingerprint"] == b_before["fingerprint"] and
           [s["name"] for s in b_sheets] == [s["name"] for s in b_sheets_before], "calc-b changed during undo")
    result = {
        "status": "PASS",
        "bound_document": a["title"], "witness_document": b["title"],
        "frontmost_during_write": frontmost_after,
        "undo_activated_bound_document": undone["data"].get("activated_document"),
        "steps": steps,
    }
    save(options.output_dir / "binding-live.json", result)
    print(json.dumps({"status": "PASS", "frontmost_during_write": frontmost_after,
                      "transcript": str(options.output_dir / "binding-live.json")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
