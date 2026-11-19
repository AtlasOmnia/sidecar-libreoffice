#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live Writer and Impress proof with Writer cleanup by undo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from live_client import invoke

MARKER = "\nHERMES-WRITER-LIVE-PROOF"


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8660)
    options = parser.parse_args()
    documents = invoke(options.port, options.token_file, "list_documents", {})["data"]["documents"]
    by_type = {kind: [item for item in documents if item["type"] == kind] for kind in ("writer", "impress")}
    if len(by_type["writer"]) != 1 or len(by_type["impress"]) != 1:
        raise RuntimeError(f"expected one Writer and one Impress document: {by_type}")
    writer_id = by_type["writer"][0]["document_id"]
    impress_id = by_type["impress"][0]["document_id"]
    writer = {"document": by_type["writer"][0], "steps": []}
    original = invoke(options.port, options.token_file, "writer_get_text", {"document_id": writer_id})
    writer["steps"].append({"name": "writer_get_text_before", "response": original})
    inserted = invoke(options.port, options.token_file, "writer_insert_text", {
        "document_id": writer_id, "text": MARKER, "position": "end",
    })
    writer["steps"].append({"name": "writer_insert_text", "response": inserted})
    changed = invoke(options.port, options.token_file, "writer_get_text", {"document_id": writer_id})
    writer["steps"].append({"name": "writer_get_text_after", "response": changed})
    if MARKER.strip() not in changed["data"]["text"]:
        raise AssertionError("Writer marker was not observed")
    undone = invoke(options.port, options.token_file, "undo", {"document_id": writer_id})
    writer["steps"].append({"name": "undo", "response": undone})
    restored = invoke(options.port, options.token_file, "writer_get_text", {"document_id": writer_id})
    writer["steps"].append({"name": "writer_get_text_restored", "response": restored})
    if restored["data"]["text"] != original["data"]["text"]:
        raise AssertionError("Writer undo did not restore original text")
    writer["status"] = "PASS"
    impress = invoke(options.port, options.token_file, "impress_list_slides", {"document_id": impress_id})
    if impress["data"]["slide_count"] < 1:
        raise AssertionError("Impress document has no slides")
    save(options.output_dir / "writer-live.json", writer)
    save(options.output_dir / "impress-live.json", {"status": "PASS", "document": by_type["impress"][0], "response": impress})
    print(json.dumps({"status": "PASS", "writer": str(options.output_dir / "writer-live.json"), "impress": str(options.output_dir / "impress-live.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
