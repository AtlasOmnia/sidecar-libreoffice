# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Byte formats of docs/web-protocol.md (0.5.0), shared by the stub server and the harness.

Everything here is derived from the frozen contract text, not from any plugin code, and is
checked against the contract's own test vectors (section 3.3) by ``selftest.py``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Optional

# -- section 2: encodings -------------------------------------------------------------------
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")          # launch code, session token, nonces
ID22_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")          # launch id, turn id
COUNTER_RE = re.compile(r"^[1-9][0-9]{0,9}$")         # decimal, no leading zeros
STATIC_REL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
DOCUMENT_TYPES = ("calc", "writer", "impress")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_int(value: object) -> bool:
    """JSON booleans are not integers (section 2)."""
    return isinstance(value, int) and not isinstance(value, bool)


def valid_document(doc: object) -> bool:
    return (isinstance(doc, dict) and set(doc) == {"document_id", "title", "type"}
            and isinstance(doc["document_id"], str) and 1 <= len(doc["document_id"]) <= 512
            and isinstance(doc["title"], str) and len(doc["title"]) <= 1000
            and doc["type"] in DOCUMENT_TYPES)


def valid_rectangle(rect: object) -> bool:
    if not (isinstance(rect, dict) and set(rect) == {"x", "y", "width", "height"}):
        return False
    if not all(is_int(rect[k]) for k in rect):
        return False
    return (-32768 <= rect["x"] <= 32767 and -32768 <= rect["y"] <= 32767
            and 240 <= rect["width"] <= 8192 and 240 <= rect["height"] <= 8192)


# -- section 3: launcher channel (R18) ------------------------------------------------------
def _hmac(key: bytes, *parts: str) -> bytes:
    return hmac.new(key, "\n".join(parts).encode("ascii"), hashlib.sha256).digest()


def server_proof(token: str, port: int, client_nonce: str, server_nonce: str) -> str:
    return b64u(_hmac(token.strip().encode("utf-8"), "sidecar-launcher/1 hello", str(port), client_nonce, server_nonce))


def channel_key(token: str, port: int, client_nonce: str, server_nonce: str) -> bytes:
    return _hmac(token.strip().encode("utf-8"), "sidecar-launcher/1 channel", str(port), client_nonce, server_nonce)


def request_mac(key: bytes, method: str, path: str, counter: int, body: bytes) -> str:
    return b64u(_hmac(key, "sidecar-launcher/1 request", method.upper(), path, str(counter),
                      hashlib.sha256(body).hexdigest()))


def response_mac(key: bytes, counter: int, status: int, body: bytes) -> str:
    return b64u(_hmac(key, "sidecar-launcher/1 response", str(counter), str(status),
                      hashlib.sha256(body).hexdigest()))


VECTORS = {
    "token": "example-pairing-token-0123456789abcdefghijklmnopq",
    "port": 8661,
    "client_nonce": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    "server_nonce": "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8",
    "server_proof": "6gAIEWKHsH5HilYgR4IFZrAzSBZsyxB9kUYyFBW3DZk",
    "channel_key": "49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1",
    "request1_body": ('{"document":{"document_id":"lo:5f0c:1","title":"sales.ods","type":"calc"},'
                      '"host_scheme":"light","mode":"app","bounds":{"x":1240,"y":102,"width":400,"height":848}}'),
    "request1_body_sha256": "3220ea4ed994980c359a102c7b2d2d604bb604de9885a619bedc17373995bbba",
    "request1_mac": "PmCfNapRjDIVbyHqzSrGnkU_mqgxbEr4XLWu0iQocX8",
    "response1_body": '{"ok":true}',
    "response1_body_sha256": "4062edaf750fb8074e7e83e0c9028c94e32468a8b6f1614774328ef045150f93",
    "response1_mac": "ZgH8usVq9byBTZB7tp2Fi7PfEpJk8YBIw-EfdEN6FRc",
    "request2_path": "/launcher/launch/AAAAAAAAAAAAAAAAAAAAAA",
    "request2_mac": "pz9hSDdfVxWMTMKc772QPcntSZE8IV0T3jHUv836CuI",
}


def check_vectors() -> list[str]:
    """Recompute every section 3.3 value; returns the names that do not match (empty = all match)."""
    v = VECTORS
    bad = []
    if server_proof(v["token"], v["port"], v["client_nonce"], v["server_nonce"]) != v["server_proof"]:
        bad.append("server_proof")
    key = channel_key(v["token"], v["port"], v["client_nonce"], v["server_nonce"])
    if key.hex() != v["channel_key"]:
        bad.append("channel_key")
    body1 = v["request1_body"].encode()
    if hashlib.sha256(body1).hexdigest() != v["request1_body_sha256"]:
        bad.append("request1_body_sha256")
    if request_mac(key, "POST", "/launcher/launch", 1, body1) != v["request1_mac"]:
        bad.append("request1_mac")
    resp1 = v["response1_body"].encode()
    if hashlib.sha256(resp1).hexdigest() != v["response1_body_sha256"]:
        bad.append("response1_body_sha256")
    if response_mac(key, 1, 201, resp1) != v["response1_mac"]:
        bad.append("response1_mac")
    if request_mac(key, "GET", v["request2_path"], 2, b"") != v["request2_mac"]:
        bad.append("request2_mac")
    if b64u_decode(v["client_nonce"]) != bytes(range(32)) or b64u_decode(v["server_nonce"]) != bytes(range(32, 64)):
        bad.append("nonce_bytes")
    return bad


