#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live check of libreoffice_undo and paragraph-mode Writer inserts through the real plugin handlers.

Needs a running isolated LibreOffice with exactly one Calc and one Writer document open. It never
launches or kills LibreOffice. Every change it makes is undone and verified by readback.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import plugin  # noqa: E402
from live_client import read_token  # noqa: E402


class Context:
    def __init__(self, port: int) -> None:
        self.port = port
        self.tools: dict = {}

    def get_config(self, key, default=None):
        return self.port if key == "bridge_port" else default

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs["handler"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8660)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    token = read_token(options.token_file)
    plugin._secret = lambda: token  # the isolated profile's token instead of Hermes's secret store
    ctx = Context(options.port)
    plugin.register(ctx)
    steps: list[dict] = []

    def call(name: str, **args):
        result = json.loads(ctx.tools[name](args))
        steps.append({"tool": name, "args": {k: v for k, v in args.items() if k != "document_id"}, "result": result})
        if isinstance(result, dict) and result.get("ok") is False:
            raise AssertionError(f"{name} failed: {result['error']}")
        return result

    summary: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        documents = call("calc_get_overview")["documents"]
        calc = [d for d in documents if d["type"] == "calc"]
        writer = [d for d in documents if d["type"] == "writer"]
        if len(calc) != 1 or len(writer) != 1:
            raise AssertionError(f"expected one Calc and one Writer document, got {len(calc)} and {len(writer)}")
        calc_id, writer_id = calc[0]["document_id"], writer[0]["document_id"]

        cell = {"document_id": calc_id, "sheet": "Sheet1", "address": "A20"}
        before = call("calc_get_range", **cell)["fingerprint"]
        call("calc_set_range", **cell, values=[["HERMES-B1-ONE"]])
        undone = call("libreoffice_undo", document_id=calc_id)
        if undone["undone"] != 1 or call("calc_get_range", **cell)["fingerprint"] != before:
            raise AssertionError("one-step Calc undo did not restore A20")
        call("calc_set_range", **cell, values=[["HERMES-B1-ONE"]])
        call("calc_set_range", **cell, values=[["HERMES-B1-TWO"]])
        undone = call("libreoffice_undo", document_id=calc_id, steps=2)
        if undone["undone"] != 2 or call("calc_get_range", **cell)["fingerprint"] != before:
            raise AssertionError("two-step Calc undo did not restore A20")
        summary["calc"] = {"one_step_restored": True, "two_steps_restored": True}

        original = call("writer_get_text", document_id=writer_id)["paragraphs"]
        for position, text in (("end", "HERMES-B1 paragraph at the end."), ("start", "HERMES-B1 paragraph at the start.")):
            call("writer_insert_text", document_id=writer_id, text=text, position=position, as_paragraph=True)
            changed = call("writer_get_text", document_id=writer_id)["paragraphs"]
            expected = original + [text] if position == "end" else [text] + original
            if changed != expected:
                raise AssertionError(f"{position}: paragraphs after insert were {changed!r}")
            undone = call("libreoffice_undo", document_id=writer_id)
            restored = call("writer_get_text", document_id=writer_id)["paragraphs"]
            if restored != original:
                raise AssertionError(f"{position}: undo left {restored!r} instead of {original!r}")
            summary[f"writer_{position}"] = {
                "own_paragraph": True, "undo_title": undone["steps"][0]["title"],
                "paragraphs_before": len(original), "paragraphs_after_insert": len(changed),
                "paragraphs_after_undo": len(restored), "no_empty_paragraph_left": True,
            }
        summary["status"] = "PASS"
    except Exception as exc:  # recorded in the transcript, then the run fails
        summary["status"] = "FAIL"
        summary["error"] = str(exc)
    output = Path(options.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"summary": summary, "steps": steps}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
