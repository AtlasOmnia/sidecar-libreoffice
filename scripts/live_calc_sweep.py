#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live sweep of every Calc tool operation, then full undo back to the baseline."""
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


SWEEP_SHEET = "HermesSweep"


def main() -> int:
    options = options_parser(__doc__).parse_args()
    steps = []

    def step(name, op, args):
        response = call(options, op, args)
        steps.append({"name": name, "op": op, "args": args, "response": response})
        return response

    overview = step("overview", "get_overview", {})["data"]["documents"]
    calc = [d for d in overview if d["type"] == "calc" and d["title"] == "calc-a.ods"]
    expect(len(calc) == 1, "calc-a.ods must be open exactly once")
    doc = calc[0]
    did, sheet = doc["document_id"], doc["sheets"][0]["name"]
    expect(SWEEP_SHEET not in [s["name"] for s in doc["sheets"]], f"{SWEEP_SHEET} already exists; restore the document first")
    region = {"document_id": did, "sheet": sheet, "address": "A1:H20"}
    baseline = step("baseline", "get_range", region)["data"]
    # Witness: every other open Calc document must keep its sheets and A1:H20 through the sweep,
    # including its undo phase (LibreOffice applies parts of undo through the focused view).
    witnesses = {d["document_id"]: {"title": d["title"], "sheets": [s["name"] for s in d["sheets"]],
                                    "fingerprint": step(f"witness_before_{d['title']}", "get_range", {
                                        "document_id": d["document_id"], "sheet": d["sheets"][0]["name"],
                                        "address": "A1:H20"})["data"]["fingerprint"]}
                 for d in overview if d["type"] == "calc" and d["document_id"] != did}
    target = {"document_id": did, "sheet": sheet, "address": "G1:H2"}
    mutations = []

    def mutate(name, op, args):
        response = step(name, op, args)
        expect(response.get("ok") is True, f"{name} failed: {response}")
        mutations.append(name)
        return response

    mutate("set_range", "set_range", {**target, "values": [["SWEEP", 2], ["=H1*21", None]]})
    got = step("get_after_set", "get_range", target)["data"]
    expect(got["values"][1][0] == 42, f"formula did not evaluate: {got['values']}")
    mutate("format_range", "format_range", {**target, "format": {"bold": True, "fillColor": "#FFE699", "numberFormat": "0.00", "borders": True}})
    bad_format = step("format_unsupported_key", "format_range", {**target, "format": {"mergeCells": True}})
    expect(bad_format.get("category") == "Validation", f"expected Validation: {bad_format}")
    mutate("clear_range", "clear_range", target)
    cleared = step("get_after_clear", "get_range", target)["data"]
    expect(all(v in ("", None) for row in cleared["values"] for v in row), f"clear left values: {cleared['values']}")
    mutate("create_sheet", "create_sheet", {"document_id": did, "name": SWEEP_SHEET})
    duplicate = step("create_sheet_duplicate", "create_sheet", {"document_id": did, "name": SWEEP_SHEET})
    expect(duplicate.get("category") == "Validation", f"expected Validation: {duplicate}")
    inspect = step("plan_inspect", "execute_plan", {"document_id": did, "plan": {
        "version": 1, "outcome": "inspect", "reason": "sweep inspect",
        "reads": [{"kind": "read_range", "sheet": sheet, "address": "A1:C3"},
                  {"kind": "summarize_range_stats", "sheet": sheet, "address": "A1:C3"}]}})
    expect(inspect.get("ok") is True, f"inspect failed: {inspect}")
    plan = {"version": 1, "outcome": "execute", "summary": "sweep execute", "operations": [
        {"kind": "write_values", "sheet": SWEEP_SHEET, "address": "A1:B2", "values": [["Item", "Qty"], ["Widget", 3]]},
        {"kind": "write_formula", "sheet": SWEEP_SHEET, "address": "B3", "formula": "=B2*2"},
        {"kind": "format_range", "sheet": SWEEP_SHEET, "address": "A1:B1", "format": {"bold": True}},
    ], "verification": [
        {"kind": "range_values_equal", "sheet": SWEEP_SHEET, "address": "A1:B3", "expectedValues": [["Item", "Qty"], ["Widget", 3], ["", 6]]},
        {"kind": "range_formulas_equal", "sheet": SWEEP_SHEET, "address": "B3", "expectedFormulas": [["=B2*2"]]},
        {"kind": "sheet_exists", "name": SWEEP_SHEET},
        {"kind": "sentinel_unchanged", "sheet": SWEEP_SHEET, "address": "D10", "expected": None},
    ]}
    executed = mutate("plan_execute", "execute_plan", {"document_id": did, "plan": plan})
    expect(executed["data"]["verified"] is True, f"plan verification failed: {executed['data']}")
    bad_plan = step("plan_invalid_whole_plan", "execute_plan", {"document_id": did, "plan": {
        **plan, "operations": [plan["operations"][0], {"kind": "delete_sheet", "name": sheet}]}})
    expect(bad_plan.get("category") == "Validation", f"expected Validation: {bad_plan}")
    undo_titles = []
    undo_results = []
    for _ in mutations:
        undone = step("undo", "undo", {"document_id": did})
        expect(undone.get("ok") is True, f"undo failed: {undone}")
        undo_titles.append(undone["data"]["title"])
        undo_results.append({k: undone["data"].get(k) for k in ("title", "activated_document")})
    expect(all(t.startswith("Hermes:") for t in undo_titles), f"unexpected undo titles: {undo_titles}")
    final = step("final_overview", "get_overview", {})["data"]["documents"]
    final_doc = [d for d in final if d["document_id"] == did][0]
    expect(SWEEP_SHEET not in [s["name"] for s in final_doc["sheets"]], "sweep sheet survived undo")
    restored = step("restored", "get_range", region)["data"]
    expect(restored["fingerprint"] == baseline["fingerprint"], "calc-a A1:H20 not restored to baseline")
    for d in final:
        if d["document_id"] in witnesses:
            before = witnesses[d["document_id"]]
            now = step(f"witness_after_{d['title']}", "get_range", {"document_id": d["document_id"],
                       "sheet": before["sheets"][0], "address": "A1:H20"})["data"]["fingerprint"]
            expect([s["name"] for s in d["sheets"]] == before["sheets"] and now == before["fingerprint"],
                   f"witness document {before['title']} changed during the sweep")
    save(options.output_dir / "calc-sweep-live.json", {"status": "PASS", "mutations": mutations, "undo_titles": undo_titles, "undo_results": undo_results,
                                                         "witnesses_unchanged": [w["title"] for w in witnesses.values()], "steps": steps})
    print(json.dumps({"status": "PASS", "mutations": mutations, "transcript": str(options.output_dir / "calc-sweep-live.json")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
