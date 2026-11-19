# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Hermes tools for explicitly bound LibreOffice documents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import logging
import math
import os
import re

from .bridge_client import BridgeClient, BridgeError
from .chat_bridge import DEFAULT_API_PORT, DEFAULT_CHAT_PORT, ChatBridge, SecretProvider, SecretUnavailable
from .csv_import import parse_csv, target_address
from .plan_logic import validate_document_id, validate_format, validate_plan, validate_sheet
from .range_logic import RangeValidationError, parse_a1, validate_matrix

_TOOLSET = "sidecar-libreoffice"
_SCHEMA_PATH = Path(__file__).with_name("tool_schemas.json")
MAX_UNDO_STEPS = 10


def _load_schemas() -> list[dict[str, Any]]:
    value = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise RuntimeError("LibreOffice plugin schemas must be an array")
    names: set[str] = set()
    for schema in value:
        if not isinstance(schema, dict) or not isinstance(schema.get("name"), str):
            raise RuntimeError("LibreOffice plugin contains an invalid schema")
        name = schema["name"]
        if not name.startswith(("calc_", "writer_", "impress_", "libreoffice_")) or name in names:
            raise RuntimeError("LibreOffice tool names must be unique and namespaced")
        names.add(name)
    return value


_SCHEMAS = _load_schemas()
# Tools whose bridge operation has the tool's own name and whose arguments are checked against
# the tool's schema here and again, in full, by the bridge. True marks a change to the document.
_DIRECT_TOOLS = {
    "impress_list_layouts": False,
    "impress_get_current_slide": False,
    "calc_recalculate": True,
    "calc_activate_sheet": True,
    "calc_insert_table": True,
    "calc_protect_sheet": True,
    "calc_freeze_panes": True,
    "calc_set_validation": True,
    "calc_add_conditional_format": True,
    "calc_get_rules": False,
    "calc_list_charts": False,
    "calc_create_chart": True,
    "calc_filter_range": True,
    "calc_sort_range": True,
    "impress_add_shape": True,
    "impress_format_text": True,
    "impress_set_slide_layout": True,
    "writer_add_comment": True,
    "writer_get_comments": False,
    "writer_format_selection": True,
    "writer_apply_edits": True,
    "writer_insert_paragraphs": True,
    "writer_get_table": False,
    "calc_rename_sheet": True, "calc_copy_sheet": True, "calc_delete_sheet": True, "calc_find_replace": True,
    "calc_autofill": True, "calc_range_stats": False, "calc_add_comment": True, "calc_get_comments": False, "calc_clear_comments": True,
    "writer_get_structure": False, "writer_set_paragraph_style": True, "writer_find_replace": True,
    "writer_get_selection": False, "writer_replace_selection": True,
    "impress_get_slide": False, "impress_create_slide": True, "impress_delete_slide": True,
    "impress_set_shape_text": True, "impress_add_text_box": True, "impress_find_replace": True,
}
_SCHEMA_BY_NAME = {schema["name"]: schema for schema in _SCHEMAS}
_SKILLS = {
    "libreoffice-guidance": "Use when building, analyzing, editing or styling a spreadsheet, document or slide deck that is open in LibreOffice.",
    "calc-plans": "Use when a multi-step Calc change should be one verified undo step through calc_execute_plan.",
}


def _validate_schema_args(name: str, args: dict[str, Any]) -> None:
    """Check ``args`` against the flat parameter schema of a direct tool."""
    parameters = _SCHEMA_BY_NAME[name]["parameters"]
    properties = parameters["properties"]
    _exact(args, set(parameters["required"]), set(properties) - set(parameters["required"]))
    for key, value in args.items():
        if not _valid_value(properties[key], value):
            raise RangeValidationError(f"{key} is invalid")
    for key in ("address", "cell"):
        if key in args:
            parse_a1(args[key])
    _validate_operator_values(name, args)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# What each operator takes, exactly as the bridge checks it (office_ops calc_filter_range and
# calc_add_conditional_format); the schema alone cannot tie a value's type to the operator.
_FILTER_NUMBER_OPERATORS = frozenset({"greater", "greater_equal", "less", "less_equal"})


