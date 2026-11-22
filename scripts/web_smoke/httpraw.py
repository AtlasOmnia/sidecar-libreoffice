# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Raw HTTP for probes (exact header control, duplicates allowed) and a reference R18 launcher
client written from docs/web-protocol.md sections 3 and 4."""
from __future__ import annotations

import hmac
import http.client
import json
import secrets
import socket
from typing import Any, Optional, Sequence

try:
    from . import contract  # type: ignore
except ImportError:
    import contract  # type: ignore


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes, elapsed: float) -> None:
        self.status, self.headers, self.body, self.elapsed = status, headers, body, elapsed

    def header(self, name: str) -> Optional[str]:
        values = self.all(name)
        return values[0] if values else None

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    @property
    def code(self) -> Optional[str]:
        data = self.json()
        return data.get("code") if isinstance(data, dict) else None

    def __repr__(self) -> str:
        return f"<{self.status} {self.code or ''}>"


def request(port: int, method: str, target: str, headers: Sequence[tuple[str, str]] = (), body: Optional[bytes] = None,
            *, host: Optional[str] = "default", timeout: float = 40.0, content_length: Optional[str] = "auto") -> Response:
    """One request with exactly the given headers. ``host='default'`` sends ``127.0.0.1:<port>``;
    None sends no Host header. ``content_length='auto'`` sends the body length when there is a body."""
    import time
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    start = time.monotonic()
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        if host == "default":
            conn.putheader("Host", f"127.0.0.1:{port}")
        elif host is not None:
            conn.putheader("Host", host)
        for name, value in headers:
            conn.putheader(name, value)
        if body is not None and content_length == "auto":
            conn.putheader("Content-Length", str(len(body)))
        elif content_length not in ("auto", None):
            conn.putheader("Content-Length", content_length)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        return Response(resp.status, list(resp.getheaders()), data, time.monotonic() - start)
    finally:
        conn.close()


def jbody(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class NotYourHermes(Exception):
    """Proof or response MAC wrong, missing field, or a non-200 hello (section 3.1)."""


class LauncherClient:
    """The launcher side of R18. Never sends the pairing token; verifies every answer's MAC."""

    def __init__(self, port: int, token: str) -> None:
        self.port, self.token = port, token
        self.server_nonce: Optional[str] = None
        self.key: Optional[bytes] = None
        self.counter = 0
        self.hello_answer: Optional[dict] = None

    def hello(self) -> dict:
        client_nonce = contract.b64u(secrets.token_bytes(32))
        resp = request(self.port, "POST", "/launcher/hello", [("Content-Type", "application/json")],
                       jbody({"v": 1, "client_nonce": client_nonce}))
        data = resp.json()
        if resp.status != 200 or not isinstance(data, dict):
            raise NotYourHermes(f"hello answered {resp.status} {resp.code}")
        nonce, proof = data.get("server_nonce"), data.get("server_proof")
        if not (isinstance(nonce, str) and contract.CODE_RE.fullmatch(nonce) and isinstance(proof, str)):
            raise NotYourHermes("hello answer lacks fields")
        expected = contract.server_proof(self.token, self.port, client_nonce, nonce)
        if not hmac.compare_digest(proof, expected):
            raise NotYourHermes("server proof mismatch")
        self.server_nonce = nonce
        self.key = contract.channel_key(self.token, self.port, client_nonce, nonce)
        self.counter = 0
        self.hello_answer = data
        return data

    def sign(self, method: str, path: str, body: bytes, counter: Optional[int] = None) -> list[tuple[str, str]]:
        assert self.key is not None and self.server_nonce is not None
        n = self.counter + 1 if counter is None else counter
        return [("X-Sidecar-Channel", self.server_nonce), ("X-Sidecar-Counter", str(n)),
                ("X-Sidecar-MAC", contract.request_mac(self.key, method, path, n, body))]

    def call(self, method: str, path: str, payload: Any = None, *, verify: bool = True) -> Response:
        body = b"" if payload is None else jbody(payload)
        headers = self.sign(method, path, body)
        self.counter += 1
        if payload is not None:
            headers.append(("Content-Type", "application/json"))
        resp = request(self.port, method, path, headers, body if payload is not None else None)
        if verify and resp.status != 401:
            mac = resp.all("X-Sidecar-MAC")
            want = contract.response_mac(self.key, self.counter, resp.status, resp.body)
            if len(mac) != 1 or not hmac.compare_digest(mac[0], want):
                raise NotYourHermes("response MAC missing or wrong")
        return resp

    def launch(self, document: dict, host_scheme: Optional[str] = "light", mode: str = "app",
               bounds: Optional[dict] = None, follow: Optional[bool] = None) -> Response:
        if mode == "app" and bounds is None:
            bounds = {"x": 1240, "y": 102, "width": 400, "height": 848}
        payload = {"document": document, "host_scheme": host_scheme, "mode": mode, "bounds": bounds}
        if follow is not None:
            payload["follow"] = follow
        resp = self.call("POST", "/launcher/launch", payload)
        data = resp.json()
        if resp.status == 201 and not contract.launch_url_re(self.port).fullmatch(data.get("url") or ""):
            raise NotYourHermes("launch url does not match the expected shape")
        return resp

    def status(self, launch_id: str) -> Response:
        return self.call("GET", f"/launcher/launch/{launch_id}")

    def release(self, document_id: Optional[str], reason: str) -> Response:
        return self.call("POST", "/launcher/release", {"document_id": document_id, "reason": reason})


def doc(n: int = 1, title: str = "sales.ods", kind: str = "calc") -> dict:
    return {"document_id": f"lo:test:{n}", "title": title, "type": kind}


def open_launch(port: int, token: str, document: dict, **kw: Any) -> tuple[str, str]:
    """hello + launch on a fresh channel -> (launch_id, url)."""
    client = LauncherClient(port, token)
    client.hello()
    resp = client.launch(document, **kw)
    data = resp.json()
    if resp.status != 201:
        raise RuntimeError(f"launch answered {resp.status} {resp.code}")
    return data["launch_id"], data["url"]


def port_refuses(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return False
    except OSError:
        return True
