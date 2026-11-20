# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Write or check plugin/sidebar/SHA256SUMS, the list of files the plugin serves at /ui/.

    python3 scripts/sidebar_sums.py --check   # exit 1 if SHA256SUMS does not match the files
    python3 scripts/sidebar_sums.py --write   # rewrite SHA256SUMS from the files

The plugin serves a file only when it is listed in SHA256SUMS and its bytes still match, so this
list is the bundle's source of truth. Served files are the page's HTML, JavaScript, CSS and SVG.
Licences, VENDOR.json, UPSTREAM.json and SHA256SUMS itself are shipped but never served. Any other
kind of file in plugin/sidebar/ is an error (no binaries, archives or minified bundles).

A developer tool: it never runs from the plugin and never writes into an installed copy.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDEBAR = ROOT / "plugin" / "sidebar"
SUMS_NAME = "SHA256SUMS"
SERVED_SUFFIXES = (".html", ".js", ".css", ".svg")
NOT_SERVED = frozenset({
    SUMS_NAME,
    "UPSTREAM.json",
    "vendor/VENDOR.json",
    "vendor/marked.LICENSE.md",
    "vendor/dompurify.LICENSE",
})
# docs/web-protocol.md section 7: the only relative paths the plugin accepts under /ui/.
RELPATH_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
LINE_RE = re.compile(r"^([0-9a-f]{64})  (\S+)$")


class BundleError(Exception):
    """The bundle folder holds a file that may not be there."""


def bundle_files(sidebar: Path = SIDEBAR) -> tuple[list[str], list[str]]:
    """(served, not_served) relative paths, sorted. Raises BundleError for anything else."""
    served: list[str] = []
    not_served: list[str] = []
    problems: list[str] = []
    for path in sorted(sidebar.rglob("*")):
        rel = path.relative_to(sidebar).as_posix()
        if path.is_symlink():
            problems.append(f"{rel}: symbolic links are not allowed")
            continue
        if path.is_dir():
            continue
        if rel in NOT_SERVED:
            not_served.append(rel)
        elif rel.endswith(SERVED_SUFFIXES) and RELPATH_RE.match(rel):
            served.append(rel)
        elif rel.endswith(SERVED_SUFFIXES):
            problems.append(f"{rel}: name not servable (lowercase letters, digits, '.', '_', '-' only)")
        else:
            problems.append(f"{rel}: not a page file, licence or pin record")
    if problems:
        raise BundleError("; ".join(problems))
    return served, not_served


def render_sums(sidebar: Path = SIDEBAR) -> str:
    """SHA256SUMS text: '<64 hex>  <relpath>' per served file, sorted by path, LF, final newline."""
    served, _ = bundle_files(sidebar)
    lines = [f"{hashlib.sha256((sidebar / rel).read_bytes()).hexdigest()}  {rel}" for rel in served]
    return "".join(line + "\n" for line in lines)


def check(sidebar: Path = SIDEBAR) -> list[str]:
    """Differences between SHA256SUMS and the files; empty when they match."""
    try:
        expected = render_sums(sidebar)
    except BundleError as exc:
        return [str(exc)]
    sums = sidebar / SUMS_NAME
    if not sums.is_file():
        return [f"{SUMS_NAME} is missing"]
    actual = sums.read_bytes().decode("utf-8", errors="replace")
    if actual == expected:
        return []
    problems = []
    want: dict[str, str] = {}
    for line in expected.splitlines():
        digest, rel = line.split("  ", 1)
        want[rel] = digest
    have: dict[str, str] = {}
    for number, line in enumerate(actual.split("\n"), 1):
        if not line:
            continue
        match = LINE_RE.match(line)
        if not match:
            problems.append(f"{SUMS_NAME}:{number}: malformed line")
            continue
        have[match.group(2)] = match.group(1)
    for rel in sorted(set(want) | set(have)):
        if rel not in have:
            problems.append(f"{rel}: served file not listed")
        elif rel not in want:
            problems.append(f"{rel}: listed but not a served file")
        elif have[rel] != want[rel]:
            problems.append(f"{rel}: hash differs")
    if not problems:
        problems.append(f"{SUMS_NAME}: order or line endings differ (sorted, LF, final newline)")
    return problems


def main(argv: list[str] | None = None, sidebar: Path = SIDEBAR) -> int:
    parser = argparse.ArgumentParser(description="Write or check plugin/sidebar/SHA256SUMS.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare SHA256SUMS with the files (default)")
    mode.add_argument("--write", action="store_true", help="rewrite SHA256SUMS from the files")
    args = parser.parse_args(argv)
    if args.write:
        try:
            text = render_sums(sidebar)
        except BundleError as exc:
            print(f"sidebar_sums: {exc}", file=sys.stderr)
            return 1
        (sidebar / SUMS_NAME).write_bytes(text.encode("utf-8"))
        print(f"sidebar_sums: wrote {len(text.splitlines())} entries")
        return 0
    problems = check(sidebar)
    for problem in problems:
        print(f"sidebar_sums: {problem}", file=sys.stderr)
    if not problems:
        print("sidebar_sums: SHA256SUMS matches the served files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
