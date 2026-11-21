# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""In-process bridge for explicitly bound LibreOffice documents."""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import stat
import sys
import threading
import time
from typing import Any, Callable

import uno
import unohelper
from com.sun.star.awt import XCallback
from com.sun.star.datatransfer import XTransferable
from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.task import XJob

# LibreOffice loads Python components by URI and does not guarantee that the
# component's sibling directory is importable. Keep the pure helper beside the
# component and add only that extension-owned directory.
_COMPONENT_DIR = str(Path(__file__).resolve().parent)
if _COMPONENT_DIR not in sys.path:
    sys.path.insert(0, _COMPONENT_DIR)
from range_logic import RangeValidationError, parse_a1, validate_matrix
from office_ops import EXTRA_OPERATIONS, INTERNAL_OPERATIONS, ExtraOps
import private_files

IMPLEMENTATION_NAME = "com.atlasomnia.sidecar.libreoffice.Job"
SERVICE_NAMES = ("com.sun.star.task.Job", "com.sun.star.frame.ProtocolHandler")
COPY_TOKEN_URL = "vnd.atlasomnia.sidecar:copy-token"
OPEN_CHAT_URL = "vnd.atlasomnia.sidecar:open-chat"
DEFAULT_PORT = 8660
MAX_BODY = 1024 * 1024
# Listener resource bounds: at most MAX_HANDLERS connections are served at once (checked before a
# worker thread exists; more are refused at once), and a request's headers and body must arrive
# within ONE READ_DEADLINE_SECONDS deadline, so a slow or silent local client cannot pin threads.
MAX_HANDLERS = 16
READ_DEADLINE_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 30.0
CALLBACK_TIMEOUT = 8.0
# LibreOffice applies parts of an undo through the ACTIVE view, not the document the step
# belongs to. Observed live on LibreOffice 26.8 with calc-b in front: undoing calc-a's
# "create sheet" left calc-a's sheet in place, and undoing a calc-a plan that wrote a formula on
# its third sheet added an empty sheet to calc-b. The bridge therefore undoes only while the
# bound document is LibreOffice's focused view (tracked from OnFocus/OnUnfocus events), activating
# it first when needed, and otherwise refuses without undoing anything.
PLAN_UNDO_TITLE = "Hermes: execute workbook plan"
PLAN_WITH_SHEETS_UNDO_TITLE = "Hermes: execute workbook plan (adds sheets)"
ACTIVATION_WAIT_SECONDS = 2.0
# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK (a UNO constants group, value 0).
PARAGRAPH_BREAK = 0
# Every undo context the bridge opens is titled with this prefix; the plugin's undo tool only
# undoes steps that carry it, never the user's own edits.
HERMES_UNDO_PREFIX = "Hermes:"
# macOS lost wakeup (observed live on LibreOffice 26.8). An AsyncCallback is a VCL user
# event plus one YieldWakeupEvent posted through the main dispatch queue. With LibreOffice hidden,
# bridge undo (whose main-thread calls follow each other immediately) timed out in 21 of 30 live
# iterations: the call stayed queued while the main thread slept in nextEventMatchingMask, and any
# later post released it at once. Best-supported reading of AquaSalInstance::DoYield: it drains
# user events, then polls native events without blocking and drops a wakeup that arrives in
# between, then blocks with the user event still queued. Visible windows hide this because caret
# and repaint timers wake the loop again; a hidden LibreOffice has no such timers. So while a call
# has not started, the bridge re-posts a no-op wakeup at this interval.
WAKEUP_REPOST_SECONDS = 0.2
_SERVER_LOCK = threading.Lock()
_SERVER = None


def open_sidecar_deck(controller: Any) -> bool:
    """Show a window's sidebar on the Hermes deck; ``False`` when the deck is not registered.

    Activating a deck that is already the active one collapses the sidebar (seen live on
    LibreOffice 26.8), so an active deck is only expanded, never activated again.
    """
    sidebar = controller.getSidebar()
    sidebar.setVisible(True)
    decks = sidebar.getDecks()
    if not decks.hasByName("SidecarDeck"):
        return False
    deck = decks.getByName("SidecarDeck")
    if deck.isActive():
        sidebar.showDecks(True)
    else:
        deck.activate(True)
    return True


