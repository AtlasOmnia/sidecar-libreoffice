# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""macOS adapter for the Sidecar launcher (docs/web-protocol.md section 13.2).

Shipped on every system, imported only on macOS. Everything here runs a fixed program with an
argument list (never a shell) and a cleaned environment. No native code is loaded into
LibreOffice: raising a window asks ``/usr/bin/osascript`` to activate the browser by its PID.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

# (name, path of the executable inside the app bundle), in preference order. Fixed locations in
# /Applications and ~/Applications only; no LaunchServices lookup.
BUNDLES = (
    ("Google Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Microsoft Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
)
OPEN = "/usr/bin/open"
OSASCRIPT = "/usr/bin/osascript"
DROPPED_PREFIXES = ("PYTHON", "DYLD_", "URE_", "SAL_")
OPEN_TIMEOUT_SECONDS = 10.0
RAISE_TIMEOUT_SECONDS = 5.0
TERMINATE_WAIT_SECONDS = 3.0
# Activates the running application with this PID. The PID is an argument, never part of the
# script text. NSApplicationActivateAllWindows = 1.
RAISE_SCRIPT = ("function run(argv) { ObjC.import('AppKit'); "
                "var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0], 10)); "
                "if (!app || app.isNil()) return 'missing'; "
                "return app.activateWithOptions(1) ? 'raised' : 'refused'; }")
_CHILDREN: dict[int, Any] = {}


def roots() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def find_browsers() -> list[tuple[str, str]]:
    found = []
    for name, relative in BUNDLES:
        for root in roots():
            path = root / relative
            if path.is_file() and os.access(path, os.X_OK):
                found.append((name, str(path)))
                break
    return found


def cache_dir() -> Path:
    return Path.home() / "Library" / "Caches"


def clean_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """LibreOffice's environment without the variables that steer its own runtime."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if not key.upper().startswith(DROPPED_PREFIXES)}


def spawn(argv: list[str]) -> int:
    """Start the browser detached from LibreOffice's session; returns its PID."""
    process = subprocess.Popen(argv, env=clean_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                               start_new_session=True, shell=False)
    _CHILDREN[process.pid] = process
    return process.pid


def running(pid: int) -> bool:
    """True while a browser this LibreOffice started with ``pid`` has not exited."""
    process = _CHILDREN.get(pid)
    return process is not None and process.poll() is None


def open_default(path: Path) -> None:
    """D1 tab mode: the default handler for .html files opens the bootstrap file."""
    subprocess.run([OPEN, str(path)], env=clean_environment(), stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=OPEN_TIMEOUT_SECONDS,
                   check=True, shell=False)


def raise_pid(pid: int) -> bool:
    """Bring the browser this LibreOffice started to the front; False when it could not."""
    if os.environ.get("HERMES_LO_SIDECAR_NO_NATIVE") == "1" or not running(pid):
        return False
    try:
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", RAISE_SCRIPT, str(int(pid))],
                                env=clean_environment(), stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=RAISE_TIMEOUT_SECONDS, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"raised"


def terminate(pid: int, wait: float = TERMINATE_WAIT_SECONDS) -> None:
    """Ask the browser this LibreOffice started to quit (SIGTERM: Chromium shuts down normally),
    then wait at most ``wait`` seconds (never more than TERMINATE_WAIT_SECONDS) for it to exit."""
    process = _CHILDREN.get(pid)
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(max(0.0, min(float(wait), TERMINATE_WAIT_SECONDS)))
    except subprocess.TimeoutExpired:
        pass
