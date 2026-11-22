# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Black-box probe matrix for the 0.5.0 window endpoint (G1 rows, the black-box part of G2,
connection budget, unload). Implementation-agnostic: it speaks only HTTP to a ``targets.Target``
and reads the fake Runs API's call log. Every probe gets a fresh target.

    python3 scripts/web_smoke/probes.py --bundle <ui folder> [--only PREFIX] [--slow] [--json out.json]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract  # noqa: E402
import corpus  # noqa: E402
import httpraw  # noqa: E402
from httpraw import LauncherClient, NotYourHermes, Response, doc, jbody  # noqa: E402

PROBES: list["Probe"] = []


class Skip(Exception):
    pass


class Probe:
    def __init__(self, pid: str, gate: str, title: str, needs: tuple[str, ...], fn: Callable[["Ctx"], None]) -> None:
        self.id, self.gate, self.title, self.needs, self.fn = pid, gate, title, needs, fn


def probe(pid: str, gate: str, title: str, needs: tuple[str, ...] = ()) -> Callable:
    def deco(fn: Callable[["Ctx"], None]) -> Callable[["Ctx"], None]:
        PROBES.append(Probe(pid, gate, title, needs, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, target: Any, slow: bool) -> None:
        self.t = target
        self.slow = slow
        self.checks: list[dict[str, Any]] = []
        self.responses: list[Response] = []
        self.secrets: set[str] = {target.token} if target.token else set()

    # -- recording --
    def check(self, label: str, ok: bool, expected: Any = None, observed: Any = None) -> bool:
        self.checks.append({"label": label, "ok": bool(ok), "expected": expected, "observed": observed})
        return bool(ok)

    def expect(self, label: str, resp: Response, status: int, code: Optional[str] = None) -> bool:
        ok = resp.status == status and (code is None or resp.code == code)
        return self.check(label, ok, f"{status} {code or ''}".strip(), f"{resp.status} {resp.code or ''}".strip())

    def wait_seconds(self, name: str, needed: float, limit: float = 40.0) -> None:
        if needed > limit and not self.slow:
            raise Skip(f"needs {needed:.0f} s of {name}; run with --slow (or a target with short timings)")

    # -- HTTP --
    def req(self, method: str, path: str, headers: list = (), body: Optional[bytes] = None, **kw: Any) -> Response:
        resp = httpraw.request(self.t.port, method, path, list(headers), body, **kw)
        self.responses.append(resp)
        return resp

    @property
    def origin(self) -> str:
        return self.t.origin

    def get_h(self, token: Optional[str]) -> list:
        return [("Sec-Fetch-Site", "same-origin")] + ([("X-Sidecar-Session", token)] if token else [])

    def post_h(self, token: Optional[str]) -> list:
        return ([("Origin", self.origin), ("Sec-Fetch-Site", "same-origin"), ("Content-Type", "application/json")]
                + ([("X-Sidecar-Session", token)] if token else []))

    def get(self, path: str, token: Optional[str], **kw: Any) -> Response:
        return self.req("GET", path, self.get_h(token), **kw)

    def post(self, path: str, payload: Any, token: Optional[str], **kw: Any) -> Response:
        return self.req("POST", path, self.post_h(token), jbody(payload), **kw)

    # -- flows --
    def launcher(self) -> LauncherClient:
        client = LauncherClient(self.t.port, self.t.token)
        client.hello()
        return client

    def launch(self, n: int = 1, **kw: Any) -> tuple[str, str, str]:
        resp = self.launcher().launch(doc(n), **kw)
        if resp.status != 201:
            raise AssertionError(f"launch answered {resp.status} {resp.code}")
        data = resp.json()
        code = data["url"].split("#launch=", 1)[1]
        self.secrets.add(code)
        return data["launch_id"], data["url"], code

    def exchange(self, code: str) -> Response:
        return self.post("/api/session", {"launch": code}, None)

    def session(self, n: int = 1, **kw: Any) -> tuple[str, dict]:
        _lid, _url, code = self.launch(n, **kw)
        resp = self.exchange(code)
        if resp.status != 200:
            raise AssertionError(f"exchange answered {resp.status} {resp.code}")
        data = resp.json()
        self.secrets.add(data["session"])
        return data["session"], data

    def chat(self, token: str, message: str) -> Response:
        return self.post("/api/chat", {"message": message}, token)

    def poll(self, token: str, turn_id: str, seq: int, have: int = 0, **kw: Any) -> Response:
        return self.get(f"/api/turn?id={turn_id}&seq={seq}&have={have}", token, **kw)

    def until_done(self, token: str, turn: dict, limit: float = 40.0) -> dict:
        seq, text, deadline = turn["seq"], "", time.monotonic() + limit
        while time.monotonic() < deadline:
            resp = self.poll(token, turn["id"], seq, len(text))
            data = resp.json()
            if resp.status != 200:
                raise AssertionError(f"turn poll answered {resp.status} {resp.code}")
            view = data["turn"]
            if view["text_from"] == len(text):
                text += view["text"]
            seq = view["seq"]
            if view["state"] == "done":
                view["text_all"] = text
                return view
        raise AssertionError("turn did not finish in time")

    def chat_id_of(self, token: str) -> Optional[str]:
        """The fake Runs API session id that belongs to this page session (via its last run)."""
        fake = self.t.fake
        if fake is None:
            return None
        with fake.lock:
            return fake.calls[-1]["session_id"] if fake.calls else None

    def wait_stop(self, chat_id: str, limit: float = 5.0) -> bool:
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.t.fake.calls_for(chat_id, "stop"):
                return True
            time.sleep(0.05)
        return False


def run_probe(p: Probe, make_target: Callable[[], Any], slow: bool) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"id": p.id, "gate": p.gate, "title": p.title}
    target = None
    ctx = None
    try:
        target = make_target()
        missing = [n for n in p.needs if n not in target.capabilities]
        if missing:
            raise Skip("target lacks " + ", ".join(missing))
        ctx = Ctx(target, slow)
        p.fn(ctx)
        # Global rules on everything this probe saw (section 1, 1.1).
        cookies = [r for r in ctx.responses if r.all("Set-Cookie")]
        ctx.check("no Set-Cookie on any response", not cookies, 0, len(cookies))
        cors = [r for r in ctx.responses if any(k.lower().startswith("access-control-") for k, _ in r.headers)]
        ctx.check("no CORS headers on any response", not cors, 0, len(cors))
        conn = [r for r in ctx.responses if (r.header("Connection") or "").lower() != "close"]
        ctx.check("every response closes its connection", not conn, 0, len(conn))
        result["status"] = "pass" if all(c["ok"] for c in ctx.checks) else "fail"
    except Skip as exc:
        result["status"], result["note"] = "skip", str(exc)
    except Exception as exc:  # noqa: BLE001 - a probe crash is a failed probe, with its trace
        result["status"] = "error"
        result["note"] = f"{type(exc).__name__}: {exc}"
        result["trace"] = traceback.format_exc()[-1500:]
    finally:
        if target is not None:
            try:
                target.close()
            except Exception:  # noqa: BLE001
                pass
    result["checks"] = ctx.checks if ctx else []
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


# ====================================================================================== G1: Host
@probe("g1.host.variants", "G1", "Host must be exactly 127.0.0.1:<port> on every new route family")
def _(c: Ctx) -> None:
    p = c.t.port
    variants = {"missing": None, "localhost": f"localhost:{p}", "rebinding": f"sidecar.example:{p}",
                "no-port": "127.0.0.1", "other-port": "127.0.0.1:1", "ipv6": f"[::1]:{p}",
                "trailing-dot": f"127.0.0.1.:{p}", "upper": f"LOCALHOST:{p}", "ip-decimal": f"2130706433:{p}"}
    routes = [("GET", "/api/state", c.get_h(None), None), ("POST", "/launcher/hello", [("Content-Type", "application/json")],
              jbody({"v": 1, "client_nonce": "A" * 43})), ("GET", "/ui/index.html", [], None),
              ("POST", "/api/session", c.post_h(None), jbody({"launch": "A" * 43}))]
    for name, host in variants.items():
        for method, path, headers, body in routes:
            c.expect(f"{name} {method} {path}", c.req(method, path, headers, body, host=host), 403, "bad_host")
    good = f"127.0.0.1:{p}"
    for method, path, headers, body in routes:
        resp = c.req(method, path, [("Host", good)] + headers, body, host=good)
        c.expect(f"duplicate Host {method} {path}", resp, 403, "bad_host")


@probe("g1.host.legacy", "G1", "Legacy /health and /chat keep the 0.4.0 Host refusal")
def _(c: Ctx) -> None:
    resp = c.req("GET", "/health", [("X-Hermes-Plugin-Key", c.t.token)], host=f"localhost:{c.t.port}")
    c.check("GET /health with localhost Host -> 403", resp.status == 403, 403, resp.status)
    resp = c.req("GET", "/health", [("X-Hermes-Plugin-Key", c.t.token)])
    c.check("GET /health with the pairing token -> 200 (legacy kept)", resp.status == 200, 200, resp.status)