class BridgeFault(RuntimeError):
    def __init__(self, category: str, message: str, status: int = 400, state: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status = status
        self.state = state


_VALIDATION_ERRORS = (RangeValidationError, KeyError, ValueError, TypeError)


class MainThreadCall(unohelper.Base, XCallback):
    """One main-thread operation with an atomic queued/started/cancelled state.

    A timed-out call that LibreOffice has not started yet is cancelled under the
    lock, so the queued callback becomes a no-op and can never mutate later.
    Mutating callables receive ``begin_mutation`` and must call it immediately
    before their first document change; failures before that point are not
    reported as uncertain.
    """

    def __init__(self, fn: Callable[..., Any], *, mutation: bool = False) -> None:
        self.fn = fn
        self.mutation = mutation
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = "queued"
        self.mutation_started = False
        self.result = None
        self.error = None

    @property
    def started(self) -> bool:
        return self.state in ("started", "done")

    def begin_mutation(self) -> None:
        self.mutation_started = True

    def try_cancel(self) -> bool:
        with self.lock:
            if self.state == "queued":
                self.state = "cancelled"
                return True
            return False

    def notify(self, _data: Any) -> None:
        with self.lock:
            if self.state != "queued":
                return
            self.state = "started"
        try:
            self.result = self.fn(self.begin_mutation) if self.mutation else self.fn()
        except BaseException as exc:
            self.error = exc
        finally:
            with self.lock:
                self.state = "done"
            self.event.set()


class MainThreadWakeup(unohelper.Base, XCallback):
    """A no-op main-thread callback. Posting it only wakes LibreOffice's event loop, which then
    also runs any bridge call still queued behind a lost wakeup (see WAKEUP_REPOST_SECONDS)."""

    def notify(self, _data: Any) -> None:
        return None


class FocusState:
    """The document LibreOffice last reported as focused (OnFocus), or None when unknown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._focused: Any = None

    def on_event(self, name: str, source: Any) -> None:
        with self._lock:
            if name == "OnFocus":
                self._focused = source
            elif name in ("OnUnfocus", "OnUnload", "OnPrepareUnload") and self._focused is not None:
                try:
                    same = bool(self._focused == source)
                except Exception:
                    same = True
                if same:
                    self._focused = None

    def is_focused(self, doc: Any) -> bool:
        with self._lock:
            focused = self._focused
        if focused is None:
            return False
        try:
            return bool(focused == doc)
        except Exception:
            return False


def _install_focus_listener(ctx: Any, state: FocusState, logger: logging.Logger) -> Any:
    try:
        from com.sun.star.document import XDocumentEventListener

        class FocusListener(unohelper.Base, XDocumentEventListener):
            def documentEventOccured(self, event: Any) -> None:  # UNO's spelling
                state.on_event(str(getattr(event, "EventName", "")), getattr(event, "Source", None))

            def disposing(self, _source: Any) -> None:
                return None

        listener = FocusListener()
        broadcaster = ctx.getValueByName("/singletons/com.sun.star.frame.theGlobalEventBroadcaster")
        broadcaster.addDocumentEventListener(listener)
        return listener
    except Exception as exc:  # undo then fails closed: no document is ever considered focused
        logger.error("focus tracking unavailable; bridge undo will refuse: %s", type(exc).__name__)
        return None


class BridgeRuntime(ExtraOps):
    _fault = BridgeFault

    def __init__(self, ctx: Any, async_callback: Any, token: str, port: int, logger: logging.Logger) -> None:
        self.ctx = ctx
        self.async_callback = async_callback
        self.token = token
        self.port = port
        self.logger = logger
        self.desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        self.session_nonce = secrets.token_hex(4)
        self._documents: dict[str, Any] = {}
        self.focus = FocusState()
        self._focus_listener = _install_focus_listener(ctx, self.focus, logger)

    def _wait_with_wakeups(self, call: MainThreadCall) -> bool:
        """Wait up to CALLBACK_TIMEOUT for ``call`` to finish, re-posting a no-op wakeup every
        WAKEUP_REPOST_SECONDS while it has not started. Returns whether it finished."""
        started_at = time.monotonic()
        deadline = started_at + CALLBACK_TIMEOUT
        reposts = 0
        while not call.started:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if call.event.wait(min(WAKEUP_REPOST_SECONDS, remaining)) or call.started:
                break
            try:
                self.async_callback.addCallback(MainThreadWakeup(), None)
            except Exception as exc:  # keep waiting on the original post; never fail the call here
                self.logger.error("wakeup re-post failed: %s", type(exc).__name__)
                break
            reposts += 1
        if reposts:
            self.logger.info(
                "main-thread call needed %d wakeup re-post(s); started=%s after %.2fs",
                reposts, call.started, time.monotonic() - started_at,
            )
        return call.event.wait(max(0.0, deadline - time.monotonic()))

    def dispatch(self, fn: Callable[..., Any], *, mutation: bool = False) -> Any:
        """Run ``fn`` on the LibreOffice main thread and classify the outcome.

        Categories: a call cancelled before it started is ``Timeout`` with
        state ``not_started`` (it will never run). Reads never become
        ``ExecutionUncertain``. A mutation is ``ExecutionUncertain`` only when
        its outcome is unknown: it started and timed out, or it failed after
        ``begin_mutation`` was called.
        """
        call = MainThreadCall(fn, mutation=mutation)
        self.async_callback.addCallback(call, None)
        if not self._wait_with_wakeups(call):
            if call.try_cancel():
                raise BridgeFault(
                    "Timeout", "LibreOffice was busy; the operation was cancelled before it started",
                    504, state="not_started",
                )
            if mutation:
                raise BridgeFault(
                    "ExecutionUncertain",
                    "LibreOffice mutation started but did not finish in time; it was not retried",
                    504, state="started",
                )
            raise BridgeFault("Timeout", "LibreOffice read timed out", 504, state="started")
        error = call.error
        if error is None:
            return call.result
        if mutation and call.mutation_started:
            self.logger.error("mutation failed after it started: %s: %s", type(error).__name__, error)
            if isinstance(error, BridgeFault) and error.category == "ExecutionUncertain":
                error.state = "started"
                raise error  # an operation's own account of a part-way failure (e.g. compensated: true/false)
            raise BridgeFault(
                "ExecutionUncertain",
                "LibreOffice mutation failed after it started; check the document (Edit > Undo lists Hermes steps)",
                500, state="started",
            )
        not_started = "not_started" if mutation else None
        if isinstance(error, BridgeFault):
            if error.state is None and not_started:
                error.state = not_started
            raise error
        if isinstance(error, _VALIDATION_ERRORS):
            raise BridgeFault("Validation", str(error), 400, state=not_started)
        self.logger.error("main-thread operation failed: %s: %s", type(error).__name__, error)
        raise BridgeFault("Unavailable", "LibreOffice could not perform the operation", 503, state=not_started)

    @staticmethod
    def document_type(doc: Any) -> str | None:
        if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            return "calc"
        if doc.supportsService("com.sun.star.text.TextDocument"):
            return "writer"
        if doc.supportsService("com.sun.star.presentation.PresentationDocument"):
            return "impress"
        return None

    def _runtime_uid(self, doc: Any) -> str | None:
        try:
            value = doc.getPropertyValue("RuntimeUID")
        except Exception:
            value = getattr(doc, "RuntimeUID", None)
        return str(value) if value else None

    def _id_for_document(self, doc: Any) -> str:
        uid = self._runtime_uid(doc)
        if uid:
            document_id = f"lo:{self.session_nonce}:{uid}"
        else:
            for document_id, existing in self._documents.items():
                try:
                    if existing == doc:
                        return document_id
                except Exception:
                    continue
            document_id = f"lo:{self.session_nonce}:fallback-{len(self._documents) + 1}"
        self._documents[document_id] = doc
        return document_id

    def _enumerate_documents(self) -> list[Any]:
        documents = []
        enumeration = self.desktop.getComponents().createEnumeration()
        while enumeration.hasMoreElements():
            component = enumeration.nextElement()
            try:
                if self.document_type(component) is not None:
                    documents.append(component)
            except Exception:
                continue
        return documents

    def resolve_document(self, document_id: str, expected_type: str | None = None) -> Any:
        if not isinstance(document_id, str) or not document_id or len(document_id) > 512:
            raise BridgeFault("Validation", "document_id must be a non-empty bounded string", 400)
        live: dict[str, Any] = {}
        for doc in self._enumerate_documents():
            live[self._id_for_document(doc)] = doc
        self._documents = live
        doc = live.get(document_id)
        if doc is None:
            raise BridgeFault("NotFound", "LibreOffice document was not found", 404)
        actual_type = self.document_type(doc)
        if expected_type is not None and actual_type != expected_type:
            raise BridgeFault("Validation", f"tool requires a {expected_type} document", 400)
        return doc

    def document_identity(self, doc: Any) -> dict[str, Any]:
        title = getattr(doc, "Title", "") or getattr(doc, "getTitle", lambda: "")()
        url = getattr(doc, "URL", "") or getattr(doc, "getURL", lambda: "")()
        return {
            "document_id": self._id_for_document(doc),
            "type": self.document_type(doc),
            "title": str(title),
            "url": str(url),
        }

    @staticmethod
    def canonical_fingerprint(values: list, formulas: list) -> str:
        payload = json.dumps(
            {"values": values, "formulas": formulas},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _read_range(self, doc: Any, sheet_name: str, address: str) -> dict[str, Any]:
        bounds = parse_a1(address)
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise BridgeFault("NotFound", f"Calc sheet does not exist: {sheet_name}", 404)
        sheet = sheets.getByName(sheet_name)
        cell_range = sheet.getCellRangeByPosition(*bounds)
        all_values = [list(row) for row in cell_range.getDataArray()]
        all_formulas = [list(row) for row in cell_range.getFormulaArray()]
        total_rows = bounds[3] - bounds[1] + 1
        total_cols = bounds[2] - bounds[0] + 1
        display_cols = min(total_cols, 5000)
        display_rows = min(total_rows, max(1, 5000 // display_cols))
        values = [row[:display_cols] for row in all_values[:display_rows]]
        formulas = [row[:display_cols] for row in all_formulas[:display_rows]]
        number_formats = self._number_formats(doc, cell_range, display_rows, display_cols)
        return {
            "sheet": sheet_name,
            "address": address,
            "values": values,
            "formulas": formulas,
            "number_formats": number_formats,
            "truncated": display_rows * display_cols < total_rows * total_cols,
            "cell_count": total_rows * total_cols,
            # Covers the whole address (at most 10,000 cells), including what a truncated read
            # does not return, so if_match sees a change anywhere in the range.
            "fingerprint": self.canonical_fingerprint(all_values, all_formulas),
            "document": self.document_identity(doc),
        }

    def get_range(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id = args.get("document_id")
        sheet_name = args.get("sheet")
        address = args.get("address")
        if not isinstance(document_id, str) or not document_id:
            raise BridgeFault("Validation", "document_id is required", 400)
        if not isinstance(sheet_name, str) or not sheet_name or len(sheet_name) > 255:
            raise BridgeFault("Validation", "sheet must be a non-empty bounded string", 400)
        if not isinstance(address, str):
            raise BridgeFault("Validation", "address must be a string", 400)
        parse_a1(address)
        return self.dispatch(
            lambda: self._read_range(self.resolve_document(document_id, "calc"), sheet_name, address)
        )

    @staticmethod
    def _set_cell(cell: Any, value: Any) -> None:
        if isinstance(value, str) and value.startswith("="):
            cell.setFormula(value)
        elif value is None:
            cell.setString("")
        elif isinstance(value, bool):
            cell.setValue(1.0 if value else 0.0)
        elif isinstance(value, (int, float)):
            cell.setValue(float(value))
        else:
            cell.setString(value)

    @staticmethod
    def _color(value: str) -> int:
        if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
            raise BridgeFault("Validation", "colors must use #RRGGBB", 400)
        try:
            return int(value[1:], 16)
        except ValueError as exc:
            raise BridgeFault("Validation", "colors must use #RRGGBB", 400) from exc

    @staticmethod
    def _format_keys() -> set[str]:
        return {
            "bold", "italic", "fontColor", "fillColor", "numberFormat", "borders",
            "horizontalAlignment", "verticalAlignment", "fontName", "fontSize",
            "underline", "wrapText", "columnAutofit",
        }

    def _validate_format(self, spec: Any) -> None:
        if not isinstance(spec, dict) or not spec:
            raise BridgeFault("Validation", "format must be a non-empty object", 400)
        unsupported = sorted(set(spec) - self._format_keys())
        if unsupported:
            raise BridgeFault("Validation", f"unsupported format keys: {', '.join(unsupported)}", 400)
        for key in ("bold", "italic", "borders", "wrapText", "columnAutofit"):
            if key in spec and not isinstance(spec[key], bool):
                raise BridgeFault("Validation", f"{key} must be boolean", 400)
        for key in ("fontColor", "fillColor"):
            if key in spec:
                self._color(spec[key])
        for key in ("numberFormat", "fontName"):
            if key in spec and (not isinstance(spec[key], str) or not spec[key]):
                raise BridgeFault("Validation", f"{key} must be a non-empty string", 400)
        if "fontSize" in spec and (
            not isinstance(spec["fontSize"], (int, float)) or isinstance(spec["fontSize"], bool)
            or not 0 < spec["fontSize"] <= 409
        ):
            raise BridgeFault("Validation", "fontSize must be between 0 and 409", 400)
        if spec.get("horizontalAlignment") not in (None, "General", "Left", "Center", "Right", "Fill", "Justify"):
            raise BridgeFault("Validation", "unsupported horizontalAlignment", 400)
        if spec.get("verticalAlignment") not in (None, "Top", "Center", "Bottom", "Standard"):
            raise BridgeFault("Validation", "unsupported verticalAlignment", 400)
        if spec.get("underline") not in (None, "None", "Single", "Double"):
            raise BridgeFault("Validation", "unsupported underline", 400)

    def _apply_format(self, doc: Any, cell_range: Any, spec: dict[str, Any]) -> None:
        self._validate_format(spec)
        if "bold" in spec:
            cell_range.CharWeight = 150.0 if spec["bold"] else 100.0
        if "italic" in spec:
            # FontSlant is an enum, not a constant group (getConstantByName always failed).
            cell_range.CharPosture = uno.Enum("com.sun.star.awt.FontSlant", "ITALIC" if spec["italic"] else "NONE")
        if "fontColor" in spec:
            cell_range.CharColor = self._color(spec["fontColor"])
        if "fillColor" in spec:
            cell_range.CellBackColor = self._color(spec["fillColor"])
        if "fontName" in spec:
            cell_range.CharFontName = spec["fontName"]
        if "fontSize" in spec:
            cell_range.CharHeight = float(spec["fontSize"])
        if "wrapText" in spec:
            cell_range.IsTextWrapped = spec["wrapText"]
        if "underline" in spec:
            cell_range.CharUnderline = {
                "None": 0, "Single": 1, "Double": 2,
            }[spec["underline"]]
        if "horizontalAlignment" in spec:
            cell_range.HoriJustify = {
                "General": 0, "Left": 1, "Center": 2, "Right": 3, "Fill": 4, "Justify": 5,
            }[spec["horizontalAlignment"]]
        if "verticalAlignment" in spec:
            cell_range.VertJustify = {"Standard": 0, "Top": 1, "Center": 2, "Bottom": 3}[spec["verticalAlignment"]]
        if "numberFormat" in spec:
            # Codes are English notation, converted to the document's locale: parsed in a
            # decimal-comma locale, "0.00" became "000". addNewConverted returns the existing
            # key when the converted code is already known (measured live on 26.8).
            english = uno.createUnoStruct("com.sun.star.lang.Locale")
            english.Language, english.Country = "en", "US"
            cell_range.NumberFormat = doc.getNumberFormats().addNewConverted(spec["numberFormat"], english, doc.CharLocale)
        if "borders" in spec:
            line = uno.createUnoStruct("com.sun.star.table.BorderLine2")
            line.Color = 0
            line.LineWidth = 20 if spec["borders"] else 0
            line.OuterLineWidth = 20 if spec["borders"] else 0
            for name in ("TopBorder", "BottomBorder", "LeftBorder", "RightBorder"):
                setattr(cell_range, name, line)
        if spec.get("columnAutofit"):
            cell_range.getColumns().OptimalWidth = True

    def _sheet_and_range(self, doc: Any, sheet_name: str, address: str) -> tuple[Any, Any]:
        bounds = parse_a1(address)
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise BridgeFault("NotFound", f"Calc sheet does not exist: {sheet_name}", 404)
        sheet = sheets.getByName(sheet_name)
        return sheet, sheet.getCellRangeByPosition(*bounds)

    def _write_values(self, sheet: Any, bounds: tuple[int, int, int, int], values: list[list[Any]]) -> None:
        start_col, start_row, _end_col, _end_row = bounds
        for row_offset, row in enumerate(values):
            for col_offset, value in enumerate(row):
                self._set_cell(sheet.getCellByPosition(start_col + col_offset, start_row + row_offset), value)

    def _number_formats(self, doc: Any, cell_range: Any, rows: int, cols: int) -> list[list[str]]:
        formats = doc.getNumberFormats()
        result = []
        for row in range(rows):
            output_row = []
            for col in range(cols):
                key = cell_range.getCellByPosition(col, row).NumberFormat
                try:
                    output_row.append(str(formats.getByKey(key).FormatString))
                except Exception:
                    output_row.append(str(key))
            result.append(output_row)
        return result

    def _is_frontmost(self, doc: Any, current: Any) -> bool:
        """Report (never use) which document is in front, for binding evidence."""
        if current is None:
            return False
        try:
            return bool(current == doc)
        except Exception:
            return False

    def _current_component(self) -> Any:
        try:
            return self.desktop.getCurrentComponent()
        except Exception:
            return None

    def get_overview(self) -> dict[str, Any]:
        def inspect() -> dict[str, Any]:
            documents = []
            current = self._current_component()
            for doc in self._enumerate_documents():
                item = self.document_identity(doc)
                item["frontmost"] = self._is_frontmost(doc, current)
                if item["type"] == "calc":
                    sheets = doc.getSheets()
                    names = list(sheets.getElementNames())
                    controller = doc.getCurrentController()
                    active = str(controller.getActiveSheet().Name) if controller else ""
                    sheet_rows = []
                    for name in names:
                        sheet = sheets.getByName(name)
                        cursor = sheet.createCursor()
                        cursor.gotoEndOfUsedArea(True)
                        address = cursor.getRangeAddress()
                        used = f"A1:{self._column_label(address.EndColumn)}{address.EndRow + 1}"
                        sheet_rows.append({"name": name, "used_range": used})
                    selection = ""
                    try:
                        selected = controller.getSelection().getRangeAddress()
                        selection = f"{self._column_label(selected.StartColumn)}{selected.StartRow + 1}:{self._column_label(selected.EndColumn)}{selected.EndRow + 1}"
                    except Exception:
                        pass
                    item.update({"sheets": sheet_rows, "active_sheet": active, "selection": selection})
                documents.append(item)
            return {"documents": documents}
        return self.dispatch(inspect)

    @staticmethod
    def _column_label(index: int) -> str:
        label = ""
        value = index + 1
        while value:
            value, remainder = divmod(value - 1, 26)
            label = chr(65 + remainder) + label
        return label

    def format_range(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id, sheet_name, address, spec = (
            args.get("document_id"), args.get("sheet"), args.get("address"), args.get("format")
        )
        if not all(isinstance(value, str) and value for value in (document_id, sheet_name, address)):
            raise BridgeFault("Validation", "document_id, sheet, and address are required", 400)
        parse_a1(address)
        self._validate_format(spec)
        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            _sheet, cell_range = self._sheet_and_range(doc, sheet_name, address)
            undo = doc.getUndoManager()
            begin()
            undo.enterUndoContext(f"Hermes: format {address}")
            try:
                self._apply_format(doc, cell_range, spec)
            finally:
                undo.leaveUndoContext()
            return {"formatted": True, "document_id": document_id, "sheet": sheet_name, "address": address}
        return self.dispatch(write, mutation=True)

    def clear_range(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id, sheet_name, address = args.get("document_id"), args.get("sheet"), args.get("address")
        if not all(isinstance(value, str) and value for value in (document_id, sheet_name, address)):
            raise BridgeFault("Validation", "document_id, sheet, and address are required", 400)
        parse_a1(address)
        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            _sheet, cell_range = self._sheet_and_range(doc, sheet_name, address)
            undo = doc.getUndoManager()
            begin()
            undo.enterUndoContext(f"Hermes: clear {address}")
            try:
                cell_range.clearContents(1023)
            finally:
                undo.leaveUndoContext()
            return {"cleared": True, "document_id": document_id, "sheet": sheet_name, "address": address}
        return self.dispatch(write, mutation=True)

    def create_sheet(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id, name = args.get("document_id"), args.get("name")
        if not isinstance(document_id, str) or not document_id or not isinstance(name, str) or not name or len(name) > 255:
            raise BridgeFault("Validation", "document_id and a bounded sheet name are required", 400)
        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheets = doc.getSheets()
            if sheets.hasByName(name):
                raise BridgeFault("Validation", f"Calc sheet already exists: {name}", 400)
            undo = doc.getUndoManager()
            begin()
            undo.enterUndoContext(f"Hermes: create sheet {name}")
            try:
                sheets.insertNewByName(name, len(sheets.getElementNames()))
            finally:
                undo.leaveUndoContext()
            return {"created": True, "document_id": document_id, "name": name}
        return self.dispatch(write, mutation=True)

    def set_range(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id = args.get("document_id")
        sheet_name = args.get("sheet")
        address = args.get("address")
        values = args.get("values")
        if_match = args.get("if_match")
        if not isinstance(document_id, str) or not document_id:
            raise BridgeFault("Validation", "document_id is required", 400)
        if not isinstance(sheet_name, str) or not sheet_name or len(sheet_name) > 255:
            raise BridgeFault("Validation", "sheet must be a non-empty bounded string", 400)
        if not isinstance(address, str):
            raise BridgeFault("Validation", "address must be a string", 400)
        bounds = validate_matrix(address, values)
        if if_match is not None and (not isinstance(if_match, str) or len(if_match) != 64):
            raise BridgeFault("Validation", "if_match must be a SHA-256 fingerprint", 400)

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            current = self._read_range(doc, sheet_name, address)
            if if_match is not None and not hmac.compare_digest(current["fingerprint"], if_match):
                raise BridgeFault("Stale", "Calc range changed since it was read", 409)
            sheet = doc.getSheets().getByName(sheet_name)
            undo = doc.getUndoManager()
            begin()
            undo.enterUndoContext(f"Hermes: set {address}")
            try:
                start_col, start_row, _end_col, _end_row = bounds
                for row_offset, row in enumerate(values):
                    for col_offset, value in enumerate(row):
                        self._set_cell(sheet.getCellByPosition(start_col + col_offset, start_row + row_offset), value)
            finally:
                undo.leaveUndoContext()
            result = self._read_range(doc, sheet_name, address)
            return {"written": True, **result}

        return self.dispatch(write, mutation=True)

    def writer_get_text(self, args: dict[str, Any]) -> dict[str, Any]:
        """One page of the body: up to 500 blocks and 50,000 characters, table cells included.
        ``continuation`` from the previous page is refused as Stale if the document changed."""
        if not isinstance(args, dict) or not {"document_id"} <= set(args) <= {"document_id", "offset", "limit", "continuation"} \
                or not isinstance(args.get("document_id"), str) or not args["document_id"]:
            raise BridgeFault("Validation", "writer_get_text requires document_id", 400)
        document_id = args["document_id"]
        paging = {k: v for k, v in args.items() if k != "document_id"}
        self._page_request(paging)
        return self.dispatch(lambda: self.writer_page_text(self.resolve_document(document_id, "writer"), paging))

    def writer_insert_text(self, args: dict[str, Any]) -> dict[str, Any]:
        if not {"document_id", "text", "position"} <= set(args) <= {"document_id", "text", "position", "as_paragraph"}:
            raise BridgeFault("Validation", "writer_insert_text arguments are invalid", 400)
        document_id, text, position = args["document_id"], args["text"], args["position"]
        as_paragraph = args.get("as_paragraph", False)
        if not isinstance(document_id, str) or not document_id or not isinstance(text, str) or not text or len(text) > 50000 or position not in ("start", "end") or not isinstance(as_paragraph, bool):
            raise BridgeFault("Validation", "writer_insert_text arguments are invalid", 400)
        # Paragraph mode: the bridge adds the real paragraph break itself, so edge newlines would
        # only leave empty paragraphs behind; inner newlines become paragraph breaks too.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n") if as_paragraph else [text]
        if as_paragraph and lines == [""]:
            raise BridgeFault("Validation", "writer_insert_text text is empty", 400)
        title = f"Hermes: insert Writer {'paragraph' if as_paragraph else 'text'} at {position}"

        def write(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            body = doc.getText()
            cursor = body.createTextCursor()
            cursor.gotoStart(False) if position == "start" else cursor.gotoEnd(False)
            # An already-empty edge paragraph (an empty document, or a blank last/first line) is
            # filled in place; a break is added only when the edge paragraph holds text.
            separate = as_paragraph and not (cursor.isStartOfParagraph() if position == "end" else cursor.isEndOfParagraph())
            undo = doc.getUndoManager()
            begin()
            # The paragraph break(s) and the text form ONE undo step, so undo leaves no empty paragraph.
            undo.enterUndoContext(title)
            try:
                if separate and position == "end":
                    body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                for index, line in enumerate(lines):
                    if index:
                        body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                    if line:
                        body.insertString(cursor, line, False)
                if separate and position == "start":
                    body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            finally:
                undo.leaveUndoContext()
            return {"inserted": True, "position": position, "as_paragraph": as_paragraph,
                    "characters": sum(len(line) for line in lines), "undo_title": title,
                    "document": self.document_identity(doc)}
        return self.dispatch(write, mutation=True)

    def impress_list_slides(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"document_id"} or not isinstance(args.get("document_id"), str) or not args["document_id"]:
            raise BridgeFault("Validation", "impress_list_slides requires document_id", 400)
        document_id = args["document_id"]
        def read() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            pages = doc.getDrawPages()
            slides = []
            for index in range(min(pages.getCount(), 200)):
                page = pages.getByIndex(index)
                title, texts = "", []
                for shape_index in range(page.getCount()):
                    shape = page.getByIndex(shape_index)
                    try:
                        text = str(shape.getString())
                    except Exception:
                        continue
                    if not text:
                        continue
                    texts.append(text)
                    if not title and "Title" in str(getattr(shape, "ShapeType", "")):
                        title = text
                slides.append({"index": index + 1, "title": title, "text": "\n".join(texts)[:10000]})
            return {"document": self.document_identity(doc), "slide_count": pages.getCount(), "slides": slides, "truncated": pages.getCount() > 200}
        return self.dispatch(read)

    def _validate_plan(self, plan: Any) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("version") != 1:
            raise BridgeFault("Validation", "plan version must be 1", 400)
        outcome = plan.get("outcome")
        if outcome not in ("inspect", "execute", "ask", "refuse"):
            raise BridgeFault("Validation", "plan outcome is invalid", 400)
        required = {
            "inspect": {"version", "outcome", "reason", "reads"},
            "execute": {"version", "outcome", "summary", "operations", "verification"},
            "ask": {"version", "outcome", "message"},
            "refuse": {"version", "outcome", "message"},
        }[outcome]
        if set(plan) != required:
            raise BridgeFault("Validation", "plan fields do not match its outcome", 400)
        text_key = "reason" if outcome == "inspect" else "summary" if outcome == "execute" else "message"
        if not isinstance(plan.get(text_key), str) or not plan[text_key]:
            raise BridgeFault("Validation", f"plan {text_key} is required", 400)
        limit = {"reason": 1024, "summary": 2048, "message": 2048}[text_key]
        if len(plan[text_key]) > limit:
            raise BridgeFault("Validation", f"plan {text_key} exceeds {limit} characters", 400)
        if outcome == "inspect":
            reads = plan.get("reads")
            if not isinstance(reads, list) or not 1 <= len(reads) <= 50:
                raise BridgeFault("Validation", "inspect plan needs 1-50 reads", 400)
            for read in reads:
                if not isinstance(read, dict) or set(read) != {"kind", "sheet", "address"} or read.get("kind") not in ("read_range", "summarize_range_stats"):
                    raise BridgeFault("Validation", "inspection operation is invalid", 400)
                if not isinstance(read["sheet"], str) or not read["sheet"]:
                    raise BridgeFault("Validation", "inspection sheet is invalid", 400)
                parse_a1(read["address"])
        elif outcome == "execute":
            operations, verification = plan.get("operations"), plan.get("verification")
            if not isinstance(operations, list) or not 1 <= len(operations) <= 30:
                raise BridgeFault("Validation", "execute plan needs 1-30 operations", 400)
            if not isinstance(verification, list) or not 1 <= len(verification) <= 30:
                raise BridgeFault("Validation", "execute plan needs 1-30 verification rules", 400)
            for op in operations:
                if not isinstance(op, dict) or not isinstance(op.get("kind"), str):
                    raise BridgeFault("Validation", "workbook operation is invalid", 400)
                kind = op["kind"]
                required_by_kind = {
                    "write_values": {"kind", "sheet", "address", "values"},
                    "write_formula": {"kind", "sheet", "address", "formula"},
                    "format_range": {"kind", "sheet", "address", "format"},
                    "clear_range": {"kind", "sheet", "address"},
                    "create_sheet": {"kind", "name"},
                }
                if kind not in required_by_kind or set(op) != required_by_kind[kind]:
                    raise BridgeFault("Validation", f"unsupported or malformed operation: {kind}", 400)
                if kind == "create_sheet":
                    if not isinstance(op["name"], str) or not op["name"] or len(op["name"]) > 255:
                        raise BridgeFault("Validation", "sheet name is invalid", 400)
                    continue
                if not isinstance(op["sheet"], str) or not op["sheet"]:
                    raise BridgeFault("Validation", "operation sheet is invalid", 400)
                if kind == "write_values":
                    validate_matrix(op["address"], op["values"])
                else:
                    bounds = parse_a1(op["address"])
                    if kind == "write_formula" and (
                        bounds[0] != bounds[2] or bounds[1] != bounds[3]
                        or not isinstance(op["formula"], str) or not op["formula"].startswith("=")
                    ):
                        raise BridgeFault("Validation", "write_formula requires one cell and an = formula", 400)
                    if kind == "format_range":
                        self._validate_format(op["format"])
            allowed_verification = {
                "range_values_equal": "expectedValues",
                "range_formulas_equal": "expectedFormulas",
                "sheet_exists": "name",
                "sentinel_unchanged": "expected",
                "range_number_format_equal": "expectedNumberFormat",
            }
            for rule in verification:
                if not isinstance(rule, dict) or rule.get("kind") not in allowed_verification:
                    raise BridgeFault("Validation", "verification rule is invalid", 400)
                kind = rule["kind"]
                if kind == "sheet_exists":
                    if set(rule) != {"kind", "name"} or not isinstance(rule["name"], str):
                        raise BridgeFault("Validation", "sheet_exists verification is invalid", 400)
                else:
                    expected_key = allowed_verification[kind]
                    if set(rule) != {"kind", "sheet", "address", expected_key}:
                        raise BridgeFault("Validation", f"{kind} verification is invalid", 400)
                    bounds = parse_a1(rule["address"])
                    if kind == "sentinel_unchanged" and (
                        bounds[0] != bounds[2] or bounds[1] != bounds[3]
                        or not (rule["expected"] is None or isinstance(rule["expected"], (str, bool, int, float)))
                    ):
                        raise BridgeFault("Validation", "sentinel_unchanged needs one cell and a scalar expected value", 400)
        return plan

    @staticmethod
    def _stats(values: list[list[Any]]) -> dict[str, Any]:
        flat = [item for row in values for item in row]
        numeric = [float(item) for item in flat if isinstance(item, (int, float)) and not isinstance(item, bool)]
        return {
            "count": len(flat), "numericCount": len(numeric), "sum": sum(numeric),
            "average": sum(numeric) / len(numeric) if numeric else None,
            "min": min(numeric) if numeric else None, "max": max(numeric) if numeric else None,
            "blankCount": sum(item in (None, "") for item in flat),
        }

    def execute_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"document_id", "plan"} or not isinstance(args.get("document_id"), str) or not args["document_id"]:
            raise BridgeFault("Validation", "calc_execute_plan requires document_id and plan", 400)
        document_id = args["document_id"]
        plan = self._validate_plan(args["plan"])
        if plan["outcome"] in ("ask", "refuse"):
            return {"outcome": plan["outcome"], "message": plan["message"]}

        def run(begin: Callable[[], None] | None = None) -> dict[str, Any]:
            doc = self.resolve_document(document_id, "calc")
            sheets = doc.getSheets()
            if plan["outcome"] == "inspect":
                results = []
                for read in plan["reads"]:
                    data = self._read_range(doc, read["sheet"], read["address"])
                    results.append({"kind": read["kind"], "data": data if read["kind"] == "read_range" else self._stats(data["values"])})
                return {"outcome": "inspect", "results": results}
            simulated = set(sheets.getElementNames())
            for op in plan["operations"]:
                if op["kind"] == "create_sheet":
                    if op["name"] in simulated:
                        raise BridgeFault("Validation", f"Calc sheet already exists: {op['name']}", 400)
                    simulated.add(op["name"])
                elif op["sheet"] not in simulated:
                    raise BridgeFault("NotFound", f"Calc sheet does not exist: {op['sheet']}", 404)
            for rule in plan["verification"]:
                target = rule.get("sheet") or rule.get("name")
                if target not in simulated:
                    raise BridgeFault("NotFound", f"Calc sheet does not exist: {target}", 404)
            undo = doc.getUndoManager()
            if begin is not None:
                begin()
            adds_sheets = any(op["kind"] == "create_sheet" for op in plan["operations"])
            undo.enterUndoContext(PLAN_WITH_SHEETS_UNDO_TITLE if adds_sheets else PLAN_UNDO_TITLE)
            try:
                for op in plan["operations"]:
                    kind = op["kind"]
                    if kind == "create_sheet":
                        sheets.insertNewByName(op["name"], len(sheets.getElementNames()))
                        continue
                    sheet, cell_range = self._sheet_and_range(doc, op["sheet"], op["address"])
                    if kind == "write_values":
                        self._write_values(sheet, validate_matrix(op["address"], op["values"]), op["values"])
                    elif kind == "write_formula":
                        # A one-cell range object has no setFormula; the cell does.
                        cell_range.getCellByPosition(0, 0).setFormula(op["formula"])
                    elif kind == "format_range":
                        self._apply_format(doc, cell_range, op["format"])
                    elif kind == "clear_range":
                        cell_range.clearContents(1023)
            finally:
                undo.leaveUndoContext()
            checks = []
            for rule in plan["verification"]:
                kind = rule["kind"]
                if kind == "sheet_exists":
                    actual, passed = sheets.hasByName(rule["name"]), sheets.hasByName(rule["name"])
                else:
                    data = self._read_range(doc, rule["sheet"], rule["address"])
                    if kind == "range_values_equal":
                        actual, passed = data["values"], data["values"] == rule["expectedValues"]
                    elif kind == "sentinel_unchanged":
                        actual = data["values"][0][0]
                        expected = "" if rule["expected"] is None else rule["expected"]
                        if isinstance(expected, bool):
                            expected = 1.0 if expected else 0.0
                        passed = actual == expected
                    elif kind == "range_formulas_equal":
                        actual, passed = data["formulas"], data["formulas"] == rule["expectedFormulas"]
                    else:
                        actual = data["number_formats"]
                        passed = all(value == rule["expectedNumberFormat"] for row in actual for value in row)
                checks.append({"kind": kind, "passed": passed, "actual": actual})
            return {"outcome": "execute", "summary": plan["summary"], "verification": checks, "verified": all(item["passed"] for item in checks)}
        return self.dispatch(run, mutation=plan["outcome"] == "execute")

    def _is_active(self, doc: Any) -> bool:
        """The bound document is LibreOffice's focused view and the desktop's current component."""
        return self.focus.is_focused(doc) and self._is_frontmost(doc, self._current_component())

    def _activate(self, doc: Any) -> None:
        frame = doc.getCurrentController().getFrame()
        frame.activate()
        for window in (frame.getContainerWindow(), frame.getComponentWindow()):
            for name in ("toFront", "setFocus"):
                try:
                    getattr(window, name)()
                except Exception:
                    continue

    def _focus_for_undo(self, document_id: str, active: bool) -> tuple[bool, bool]:
        """Bring the document to the front unless ``active`` already says it is LibreOffice's
        focused window, and wait up to ACTIVATION_WAIT_SECONDS for that to be confirmed.
        LibreOffice applies parts of an undo through the focused window (live tests saw undo
        changes land in the front document). Off the main thread; returns (active, activated)."""
        if active:
            return True, False
        self.dispatch(lambda: self._activate(self.resolve_document(document_id)))
        deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
        while not active and time.monotonic() < deadline:
            time.sleep(0.1)
            active = self.dispatch(lambda: self._is_active(self.resolve_document(document_id)))
        return active, active

    def undo(self, args: dict[str, Any]) -> dict[str, Any]:
        document_id = args.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise BridgeFault("Validation", "document_id is required", 400)
        hermes_only = args.get("hermes_only", False)
        if not set(args) <= {"document_id", "hermes_only"} or not isinstance(hermes_only, bool):
            raise BridgeFault("Validation", "undo arguments are invalid", 400)

        def top() -> tuple[str, bool]:
            doc = self.resolve_document(document_id)
            undo = doc.getUndoManager()
            if not undo.isUndoPossible():
                raise BridgeFault("NotFound", "No undo action is available", 404, state="not_started")
            title = str(undo.getCurrentUndoActionTitle())
            if hermes_only and not title.startswith(HERMES_UNDO_PREFIX):
                raise BridgeFault(
                    "Validation",
                    f"The last undo step is not a Hermes change ({title[:80]}); nothing was undone "
                    "(use Edit > Undo for your own edits)",
                    409, state="not_started",
                )
            return title, self._is_active(doc)

        title, active = self.dispatch(top)
        active, activated = self._focus_for_undo(document_id, active)
        if not active:
            raise BridgeFault(
                "Validation",
                "LibreOffice applies undo through the focused window, and this document could not be "
                "confirmed as the focused window; nothing was undone (use Edit > Undo in its window)",
                409, state="not_started",
            )

        def perform(begin: Callable[[], None]) -> dict[str, Any]:
            doc = self.resolve_document(document_id)
            undo = doc.getUndoManager()
            if not undo.isUndoPossible() or str(undo.getCurrentUndoActionTitle()) != title:
                raise BridgeFault("Stale", "The undo stack changed; nothing was undone", 409, state="not_started")
            if not self._is_active(doc):
                raise BridgeFault("Validation", "The document stopped being the focused window; nothing was undone",
                                  409, state="not_started")
            begin()
            undo.undo()
            return {"undone": True, "title": title, "activated_document": activated,
                    "document": self.document_identity(doc)}

        return self.dispatch(perform, mutation=True)

    def show_chat_panel(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open the sidebar of one document's window on the Hermes deck."""
        document_id = args.get("document_id")
        if set(args) != {"document_id"} or not isinstance(document_id, str) or not document_id:
            raise BridgeFault("Validation", "show_chat_panel requires document_id", 400)

        def show() -> dict[str, Any]:
            doc = self.resolve_document(document_id)
            if not open_sidecar_deck(doc.getCurrentController()):
                raise BridgeFault("Unavailable", "the Hermes sidebar deck is not registered in this LibreOffice", 503)
            return {"shown": True, "document": self.document_identity(doc)}
        return self.dispatch(show)

    def dev_chat_panel(self, args: dict[str, Any]) -> dict[str, Any]:
        """Development only (LibreOffice started with HERMES_LO_DEV=1): drive or read the open chat panel."""
        if os.environ.get("HERMES_LO_DEV") != "1":
            raise BridgeFault("Validation", "unsupported operation: dev_chat_panel", 400)
        action, text = args.get("action"), args.get("text", "")
        if action not in ("submit", "read") or not isinstance(text, str) or len(text) > 8000:
            raise BridgeFault("Validation", "dev_chat_panel arguments are invalid", 400)
        import panel_registry

        def run() -> dict[str, Any]:
            panel = panel_registry.latest()
            if panel is None:
                raise BridgeFault("NotFound", "no chat panel is open", 404)
            if action == "submit":
                panel.submit(text)
            return panel.snapshot()
        return self.dispatch(run)

    def dev_select_text(self, args: dict[str, Any]) -> dict[str, Any]:
        """Development only (HERMES_LO_DEV=1): select the n-th match of a literal string in a
        Writer document's window, so acceptance runs can create and change a selection."""
        if os.environ.get("HERMES_LO_DEV") != "1":
            raise BridgeFault("Validation", "unsupported operation: dev_select_text", 400)
        document_id, search, occurrence = args.get("document_id"), args.get("search"), args.get("occurrence", 1)
        if not isinstance(search, str) or not search or len(search) > 1000 or not isinstance(occurrence, int) or occurrence < 1:
            raise BridgeFault("Validation", "dev_select_text arguments are invalid", 400)

        def run() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "writer")
            descriptor = doc.createSearchDescriptor()
            descriptor.SearchString = search
            descriptor.SearchCaseSensitive = True
            found = doc.findAll(descriptor)
            if found is None or found.getCount() < occurrence:
                raise BridgeFault("NotFound", "no such match", 404)
            doc.getCurrentController().select(found.getByIndex(occurrence - 1))
            return {"selected": search}
        return self.dispatch(run)

    def dev_set_current_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        """Development only (HERMES_LO_DEV=1): show slide n in an Impress document's window, so
        acceptance runs can move the user's position."""
        if os.environ.get("HERMES_LO_DEV") != "1":
            raise BridgeFault("Validation", "unsupported operation: dev_set_current_slide", 400)
        document_id, number = args.get("document_id"), args.get("slide")
        if not isinstance(number, int) or number < 1:
            raise BridgeFault("Validation", "slide must be a 1-based number", 400)

        def run() -> dict[str, Any]:
            doc = self.resolve_document(document_id, "impress")
            pages = doc.getDrawPages()
            if number > pages.getCount():
                raise BridgeFault("NotFound", "no such slide", 404)
            doc.getCurrentController().setCurrentPage(pages.getByIndex(number - 1))
            return {"slide": number}
        return self.dispatch(run)

    def list_documents(self) -> dict[str, Any]:
        def inspect() -> dict[str, Any]:
            current = self._current_component()
            documents = []
            for doc in self._enumerate_documents():
                item = self.document_identity(doc)
                item["frontmost"] = self._is_frontmost(doc, current)
                documents.append(item)
            return {"documents": documents}
        return self.dispatch(inspect)

    def invoke(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "health":
            return {"status": "ok", "port": self.port, "main_thread_marshalling": "AsyncCallback"}
        if op == "list_documents":
            return self.list_documents()
        if op == "get_overview":
            return self.get_overview()
        if op == "get_range":
            return self.get_range(args)
        if op == "set_range":
            return self.set_range(args)
        if op == "format_range":
            return self.format_range(args)
        if op == "clear_range":
            return self.clear_range(args)
        if op == "create_sheet":
            return self.create_sheet(args)
        if op == "execute_plan":
            return self.execute_plan(args)
        if op == "writer_get_text":
            return self.writer_get_text(args)
        if op == "writer_insert_text":
            return self.writer_insert_text(args)
        if op == "impress_list_slides":
            return self.impress_list_slides(args)
        if op == "undo":
            return self.undo(args)
        if op == "dev_chat_panel":
            return self.dev_chat_panel(args)
        if op == "show_chat_panel":
            return self.show_chat_panel(args)
        if op == "dev_select_text":
            return self.dev_select_text(args)
        if op == "dev_set_current_slide":
            return self.dev_set_current_slide(args)
        if op in EXTRA_OPERATIONS or op in INTERNAL_OPERATIONS:
            return getattr(self, op)(args)
        raise BridgeFault("Validation", f"unsupported operation: {op}", 400)


_WINDOWS = os.name == "nt"


class _DeadlineReader(io.RawIOBase):
    """Socket reads bounded by ONE absolute deadline: every recv gets only the time that is
    left, so a client that trickles bytes cannot extend it the way a per-recv timeout allows."""

    def __init__(self, connection: Any, deadline: float) -> None:
        super().__init__()
        self._connection, self._deadline = connection, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("request was not received in time")
        self._connection.settimeout(left)
        return self._connection.recv_into(buffer)


class BoundedHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server that serves at most ``max_handlers`` connections at once; the limit
    is checked in the accepting thread, before a worker thread exists, and a connection over it
    gets the prepared ``refusal`` response without waiting and is closed."""

    daemon_threads = True

    def __init__(self, address: Any, handler: Any, *, max_handlers: int, refusal: bytes) -> None:
        self._slots = threading.BoundedSemaphore(max_handlers)
        self._refusal = refusal
        super().__init__(address, handler)

    def server_bind(self) -> None:
        if _WINDOWS:
            # Windows: SO_REUSEADDR (the stdlib default here) would let another socket, even one of
            # another account, bind this address and port while the listener is up and receive
            # some of its connections. SO_EXCLUSIVEADDRUSE refuses every second bind instead.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        super().server_bind()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.setblocking(False)   # the accepting thread never waits on this client
                request.send(self._refusal)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _busy_refusal() -> bytes:
    data = json.dumps({"ok": False, "category": "Capacity", "error": "the bridge is busy; try again shortly"},
                      separators=(",", ":")).encode("utf-8")
    return (f"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii") + data


def make_server(runtime: "BridgeRuntime", port: int) -> BoundedHTTPServer:
    """The bridge's loopback listener, bounded by MAX_HANDLERS and READ_DEADLINE_SECONDS."""
    server = BoundedHTTPServer(("127.0.0.1", port), handler_for(runtime), max_handlers=MAX_HANDLERS,
                               refusal=_busy_refusal())
    server.daemon_threads = True
    return server


_BRIDGE_ROUTES = {("POST", "/invoke"): "invoke"}


def handler_for(runtime: BridgeRuntime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SidecarLibreOfficeBridge/0.4"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            # ONE deadline for the whole request, headers and body: see _DeadlineReader.
            plain, self.rfile = self.rfile, io.BufferedReader(
                _DeadlineReader(self.connection, time.monotonic() + READ_DEADLINE_SECONDS))
            plain.close()

        # bridge.log gets a fixed route label and the status only, never the request line: a
        # local client chooses the path, and http.server's own messages quote it.
        def log_message(self, fmt: str, *args: Any) -> None:
            return None

        def log_error(self, fmt: str, *args: Any) -> None:
            runtime.logger.info("http request not completed")

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            command = getattr(self, "command", None)
            route = _BRIDGE_ROUTES.get((command, getattr(self, "path", None)), "unknown")
            method = command if command in ("GET", "POST") else "other"
            try:
                status = str(int(code))
            except (TypeError, ValueError):
                status = "-"
            runtime.logger.info("http %s %s -> %s", method, route, status)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.connection.settimeout(WRITE_TIMEOUT_SECONDS)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            try:
                if self.path != "/invoke":
                    raise BridgeFault("Protocol", "unknown bridge route", 404)
                if self.headers.get("Origin") is not None:
                    raise BridgeFault("Auth", "Origin requests are forbidden", 403)
                if self.headers.get("Host") != f"127.0.0.1:{runtime.port}":
                    raise BridgeFault("Auth", "Host must be the configured loopback endpoint", 403)
                supplied = self.headers.get("X-Hermes-Plugin-Key", "")
                # Bytes, not str: compare_digest refuses non-ASCII str, which made a 500 of a 401.
                if not hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), runtime.token.encode("utf-8")):
                    raise BridgeFault("Auth", "bridge authentication failed", 401)
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise BridgeFault("Protocol", "Content-Type must be application/json", 415)
                length_text = self.headers.get("Content-Length")
                if length_text is None:
                    raise BridgeFault("Protocol", "Content-Length is required", 411)
                try:
                    length = int(length_text)
                except ValueError:
                    raise BridgeFault("Protocol", "Content-Length is invalid", 400)
                if length < 0 or length > MAX_BODY:
                    raise BridgeFault("Capacity", "request body exceeds 1 MiB", 413)
                try:
                    raw = self.rfile.read(length)
                except OSError:   # socket.timeout included: the ONE read deadline passed
                    raise BridgeFault("Protocol", "request body did not arrive in time", 408) from None
                if len(raw) != length:
                    raise BridgeFault("Protocol", "request body is truncated", 400)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise BridgeFault("Protocol", "request body is not valid JSON", 400)
                if not isinstance(payload, dict) or set(payload) != {"op", "args"}:
                    raise BridgeFault("Validation", "request must contain only op and args", 400)
                op, args = payload["op"], payload["args"]
                if not isinstance(op, str) or not isinstance(args, dict):
                    raise BridgeFault("Validation", "op must be a string and args must be an object", 400)
                result = runtime.invoke(op, args)
                self.send_json(200, {"ok": True, "data": result})
            except RangeValidationError as exc:
                # Raised while checking arguments, before anything was dispatched to LibreOffice.
                self.send_json(400, {"ok": False, "category": "Validation", "error": str(exc), "state": "not_started"})
            except BridgeFault as exc:
                body = {"ok": False, "category": exc.category, "error": str(exc)}
                if exc.state is not None:
                    body["state"] = exc.state
                self.send_json(exc.status, body)
            except Exception:
                runtime.logger.exception("unhandled bridge request failure")
                self.send_json(500, {"ok": False, "category": "Protocol", "error": "Unhandled bridge failure"})

    return Handler


