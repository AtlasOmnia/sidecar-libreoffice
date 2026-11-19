#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Verify the GUI-installed extension in the isolated profile matches the built .oxt (read-only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dev_paths import PROFILE  # noqa: E402


def member_hashes(oxt: Path) -> dict[str, str]:
    with zipfile.ZipFile(oxt) as archive:
        return {
            info.filename: hashlib.sha256(archive.read(info)).hexdigest()
            for info in archive.infolist() if not info.is_dir()
        }


def installed_dirs(profile: Path, name: str) -> list[Path]:
    cache = profile / "user" / "uno_packages" / "cache" / "uno_packages"
    return sorted(cache.glob(f"*/{name}")) if cache.is_dir() else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oxt", type=Path)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    options = parser.parse_args()
    expected = member_hashes(options.oxt)
    report = {"oxt": str(options.oxt), "oxt_sha256": hashlib.sha256(options.oxt.read_bytes()).hexdigest(), "candidates": []}
    match = None
    for directory in installed_dirs(options.profile, options.oxt.name):
        mismatched = sorted(
            member for member, digest in expected.items()
            if not (directory / member).is_file()
            or hashlib.sha256((directory / member).read_bytes()).hexdigest() != digest
        )
        report["candidates"].append({"dir": str(directory), "mismatched": mismatched})
        if not mismatched:
            match = str(directory)
    report["installed_match"] = match
    print(json.dumps(report, indent=2))
    return 0 if match else 3


if __name__ == "__main__":
    sys.exit(main())
