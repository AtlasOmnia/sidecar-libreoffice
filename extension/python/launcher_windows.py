# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Windows adapter for the Sidecar launcher (docs/web-protocol.md section 13.2).

Shipped on every system, imported only on Windows. In 0.5.0 Windows has no app window: Open
Sidecar opens the bootstrap file in the user's default browser as a normal tab (owner decision D1
applied to Windows after spike S3). The Edge app window, with its DIP scaling, raise by PID and
Startup Boost handling, is planned for 0.5.1. No native code is loaded into LibreOffice.
"""
from __future__ import annotations

import os
from pathlib import Path


def find_browsers() -> list[tuple[str, str]]:
    """No app-window browser in 0.5.0, so the launcher always uses tab mode on Windows."""
    return []


def cache_dir() -> Path:
    value = os.environ.get("LOCALAPPDATA", "")
    path = Path(value)
    if not value or not path.is_absolute():
        raise OSError("LOCALAPPDATA is not set")
    return path


def spawn(argv: list[str]) -> int:
    raise OSError("no app window on Windows in this version")


def running(pid: int) -> bool:
    return False


def open_default(path: Path) -> None:
    """Open the bootstrap file with the default handler for .html (ShellExecute "open")."""
    os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only


def raise_pid(pid: int) -> bool:
    return False


def terminate(pid: int, wait: float = 0.0) -> None:
    return None
