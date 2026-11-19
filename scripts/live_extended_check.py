#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Live check of the sheet, find-and-replace, style, selection and slide operations.

Needs a running isolated LibreOffice with exactly one Calc, one Writer and one Impress document
open (the docs/fixtures demo files work). It never launches or kills LibreOffice. Every change
is undone, or removed again when LibreOffice recorded no undo step, and the documents are
compared with their starting state at the end. The report lists, per operation, whether
LibreOffice recorded an undo step and whether undoing it restored the document.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_client import ClientError, invoke  # noqa: E402


class Check:
    def __init__(self, port: int, token_file: str) -> None:
        self.port, self.token_file = port, token_file
        self.report: dict[str, dict] = {}

    def call(self, op: str, **args):
        value = invoke(self.port, self.token_file, op, args, timeout=20.0)
        if not value.get("ok"):
            raise SystemExit(f"{op} failed: {value}")
        return value["data"]

    def refused(self, op: str, category: str, **args) -> None:
        try:
            value = invoke(self.port, self.token_file, op, args, timeout=20.0)
        except ClientError as exc:
            detail = json.loads(str(exc))
            expect(detail.get("category") == category, f"{op}: expected {category}, got {detail}")
            return
        raise SystemExit(f"{op} was not refused: {value}")

    def undo(self, document_id: str) -> str:
        return self.call("undo", document_id=document_id, hermes_only=True)["title"]

    def change(self, name: str, document_id: str, op: str, state, *, cleanup=None, **args):
        """Run a mutation, then undo it (or clean up) and require the starting state again."""
        before = state()
        data = self.call(op, document_id=document_id, **args)
        after = state()
        entry = {"undoable": bool(data.get("undoable")), "changed": before != after}
        expect(entry["changed"], f"{name}: the document did not change")
        if entry["undoable"]:
            title = self.undo(document_id)
            expect(title == data["undo_title"], f"{name}: undid {title!r}, expected {data['undo_title']!r}")
            entry["undo_restored"] = state() == before
            expect(entry["undo_restored"], f"{name}: undo did not restore the document")
        else:
            expect(cleanup is not None, f"{name}: LibreOffice recorded no undo step and there is no cleanup")
            cleanup(data)
            entry["cleaned_up"] = state() == before
            expect(entry["cleaned_up"], f"{name}: cleanup did not restore the document")
        self.report[name] = entry
        return data


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8660)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    check = Check(options.port, options.token_file)
    documents = check.call("list_documents")["documents"]
    by_type = {kind: [d for d in documents if d["type"] == kind] for kind in ("calc", "writer", "impress")}
    for kind, found in by_type.items():
        expect(len(found) == 1, f"exactly one {kind} document must be open, found {len(found)}")
    calc, writer, impress = (by_type[kind][0]["document_id"] for kind in ("calc", "writer", "impress"))

    # ---- Calc ----
    def calc_state():
        overview = [d for d in check.call("get_overview")["documents"] if d["document_id"] == calc][0]
        names = [sheet["name"] for sheet in overview["sheets"]]
        return names, check.call("get_range", document_id=calc, sheet=names[0], address="A1:H20")["fingerprint"]

    calc_before = calc_state()
    sheet = calc_before[0][0]
    stats = check.call("calc_range_stats", document_id=calc, sheet=sheet, address="A1:H20")["stats"]
    expect(stats["count"] == 160 and stats["numericCount"] >= 1, f"unexpected stats: {stats}")
    check.report["calc_range_stats"] = {"read_only": True, "stats": stats}

    check.change("calc_copy_sheet", calc, "calc_copy_sheet", calc_state, sheet=sheet, new_name="HermesCopy")
    check.call("calc_copy_sheet", document_id=calc, sheet=sheet, new_name="HermesCopy")
    check.change("calc_rename_sheet", calc, "calc_rename_sheet", calc_state, sheet="HermesCopy", new_name="HermesRenamed")
    check.change("calc_delete_sheet", calc, "calc_delete_sheet", calc_state, sheet="HermesCopy")
    check.undo(calc)  # the second copy
    expect(calc_state() == calc_before, "Calc sheets were not restored after the sheet checks")

    check.call("set_range", document_id=calc, sheet=sheet, address="G1:G2", values=[["hermes alpha"], ["alpha"]])
    replaced = check.change("calc_find_replace", calc, "calc_find_replace", calc_state, sheet=sheet, search="alpha", replace="gamma")
    expect(replaced["replaced"] == 2, f"expected 2 replacements, got {replaced}")
    whole = check.change("calc_find_replace_whole_cell", calc, "calc_find_replace", calc_state, sheet=sheet,
                         search="alpha", replace="gamma", whole_cell=True)
    expect(whole["replaced"] == 1, f"expected 1 whole-cell replacement, got {whole}")
    none = check.call("calc_find_replace", document_id=calc, sheet=sheet, search="no-such-text-zz", replace="x")
    expect(none["replaced"] == 0 and none["undoable"] is False, f"unexpected empty replace: {none}")
    check.undo(calc)  # the set_range
    expect(calc_state() == calc_before, "Calc was not restored after find and replace")

    check.call("set_range", document_id=calc, sheet=sheet, address="G1:G2", values=[[1], [2]])
    filled = check.change("calc_autofill", calc, "calc_autofill", calc_state, sheet=sheet, address="G1:G6",
                          direction="down", source_count=2)
    expect([row[0] for row in filled["values"]] == [1, 2, 3, 4, 5, 6], f"unexpected fill: {filled['values']}")
    check.undo(calc)
    expect(calc_state() == calc_before, "Calc was not restored after autofill")

    def comment_state():
        return check.call("calc_get_comments", document_id=calc, sheet=sheet)["comments"]

    expect(comment_state() == [], "the Calc sheet must start without comments")
    added = check.change("calc_add_comment", calc, "calc_add_comment", comment_state, sheet=sheet, cell="G1", text="Hermes live check")
    expect(added["comment"] == "Hermes live check", f"comment not read back: {added}")
    check.call("calc_add_comment", document_id=calc, sheet=sheet, cell="G1", text="Hermes live check")
    expect(comment_state() == [{"cell": "G1", "text": "Hermes live check"}], f"unexpected comments: {comment_state()}")
    check.change("calc_clear_comments", calc, "calc_clear_comments", comment_state, sheet=sheet, address="G1:G2")
    check.undo(calc)  # the second add
    expect(comment_state() == [], "comments were not restored")
    check.report["calc_get_comments"] = {"read_only": True}
    check.refused("calc_add_comment", "Validation", document_id=calc, sheet=sheet, cell="G1:G2", text="x")
    check.refused("calc_delete_sheet", "NotFound", document_id=calc, sheet="NoSuchSheetZz")
    expect(calc_state() == calc_before, "Calc was not restored at the end")

    # ---- Writer ----
    def writer_state():
        structure = check.call("writer_get_structure", document_id=writer)
        return [(p.get("style"), p.get("preview")) for p in structure["paragraphs"]], check.call("writer_get_text", document_id=writer)["text"]

    writer_before = writer_state()
    first_style, first_preview = writer_before[0][0]
    full_first = check.call("writer_get_text", document_id=writer)["paragraphs"][0]
    expect(bool(full_first.strip()), "the Writer document's first paragraph must hold text")
    check.report["writer_get_structure"] = {"read_only": True, "first_style": first_style}
    styled = check.change("writer_set_paragraph_style", writer, "writer_set_paragraph_style", writer_state,
                          paragraph=1, style="Heading 1", expected_text=full_first)
    expect(styled["style"] == "Heading 1" and styled["previous_style"] == first_style, f"unexpected style result: {styled}")
    check.refused("writer_set_paragraph_style", "Stale", document_id=writer, paragraph=1, style="Heading 1", expected_text=full_first + "x")
    check.refused("writer_set_paragraph_style", "Validation", document_id=writer, paragraph=1, style="No Such Style Zz")
    word = full_first.split()[0]
    replaced = check.change("writer_find_replace", writer, "writer_find_replace", writer_state, search=word, replace="HERMESX", match_case=True)
    expect(replaced["replaced"] >= 1, f"unexpected replace: {replaced}")
    selection = check.call("writer_get_selection", document_id=writer)
    check.report["writer_get_selection"] = {"read_only": True, "selections": len(selection["selections"])}
    check.refused("writer_replace_selection", "Stale", document_id=writer, text="x", expected_text=selection["text"] + "-stale")
    if len(selection["selections"]) == 1:
        check.change("writer_replace_selection", writer, "writer_replace_selection", writer_state,
                     text="[Hermes]", expected_text=selection["text"])
    else:
        check.report["writer_replace_selection"] = {"skipped": "the document has no single selection"}
    expect(writer_state() == writer_before, "Writer was not restored at the end")

    # ---- Impress ----
    def impress_state():
        slides = check.call("impress_list_slides", document_id=impress)
        return slides["slide_count"], [(s["title"], s["text"]) for s in slides["slides"]]

    impress_before = impress_state()
    count = impress_before[0]
    first = check.call("impress_get_slide", document_id=impress, slide=1)
    check.report["impress_get_slide"] = {"read_only": True, "shapes": len(first["shapes"]), "layout": first["layout"]}
    remove_last = lambda _data: check.call("impress_delete_slide", document_id=impress, slide=count + 1)
    created = check.change("impress_create_slide", impress, "impress_create_slide", impress_state, cleanup=remove_last,
                           title="Hermes live slide", body="First point\nSecond point")
    expect(created["slide"] == count + 1 and created["title_set"] and created["body_set"], f"unexpected create result: {created}")

    # A working slide for the remaining checks; removed at the end.
    check.call("impress_create_slide", document_id=impress, title="Working slide", body="Body text")
    working = count + 1
    read = check.call("impress_get_slide", document_id=impress, slide=working)
    roles = {shape.get("placeholder"): shape for shape in read["shapes"]}
    expect(roles.get("title", {}).get("text") == "Working slide" and roles.get("body", {}).get("text") == "Body text",
           f"placeholders not read back: {read['shapes']}")
    restore_title = lambda data: check.call("impress_set_shape_text", document_id=impress, slide=working, placeholder="title", text=data["previous_text"])
    check.change("impress_set_shape_text", impress, "impress_set_shape_text", impress_state, cleanup=restore_title,
                 slide=working, placeholder="title", text="Retitled by Hermes")

    def slide_shapes():
        return [(s["type"], s.get("text")) for s in check.call("impress_get_slide", document_id=impress, slide=working)["shapes"]]
    check.change("impress_add_text_box", impress, "impress_add_text_box", slide_shapes,
                 slide=working, text="Text box from Hermes", x_cm=2, y_cm=12, width_cm=10, height_cm=2)
    swap_back = lambda _data: check.call("impress_find_replace", document_id=impress, search="Body-replaced", replace="Body", match_case=True)
    replaced = check.change("impress_find_replace", impress, "impress_find_replace", impress_state, cleanup=swap_back,
                            search="Body", replace="Body-replaced", slide=working, match_case=True)
    expect(replaced["replaced"] == 1, f"unexpected replace: {replaced}")
    restore_slide = lambda _data: check.call("impress_create_slide", document_id=impress, title="Working slide", body="Body text")
    check.change("impress_delete_slide", impress, "impress_delete_slide", impress_state, cleanup=restore_slide, slide=working)
    check.refused("impress_get_slide", "NotFound", document_id=impress, slide=working + 5)
    # Remove the working slide: by undo when its creation was recorded, otherwise by deleting it.
    if check.report["impress_create_slide"]["undoable"]:
        check.undo(impress)
    else:
        check.call("impress_delete_slide", document_id=impress, slide=working)
    expect(impress_state() == impress_before, "Impress was not restored at the end")

    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps({"status": "PASS", "operations": check.report}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "operations": check.report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