def profile_bridge_dir(ctx: Any) -> Path:
    substitution = ctx.getServiceManager().createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
    user_url = substitution.substituteVariables("$(user)", True)
    return Path(uno.fileUrlToSystemPath(user_url)) / "hermes-bridge"


def load_startup_config(ctx: Any) -> tuple[Path, str, int, logging.Logger]:
    """The bridge directory, pairing token, port and logger. The directory, token and port file
    are checked as private to this user with the platform's own rules (see private_files):
    POSIX permission bits, or the owner and DACL on Windows. A failed check refuses to start; it
    is never bypassed. bridge.log is not checked: it is created with the folder's inherited
    permissions and never holds the token."""
    bridge_dir = profile_bridge_dir(ctx)
    try:
        private_files.ensure_private_dir(bridge_dir)
    except OSError as exc:
        raise RuntimeError(f"bridge directory is not private: {exc}") from None
    token_path = bridge_dir / "token"
    if not os.path.lexists(token_path):
        try:
            private_files.create_private_file(token_path, (secrets.token_urlsafe(32) + "\n").encode("utf-8"))
        except FileExistsError:
            pass  # created concurrently; it is checked like any existing token below
        except OSError as exc:  # refused before any token byte was written; see private_files
            raise RuntimeError(f"token file could not be created privately: {exc}") from None
    try:
        raw = private_files.read_private_bytes(token_path, 4096)
    except OSError as exc:
        raise RuntimeError(f"token file is not private: {exc}") from None
    log_path = bridge_dir / "bridge.log"
    logger = logging.getLogger("sidecar_libreoffice_bridge")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("token file content is invalid") from None
    if len(token.encode("utf-8")) < 32 or any(ord(ch) < 33 or ord(ch) == 127 for ch in token):
        raise RuntimeError("token file content is invalid")
    port = DEFAULT_PORT
    port_path = bridge_dir / "port"
    if os.path.lexists(port_path):
        try:
            text = private_files.read_private_bytes(port_path, 16, secret=False).decode("ascii").strip()
        except (OSError, UnicodeDecodeError):
            raise RuntimeError("port configuration is insecure") from None
        if not text.isdigit():
            raise RuntimeError("configured port is invalid")
        port = int(text)
        if not 1024 <= port <= 65535:
            raise RuntimeError("configured port is outside the allowed range")
    return bridge_dir, token, port, logger