# ============================================================================ G1: Origin / fetch
@probe("g1.origin.post", "G1", "POST /api/* needs exactly one Origin equal to ORIGIN and same-origin fetch metadata")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    body = jbody({"message": "hello"})
    base = [("Content-Type", "application/json"), ("X-Sidecar-Session", tok)]
    cases = {
        "absent": ([("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "foreign": ([("Origin", "https://evil.example"), ("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "null": ([("Origin", "null"), ("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "duplicate": ([("Origin", c.origin), ("Origin", c.origin), ("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "other-loopback-port": ([("Origin", "http://127.0.0.1:1"), ("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "localhost": ([("Origin", f"http://localhost:{c.t.port}"), ("Sec-Fetch-Site", "same-origin")], "bad_origin"),
        "trailing-slash": ([("Origin", c.origin + "/")], "bad_origin"),
        "same-site": ([("Origin", c.origin), ("Sec-Fetch-Site", "same-site")], "cross_site"),
        "cross-site": ([("Origin", c.origin), ("Sec-Fetch-Site", "cross-site")], "cross_site"),
        "none": ([("Origin", c.origin), ("Sec-Fetch-Site", "none")], "cross_site"),
    }
    for name, (headers, code) in cases.items():
        c.expect(f"POST /api/chat Origin {name}", c.req("POST", "/api/chat", base + headers, body), 403, code)
    resp = c.req("POST", "/api/chat", base + [("Origin", c.origin)], body)
    c.expect("POST with exact Origin and no Sec-Fetch-Site is accepted", resp, 202)


@probe("g1.origin.get", "G1", "GET /api/* needs same-origin Sec-Fetch-Site when Origin is absent")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    s = [("X-Sidecar-Session", tok)]
    c.expect("no Origin, no Sec-Fetch-Site", c.req("GET", "/api/state", s), 403, "bad_origin")
    c.expect("Sec-Fetch-Site same-site", c.req("GET", "/api/state", s + [("Sec-Fetch-Site", "same-site")]), 403, "cross_site")
    c.expect("Sec-Fetch-Site cross-site", c.req("GET", "/api/state", s + [("Sec-Fetch-Site", "cross-site")]), 403, "cross_site")
    c.expect("Sec-Fetch-Site none", c.req("GET", "/api/state", s + [("Sec-Fetch-Site", "none")]), 403, "cross_site")
    c.expect("foreign Origin with same-origin", c.req("GET", "/api/state", s + [
        ("Origin", "https://evil.example"), ("Sec-Fetch-Site", "same-origin")]), 403, "bad_origin")
    c.expect("exact Origin only", c.req("GET", "/api/state", s + [("Origin", c.origin)]), 200)
    c.expect("same-origin only", c.req("GET", "/api/state", s + [("Sec-Fetch-Site", "same-origin")]), 200)


@probe("g1.origin.exchange", "G1", "A refused exchange (foreign Origin) does not spend the launch code")
def _(c: Ctx) -> None:
    _lid, _url, code = c.launch()
    resp = c.req("POST", "/api/session", [("Origin", "https://evil.example"), ("Content-Type", "application/json")],
                 jbody({"launch": code}))
    c.expect("foreign Origin exchange", resp, 403, "bad_origin")
    c.expect("the real page still exchanges", c.exchange(code), 200)


# ========================================================================= G1: role separation
@probe("g1.roles", "G1", "Each route family refuses the other families' credential headers (403 forbidden_credential)")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    hello = jbody({"v": 1, "client_nonce": contract.b64u(secrets.token_bytes(32))})
    for name, value in (("X-Hermes-Plugin-Key", c.t.token), ("X-Sidecar-Channel", "A" * 43),
                        ("X-Sidecar-Counter", "1"), ("X-Sidecar-MAC", "A" * 43)):
        c.expect(f"/api/state with {name}", c.req("GET", "/api/state", c.get_h(tok) + [(name, value)]),
                 403, "forbidden_credential")
    for name, value in (("Origin", c.origin), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "cors"),
                        ("Sec-Fetch-Dest", "empty"), ("X-Sidecar-Session", tok), ("X-Hermes-Plugin-Key", c.t.token)):
        c.expect(f"/launcher/hello with {name}", c.req("POST", "/launcher/hello",
                 [("Content-Type", "application/json"), (name, value)], hello), 403, "forbidden_credential")
    for name, value in (("X-Hermes-Plugin-Key", c.t.token), ("X-Sidecar-Session", tok), ("X-Sidecar-MAC", "A" * 43),
                        ("X-Sidecar-Channel", "A" * 43), ("X-Sidecar-Counter", "1")):
        c.expect(f"/ui/index.html with {name}", c.req("GET", "/ui/index.html", [(name, value)]),
                 403, "forbidden_credential")
    legacy = [("X-Hermes-Plugin-Key", c.t.token)]
    for name, value in (("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "cors"), ("X-Sidecar-Session", tok),
                        ("X-Sidecar-Channel", "A" * 43), ("X-Sidecar-Counter", "1"), ("X-Sidecar-MAC", "A" * 43)):
        resp = c.req("GET", "/health", legacy + [(name, value)])
        c.check(f"/health with {name} -> 403", resp.status == 403, 403, resp.status)
    resp = c.req("GET", "/health", legacy + [("Origin", c.origin)])
    c.check("/health with Origin -> 403 (0.4.0 refusal)", resp.status == 403, 403, resp.status)
    resp = c.req("POST", "/chat", legacy + [("Content-Type", "application/json"), ("X-Sidecar-Session", tok)], jbody({}))
    c.check("/chat with X-Sidecar-Session -> 403", resp.status == 403, 403, resp.status)
    c.expect("pairing token as a session token", c.get("/api/state", c.t.token), 401, "no_session")
    resp = c.req("GET", "/health", [("X-Hermes-Plugin-Key", tok)])
    c.check("session token as the pairing token on /health -> 401", resp.status == 401, 401, resp.status)


@probe("g1.cookies", "G1", "No cookie is read or set; the session travels only in X-Sidecar-Session")
def _(c: Ctx) -> None:
    tok, data = c.session()
    c.expect("Cookie with the session token only", c.req("GET", "/api/state", [
        ("Sec-Fetch-Site", "same-origin"), ("Cookie", f"session={tok}; X-Sidecar-Session={tok}")]), 401, "no_session")
    c.check("exchange answer has the session", isinstance(data.get("session"), str) and bool(
        contract.CODE_RE.fullmatch(data["session"])), "43 chars", len(data.get("session", "")))


@probe("g1.session.header", "G1", "Missing, malformed, duplicate or unknown session headers answer 401 no_session")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    for name, headers in (("missing", []), ("malformed", [("X-Sidecar-Session", "abc")]),
                          ("unknown", [("X-Sidecar-Session", contract.b64u(secrets.token_bytes(32)))]),
                          ("duplicate", [("X-Sidecar-Session", tok), ("X-Sidecar-Session", tok)])):
        resp = c.req("GET", "/api/state", [("Sec-Fetch-Site", "same-origin")] + headers)
        c.expect(f"{name} session header", resp, 401, "no_session")
        c.check(f"{name}: ended is null", (resp.json() or {}).get("ended", "absent") is None, None,
                (resp.json() or {}).get("ended", "absent"))


# ================================================================================ G1: methods
@probe("g1.methods", "G1", "OPTIONS, HEAD and other methods get JSON 405 with Allow and no CORS; unknown paths 404")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    pre = [("Origin", "https://evil.example"), ("Access-Control-Request-Method", "POST"),
           ("Access-Control-Request-Headers", "x-sidecar-session,content-type")]
    for method, path, allow in (("OPTIONS", "/api/state", "GET"), ("OPTIONS", "/api/chat", "POST"),
                                ("HEAD", "/api/state", "GET"), ("PUT", "/api/chat", "POST"),
                                ("DELETE", "/launcher/hello", "POST"), ("GET", "/api/chat", "POST"),
                                ("POST", "/api/state", "GET"), ("OPTIONS", "/ui/index.html", "GET"),
                                ("HEAD", "/ui/index.html", "GET"), ("POST", "/ui/index.html", "GET"),
                                ("GET", "/launcher/launch", "POST"), ("PATCH", "/api/turn/stop", "POST"),
                                ("OPTIONS", "/launcher/launch", "POST")):
        headers = pre if method == "OPTIONS" else c.get_h(tok)
        resp = c.req(method, path, headers)
        ok = resp.status == 405 and resp.header("Allow") == allow and (method == "HEAD" or resp.code == "method_not_allowed")
        c.check(f"{method} {path} -> 405 Allow {allow}", ok, f"405 {allow}", f"{resp.status} {resp.header('Allow')} {resp.code}")
    for path in ("/api/nope", "/", "/ui", "/ui/", "/favicon.ico", "/apple-touch-icon.png",
                 "/apple-touch-icon-precomposed.png", "/launcher/nope", "/api/state/x", "/api/state?x=1"):
        c.expect(f"GET {path}", c.req("GET", path, c.get_h(tok)), 404, "not_found")
    resp = c.req("OPTIONS", "/health", [])
    c.check("OPTIONS /health keeps 0.4.0's 501", resp.status == 501, 501, resp.status)


# =================================================================================== G1: body
@probe("g1.body", "G1", "POST bodies: JSON type, Content-Length, size limits, exact keys")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    h = [("Origin", c.origin), ("Sec-Fetch-Site", "same-origin"), ("X-Sidecar-Session", tok)]
    ok_body = jbody({"message": "hi"})
    c.expect("text/plain", c.req("POST", "/api/chat", h + [("Content-Type", "text/plain")], ok_body), 415, "unsupported_media_type")
    c.expect("no Content-Type", c.req("POST", "/api/chat", h, ok_body), 415, "unsupported_media_type")
    c.expect("duplicate Content-Type", c.req("POST", "/api/chat", h + [("Content-Type", "application/json")] * 2, ok_body),
             415, "unsupported_media_type")
    c.expect("no Content-Length", c.req("POST", "/api/chat", h + [("Content-Type", "application/json")], None,
                                        content_length=None), 411, "length_required")
    c.expect("/api/session 16 KiB + 1", c.req("POST", "/api/session", c.post_h(None),
             b"{" + b" " * (16 * 1024 - 1) + b"}"), 413, "too_large")
    c.expect("/api/new-chat 16 KiB + 1", c.req("POST", "/api/new-chat", c.post_h(tok),
             b"{" + b" " * (16 * 1024 - 1) + b"}"), 413, "too_large")
    c.expect("/api/chat 64 KiB + 1", c.req("POST", "/api/chat", c.post_h(tok),
             b"{" + b" " * (64 * 1024 - 1) + b"}"), 413, "too_large")
    c.expect("/api/chat 20 KiB body, 8001 characters", c.post("/api/chat", {"message": "x" * 8001}, tok),
             400, "message_invalid")
    c.expect("extra key", c.post("/api/chat", {"message": "hi", "session_id": "lo-" + "0" * 32}, tok), 400, "bad_request")
    c.expect("JSON array", c.req("POST", "/api/chat", c.post_h(tok), b"[]"), 400, "bad_request")
    c.expect("not JSON", c.req("POST", "/api/chat", c.post_h(tok), b"{nope"), 400, "bad_request")
    c.expect("bad UTF-8", c.req("POST", "/api/chat", c.post_h(tok), b'{"message":"\xff"}'), 400, "bad_request")
    c.expect("new-chat with a key", c.post("/api/new-chat", {"x": 1}, tok), 400, "bad_request")
    for value in ("", "   \n\t "):
        c.expect(f"message {value!r}", c.post("/api/chat", {"message": value}, tok), 400, "message_invalid")
    c.expect("message not a string", c.post("/api/chat", {"message": 5}, tok), 400, "message_invalid")
    resp = c.req("POST", "/api/chat", h + [("Content-Type", "application/json; charset=utf-8")], ok_body)
    c.expect("charset parameter allowed", resp, 202)


@probe("g1.body.deadline", "G1", "A body that never arrives whole answers within the one read deadline")
def _(c: Ctx) -> None:
    import socket
    sock = socket.create_connection(("127.0.0.1", c.t.port), timeout=20)
    start = time.monotonic()
    head = (f"POST /api/session HTTP/1.1\r\nHost: 127.0.0.1:{c.t.port}\r\nOrigin: {c.origin}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"launch\":")
    sock.sendall(head.encode())
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    sock.close()
    elapsed = time.monotonic() - start
    status = int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/1.") else 0
    c.check("answered 408 (or 400)", status in (408, 400), "408", status)
    c.check("answered within 8 s", elapsed < 8, "< 8 s", round(elapsed, 2))


# ============================================================================ G1: launcher R18
@probe("g1.r18.vectors", "G1", "Harness reproduces the section 3.3 test vectors (reference for both sides)")
def _(c: Ctx) -> None:
    bad = contract.check_vectors()
    c.check("all 3.3 values recomputed", not bad, [], bad)


@probe("g1.r18.hello", "G1", "hello: proof verifies, wrong key is detected, bad bodies refused, no pairing -> 503")
def _(c: Ctx) -> None:
    client = LauncherClient(c.t.port, c.t.token)
    data = client.hello()
    c.check("proof verified by the reference launcher", True, "verified", "verified")
    c.check("hello answer shape", data.get("ok") is True and data.get("v") == 1 and isinstance(data.get("chat"), dict)
            and data.get("expires_in") == int(c.t.timing["channel_ttl"]), "ok v=1 chat expires_in", data.get("expires_in"))
    impostor = LauncherClient(c.t.port, "not-the-pairing-token")
    try:
        impostor.hello()
        c.check("a launcher with another token rejects the proof", False, "NotYourHermes", "accepted")
    except NotYourHermes:
        c.check("a launcher with another token rejects the proof", True)
    h = [("Content-Type", "application/json")]
    nonce = contract.b64u(secrets.token_bytes(32))
    for name, payload in (("v 2", {"v": 2, "client_nonce": nonce}), ("v true", {"v": True, "client_nonce": nonce}),
                          ("short nonce", {"v": 1, "client_nonce": nonce[:42]}),
                          ("extra key", {"v": 1, "client_nonce": nonce, "x": 1}), ("no nonce", {"v": 1})):
        c.expect(f"hello {name}", c.req("POST", "/launcher/hello", h, jbody(payload)), 400, "bad_request")
    a = c.req("POST", "/launcher/hello", h, jbody({"v": 1, "client_nonce": nonce})).json()
    b = c.req("POST", "/launcher/hello", h, jbody({"v": 1, "client_nonce": nonce})).json()
    c.check("same client nonce -> new server nonce each time", a["server_nonce"] != b["server_nonce"], "different", "same"
            if a["server_nonce"] == b["server_nonce"] else "different")
    if "token" in c.t.capabilities:
        c.t.set_token(False)
        c.expect("hello without a pairing token", c.req("POST", "/launcher/hello", h, jbody({"v": 1, "client_nonce": nonce})),
                 503, "no_pairing")
        c.t.set_token(True)


def _signed(c: Ctx, client: LauncherClient, method: str, path: str, body: bytes, counter: int,
            *, mac_path: Optional[str] = None, mac_body: Optional[bytes] = None, counter_text: Optional[str] = None,
            drop: tuple[str, ...] = (), dup: tuple[str, ...] = ()) -> Response:
    headers = client.sign(method, mac_path or path, body if mac_body is None else mac_body, counter)
    if counter_text is not None:
        headers = [(k, counter_text if k == "X-Sidecar-Counter" else v) for k, v in headers]
    headers = [(k, v) for k, v in headers if k not in drop] + [(k, v) for k, v in headers if k in dup]
    if body:
        headers.append(("Content-Type", "application/json"))
    return c.req(method, path, headers, body if body else None)


@probe("g1.r18.mac", "G1", "Channel calls: MAC failures, replayed/skipped/zero-padded counters all answer 401 and drop the channel")
def _(c: Ctx) -> None:
    status_path = "/launcher/launch/" + "A" * 22
    cases = {
        "wrong MAC": dict(mac_path="/launcher/release"),
        "body changed after signing": dict(mac_body=b"x"),
        "skipped counter": dict(counter=2),
        "leading zero counter": dict(counter_text="01"),
        "missing MAC": dict(drop=("X-Sidecar-MAC",)),
        "missing counter": dict(drop=("X-Sidecar-Counter",)),
        "missing channel": dict(drop=("X-Sidecar-Channel",)),
        "duplicate MAC": dict(dup=("X-Sidecar-MAC",)),
        "duplicate counter": dict(dup=("X-Sidecar-Counter",)),
    }
    for name, kw in cases.items():
        client = c.launcher()
        counter = kw.pop("counter", 1)
        resp = _signed(c, client, "GET", status_path, b"", counter, **kw)
        c.expect(f"{name} -> 401", resp, 401, "launcher_auth")
        c.check(f"{name}: 401 carries no MAC", not resp.all("X-Sidecar-MAC"), 0, len(resp.all("X-Sidecar-MAC")))
        if name not in ("missing channel",):
            follow = _signed(c, client, "GET", status_path, b"", 1)
            c.expect(f"{name}: channel dropped (a correct call now fails)", follow, 401, "launcher_auth")
    client = c.launcher()
    first = _signed(c, client, "GET", status_path, b"", 1)
    c.expect("counter 1 accepted (unknown id -> 404 with MAC)", first, 404, "not_found")
    c.check("404 carries a valid response MAC", first.all("X-Sidecar-MAC") == [
        contract.response_mac(client.key, 1, 404, first.body)], "valid", first.all("X-Sidecar-MAC"))
    c.expect("replayed counter 1", _signed(c, client, "GET", status_path, b"", 1), 401, "launcher_auth")
    c.expect("after replay, counter 2 also refused (dropped)", _signed(c, client, "GET", status_path, b"", 2),
             401, "launcher_auth")
    unknown = LauncherClient(c.t.port, c.t.token)
    unknown.server_nonce, unknown.key = contract.b64u(secrets.token_bytes(32)), secrets.token_bytes(32)
    c.expect("unknown channel", _signed(c, unknown, "GET", status_path, b"", 1), 401, "launcher_auth")
    stale = c.launcher()
    method_swap = stale.sign("GET", "/launcher/launch", b"", 1)
    c.expect("MAC for another path", c.req("GET", status_path, method_swap), 401, "launcher_auth")


@probe("g1.r18.limits", "G1", "Channel limits: 64 calls, 16 unused + 16 used channels (a hello drops only an unused one), 120 s life")
def _(c: Ctx) -> None:
    status_path = "/launcher/launch/" + "A" * 22
    client = c.launcher()
    calls = int(c.t.timing["channel_calls"])
    codes = [client.status("A" * 22).status for _ in range(calls)]
    c.check(f"{calls} calls accepted", codes == [404] * calls, f"{calls} x 404", sorted(set(codes)))
    c.expect(f"call {calls + 1} refused", _signed(c, client, "GET", status_path, b"", calls + 1), 401, "launcher_auth")
    used = c.launcher()
    c.expect("a channel in use (one MAC-verified call)", used.status("A" * 22), 404, "not_found")
    first = c.launcher()
    others = [c.launcher() for _ in range(int(c.t.timing["max_channels"]))]
    c.expect("the oldest unused of 18 channels was dropped", _signed(c, first, "GET", status_path, b"", 1), 401,
             "launcher_auth")
    c.expect("the newest channel works", others[-1].status("A" * 22), 404, "not_found")
    c.expect("the channel in use survived 17 hellos (revision 3, 3.1)", used.status("A" * 22), 404, "not_found")
    ttl = c.t.timing["channel_ttl"]
    c.wait_seconds("channel life", ttl + 1)
    aged = c.launcher()
    time.sleep(ttl + 1)
    c.expect("a channel past its life", _signed(c, aged, "GET", status_path, b"", 1), 401, "launcher_auth")


# ========================================================================= G1: launch and codes
@probe("g1.launch.shape", "G1", "launch: 201 shape, URL rule, document binding, every refused body answered with a MAC")
def _(c: Ctx) -> None:
    client = c.launcher()
    resp = client.launch(doc(1), host_scheme="dark", mode="app", bounds={"x": -100, "y": 30, "width": 400, "height": 900})
    data = resp.json()
    c.expect("launch 201", resp, 201)
    c.check("launch_id 22 chars", bool(contract.ID22_RE.fullmatch(data.get("launch_id") or "")), "22", data.get("launch_id"))
    c.check("url matches section 4.1", bool(contract.launch_url_re(c.t.port).fullmatch(data.get("url") or "")), "match", "?")
    c.check("window_live false, expires_in = code life", data.get("window_live") is False
            and data.get("expires_in") == int(c.t.timing["code_ttl"]), int(c.t.timing["code_ttl"]), data.get("expires_in"))
    code = data["url"].split("#launch=")[1]
    c.secrets.add(code)
    ex = c.exchange(code).json()
    c.secrets.add(ex["session"])
    c.check("exchange returns the bound document/scheme/mode/bounds",
            ex["document"] == doc(1) and ex["host_scheme"] == "dark" and ex["mode"] == "app"
            and ex["bounds"] == {"x": -100, "y": 30, "width": 400, "height": 900}, "bound values", "differs")
    legacy_keys = {"ok", "session", "idle_timeout", "document", "host_scheme", "mode", "bounds",
                   "chat", "chat_label", "endpoint", "limits"}
    c.check("exchange shape (revision 4/5 mixed-pair compatible)",
            set(ex) in (legacy_keys, legacy_keys | {"follow"}) and ex.get("follow", False) is False
            and ex["endpoint"] == c.origin and ex["limits"] == {"max_message": 8000}, "section 6.1 keys", sorted(ex))
    label = ex.get("chat_label") or ""
    c.check("chat_label is lo- and 8 hex digits (section 6.1 example \"lo-1a2b3c4d\")",
            re.fullmatch(r"lo-[0-9a-f]{8}", label) is not None, "lo-xxxxxxxx", f"{len(label)} chars, prefix lo-: {label.startswith('lo-')}")
    tab = c.launcher().launch(doc(2), host_scheme=None, mode="tab", bounds=None)
    c.expect("tab mode with null bounds and null scheme", tab, 201)
    rect = {"x": 0, "y": 0, "width": 400, "height": 800}
    bad = {
        "bounds with a boolean": dict(bounds={"x": True, "y": 0, "width": 400, "height": 800}),
        "width 239": dict(bounds=dict(rect, width=239)),
        "height 8193": dict(bounds=dict(rect, height=8193)),
        "x 32768": dict(bounds=dict(rect, x=32768)),
        "float y": dict(bounds=dict(rect, y=1.5)),
        "extra bounds key": dict(bounds=dict(rect, z=1)),
        "tab with bounds": dict(mode="tab", bounds=rect),
        "app without bounds": dict(mode="app", bounds=None),
        "mode window": dict(mode="window", bounds=rect),
        "scheme blue": dict(host_scheme="blue", bounds=rect),
        "type draw": dict(document=dict(doc(3), type="draw"), bounds=rect),
        "empty document_id": dict(document=dict(doc(3), document_id=""), bounds=rect),
        "title 1001": dict(document=dict(doc(3), title="t" * 1001), bounds=rect),
        "document extra key": dict(document=dict(doc(3), path="/x"), bounds=rect),
    }
    for name, kw in bad.items():
        client = c.launcher()
        payload = {"document": kw.get("document", doc(3)), "host_scheme": kw.get("host_scheme", "light"),
                   "mode": kw.get("mode", "app"), "bounds": kw.get("bounds")}
        try:
            r = client.call("POST", "/launcher/launch", payload)
            c.expect(f"launch {name}", r, 400, "bad_request")
        except NotYourHermes as exc:
            c.check(f"launch {name}: answered with a valid MAC", False, "MAC", str(exc))
    client = c.launcher()
    try:
        r = client.call("POST", "/launcher/launch", {"document": doc(3), "host_scheme": "light", "mode": "app"})
        c.expect("launch missing bounds key (MAC-verified 400)", r, 400, "bad_request")
    except NotYourHermes as exc:
        c.check("launch missing bounds key answered with a MAC", False, "MAC", str(exc))


@probe("g1.launch.status", "G1", "launch status: pending -> exchanged; unknown id 404; release shape")
def _(c: Ctx) -> None:
    client = c.launcher()
    data = client.launch(doc(1)).json()
    c.check("pending before exchange", client.status(data["launch_id"]).json().get("state") == "pending", "pending", "?")
    code = data["url"].split("#launch=")[1]
    c.secrets.add(code)
    tok = c.exchange(code).json()["session"]
    c.secrets.add(tok)
    c.check("exchanged after exchange", client.status(data["launch_id"]).json().get("state") == "exchanged", "exchanged", "?")
    c.expect("unknown launch id", client.status("B" * 22), 404, "not_found")
    # A malformed id is answered after the channel MAC is checked: a signed 404, like an unknown id (4.2).
    client = c.launcher()
    r = client.call("GET", "/launcher/launch/short", verify=False)
    macs = r.all("X-Sidecar-MAC")
    signed = len(macs) == 1 and hmac.compare_digest(macs[0], contract.response_mac(client.key, client.counter, r.status, r.body))
    c.check("malformed launch id: signed 404 not_found", r.status == 404 and r.code == "not_found" and signed,
            "404 not_found, valid MAC", f"{r.status} {r.code}, MAC {'valid' if signed else len(macs)}")
    client = c.launcher()
    for name, payload in (("doc with host_quit", {"document_id": "lo:test:1", "reason": "host_quit"}),
                          ("null with document_closed", {"document_id": None, "reason": "document_closed"}),
                          ("bad reason", {"document_id": "lo:test:1", "reason": "bye"})):
        c.expect(f"release {name}", client.release(payload["document_id"], payload["reason"]), 400, "bad_request")


@probe("g1.release.attachment", "G1", "Scoped release revokes only its code/session; old shape remains supported")
def _(c: Ctx) -> None:
    client = c.launcher()
    first_id, _, first_code = c.launch(1)
    second_id, _, second_code = c.launch(1)
    body = {"document_id": doc(1)["document_id"], "reason": "document_closed", "launch_id": first_id}
    response = client.call("POST", "/launcher/release", body)
    c.expect("scoped pending release", response, 200)
    c.check("pending release ends zero sessions", response.json().get("released") == 0, 0, response.json().get("released"))
    c.expect("only old code revoked", c.exchange(first_code), 401, "launch_expired")
    exchanged = c.exchange(second_code)
    c.expect("replacement pending code remains exchangeable", exchanged, 200)
    token = exchanged.json()["session"]
    c.secrets.add(token)
    c.expect("late old attachment is idempotent", client.call("POST", "/launcher/release", body), 200)
    c.expect("replacement still usable", c.get("/api/state", token), 200)
    for bad in (None, False, "short", "A" * 22 + "\n"):
        c.expect("invalid scoped id refused", client.call("POST", "/launcher/release", dict(body, launch_id=bad)), 400, "bad_request")
    body["launch_id"] = second_id
    c.expect("wrong document association", client.call("POST", "/launcher/release", dict(body, document_id="wrong")), 400, "bad_request")
    c.expect("host quit cannot carry id", client.call("POST", "/launcher/release", dict(body, document_id=None, reason="host_quit")), 400, "bad_request")
    c.expect("scoped live release", client.call("POST", "/launcher/release", body), 200)
    c.expect("its own session ended", c.get("/api/state", token), 401, "no_session")
    third_id, _, third_code = c.launch(1)
    c.expect("old launcher legacy body", client.release(doc(1)["document_id"], "document_closed"), 200)
    c.expect("legacy pending revocation", c.exchange(third_code), 401, "launch_expired")


@probe("g1.code.single_use", "G1", "A code is single use; a second presentation revokes its session, stops its run, sets double_use")
def _(c: Ctx) -> None:
    client = c.launcher()
    data = client.launch(doc(1)).json()
    code = data["url"].split("#launch=")[1]
    c.secrets.add(code)
    first = c.exchange(code)
    tok = first.json()["session"]
    c.secrets.add(tok)
    turn = c.chat(tok, "keep going [hang]").json()["turn"]
    time.sleep(0.4)
    chat_id = c.chat_id_of(tok)
    second = c.exchange(code)
    c.expect("second presentation", second, 401, "launch_double_use")
    st = c.get("/api/state", tok)
    c.expect("first session ended", st, 401, "no_session")
    c.check("ended reason double_use", (st.json() or {}).get("ended") == "double_use", "double_use", (st.json() or {}).get("ended"))
    c.check("launch state double_use", client.status(data["launch_id"]).json().get("state") == "double_use", "double_use", "?")
    if "fake" in c.t.capabilities:
        c.check("its driven run was asked to stop", c.wait_stop(chat_id), "stop call", "none")
    third = c.exchange(code)
    c.check("third presentation also refused", third.status == 401, 401, third.status)
    _ = turn


@probe("g1.code.unknown", "G1", "Unknown or malformed codes answer 401 launch_expired")
def _(c: Ctx) -> None:
    c.expect("random code", c.exchange(contract.b64u(secrets.token_bytes(32))), 401, "launch_expired")
    resp = c.exchange("A" * 42)
    c.check("42-char code -> 401 launch_expired (or 400)", (resp.status, resp.code) in ((401, "launch_expired"),
            (400, "bad_request")), "401 launch_expired", f"{resp.status} {resp.code}")
    c.expect("launch not a string", c.post("/api/session", {"launch": 5}, None), 400, "bad_request")


@probe("g1.code.ttl", "G1", "A code expires after its life; the launch state becomes expired; a spent code replayed after purge is only launch_expired")
def _(c: Ctx) -> None:
    ttl = c.t.timing["code_ttl"]
    c.wait_seconds("code life", ttl + 1.5)
    client = c.launcher()
    data = client.launch(doc(1)).json()
    code = data["url"].split("#launch=")[1]
    tok, _ = c.session(2)
    _lid2, _url2, used = c.launch(3)
    tok3 = c.exchange(used).json()["session"]
    c.secrets.update({code, tok, tok3})
    # Wait out the code life while tok3's window keeps polling /api/state (as an idle page does every
    # 3 s), so the session stays live even where the idle timeout is shortened below the wait.
    deadline = time.monotonic() + ttl + 1.0
    while time.monotonic() < deadline:
        c.get("/api/state", tok3)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    c.expect("expired code", c.exchange(code), 401, "launch_expired")
    st = LauncherClient(c.t.port, c.t.token)
    st.hello()
    state = st.status(data["launch_id"]).json().get("state")
    c.check("launch state expired", state == "expired", "expired", state)
    c.expect("spent code replayed after purge", c.exchange(used), 401, "launch_expired")
    c.expect("its session is still alive", c.get("/api/state", tok3), 200)


@probe("g1.code.cap", "G1", "At most 16 unexchanged codes; the 17th launch answers 429 too_many_launches (MAC-signed)")
def _(c: Ctx) -> None:
    for n in range(int(c.t.timing["max_codes"])):
        c.launch(100 + n)
    client = c.launcher()
    resp = client.launch(doc(200))
    c.expect("17th launch", resp, 429, "too_many_launches")
    ttl = c.t.timing["code_ttl"]
    if ttl <= 40 or c.slow:
        time.sleep(ttl + 1.0)
        c.expect("after the codes expire (purged), launching works again", c.launcher().launch(doc(201)), 201)


@probe("g1.launch.window_live", "G1", "A live session for the document answers 200 window_live and mints no code")
def _(c: Ctx) -> None:
    tok, _ = c.session(1)
    resp = c.launcher().launch(doc(1))
    data = resp.json()
    c.expect("launch for a live document", resp, 200)
    c.check("window_live true, no code", data.get("window_live") is True and data.get("url") is None
            and data.get("launch_id") is None and data.get("expires_in") == 0, "window_live", data)
    c.expect("another document still launches", c.launcher().launch(doc(2)), 201)
    _ = tok


# ============================================================================== G1: sessions
@probe("g1.session.replaced", "G1", "A newer session for the same document ends the older one (replaced)")
def _(c: Ctx) -> None:
    live = c.t.timing["window_live"]
    c.wait_seconds("window-live period", live + 1)
    tok, _ = c.session(1)
    time.sleep(live + 1.0)
    tok2, _ = c.session(1)
    st = c.get("/api/state", tok)
    c.expect("older session", st, 401, "no_session")
    c.check("ended replaced", (st.json() or {}).get("ended") == "replaced", "replaced", (st.json() or {}).get("ended"))
    c.expect("newer session works", c.get("/api/state", tok2), 200)


@probe("g1.session.evicted", "G1", "At 8 sessions the least recently seen one without a driven run is evicted")
def _(c: Ctx) -> None:
    cap = int(c.t.timing["max_sessions"])
    toks = [c.session(n)[0] for n in range(1, cap + 1)]
    c.chat(toks[0], "busy [hang]")                    # driven: not evictable although oldest
    time.sleep(0.3)
    for tok in toks[2:]:
        c.get("/api/state", tok)                      # toks[1] is now the least recently seen idle one
    new, _ = c.session(cap + 1)
    st = c.get("/api/state", toks[1])
    c.check("LRU idle session evicted", st.status == 401 and (st.json() or {}).get("ended") == "evicted",
            "401 evicted", f"{st.status} {(st.json() or {}).get('ended')}")
    c.expect("the driven session survives", c.get("/api/state", toks[0]), 200)
    c.expect("the new session works", c.get("/api/state", new), 200)
    c.check("session_cap (429) is unreachable black-box: MAX_ACTIVE 4 < 8 sessions, so one is always evictable",
            True, "note", "note")


@probe("g1.session.end_reasons", "G1", "close, document release, host quit and idle end sessions with their reason")
def _(c: Ctx) -> None:
    a, _ = c.session(1)
    c.expect("close", c.post("/api/session/close", {}, a), 200)
    st = c.get("/api/state", a)
    c.check("closed", (st.json() or {}).get("ended") == "closed", "closed", (st.json() or {}).get("ended"))
    b, _ = c.session(2)
    d, _ = c.session(3)
    e, _ = c.session(4)
    rel = c.launcher().release("lo:test:2", "document_closed")
    c.check("release one document -> released 1", rel.status == 200 and rel.json().get("released") == 1, 1, rel.json())
    st = c.get("/api/state", b)
    c.check("host_gone", (st.json() or {}).get("ended") == "host_gone", "host_gone", (st.json() or {}).get("ended"))
    c.expect("other document untouched", c.get("/api/state", d), 200)
    rel = c.launcher().release(None, "host_quit")
    c.check("host quit releases the rest (2)", rel.status == 200 and rel.json().get("released") == 2, 2, rel.json())
    c.check("host_gone for all", all((c.get("/api/state", t).json() or {}).get("ended") == "host_gone" for t in (d, e)),
            "host_gone", "?")
    idle = c.t.timing["idle"]
    c.wait_seconds("idle timeout", idle + 1)
    f, _ = c.session(5)
    time.sleep(idle + 1.0)
    st = c.get("/api/state", f)
    c.check("idle", st.status == 401 and (st.json() or {}).get("ended") == "idle", "401 idle", (st.json() or {}).get("ended"))


@probe("g1.session.end_stops_run", "G1", "Ending a session (close, release) asks its driven run to stop", needs=("fake",))
def _(c: Ctx) -> None:
    for how in ("close", "release"):
        tok, _ = c.session(1 if how == "close" else 2)
        c.chat(tok, "busy [hang]")
        time.sleep(0.4)
        chat_id = c.chat_id_of(tok)
        if how == "close":
            c.post("/api/session/close", {}, tok)
        else:
            c.launcher().release("lo:test:2", "document_closed")
        c.check(f"{how}: stop sent to the Runs API", c.wait_stop(chat_id), "stop", "none")


# ================================================================================ G1: static
def _rewrite_sums(ui: Path, extra: dict[str, str]) -> str:
    original = (ui / "SHA256SUMS").read_text()
    sums = contract.parse_sha256sums(original) or {}
    sums.update(extra)
    # bytes: write_text would write CRLF on Windows, and the plugin reads SHA256SUMS strictly (LF)
    (ui / "SHA256SUMS").write_bytes("".join(f"{v}  {k}\n" for k, v in sorted(sums.items())).encode("ascii"))
    return original


@probe("g1.static", "G1", "Static files: listed-and-matching only, exact headers, path rules, fetch metadata", needs=("ui_dir",))
def _(c: Ctx) -> None:
    ui: Path = c.t.ui_dir
    sums = contract.parse_sha256sums((ui / "SHA256SUMS").read_text()) or {}
    c.check("bundle SHA256SUMS parses (section 7.1 format)", bool(sums), "valid", "invalid")
    for rel in sums:
        resp = c.req("GET", f"/ui/{rel}", [("Sec-Fetch-Site", "same-origin")])
        want = contract.STATIC_TYPES.get(os.path.splitext(rel)[1])
        ok = resp.status == 200 and resp.header("Content-Type") == want and hashlib.sha256(resp.body).hexdigest() == sums[rel]
        c.check(f"listed {rel}", ok, f"200 {want}", f"{resp.status} {resp.header('Content-Type')}")
        missing = [k for k, v in contract.STATIC_HEADERS if resp.all(k) != [v]]
        c.check(f"{rel}: every section 7 header exactly once", not missing, [], missing)
    (ui / "unlisted.js").write_text("// not in SHA256SUMS\n")
    c.expect("unlisted file", c.req("GET", "/ui/unlisted.js", []), 404, "not_found")
    target_rel = "app.js" if "app.js" in sums else next(iter(sums))
    path = ui / target_rel
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    c.expect("tampered listed file", c.req("GET", f"/ui/{target_rel}", []), 404, "not_found")
    path.write_bytes(original)
    c.expect("restored file served again", c.req("GET", f"/ui/{target_rel}", []), 200)
    os.symlink(path, ui / "linked.js")
    saved = _rewrite_sums(ui, {"linked.js": sums[target_rel]})
    c.expect("listed symlink", c.req("GET", "/ui/linked.js", []), 404, "not_found")
    (ui / "SHA256SUMS").write_bytes(saved.encode("ascii"))
    for bad in ("/ui/../SHA256SUMS", "/ui/%2e%2e/SHA256SUMS", "/ui/vendor%2fpurify.js", "/ui/vendor\\purify.js",
                "/ui/index.html?x=1", "/ui/INDEX.html", "/ui/./index.html", "/ui//index.html", "/ui/index.html/",
                "/ui/SHA256SUMS", "/ui/vendor/VENDOR.json", "/ui/UPSTREAM.json", "/ui/vendor/dompurify.LICENSE",
                "/ui/%69ndex.html", "/ui/.hidden.js"):
        c.expect(f"GET {bad}", c.req("GET", bad, []), 404, "not_found")
    nav = [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]
    c.expect("index.html cross-site navigation (bootstrap file)", c.req("GET", "/ui/index.html",
             [("Sec-Fetch-Site", "cross-site")] + nav), 200)
    c.expect("index.html navigation with Sec-Fetch-Site none", c.req("GET", "/ui/index.html", [("Sec-Fetch-Site", "none")] + nav), 200)
    c.expect("index.html without fetch metadata", c.req("GET", "/ui/index.html", []), 200)
    c.expect("index.html cross-site as a script", c.req("GET", "/ui/index.html", [
        ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "script")]), 403, "cross_site")
    c.expect("index.html cross-site in an iframe", c.req("GET", "/ui/index.html", [
        ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "iframe")]), 403, "cross_site")
    for site in ("cross-site", "same-site", "none"):
        c.expect(f"{target_rel} with Sec-Fetch-Site {site}", c.req("GET", f"/ui/{target_rel}",
                 [("Sec-Fetch-Site", site), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "script")]), 403, "cross_site")
    c.expect(f"{target_rel} without fetch metadata", c.req("GET", f"/ui/{target_rel}", []), 200)