def _validate_operator_values(name: str, args: dict[str, Any]) -> None:
    if name == "calc_filter_range":
        for condition in args["conditions"]:
            operator, value = condition["operator"], condition.get("value")
            if operator == "not_empty":
                valid = "value" not in condition
            elif operator in _FILTER_NUMBER_OPERATORS:
                valid = _is_number(value)
            elif operator in ("equals", "not_equals"):
                valid = _is_number(value) or isinstance(value, str)
            else:
                valid = isinstance(value, str) and value != ""
            if not valid:
                raise RangeValidationError(f"filter operator {operator} does not take this value")
    elif name == "calc_add_conditional_format":
        operator, value1, value2 = args["operator"], args["value1"], args.get("value2")
        if operator == "formula":
            valid = isinstance(value1, str) and not value1.startswith("=") and "value2" not in args
        else:
            valid = (all(_is_number(v) or (isinstance(v, str) and len(v) <= 255) for v in (value1, value2) if v is not None)
                     and ("value2" in args) == operator.endswith("between"))
        if not valid:
            raise RangeValidationError(f"conditional format operator {operator} does not take these values")


def _in_bounds(spec: dict[str, Any], value: Any) -> bool:
    """A missing bound is no bound."""
    return (("minimum" not in spec or value >= spec["minimum"]) and ("maximum" not in spec or value <= spec["maximum"])
            and ("exclusiveMinimum" not in spec or value > spec["exclusiveMinimum"])
            and ("exclusiveMaximum" not in spec or value < spec["exclusiveMaximum"]))


def _valid_value(spec: dict[str, Any], value: Any) -> bool:
    """The subset of JSON Schema the tool schemas use: bounded scalars, unions of them, arrays and
    closed objects. Total: a missing keyword is no constraint, an unknown type is invalid."""
    kind = spec.get("type")
    if isinstance(kind, list):   # a union: valid as any one member type, under that type's keywords
        return any(_valid_value({**spec, "type": member}, value) for member in kind)
    if kind == "null":
        return value is None
    if kind == "string":
        valid = isinstance(value, str) and spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", len(value))
        return valid and ("enum" not in spec or value in spec["enum"])
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and _in_bounds(spec, value)
    if kind == "number":
        return (_is_number(value) and (isinstance(value, int) or math.isfinite(value)) and _in_bounds(spec, value))
    if kind == "array":
        items = spec.get("items")
        return (isinstance(value, list) and spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", len(value))
                and (items is None or all(_valid_value(items, item) for item in value)))
    if kind == "object":
        properties, required = spec.get("properties", {}), set(spec.get("required", []))
        return (isinstance(value, dict) and required <= set(value) <= set(properties)
                and all(_valid_value(properties[k], v) for k, v in value.items()))
    return False


def _secret() -> str:
    from agent.secret_scope import get_secret

    return get_secret("LIBREOFFICE_LIVE_BRIDGE_TOKEN", "") or ""


def _api_server_key() -> str:
    from agent.secret_scope import get_secret

    return get_secret("API_SERVER_KEY", "") or ""


def _scoped_reader(read: Callable[[], str]) -> Callable[[], str]:
    """A reader for SecretProvider: Hermes's unscoped-read refusal (multiplexed gateway, worker
    thread) becomes SecretUnavailable, the one failure that keeps the cached value; every other
    result, an empty one included, is authoritative."""
    def reader() -> str:
        try:
            from agent.secret_scope import UnscopedSecretError
        except ImportError:
            UnscopedSecretError = ()   # noqa: N806 - an older store has no such refusal
        try:
            return read()
        except UnscopedSecretError:   # type: ignore[misc]
            raise SecretUnavailable() from None
    return reader


_TOKEN = SecretProvider(_scoped_reader(lambda: _secret()))
_API_KEY = SecretProvider(_scoped_reader(lambda: _api_server_key()))
_CHAT: ChatBridge | None = None
_log = logging.getLogger(__name__)


def _api_profile_prefix() -> str:
    """'/p/<profile>' for a named Hermes profile, '' for the default one (the API server's routing)."""
    try:
        from hermes_constants import PROFILE_ID_RE, get_hermes_home
    except ImportError:
        return ""
    home = Path(get_hermes_home())
    profile = home.name if home.parent.name == "profiles" else "default"
    return "" if profile == "default" or not PROFILE_ID_RE.fullmatch(profile) else f"/p/{profile}"


def _port_setting(ctx: Any, key: str, default: int, *, allow_zero: bool = False) -> int:
    value = ctx.get_config(key, default=default)
    if isinstance(value, int) and not isinstance(value, bool) and (1024 <= value <= 65535 or (allow_zero and value == 0)):
        return value
    return default


