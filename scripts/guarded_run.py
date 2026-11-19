#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Run a command while proving the real LibreOffice profile is unchanged."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from real_profile_sentinel import DEFAULT_ROOT, compare_files, write_snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    temp_root = Path(os.environ.get("TMPDIR", "/tmp"))
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-real-profile-sentinel-", dir=temp_root) as directory:
        before = Path(directory) / "before.json"
        after = Path(directory) / "after.json"
        write_snapshot(options.root, before)
        try:
            child = subprocess.run(command, env=os.environ.copy(), check=False)
            child_rc = child.returncode
        finally:
            write_snapshot(options.root, after)
        compare_rc = compare_files(before, after)
        if compare_rc:
            print("guarded_run: real LibreOffice profile changed", file=sys.stderr)
            return 90
        return child_rc


if __name__ == "__main__":
    sys.exit(main())
