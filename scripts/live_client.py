#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Stdlib client for the in-process LibreOffice bridge."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extension" / "python"))
import private_files  # noqa: E402  (owner/mode checks on POSIX, owner/DACL checks on Windows)

MAX_RESPONSE = 2 * 1024 * 1024


class ClientError(RuntimeError):
    pass


def read_token(path: str) -> str:
    token_path = Path(path).expanduser()
    try:
        # The same check the sidebar uses: POSIX mode bits cannot judge a Windows DACL.
        raw = private_files.read_private_bytes(token_path, 4096)
    except (OSError, RuntimeError, ValueError):
        raise ClientError("token file is missing or insecure") from None
    token = raw.decode("utf-8").strip()
    if len(token.encode("utf-8")) < 32:
        raise ClientError("token is invalid")
    return token


def invoke(port: int, token_path: str, op: str, args: dict, timeout: float = 8.0) -> dict:
    token = read_token(token_path)
    body = json.dumps({"op": op, "args": args}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/invoke",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Hermes-Plugin-Key": token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_RESPONSE + 1)
        try:
            detail = json.loads(data.decode("utf-8"))
        except Exception:
            detail = {"error": f"HTTP {exc.code}"}
        raise ClientError(json.dumps(detail, separators=(",", ":"))) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClientError("LibreOffice bridge is unavailable") from exc
    if len(data) > MAX_RESPONSE:
        raise ClientError("bridge response is too large")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ClientError("bridge returned a non-object response")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8660)
    parser.add_argument("--token-file", default=os.environ.get("LIBREOFFICE_BRIDGE_TOKEN_FILE"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    sub.add_parser("documents")
    get_parser = sub.add_parser("get")
    get_parser.add_argument("document_id")
    get_parser.add_argument("sheet")
    get_parser.add_argument("address")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("document_id")
    set_parser.add_argument("sheet")
    set_parser.add_argument("address")
    set_parser.add_argument("values_json")
    set_parser.add_argument("--if-match")
    undo_parser = sub.add_parser("undo")
    undo_parser.add_argument("document_id")
    call_parser = sub.add_parser("call", help="any bridge operation: call OP '{\"document_id\": ...}'")
    call_parser.add_argument("op")
    call_parser.add_argument("args_json", nargs="?", default="{}")
    options = parser.parse_args()
    if not options.token_file:
        parser.error("--token-file or LIBREOFFICE_BRIDGE_TOKEN_FILE is required")
    if options.command == "health":
        result = invoke(options.port, options.token_file, "health", {})
    elif options.command == "documents":
        result = invoke(options.port, options.token_file, "list_documents", {})
    elif options.command == "get":
        result = invoke(options.port, options.token_file, "get_range", {
            "document_id": options.document_id, "sheet": options.sheet, "address": options.address,
        })
    elif options.command == "set":
        args = {
            "document_id": options.document_id,
            "sheet": options.sheet,
            "address": options.address,
            "values": json.loads(options.values_json),
        }
        if options.if_match:
            args["if_match"] = options.if_match
        result = invoke(options.port, options.token_file, "set_range", args)
    elif options.command == "call":
        args = json.loads(options.args_json)
        if not isinstance(args, dict):
            parser.error("args_json must be a JSON object")
        result = invoke(options.port, options.token_file, options.op, args, timeout=20.0)
    else:
        result = invoke(options.port, options.token_file, "undo", {"document_id": options.document_id})
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