# -- sections 6, 7: headers -----------------------------------------------------------------
STATIC_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; worker-src 'none'; "
              "manifest-src 'none'; require-trusted-types-for 'script'; trusted-types dompurify")
PERMISSIONS_POLICY = ("accelerometer=(), camera=(), display-capture=(), fullscreen=(), geolocation=(), gyroscope=(), "
                      "hid=(), magnetometer=(), microphone=(), midi=(), payment=(), serial=(), usb=(), "
                      "clipboard-read=(), clipboard-write=(self)")
STATIC_HEADERS = (
    ("Content-Security-Policy", STATIC_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Permissions-Policy", PERMISSIONS_POLICY),
    ("Cache-Control", "no-cache"),
)
API_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Referrer-Policy", "no-referrer"),
)
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

# -- section 1.2: role separation -----------------------------------------------------------
FORBIDDEN = {
    "legacy": ("x-sidecar-session", "x-sidecar-channel", "x-sidecar-counter", "x-sidecar-mac"),   # + Origin, Sec-Fetch-*
    "launcher": ("origin", "x-sidecar-session", "x-hermes-plugin-key"),                         # + Sec-Fetch-*
    "ui": ("x-hermes-plugin-key", "x-sidecar-session", "x-sidecar-channel", "x-sidecar-counter", "x-sidecar-mac"),
    "api": ("x-hermes-plugin-key", "x-sidecar-channel", "x-sidecar-counter", "x-sidecar-mac"),
}

# -- section 11: log labels -----------------------------------------------------------------
LOG_LABELS = ("health", "chat", "launcher-hello", "launcher-launch", "launcher-status", "launcher-release",
              "ui-static", "api-session", "api-state", "api-chat", "api-turn", "api-stop", "api-new-chat",
              "api-close", "method", "unknown")

# -- section 9: machine codes ---------------------------------------------------------------
CODES = {
    "bad_host": 403, "forbidden_credential": 403, "cross_site": 403, "bad_origin": 403,
    "method_not_allowed": 405, "unsupported_media_type": 415, "length_required": 411, "too_large": 413,
    "body_timeout": 408, "bad_request": 400, "not_found": 404, "no_pairing": 503, "launcher_auth": 401,
    "channel_limit": 429, "too_many_launches": 429, "session_cap": 429, "launch_expired": 401,
    "launch_double_use": 401, "no_session": 401, "busy": 409, "unresolved": 409, "too_many": 429,
    "plugin_inactive": 503, "worker_failed": 503, "message_invalid": 400, "unknown_turn": 404,
    "turn_running": 409, "turned_off": 503, "internal": 500, "endpoint_busy": 503, "already_sent": 409,
}
END_REASONS = ("closed", "replaced", "host_gone", "turned_off", "idle", "double_use", "evicted")

# -- section 9.2: page texts ----------------------------------------------------------------
PAGE_TEXTS = {
    None: "Click Open Sidecar in LibreOffice to reconnect.",
    "replaced": "This document is open in a newer Sidecar window. This one is disconnected.",
    "host_gone": "LibreOffice closed this document.",
    "turned_off": "The LibreOffice plugin was turned off in Hermes.",
    "idle": "This window was idle too long. Click Open Sidecar in LibreOffice again.",
    "double_use": "This launch was used by another program; that connection was closed. Click Open Sidecar again.",
    "evicted": ("Too many Sidecar windows are open, so this one was disconnected. "
                "Click Open Sidecar in LibreOffice again."),
}
LAUNCH_EXPIRED_TEXT = ("This launch link was already used or has expired. "
                       "Click Open Sidecar in LibreOffice again.")
WORKING_TEXT = "Hermes is working…"
STOPPING_TEXT = "Stopping…"
APPROVAL_TEXT = ("Hermes asked for an approval that Sidecar cannot give. Press Stop and ask in Hermes itself.")

# -- section 7.3: DOM ids -------------------------------------------------------------------
DOM_IDS = ("app-title", "settings-button", "connection-status", "privacy-badge", "desk-connect",
           "desk-connect-text", "desk-connect-code", "desk-connect-button", "bridge-status", "bridge-session-id",
           "new-chat-button", "messages", "prompt-input", "stop-button", "send-button", "settings-dialog",
           "close-settings-button", "save-settings-button", "bridge-url", "document-line")

# -- section 5: bootstrap file --------------------------------------------------------------
LAUNCH_URL_RE_TEMPLATE = r"^http://127\.0\.0\.1:{port}/ui/index\.html#launch=[A-Za-z0-9_-]{{43}}$"


def launch_url_re(port: int) -> "re.Pattern[str]":
    return re.compile(LAUNCH_URL_RE_TEMPLATE.format(port=port))


def bootstrap_html(url: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9:/._#=-]+", url):
        raise ValueError("launch url has characters outside the bootstrap rule")
    return ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f'<meta http-equiv="refresh" content="0;url={url}"><title>Opening Sidecar…</title>')


def parse_sha256sums(text: str) -> Optional[dict[str, str]]:
    """Section 7.1 format; None when the file breaks the format (then nothing is served)."""
    if not text.endswith("\n"):
        return None
    out: dict[str, str] = {}
    previous = None
    for line in text[:-1].split("\n"):
        m = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not m or not STATIC_REL_RE.fullmatch(m.group(2)):
            return None
        if previous is not None and m.group(2) <= previous:
            return None
        previous = m.group(2)
        out[m.group(2)] = m.group(1)
    return out