def _in_gateway_process() -> bool:
    """True only in the Hermes gateway process itself, which serves the API server.

    The gateway sets ``_HERMES_GATEWAY`` when its ``gateway.run`` module loads, and every command
    it launches inherits the variable; only the gateway has the module loaded.
    """
    import sys

    return os.environ.get("_HERMES_GATEWAY") == "1" and "gateway.run" in sys.modules


def _start_chat(ctx: Any, owned: dict[str, Any]) -> None:
    """Start the sidebar chat endpoint in the process that serves the Hermes API server.

    Called last in ``register``, after the tool fence and the unload cleanup exist; the bridge
    forwards only while this registration's fence is live (``owned["fence"]``).
    """
    global _CHAT
    _TOKEN.refresh()
    _API_KEY.refresh()
    if _CHAT is not None or not _in_gateway_process():
        return
    port = _port_setting(ctx, "chat_port", DEFAULT_CHAT_PORT, allow_zero=True)
    if port == 0:   # off unless the user turned it on (README: Turn on the sidebar chat)
        _log.info("libreoffice sidebar chat endpoint is off (plugin setting chat_port is 0)")
        return
    try:
        chat = ChatBridge(port=port, token=_TOKEN, api_key=_API_KEY, api_prefix=_api_profile_prefix(),
                          api_port=_port_setting(ctx, "api_server_port", DEFAULT_API_PORT),
                          fence_active=lambda: bool(owned.get("fence")))
        chat.start()
    except (OSError, ValueError) as exc:
        _log.warning("libreoffice chat endpoint not started on 127.0.0.1:%s: %s", port, type(exc).__name__)
        return
    _CHAT = owned["chat"] = chat
    _log.info("libreoffice chat endpoint listening on 127.0.0.1:%s", chat.port)


def _shutdown_chat(owned: dict[str, Any]) -> None:
    """Unload cleanup for one registration, in this order: close the chat endpoint's ingress,
    stop the runs it forwarded and watch them settle within one time budget while the tool fence
    is still registered, forget the cached secrets, and only then mark the fence gone. Hermes
    disposes registrations in reverse order, so this runs before the ``pre_tool_call`` hook is
    removed. Hermes removes that hook after this returns whatever it reports (its unload swallows
    callback errors and disposes every registration), so a run that could not be confirmed
    stopped is logged here: it may go on without the fence. Idempotent."""
    global _CHAT
    chat = owned.pop("chat", None)
    try:
        if chat is not None and chat.close():
            _log.warning("libreoffice: unloading while sidebar chat runs are unresolved; Hermes removes the "
                         "panel tool fence now, so they may continue without it")
    finally:
        if chat is not None:
            if _CHAT is chat:
                _CHAT = None
            _TOKEN.clear()
            _API_KEY.clear()
        owned["fence"] = False


# Sessions opened by the sidebar panel (their id is minted by the panel in this shape) may use
# this plugin's tools and the read-only skill and tool-lookup helpers, and nothing else: the
# pairing token that opens a panel chat must not become a way to run a shell or read files.
_PANEL_SESSION = re.compile(r"^lo-[0-9a-f]{32}$")
_PANEL_HELPER_TOOLS = frozenset({"skill_view", "skills_list", "tool_search", "tool_describe"})
_PANEL_BLOCK_MESSAGE = ("This tool is not available in a chat started from the LibreOffice panel, which can only "
                        "use the LibreOffice document tools. Tell the user to ask in Hermes itself for anything else.")
# The turn's session identity Hermes binds for the API server's run (documented public plugin
# API: gateway.session_context.get_session_env). The canonical session owner binds the
# conversation's root id as the chat id and in the route key, which survives compression.
_SESSION_CONTEXT_NAMES = ("HERMES_SESSION_CHAT_ID", "HERMES_SESSION_KEY")


def _session_context_ids() -> list[str]:
    try:
        from gateway.session_context import get_session_env
    except Exception:   # noqa: BLE001 - outside a Hermes process there is no turn context
        return []
    ids: list[str] = []
    for name in _SESSION_CONTEXT_NAMES:
        try:
            value = get_session_env(name, "")
        except Exception:   # noqa: BLE001 - a missing identity must never break other chats
            continue
        if isinstance(value, str) and value:
            ids.extend((value, value.rsplit(":", 1)[-1]))   # a route key ends with the chat id
    return ids


def _is_panel_turn(session_id: Any, conversation_id: Any) -> bool:
    """A turn belongs to a panel chat when any identity Hermes gives it is a panel id: the
    physical ``session_id`` (rotates on compression), the compression-stable ``conversation_id``
    (Hermes 0.23.1+), or the session identity bound for the turn."""
    candidates = [session_id, conversation_id, *_session_context_ids()]
    return any(isinstance(value, str) and _PANEL_SESSION.fullmatch(value) for value in candidates)


