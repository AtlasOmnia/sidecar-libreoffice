# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Owner-only checks for the files the bridge keeps in the LibreOffice profile.

The pairing token, the port file and the sidebar preference live in
``<UserInstallation>/user/hermes-bridge``. Other unprivileged local accounts must not be able
to read the token, or to replace any of these files.

- POSIX: no symlink, the right file type, a single link, owned by this user, and no group or
  other permission bits. These are mode-bit checks; extended (macOS or NFSv4) ACLs are not read.
- Windows: no reparse point (symlink or junction), the right file type, a single link, owned by
  this user or the Administrators group, and a DACL whose effective allow entries name only
  this user, SYSTEM, Administrators and OWNER RIGHTS. Permission bits from ``os.stat`` do not
  describe Windows access, so they are never consulted there.

Trust boundary: SYSTEM, Administrators and any account that holds backup, restore,
take-ownership or debug privileges can bypass these checks. They are trusted, not kept out.

Creation (:func:`create_private_file`) is exclusive and owner-only from the first moment. On
Windows the file is created by ``CreateFileW(CREATE_NEW)`` with an explicit protected DACL
(this user, SYSTEM, Administrators), so no inheritable entry of the directory applies to it.
The opened object is validated before any byte is written. A failure removes only the object
that was opened, never a name that something else now occupies.

Reads (:func:`read_private_bytes`) check the opened object, not just the path: the file is
opened, its identity is compared with the checked path, and its type, link count and owner and
permissions (or DACL) are checked again on the open handle. A file swapped in or hard-linked in
between is refused. Only the final path component is checked for links. A read that reaches a
genuinely private file through a symlinked or junctioned *ancestor* is allowed: what is
protected is the bytes actually read, not the path. Writers that depend on the path use
:func:`pinned_directory`.

Stdlib only (``ctypes`` on Windows).
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat

IS_WINDOWS = os.name == "nt"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Well-known SIDs allowed besides the current user. OWNER RIGHTS stands for the object's owner,
# which is itself required to be this user or Administrators; new folders under a Windows 11
# profile carry SYSTEM, Administrators and OWNER RIGHTS entries (measured on a Windows 11 test host).
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"


class UnsafePath(OSError):
    """The path is not a private regular file or directory of this user."""


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _check_type(path: Path, info: os.stat_result, directory: bool) -> None:
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise UnsafePath(f"{path.name} is a link")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"{path.name} is not a directory")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise UnsafePath(f"{path.name} is not a single regular file")


def _posix_mask(secret: bool) -> int:
    # A secret may not be readable by others; a non-secret setting only may not be writable.
    return 0o077 if secret else 0o022


def _check_posix(path: Path, info: os.stat_result, secret: bool = True) -> None:
    if info.st_uid != os.geteuid():
        raise UnsafePath(f"{path.name} belongs to another user")
    if stat.S_IMODE(info.st_mode) & _posix_mask(secret):
        raise UnsafePath(f"{path.name} permissions are insecure")


def check_private(path: Path, *, directory: bool, secret: bool = True) -> None:
    """Raise :class:`UnsafePath` unless ``path`` is a private file or directory of this user.

    ``secret=False`` (POSIX only) also accepts a file others may read but not write, such as
    the port setting. Windows always requires the private DACL.
    """
    path = Path(path)
    info = os.lstat(path)
    _check_type(path, info, directory)
    if IS_WINDOWS:
        _windows().check_path(str(path))
    else:
        _check_posix(path, info, secret)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (owner-only on POSIX) if missing, then require it to be private."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(path, directory=True)
    return path


def read_private_bytes(path: Path, limit: int, *, secret: bool = True) -> bytes:
    """Read at most ``limit`` bytes from a private file, checking the opened object itself:
    same identity as the checked path, a single regular file, and private (owner and mode
    bits, or the handle's owner, DACL and link count on Windows)."""
    path = Path(path)
    check_private(path, directory=False, secret=secret)
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafePath(f"{path.name} was replaced while it was opened")
        _check_type(path, info, directory=False)  # type and link count of the opened object
        if IS_WINDOWS:
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
            _windows().check_handle(handle)
            _windows().check_single_file(handle)
        else:
            _check_posix(path, info, secret)
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > limit:
        raise UnsafePath(f"{path.name} is too large")
    return data