@probe("g1.api.headers", "G1", "Every /api answer carries the section 6 headers; errors carry code; no ids leak")
def _(c: Ctx) -> None:
    tok, ex = c.session()
    turn = c.chat(tok, "hello [md]").json()["turn"]
    c.until_done(tok, turn)
    samples = [c.get("/api/state", tok), c.get("/api/state", None), c.post("/api/new-chat", {}, tok),
               c.req("GET", "/api/nope", []), c.post("/api/session", {"launch": "A" * 43}, None)]
    for resp in samples:
        missing = [k for k, v in contract.API_HEADERS if resp.all(k) != [v]]
        c.check(f"{resp.status} {resp.code}: section 6 headers", not missing and
                resp.header("Content-Type", ) == "application/json", [], missing)
        if resp.status >= 400:
            data = resp.json() or {}
            c.check(f"{resp.status} {resp.code}: error JSON", data.get("ok") is False and isinstance(data.get("error"), str)
                    and data.get("code") in contract.CODES and contract.CODES[data["code"]] == resp.status,
                    "ok/error/code", data)
    bodies = b"".join(r.body for r in c.responses)
    c.check("no Hermes run id in any answer", b"run_" not in bodies, "absent", "present")
    c.check("no full chat session id (lo-<32 hex>) in any answer",
            re.search(rb"lo-[0-9a-f]{32}", bodies) is None, "absent", "present")


