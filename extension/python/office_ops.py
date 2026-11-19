# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Further Calc, Writer and Impress operations for the in-process bridge.

``ExtraOps`` is mixed into ``BridgeRuntime``. It relies on the runtime's ``dispatch``,
``resolve_document``, ``document_identity``, ``_read_range``, ``_sheet_and_range``, ``_stats``
and ``_fault`` (the bridge's fault class). Every mutation runs inside one undo context titled
``Hermes: ...`` and reports ``undoable``: whether LibreOffice actually recorded that step.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

import uno
import unohelper

try:
    from com.sun.star.document import XUndoAction
    from com.sun.star.lang import XComponent
except ImportError:  # unit tests load this module without LibreOffice
    XUndoAction = object

    class XComponent:  # noqa: D101 - stand-in base for unit tests
        pass

from range_logic import parse_a1, validate_matrix

MAX_TEXT = 50000
MAX_SEARCH = 1000
MAX_STATS_CELLS = 10000
MAX_SHAPES = 200
MAX_STRUCTURE = 500
PREVIEW = 200
# Paged Writer reads: blocks (paragraphs and tables, in body order) per page, table cells per
# page, and characters per cell. A continuation token binds the next page to the exact content
# the previous page was read from.
MAX_PAGE_BLOCKS = 500
MAX_TABLE_CELLS = 2000
MAX_CELL_TEXT = 2000
MAX_INSERT_PARAGRAPHS = 50
MAX_EDITS = 50
MAX_PARAGRAPH_TEXT = 10000
# Serialised JSON per Writer read (pages, table windows, comments): well below the plugin's 2 MiB
# response limit (plugin/bridge_client.MAX_RESPONSE_BYTES), measured the way the bridge sends it.
MAX_READ_BYTES = 1024 * 1024
# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
PARAGRAPH_BREAK = 0
CONTINUATION = re.compile(r"^([1-9][0-9]{0,5})\.([0-9a-f]{32})$")
# com.sun.star.sheet.CellFlags.ANNOTATION
CELL_FLAG_ANNOTATION = 8
# com.sun.star.presentation layouts (AutoLayout numbers).
SLIDE_LAYOUTS = {"title": 0, "title_content": 1, "title_only": 19, "blank": 20}
# Layouts an existing slide may be switched to (AutoLayout numbers); creation keeps the four above.
CHANGE_LAYOUTS = {**SLIDE_LAYOUTS, "title_two_content": 3}
SHAPE_KINDS = {"rectangle": "RectangleShape", "rounded_rectangle": "RectangleShape", "ellipse": "EllipseShape",
               "line": "LineShape"}
IMPRESS_TEXT_KEYS = {"bold", "italic", "underline", "font_name", "font_size", "color"}
TITLE_SHAPE = "com.sun.star.presentation.TitleTextShape"
BODY_SHAPES = ("com.sun.star.presentation.OutlinerShape", "com.sun.star.presentation.SubtitleShape")


def _is_str(value: Any, limit: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value))


def _is_index(value: Any, limit: int = 100000) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= limit


