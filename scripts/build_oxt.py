#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Build the LibreOffice extension deterministically with stdlib zipfile.

Every member is stored uncompressed (ZIP_STORED) with a fixed timestamp, permissions, creator
system and order, so the archive bytes do not depend on the zlib a Python is linked against or on
the operating system: any CPython >= 3.9 on macOS, Windows or Linux builds the same .oxt.

``--check`` also prints the build's SHA-256 and fails unless it equals the expected SHA-256 written
in README.md, the one place the release hash is recorded.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
OUTPUT = ROOT / "dist" / "sidecar-libreoffice.oxt"
README = ROOT / "README.md"
FILES = [
    "META-INF/manifest.xml",
    "description.xml",
    "README.txt",
    "Jobs.xcu",
    "Addons.xcu",
    "ProtocolHandler.xcu",
    "python/hermes_bridge.py",
    "python/range_logic.py",
    "python/office_ops.py",
    "python/hermes_sidebar.py",
    "python/panel_registry.py",
    "python/private_files.py",
    "python/sidecar_launcher.py",
    "python/window_follow.py",
    "python/launcher_macos.py",
    "python/launcher_windows.py",
    "Factories.xcu",
    "Sidebar.xcu",
    "dialogs/SidecarChatPanel.xdl",
    "icons/sidecar-deck.png",
]
# Repository files shipped at the root of the archive, so a standalone .oxt carries its license.
ROOT_FILES = ["LICENSE", "NOTICE"]
_DOCUMENTED = re.compile(r"Expected SHA-256 of `sidecar-libreoffice\.oxt`[^`]*`([0-9a-f]{64})`")


def documented_sha256() -> str:
    """The expected SHA-256 of the .oxt stated in README.md's install steps."""
    found = _DOCUMENTED.findall(README.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise SystemExit("README.md must state exactly one expected SHA-256 of `sidecar-libreoffice.oxt`")
    return found[0]


def main(argv: list[str] | None = None) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, path in [(name, SOURCE / name) for name in FILES] + [(name, ROOT / name) for name in ROOT_FILES]:
            if not path.is_file():
                raise SystemExit(f"missing extension input: {relative}")
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3  # Unix; zipfile defaults to 0 on Windows, which would change the bytes
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"built {OUTPUT}")
    if argv and "--check" in argv:
        digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
        documented = documented_sha256()
        print(f"sha256     {digest}\ndocumented {documented}")
        if digest != documented:
            raise SystemExit("the built .oxt does not match the SHA-256 in README.md")


if __name__ == "__main__":
    main(sys.argv[1:])