def _unlink_if_same(path: Path, opened: os.stat_result) -> None:
    """Remove ``path`` only while it still names the object we opened (POSIX has no unlink by
    descriptor; the remaining window needs write access to the directory)."""
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
        os.unlink(path)


def create_private_file(path: Path, data: bytes) -> None:
    """Create a new private file; never follows or truncates an existing one.

    The opened object is validated before the first byte is written; on any failure only the
    object that was opened is removed."""
    path = Path(path)
    data = bytes(data)
    if IS_WINDOWS:
        api = _windows()
        handle = api.create_new(str(path))  # protected owner-only DACL, nothing inherited
        try:
            api.check_handle(handle)
            api.check_single_file(handle)
            api.write_all(handle, data)
        except BaseException:
            api.delete_handle(handle)  # deletes the opened file itself, not whatever has its name
            raise
        finally:
            api.close(handle)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o600)
    opened = None
    try:
        opened = os.fstat(fd)
        _check_type(path, opened, directory=False)
        _check_posix(path, opened, secret=True)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except BaseException:
        if opened is not None:
            _unlink_if_same(path, opened)
        raise
    finally:
        os.close(fd)


class PinnedDirectory:
    """Directories from the file-system anchor to a folder, held open (see pinned_directory)."""

    def __init__(self) -> None:
        self.entries: list[tuple[Path, int, object]] = []  # path, fd or handle, POSIX identity

    def verify(self) -> None:
        """POSIX: refuse if any held component no longer has its path. Windows: the open
        handles already prevent every rename, deletion and replacement, so nothing can move."""
        if IS_WINDOWS:
            return
        for path, _fd, identity in self.entries:
            try:
                info = os.lstat(path)
            except OSError:
                raise UnsafePath(f"{path.name} changed while it was in use") from None
            if (info.st_dev, info.st_ino) != identity or stat.S_ISLNK(info.st_mode):
                raise UnsafePath(f"{path.name} changed while it was in use")

    def close(self) -> None:
        while self.entries:
            _path, token, _identity = self.entries.pop()
            if IS_WINDOWS:
                _windows().close(token)
            else:
                os.close(token)


def _pin_component(pinned: PinnedDirectory, path: Path, kind: str) -> None:
    """Open one directory without following a link, check it, and keep it open.
    ``kind``: "ancestor" (no further check), "root" (not writable by others) or "private"."""
    if IS_WINDOWS:
        api = _windows()
        handle = api.open_directory(str(path))  # refuses a reparse point or a non-directory
        pinned.entries.append((path, handle, None))
        if kind != "ancestor":
            api.check_handle(handle, readers_ok=kind == "root")
        return
    before = os.lstat(path)
    _check_type(path, before, directory=True)
    fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK)
    info = os.fstat(fd)
    pinned.entries.append((path, fd, (info.st_dev, info.st_ino)))
    if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"{path.name} was replaced while it was opened")
    if kind != "ancestor":
        _check_posix(path, info, secret=kind == "private")


@contextmanager
def pinned_directory(folder: Path, *, create: bool = False):
    """Hold every directory from the file-system anchor to ``folder`` open while the caller
    works inside ``folder``, so its path names the checked directory throughout.

    ``folder.parent`` is the trusted root, the profile directory the caller resolved: it must
    be owned by this user (or Administrators on Windows) and not writable by others (POSIX: no
    group or other write bit; Windows: other accounts may hold only read, list and traverse
    rights, nothing that adds, deletes, renames or re-permissions an entry).
    ``folder`` itself must be private. No component may be a symlink or reparse point.

    Windows: each directory is opened by ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT``
    and without ``FILE_SHARE_DELETE``, so until the block ends no component can be renamed,
    deleted or replaced by a junction, by anyone who lacks the privileges outside the trust
    boundary. POSIX: a descriptor cannot prevent a rename, so :meth:`PinnedDirectory.verify`
    detects a switch instead; the caller verifies after pinning, before the replace and before
    cleanup, and a detected switch refuses the replace. (The POSIX preference
    code walks with ``dir_fd`` instead and uses this only where ``dir_fd`` is missing.)
    """
    folder = Path(folder)
    if not folder.is_absolute() or ".." in folder.parts or len(folder.parts) < 3:
        raise UnsafePath("invalid directory")
    pinned = PinnedDirectory()
    try:
        components = [Path(folder.anchor)]
        for part in folder.parts[1:]:
            components.append(components[-1] / part)
        for path in components[:-2]:
            _pin_component(pinned, path, "ancestor")
        _pin_component(pinned, components[-2], "root")
        if create and not os.path.lexists(folder):
            pinned.verify()
            os.mkdir(folder, 0o700)
        _pin_component(pinned, folder, "private")
        pinned.verify()
        yield pinned
    finally:
        pinned.close()