def _json_bytes(value: Any) -> int:
    """Bytes ``value`` takes in a bridge reply (handler_for.send_json's encoding)."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class HermesUndoAction(unohelper.Base, XComponent, XUndoAction):
    """One undo step made of two callables.

    Impress records no undo action for a slide or shape that is added through its document API
    (seen live on LibreOffice 26.8: undoing a new slide emptied its text and left the slide). The
    bridge therefore adds this action itself: ``undo`` removes what was added and ``redo`` builds
    it again.

    The undo manager disposes an action it drops (cleared stack, closed document). dispose()
    releases the callables, and every document object they hold, at that point. Left to pyuno's
    GC thread, the release came after the model was gone, and a held text shape aborted
    LibreOffice 26.8 when a deck was closed after impress_add_text_box.
    """

    def __init__(self, title: str, undo_fn: Callable[[], Any], redo_fn: Callable[[], Any]) -> None:
        self.Title = title
        self._undo_fn = undo_fn
        self._redo_fn = redo_fn

    def undo(self) -> None:
        self._undo_fn()

    def redo(self) -> None:
        self._redo_fn()

    def dispose(self) -> None:
        self._undo_fn = self._redo_fn = None

    def addEventListener(self, _listener: Any) -> None:
        pass

    def removeEventListener(self, _listener: Any) -> None:
        pass


class ExtraOps:
    """Operations added after the first release; see the module docstring."""

    # -- shared helpers ---------------------------------------------------

    def _check(self, args: dict[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
        if not isinstance(args, dict) or not required <= set(args) or not set(args) <= required | set(optional):
            raise self._fault("Validation", "operation arguments are invalid", 400)
        if not _is_str(args.get("document_id"), 512):
            raise self._fault("Validation", "document_id is required", 400)

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise self._fault("Validation", message, 400)

    @staticmethod
    def _undo_titles(undo: Any) -> tuple[str, ...]:
        """Every step on the undo stack, newest first (as getAllUndoActionTitles lists them)."""
        return tuple(str(title) for title in undo.getAllUndoActionTitles())

    @staticmethod
    def _recorded(before: tuple[str, ...], after: tuple[str, ...], title: str) -> bool:
        """Whether exactly one step titled ``title`` was pushed on top of ``before``.

        The title alone is not enough: an undo context that records nothing leaves the stack as
        it was, and the step below may carry the same title (seen live on 26.8). At LibreOffice's
        step limit the oldest step drops off; an unchanged list is never counted as recorded.
        """
        if after == (title,) + before:
            return True
        return bool(before) and after != before and after == (title,) + before[:-1]

    def _in_undo(self, doc: Any, title: str, begin: Callable[[], None], work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run ``work`` as one undo step and say whether LibreOffice recorded it."""
        undo = doc.getUndoManager()
        before = self._undo_titles(undo)
        begin()
        undo.enterUndoContext(title)
        try:
            result = work()
        finally:
            undo.leaveUndoContext()
        recorded = self._recorded(before, self._undo_titles(undo), title)
        result.update({"undo_title": title, "undoable": recorded, "document": self.document_identity(doc)})
        return result

    def _in_custom_undo(self, doc: Any, title: str, begin: Callable[[], None], build: Callable[[], dict[str, Any]],
                        remove: Callable[[], Any]) -> dict[str, Any]:
        """Run ``build`` with LibreOffice's own recording paused, then add one bridge undo step.

        Undo calls ``remove`` and redo calls ``build`` again, so the step never depends on the
        partial actions LibreOffice would have recorded for it.
        If ``build`` fails part-way, ``remove`` runs at once (still unrecorded) to take back what
        was already changed, and the fault says whether that worked (``compensated``): there is no
        undo step for a failed change.
        """
        undo = doc.getUndoManager()
        before = self._undo_titles(undo)
        begin()
        undo.lock()
        try:
            try:
                result = build()
            except Exception as exc:
                try:
                    remove()
                    compensated = True
                except Exception:
                    compensated = False
                raise self._fault(
                    "ExecutionUncertain",
                    f"LibreOffice failed part-way through this change ({type(exc).__name__}); there is no undo step for it. "
                    + ("The bridge took back what had changed (compensated: true); re-read the document to confirm."
                       if compensated else
                       "The bridge could not take back what had changed (compensated: false); check the document."),
                    500, state="started") from exc
        finally:
            undo.unlock()
        undo.addUndoAction(HermesUndoAction(title, remove, build))
        recorded = self._recorded(before, self._undo_titles(undo), title)
        result.update({"undo_title": title, "undoable": recorded, "document": self.document_identity(doc)})
        return result

    def _sheet(self, doc: Any, name: str) -> Any:
        sheets = doc.getSheets()
        if not sheets.hasByName(name):
            raise self._fault("NotFound", f"Calc sheet does not exist: {name}", 404)
        return sheets.getByName(name)

    def _slide(self, doc: Any, number: int) -> Any:
        pages = doc.getDrawPages()
        if number > pages.getCount():
            raise self._fault("NotFound", f"slide {number} does not exist (the presentation has {pages.getCount()})", 404)
        return pages.getByIndex(number - 1)

    @staticmethod
    def _shape_text(shape: Any) -> str | None:
        try:
            return str(shape.getString())
        except Exception:
            return None

    # -- Calc -------------------------------------------------------------

    def calc_rename_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "new_name"})
        document_id, sheet_name, new_name = args["document_id"], args["sheet"], args["new_name"]
        self._require(_is_str(sheet_name, 255) and _is_str(new_name, 255), "sheet and new_name must be bounded names")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheet = self._sheet(doc, sheet_name)
            self._require(not doc.getSheets().hasByName(new_name), f"Calc sheet already exists: {new_name}")

            def work() -> dict[str, Any]:
                sheet.setName(new_name)
                return {"renamed": True, "sheet": sheet_name, "new_name": new_name}
            return self._in_undo(doc, f"Hermes: rename sheet {sheet_name}", begin, work)
        return self.dispatch(write, mutation=True)

    def calc_copy_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "new_name"})
        document_id, sheet_name, new_name = args["document_id"], args["sheet"], args["new_name"]
        self._require(_is_str(sheet_name, 255) and _is_str(new_name, 255), "sheet and new_name must be bounded names")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            self._sheet(doc, sheet_name)
            sheets = doc.getSheets()
            self._require(not sheets.hasByName(new_name), f"Calc sheet already exists: {new_name}")

            def work() -> dict[str, Any]:
                sheets.copyByName(sheet_name, new_name, len(sheets.getElementNames()))
                return {"copied": True, "sheet": sheet_name, "new_name": new_name}
            return self._in_undo(doc, f"Hermes: copy sheet {sheet_name}", begin, work)
        return self.dispatch(write, mutation=True)

    def calc_delete_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet"})
        document_id, sheet_name = args["document_id"], args["sheet"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            self._sheet(doc, sheet_name)
            sheets = doc.getSheets()
            self._require(len(sheets.getElementNames()) > 1, "the only sheet of a spreadsheet cannot be deleted")

            def work() -> dict[str, Any]:
                sheets.removeByName(sheet_name)
                return {"deleted": True, "sheet": sheet_name}
            return self._in_undo(doc, f"Hermes: delete sheet {sheet_name}", begin, work)
        return self.dispatch(write, mutation=True)

    def calc_find_replace(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "search", "replace"}, {"match_case", "whole_cell"})
        document_id, sheet_name, search, replace = args["document_id"], args["sheet"], args["search"], args["replace"]
        match_case, whole_cell = args.get("match_case", False), args.get("whole_cell", False)
        self._require(_is_str(sheet_name, 255) and _is_str(search, MAX_SEARCH) and _is_str(replace, MAX_SEARCH, empty=True)
                      and isinstance(match_case, bool) and isinstance(whole_cell, bool), "find and replace arguments are invalid")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheet = self._sheet(doc, sheet_name)
            descriptor = sheet.createReplaceDescriptor()
            descriptor.SearchString = search
            descriptor.ReplaceString = replace
            descriptor.SearchCaseSensitive = match_case
            descriptor.SearchWords = whole_cell
            found = sheet.findAll(descriptor)
            if found is None or found.getCount() == 0:
                return {"replaced": 0, "sheet": sheet_name, "undoable": False, "document": self.document_identity(doc)}

            def work() -> dict[str, Any]:
                return {"replaced": int(sheet.replaceAll(descriptor)), "sheet": sheet_name}
            return self._in_undo(doc, f"Hermes: replace in {sheet_name}", begin, work)
        return self.dispatch(write, mutation=True)

    def calc_autofill(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address", "direction"}, {"source_count"})
        document_id, sheet_name, address, direction = args["document_id"], args["sheet"], args["address"], args["direction"]
        source_count = args.get("source_count", 1)
        self._require(_is_str(sheet_name, 255) and direction in ("down", "right") and _is_index(source_count, 1000),
                      "autofill arguments are invalid")
        bounds = parse_a1(address)
        span = (bounds[3] - bounds[1] + 1) if direction == "down" else (bounds[2] - bounds[0] + 1)
        self._require(source_count < span, "address must be larger than the source rows or columns it is filled from")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            _sheet, cell_range = self._sheet_and_range(doc, sheet_name, address)
            fill = uno.Enum("com.sun.star.sheet.FillDirection", "TO_BOTTOM" if direction == "down" else "TO_RIGHT")

            def work() -> dict[str, Any]:
                cell_range.fillAuto(fill, source_count)
                return {"filled": True, "direction": direction, "source_count": source_count}
            result = self._in_undo(doc, f"Hermes: autofill {address}", begin, work)
            read = self._read_range(doc, sheet_name, address)
            read.update(result)
            return read
        return self.dispatch(write, mutation=True)

    def calc_range_stats(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address"})
        document_id, sheet_name, address = args["document_id"], args["sheet"], args["address"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")
        bounds = parse_a1(address)
        self._require((bounds[3] - bounds[1] + 1) * (bounds[2] - bounds[0] + 1) <= MAX_STATS_CELLS, "range is too large")

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            _sheet, cell_range = self._sheet_and_range(doc, sheet_name, address)
            values = [list(row) for row in cell_range.getDataArray()]
            return {"sheet": sheet_name, "address": address, "stats": self._stats(values), "document": self.document_identity(doc)}
        return self.dispatch(read)

    def calc_add_comment(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "cell", "text"})
        document_id, sheet_name, cell_address, text = args["document_id"], args["sheet"], args["cell"], args["text"]
        self._require(_is_str(sheet_name, 255) and _is_str(text, 2000), "comment arguments are invalid")
        bounds = parse_a1(cell_address)
        self._require(bounds[0] == bounds[2] and bounds[1] == bounds[3], "cell must be one cell, such as B2")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheet = self._sheet(doc, sheet_name)
            cell = sheet.getCellByPosition(bounds[0], bounds[1])

            def work() -> dict[str, Any]:
                cell.clearContents(CELL_FLAG_ANNOTATION)
                sheet.getAnnotations().insertNew(cell.getCellAddress(), text)
                return {"commented": True, "sheet": sheet_name, "cell": cell_address,
                        "comment": str(cell.getAnnotation().getString())}
            return self._in_undo(doc, f"Hermes: comment on {cell_address}", begin, work)
        return self.dispatch(write, mutation=True)

    def calc_get_comments(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet"})
        document_id, sheet_name = args["document_id"], args["sheet"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            annotations = self._sheet(doc, sheet_name).getAnnotations()
            total = int(annotations.getCount())
            comments = []
            for index in range(min(total, 500)):
                annotation = annotations.getByIndex(index)
                position = annotation.getPosition()
                text = str(annotation.getString())
                if not text:  # an .xlsx comment reads "" until LibreOffice builds its caption
                    text = str(annotation.getAnnotationShape().getString())
                comments.append({"cell": f"{self._column_label(position.Column)}{position.Row + 1}",
                                 "text": text[:2000]})
            return {"sheet": sheet_name, "count": total, "comments": comments, "truncated": total > 500,
                    "document": self.document_identity(doc)}
        return self.dispatch(read)

    def calc_clear_comments(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address"})
        document_id, sheet_name, address = args["document_id"], args["sheet"], args["address"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")
        parse_a1(address)

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            _sheet, cell_range = self._sheet_and_range(doc, sheet_name, address)

            def work() -> dict[str, Any]:
                cell_range.clearContents(CELL_FLAG_ANNOTATION)
                return {"cleared_comments": True, "sheet": sheet_name, "address": address}
            return self._in_undo(doc, f"Hermes: clear comments in {address}", begin, work)
        return self.dispatch(write, mutation=True)

    # -- Writer -----------------------------------------------------------

    def _writer_blocks(self, doc: Any) -> list[Any]:
        blocks = []
        enumeration = doc.getText().createEnumeration()
        while enumeration.hasMoreElements():
            blocks.append(enumeration.nextElement())
        return blocks

    @staticmethod
    def _is_table(block: Any) -> bool:
        try:
            return bool(block.supportsService("com.sun.star.text.TextTable"))
        except Exception:
            return False

    # -- paged reads ------------------------------------------------------

    @staticmethod
    def _table_cells(block: Any) -> list[tuple[str, str]]:
        cells = []
        for name in block.getCellNames():
            try:
                cells.append((str(name), str(block.getCellByName(name).getString())))
            except Exception:
                cells.append((str(name), ""))
        return cells

    def _table_shape(self, block: Any) -> dict[str, Any]:
        shape = {"name": str(getattr(block, "Name", ""))}
        try:
            shape.update({"rows": int(block.getRows().getCount()), "columns": int(block.getColumns().getCount())})
        except Exception:
            pass
        return shape

    def _snapshot(self, block: Any) -> tuple:
        """One block as plain data: ('table', name, cells) or ('paragraph', text)."""
        if self._is_table(block):
            return ("table", str(getattr(block, "Name", "")), self._table_cells(block))
        return ("paragraph", str(getattr(block, "String", "")))

    @staticmethod
    def _content_fingerprint(snapshots: list[tuple]) -> str:
        payload = json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:32]

    def _page_request(self, args: dict[str, Any]) -> tuple[int, int, str | None]:
        offset, limit, token = args.get("offset"), args.get("limit", MAX_PAGE_BLOCKS), args.get("continuation")
        self._require(_is_index(limit, MAX_PAGE_BLOCKS), f"limit must be 1 to {MAX_PAGE_BLOCKS}")
        if token is not None:
            match = CONTINUATION.match(token) if isinstance(token, str) else None
            self._require(match is not None and offset is None, "continuation must come from the previous page, without offset")
            return int(match.group(1)), limit, match.group(2)
        self._require(offset is None or _is_index(offset), "offset must be a 1-based block number")
        return offset or 1, limit, None

    @staticmethod
    def _cell_window(cells: list[tuple[str, str]], offset: int, chars: int | None, size: int) -> tuple[int, int, int]:
        """How many cells from ``offset`` fit MAX_TABLE_CELLS, ``chars`` characters (None: no
        limit) and ``size`` reply bytes, as (count, characters, bytes). The first cell always fits,
        so a table can always be read further."""
        count, used, used_bytes = 0, 0, 0
        for name, text in cells[offset:offset + MAX_TABLE_CELLS]:
            shown = text[:MAX_CELL_TEXT]
            cost = _json_bytes({"cell": name, "text": shown}) + 1
            if count and ((chars is not None and used + len(shown) > chars) or used_bytes + cost > size):
                break
            count, used, used_bytes = count + 1, used + len(shown), used_bytes + cost
        return count, used, used_bytes

    def _writer_page(self, doc: Any, args: dict[str, Any], text_budget: int | None) -> dict[str, Any]:
        """The requested page of blocks plus paging facts. Refuses a stale continuation.

        With a ``text_budget`` (writer_get_text) the page also stays within MAX_READ_BYTES of
        reply: a table's returned cells count against both, and a table that does not fit whole
        starts the next page, where its first cells are returned and the rest is left to
        writer_get_table (``next_cell_offset``). Each page holds at least one block.
        """
        start, limit, expected = self._page_request(args)
        blocks = self._writer_blocks(doc)
        snapshots = [self._snapshot(block) for block in blocks]
        fingerprint = self._content_fingerprint(snapshots)
        if expected is not None and expected != fingerprint:
            raise self._fault("Stale", "the document changed after the previous page was read; read it again from "
                              "the start (offset 1) so nothing is skipped or repeated", 409)
        if start > max(len(blocks), 1):
            raise self._fault("NotFound", f"block {start} does not exist (the document has {len(blocks)})", 404)
        page, used, used_bytes = [], 0, 0
        cells_left = MAX_TABLE_CELLS
        for number in range(start, len(blocks) + 1):
            if len(page) >= limit:
                break
            kind, *rest = snapshots[number - 1]
            if text_budget is None:  # writer_get_structure: no text or cells are returned
                if kind == "table" and page and len(rest[1]) > cells_left:
                    break
                page.append((number, blocks[number - 1], snapshots[number - 1], None))
                if kind == "table":
                    cells_left -= min(len(rest[1]), cells_left)
                continue
            if kind == "paragraph":
                size = len(rest[0])
                cost = 2 * _json_bytes(rest[0][:MAX_TEXT]) + 2  # in "paragraphs" and again in "text"
                if page and (used + size > text_budget or used_bytes + cost > MAX_READ_BYTES):
                    break
                page.append((number, blocks[number - 1], snapshots[number - 1], None))
                used, used_bytes = used + size, used_bytes + cost
                continue
            cells = rest[1]
            if page and len(cells) > cells_left:
                break
            shown, chars, cost = self._cell_window(cells, 0, max(text_budget - used, 0), MAX_READ_BYTES - used_bytes)
            if page and (shown < len(cells) or used + chars > text_budget or used_bytes + cost > MAX_READ_BYTES):
                break
            page.append((number, blocks[number - 1], snapshots[number - 1], shown))
            used, used_bytes = used + chars, used_bytes + cost
            cells_left -= min(len(cells), cells_left)
        end = page[-1][0] if page else start - 1
        next_offset = end + 1 if end < len(blocks) else None
        return {"page": page, "start": start, "total": len(blocks), "next_offset": next_offset,
                "continuation": f"{next_offset}.{fingerprint}" if next_offset else None, "fingerprint": fingerprint}

    def _table_payload(self, number: int, block: Any, cells: list[tuple[str, str]], offset: int, count: int) -> dict[str, Any]:
        """``count`` cells from ``offset`` (0-based); ``next_cell_offset`` is the 1-based cell
        writer_get_table continues from, or None when the last cell was returned."""
        shown = cells[offset:offset + count]
        item = {"block": number, **self._table_shape(block), "cell_count": len(cells),
                "cells": [{"cell": name, "text": text[:MAX_CELL_TEXT]} for name, text in shown]}
        item["cells_truncated"] = offset + len(shown) < len(cells)
        item["next_cell_offset"] = offset + len(shown) + 1 if item["cells_truncated"] else None
        item["text_clipped"] = any(len(text) > MAX_CELL_TEXT for _name, text in shown)
        return item

    def writer_page_text(self, doc: Any, args: dict[str, Any]) -> dict[str, Any]:
        """Paged writer_get_text body; see the bridge's writer_get_text."""
        info = self._writer_page(doc, args, MAX_TEXT)
        paragraphs, tables, clipped = [], [], []
        for number, block, snapshot, shown in info["page"]:
            if snapshot[0] == "table":
                paragraphs.append("")  # unchanged default shape: a table's own String is empty
                tables.append(self._table_payload(number, block, snapshot[2], 0, shown))
                continue
            text = snapshot[1]
            if len(text) > MAX_TEXT:
                clipped.append(number)
                text = text[:MAX_TEXT]
            paragraphs.append(text)
        complete = info["next_offset"] is None and info["start"] == 1 and not clipped and \
            not any(t["cells_truncated"] or t["text_clipped"] for t in tables)
        return {"document": self.document_identity(doc), "paragraphs": paragraphs, "text": "\n".join(paragraphs),
                "tables": tables, "start": info["start"], "returned": len(paragraphs), "total_blocks": info["total"],
                "next_offset": info["next_offset"], "continuation": info["continuation"], "clipped_blocks": clipped,
                "fingerprint": info["fingerprint"],
                "truncated": info["next_offset"] is not None or bool(clipped)
                or any(t["cells_truncated"] or t["text_clipped"] for t in tables),
                "complete_document": complete}

    def writer_get_structure(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"}, {"offset", "limit", "continuation"})
        document_id = args["document_id"]
        paging = {k: v for k, v in args.items() if k != "document_id"}
        self._page_request(paging)

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            info = self._writer_page(doc, paging, None)
            items = []
            for index, block, snapshot, _shown in info["page"]:
                if snapshot[0] == "table":
                    items.append({"paragraph": index, "type": "table", **self._table_shape(block),
                                  "cell_count": len(snapshot[2])})
                    continue
                text = snapshot[1]
                items.append({
                    "paragraph": index, "type": "paragraph",
                    "style": str(getattr(block, "ParaStyleName", "")),
                    "outline_level": int(getattr(block, "OutlineLevel", 0) or 0),
                    "preview": text[:PREVIEW], "characters": len(text),
                })
            return {"document": self.document_identity(doc), "count": info["total"], "paragraphs": items,
                    "start": info["start"], "next_offset": info["next_offset"], "continuation": info["continuation"],
                    "truncated": info["next_offset"] is not None}
        return self.dispatch(read)

    def writer_get_table(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"}, {"table", "block", "cell_offset"})
        document_id, name, number = args["document_id"], args.get("table"), args.get("block")
        offset = args.get("cell_offset", 1)
        self._require((name is None) != (number is None), "give exactly one of table (its name) or block (its number)")
        self._require((name is None or _is_str(name, 255)) and (number is None or _is_index(number))
                      and _is_index(offset, 10_000_000), "table arguments are invalid")

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            blocks = self._writer_blocks(doc)
            found = [(i, b) for i, b in enumerate(blocks, start=1) if self._is_table(b)
                     and (str(getattr(b, "Name", "")) == name if name is not None else i == number)]
            if not found:
                raise self._fault("NotFound", "that table does not exist in the document body", 404)
            index, block = found[0]
            cells = self._table_cells(block)
            count, _chars, _size = self._cell_window(cells, offset - 1, None, MAX_READ_BYTES)
            return {"document": self.document_identity(doc), "table": self._table_payload(index, block, cells, offset - 1, count)}
        return self.dispatch(read)

    def _paragraph_style_name(self, doc: Any, style: str) -> str:
        """The internal name of a paragraph style given either its internal or its displayed name.

        LibreOffice's API knows styles by internal names, which differ from the names in its
        user interface for some styles (seen live on 26.8: 'Standard' is shown as 'Default
        Paragraph Style', and 'Body Text' is not an internal name).
        """
        styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        if styles.hasByName(style):
            return style
        for name in styles.getElementNames():
            try:
                if str(styles.getByName(name).DisplayName) == style:
                    return str(name)
            except Exception:
                continue
        raise self._fault("Validation", f"paragraph style does not exist in this document: {style}", 400)

    def writer_set_paragraph_style(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "paragraph", "style"}, {"expected_text"})
        document_id, number, style = args["document_id"], args["paragraph"], args["style"]
        expected = args.get("expected_text")
        self._require(_is_index(number) and _is_str(style, 255) and (expected is None or _is_str(expected, MAX_TEXT, empty=True)),
                      "paragraph style arguments are invalid")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            blocks = self._writer_blocks(doc)
            if number > len(blocks):
                raise self._fault("NotFound", f"paragraph {number} does not exist (the document has {len(blocks)})", 404)
            block = blocks[number - 1]
            self._require(not self._is_table(block), f"paragraph {number} is a table")
            if expected is not None and str(block.String) != expected:
                raise self._fault("Stale", "the paragraph text changed since it was read", 409)
            internal = self._paragraph_style_name(doc, style)
            previous = str(block.ParaStyleName)

            def work() -> dict[str, Any]:
                block.setPropertyValue("ParaStyleName", internal)
                return {"styled": True, "paragraph": number, "previous_style": previous, "style": str(block.ParaStyleName)}
            return self._in_undo(doc, f"Hermes: style paragraph {number} as {style}", begin, work)
        return self.dispatch(write, mutation=True)

    def _paragraph_items(self, items: Any) -> list[dict[str, Any]]:
        self._require(isinstance(items, list) and 1 <= len(items) <= MAX_INSERT_PARAGRAPHS,
                      f"paragraphs must be a list of 1 to {MAX_INSERT_PARAGRAPHS} items")
        total = 0
        for item in items:
            self._require(isinstance(item, dict) and "text" in item and set(item) <= {"text", "style"},
                          "each paragraph needs text and may name a style")
            text, style = item["text"], item.get("style")
            self._require(_is_str(text, MAX_PARAGRAPH_TEXT, empty=True) and "\n" not in text and "\r" not in text,
                          "paragraph text must be one line of at most 10,000 characters")
            self._require(style is None or _is_str(style, 255), "style must be a bounded name")
            total += len(text)
        self._require(total <= MAX_TEXT, "paragraphs exceed 50,000 characters in total")
        return items

    def writer_insert_paragraphs(self, args: dict[str, Any]) -> dict[str, Any]:
        """Insert 1-50 paragraphs, each with an optional paragraph style, as ONE undo step.

        Every style and the anchor are resolved before the first change; an unknown style, a
        moved anchor or a table anchor is refused with nothing written.
        """
        self._check(args, {"document_id", "paragraphs", "position"}, {"after_paragraph", "expected_text"})
        document_id, position = args["document_id"], args["position"]
        items = self._paragraph_items(args["paragraphs"])
        after, expected = args.get("after_paragraph"), args.get("expected_text")
        self._require(position in ("start", "end", "after"), "position must be start, end or after")
        if position == "after":
            self._require(_is_index(after) and _is_str(expected, MAX_TEXT, empty=True),
                          "position after needs after_paragraph and its expected_text")
        else:
            self._require(after is None and expected is None, "after_paragraph and expected_text are only for position after")
        title = f"Hermes: insert {len(items)} Writer paragraph{'s' if len(items) > 1 else ''}"

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            styles = [self._paragraph_style_name(doc, item["style"]) if item.get("style") else None for item in items]
            blocks = self._writer_blocks(doc)
            text = doc.getText()
            empty = len(blocks) == 1 and not self._is_table(blocks[0]) and str(blocks[0].String) == ""
            if position == "after":
                if after > len(blocks):
                    raise self._fault("NotFound", f"paragraph {after} does not exist (the document has {len(blocks)})", 404)
                anchor = blocks[after - 1]
                self._require(not self._is_table(anchor), f"paragraph {after} is a table")
                if str(anchor.String) != expected:
                    raise self._fault("Stale", "the anchor paragraph changed since it was read", 409)
            elif position == "start":
                self._require(not blocks or not self._is_table(blocks[0]),
                              "the document starts with a table; insert after a paragraph instead")

            def work() -> dict[str, Any]:
                if empty:
                    cursor = text.createTextCursor()
                    cursor.gotoStart(False)
                elif position == "after":
                    cursor = text.createTextCursorByRange(anchor.getEnd())
                elif position == "end":
                    cursor = text.createTextCursorByRange(blocks[-1].getEnd())
                else:
                    cursor = text.createTextCursorByRange(blocks[0].getStart())
                for index, (item, style) in enumerate(zip(items, styles)):
                    if empty and index == 0:
                        pass  # fill the document's only, empty paragraph in place
                    elif position == "start" and not empty:
                        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                        cursor.gotoPreviousParagraph(False)
                    else:
                        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                    if item["text"]:
                        text.insertString(cursor, item["text"], False)
                    if style is not None:
                        cursor.setPropertyValue("ParaStyleName", style)
                    if position == "start" and not empty:
                        cursor.gotoNextParagraph(False)
                return {}
            result = self._in_undo(doc, title, begin, work)
            now = self._writer_blocks(doc)
            first = 1 if (empty or position == "start") else after + 1 if position == "after" else len(now) - len(items) + 1
            readback = [{"paragraph": first + i, "style": str(getattr(now[first - 1 + i], "ParaStyleName", "")),
                         "text": str(getattr(now[first - 1 + i], "String", ""))[:PREVIEW]}
                        for i in range(len(items)) if first - 1 + i < len(now)]
            verified = len(readback) == len(items) and all(
                str(getattr(now[first - 1 + i], "String", "")) == item["text"] and (styles[i] is None or readback[i]["style"] == styles[i])
                for i, item in enumerate(items))
            result.update({"inserted": len(items), "position": position, "paragraphs": readback, "verified": verified,
                           "paragraph_count": len(now)})
            return result
        return self.dispatch(write, mutation=True)

    def _overlaps(self, text: Any, a: Any, b: Any) -> bool:
        """Whether two found ranges overlap. Ranges in different texts (a table cell and the
        body) never overlap; a comparison LibreOffice refuses is treated as an overlap."""
        try:
            if a.getText() != b.getText():
                return False
            same_text = a.getText()
            # compareRegionStarts(x, y) is 1 when x starts before y, 0 when equal, -1 when after.
            a_before_b_end = same_text.compareRegionStarts(a, b.getEnd()) == 1
            b_before_a_end = same_text.compareRegionStarts(b, a.getEnd()) == 1
            return a_before_b_end and b_before_a_end
        except Exception:
            return True

    def writer_apply_edits(self, args: dict[str, Any]) -> dict[str, Any]:
        """Exact literal edits, all or nothing: every target is found before anything changes.

        Strict contract: an edit whose text is missing, or found more than once without an
        ``occurrence``, or whose chosen match overlaps another edit's, refuses the WHOLE batch
        with nothing changed. ``if_match`` (writer_get_text's ``fingerprint``) refuses a
        changed document as Stale. The batch is one undo step.
        """
        self._check(args, {"document_id", "edits"}, {"if_match"})
        document_id, edits, if_match = args["document_id"], args["edits"], args.get("if_match")
        self._require(isinstance(edits, list) and 1 <= len(edits) <= MAX_EDITS, f"edits must be 1 to {MAX_EDITS} items")
        for edit in edits:
            self._require(isinstance(edit, dict) and {"find", "replace"} <= set(edit) <= {"find", "replace", "occurrence", "match_case"},
                          "each edit needs find and replace and may give occurrence and match_case")
            self._require(_is_str(edit["find"], MAX_SEARCH) and _is_str(edit["replace"], MAX_SEARCH, empty=True)
                          and edit["find"] != edit["replace"] and "\n" not in edit["find"] + edit["replace"]
                          and (edit.get("occurrence") is None or _is_index(edit["occurrence"], 10000))
                          and isinstance(edit.get("match_case", True), bool), "an edit is invalid")
        self._require(if_match is None or (isinstance(if_match, str) and re.fullmatch(r"[0-9a-f]{32}", if_match)),
                      "if_match must be the fingerprint writer_get_text returned")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            if if_match is not None:
                current = self._content_fingerprint([self._snapshot(b) for b in self._writer_blocks(doc)])
                if current != if_match:
                    raise self._fault("Stale", "the document changed since it was read; nothing was changed", 409)
            chosen, problems, report = [], [], []
            for number, edit in enumerate(edits, start=1):
                descriptor = doc.createSearchDescriptor()
                descriptor.SearchString = edit["find"]
                descriptor.SearchCaseSensitive = edit.get("match_case", True)
                descriptor.SearchRegularExpression = False
                found = doc.findAll(descriptor)
                matches = [found.getByIndex(i) for i in range(found.getCount())] if found is not None else []
                occurrence = edit.get("occurrence")
                if not matches:
                    problems.append(f"edit {number} ({edit['find'][:40]!r}) was not found")
                elif occurrence is None and len(matches) > 1:
                    problems.append(f"edit {number} ({edit['find'][:40]!r}) matches {len(matches)} times; give occurrence 1-{len(matches)}")
                elif occurrence is not None and occurrence > len(matches):
                    problems.append(f"edit {number} ({edit['find'][:40]!r}) has only {len(matches)} match(es)")
                else:
                    target = matches[(occurrence or 1) - 1]
                    for other_number, other in chosen:
                        if self._overlaps(doc.getText(), target, other):
                            problems.append(f"edit {number} overlaps edit {other_number}")
                    chosen.append((number, target))
                report.append({"edit": number, "find": edit["find"][:200], "matches": len(matches), "occurrence": occurrence or 1})
            if problems:
                raise self._fault("Validation", "nothing was changed: " + "; ".join(problems[:10]), 409)

            def work() -> dict[str, Any]:
                for number, target in chosen:
                    target.setString(edits[number - 1]["replace"])
                return {}
            result = self._in_undo(doc, f"Hermes: apply {len(edits)} Writer edit{'s' if len(edits) > 1 else ''}", begin, work)
            for item, (number, target) in zip(report, chosen):
                item["applied"] = str(target.getString()) == edits[number - 1]["replace"]
            result.update({"applied": sum(item["applied"] for item in report), "edits": report,
                           "verified": all(item["applied"] for item in report)})
            return result
        return self.dispatch(write, mutation=True)

    def writer_find_replace(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "search", "replace"}, {"match_case", "whole_words"})
        document_id, search, replace = args["document_id"], args["search"], args["replace"]
        match_case, whole_words = args.get("match_case", False), args.get("whole_words", False)
        self._require(_is_str(search, MAX_SEARCH) and _is_str(replace, MAX_SEARCH, empty=True)
                      and isinstance(match_case, bool) and isinstance(whole_words, bool), "find and replace arguments are invalid")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            descriptor = doc.createReplaceDescriptor()
            descriptor.SearchString = search
            descriptor.ReplaceString = replace
            descriptor.SearchCaseSensitive = match_case
            descriptor.SearchWords = whole_words
            found = doc.findAll(descriptor)
            if found is None or found.getCount() == 0:
                return {"replaced": 0, "undoable": False, "document": self.document_identity(doc)}

            def work() -> dict[str, Any]:
                return {"replaced": int(doc.replaceAll(descriptor))}
            return self._in_undo(doc, "Hermes: replace in Writer document", begin, work)
        return self.dispatch(write, mutation=True)

    def _writer_selection(self, doc: Any) -> list[Any]:
        controller = doc.getCurrentController()
        selection = controller.getSelection() if controller else None
        if selection is None or not hasattr(selection, "getCount"):
            return []
        ranges = []
        for index in range(min(int(selection.getCount()), 50)):
            item = selection.getByIndex(index)
            if hasattr(item, "getString"):
                ranges.append(item)
        return ranges

    def writer_get_selection(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"})
        document_id = args["document_id"]

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            ranges = self._writer_selection(doc)
            texts = [str(item.getString())[:MAX_TEXT] for item in ranges]
            result = {"document": self.document_identity(doc), "selections": texts, "text": "\n".join(texts),
                      "has_selection": any(texts)}
            if len(ranges) == 1 and texts[0]:
                result["format"] = self._selection_format(ranges[0])
            return result
        return self.dispatch(read)

    def writer_replace_selection(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "text", "expected_text"})
        document_id, text, expected = args["document_id"], args["text"], args["expected_text"]
        self._require(_is_str(text, MAX_TEXT, empty=True) and _is_str(expected, MAX_TEXT, empty=True),
                      "text and expected_text must be bounded strings")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            ranges = self._writer_selection(doc)
            self._require(len(ranges) == 1, "the document must have exactly one text selection or cursor position")
            if str(ranges[0].getString()) != expected:
                raise self._fault("Stale", "the selection changed since it was read", 409)

            def work() -> dict[str, Any]:
                ranges[0].setString(text)
                return {"replaced_selection": True, "characters": len(text)}
            return self._in_undo(doc, "Hermes: replace Writer selection", begin, work)
        return self.dispatch(write, mutation=True)

    # -- Calc: literal import ----------------------------------------------------

    def calc_write_literal(self, args: dict[str, Any]) -> dict[str, Any]:
        """Write a matrix into EMPTY cells without interpreting it: strings go through setString
        (never a formula, never re-typed), numbers through setValue, None leaves a cell empty.
        Every destination cell must be empty or nothing is written. One undo step; the result
        reports each cell's type and value as read back."""
        self._check(args, {"document_id", "sheet", "address", "values"})
        document_id, sheet_name, address, values = args["document_id"], args["sheet"], args["address"], args["values"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")
        try:
            start_col, start_row, end_col, end_row = validate_matrix(address, values)
        except Exception as exc:
            raise self._fault("Validation", str(exc), 400) from exc
        for row in values:
            for item in row:
                self._require(item is None or (isinstance(item, str) and 0 < len(item) <= 32767)
                              or (isinstance(item, (int, float)) and not isinstance(item, bool)),
                              "literal cells are non-empty text, numbers or null")

        def cell_type(cell: Any) -> str:
            return str(getattr(cell.getType(), "value", cell.getType()))

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            sheet = sheets.getByName(sheet_name)
            cells = [[sheet.getCellByPosition(start_col + c, start_row + r) for c in range(end_col - start_col + 1)]
                     for r in range(end_row - start_row + 1)]
            filled = sum(1 for row in cells for cell in row if cell_type(cell) != "EMPTY")
            if filled:
                raise self._fault("Validation", f"{filled} destination cell(s) in {address} are not empty; nothing was "
                                  "written. Choose an empty area or clear it first", 409)

            # setString silently switches a cell to the Text number format ("@") when its text would
            # otherwise read as a number or formula ("007", "=1+1"), and LibreOffice's own undo does
            # not record that switch, so an undo left the format behind (LibreOffice 26.8).
            # Each text cell is therefore set to "@" explicitly first, through the API, which
            # LibreOffice records in this same undo step; setString then changes no format and one
            # undo restores formats and content exactly. Imported text stays literal on later edits.
            title = f"Hermes: import into {address}"
            formats = doc.getNumberFormats()
            locale = uno.createUnoStruct("com.sun.star.lang.Locale")
            text_key = formats.queryKey("@", locale, False)
            if text_key == -1:
                text_key = formats.addNew("@", locale)

            def work() -> dict[str, Any]:
                for row_cells, row_values in zip(cells, values):
                    for cell, value in zip(row_cells, row_values):
                        if isinstance(value, str):
                            cell.setPropertyValue("NumberFormat", text_key)
                            cell.setString(value)
                        elif value is not None:
                            cell.setValue(float(value))
                return {}
            result = self._in_undo(doc, title, begin, work)
            mismatches, counts = [], {"text": 0, "number": 0, "empty": 0}
            for r, (row_cells, row_values) in enumerate(zip(cells, values)):
                for c, (cell, value) in enumerate(zip(row_cells, row_values)):
                    kind = cell_type(cell)
                    if isinstance(value, str):
                        ok = kind == "TEXT" and str(cell.getString()) == value
                        counts["text"] += ok
                    elif value is None:
                        ok = kind == "EMPTY"
                        counts["empty"] += ok
                    else:
                        ok = kind == "VALUE" and float(cell.getValue()) == float(value)
                        counts["number"] += ok
                    if not ok and len(mismatches) < 20:
                        mismatches.append({"row": r + 1, "column": c + 1, "type": kind.lower()})
            result.update({"imported": True, "address": address, "rows": len(values), "columns": len(values[0]),
                           "cells": counts, "mismatches": mismatches, "verified": not mismatches})
            return result
        return self.dispatch(write, mutation=True)

    # -- Calc: sort and filter ---------------------------------------------------

    FILTER_OPERATORS = {"equals": "EQUAL", "not_equals": "NOT_EQUAL", "greater": "GREATER", "greater_equal": "GREATER_EQUAL",
                        "less": "LESS", "less_equal": "LESS_EQUAL", "contains": "CONTAINS", "not_contains": "DOES_NOT_CONTAIN",
                        "begins_with": "BEGINS_WITH", "ends_with": "ENDS_WITH", "not_empty": "NOT_EMPTY"}
    # FilterOperator2.EMPTY is not offered: on LibreOffice 26.8 it filtered exactly like NOT_EMPTY
    # (measured live), so an "empty" filter would silently show the opposite rows.

    def _table_target(self, args: dict[str, Any]) -> tuple[str, str, tuple[int, int, int, int]]:
        sheet_name, address = args["sheet"], args["address"]
        self._require(_is_str(sheet_name, 255) and isinstance(args["has_header"], bool), "sheet and has_header are required")
        try:
            bounds = parse_a1(address)
        except Exception as exc:
            raise self._fault("Validation", str(exc), 400) from exc
        data_rows = bounds[3] - bounds[1] + 1 - (1 if args["has_header"] else 0)
        self._require(data_rows >= 1, "the range needs at least one data row")
        return sheet_name, address, bounds

    def _range_column(self, bounds: tuple[int, int, int, int], column: Any) -> int:
        self._require(isinstance(column, str) and re.fullmatch(r"[A-Za-z]{1,3}", column) is not None, "column must be a letter such as B")
        index = parse_a1(f"{column}1")[0]
        self._require(bounds[0] <= index <= bounds[2], f"column {column.upper()} is outside the range")
        return index - bounds[0]

    @staticmethod
    def _range_strings(sheet: Any, bounds: tuple[int, int, int, int]) -> list[tuple[str, ...]]:
        cells = sheet.getCellRangeByPosition(*bounds)
        return [tuple(str(cells.getCellByPosition(c, r).getString()) for c in range(bounds[2] - bounds[0] + 1))
                for r in range(bounds[3] - bounds[1] + 1)]

    def calc_sort_range(self, args: dict[str, Any]) -> dict[str, Any]:
        """Sort the rows of a range by up to three columns, keeping each row together.

        LibreOffice's sort (probed live on 26.8): stable for ties; ascending puts numbers before
        text, descending text before numbers, and empty keys are last either way; formulas that refer to their own row move with it. Afterwards the
        multiset of complete rows (as displayed) must be unchanged; if a formula that refers to
        another row changed its result, the sort is undone at once and reported, not kept. That
        undo goes through libreoffice_undo's focused-window guard (LibreOffice applies parts of an
        undo through the focused window): when the document cannot be confirmed as focused, nothing
        is undone and the result says the sort is still applied.
        """
        self._check(args, {"document_id", "sheet", "address", "keys", "has_header"}, {"case_sensitive"})
        sheet_name, address, bounds = self._table_target(args)
        keys, case_sensitive = args["keys"], args.get("case_sensitive", False)
        self._require(isinstance(keys, list) and 1 <= len(keys) <= 3 and isinstance(case_sensitive, bool), "give one to three sort keys")
        fields, seen = [], set()
        for key in keys:
            self._require(isinstance(key, dict) and {"column"} <= set(key) <= {"column", "descending"}
                          and isinstance(key.get("descending", False), bool), "each key is {column, descending}")
            index = self._range_column(bounds, key["column"])
            self._require(index not in seen, "a column can be a sort key only once")
            seen.add(index)
            fields.append((index, not key.get("descending", False)))
        header = args["has_header"]

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            sheet = sheets.getByName(sheet_name)
            target = sheet.getCellRangeByPosition(*bounds)
            before = self._range_strings(sheet, bounds)
            descriptor = {p.Name: p for p in target.createSortDescriptor()}
            sort_fields = []
            for index, ascending in fields:
                field = uno.createUnoStruct("com.sun.star.table.TableSortField")
                field.Field, field.IsAscending, field.IsCaseSensitive = index, ascending, case_sensitive
                sort_fields.append(field)
            descriptor["SortFields"].Value = uno.Any("[]com.sun.star.table.TableSortField", tuple(sort_fields))
            descriptor["ContainsHeader"].Value = header
            descriptor["IsSortColumns"].Value = False
            if "BindFormatsToContent" in descriptor:
                descriptor["BindFormatsToContent"].Value = True

            def work() -> dict[str, Any]:
                uno.invoke(target, "sort", (tuple(descriptor.values()),))
                return {}
            result = self._in_undo(doc, f"Hermes: sort {address}", begin, work)
            after = self._range_strings(sheet, bounds)
            skip = 1 if header else 0
            preserved = (sorted(before[skip:]) == sorted(after[skip:])) and before[:skip] == after[:skip]
            result.update({"address": address, "rows": len(after) - skip, "keys": keys, "rows_preserved": preserved,
                           "first_key_values": [row[fields[0][0]] for row in after[skip:skip + 50]], "sorted": preserved})
            return result, (None if preserved else (before, self._undo_titles(doc.getUndoManager())))

        result, revert = self.dispatch(write, mutation=True)
        if revert is None:
            return result
        changed = "sorting changed what some rows show (formulas that refer to other rows); "
        if not result["undoable"]:
            result.update({"reverted": False, "reason": changed + "LibreOffice recorded no undo step, so the bridge could "
                           "not undo it: THE SORT IS STILL APPLIED; check the document"})
            return result
        before, titles = revert
        still = f"THE SORT IS STILL APPLIED (undo '{result['undo_title']}' with Edit > Undo in its window)"
        try:
            active = self.dispatch(lambda: self._is_active(self.resolve_document(args["document_id"], "calc")))
            active, activated = self._focus_for_undo(args["document_id"], active)
        except self._fault:
            active, activated = False, False
        if not active:
            result.update({"reverted": False, "reason": changed + "LibreOffice applies undo through the focused window and "
                           "this document could not be confirmed as the focused window, so nothing was undone: " + still})
            return result

        def undo_sort(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            undo = doc.getUndoManager()
            if self._undo_titles(undo) != titles or not self._is_active(doc):
                return {"reverted": False, "reason": changed + "the undo stack or the focused window changed before "
                        "the bridge could undo it, so nothing was undone: " + still}
            begin()
            undo.undo()
            reverted = self._range_strings(doc.getSheets().getByName(sheet_name), bounds) == before
            return {"reverted": reverted, "undoable": not reverted,
                    "reason": changed + ("the sort was undone, the range is as before" if reverted else
                                         "UNDO DID NOT RESTORE THE RANGE; check the document")}
        try:
            result.update(self.dispatch(undo_sort, mutation=True))
        except self._fault as exc:
            if exc.state == "started":
                raise  # the undo began: its outcome is unknown (ExecutionUncertain)
            result.update({"reverted": False, "reason": changed + f"the bridge could not undo it ({exc}): " + still})
        result["activated_document"] = activated
        return result

    def calc_filter_range(self, args: dict[str, Any]) -> dict[str, Any]:
        """Hide the data rows of a range that do not meet the conditions (a standard filter);
        no value changes. An empty condition list removes the filter and shows every row."""
        self._check(args, {"document_id", "sheet", "address", "conditions", "has_header"}, {"match"})
        sheet_name, address, bounds = self._table_target(args)
        conditions, match = args["conditions"], args.get("match", "all")
        self._require(isinstance(conditions, list) and len(conditions) <= 8 and match in ("all", "any"),
                      "conditions is a list of up to eight, match is all or any")
        parsed = []
        for condition in conditions:
            self._require(isinstance(condition, dict) and {"column", "operator"} <= set(condition) <= {"column", "operator", "value"},
                          "each condition is {column, operator, value}")
            operator = condition["operator"]
            self._require(operator in self.FILTER_OPERATORS, "unsupported filter operator")
            value = condition.get("value")
            if operator == "not_empty":
                self._require(value is None, f"{operator} takes no value")
            elif operator in ("greater", "greater_equal", "less", "less_equal"):
                self._require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{operator} needs a number")
            elif operator in ("equals", "not_equals"):
                self._require((isinstance(value, (int, float)) and not isinstance(value, bool)) or _is_str(value, 1000, empty=True),
                              f"{operator} needs a number or text")
            else:
                self._require(_is_str(value, 1000), f"{operator} needs text")
            parsed.append((self._range_column(bounds, condition["column"]), operator, value))
        header = args["has_header"]

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            sheet = sheets.getByName(sheet_name)
            target = sheet.getCellRangeByPosition(*bounds)
            before = self._range_strings(sheet, bounds)
            descriptor = target.createFilterDescriptor(True)
            fields = []
            for number, (index, operator, value) in enumerate(parsed):
                field = uno.createUnoStruct("com.sun.star.sheet.TableFilterField2")
                field.Field = index
                field.Operator = uno.getConstantByName("com.sun.star.sheet.FilterOperator2." + self.FILTER_OPERATORS[operator])
                if number:
                    field.Connection = uno.Enum("com.sun.star.sheet.FilterConnection", "AND" if match == "all" else "OR")
                if isinstance(value, (int, float)):
                    field.IsNumeric, field.NumericValue = True, float(value)
                elif isinstance(value, str):
                    field.IsNumeric, field.StringValue = False, value
                fields.append(field)
            descriptor.setFilterFields2(tuple(fields))
            descriptor.ContainsHeader = header

            def work() -> dict[str, Any]:
                target.filter(descriptor)
                return {}
            result = self._in_undo(doc, f"Hermes: {'filter' if parsed else 'remove filter from'} {address}", begin, work)
            rows = sheet.getRows()
            first = bounds[1] + (1 if header else 0)
            hidden = [r + 1 for r in range(first, bounds[3] + 1) if not rows.getByIndex(r).IsVisible]
            result.update({"filtered": bool(parsed), "address": address, "data_rows": bounds[3] + 1 - first,
                           "visible_rows": bounds[3] + 1 - first - len(hidden), "hidden_rows": hidden[:500],
                           "values_unchanged": self._range_strings(sheet, bounds) == before})
            return result
        return self.dispatch(write, mutation=True)

    # -- Calc: charts --------------------------------------------------------------

    CHART_TYPES = {"column": ("BarDiagram", False), "bar": ("BarDiagram", True), "line": ("LineDiagram", None),
                   "area": ("AreaDiagram", None), "pie": ("PieDiagram", None), "scatter": ("XYDiagram", None)}

    @staticmethod
    def _cell_kind(cell: Any) -> str:
        kind = cell.getType()
        return str(getattr(kind, "value", kind))

    @staticmethod
    def _a1(col: int, row: int) -> str:
        label, index = "", col + 1
        while index:
            index, rest = divmod(index - 1, 26)
            label = chr(65 + rest) + label
        return f"{label}{row + 1}"

    def _chart_record(self, doc: Any, sheet: Any, name: str) -> dict[str, Any]:
        chart = sheet.getCharts().getByName(name)
        model = chart.getEmbeddedObject()
        diagram = model.getDiagram()
        kind = str(diagram.getDiagramType()).rsplit(".", 1)[-1]
        if kind == "BarDiagram":
            kind = "bar" if bool(getattr(diagram, "Vertical", False)) else "column"
        else:
            kind = {v[0]: k for k, v in self.CHART_TYPES.items() if v[1] is None}.get(kind, kind)
        sheets = doc.getSheets()
        ranges = []
        for address in chart.getRanges():
            sheet_name = sheets.getByIndex(address.Sheet).getName()
            ranges.append(f"{sheet_name}.{self._a1(address.StartColumn, address.StartRow)}:"
                          f"{self._a1(address.EndColumn, address.EndRow)}")
        record = {"name": name, "type": kind, "title": str(model.getTitle().String) if model.HasMainTitle else "",
                  "ranges": ranges, "has_legend": bool(model.HasLegend)}
        page = sheet.getDrawPage()
        for i in range(page.getCount()):
            shape = page.getByIndex(i)
            if getattr(shape, "PersistName", None) == name:
                position, size = shape.getPosition(), shape.getSize()
                record.update({"x_cm": round(position.X / 1000, 2), "y_cm": round(position.Y / 1000, 2),
                               "width_cm": round(size.Width / 1000, 2), "height_cm": round(size.Height / 1000, 2)})
        return record

    def calc_list_charts(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet"})
        sheet_name = args["sheet"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")

        def read() -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            sheet = sheets.getByName(sheet_name)
            names = list(sheet.getCharts().getElementNames())
            return {"document": self.document_identity(doc), "sheet": sheet_name, "count": len(names),
                    "charts": [self._chart_record(doc, sheet, name) for name in names[:50]]}
        return self.dispatch(read)

    def calc_create_chart(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address", "type", "anchor_cell"},
                    {"title", "width_cm", "height_cm", "first_row_labels", "first_column_labels", "legend", "if_match"})
        sheet_name, address, kind, anchor = args["sheet"], args["address"], args["type"], args["anchor_cell"]
        title = args.get("title", "")
        width, height = args.get("width_cm", 16), args.get("height_cm", 9)
        flags = {key: args.get(key, True) for key in ("first_row_labels", "first_column_labels", "legend")}
        if_match = args.get("if_match")
        self._require(_is_str(sheet_name, 255) and kind in self.CHART_TYPES, "sheet and a supported type are required")
        self._require(isinstance(title, str) and len(title) <= 255, "title is bounded text")
        self._require(all(isinstance(v, bool) for v in flags.values()), "label and legend flags are true or false")
        self._require(all(isinstance(v, (int, float)) and not isinstance(v, bool) and 2 <= v <= 50 for v in (width, height)),
                      "width_cm and height_cm are 2 to 50")
        self._require(if_match is None or (isinstance(if_match, str) and len(if_match) == 64), "if_match is a range fingerprint")
        try:
            bounds = parse_a1(address)
            at = parse_a1(anchor)
        except Exception as exc:
            raise self._fault("Validation", str(exc), 400) from exc
        self._require(at[:2] == at[2:], "anchor_cell is a single cell")
        self._require(bounds[2] > bounds[0] or not flags["first_column_labels"], "the range needs a values column")
        self._require(bounds[3] > bounds[1] or not flags["first_row_labels"], "the range needs a values row")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            if if_match is not None and self._read_range(doc, sheet_name, address)["fingerprint"] != if_match:
                raise self._fault("Stale", "the source range changed since it was read", 409)
            sheet = sheets.getByName(sheet_name)
            source = sheet.getCellRangeByPosition(*bounds)
            first_col, first_row = (1 if flags["first_column_labels"] else 0), (1 if flags["first_row_labels"] else 0)
            numbers = sum(1 for r in range(first_row, bounds[3] - bounds[1] + 1) for c in range(first_col, bounds[2] - bounds[0] + 1)
                          if self._cell_kind(source.getCellByPosition(c, r)) in ("VALUE", "FORMULA"))
            if not numbers:
                raise self._fault("Validation", "the range has no numbers or formulas to chart; nothing was created", 400)
            charts = sheet.getCharts()
            # LibreOffice refuses a chart whose name any OLE object on ANY sheet already has
            # (ScChartsObj::addNewByName; seen live), so the name is unique in the document.
            existing = set()
            for index in range(sheets.getCount()):
                other = sheets.getByIndex(index)
                existing.update(str(n) for n in other.getCharts().getElementNames())
                page = other.getDrawPage()
                existing.update(str(getattr(page.getByIndex(i), "PersistName", "") or "") for i in range(page.getCount()))
            name = next(f"Chart {n}" for n in range(1, 10000) if f"Chart {n}" not in existing)
            position = sheet.getCellByPosition(at[0], at[1]).Position
            rect = uno.createUnoStruct("com.sun.star.awt.Rectangle")
            rect.X, rect.Y = position.X, position.Y
            rect.Width, rect.Height = int(width * 1000), int(height * 1000)
            diagram, vertical = self.CHART_TYPES[kind]

            def work() -> dict[str, Any]:
                charts.addNewByName(name, rect, (source.getRangeAddress(),), flags["first_row_labels"], flags["first_column_labels"])
                model = charts.getByName(name).getEmbeddedObject()
                model.setDiagram(model.createInstance("com.sun.star.chart." + diagram))
                if vertical is not None:
                    model.getDiagram().Vertical = vertical
                if kind == "scatter":
                    model.getDiagram().Lines = False  # points only; the default joins them (seen rendered)
                model.HasMainTitle = bool(title)
                if title:
                    model.getTitle().String = title
                model.HasLegend = flags["legend"]
                return {}
            result = self._in_undo(doc, f"Hermes: chart from {address}", begin, work)
            record = self._chart_record(doc, sheet, name)
            expected = f"{sheet_name}.{self._a1(bounds[0], bounds[1])}:{self._a1(bounds[2], bounds[3])}"
            result.update({"created": True, "chart": record,
                           "verified": record["type"] == kind and record["title"] == title and record["ranges"] == [expected]
                           and record["has_legend"] == flags["legend"]})
            return result
        return self.dispatch(write, mutation=True)

    # -- Calc: conditional formatting, validation, freeze panes, protection -------

    CF_OPERATORS = {"greater": "GREATER", "greater_equal": "GREATER_EQUAL", "less": "LESS", "less_equal": "LESS_EQUAL",
                    "equal": "EQUAL", "not_equal": "NOT_EQUAL", "between": "BETWEEN", "not_between": "NOT_BETWEEN",
                    "formula": "FORMULA"}
    CF_STYLES = ("Good", "Neutral", "Bad", "Warning", "Error", "Accent", "Accent 1", "Accent 2", "Accent 3", "Note")
    VALIDATION_OPERATORS = {"between": "BETWEEN", "not_between": "NOT_BETWEEN", "greater": "GREATER",
                            "greater_equal": "GREATER_EQUAL", "less": "LESS", "less_equal": "LESS_EQUAL", "equal": "EQUAL"}

    def _calc_sheet_range(self, doc: Any, sheet_name: str, bounds: tuple[int, int, int, int]) -> tuple[Any, Any]:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise self._fault("NotFound", "no such sheet", 404)
        sheet = sheets.getByName(sheet_name)
        return sheet, sheet.getCellRangeByPosition(*bounds)

    def _sheet_bounds(self, args: dict[str, Any]) -> tuple[str, tuple[int, int, int, int]]:
        self._require(_is_str(args.get("sheet"), 255), "sheet must be a bounded name")
        try:
            return args["sheet"], parse_a1(args["address"])
        except Exception as exc:
            raise self._fault("Validation", str(exc), 400) from exc

    @staticmethod
    def _formula_operand(value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return repr(float(value)) if isinstance(value, float) else str(value)
        return '"' + value.replace('"', '""') + '"'

    def _condition_records(self, target: Any) -> list[dict[str, Any]]:
        entries = target.ConditionalFormat
        records = []
        for i in range(entries.getCount()):
            entry = entries.getByIndex(i)
            operator = str(getattr(entry.getOperator(), "value", entry.getOperator()))
            names = {v: k for k, v in self.CF_OPERATORS.items()}
            source = entry.getSourcePosition()
            records.append({"operator": names.get(operator, operator.lower()), "value1": str(entry.getFormula1()),
                            "value2": str(entry.getFormula2()), "style": str(entry.getStyleName()),
                            "anchor": self._a1(source.Column, source.Row), "anchor_sheet": int(source.Sheet)})
        return records

    def _validation_record(self, target: Any) -> dict[str, Any]:
        rule = target.Validation
        kind = str(getattr(rule.Type, "value", rule.Type))
        record = {"type": {"ANY": "none", "LIST": "list", "WHOLE": "whole_number", "DECIMAL": "decimal"}.get(kind, kind.lower())}
        if kind != "ANY":
            operator = str(getattr(rule.Operator, "value", rule.Operator))
            record.update({"formula1": str(rule.Formula1), "formula2": str(rule.Formula2),
                           "operator": {v: k for k, v in self.VALIDATION_OPERATORS.items()}.get(operator, operator.lower()),
                           "rejects_invalid": bool(rule.ShowErrorMessage) and
                           str(getattr(rule.ErrorAlertStyle, "value", rule.ErrorAlertStyle)) == "STOP"})
        return record

    def _view(self, doc: Any) -> Any:
        controllers, enumeration = 0, doc.getControllers()
        while enumeration.hasMoreElements():
            enumeration.nextElement()
            controllers += 1
        if controllers != 1:
            raise self._fault("Validation", f"the document has {controllers} windows; freeze panes needs exactly one", 409)
        return doc.getCurrentController()

    @staticmethod
    def _frozen(view: Any, sheet_name: str) -> dict[str, Any]:
        """Frozen columns/rows of one sheet, from the window's own view settings string.

        getSplitRow() reported one row more than was frozen on LibreOffice 26.8, and the
        document-level view data carries no split state at run time; the controller's settings
        string matched the saved settings.xml exactly (all measured live), so it is parsed here.
        """
        names = list(view.getModel().getSheets().getElementNames())
        return parse_view_freeze(str(view.getViewData()), names.index(sheet_name))

    def calc_get_rules(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address"})
        sheet_name, bounds = self._sheet_bounds(args)

        def read() -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheet, target = self._calc_sheet_range(doc, sheet_name, bounds)
            result = {"document": self.document_identity(doc), "sheet": sheet_name, "address": args["address"],
                      "conditional_formats": self._condition_records(target), "validation": self._validation_record(target),
                      "sheet_protected": bool(sheet.isProtected())}
            try:
                result["freeze"] = self._frozen(self._view(doc), sheet_name)
            except Exception:
                result["freeze"] = None
            return result
        return self.dispatch(read)

    def calc_add_conditional_format(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address", "operator", "value1", "style"}, {"value2"})
        sheet_name, bounds = self._sheet_bounds(args)
        operator, value1, value2, style = args["operator"], args["value1"], args.get("value2"), args["style"]
        self._require(operator in self.CF_OPERATORS and style in self.CF_STYLES, "a supported operator and style are required")
        if operator == "formula":
            self._require(_is_str(value1, 1000) and not value1.startswith("=") and value2 is None,
                          "a formula condition is one expression without a leading =, such as B2>C2")
        else:
            for value in (value1, value2):
                self._require(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)) or _is_str(value, 255),
                              "values are numbers or short text")
            self._require(value1 is not None and ((value2 is not None) == operator.endswith("between")),
                          "between and not_between need value1 and value2; the others only value1")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            _sheet, target = self._calc_sheet_range(doc, sheet_name, bounds)
            entries = target.ConditionalFormat
            before = entries.getCount()
            self._require(before < 20, "the range already has 20 conditional formats")

            def pv(name: str, value: Any) -> Any:
                prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
                prop.Name, prop.Value = name, value
                return prop
            # Relative references in a condition are relative to SourcePosition; without it
            # LibreOffice anchors them at A1 of the first sheet, so "B2>C2" on B2:B10 compared the
            # wrong rows (seen live). The anchor is the range's top-left cell, on its own sheet.
            anchor = uno.createUnoStruct("com.sun.star.table.CellAddress")
            anchor.Sheet, anchor.Column, anchor.Row = int(target.getRangeAddress().Sheet), bounds[0], bounds[1]
            formula1 = value1 if operator == "formula" else self._formula_operand(value1)
            formula2 = self._formula_operand(value2) if value2 is not None else None
            props = [pv("Operator", uno.Enum("com.sun.star.sheet.ConditionOperator", self.CF_OPERATORS[operator])),
                     pv("Formula1", formula1), pv("StyleName", style), pv("SourcePosition", anchor)]
            if formula2 is not None:
                props.append(pv("Formula2", formula2))
            entries.addNew(tuple(props))

            def work() -> dict[str, Any]:
                target.ConditionalFormat = entries
                return {}
            result = self._in_undo(doc, f"Hermes: conditional format {args['address']}", begin, work)
            rules = self._condition_records(target)
            # The expression is read back as LibreOffice stores it relative to the anchor, so a
            # correct anchor plus an unchanged expression is the rule that was asked for.
            last = rules[-1] if len(rules) == before + 1 else {}
            result.update({"added": True, "conditional_formats": rules,
                           "verified": bool(last) and last["operator"] == operator and last["style"] == style
                           and last["value1"] == formula1 and (formula2 is None or last["value2"] == formula2)
                           and (last["anchor_sheet"], last["anchor"]) == (anchor.Sheet, self._a1(bounds[0], bounds[1]))})
            return result
        return self.dispatch(write, mutation=True)

    def calc_set_validation(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "sheet", "address", "type"},
                    {"items", "operator", "value1", "value2", "error_message", "reject_invalid"})
        sheet_name, bounds = self._sheet_bounds(args)
        kind, items = args["type"], args.get("items")
        operator, value1, value2 = args.get("operator"), args.get("value1"), args.get("value2")
        message, reject = args.get("error_message", ""), args.get("reject_invalid", True)
        self._require(kind in ("list", "whole_number", "decimal", "none"), "type is list, whole_number, decimal or none")
        self._require(isinstance(message, str) and len(message) <= 255 and isinstance(reject, bool), "error_message and reject_invalid are invalid")
        if kind == "list":
            self._require(isinstance(items, list) and 1 <= len(items) <= 100 and all(_is_str(i, 255) for i in items)
                          and operator is None and value1 is None and value2 is None, "a list needs 1 to 100 short text items only")
        elif kind == "none":
            self._require(items is None and operator is None and value1 is None and value2 is None, "type none takes no rule")
        else:
            self._require(items is None and operator in self.VALIDATION_OPERATORS, "a number rule needs an operator")
            for value in (value1, value2):
                self._require(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)), "limits are numbers")
            self._require(value1 is not None and ((value2 is not None) == operator.endswith("between")),
                          "between and not_between need value1 and value2; the others only value1")
            self._require(kind == "decimal" or all(v is None or float(v).is_integer() for v in (value1, value2)),
                          "whole_number limits are whole numbers")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            _sheet, target = self._calc_sheet_range(doc, sheet_name, bounds)
            rule = target.Validation
            rule.Type = uno.Enum("com.sun.star.sheet.ValidationType",
                                 {"list": "LIST", "whole_number": "WHOLE", "decimal": "DECIMAL", "none": "ANY"}[kind])
            formula1 = formula2 = None
            if kind == "list":
                formula1 = ";".join(self._formula_operand(item) for item in items)
                rule.Formula1 = formula1
                rule.ShowList = 1
            elif kind != "none":
                rule.Operator = uno.Enum("com.sun.star.sheet.ConditionOperator", self.VALIDATION_OPERATORS[operator])
                formula1 = self._formula_operand(value1)
                formula2 = self._formula_operand(value2) if value2 is not None else ""
                rule.Formula1, rule.Formula2 = formula1, formula2
            rule.ShowErrorMessage = kind != "none" and reject
            rule.ErrorAlertStyle = uno.Enum("com.sun.star.sheet.ValidationAlertStyle", "STOP")
            rule.ErrorMessage = message
            rule.IgnoreBlankCells = True

            def work() -> dict[str, Any]:
                target.Validation = rule
                return {}
            result = self._in_undo(doc, f"Hermes: validation {args['address']}", begin, work)
            record = self._validation_record(target)
            # Everything that was asked for must read back: the limits (Formula2 only when the
            # operator uses it), the operator for number rules, and whether bad input is rejected.
            verified = record["type"] == kind
            if kind != "none":
                verified = verified and record["formula1"] == formula1 and record["rejects_invalid"] == reject
            if kind not in ("list", "none"):
                verified = verified and record["operator"] == operator and (value2 is None or record["formula2"] == formula2)
            result.update({"validation": record, "verified": verified})
            return result
        return self.dispatch(write, mutation=True)

    def calc_freeze_panes(self, args: dict[str, Any]) -> dict[str, Any]:
        """Freeze the first columns/rows of one sheet in the document's only window (0 and 0 unfreezes).
        The view's active sheet is restored afterwards; the bridge supplies the undo step."""
        self._check(args, {"document_id", "sheet", "columns", "rows"})
        sheet_name, columns, rows = args["sheet"], args["columns"], args["rows"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")
        self._require(all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100 for v in (columns, rows)),
                      "columns and rows are 0 to 100")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            view = self._view(doc)
            previous = self._frozen(view, sheet_name)

            def freeze(cols: int, count: int) -> None:
                active = view.getActiveSheet()
                view.setActiveSheet(sheets.getByName(sheet_name))
                try:
                    view.freezeAtPosition(cols, count)
                finally:
                    view.setActiveSheet(active)

            def build() -> dict[str, Any]:
                freeze(columns, rows)
                return {}
            result = self._in_custom_undo(doc, f"Hermes: freeze panes on {sheet_name}", begin, build,
                                          lambda: freeze(previous["columns"], previous["rows"]))
            state = self._frozen(view, sheet_name)
            result.update({"sheet": sheet_name, "previous": previous, "freeze": state,
                           "verified": (state["columns"], state["rows"]) == (columns, rows)})
            return result
        return self.dispatch(write, mutation=True)

    def calc_protect_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        """Protect or unprotect a sheet WITHOUT a password (no password is ever accepted or logged).
        A sheet protected with a password by the user is never unprotected. Bridge undo step."""
        self._check(args, {"document_id", "sheet", "protect"})
        sheet_name, protect = args["sheet"], args["protect"]
        self._require(_is_str(sheet_name, 255) and isinstance(protect, bool), "sheet and protect are required")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            sheet = sheets.getByName(sheet_name)
            if bool(sheet.isProtected()) == protect:
                return {"changed": False, "protected": protect, "undoable": False, "document": self.document_identity(doc)}
            title = f"Hermes: {'protect' if protect else 'unprotect'} {sheet_name}"
            undo_manager = doc.getUndoManager()

            def apply(on: bool) -> None:
                sheet.protect("") if on else sheet.unprotect("")

            def work() -> dict[str, Any]:
                try:
                    apply(protect)
                except Exception:
                    # Only a password-free protection can be lifted with an empty password; a
                    # user's password makes LibreOffice refuse, and nothing changes. Any other
                    # failure (or one while protecting) is not a password refusal.
                    if not protect and bool(sheet.isProtected()):
                        return {"password_refused": True}
                    raise
                # LibreOffice records its own step, but undoing it left the protection unchanged
                # (measured live on 26.8). This action in the same context reverses it for real.
                undo_manager.addUndoAction(HermesUndoAction(title, lambda: apply(not protect), lambda: apply(protect)))
                return {}
            result = self._in_undo(doc, title, begin, work)
            if result.pop("password_refused", False):
                result.update({"changed": False, "protected": bool(sheet.isProtected()), "undoable": False,
                               "reason": "the sheet is protected with a password; only the user can unprotect it in LibreOffice"})
                return result
            result.update({"changed": True, "protected": bool(sheet.isProtected()),
                           "verified": bool(sheet.isProtected()) == protect})
            return result
        return self.dispatch(write, mutation=True)

    # -- Calc: tables, active sheet, recalculation -------------------------------------

    TABLE_STYLES = ("none", "Default Style", "Academic", "Box List Blue", "Box List Green", "Box List Red", "Box List Yellow",
                    "Elegant", "Financial", "Simple Grid Columns", "Simple Grid Rows", "Simple List Shaded")

    def calc_insert_table(self, args: dict[str, Any]) -> dict[str, Any]:
        """Turn a range with a header row into a named table: a LibreOffice database range with
        filter buttons and, optionally, a built-in AutoFormat style. One undo step."""
        self._check(args, {"document_id", "sheet", "address"}, {"name", "style", "filter_buttons", "if_match"})
        sheet_name, bounds = self._sheet_bounds(args)
        name, style = args.get("name"), args.get("style", "none")
        buttons, if_match = args.get("filter_buttons", True), args.get("if_match")
        self._require(name is None or (isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is not None),
                      "name starts with a letter and uses letters, digits and _ (64 max)")
        self._require(style in self.TABLE_STYLES and isinstance(buttons, bool), "a supported style is required")
        self._require(if_match is None or (isinstance(if_match, str) and len(if_match) == 64), "if_match is a range fingerprint")
        self._require(bounds[3] > bounds[1], "a table needs a header row and at least one data row")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheet, target = self._calc_sheet_range(doc, sheet_name, bounds)
            if if_match is not None and self._read_range(doc, sheet_name, args["address"])["fingerprint"] != if_match:
                raise self._fault("Stale", "the range changed since it was read", 409)
            headers = [str(target.getCellByPosition(c, 0).getString()).strip() for c in range(bounds[2] - bounds[0] + 1)]
            if not all(headers) or len({h.lower() for h in headers}) != len(headers):
                raise self._fault("Validation", "the first row must hold a distinct, non-empty label for every column", 400)
            ranges = doc.DatabaseRanges
            for existing in ranges.getElementNames():
                other = ranges.getByName(existing).DataArea
                if other.Sheet == target.getRangeAddress().Sheet and not (
                        other.EndColumn < bounds[0] or other.StartColumn > bounds[2] or other.EndRow < bounds[1] or other.StartRow > bounds[3]):
                    raise self._fault("Validation", f"the range overlaps the table {existing}", 409)
            table = name or next(f"Table{n}" for n in range(1, 10000) if not ranges.hasByName(f"Table{n}"))
            if ranges.hasByName(table):
                raise self._fault("Validation", f"a table named {table} already exists", 409)
            if style != "none":
                autoformats = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.sheet.TableAutoFormats", self.ctx)
                if not autoformats.hasByName(style):
                    raise self._fault("Validation", f"this LibreOffice has no table style named {style}; nothing was changed", 400)

            def work() -> dict[str, Any]:
                ranges.addNewByName(table, target.getRangeAddress())
                ranges.getByName(table).AutoFilter = buttons
                if style != "none":
                    target.autoFormat(style)
                return {}
            result = self._in_undo(doc, f"Hermes: table {table}", begin, work)
            made = ranges.getByName(table)
            area = made.DataArea
            result.update({"created": True, "table": {"name": table, "address": f"{self._a1(area.StartColumn, area.StartRow)}:"
                                                      f"{self._a1(area.EndColumn, area.EndRow)}", "columns": headers,
                                                      "filter_buttons": bool(made.AutoFilter), "style": style},
                           "verified": (area.StartColumn, area.StartRow, area.EndColumn, area.EndRow) == bounds
                           and bool(made.AutoFilter) == buttons})
            return result
        return self.dispatch(write, mutation=True)

    def calc_activate_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show a sheet in the named document's only window. A view change, not an edit:
        there is nothing in the document to undo, and the previous sheet is returned."""
        self._check(args, {"document_id", "sheet"})
        sheet_name = args["sheet"]
        self._require(_is_str(sheet_name, 255), "sheet must be a bounded name")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise self._fault("NotFound", "no such sheet", 404)
            view = self._view(doc)
            previous = str(view.getActiveSheet().getName())
            begin()
            view.setActiveSheet(sheets.getByName(sheet_name))
            active = str(view.getActiveSheet().getName())
            return {"active_sheet": active, "previous_sheet": previous, "verified": active == sheet_name,
                    "undoable": False, "view_change_only": True, "document": self.document_identity(doc)}
        return self.dispatch(write, mutation=True)

    def calc_recalculate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Recalculate the named document's formulas (all of them by default). Only formula
        results change, from the current inputs, so there is no undo step; returns whether
        automatic calculation is on."""
        self._check(args, {"document_id"}, {"all"})
        everything = args.get("all", True)
        self._require(isinstance(everything, bool), "all is true or false")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "calc")
            begin()
            doc.calculateAll() if everything else doc.calculate()
            return {"recalculated": "all" if everything else "dirty", "automatic_calculation": bool(doc.isAutomaticCalculationEnabled()),
                    "undoable": False, "document": self.document_identity(doc)}
        return self.dispatch(write, mutation=True)

    # -- Impress: current slide and layouts -----------------------------------------

    def impress_get_current_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"})

        def read() -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "impress")
            view = self._view(doc)
            current = view.getCurrentPage()
            selected: list[int] = []
            try:
                for page in view.getSlideSorterSelection() or ():
                    selected.append(int(page.Number))
            except Exception:
                selected = []
            number = int(current.Number)
            return {"document": self.document_identity(doc), "current_slide": number,
                    "selected_slides": sorted(selected) or [number],
                    "selection_source": "slide panel" if selected else "current slide"}
        return self.dispatch(read)

    def impress_list_layouts(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"})
        names = {v: k for k, v in CHANGE_LAYOUTS.items()}

        def read() -> dict[str, Any]:
            doc = self.resolve_document(args["document_id"], "impress")
            pages, masters = doc.getDrawPages(), doc.getMasterPages()
            usage: dict[str, list[int]] = {}
            slides = []
            for i in range(pages.getCount()):
                page = pages.getByIndex(i)
                master = str(page.MasterPage.Name)
                usage.setdefault(master, []).append(i + 1)
                slides.append({"slide": i + 1, "layout": names.get(int(page.Layout), f"other ({int(page.Layout)})"), "master": master})
            return {"document": self.document_identity(doc),
                    "layouts": [{"layout": "title", "placeholders": ["title", "subtitle"]},
                                {"layout": "title_content", "placeholders": ["title", "body"]},
                                {"layout": "title_two_content", "placeholders": ["title", "body", "body"]},
                                {"layout": "title_only", "placeholders": ["title"]},
                                {"layout": "blank", "placeholders": []}],
                    "masters": [{"name": str(masters.getByIndex(i).Name), "slides": usage.get(str(masters.getByIndex(i).Name), [])}
                                for i in range(masters.getCount())],
                    "slides": slides[:500]}
        return self.dispatch(read)

    # -- Writer: character formatting and comments --------------------------

    _CHAR_KEYS = {"bold", "italic", "underline", "strikethrough", "font_name", "font_size", "color", "highlight"}

    def _char_format(self, spec: Any) -> dict[str, Any]:
        self._require(isinstance(spec, dict) and spec and set(spec) <= self._CHAR_KEYS,
                      "format needs at least one of: " + ", ".join(sorted(self._CHAR_KEYS)))
        for key in ("bold", "italic", "underline", "strikethrough"):
            self._require(key not in spec or isinstance(spec[key], bool), f"{key} must be true or false")
        self._require("font_name" not in spec or _is_str(spec["font_name"], 255), "font_name must be a bounded name")
        size = spec.get("font_size")
        self._require(size is None or (isinstance(size, (int, float)) and not isinstance(size, bool) and 1 <= size <= 409),
                      "font_size must be 1 to 409 points")
        for key in ("color", "highlight"):
            value = spec.get(key)
            self._require(value is None or (isinstance(value, str) and (re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
                                                                        or (key == "highlight" and value == "none"))),
                          f"{key} must be #RRGGBB" + (" or none" if key == "highlight" else ""))
        return spec

    @staticmethod
    def _char_properties(spec: dict[str, Any]) -> list[tuple[str, Any]]:
        props = []
        if "bold" in spec:
            props.append(("CharWeight", 150.0 if spec["bold"] else 100.0))
        if "italic" in spec:
            props.append(("CharPosture", uno.Enum("com.sun.star.awt.FontSlant", "ITALIC" if spec["italic"] else "NONE")))
        if "underline" in spec:
            props.append(("CharUnderline", 1 if spec["underline"] else 0))
        if "strikethrough" in spec:
            props.append(("CharStrikeout", 1 if spec["strikethrough"] else 0))
        if "font_name" in spec:
            props.append(("CharFontName", spec["font_name"]))
        if "font_size" in spec:
            props.append(("CharHeight", float(spec["font_size"])))
        if "color" in spec:
            props.append(("CharColor", int(spec["color"][1:], 16)))
        if "highlight" in spec:
            props.append(("CharBackColor", -1 if spec["highlight"] == "none" else int(spec["highlight"][1:], 16)))
        return props

    @staticmethod
    def _read_char_format(target: Any) -> dict[str, Any]:
        def get(name: str) -> Any:
            try:
                return target.getPropertyValue(name)
            except Exception:
                return getattr(target, name, None)
        posture = get("CharPosture")
        back = get("CharBackColor")
        color = get("CharColor")
        return {"bold": (get("CharWeight") or 100) >= 150, "italic": str(getattr(posture, "value", posture)) == "ITALIC",
                "underline": bool(get("CharUnderline")), "strikethrough": bool(get("CharStrikeout")),
                "font_name": str(get("CharFontName") or ""), "font_size": float(get("CharHeight") or 0),
                "color": None if color in (None, -1) else f"#{int(color) & 0xFFFFFF:06X}",
                "highlight": "none" if back in (None, -1) else f"#{int(back) & 0xFFFFFF:06X}"}

    def _selection_format(self, target: Any) -> dict[str, Any]:
        """Character formatting of a selection; a property that varies inside it reads 'mixed'."""
        names = {"bold": "CharWeight", "italic": "CharPosture", "underline": "CharUnderline", "strikethrough": "CharStrikeout",
                 "font_name": "CharFontName", "font_size": "CharHeight", "color": "CharColor", "highlight": "CharBackColor"}
        values = self._read_char_format(target)
        for key, name in names.items():
            try:
                if str(getattr(target.getPropertyState(name), "value", "")) == "AMBIGUOUS_VALUE":
                    values[key] = "mixed"
            except Exception:
                continue
        return values

    def _single_selection(self, doc: Any, expected: str) -> Any:
        ranges = self._writer_selection(doc)
        self._require(len(ranges) == 1, "the document must have exactly one text selection")
        if str(ranges[0].getString()) != expected:
            raise self._fault("Stale", "the selection changed since it was read", 409)
        self._require(expected != "", "select the text first; the cursor alone has nothing to format or comment on")
        return ranges[0]

    def writer_format_selection(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "expected_text", "format"})
        document_id, expected = args["document_id"], args["expected_text"]
        self._require(_is_str(expected, MAX_TEXT, empty=True), "expected_text must be a bounded string")
        spec = self._char_format(args["format"])
        props = self._char_properties(spec)

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            target = self._single_selection(doc, expected)
            previous = self._read_char_format(target)

            def work() -> dict[str, Any]:
                for name, value in props:
                    target.setPropertyValue(name, value)
                return {}
            result = self._in_undo(doc, "Hermes: format Writer selection", begin, work)
            applied = self._read_char_format(target)
            result.update({"formatted": True, "characters": len(expected), "previous": previous, "applied": applied,
                           "verified": all(applied[key] == (value.upper() if isinstance(value, str) and value.startswith("#") else
                                                            float(value) if key == "font_size" else value)
                                           for key, value in spec.items())})
            return result
        return self.dispatch(write, mutation=True)

    def _model_range(self, selection: Any) -> Any:
        """A fresh document cursor over exactly the selected text.

        On LibreOffice 26.8 a comment inserted over the selection object itself, or over a cursor
        copied from it, fails with 'no SwTextAttr inserted?' whenever the selection covers
        formatted text, while a cursor walked to the same positions works (probed live). The
        walk compares positions with the selection instead of counting characters, so field
        placeholders and surrogate pairs cannot shift it. Refuses when the copy is not exact.
        """
        text = selection.getText()
        cursor = text.createTextCursorByRange(selection.getStart())
        cursor.gotoStartOfParagraph(False)
        steps = 0
        while text.compareRegionStarts(cursor, selection.getStart()) == 1 and steps <= MAX_TEXT + 1000:
            cursor.goRight(1, False)
            steps += 1
        target = text.createTextCursorByRange(cursor)
        while text.compareRegionEnds(target, selection.getEnd()) == 1 and steps <= 2 * MAX_TEXT + 2000:
            target.goRight(1, True)
            steps += 1
        if str(target.getString()) != str(selection.getString()):
            raise self._fault("Validation", "the selection could not be anchored exactly; nothing was changed", 400)
        return target

    def _annotations(self, doc: Any) -> list[Any]:
        found = []
        enumeration = doc.getTextFields().createEnumeration()
        while enumeration.hasMoreElements():
            field = enumeration.nextElement()
            try:
                if field.supportsService("com.sun.star.text.textfield.Annotation"):
                    found.append(field)
            except Exception:
                continue
        return found

    @staticmethod
    def _annotation_record(field: Any) -> dict[str, Any]:
        try:
            anchor = str(field.getAnchor().getString())
        except Exception:
            anchor = ""
        return {"author": str(getattr(field, "Author", ""))[:255], "text": str(getattr(field, "Content", ""))[:2000],
                "anchor_text": anchor[:PREVIEW], "anchor_characters": len(anchor)}

    def writer_get_comments(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"})
        document_id = args["document_id"]

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            fields = self._annotations(doc)
            comments, size = [], 0
            for field in fields[:500]:
                record = self._annotation_record(field)
                size += _json_bytes(record) + 1
                if comments and size > MAX_READ_BYTES:
                    break
                comments.append(record)
            return {"document": self.document_identity(doc), "count": len(fields), "returned": len(comments),
                    "comments": comments, "truncated": len(comments) < len(fields)}
        return self.dispatch(read)

    def writer_add_comment(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "expected_text", "text"}, {"author"})
        document_id, expected, note = args["document_id"], args["expected_text"], args["text"]
        author = args.get("author", "Hermes")
        self._require(_is_str(expected, MAX_TEXT, empty=True) and _is_str(note, 2000) and _is_str(author, 255),
                      "comment arguments are invalid")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            target = self._model_range(self._single_selection(doc, expected))
            before = len(self._annotations(doc))
            field = doc.createInstance("com.sun.star.text.textfield.Annotation")
            field.Author, field.Content = author, note

            def work() -> dict[str, Any]:
                # Inserting over a non-empty document cursor anchors the comment to that text and
                # keeps it (probed live on LibreOffice 26.8, with LibreOffice's own undo step).
                target.getText().insertTextContent(target, field, True)
                return {}
            result = self._in_undo(doc, "Hermes: comment on Writer selection", begin, work)
            record = self._annotation_record(field)
            result.update({"commented": True, "comment": record, "comment_count": len(self._annotations(doc)),
                           "verified": record["anchor_text"] == expected[:PREVIEW] and record["text"] == note[:2000]
                           and len(self._annotations(doc)) == before + 1})
            return result
        return self.dispatch(write, mutation=True)

    # -- Impress ----------------------------------------------------------

    def impress_get_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "slide"})
        document_id, number = args["document_id"], args["slide"]
        self._require(_is_index(number), "slide must be a 1-based slide number")

        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            shapes = []
            for index in range(min(page.getCount(), MAX_SHAPES)):
                shape = page.getByIndex(index)
                kind = str(getattr(shape, "ShapeType", ""))
                item: dict[str, Any] = {"shape": index + 1, "type": kind.rsplit(".", 1)[-1], "name": str(getattr(shape, "Name", ""))}
                if kind == TITLE_SHAPE:
                    item["placeholder"] = "title"
                elif kind in BODY_SHAPES:
                    item["placeholder"] = "body"
                text = self._shape_text(shape)
                if text is not None:
                    item["text"] = text[:10000]
                    if text:
                        try:
                            cursor = shape.getText().createTextCursor()
                            cursor.gotoStart(False)
                            cursor.gotoEnd(True)
                            item["format"] = self._selection_format(cursor)
                        except Exception:
                            pass
                try:
                    position, size = shape.getPosition(), shape.getSize()
                    item.update({"x_cm": position.X / 1000, "y_cm": position.Y / 1000,
                                 "width_cm": size.Width / 1000, "height_cm": size.Height / 1000})
                except Exception:
                    pass
                for key, name in (("fill_color", "FillColor"), ("line_color", "LineColor")):
                    try:
                        if kind.endswith(("RectangleShape", "EllipseShape", "LineShape")):
                            item[key] = f"#{int(shape.getPropertyValue(name)) & 0xFFFFFF:06X}"
                    except Exception:
                        pass
                shapes.append(item)
            return {"document": self.document_identity(doc), "slide": number, "slide_count": doc.getDrawPages().getCount(),
                    "name": str(getattr(page, "Name", "")), "layout": int(getattr(page, "Layout", -1)),
                    "width_cm": int(getattr(page, "Width", 0)) / 1000, "height_cm": int(getattr(page, "Height", 0)) / 1000,
                    "shapes": shapes, "truncated": page.getCount() > MAX_SHAPES}
        return self.dispatch(read)

    def _placeholder(self, page: Any, which: str) -> Any:
        wanted = (TITLE_SHAPE,) if which == "title" else BODY_SHAPES
        for index in range(page.getCount()):
            shape = page.getByIndex(index)
            if str(getattr(shape, "ShapeType", "")) in wanted:
                return shape
        return None

    def impress_create_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id"}, {"title", "body", "layout", "after_slide"})
        document_id = args["document_id"]
        title, body, after = args.get("title"), args.get("body"), args.get("after_slide")
        layout = args.get("layout", "title_content" if body else "title_only" if title else "blank")
        self._require((title is None or _is_str(title, 1000)) and (body is None or _is_str(body, 10000))
                      and layout in SLIDE_LAYOUTS and (after is None or _is_index(after)), "create slide arguments are invalid")
        self._require(not (title and layout == "blank") and not (body and layout in ("blank", "title_only")),
                      "the chosen layout has no placeholder for the given title or body")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            pages = doc.getDrawPages()
            count = pages.getCount()
            anchor = count if after is None else after
            if anchor > count:
                raise self._fault("NotFound", f"slide {anchor} does not exist (the presentation has {count})", 404)

            made: dict[str, Any] = {}

            def build() -> dict[str, Any]:
                page = pages.insertNewByIndex(anchor - 1)
                made["page"] = page
                page.setPropertyValue("Layout", SLIDE_LAYOUTS[layout])
                done = {"title_set": False, "body_set": False}
                for key, value in (("title", title), ("body", body)):
                    shape = self._placeholder(page, key) if value else None
                    if shape is not None:
                        shape.setString(value)
                        done[f"{key}_set"] = True
                return {"created": True, "slide": anchor + 1, "layout": layout, "slide_count": pages.getCount(), **done}
            return self._in_custom_undo(doc, f"Hermes: create slide {anchor + 1}", begin, build,
                                        lambda: pages.remove(made["page"]))
        return self.dispatch(write, mutation=True)

    def impress_delete_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "slide"})
        document_id, number = args["document_id"], args["slide"]
        self._require(_is_index(number), "slide must be a 1-based slide number")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            pages = doc.getDrawPages()
            self._require(pages.getCount() > 1, "the only slide of a presentation cannot be deleted")

            def work() -> dict[str, Any]:
                pages.remove(page)
                return {"deleted": True, "slide": number, "slide_count": pages.getCount()}
            return self._in_undo(doc, f"Hermes: delete slide {number}", begin, work)
        return self.dispatch(write, mutation=True)

    def impress_set_shape_text(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "slide", "text"}, {"shape", "placeholder"})
        document_id, number, text = args["document_id"], args["slide"], args["text"]
        shape_number, placeholder = args.get("shape"), args.get("placeholder")
        self._require(_is_index(number) and _is_str(text, 10000, empty=True), "slide and text are required")
        self._require((shape_number is None) != (placeholder is None), "give exactly one of shape or placeholder")
        self._require(placeholder in (None, "title", "body") and (shape_number is None or _is_index(shape_number, MAX_SHAPES)),
                      "shape must be a 1-based shape number, or placeholder must be title or body")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            if placeholder is not None:
                shape = self._placeholder(page, placeholder)
                if shape is None:
                    raise self._fault("NotFound", f"slide {number} has no {placeholder} placeholder", 404)
            else:
                if shape_number > page.getCount():
                    raise self._fault("NotFound", f"shape {shape_number} does not exist on slide {number}", 404)
                shape = page.getByIndex(shape_number - 1)
            previous = self._shape_text(shape)
            self._require(previous is not None, "that shape cannot hold text")

            # Impress records no undo action for text set through the API (seen live), so the
            # bridge adds its own: undo puts the previous text back as plain text.
            def build() -> dict[str, Any]:
                shape.setString(text)
                return {"text_set": True, "slide": number, "previous_text": previous[:10000], "text": str(shape.getString())[:10000]}
            return self._in_custom_undo(doc, f"Hermes: set text on slide {number}", begin, build,
                                        lambda: shape.setString(previous))
        return self.dispatch(write, mutation=True)

    def impress_add_text_box(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "slide", "text"}, {"x_cm", "y_cm", "width_cm", "height_cm"})
        document_id, number, text = args["document_id"], args["slide"], args["text"]
        self._require(_is_index(number) and _is_str(text, 10000), "slide and text are required")
        box = {}
        for key, default in (("x_cm", 2.0), ("y_cm", 4.0), ("width_cm", 12.0), ("height_cm", 2.0)):
            value = args.get(key, default)
            self._require(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 200, f"{key} is invalid")
            box[key] = int(round(float(value) * 1000))
        self._require(box["width_cm"] > 0 and box["height_cm"] > 0, "width_cm and height_cm must be positive")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)

            made: dict[str, Any] = {}

            def build() -> dict[str, Any]:
                shape = doc.createInstance("com.sun.star.drawing.TextShape")
                made["shape"] = shape
                page.add(shape)
                position = uno.createUnoStruct("com.sun.star.awt.Point")
                position.X, position.Y = box["x_cm"], box["y_cm"]
                size = uno.createUnoStruct("com.sun.star.awt.Size")
                size.Width, size.Height = box["width_cm"], box["height_cm"]
                shape.setPosition(position)
                shape.setSize(size)
                shape.setString(text)
                return {"added": True, "slide": number, "shape": page.getCount()}
            return self._in_custom_undo(doc, f"Hermes: add text box on slide {number}", begin, build,
                                        lambda: page.remove(made["shape"]))
        return self.dispatch(write, mutation=True)

    def _slide_title(self, page: Any) -> str:
        """A slide's title as impress_list_slides reads it: the first title shape holding text."""
        for index in range(page.getCount()):
            shape = page.getByIndex(index)
            if "Title" in str(getattr(shape, "ShapeType", "")):
                text = self._shape_text(shape)
                if text:
                    return text
        return ""

    def _expected_target(self, args: dict[str, Any]) -> None:
        for key in ("expected_text", "expected_title"):
            self._require(args.get(key) is None or _is_str(args[key], 10000, empty=True), f"{key} is bounded text")

    def _check_expected_title(self, page: Any, number: int, args: dict[str, Any]) -> None:
        """Refuse as Stale when slide ``number`` no longer has the title it had when it was read
        (slides may be reordered or retitled between a read and a write)."""
        expected = args.get("expected_title")
        if expected is not None and self._slide_title(page) != expected:
            raise self._fault("Stale", f"slide {number}'s title changed since it was read; read the slide again", 409)

    def _shape_target(self, page: Any, number: int, args: dict[str, Any]) -> Any:
        shape_number, placeholder = args.get("shape"), args.get("placeholder")
        if placeholder is not None:
            shape = self._placeholder(page, placeholder)
            if shape is None:
                raise self._fault("NotFound", f"slide {number} has no {placeholder} placeholder", 404)
            return shape
        if shape_number > page.getCount():
            raise self._fault("NotFound", f"shape {shape_number} does not exist on slide {number}", 404)
        return page.getByIndex(shape_number - 1)

    def impress_set_slide_layout(self, args: dict[str, Any]) -> dict[str, Any]:
        """Switch an existing slide's layout as one undo step.

        LibreOffice keeps placeholders that hold text when the new layout has no slot for them
        (probed live), so no text is lost; the result lists the shapes after the change.
        """
        self._check(args, {"document_id", "slide", "layout"}, {"expected_title"})
        document_id, number, layout = args["document_id"], args["slide"], args["layout"]
        self._require(_is_index(number) and layout in CHANGE_LAYOUTS, "slide and a supported layout are required")
        self._expected_target(args)

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            self._check_expected_title(page, number, args)
            previous = int(page.Layout)
            texts_before = sorted(t for t in (self._shape_text(page.getByIndex(i)) for i in range(page.getCount())) if t)

            if previous == CHANGE_LAYOUTS[layout]:
                return {"slide": number, "layout": layout, "previous_layout": layout, "changed": False, "undoable": False,
                        "text_preserved": True, "document": self.document_identity(doc)}

            title = f"Hermes: layout of slide {number}"
            target = CHANGE_LAYOUTS[layout]
            # Switching a placeholder between body (outline) and subtitle kinds, or changing a slide
            # whose placeholders do not match its layout number, is not restored exactly by undo
            # (measured live on 26.8), so it is refused before anything changes.
            kinds = [(str(getattr(page.getByIndex(i), "ShapeType", "")), self._shape_text(page.getByIndex(i)) or "")
                     for i in range(page.getCount())]
            outline = [text for kind, text in kinds if kind == BODY_SHAPES[0]]
            subtitle = [text for kind, text in kinds if kind == BODY_SHAPES[1]]
            if (previous == 0 and outline) or (previous in (1, 3) and subtitle):
                raise self._fault("Validation", "this slide's placeholders do not match its current layout, so a layout "
                                  "change could not be undone exactly; nothing was changed", 400)
            if (target == 0 and any(outline)) or (target in (1, 3) and any(subtitle)):
                raise self._fault("Validation", "that layout would turn the slide's filled body into a subtitle (or back), "
                                  "which undo cannot restore exactly; nothing was changed", 400)

            def work() -> dict[str, Any]:
                page.setPropertyValue("Layout", target)
                # LibreOffice's own step restores the placeholders but not the layout number, and
                # records nothing when no placeholder moves (both seen live on 26.8). This action,
                # inside the same context, sets the number back; it is undone first, then
                # LibreOffice's step restores the shapes exactly as they were.
                undo_manager.addUndoAction(HermesUndoAction(title, lambda: page.setPropertyValue("Layout", previous),
                                                            lambda: page.setPropertyValue("Layout", target)))
                return {}
            undo_manager = doc.getUndoManager()
            result = self._in_undo(doc, title, begin, work)
            texts_after = sorted(t for t in (self._shape_text(page.getByIndex(i)) for i in range(page.getCount())) if t)
            names = {v: k for k, v in CHANGE_LAYOUTS.items()}
            result.update({"slide": number, "layout": layout, "previous_layout": names.get(previous, previous), "changed": True,
                           "text_preserved": texts_before == texts_after,
                           "shapes": [{"shape": i + 1, "type": str(page.getByIndex(i).ShapeType).rsplit(".", 1)[-1]}
                                      for i in range(page.getCount())]})
            return result
        return self.dispatch(write, mutation=True)

    def _impress_text_spec(self, spec: Any) -> list[tuple[str, Any]]:
        self._require(isinstance(spec, dict) and spec and set(spec) <= IMPRESS_TEXT_KEYS,
                      "format needs at least one of: " + ", ".join(sorted(IMPRESS_TEXT_KEYS)))
        self._char_format({k: v for k, v in spec.items()})
        return self._char_properties(spec)

    def impress_format_text(self, args: dict[str, Any]) -> dict[str, Any]:
        """Format ALL the text of one shape. Refuses when a property to change is mixed inside the
        shape, because undo could not restore it exactly; undo restores the previous values (or
        the style default when the text had no direct formatting)."""
        self._check(args, {"document_id", "slide", "format"}, {"shape", "placeholder", "expected_text", "expected_title"})
        document_id, number = args["document_id"], args["slide"]
        shape_number, placeholder = args.get("shape"), args.get("placeholder")
        self._require(_is_index(number), "slide must be a 1-based slide number")
        self._require((shape_number is None) != (placeholder is None), "give exactly one of shape or placeholder")
        self._require(placeholder in (None, "title", "body") and (shape_number is None or _is_index(shape_number, MAX_SHAPES)),
                      "shape must be a 1-based shape number, or placeholder must be title or body")
        self._expected_target(args)
        props = self._impress_text_spec(args["format"])

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            self._check_expected_title(page, number, args)
            shape = self._shape_target(page, number, args)
            if args.get("expected_text") is not None and self._shape_text(shape) != args["expected_text"]:
                raise self._fault("Stale", "that shape's text changed since it was read; read the slide again", 409)
            self._require(bool(self._shape_text(shape)), "that shape holds no text to format")

            def whole() -> Any:
                cursor = shape.getText().createTextCursor()
                cursor.gotoStart(False)
                cursor.gotoEnd(True)
                return cursor
            cursor = whole()
            previous = []
            for name, _value in props:
                state = str(getattr(cursor.getPropertyState(name), "value", ""))
                if state == "AMBIGUOUS_VALUE":
                    raise self._fault("Validation", f"{name} varies inside this shape's text; formatting it would lose "
                                      "formatting that undo cannot restore", 400)
                previous.append((name, state == "DEFAULT_VALUE", cursor.getPropertyValue(name)))

            def build() -> dict[str, Any]:
                target = whole()
                for name, value in props:
                    target.setPropertyValue(name, value)
                return {}

            def restore() -> None:
                target = whole()
                for name, was_default, value in previous:
                    if was_default:
                        target.setPropertyToDefault(name)
                    # Impress text cursors may ignore setPropertyToDefault (seen live on 26.8):
                    # then the previous value is set explicitly, which looks the same.
                    if target.getPropertyValue(name) != value:
                        target.setPropertyValue(name, value)
            result = self._in_custom_undo(doc, f"Hermes: format text on slide {number}", begin, build, restore)
            applied = self._read_char_format(whole())
            result.update({"formatted": True, "slide": number, "applied": {k: applied[k] for k in args["format"]}})
            return result
        return self.dispatch(write, mutation=True)

    def impress_add_shape(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "slide", "kind", "x_cm", "y_cm", "width_cm", "height_cm"},
                    {"fill_color", "line_color", "text", "expected_title"})
        document_id, number, kind, text = args["document_id"], args["slide"], args["kind"], args.get("text")
        self._require(_is_index(number) and kind in SHAPE_KINDS, "slide and a supported kind are required")
        self._expected_target(args)
        self._require(text is None or (_is_str(text, 10000) and kind != "line"), "text is a bounded string and lines hold none")
        box = {}
        for key in ("x_cm", "y_cm", "width_cm", "height_cm"):
            value = args[key]
            self._require(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 200, f"{key} is invalid")
            box[key] = int(round(float(value) * 1000))
        self._require(box["width_cm"] > 0 and (box["height_cm"] > 0 or kind == "line"), "the shape needs a positive size")
        colors = {}
        for key, name in (("fill_color", "FillColor"), ("line_color", "LineColor")):
            if key in args:
                value = args[key]
                self._require(isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None and
                              not (key == "fill_color" and kind == "line"), f"{key} must be #RRGGBB")
                colors[name] = int(value[1:], 16)

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            page = self._slide(doc, number)
            self._check_expected_title(page, number, args)
            made: dict[str, Any] = {}

            def build() -> dict[str, Any]:
                shape = doc.createInstance("com.sun.star.drawing." + SHAPE_KINDS[kind])
                made["shape"] = shape
                page.add(shape)
                position = uno.createUnoStruct("com.sun.star.awt.Point")
                position.X, position.Y = box["x_cm"], box["y_cm"]
                size = uno.createUnoStruct("com.sun.star.awt.Size")
                size.Width, size.Height = box["width_cm"], box["height_cm"]
                shape.setPosition(position)
                shape.setSize(size)
                if kind == "rounded_rectangle":
                    shape.setPropertyValue("CornerRadius", min(box["width_cm"], box["height_cm"]) // 6)
                for name, value in colors.items():
                    shape.setPropertyValue(name, value)
                if text:
                    shape.setString(text)
                return {"added": True, "slide": number, "shape": page.getCount(), "kind": kind}
            return self._in_custom_undo(doc, f"Hermes: add {kind.replace('_', ' ')} on slide {number}", begin, build,
                                        lambda: page.remove(made["shape"]))
        return self.dispatch(write, mutation=True)

    def impress_find_replace(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check(args, {"document_id", "search", "replace"}, {"slide", "match_case"})
        document_id, search, replace = args["document_id"], args["search"], args["replace"]
        number, match_case = args.get("slide"), args.get("match_case", False)
        self._require(_is_str(search, MAX_SEARCH) and _is_str(replace, MAX_SEARCH, empty=True)
                      and isinstance(match_case, bool) and (number is None or _is_index(number)), "find and replace arguments are invalid")

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            pages = doc.getDrawPages()
            targets = [self._slide(doc, number)] if number is not None else [pages.getByIndex(i) for i in range(pages.getCount())]
            needle = search if match_case else search.lower()

            # Shapes whose text holds the search string, with that text, so undo can put it back.
            originals: list[tuple[Any, str]] = []
            changed_pages: list[Any] = []
            for page in targets:
                hit = False
                for index in range(page.getCount()):
                    shape = page.getByIndex(index)
                    text = self._shape_text(shape) or ""
                    if needle in (text if match_case else text.lower()):
                        originals.append((shape, text))
                        hit = True
                if hit:
                    changed_pages.append(page)
            if not originals:
                return {"replaced": 0, "undoable": False, "document": self.document_identity(doc)}

            def build() -> dict[str, Any]:
                total = 0
                for page in changed_pages:
                    descriptor = page.createReplaceDescriptor()
                    descriptor.setSearchString(search)
                    descriptor.setReplaceString(replace)
                    descriptor.setPropertyValue("SearchCaseSensitive", match_case)
                    total += int(page.replaceAll(descriptor))
                return {"replaced": total, "slides_changed": len(changed_pages)}

            def restore() -> None:
                for shape, text in originals:
                    shape.setString(text)
            return self._in_custom_undo(doc, "Hermes: replace in presentation", begin, build, restore)
        return self.dispatch(write, mutation=True)


def parse_view_freeze(data: str, sheet_index: int) -> dict[str, Any]:
    """Freeze state of one sheet from a Calc controller's view settings string.

    Format (LibreOffice ScViewData user data): "zoom;tab;tw:<width>;<sheet 0>;<sheet 1>..."; each
    sheet part is "curX/curY/hMode/vMode/hPos/vPos/..." ("+" may replace "/"), mode 2 = frozen.
    Anything unexpected raises, so callers fail closed instead of guessing.
    """
    parts = data.split(";")
    starts = [i for i, part in enumerate(parts) if part.startswith("tw:")]
    if len(starts) != 1 or starts[0] + 1 + sheet_index >= len(parts):
        raise ValueError("unrecognised view settings")
    fields = re.split(r"[/+]", parts[starts[0] + 1 + sheet_index])
    if len(fields) < 6 or not all(f.lstrip("-").isdigit() for f in fields[:6]):
        raise ValueError("unrecognised sheet view settings")
    h_mode, v_mode, h_pos, v_pos = (int(f) for f in fields[2:6])
    columns, rows = (h_pos if h_mode == 2 else 0), (v_pos if v_mode == 2 else 0)
    return {"frozen": bool(columns or rows), "columns": columns, "rows": rows}


EXTRA_OPERATIONS = (
    "calc_rename_sheet", "calc_copy_sheet", "calc_delete_sheet", "calc_find_replace", "calc_autofill",
    "calc_range_stats", "calc_add_comment", "calc_get_comments", "calc_clear_comments",
    "writer_get_structure", "writer_get_table", "writer_insert_paragraphs", "writer_apply_edits", "writer_set_paragraph_style", "writer_find_replace",
    "calc_sort_range", "calc_filter_range", "calc_create_chart", "calc_list_charts", "calc_get_rules",
    "calc_add_conditional_format", "calc_set_validation", "calc_freeze_panes", "calc_protect_sheet",
    "calc_insert_table", "calc_activate_sheet", "calc_recalculate", "impress_get_current_slide", "impress_list_layouts", "writer_get_selection", "writer_replace_selection", "writer_format_selection",
    "writer_get_comments", "writer_add_comment",
    "impress_get_slide", "impress_create_slide", "impress_delete_slide", "impress_set_shape_text",
    "impress_add_text_box", "impress_find_replace", "impress_set_slide_layout", "impress_format_text", "impress_add_shape",
)

# Bridge operations reached only through a plugin tool that prepares their input
# (calc_import_csv parses CSV in the plugin, then sends calc_write_literal).
INTERNAL_OPERATIONS = ("calc_write_literal",)
