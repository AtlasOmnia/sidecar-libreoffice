#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Read-only snapshots and comparisons for a LibreOffice user profile."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "LibreOffice"


def snapshot(root: Path) -> dict[str, dict[str, Any]]:
    root = root.expanduser().resolve(strict=False)
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            item: dict[str, Any] = {
                "type": "other",
                "size": info.st_size,
                "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns,
            }
            if stat.S_ISLNK(info.st_mode):
                item["type"] = "symlink"
                item["target"] = os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                item["type"] = "directory"
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                item["type"] = "file"
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                item["sha256"] = digest.hexdigest()
            result[relative] = item
    return dict(sorted(result.items()))


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"path": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return value


def write_snapshot(root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot(root), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compare_files(before_path: Path, after_path: Path) -> int:
    diff = differences(load(before_path), load(after_path))
    if diff:
        print(json.dumps({"status": "CHANGED", "differences": diff}, indent=2, sort_keys=True))
        return 1
    print(json.dumps({"status": "UNCHANGED", "differences": []}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("output", type=Path)
    snapshot_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    options = parser.parse_args()
    if options.command == "snapshot":
        write_snapshot(options.root, options.output)
        print(json.dumps({"status": "SNAPSHOT", "path": str(options.output)}, sort_keys=True))
        return 0
    return compare_files(options.before, options.after)


if __name__ == "__main__":
    sys.exit(main())