_WINDOWS = None


def _windows():
    global _WINDOWS
    if _WINDOWS is None:
        _WINDOWS = _WindowsAcl()
    return _WINDOWS


class _WindowsAcl:
    """DACL inspection and handle-based file operations through advapi32 and kernel32.
    Signatures follow the Win32 headers (aclapi.h, securitybaseapi.h, sddl.h, fileapi.h,
    winbase.h); they were exercised on Windows 11 with CPython 3.14."""

    SE_FILE_OBJECT = 1
    OWNER_SECURITY_INFORMATION = 0x1
    DACL_SECURITY_INFORMATION = 0x4
    TOKEN_QUERY = 0x8
    TOKEN_USER_CLASS = 1
    ACCESS_ALLOWED_ACE_TYPE = 0
    ACCESS_DENIED_ACE_TYPE = 1
    INHERIT_ONLY_ACE = 0x8
    # Rights that read, list or traverse but cannot add, remove, rename or re-permission anything:
    # FILE_READ_DATA/LIST_DIRECTORY, FILE_READ_EA, FILE_EXECUTE/TRAVERSE, FILE_READ_ATTRIBUTES,
    # READ_CONTROL, SYNCHRONIZE, GENERIC_READ and GENERIC_EXECUTE.
    READ_ONLY_RIGHTS = 0x1 | 0x8 | 0x20 | 0x80 | 0x20000 | 0x100000 | 0x80000000 | 0x20000000
    SDDL_REVISION_1 = 1
    FILE_LIST_DIRECTORY = 0x1
    FILE_READ_ATTRIBUTES = 0x80
    DELETE = 0x10000
    READ_CONTROL = 0x20000
    SYNCHRONIZE = 0x100000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_DISPOSITION_INFO_CLASS = 4

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PVOID = ctypes.c_void_p
        self.GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        self.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(PVOID),
                                               ctypes.POINTER(PVOID), ctypes.POINTER(PVOID), ctypes.POINTER(PVOID),
                                               ctypes.POINTER(PVOID)]
        self.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.GetSecurityInfo = advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(PVOID),
                                         ctypes.POINTER(PVOID), ctypes.POINTER(PVOID), ctypes.POINTER(PVOID),
                                         ctypes.POINTER(PVOID)]
        self.GetSecurityInfo.restype = wintypes.DWORD
        self.GetAce = advapi32.GetAce
        self.GetAce.argtypes = [PVOID, wintypes.DWORD, ctypes.POINTER(PVOID)]
        self.GetAce.restype = wintypes.BOOL
        self.ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
        self.ConvertSidToStringSidW.argtypes = [PVOID, ctypes.POINTER(wintypes.LPWSTR)]
        self.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = \
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(PVOID), ctypes.POINTER(wintypes.ULONG)]
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.IsValidSid = advapi32.IsValidSid
        self.IsValidSid.argtypes = [PVOID]
        self.IsValidSid.restype = wintypes.BOOL
        self.OpenProcessToken = advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.OpenProcessToken.restype = wintypes.BOOL
        self.GetTokenInformation = advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, PVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.GetTokenInformation.restype = wintypes.BOOL
        self.GetCurrentProcess = kernel32.GetCurrentProcess
        self.GetCurrentProcess.argtypes = []
        self.GetCurrentProcess.restype = wintypes.HANDLE
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL
        self.LocalFree = kernel32.LocalFree
        self.LocalFree.argtypes = [PVOID]
        self.LocalFree.restype = PVOID

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", PVOID), ("bInheritHandle", wintypes.BOOL)]

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                        ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                        ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                        ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                        ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]

        self.SECURITY_ATTRIBUTES, self.BY_HANDLE_FILE_INFORMATION = SECURITY_ATTRIBUTES, BY_HANDLE_FILE_INFORMATION
        self.CreateFileW = kernel32.CreateFileW
        self.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.CreateFileW.restype = wintypes.HANDLE
        self.WriteFile = kernel32.WriteFile
        self.WriteFile.argtypes = [wintypes.HANDLE, PVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), PVOID]
        self.WriteFile.restype = wintypes.BOOL
        self.GetFileInformationByHandle = kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        self.GetFileInformationByHandle.restype = wintypes.BOOL
        self.SetFileInformationByHandle = kernel32.SetFileInformationByHandle
        self.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, PVOID, wintypes.DWORD]
        self.SetFileInformationByHandle.restype = wintypes.BOOL
        self.INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        self.user_sid = self._current_user_sid()

    def _error(self, what: str) -> OSError:
        code = self.ctypes.get_last_error()
        error = self.ctypes.WinError(code)  # maps e.g. ERROR_FILE_EXISTS to FileExistsError
        error.strerror = f"{what}: {error.strerror}"
        return error

    def _sid_string(self, sid) -> str:
        if not sid or not self.IsValidSid(sid):
            raise UnsafePath("security descriptor has no valid SID")
        text = self.wintypes.LPWSTR()
        if not self.ConvertSidToStringSidW(sid, self.ctypes.byref(text)):
            raise UnsafePath("SID could not be read")
        try:
            return str(text.value)
        finally:
            self.LocalFree(self.ctypes.cast(text, self.ctypes.c_void_p))

    def _current_user_sid(self) -> str:
        ctypes, wintypes = self.ctypes, self.wintypes
        token = wintypes.HANDLE()
        if not self.OpenProcessToken(self.GetCurrentProcess(), self.TOKEN_QUERY, ctypes.byref(token)):
            raise UnsafePath("process token is unavailable")
        try:
            needed = wintypes.DWORD()
            self.GetTokenInformation(token, self.TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(max(needed.value, 64))
            if not self.GetTokenInformation(token, self.TOKEN_USER_CLASS, buffer, len(buffer), ctypes.byref(needed)):
                raise UnsafePath("process user is unavailable")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }.
            sid = ctypes.c_void_p.from_buffer(buffer).value
            return self._sid_string(sid)
        finally:
            self.CloseHandle(token)

    def _check(self, status: int, owner, dacl, descriptor, readers_ok: bool = False) -> None:
        ctypes = self.ctypes
        try:
            if status != 0:
                raise UnsafePath(f"security information unavailable (error {status})")
            if self._sid_string(owner.value) not in (self.user_sid, ADMINISTRATORS_SID):
                raise UnsafePath("owned by another account")
            if not dacl.value:
                raise UnsafePath("no access control list: everyone has access")
            # ACL header: BYTE AclRevision; BYTE Sbz1; WORD AclSize; WORD AceCount; WORD Sbz2.
            count = ctypes.c_ushort.from_address(dacl.value + 4).value
            trusted = {self.user_sid, SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID}
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.GetAce(dacl, index, ctypes.byref(ace)):
                    raise UnsafePath("access control entry could not be read")
                # ACE_HEADER: BYTE AceType; BYTE AceFlags; WORD AceSize; then ACCESS_MASK and the SID.
                ace_type = ctypes.c_ubyte.from_address(ace.value).value
                ace_flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                # An inherit-only entry grants nothing on this object. It would apply to a child
                # created with inherited security, which is why create_private_file never does that.
                if ace_flags & self.INHERIT_ONLY_ACE or ace_type == self.ACCESS_DENIED_ACE_TYPE:
                    continue
                if ace_type != self.ACCESS_ALLOWED_ACE_TYPE:
                    raise UnsafePath("unsupported access control entry")
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if readers_ok and not mask & ~self.READ_ONLY_RIGHTS:
                    continue  # others may read this directory but not change its namespace
                if mask and self._sid_string(ace.value + 8) not in trusted:
                    raise UnsafePath("another account has access")
        finally:
            if descriptor.value:
                self.LocalFree(descriptor)

    def _query(self, function, target, readers_ok: bool = False) -> None:
        ctypes = self.ctypes
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        status = function(target, self.SE_FILE_OBJECT, self.OWNER_SECURITY_INFORMATION | self.DACL_SECURITY_INFORMATION,
                          ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        self._check(status, owner, dacl, descriptor, readers_ok)

    def check_path(self, path: str) -> None:
        self._query(self.GetNamedSecurityInfoW, path)

    def check_handle(self, handle: int, readers_ok: bool = False) -> None:
        """``readers_ok``: also accept other accounts' read, list and traverse rights (used for
        the trusted root, the Windows analogue of "not writable by others")."""
        self._query(self.GetSecurityInfo, self.wintypes.HANDLE(handle), readers_ok)

    def private_sddl(self) -> str:
        """Owner this user; a protected DACL (``P``: nothing inherited) granting full control to
        this user, SYSTEM and Administrators, with no inheritable entries of its own."""
        return f"O:{self.user_sid}D:P(A;;FA;;;{self.user_sid})(A;;FA;;;SY)(A;;FA;;;BA)"

    def file_info(self, handle: int):
        info = self.BY_HANDLE_FILE_INFORMATION()
        if not self.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            raise self._error("file information unavailable")
        return info

    def check_single_file(self, handle: int) -> None:
        info = self.file_info(handle)
        if info.dwFileAttributes & (self.FILE_ATTRIBUTE_REPARSE_POINT | self.FILE_ATTRIBUTE_DIRECTORY):
            raise UnsafePath("opened object is not a regular file")
        if info.nNumberOfLinks != 1:
            raise UnsafePath("opened file has more than one name")

    def create_new(self, path: str) -> int:
        ctypes = self.ctypes
        descriptor, size = ctypes.c_void_p(), self.wintypes.ULONG()
        if not self.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                self.private_sddl(), self.SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(size)):
            raise self._error("private security descriptor could not be built")
        try:
            attributes = self.SECURITY_ATTRIBUTES(ctypes.sizeof(self.SECURITY_ATTRIBUTES), descriptor, False)
            # Share mode 0: nobody can open, rename or delete the file while it is being filled.
            handle = self.CreateFileW(path, self.GENERIC_WRITE | self.READ_CONTROL | self.DELETE | self.FILE_READ_ATTRIBUTES,
                                      0, ctypes.byref(attributes), self.CREATE_NEW,
                                      self.FILE_ATTRIBUTE_NORMAL | self.FILE_FLAG_OPEN_REPARSE_POINT, None)
            if handle is None or handle == self.INVALID_HANDLE_VALUE:
                raise self._error("private file could not be created")
            return handle
        finally:
            self.LocalFree(descriptor)

    def write_all(self, handle: int, data: bytes) -> None:
        ctypes = self.ctypes
        buffer = (ctypes.c_char * max(len(data), 1)).from_buffer_copy(data or b"\0")
        offset = 0
        while offset < len(data):
            written = self.wintypes.DWORD()
            if not self.WriteFile(handle, ctypes.addressof(buffer) + offset, len(data) - offset,
                                  ctypes.byref(written), None) or not written.value:
                raise self._error("private file could not be written")
            offset += written.value

    def delete_handle(self, handle: int) -> None:
        """Mark the opened file itself for deletion when its handle closes (best effort)."""
        flag = self.ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO { BOOLEAN DeleteFile; }
        self.SetFileInformationByHandle(handle, self.FILE_DISPOSITION_INFO_CLASS, self.ctypes.byref(flag), 1)

    def close(self, handle: int) -> None:
        self.CloseHandle(handle)

    def open_directory(self, path: str) -> int:
        """Open a directory itself (not a reparse target), without FILE_SHARE_DELETE. Listing
        access is requested because Windows only enforces share modes against opens that hold
        data or delete access; attribute-only opens would not block a rename."""
        handle = self.CreateFileW(path, self.FILE_LIST_DIRECTORY | self.FILE_READ_ATTRIBUTES | self.READ_CONTROL | self.SYNCHRONIZE,
                                  self.FILE_SHARE_READ | self.FILE_SHARE_WRITE, None, self.OPEN_EXISTING,
                                  self.FILE_FLAG_BACKUP_SEMANTICS | self.FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle is None or handle == self.INVALID_HANDLE_VALUE:
            raise self._error(f"{Path(path).name or path} could not be opened")
        try:
            attributes = self.file_info(handle).dwFileAttributes
            if attributes & self.FILE_ATTRIBUTE_REPARSE_POINT:
                raise UnsafePath(f"{Path(path).name or path} is a link")
            if not attributes & self.FILE_ATTRIBUTE_DIRECTORY:
                raise UnsafePath(f"{Path(path).name or path} is not a directory")
        except BaseException:
            self.CloseHandle(handle)
            raise
        return handle