@probe("g1.logs", "G1", "Log lines are metadata only: fixed labels, no path, id, title, message, token or code", needs=("logs",))
def _(c: Ctx) -> None:
    title = "Secret-Title-" + secrets.token_hex(4) + ".ods"
    client = c.launcher()
    data = client.launch({"document_id": "lo:SECRETDOC:9", "title": title, "type": "calc"}).json()
    code = data["url"].split("#launch=")[1]
    tok = c.exchange(code).json()["session"]
    message = "SECRET-MESSAGE-" + secrets.token_hex(4)
    turn = c.chat(tok, message).json()["turn"]
    c.until_done(tok, turn)
    c.req("GET", "/ui/index.html", [])
    c.req("OPTIONS", "/api/state", [])
    c.req("GET", "/ui/../etc/passwd", [])
    c.req("GET", "/nope/" + secrets.token_hex(4), [])
    c.req("GET", "/api/state", [("Sec-Fetch-Site", "same-origin"), ("X-Sidecar-Session", "x" * 43)])
    lines = [l for l in c.t.logs.lines if "libreoffice chat " in l]
    labels = "|".join(contract.LOG_LABELS)
    pattern = re.compile(rf"libreoffice chat (GET|POST|HEAD|OPTIONS|OTHER) ({labels}) -> \d{{3}} in \d+\.\ds$")
    request_lines = [l for l in lines if " -> " in l]
    bad = [l for l in request_lines if not pattern.search(l)]
    c.check("request log lines use fixed labels only", request_lines and not bad, "all match", bad[:5])
    needles = [title, "SECRETDOC", message, tok, code, turn["id"], data["launch_id"], c.t.token, "/ui/", "/api/", "passwd"]
    leaked = [n[:12] for n in needles if any(n in l for l in c.t.logs.lines)]
    c.check("no secret, id, title, message or path in any log line", not leaked, [], leaked)
    used = sorted({m.group(2) for l in request_lines for m in [pattern.search(l)] if m})
    c.check("labels seen", True, "info", used)


