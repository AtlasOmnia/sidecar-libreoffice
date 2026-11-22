#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Revision-5 black-box follow probes; explicit target, no product imports.

python3 scripts/web_smoke/follow_probe.py --target stub --json evidence.json
External mode never owns/stops the server; use only a disposable test endpoint.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import contract
from httpraw import LauncherClient, doc, jbody, request
import targets

RECT = {"x": 900, "y": 120, "width": 400, "height": 600}


def run(target):
    checks = []
    def check(label, ok, observed=None):
        checks.append({"label": label, "ok": bool(ok), "observed": observed})
    def expect(label, response, status, code=None):
        check(label, response.status == status and (code is None or response.code == code),
              {"status": response.status, "code": response.code})
        return response
    def get(token, query="seq=0", **kw):
        return request(target.port, "GET", "/api/window?" + query,
                       [("Sec-Fetch-Site", "same-origin"), ("X-Sidecar-Session", token)], **kw)
    def post(path, payload, token=None):
        headers = [("Origin", target.origin), ("Content-Type", "application/json")]
        if token: headers.append(("X-Sidecar-Session", token))
        return request(target.port, "POST", path, headers, jbody(payload))
    client = LauncherClient(target.port, target.token)
    hello = client.hello()
    check("hello advertises window_follow", "window_follow" in hello.get("caps", []))
    def session(n, follow=None, mode="app"):
        response = expect("launch " + str(n), client.launch(doc(n), mode=mode, follow=follow), 201)
        if response.status != 201: raise AssertionError("launch refused")
        data = response.json()
        exchanged = expect("exchange " + str(n), post("/api/session", {"launch": data["url"].split("#launch=")[1]}), 200)
        check("follow echo " + str(n), data.get("follow") is (follow is True)
              and exchanged.json().get("follow") is (follow is True))
        return data["launch_id"], exchanged.json()["session"]
    plain_id, plain = session(501)
    expect("old launch has no follow", get(plain), 404, "no_follow")
    expect("follow false is invalid", client.launch(doc(502), follow=False), 400, "bad_request")
    expect("tab follow is invalid", client.launch(doc(503), mode="tab", follow=True), 400, "bad_request")
    lid, token = session(504, True)
    live = client.launch(doc(504), follow=True)
    expect("existing window stays live", live, 200)
    check("live never rebinds", live.json().get("follow") is False and live.json().get("launch_id") is None)
    for query in ("", "seq=01", "seq=-1", "seq=2147483648", "seq=0&seq=1", "seq=0&x=1", "seq=%30", "seq=1.0"):
        expect("reject query " + query, get(token, query), 400, "bad_request")
    expect("missing session", request(target.port, "GET", "/api/window?seq=0", [("Sec-Fetch-Site", "same-origin")]), 401)
    expect("window bad Host before auth", request(target.port, "GET", "/api/window?seq=0", host="foreign"), 403, "bad_host")
    expect("window foreign credential", request(target.port, "GET", "/api/window?seq=0",
           [("X-Sidecar-Channel", "x")]), 403, "forbidden_credential")
    expect("window method before credential", request(target.port, "POST", "/api/window?seq=0",
           [("X-Sidecar-Channel", "x")]), 405, "method_not_allowed")
    def bounds(seq=1, state="normal", rect=RECT, launch_id=lid):
        return {"launch_id": launch_id, "seq": seq, "state": state, "bounds": rect}
    for name, payload in (("boolean seq", bounds(True)), ("zero seq", bounds(0)),
                          ("overflow", bounds(2147483648)), ("null normal", bounds(rect=None)),
                          ("hidden geometry", bounds(state="hidden")),
                          ("boolean coordinate", bounds(rect=dict(RECT, x=True))),
                          ("small width", bounds(rect=dict(RECT, width=239))),
                          ("extra field", dict(bounds(), extra=1))):
        expect(name, client.call("POST", "/launcher/bounds", payload), 400, "bad_request")
    expect("unsigned bounds refused", request(target.port, "POST", "/launcher/bounds",
           [("Content-Type", "application/json")], jbody(bounds())), 401, "launcher_auth")
    expect("bounds to no-follow", client.call("POST", "/launcher/bounds", bounds(launch_id=plain_id)), 409, "attachment_stale")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(get, token)
        time.sleep(.1)
        expect("signed bounds accepted", client.call("POST", "/launcher/bounds", bounds()), 200)
        wake = expect("park wakes", pending.result(4), 200)
        check("snapshot exact/no launch id", wake.json().get("window") == {"seq": 1, "state": "normal", "bounds": RECT}
              and "launch_id" not in json.dumps(wake.json()))
    expect("stale sequence", client.call("POST", "/launcher/bounds", bounds()), 409, "attachment_stale")
    for seq, state in enumerate(("minimized", "hidden", "fullscreen", "window_closed"), 2):
        expect(state, client.call("POST", "/launcher/bounds", bounds(seq, state, None)), 200)
        response = get(token)
        check(state + " null geometry", response.json().get("window") == {"seq": seq, "state": state, "bounds": None})
    # A finite immediate read also exercises ExternalTarget with no server internals.
    expect("latest snapshot immediate", get(token), 200)
    expect("close", post("/api/session/close", {}, token), 200)
    expect("ended bounds cannot revive", client.call("POST", "/launcher/bounds", bounds(8)), 409, "attachment_stale")
    expect("ended page", get(token), 401, "no_session")
    post("/api/session/close", {}, plain)
    return checks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=("stub", "plugin", "external"), required=True)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--bundle", type=Path)
    ap.add_argument("--port", type=int)
    ap.add_argument("--token-file", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args()
    bundle = args.bundle or args.repo / "plugin/sidebar"
    if args.target == "external":
        if not args.port or not args.token_file: ap.error("external needs --port and --token-file")
        target = targets.ExternalTarget(args.port, args.token_file)
    elif args.target == "plugin": target = targets.PluginTarget(bundle, "plugin_factory:make_endpoint", args.repo)
    else: target = targets.StubTarget(bundle, fast=False, repo=args.repo)
    result = {"target": args.target, "checks": [], "status": "error"}
    try:
        result["checks"] = run(target)
        result["status"] = "pass" if all(c["ok"] for c in result["checks"]) else "fail"
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        target.close()
    from run_gates import write_json
    write_json(args.json, result)
    print(json.dumps({"target": args.target, "status": result["status"], "checks": len(result["checks"])}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__": raise SystemExit(main())