def start_server(ctx: Any) -> None:
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return
        try:
            _bridge_dir, token, port, logger = load_startup_config(ctx)
            callback = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
            runtime = BridgeRuntime(ctx, callback, token, port, logger)
            server = make_server(runtime, port)
            thread = threading.Thread(target=server.serve_forever, name="SidecarLibreOfficeBridge", daemon=True)
            thread.start()
            _SERVER = server
            try:
                import panel_registry
                panel_registry.identify = lambda doc: {
                    key: runtime.document_identity(doc)[key] for key in ("document_id", "title", "type")
                } if runtime.document_type(doc) else None
            except Exception as exc:
                logger.warning("chat panels will not name their document: %s", type(exc).__name__)
            logger.info("bridge started on loopback port %d", port)
        except Exception as exc:
            try:
                bridge_dir = profile_bridge_dir(ctx)
                private_files.ensure_private_dir(bridge_dir)  # never log into a directory others control
                with (bridge_dir / "bridge.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"bridge did not start: {type(exc).__name__}: {exc}\n")
            except Exception:
                pass


class TokenTransferable(unohelper.Base, XTransferable):
    def __init__(self, text: str) -> None:
        self.text = text
        self.flavor = uno.createUnoStruct("com.sun.star.datatransfer.DataFlavor")
        self.flavor.MimeType = "text/plain;charset=utf-16"
        self.flavor.HumanPresentableName = "Unicode Text"
        self.flavor.DataType = uno.getTypeByName("string")

    def getTransferData(self, flavor: Any) -> str:
        if not self.isDataFlavorSupported(flavor):
            raise ValueError("unsupported clipboard flavor")
        return self.text

    def getTransferDataFlavors(self) -> tuple[Any, ...]:
        return (self.flavor,)

    def isDataFlavorSupported(self, flavor: Any) -> bool:
        return getattr(flavor, "MimeType", "") == self.flavor.MimeType


class HermesBridgeJob(unohelper.Base, XJob, XDispatchProvider, XDispatch):
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def execute(self, _arguments: Any) -> Any:
        start_server(self.ctx)
        return None

    def queryDispatch(self, url: Any, _target: str, _flags: int) -> Any:
        return self if getattr(url, "Complete", "") in (COPY_TOKEN_URL, OPEN_CHAT_URL) else None

    def queryDispatches(self, descriptors: Any) -> tuple[Any, ...]:
        return tuple(
            self.queryDispatch(item.FeatureURL, item.FrameName, item.SearchFlags)
            for item in descriptors
        )

    def dispatch(self, url: Any, _arguments: Any) -> None:
        """Copy the pairing token. Never lets an exception escape: LibreOffice runs menu
        dispatches from its event loop, and an escaping exception aborted soffice (observed
        live on LibreOffice 26.8 when the message-box type was looked up as a constant)."""
        if getattr(url, "Complete", "") == OPEN_CHAT_URL:
            try:
                desktop = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
                if not open_sidecar_deck(desktop.getCurrentFrame().getController()):
                    raise RuntimeError("deck not registered")
            except Exception as exc:
                self._log(f"open chat failed: {type(exc).__name__}")
                self._notify("Sidecar for LibreOffice could not open the chat panel in this window.", error=True)
            return
        if getattr(url, "Complete", "") != COPY_TOKEN_URL:
            return
        try:
            _bridge_dir, token, _port, _logger = load_startup_config(self.ctx)
            services = self.ctx.getServiceManager()
            clipboard = services.createInstanceWithContext(
                "com.sun.star.datatransfer.clipboard.SystemClipboard", self.ctx
            )
            clipboard.setContents(TokenTransferable(token), None)
            if hasattr(clipboard, "flushClipboard"):
                clipboard.flushClipboard()
        except Exception as exc:
            self._log(f"copy pairing token failed: {type(exc).__name__}")
            self._notify("Sidecar for LibreOffice could not copy the pairing token.", error=True)
            return
        self._notify("Pairing token copied to the clipboard.")

    def _log(self, line: str) -> None:
        try:
            with (profile_bridge_dir(self.ctx) / "bridge.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except Exception:
            return

    def _notify(self, text: str, *, error: bool = False) -> None:
        try:
            services = self.ctx.getServiceManager()
            desktop = services.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            frame = desktop.getCurrentFrame()
            parent = frame.getContainerWindow() if frame is not None else None
            if parent is None:
                return
            toolkit = services.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
            # MessageBoxType is a UNO enum, not a constants group.
            kind = uno.Enum("com.sun.star.awt.MessageBoxType", "ERRORBOX" if error else "INFOBOX")
            toolkit.createMessageBox(parent, kind, 1, "Sidecar for LibreOffice", text).execute()
        except Exception as exc:
            self._log(f"pairing token notice failed: {type(exc).__name__}")

    def addStatusListener(self, _listener: Any, _url: Any) -> None:
        return None

    def removeStatusListener(self, _listener: Any, _url: Any) -> None:
        return None


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(HermesBridgeJob, IMPLEMENTATION_NAME, SERVICE_NAMES)