def _panel_tool_fence(tool_name: str = "", args: Any = None, session_id: str = "", conversation_id: str = "",
                      **_kwargs: Any) -> Any:
    """``pre_tool_call`` hook: veto every tool outside the LibreOffice set in a panel session."""
    if not _is_panel_turn(session_id, conversation_id):
        return None
    allowed = set(_SCHEMA_BY_NAME) | _PANEL_HELPER_TOOLS
    names: list[Any] = [tool_name]
    if tool_name == "tool_call":   # the generic dispatcher: every inner call must be allowed
        calls = args.get("calls") if isinstance(args, dict) else None
        names = [call.get("name") if isinstance(call, dict) else None for call in calls] if isinstance(calls, list) and calls else [None]
    if all(isinstance(name, str) and name in allowed for name in names):
        return None
    return {"action": "block", "message": _PANEL_BLOCK_MESSAGE}


def _result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error(exc: Exception) -> str:
    if isinstance(exc, BridgeError):
        category, message = exc.category, str(exc)
    elif isinstance(exc, RangeValidationError):
        category, message = "Validation", str(exc)
    else:
        category, message = "Validation", "LibreOffice plugin rejected an unexpected local error"
    return _result({"ok": False, "error": {"category": category, "message": message}})


def _exact(args: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    if not required <= set(args) or not set(args) <= required | optional:
        raise RangeValidationError("tool arguments are invalid")


def _validate_range_target(args: dict[str, Any]) -> None:
    validate_document_id(args.get("document_id"))
    validate_sheet(args.get("sheet"))
    parse_a1(args.get("address"))


def _undo(client: BridgeClient, document_id: str, steps: int) -> str:
    """Undo up to ``steps`` Hermes steps, one bridge request each, newest first.

    Each request is a mutation sent once and never retried. The bridge refuses (``not_started``)
    a step that is not a Hermes change or whose document cannot be confirmed as the focused
    window, so the loop stops at the first error and reports the steps that were undone.
    """
    undone: list[dict[str, Any]] = []
    document: Any = None
    for _ in range(steps):
        try:
            data = client.invoke("undo", {"document_id": document_id, "hermes_only": True}, mutation=True)
        except BridgeError as exc:
            if not undone:
                raise
            failed = json.loads(_error(exc))
            failed["undone"] = undone
            return _result(failed)
        undone.append({"title": data.get("title"), "activated_document": data.get("activated_document")})
        document = data.get("document", document)
    return _result({"undone": len(undone), "steps": undone, "document": document})


def _handler_for(name: str, port: int) -> Callable[..., str]:
    def handler(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            if not isinstance(args, dict):
                raise RangeValidationError("tool arguments must be an object")
            _TOKEN.refresh()   # a tool call runs in the profile's scope; the chat endpoint's threads do not
            _API_KEY.refresh()
            client = BridgeClient(port, _secret())
            if name == "calc_get_overview":
                _exact(args, set())
                data = client.invoke("get_overview", {})
            elif name == "calc_get_range":
                _exact(args, {"document_id", "sheet", "address"})
                _validate_range_target(args)
                data = client.invoke("get_range", args)
            elif name == "calc_set_range":
                _exact(args, {"document_id", "sheet", "address", "values"}, {"if_match"})
                _validate_range_target(args)
                validate_matrix(args["address"], args["values"])
                if_match = args.get("if_match")
                if if_match is not None and (
                    not isinstance(if_match, str) or len(if_match) != 64
                    or any(char not in "0123456789abcdef" for char in if_match)
                ):
                    raise RangeValidationError("if_match must be a lowercase SHA-256 fingerprint")
                data = client.invoke("set_range", args, mutation=True)
            elif name == "calc_format_range":
                _exact(args, {"document_id", "sheet", "address", "format"})
                _validate_range_target(args)
                validate_format(args["format"])
                data = client.invoke("format_range", args, mutation=True)
            elif name == "calc_clear_range":
                _exact(args, {"document_id", "sheet", "address"})
                _validate_range_target(args)
                data = client.invoke("clear_range", args, mutation=True)
            elif name == "calc_create_sheet":
                _exact(args, {"document_id", "name"})
                validate_document_id(args.get("document_id"))
                validate_sheet(args.get("name"))
                data = client.invoke("create_sheet", args, mutation=True)
            elif name == "calc_execute_plan":
                _exact(args, {"document_id", "plan"})
                validate_document_id(args.get("document_id"))
                plan = validate_plan(args.get("plan"))
                data = client.invoke("execute_plan", args, mutation=plan["outcome"] == "execute")
            elif name == "writer_get_text":
                _validate_schema_args(name, args)
                validate_document_id(args.get("document_id"))
                data = client.invoke("writer_get_text", args)
            elif name == "writer_insert_text":
                _exact(args, {"document_id", "text", "position"}, {"as_paragraph"})
                validate_document_id(args.get("document_id"))
                if not isinstance(args.get("text"), str) or not args["text"] or len(args["text"]) > 50000 or args.get("position") not in ("start", "end"):
                    raise RangeValidationError("writer_insert_text arguments are invalid")
                if not isinstance(args.get("as_paragraph", False), bool):
                    raise RangeValidationError("as_paragraph must be true or false")
                data = client.invoke("writer_insert_text", args, mutation=True)
            elif name == "libreoffice_undo":
                _exact(args, {"document_id"}, {"steps"})
                validate_document_id(args.get("document_id"))
                steps = args.get("steps", 1)
                if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_UNDO_STEPS:
                    raise RangeValidationError(f"steps must be an integer from 1 to {MAX_UNDO_STEPS}")
                return _undo(client, args["document_id"], steps)
            elif name == "impress_list_slides":
                _exact(args, {"document_id"})
                validate_document_id(args.get("document_id"))
                data = client.invoke("impress_list_slides", args)
            elif name == "calc_import_csv":
                _exact(args, {"document_id", "sheet", "start_cell", "csv_text"}, {"delimiter", "typing"})
                validate_document_id(args["document_id"])
                validate_sheet(args["sheet"])
                matrix = parse_csv(args["csv_text"], args.get("delimiter", ","), args.get("typing", "text"))
                address = target_address(args["start_cell"], len(matrix), len(matrix[0]))
                data = client.invoke("calc_write_literal", {"document_id": args["document_id"], "sheet": args["sheet"],
                                                            "address": address, "values": matrix}, mutation=True)
            elif name in _DIRECT_TOOLS:
                _validate_schema_args(name, args)
                data = client.invoke(name, args, mutation=_DIRECT_TOOLS[name])
            else:
                raise RangeValidationError("unsupported LibreOffice tool")
            return _result(data)
        except (BridgeError, RangeValidationError) as exc:
            return _error(exc)
        except Exception as exc:   # a local defect (for example in argument checking) is an error result, never a crash
            _log.warning("libreoffice tool %s: unexpected local error %s", name, type(exc).__name__)
            return _error(exc)

    return handler


def register(ctx: Any) -> None:
    """Register all declared LibreOffice tools through public plugin APIs.

    Order matters: tools and skills, then the panel tool fence, then the unload cleanup, and the
    sidebar chat endpoint last. Hermes disposes registrations in reverse order, so on unload the
    cleanup closes the endpoint and stops its runs before the fence is removed. Without both a
    hook and an unload API the endpoint is not started at all. A failed registration runs the
    same cleanup before the error propagates.
    """
    owned: dict[str, Any] = {"chat": None, "fence": False}
    try:
        port = ctx.get_config("bridge_port", default=8660)
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            port = 8660
        for schema in _SCHEMAS:
            name = schema["name"]
            ctx.register_tool(
                name=name,
                toolset=_TOOLSET,
                schema=schema,
                handler=_handler_for(name, port),
                check_fn=None,
                override=False,
                description=schema.get("description", ""),
            )
        # Guidance skills bundled with the plugin, read-only, loaded as 'atlas-sidecar-libreoffice:<name>'.
        if hasattr(ctx, "register_skill"):
            for skill, description in _SKILLS.items():
                ctx.register_skill(skill, Path(__file__).parent / "skills" / skill / "SKILL.md", description=description)
        if callable(getattr(ctx, "register_hook", None)):
            ctx.register_hook("pre_tool_call", _panel_tool_fence)
            owned["fence"] = True
        if not (owned["fence"] and callable(getattr(ctx, "on_unload", None))):
            return   # no fence or no unload cleanup: the chat endpoint must not run

        def close_sidebar_chat() -> None:
            _shutdown_chat(owned)

        ctx.on_unload(close_sidebar_chat)
        _start_chat(ctx, owned)
    except BaseException:
        _shutdown_chat(owned)
        raise
