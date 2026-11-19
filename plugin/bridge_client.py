# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Bounded stdlib HTTP client for the LibreOffice in-process bridge."""
from __future__ import annotations

import http.client
import json
import socket
from typing import Any

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
_ALLOWED_CATEGORIES = {
    "Validation", "NotFound", "Stale", "Auth", "Unavailable", "Timeout",
    "ExecutionUncertain", "Protocol", "Capacity",
}
# Shapes a mutation may report without being uncertain: the bridge says the
# operation never started (cancelled while queued, or failed before its first
# document change).
_NEVER_STARTED_CATEGORIES = {"Timeout", "Unavailable", "Validation", "NotFound", "Stale"}
UNCERTAIN_MESSAGE = (
    "LibreOffice mutation outcome is uncertain; it was sent once and was not retried. "
    "Re-read the document before trying again."
)


class BridgeError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _decode_response(response: http.client.HTTPResponse) -> dict[str, Any]:
    length_text = response.getheader("Content-Length")
    if length_text is not None:
        try:
            declared = int(length_text)
        except ValueError as exc:
            raise BridgeError("Protocol", "LibreOffice bridge returned invalid Content-Length") from exc
        if declared < 0 or declared > MAX_RESPONSE_BYTES:
            raise BridgeError("Capacity", "LibreOffice bridge response exceeds 2 MiB")
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise BridgeError("Capacity", "LibreOffice bridge response exceeds 2 MiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("Protocol", "LibreOffice bridge returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise BridgeError("Protocol", "LibreOffice bridge returned a non-object response")
    return value


class BridgeClient:
    def __init__(self, port: int, token: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise BridgeError("Validation", "LibreOffice bridge port is invalid")
        if not isinstance(token, str) or len(token.encode("utf-8")) < 32:
            raise BridgeError("Auth", "LibreOffice bridge token is missing or invalid")
        self.port = port
        self.token = token
        self.timeout = timeout

    def invoke(self, op: str, arguments: dict[str, Any], *, mutation: bool = False) -> dict[str, Any]:
        """Send one request exactly once; never retry.

        For a mutation, anything that leaves the outcome unknown after the
        request was sent (lost connection, timeout, undecodable or oversize
        reply, a 5xx that is not an explicit never-started shape, a 2xx without
        data) is ``ExecutionUncertain``. 4xx replies keep the bridge's
        category: the bridge only answers 4xx when nothing was changed.
        """
        body = json.dumps({"op": op, "args": arguments}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise BridgeError("Capacity", "LibreOffice bridge request exceeds 1 MiB")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            try:
                connection.connect()
            except (ConnectionRefusedError, socket.gaierror, socket.timeout, TimeoutError, OSError) as exc:
                raise BridgeError("Unavailable", "LibreOffice is not running or its bridge is unavailable") from exc
            try:
                connection.request(
                    "POST", "/invoke", body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "X-Hermes-Plugin-Key": self.token,
                    },
                )
                response = connection.getresponse()
                status = response.status
                value = _decode_response(response)
            except BridgeError as exc:
                if mutation:
                    raise BridgeError("ExecutionUncertain", UNCERTAIN_MESSAGE) from exc
                raise
            except (socket.timeout, TimeoutError, ConnectionResetError, BrokenPipeError, http.client.HTTPException, OSError) as exc:
                if mutation:
                    raise BridgeError("ExecutionUncertain", UNCERTAIN_MESSAGE) from exc
                category = "Timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "Unavailable"
                raise BridgeError(category, "LibreOffice bridge request did not complete") from exc
        finally:
            connection.close()
        if status < 400 and value.get("ok") is True:
            data = value.get("data")
            if isinstance(data, dict):
                return data
            if mutation:
                raise BridgeError("ExecutionUncertain", UNCERTAIN_MESSAGE)
            raise BridgeError("Protocol", "LibreOffice bridge success response is missing data")
        category = value.get("category")
        category = category if isinstance(category, str) and category in _ALLOWED_CATEGORIES else None
        message = str(value.get("error") or f"LibreOffice bridge returned HTTP {status}")
        if mutation and (status >= 500 or status < 400):
            never_started = value.get("state") == "not_started" and category in _NEVER_STARTED_CATEGORIES
            if not never_started:
                raise BridgeError("ExecutionUncertain", UNCERTAIN_MESSAGE)
            raise BridgeError(category, message)
        if category is None:
            if status in (401, 403):
                category = "Auth"
            elif status >= 500:
                category = "Unavailable"
            else:
                category = "Protocol"
        if mutation and category == "ExecutionUncertain":
            raise BridgeError("ExecutionUncertain", UNCERTAIN_MESSAGE)
        raise BridgeError(category, message)
