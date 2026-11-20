# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The open launcher panels and the 0.4.0 theme preference, shared by the bridge and the sidebar.

LibreOffice loads each Python component file as its own module, so the two components meet
through this plain module, imported by name from the extension's ``python`` directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import threading
import uuid
import weakref
from contextlib import contextmanager
from typing import Any

_COMPONENT_DIR = str(Path(__file__).resolve().parent)
if _COMPONENT_DIR not in sys.path:
    sys.path.insert(0, _COMPONENT_DIR)
import private_files  # noqa: E402

_PANELS: list[weakref.ReferenceType] = []
_SAVED_THEMES: dict[Path, str | None] = {}
_SAVED_LOCK = threading.Lock()


def register(panel: Any) -> None:
    _PANELS.append(weakref.ref(panel))


def unregister(panel: Any) -> None:
    _PANELS[:] = [ref for ref in _PANELS if ref() is not None and ref() is not panel]


def live() -> list[Any]:
    _PANELS[:] = [ref for ref in _PANELS if ref() is not None and not getattr(ref(), "_disposed", False)]
    return [ref() for ref in _PANELS if ref() is not None]


def latest() -> Any:
    """The most recently opened live panel, or ``None``."""
    panels = live()
    return panels[-1] if panels else None


def saved_theme(folder: Path) -> str | None:
    """The Light/Dark choice the 0.4.0 panel saved in this profile, or ``None`` when it saved
    none. Read once per LibreOffice process; 0.5.0 never writes it."""
    folder = Path(folder)
    with _SAVED_LOCK:
        if folder not in _SAVED_THEMES:
            try:
                present = os.path.lexists(folder / PREFERENCE_NAME)
            except OSError:
                present = False
            _SAVED_THEMES[folder] = read_theme(folder) if present else None
        return _SAVED_THEMES[folder]


PREFERENCE_NAME = "sidebar-theme.json"
PREFERENCE_LIMIT = 128
# The descriptor walk below needs O_DIRECTORY, O_NOFOLLOW and dir_fd support (POSIX). Windows has
# none of them, so it uses the handle-checked private-file path instead (see private_files).
FD_WALK = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
           and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd)


def _validate_folder(folder: Path) -> Path:
    folder = Path(folder)
    if not folder.is_absolute() or ".." in folder.parts or folder.name != "hermes-bridge":
        raise OSError("Invalid preference directory")
    return folder


def _parse_theme(raw: bytes) -> str:
    value = json.loads(raw.decode("utf-8"))
    if isinstance(value, dict) and set(value) == {"theme"} and value["theme"] in ("light", "dark"):
        return value["theme"]
    return "dark"


# Portable path (Windows). The folder's parent is the trusted root: the LibreOffice user
# directory the sidebar resolved through PathSubstitution "$(user)". It must be owned by this
# user and not writable by others, the folder must be private, no component from the drive
# root down may be a link or reparse point, and every component stays pinned open (no
# FILE_SHARE_DELETE) through the temporary file's creation, the replacement and the cleanup.
# See private_files.pinned_directory.


def _read_theme_portable(folder: Path) -> str:
    folder = _validate_folder(folder)
    with private_files.pinned_directory(folder):
        raw = private_files.read_private_bytes(folder / PREFERENCE_NAME, PREFERENCE_LIMIT, secret=False)
    return _parse_theme(raw)


def _write_theme_portable(folder: Path, theme: str) -> None:
    folder = _validate_folder(folder)
    with private_files.pinned_directory(folder, create=True) as pinned:
        target = folder / PREFERENCE_NAME
        if os.path.lexists(target):
            private_files.check_private(target, directory=False, secret=False)
        temporary = folder / (".sidebar-theme-" + uuid.uuid4().hex)
        created = False
        try:
            private_files.create_private_file(temporary, (json.dumps({"theme": theme}) + "\n").encode("ascii"))
            created = True
            pinned.verify()
            os.replace(temporary, target)
            created = False
        finally:
            if created:
                pinned.verify()  # never delete through a switched path; refuse instead
                os.unlink(temporary)


@contextmanager
def _preference_directory(folder: Path, create: bool = False):
    """Walk with directory fds: no symlink ancestors, traversal, or credential reads."""
    folder = Path(folder)
    if not folder.is_absolute() or ".." in folder.parts or folder.name != "hermes-bridge":
        raise OSError("Invalid preference directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(folder.anchor, flags)
    try:
        for index, part in enumerate(folder.parts[1:], start=1):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create or index != len(folder.parts) - 1:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_theme(folder: Path) -> str:
    """Bounded nonblocking read of only our regular, unlinked-to-other-files preference."""
    try:
        if not FD_WALK:
            return _read_theme_portable(folder)
        with _preference_directory(folder) as directory:
            fd = os.open(PREFERENCE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128:
                    return "dark"
                value = json.loads(os.read(fd, 129).decode("utf-8"))
            finally:
                os.close(fd)
        if isinstance(value, dict) and set(value) == {"theme"} and value["theme"] in ("light", "dark"):
            return value["theme"]
    except (OSError, ValueError, UnicodeError):
        pass
    return "dark"


def write_theme(folder: Path, theme: str) -> None:
    """Atomic private preference replacement; never truncate or follow an existing file."""
    if theme not in ("light", "dark"):
        raise ValueError("Invalid theme")
    if not FD_WALK:
        _write_theme_portable(folder, theme)
        return
    with _preference_directory(folder, create=True) as directory:
        try:
            info = os.stat(PREFERENCE_NAME, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise OSError("Unsafe preference file")
        temporary = ".sidebar-theme-" + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write((json.dumps({"theme": theme}) + "\n").encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, PREFERENCE_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
