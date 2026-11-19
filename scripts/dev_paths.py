#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Locations used by the live-check scripts. Nothing here touches your everyday setup.

Everything lives under one development root, ``$HERMES_LO_DEV_ROOT`` (default
``~/.cache/hermes-libreoffice-dev``):

- ``profile/``      isolated LibreOffice profile (override: ``HERMES_LO_PROFILE``)
- ``tmp/``          temporary files for the isolated LibreOffice
- ``work/``         run outputs
"""
from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


DEV_ROOT = _path("HERMES_LO_DEV_ROOT", Path.home() / ".cache" / "hermes-libreoffice-dev")
PROFILE = _path("HERMES_LO_PROFILE", DEV_ROOT / "profile")
TMP = DEV_ROOT / "tmp"
WORK = DEV_ROOT / "work"
TOKEN_PATH = PROFILE / "user" / "hermes-bridge" / "token"

if __name__ == "__main__":
    for key in ("DEV_ROOT", "PROFILE", "TMP", "WORK", "TOKEN_PATH"):
        print(f"{key}={globals()[key]}")
