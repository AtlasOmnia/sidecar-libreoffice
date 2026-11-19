#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Prove a live Calc edit, readback, one-step undo, and restoration."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from live_client import invoke

MARKERS = [
    ["HERMES-LIVE-A", "HERMES-LIVE-B", "HERMES-LIVE-C"],
    [10101, 20202, 30303],
    ["=1+1", "=2+2", "=3+3"],
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8660)
    parser.add_argument("--sheet", default="Sheet1")
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    transcript = {"timestamp": datetime.now(timezone.utc).isoformat(), "steps": []}
    try:
        health = invoke(options.port, options.token_file, "health", {})
        transcript["steps"].append({"name": "health", "response": health})
        documents = invoke(options.port, options.token_file, "list_documents", {})
        transcript["steps"].append({"name": "list_documents", "response": documents})
        calc_documents = [item for item in documents["data"]["documents"] if item["type"] == "calc"]
        if len(calc_documents) != 1:
            raise AssertionError(f"expected exactly one Calc document, got {len(calc_documents)}")
        document_id = calc_documents[0]["document_id"]
        original = invoke(options.port, options.token_file, "get_range", {
            "document_id": document_id, "sheet": options.sheet, "address": "A1:C3",
        })
        transcript["steps"].append({"name": "get_original", "response": original})
        original_fingerprint = original["data"]["fingerprint"]
        changed = invoke(options.port, options.token_file, "set_range", {
            "document_id": document_id,
            "sheet": options.sheet, "address": "A1:C3", "values": MARKERS, "if_match": original_fingerprint,
        })
        transcript["steps"].append({"name": "set", "response": changed})
        readback = invoke(options.port, options.token_file, "get_range", {
            "document_id": document_id, "sheet": options.sheet, "address": "A1:C3",
        })
        transcript["steps"].append({"name": "get_changed", "response": readback})
        values = readback["data"]["values"]
        formulas = readback["data"]["formulas"]
        expected_values = [MARKERS[0], MARKERS[1], [2.0, 4.0, 6.0]]
        if values != expected_values or formulas[2] != MARKERS[2]:
            raise AssertionError(f"marker readback mismatch: values={values!r} formulas={formulas!r}")
        undone = invoke(options.port, options.token_file, "undo", {"document_id": document_id})
        transcript["steps"].append({"name": "undo", "response": undone})
        restored = invoke(options.port, options.token_file, "get_range", {
            "document_id": document_id, "sheet": options.sheet, "address": "A1:C3",
        })
        transcript["steps"].append({"name": "get_restored", "response": restored})
        restored_fingerprint = restored["data"]["fingerprint"]
        if restored_fingerprint != original_fingerprint:
            raise AssertionError("undo did not restore the original fingerprint")
        transcript["summary"] = {
            "status": "PASS", "range": f"{options.sheet}!A1:C3", "marker_verified": True,
            "undo_restored_original_fingerprint": True,
            "original_fingerprint": original_fingerprint,
            "changed_fingerprint": readback["data"]["fingerprint"],
            "restored_fingerprint": restored_fingerprint,
        }
    except Exception as exc:
        transcript["summary"] = {"status": "FAIL", "error": str(exc)}
    output = Path(options.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(transcript["summary"], indent=2, sort_keys=True))
    if transcript["summary"]["status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
