#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live loop: bridge undo on a BACKGROUND Calc document, many times.

Each iteration brings another window of the isolated LibreOffice to the front
(macOS accessibility, by exact PID and window title), writes one cell of the
target document by document_id, undoes it through the bridge and checks the
cell is restored. It reports every undo's latency and outcome as JSON lines.
It never launches or kills LibreOffice.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_client import ClientError, invoke  # noqa: E402


def raise_window(pid: int, title: str) -> None:
    script = (
        f'tell application "System Events" to tell (first process whose unix id is {int(pid)})\n'
        "set frontmost to true\n"
        f'perform action "AXRaise" of window {json.dumps(title)}\n'
        "end tell"
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)


def activate_process(pid: int) -> None:
    script = (
        f'tell application "System Events" to set frontmost of '
        f"(first process whose unix id is {int(pid)}) to true"
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)


def hide_process(pid: int) -> None:
    script = (
        f'tell application "System Events" to set visible of '
        f"(first process whose unix id is {int(pid)}) to false"
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)


def document_id(port: int, token_file: str, title: str) -> str:
    documents = invoke(port, token_file, "list_documents", {})["data"]["documents"]
    matches = [item["document_id"] for item in documents if item["title"] == title]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one open document titled {title!r}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8660)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--pid", type=int, required=True, help="exact isolated soffice PID")
    parser.add_argument("--target", required=True, help="title of the Calc document to undo in")
    parser.add_argument("--front", required=True, help="comma-separated titles to rotate to the front")
    parser.add_argument("--sheet", default="Sheet1")
    parser.add_argument("--cell", default="Z1")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--settle", type=float, default=0.3, help="seconds after raising a window")
    parser.add_argument("--deactivate-to-pid", type=int, default=None,
                        help="after raising, make this other app frontmost so LibreOffice is inactive")
    parser.add_argument("--hide", action="store_true", help="after raising, hide LibreOffice (Cmd-H)")
    parser.add_argument("--idle", type=float, default=0.0, help="seconds to stay idle before each undo")
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    fronts = [title for title in options.front.split(",") if title]
    target = document_id(options.port, options.token_file, options.target)
    summary = {"iterations": 0, "ok": 0, "failures": 0, "max_seconds": 0.0}
    with open(options.output, "w", encoding="utf-8") as stream:
        for index in range(1, options.iterations + 1):
            front = fronts[(index - 1) % len(fronts)]
            raise_window(options.pid, front)
            time.sleep(options.settle)
            if options.deactivate_to_pid is not None:
                activate_process(options.deactivate_to_pid)
                time.sleep(options.settle)
            if options.hide:
                hide_process(options.pid)
                time.sleep(options.settle)
            row = {"iteration": index, "front": front, "lo_inactive": options.deactivate_to_pid is not None,
                   "hidden": options.hide, "idle": options.idle}
            invoke(options.port, options.token_file, "set_range", {
                "document_id": target, "sheet": options.sheet, "address": options.cell,
                "values": [[f"undo-loop-{index}"]],
            })
            if options.idle:
                time.sleep(options.idle)
            started = time.monotonic()
            try:
                result = invoke(options.port, options.token_file, "undo", {"document_id": target}, timeout=20.0)
                row["undo"] = {key: result["data"].get(key) for key in ("title", "activated_document")}
                row["ok"] = True
            except ClientError as exc:
                row["undo_error"] = str(exc)
                row["ok"] = False
            row["seconds"] = round(time.monotonic() - started, 3)
            cell = invoke(options.port, options.token_file, "get_range", {
                "document_id": target, "sheet": options.sheet, "address": options.cell,
            })["data"]["values"][0][0]
            row["restored"] = cell == ""
            if not row["restored"] and not row["ok"]:
                # A refused undo leaves the loop's own write in place; remove it in the GUI-free way
                # (a bridge clear is a new Hermes step, so the next iteration still undoes its own write).
                invoke(options.port, options.token_file, "clear_range", {
                    "document_id": target, "sheet": options.sheet, "address": options.cell,
                })
            summary["iterations"] += 1
            summary["ok" if row["ok"] and row["restored"] else "failures"] += 1
            summary["max_seconds"] = max(summary["max_seconds"], row["seconds"])
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            print(json.dumps(row, sort_keys=True), flush=True)
    print(json.dumps({"summary": summary}, sort_keys=True))
    return 0 if summary["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
