#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Run the 0.5.0 acceptance checks that can run here and write evidence.

    python3 scripts/web_smoke/run_gates.py --evidence <folder> --vendor-from <folder> \
        [--ui-dir plugin/sidebar] [--target stub|plugin] [--plugin-factory module:function] [--slow]

Writes ``<evidence>/<gate>/<run>/`` for G1, G2, G3, G5 (JSON results, screenshots, AX roles) and
``<evidence>/SUMMARY-<run>.md``. Every 43-character base64url value (tokens, codes, nonces, MACs) is
replaced by a short hash before anything is written.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import browser_smoke  # noqa: E402
import contract  # noqa: E402
import fixture  # noqa: E402
import probes  # noqa: E402
import targets  # noqa: E402

SECRET_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
NEGATIVE_EXPECTED_FAILS = ("no forbidden tag inside #messages", "no id / name / style attribute inside #messages",
                           "no navigating <a href> left", "getElementById('send-button') is the composer's button")


def redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: "<43:" + hashlib.sha256(m.group(0).encode()).hexdigest()[:8] + ">", text)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(json.dumps(data, indent=1, default=str)), encoding="utf-8")


def tally(results: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        if r.get("status") != "info":
            out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def table(results: list[dict]) -> str:
    rows = ["| Check | Status | Seconds | Failed or note |", "| --- | --- | --- | --- |"]
    for r in results:
        if r.get("status") == "info":
            continue
        failed = [c["label"] for c in r.get("checks", []) if not c["ok"]]
        note = "; ".join(failed[:3]) or r.get("note", "")
        rows.append(f"| `{r.get('bundle', '') + ':' if r.get('bundle') else ''}{r['id']}` — {r['title']} | "
                    f"{r['status']} | {r.get('seconds', '')} | {redact(note)[:200]} |")
    return "\n".join(rows)


def environment(repo: Path) -> dict[str, Any]:
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"when": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()} {platform.machine()}", "commit": commit,
            "vectors_ok": contract.check_vectors() == [],
            "protocol_sha256": hashlib.sha256((repo / "docs/web-protocol.md").read_bytes()).hexdigest(),
            "dirty_paths": subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],capture_output=True,text=True).stdout.splitlines(),
            "evidence_scope": "bounded listed checks only; not G1-G7 completion or release acceptance"}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--vendor-from", help="folder holding vendor/marked.umd.js and vendor/purify.js (pinned)")
    ap.add_argument("--ui-dir", help="the real page bundle (plugin/sidebar); default: the fixture only")
    ap.add_argument("--target", default="stub", choices=["stub", "plugin", "external"])
    ap.add_argument("--plugin-factory", default="plugin_factory:make_endpoint")
    ap.add_argument("--repo", default=str(HERE.parents[1]))
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--skip-browser", action="store_true")
    ap.add_argument("--skip-probes", action="store_true")
    ap.add_argument("--require-real-browser", action="store_true", help="fail closed unless real plugin/external sidebar browser checks run")
    ap.add_argument("--port", type=int, help="disposable ExternalTarget endpoint")
    ap.add_argument("--token-file", type=Path)
    ap.add_argument("--run", help="run name (default: <date>-<target>-<bundles>)")
    args = ap.parse_args(argv)
    if args.require_real_browser and (args.target == "stub" or args.skip_browser or not args.ui_dir):
        ap.error("--require-real-browser needs --target plugin/external, --ui-dir, and browser execution")
    if args.target == "external" and (not args.port or not args.token_file):
        ap.error("external needs --port and --token-file for a disposable endpoint")
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    repo = Path(args.repo)
    work = Path(tempfile.mkdtemp(prefix="web-smoke-gates-"))
    env = environment(repo)
    bundles: list[tuple[str, Path, bool]] = []
    if args.ui_dir:
        reason = fixture.bundle_is_contract_page(Path(args.ui_dir))
        if reason:
            print(f"--ui-dir is not a section 7.1 bundle: {reason}", file=sys.stderr)
            return 2
        bundles.append(("sidebar", Path(args.ui_dir), True))
    if args.vendor_from:
        bundles.append(("fixture", fixture.build_fixture(work / "fixture", Path(args.vendor_from)), True))
        bundles.append(("negative-control", fixture.build_fixture(work / "fixture-unsafe", Path(args.vendor_from),
                                                                   unsafe=True), False))
    if not bundles:
        print("give --ui-dir and/or --vendor-from", file=sys.stderr)
        return 2
    probe_bundle = bundles[0][1]
    run = args.run or f"{time.strftime('%Y%m%d-%H%M')}-{args.target}-{bundles[0][0]}"

    def gdir(gate: str) -> Path:
        return evidence / gate / run

    def make_target(bundle: Path) -> Any:
        if args.target == "external":
            return targets.ExternalTarget(args.port, args.token_file)
        if args.target == "plugin":
            return targets.PluginTarget(bundle, args.plugin_factory, repo)
        return targets.StubTarget(bundle, fast=True, repo=repo)

    summary: dict[str, Any] = {"environment": env, "target": args.target, "bundles": [b[0] for b in bundles]}
    probe_results: list[dict] = []
    if not args.skip_probes:
        for p in probes.PROBES:
            r = probes.run_probe(p, lambda: make_target(probe_bundle), args.slow)
            r["bundle"] = ""
            probe_results.append(r)
            print(f"{r['status'].upper():5} {r['id']:34} {r['seconds']:6.1f}s {r.get('note', '')}", flush=True)
        write_json(gdir("G1") / "probes.json", [r for r in probe_results if r["gate"] == "G1"])
        write_json(gdir("G2") / "probes.json", [r for r in probe_results if r["gate"] == "G2"])
    browser_results: list[dict] = []
    negative: dict[str, Any] = {}
    if not args.skip_browser:
        shots = gdir("G3") / "screens"
        shots.mkdir(parents=True, exist_ok=True)
        for tag, bundle, strict in bundles:
            only = [] if strict else ["g3.r1_corpus"]
            res = browser_smoke.run_scenarios(lambda bundle=bundle: make_target(bundle), evidence=shots, tag=tag, only=only)
            if strict:
                browser_results.extend(res)
            else:
                corpus_run = next(r for r in res if r.get("id") == "g3.r1_corpus")
                failed = {c["label"] for c in corpus_run["checks"] if not c["ok"]}
                negative = {"failed_checks": sorted(failed), "expected_to_fail": list(NEGATIVE_EXPECTED_FAILS),
                            "detected": all(any(f.startswith(e) for f in failed) for e in NEGATIVE_EXPECTED_FAILS)}
                write_json(gdir("G3") / "negative-control.json", res)
        for f in list(shots.glob("*ax-roles.json")):
            gdir("G5").mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(gdir("G5") / f.name))
        for f in list(shots.glob("*sanitizer-profile.json")):
            shutil.move(str(f), str(gdir("G3") / f.name))
        write_json(gdir("G3") / "browser.json", [r for r in browser_results if r.get("gate", "").startswith("G3")
                                                     or r.get("id") == "browser"])
        write_json(gdir("G5") / "browser.json", [r for r in browser_results if "G5" in r.get("gate", "")])
    chrome = next((r.get("chrome") for r in browser_results if r.get("id") == "browser"), None)
    summary.update({"chrome": chrome, "probes": tally(probe_results), "browser": tally(browser_results),
                    "negative_control": negative})
    write_json(evidence / f"summary-{run}.json", summary)
    lines = [f"# web_smoke acceptance run {run} ({env['when']})", "",
             f"- Target: **{args.target}** (lane V stub unless stated); bundles: {', '.join(b[0] for b in bundles)}",
             f"- Commit {env['commit']}, Python {env['python']}, {env['os']}, {chrome or 'no browser run'}",
             f"- Section 3.3 vectors reproduced: {env['vectors_ok']}",
             f"- Probe matrix: {tally(probe_results)}; browser: {tally(browser_results)}",
             f"- Negative control (DOMPurify default profile, no link pass) detected by the R1 checks: "
             f"{negative.get('detected')} ({len(negative.get('failed_checks', []))} checks failed as intended)", "",
             "## Probe matrix (G1, G2 black-box)", "", table(probe_results), "",
             "## Browser (G3, G5)", "", table(browser_results), ""]
    (evidence / f"SUMMARY-{run}.md").write_text(redact("\n".join(lines)), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    ok = all(r["status"] in ("pass", "skip", "info") for r in probe_results + browser_results)
    if args.require_real_browser:
        ok = ok and bool(browser_results) and any(r.get("status") == "pass" for r in browser_results) and not any(r.get("status") == "skip" for r in browser_results)
    print(f"evidence written to {evidence}; probes {tally(probe_results)}, browser {tally(browser_results)}, "
          f"negative control detected: {negative.get('detected')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