# ======================================================================== G2 (black-box part)
@probe("g2.turn.shape", "G2", "chat 202 turn view; long-poll returns at once when behind; reply by suffix", needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    resp = c.chat(tok, "summary please [md]")
    c.expect("chat", resp, 202)
    turn = resp.json()["turn"]
    c.check("turn view keys", set(turn) == {"id", "seq", "state", "hermes_state", "send_id", "text_length", "activity",
                                            "outcome"}
            and contract.ID22_RE.fullmatch(turn["id"]) is not None and turn["state"] == "working"
            and turn["send_id"] is None, "section 6.4 (revision 3: send_id)", turn)
    done = c.until_done(tok, turn)
    c.check("completed outcome", done["outcome"] == {"kind": "completed", "message": None}, "completed", done["outcome"])
    c.check("reply text is the Markdown reply", done["text_all"] == corpus.MARKDOWN_REPLY, len(corpus.MARKDOWN_REPLY),
            len(done["text_all"]))
    c.check("activity is [] (T1)", done["activity"] == [], [], done["activity"])
    r = c.poll(tok, turn["id"], 0, 10).json()["turn"]
    c.check("have=10 -> suffix from 10", r["text_from"] == 10 and r["text"] == corpus.MARKDOWN_REPLY[10:], 10, r["text_from"])
    r = c.poll(tok, turn["id"], 0, 199999).json()["turn"]
    c.check("have beyond the text -> empty suffix", r["text"] == "" and r["text_length"] == len(corpus.MARKDOWN_REPLY),
            "", r["text"][:10])
    st = c.get("/api/state", tok).json()
    c.check("state keeps the ended turn (no text)", st["turn"] and st["turn"]["id"] == turn["id"]
            and "text" not in st["turn"] and st["turn"]["outcome"]["kind"] == "completed", "turn kept", st["turn"])
    turn2 = c.chat(tok, "again").json()["turn"]
    c.expect("a new message clears the previous turn record", c.poll(tok, turn["id"], 0, 0), 404, "unknown_turn")
    c.until_done(tok, turn2)


@probe("g2.send.idempotent", "G2", "/api/chat with a send_id: a retry answers with the same turn and starts no run",
       needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    body = {"message": "summary please [md]", "send_id": "S" * 22}
    first = c.post("/api/chat", body, tok)
    c.expect("first send", first, 202)
    turn = first.json()["turn"]
    c.check("the turn names its send_id", turn.get("send_id") == "S" * 22 and first.json().get("replayed") is False,
            "S*22, replayed false", {"send_id": turn.get("send_id"), "replayed": first.json().get("replayed")})
    again = c.post("/api/chat", body, tok)
    c.expect("the same send again (its answer was lost)", again, 200)
    c.check("answered with the same turn, replayed", again.json().get("replayed") is True
            and again.json()["turn"]["id"] == turn["id"], turn["id"], again.json().get("turn", {}).get("id"))
    c.until_done(tok, turn)
    late = c.post("/api/chat", body, tok)
    c.check("a retry after the end gets the ended turn", late.status == 200 and late.json()["turn"]["state"] == "done",
            "200 done", late.status)
    with c.t.fake.lock:
        creates = sum(1 for call in c.t.fake.calls if call["kind"] == "create")
    c.check("one run created for one send_id", creates == 1, 1, creates)
    c.expect("the same send_id with another message", c.post("/api/chat", {"message": "other", "send_id": "S" * 22}, tok),
             400, "bad_request")
    c.expect("a send_id that is not 22 base64url characters", c.post("/api/chat", {"message": "x", "send_id": "short"},
                                                                     tok), 400, "bad_request")


@probe("g2.turn.query", "G2", "/api/turn query: exactly id, seq, have; bounded integers; unknown turn 404")
def _(c: Ctx) -> None:
    tok, _ = c.session()
    tid = c.chat(tok, "hi").json()["turn"]["id"]
    for q in (f"id={tid}&seq=0", f"id={tid}&seq=0&have=0&x=1", f"id={tid}&id={tid}&seq=0&have=0",
              f"id={tid}&seq=-1&have=0", f"id={tid}&seq=200001&have=0",
              f"id={tid}&seq=0&have=1.0", "id=short&seq=0&have=0", f"id={tid}&seq=&have=0", ""):
        c.expect(f"query {q.replace(tid, '<id>') or '(none)'}", c.get(f"/api/turn?{q}" if q else "/api/turn", tok),
                 400, "bad_request")
    for q in (f"id={tid}&seq=01&have=0", f"id={tid}&seq=0&have=00"):   # 6.4: no leading zeros
        c.expect(f"query {q.replace(tid, '<id>')} (leading zero)", c.get(f"/api/turn?{q}", tok), 400, "bad_request")
    c.expect("unknown turn id", c.poll(tok, "C" * 22, 0), 404, "unknown_turn")
    other, _ = c.session(2)
    c.expect("another session's turn", c.poll(other, tid, 0), 404, "unknown_turn")
    c.expect("seq 200000 accepted", c.get(f"/api/turn?id={tid}&seq=200000&have=200000", tok), 200)


@probe("g2.turn.longpoll", "G2", "Long-poll parks until news or the poll limit; a newer poll supersedes the older", needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    turn = c.chat(tok, "wait [hang]").json()["turn"]
    deadline = time.monotonic() + 3
    seq = turn["seq"]
    while time.monotonic() < deadline:   # catch up with queued/running
        view = c.poll(tok, turn["id"], seq, timeout=60).json()["turn"]
        if view["hermes_state"] == "running":
            seq = view["seq"]
            break
        seq = view["seq"]
    limit = c.t.timing["longpoll"]
    c.wait_seconds("long-poll limit", limit + 2, limit=30)
    resp = c.poll(tok, turn["id"], seq, timeout=60)
    c.check("parked for the poll limit, then answered unchanged", resp.status == 200 and limit - 0.5 <= resp.elapsed <= limit + 2
            and resp.json()["turn"]["seq"] == seq, f"~{limit} s", round(resp.elapsed, 2))
    out: dict[str, Response] = {}
    first = threading.Thread(target=lambda: out.__setitem__("old", c.poll(tok, turn["id"], seq, timeout=60)))
    first.start()
    time.sleep(0.5)
    out["new_started"] = None  # type: ignore[assignment]
    second = threading.Thread(target=lambda: out.__setitem__("new", c.poll(tok, turn["id"], seq, timeout=60)))
    second.start()
    first.join(5)
    old = out.get("old")
    c.check("older poll answered at once with superseded", old is not None and old.status == 200
            and old.json().get("superseded") is True and old.elapsed < 2.0, "superseded < 2 s",
            None if old is None else (old.status, old.json().get("superseded"), round(old.elapsed, 2)))
    stop = c.post("/api/turn/stop", {"turn": turn["id"]}, tok)
    c.expect("stop while parked", stop, 202)
    second.join(10)
    new = out.get("new")
    c.check("the newer poll wakes on the stop", new is not None and new.status == 200 and new.elapsed < limit,
            "woken", None if new is None else round(new.elapsed, 2))


@probe("g2.stop", "G2", "Stop: 202 stopping, outcome user (USER_STOPPED), stop reaches the Runs API; Stop after done is 200", needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    turn = c.chat(tok, "go on [hang]").json()["turn"]
    time.sleep(0.5)
    chat_id = c.chat_id_of(tok)
    resp = c.post("/api/turn/stop", {"turn": turn["id"]}, tok)
    c.expect("stop", resp, 202)
    c.check("state stopping", resp.json()["turn"]["state"] == "stopping", "stopping", resp.json()["turn"]["state"])
    t0 = time.monotonic()
    done = c.until_done(tok, turn)
    c.check("outcome user with USER_STOPPED", done["outcome"] == {"kind": "user", "message":
            "Stopped. Check the document for changes made before stopping."}, "user", done["outcome"])
    c.check("stop reached the Runs API", bool(c.t.fake.calls_for(chat_id, "stop")), "stop", "none")
    c.check("stopped within 3 s", time.monotonic() - t0 < 3.0, "< 3 s", round(time.monotonic() - t0, 2))
    again = c.post("/api/turn/stop", {"turn": turn["id"]}, tok)
    c.check("stop after done -> 200 final view", again.status == 200 and again.json()["turn"]["state"] == "done",
            "200 done", f"{again.status}")
    c.expect("stop unknown turn", c.post("/api/turn/stop", {"turn": "D" * 22}, tok), 404, "unknown_turn")
    c.expect("stop with a non-string turn", c.post("/api/turn/stop", {"turn": 1}, tok), 400, "bad_request")


@probe("g2.outcomes", "G2", "Outcome kinds: error, uncertain, empty reply, refused creation, approval wording state", needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    cases = (("[fail]", "error", "Hermes could not finish this reply; see the Hermes logs"),
             ("[unknown]", "uncertain", None), ("[refuse]", "error", None), ("[noreply]", "completed", None))
    for marker, kind, message in cases:
        turn = c.chat(tok, f"case {marker}").json()["turn"]
        done = c.until_done(tok, turn)
        ok = done["outcome"]["kind"] == kind and (message is None or done["outcome"]["message"] == message)
        if marker == "[unknown]":
            ok = ok and done["outcome"]["message"].startswith("It is not known whether Hermes carried out")
        if marker == "[noreply]":
            ok = ok and done["text_all"] == "(Hermes finished without a text reply.)"
        c.check(f"{marker} -> {kind}", ok, kind, done["outcome"])
    turn = c.chat(tok, "needs approval [approval]").json()["turn"]
    seen, seq, deadline = None, turn["seq"], time.monotonic() + 5
    while time.monotonic() < deadline and seen != "waiting_for_approval":
        view = c.poll(tok, turn["id"], seq).json()["turn"]
        seen, seq = view["hermes_state"], view["seq"]
    c.check("hermes_state waiting_for_approval reported", seen == "waiting_for_approval", "waiting_for_approval", seen)
    c.post("/api/turn/stop", {"turn": turn["id"]}, tok)
    c.until_done(tok, turn)
    turn = c.chat(tok, "big [long:150000]").json()["turn"]
    done = c.until_done(tok, turn)
    c.check("150000-character reply delivered whole", len(done["text_all"]) == 150000, 150000, len(done["text_all"]))


@probe("g2.claims", "G2", "busy 409, too_many 429, turn_running 409 on New chat, plugin_inactive 503", needs=("fake",))
def _(c: Ctx) -> None:
    toks = [c.session(n)[0] for n in range(1, 6)]
    turns = [c.chat(t, "hold [hang]").json()["turn"] for t in toks[:4]]
    c.expect("second message while driven", c.chat(toks[0], "again"), 409, "busy")
    c.expect("fifth concurrent chat", c.chat(toks[4], "fifth"), 429, "too_many")
    c.expect("New chat while driven", c.post("/api/new-chat", {}, toks[0]), 409, "turn_running")
    for t, turn in zip(toks[:4], turns):
        c.post("/api/turn/stop", {"turn": turn["id"]}, t)
    for t, turn in zip(toks[:4], turns):
        c.until_done(t, turn)
    nc = c.post("/api/new-chat", {}, toks[0])
    # the label's format is judged once, in g1.launch.shape
    c.check("New chat after the turn ended -> 200 with a new label", nc.status == 200 and
            bool(nc.json().get("chat_label")), 200, nc.status)
    if "ready" in c.t.capabilities:
        c.t.set_ready("plugin_inactive")
        st = c.get("/api/state", toks[1]).json()
        c.check("state reports plugin_inactive", st["chat"] == {"ready": False, "reason": "plugin_inactive"},
                "plugin_inactive", st["chat"])
        c.expect("chat while the plugin is inactive", c.chat(toks[1], "x"), 503, "plugin_inactive")
        for reason in ("no_api_key", "bad_api_key"):
            c.t.set_ready(reason)
            st = c.get("/api/state", toks[1]).json()
            c.check(f"state reports {reason}", st["chat"] == {"ready": False, "reason": reason}, reason, st["chat"])
        c.t.set_ready(None)
        st = c.get("/api/state", toks[1]).json()
        c.check("state ready again", st["chat"] == {"ready": True, "reason": None}, "ready", st["chat"])


@probe("g2.unresolved", "G2", "An unresolved run: 409 unresolved, New chat allowed, the new chat works", needs=("fake",))
def _(c: Ctx) -> None:
    tok, _ = c.session()
    turn = c.chat(tok, "lost [unresolved]").json()["turn"]
    done = c.until_done(tok, turn, limit=40)
    c.check("outcome uncertain", done["outcome"]["kind"] == "uncertain", "uncertain", done["outcome"]["kind"])
    resp = c.chat(tok, "next", )
    c.expect("next message on the same chat", resp, 409, "unresolved")
    c.check("409 text ends with 'Start a New chat to continue.'",
            (resp.json() or {}).get("error", "").endswith("Start a New chat to continue."), "suffix", (resp.json() or {}).get("error"))
    nc = c.post("/api/new-chat", {}, tok)
    c.expect("New chat beside the unresolved run", nc, 200)
    turn2 = c.chat(tok, "fresh start").json()
    c.check("the new chat runs", turn2.get("ok") is True, True, turn2)
    c.until_done(tok, turn2["turn"])
    c.t.fake.heal_all()


@probe("g2.window_gone", "G2", "A driven turn with no long-poll for the window-gone period is asked to stop", needs=("fake",))
def _(c: Ctx) -> None:
    gone = c.t.timing["window_gone"]
    c.wait_seconds("window-gone period", gone + 3)
    tok, _ = c.session()
    turn = c.chat(tok, "nobody watches [hang]").json()["turn"]
    time.sleep(0.4)
    chat_id = c.chat_id_of(tok)
    time.sleep(max(0.0, gone - 1.5))
    c.check("no stop before the period", not c.t.fake.calls_for(chat_id, "stop"), "none", "stop")
    c.check("stop requested after the period", c.wait_stop(chat_id, 4.0), "stop", "none")
    done = c.until_done(tok, turn)
    c.check("outcome user", done["outcome"]["kind"] == "user", "user", done["outcome"]["kind"])


# ============================================================================= budget / unload
@probe("budget.stop_with_parked_polls", "G2", "4 driven turns with parked long-polls + 4 polling sessions: a 9th page load and every Stop are served", needs=("fake",))
def _(c: Ctx) -> None:
    hang = [c.session(n)[0] for n in range(1, 5)]
    idle = [c.session(n)[0] for n in range(5, 9)]
    turns = [c.chat(t, "hold [hang]").json()["turn"] for t in hang]
    time.sleep(0.5)
    results: dict[int, Response] = {}
    threads = []
    for i, (t, turn) in enumerate(zip(hang, turns)):
        view = c.get("/api/state", t).json()["turn"]
        th = threading.Thread(target=lambda i=i, t=t, turn=turn, seq=view["seq"]: results.__setitem__(
            i, c.poll(t, turn["id"], seq, timeout=60)))
        th.start()
        threads.append(th)
    stop_polling = threading.Event()

    def state_poller(tok: str) -> None:
        while not stop_polling.is_set():
            c.get("/api/state", tok)
            time.sleep(0.3)
    pollers = [threading.Thread(target=state_poller, args=(t,)) for t in idle]
    for th in pollers:
        th.start()
    time.sleep(1.0)
    page = c.req("GET", "/ui/index.html", [])
    c.check("a ninth page load is served while 4 polls are parked", page.status == 200 and page.elapsed < 2,
            "200 < 2 s", (page.status, round(page.elapsed, 2)))
    for t, turn in zip(hang, turns):
        resp = c.post("/api/turn/stop", {"turn": turn["id"]}, t)
        c.check("Stop served promptly", resp.status == 202 and resp.elapsed < 2, "202 < 2 s", (resp.status, round(resp.elapsed, 2)))
    for th in threads:
        th.join(10)
    c.check("every parked poll woke after its Stop", len(results) == 4 and all(r.status == 200 for r in results.values()),
            4, len(results))
    stop_polling.set()
    for th in pollers:
        th.join(5)
    for t, turn in zip(hang, turns):
        c.check("turn ended as user", c.until_done(t, turn)["outcome"]["kind"] == "user", "user", "?")


@probe("unload.parked", "G2", "Unload with parked long-polls: each answers once 503 turned_off before close returns; owned_runs 0; port closed", needs=("unload", "fake"))
def _(c: Ctx) -> None:
    toks = [c.session(n)[0] for n in range(1, 4)]
    turns = [c.chat(t, "hold [hang]").json()["turn"] for t in toks]
    time.sleep(0.6)
    chat_ids = [c.t.fake.calls[i]["session_id"] for i in range(len(c.t.fake.calls)) if c.t.fake.calls[i]["kind"] == "create"]
    answers: dict[int, tuple[Response, float]] = {}
    threads = []
    for i, (t, turn) in enumerate(zip(toks, turns)):
        seq = c.get("/api/state", t).json()["turn"]["seq"]
        th = threading.Thread(target=lambda i=i, t=t, turn=turn, seq=seq: answers.__setitem__(
            i, (c.poll(t, turn["id"], seq, timeout=60), time.monotonic())))
        th.start()
        threads.append(th)
    time.sleep(0.8)
    t0 = time.monotonic()
    remaining = c.t.unload(10.0)
    t1 = time.monotonic()
    for th in threads:
        th.join(5)
    c.check("close() returned within the 10 s budget", t1 - t0 < 10.0, "< 10 s", round(t1 - t0, 2))
    c.check("close() reports 0 runs left", remaining == 0, 0, remaining)
    c.check("owned_runs() == 0", c.t.owned_runs() == 0, 0, c.t.owned_runs())
    ok = [r for r, _ in answers.values() if r.status == 503 and r.code == "turned_off" and (r.json() or {}).get("ended") == "turned_off"]
    c.check("each parked poll answered 503 turned_off, ended turned_off", len(ok) == 3, 3,
            [f"{r.status} {r.code}" for r, _ in answers.values()])
    c.check("all parked answers arrived before close() returned", all(when <= t1 + 0.05 for _, when in answers.values()),
            "before", [round(when - t1, 3) for _, when in answers.values()])
    c.check("each run was asked to stop", all(c.t.fake.calls_for(cid, "stop") for cid in chat_ids), 3,
            sum(1 for cid in chat_ids if c.t.fake.calls_for(cid, "stop")))
    c.check("listener closed (connection refused)", httpraw.port_refuses(c.t.port), "refused", "open")


# ================================================================================== runner
def main(argv: Optional[list[str]] = None) -> int:
    import fixture
    import targets
    ap = argparse.ArgumentParser(description="web_smoke probe matrix")
    ap.add_argument("--bundle", required=True, help="page bundle folder (with SHA256SUMS)")
    ap.add_argument("--target", default="stub", choices=["stub", "plugin", "external"])
    ap.add_argument("--contract-timing", action="store_true", help="stub: keep the contract's timings (slow)")
    ap.add_argument("--plugin-factory", default="plugin_factory:make_endpoint")
    ap.add_argument("--repo", default=str(HERE.parents[1]))
    ap.add_argument("--port", type=int)
    ap.add_argument("--token-file")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    bundle = Path(args.bundle)

    def make() -> Any:
        if args.target == "stub":
            return targets.StubTarget(bundle, fast=not args.contract_timing, repo=Path(args.repo))
        if args.target == "plugin":
            return targets.PluginTarget(bundle, args.plugin_factory, Path(args.repo))
        return targets.ExternalTarget(args.port, Path(args.token_file))

    selected = [p for p in PROBES if not args.only or any(p.id.startswith(o) for o in args.only)]
    results = []
    for p in selected:
        r = run_probe(p, make, args.slow)
        results.append(r)
        failed = [ch["label"] for ch in r["checks"] if not ch["ok"]]
        print(f"{r['status'].upper():5} {r['id']:34} {r['seconds']:6.1f}s  {r.get('note', '')}"
              + (f"  FAILED: {failed[:4]}" if failed else ""), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=str))
    _ = fixture
    return 0 if all(r["status"] in ("pass", "skip") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
