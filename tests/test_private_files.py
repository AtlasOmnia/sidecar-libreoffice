# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Owner-only file checks (private_files) and their use for the token, port and theme files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extension" / "python"))
import private_files as P  # noqa: E402
import panel_registry as REG  # noqa: E402

POSIX = os.name != "nt"


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bridge = self.root / "user" / "hermes-bridge"


@unittest.skipUnless(POSIX, "POSIX permission rules")
class PosixRuleTests(Base):
    def test_private_directory_and_file_are_accepted_and_open_ones_refused(self):
        P.ensure_private_dir(self.bridge)
        self.assertEqual(os.stat(self.bridge).st_mode & 0o777, 0o700)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        self.assertEqual(os.stat(self.bridge / "token").st_mode & 0o777, 0o600)
        self.assertEqual(P.read_private_bytes(self.bridge / "token", 4096), b"t" * 40)
        os.chmod(self.bridge / "token", 0o640)
        with self.assertRaises(P.UnsafePath):
            P.read_private_bytes(self.bridge / "token", 4096)
        os.chmod(self.bridge, 0o750)
        with self.assertRaises(P.UnsafePath):
            P.check_private(self.bridge, directory=True)

    def test_non_secret_files_may_be_readable_but_never_writable_by_others(self):
        P.ensure_private_dir(self.bridge)
        path = self.bridge / "port"
        path.write_text("18000\n")
        os.chmod(path, 0o644)
        self.assertEqual(P.read_private_bytes(path, 16, secret=False), b"18000\n")
        with self.assertRaises(P.UnsafePath):
            P.read_private_bytes(path, 16)
        os.chmod(path, 0o664)
        with self.assertRaises(P.UnsafePath):
            P.read_private_bytes(path, 16, secret=False)

    def test_links_hard_links_wrong_types_and_oversize_are_refused(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        (self.bridge / "link").symlink_to(self.bridge / "token")
        os.link(self.bridge / "token", self.root / "second-name")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (self.root / "linked-dir").symlink_to(elsewhere)
        for call in (lambda: P.read_private_bytes(self.bridge / "link", 4096),
                     lambda: P.read_private_bytes(self.bridge / "token", 4096),  # two names now
                     lambda: P.check_private(self.root / "linked-dir", directory=True),
                     lambda: P.check_private(self.bridge, directory=False)):
            with self.assertRaises(P.UnsafePath):
                call()
        os.unlink(self.root / "second-name")
        P.create_private_file(self.bridge / "big", b"x" * 5000)
        with self.assertRaises(P.UnsafePath):
            P.read_private_bytes(self.bridge / "big", 4096)

    def test_file_replaced_between_check_and_open_is_refused(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        P.create_private_file(self.bridge / "intruder", b"i" * 40)
        real_open = os.open

        def swapping_open(path, *args, **kwargs):
            if Path(path).name == "token":
                os.replace(self.bridge / "intruder", self.bridge / "token")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(P.os, "open", swapping_open), self.assertRaises(P.UnsafePath) as caught:
            P.read_private_bytes(self.bridge / "token", 4096)
        self.assertIn("replaced", str(caught.exception))

    def test_create_never_overwrites_or_follows(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"first")
        with self.assertRaises(FileExistsError):
            P.create_private_file(self.bridge / "token", b"second")
        target = self.root / "target"
        (self.bridge / "dangling").symlink_to(target)
        with self.assertRaises(OSError):
            P.create_private_file(self.bridge / "dangling", b"x")
        self.assertFalse(target.exists())
        self.assertEqual(P.read_private_bytes(self.bridge / "token", 100), b"first")

    def test_permission_failure_is_an_error_not_a_bypass(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        os.chmod(self.bridge / "token", 0o000)
        try:
            with self.assertRaises(OSError):
                P.read_private_bytes(self.bridge / "token", 4096)
        finally:
            os.chmod(self.bridge / "token", 0o600)


class WindowsDispatchTests(Base):
    """On Windows the DACL decides; POSIX permission bits are never consulted there."""

    class FakeAcl:
        def __init__(self, allow):
            self.allow, self.paths, self.handles = allow, [], []

        def check_path(self, path):
            self.paths.append(path)
            if not self.allow:
                raise P.UnsafePath("another account has access")

        def check_handle(self, handle):
            self.handles.append(handle)
            if not self.allow:
                raise P.UnsafePath("another account has access")

    def test_windows_uses_the_acl_and_ignores_mode_bits(self):
        self.bridge.mkdir(parents=True)
        os.chmod(self.bridge, 0o777)  # what CPython reports for every ordinary Windows directory
        for allow in (True, False):
            fake = self.FakeAcl(allow)
            with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "_windows", lambda: fake):
                if allow:
                    P.check_private(self.bridge, directory=True)
                else:
                    with self.assertRaises(P.UnsafePath):
                        P.check_private(self.bridge, directory=True)
            self.assertEqual(fake.paths, [str(self.bridge)])

    def test_a_reparse_point_is_refused_before_the_acl_is_read(self):
        info = os.lstat(self.root)
        reparse = mock.Mock(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1)
        fake = self.FakeAcl(True)
        with mock.patch.object(P, "IS_WINDOWS", True), mock.patch.object(P, "_windows", lambda: fake), \
                mock.patch.object(P.os, "lstat", lambda _p: reparse), self.assertRaises(P.UnsafePath):
            P.check_private(self.root, directory=True)
        self.assertEqual(fake.paths, [])


@unittest.skipUnless(POSIX, "exercises the portable path on a POSIX file system")
class PortablePreferenceTests(Base):
    """The Windows preference path (no dir_fd), forced on POSIX."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(REG, "FD_WALK", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge.parent.mkdir(parents=True)

    def test_round_trip_and_malformed_preferences_fall_back_to_dark(self):
        self.assertEqual(REG.read_theme(self.bridge), "dark")
        REG.write_theme(self.bridge, "light")
        self.assertEqual(REG.read_theme(self.bridge), "light")
        self.assertEqual(sorted(p.name for p in self.bridge.iterdir()), ["sidebar-theme.json"])
        for raw in (b"{", b'{"theme":"blue"}', b'{"theme":"light","x":1}', b"\xff", b" " * 200):
            (self.bridge / "sidebar-theme.json").write_bytes(raw)
            self.assertEqual(REG.read_theme(self.bridge), "dark", raw)
        with self.assertRaises(ValueError):
            REG.write_theme(self.bridge, "auto")

    def test_linked_or_shared_locations_are_refused(self):
        REG.write_theme(self.bridge, "light")
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text(json.dumps({"theme": "light"}))
        os.unlink(self.bridge / "sidebar-theme.json")
        (self.bridge / "sidebar-theme.json").symlink_to(elsewhere)
        self.assertEqual(REG.read_theme(self.bridge), "dark")
        with self.assertRaises(OSError):
            REG.write_theme(self.bridge, "dark")
        self.assertEqual(json.loads(elsewhere.read_text()), {"theme": "light"})
        os.unlink(self.bridge / "sidebar-theme.json")
        os.chmod(self.bridge, 0o777)
        with self.assertRaises(OSError):
            REG.write_theme(self.bridge, "dark")
        self.assertEqual(REG.read_theme(self.bridge), "dark")
        for bad in (Path("relative/hermes-bridge"), self.root / "x" / ".." / "hermes-bridge", self.root / "other"):
            with self.assertRaises(OSError):
                REG.write_theme(bad, "dark")


@unittest.skipUnless(POSIX, "POSIX permission rules")
class StartupConfigTests(Base):
    """load_startup_config fails closed on every unsafe token, port or directory."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "tests"))
        from test_extension_bridge import BRIDGE
        self.BRIDGE = BRIDGE
        patcher = mock.patch.object(BRIDGE, "profile_bridge_dir", lambda _ctx: self.bridge)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self):
        return self.BRIDGE.load_startup_config(object())

    def test_port_file_may_be_world_readable_but_not_writable_or_garbage(self):
        _d, token, port, _l = self.load()
        self.assertEqual(port, self.BRIDGE.DEFAULT_PORT)
        (self.bridge / "port").write_text("18555\n")
        os.chmod(self.bridge / "port", 0o644)
        self.assertEqual(self.load()[2], 18555)
        self.assertEqual(self.load()[1], token)
        for mode, text in ((0o666, "18555\n"), (0o600, "abc\n"), (0o600, "80\n")):
            os.chmod(self.bridge / "port", 0o600)
            (self.bridge / "port").write_text(text)
            os.chmod(self.bridge / "port", mode)
            with self.assertRaises(RuntimeError, msg=(oct(mode), text)):
                self.load()

    def test_linked_token_or_linked_directory_refuses_to_start(self):
        self.load()
        real = self.root / "real-token"
        os.replace(self.bridge / "token", real)
        (self.bridge / "token").symlink_to(real)
        with self.assertRaises(RuntimeError):
            self.load()
        os.unlink(self.bridge / "token")
        os.replace(real, self.bridge / "token")
        self.load()
        moved = self.root / "moved"
        os.replace(self.bridge, moved)
        self.bridge.symlink_to(moved)
        with self.assertRaises(RuntimeError):
            self.load()

    def test_sidebar_token_read_refuses_links_and_open_permissions(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_sidebar import SIDEBAR
        _d, token, _p, _l = self.load()
        self.assertEqual(SIDEBAR.read_pairing_token(self.bridge / "token"), token)
        os.chmod(self.bridge / "token", 0o644)
        with self.assertRaises(SIDEBAR.ChatUnavailable) as caught:
            SIDEBAR.read_pairing_token(self.bridge / "token")
        self.assertIn("unsafe", str(caught.exception))
        os.chmod(self.bridge / "token", 0o600)
        (self.bridge / "alias").symlink_to(self.bridge / "token")
        with self.assertRaises(SIDEBAR.ChatUnavailable):
            SIDEBAR.read_pairing_token(self.bridge / "alias")


@unittest.skipUnless(os.name == "nt", "real Windows ACLs")
class RealWindowsAclTests(Base):
    def test_profile_folder_is_private_and_a_shared_or_linked_one_is_not(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        self.assertEqual(P.read_private_bytes(self.bridge / "token", 4096), b"t" * 40)
        shared = self.root / "shared"
        shared.mkdir()
        subprocess.run(["icacls", str(shared), "/grant", "*S-1-1-0:(OI)(CI)(R)"], check=True, capture_output=True)
        with self.assertRaises(P.UnsafePath):
            P.check_private(shared, directory=True)
        junction = self.root / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.bridge)], check=True, capture_output=True)
        try:
            with self.assertRaises(P.UnsafePath):
                P.check_private(junction, directory=True)
        finally:
            os.rmdir(junction)

    def test_inherit_only_untrusted_grant_never_reaches_a_new_private_file(self):
        P.ensure_private_dir(self.bridge)
        icacls(self.bridge, "/grant", "*S-1-1-0:(OI)(IO)(R)")
        P.check_private(self.bridge, directory=True)  # grants nothing on the folder itself
        control = self.bridge / "inherited-control"
        control.write_bytes(b"x")  # ordinary creation inherits the Everyone entry
        with self.assertRaises(P.UnsafePath):
            P.check_private(control, directory=False)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        P.check_private(self.bridge / "token", directory=False)
        listing = icacls(self.bridge / "token")
        self.assertNotIn("(I)", listing)  # protected DACL: nothing inherited
        self.assertNotIn("Everyone", listing)
        self.assertEqual(P.read_private_bytes(self.bridge / "token", 4096), b"t" * 40)

    def test_creation_is_exclusive_and_a_failure_deletes_only_the_opened_file(self):
        P.ensure_private_dir(self.bridge)
        path = self.bridge / "token"
        api = P._windows()
        seen = {}

        def observed_write(handle, data):
            for name, attempt in (("read", lambda: path.read_bytes()),
                                  ("rename", lambda: os.rename(path, self.bridge / "moved"))):
                try:
                    attempt()
                    seen[name] = "allowed"
                except OSError as exc:
                    seen[name] = type(exc).__name__
            raise OSError("disk full")
        with mock.patch.object(api, "write_all", observed_write), self.assertRaises(OSError):
            P.create_private_file(path, b"t" * 40)
        self.assertEqual(seen, {"read": "PermissionError", "rename": "PermissionError"})
        self.assertFalse(os.path.lexists(path))
        self.assertFalse(os.path.lexists(self.bridge / "moved"))
        with mock.patch.object(api, "check_handle", side_effect=P.UnsafePath("refused")), \
                mock.patch.object(api, "write_all") as write, self.assertRaises(P.UnsafePath):
            P.create_private_file(path, b"t" * 40)
        write.assert_not_called()
        self.assertFalse(os.path.lexists(path))
        P.create_private_file(path, b"first")
        with self.assertRaises(FileExistsError):
            P.create_private_file(path, b"second")
        self.assertEqual(P.read_private_bytes(path, 100), b"first")

    def test_native_security_descriptors(self):
        """Owner and DACL rules on descriptors built by Windows itself from SDDL. Changing a real
        file's owner to another account needs a privilege, so wrong owner is shown this way."""
        import ctypes
        from ctypes import wintypes
        api, user = P._windows(), P._windows().user_sid
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        get_owner, get_dacl = advapi32.GetSecurityDescriptorOwner, advapi32.GetSecurityDescriptorDacl
        get_owner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(wintypes.BOOL)]
        get_owner.restype = get_dacl.restype = wintypes.BOOL

        def check(sddl, readers_ok=False):
            descriptor, size = ctypes.c_void_p(), wintypes.ULONG()
            self.assertTrue(api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)), sddl)
            owner, dacl, flag, present = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.BOOL(), wintypes.BOOL()
            self.assertTrue(get_owner(descriptor, ctypes.byref(owner), ctypes.byref(flag)))
            self.assertTrue(get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(flag)))
            api._check(0, owner, dacl, descriptor, readers_ok)  # frees the descriptor
        accepted = [f"O:{user}D:P(A;;FA;;;{user})", "O:BAD:P(A;;FA;;;BA)", f"O:{user}D:P",
                    f"O:{user}D:P(A;;FA;;;OW)(A;;FA;;;SY)", f"O:{user}D:P(A;OIIO;FA;;;WD)(A;;FA;;;{user})",
                    f"O:{user}D:P(D;;FA;;;WD)(A;;FA;;;{user})", f"O:{user}D:P(A;;0;;;WD)", api.private_sddl()]
        refused = {f"O:BUD:P(A;;FA;;;{user})": "another account", f"O:{user}D:NO_ACCESS_CONTROL": "no access control",
                   f"O:{user}D:P(A;;GR;;;WD)": "another account", f"O:{user}D:P(A;;FA;;;AU)": "another account",
                   f"O:{user}D:P(XA;;FA;;;WD;(Member_of {{SID(BA)}}))": "unsupported"}
        for sddl in accepted:
            check(sddl)
        for sddl, reason in refused.items():
            with self.assertRaises(P.UnsafePath, msg=sddl) as caught:
                check(sddl)
            self.assertIn(reason, str(caught.exception), sddl)
        # the trusted root (readers_ok): others may read, list and traverse, never change entries
        for sddl in (f"O:{user}D:P(A;;FR;;;WD)", f"O:{user}D:P(A;;0x1200a9;;;WD)", f"O:{user}D:P(A;;GRGX;;;WD)"):
            check(sddl, readers_ok=True)
            with self.assertRaises(P.UnsafePath, msg=sddl):
                check(sddl)
        for rights in ("FW", "GA", "GW", "0x2", "0x4", "0x40", "0x100", "SD", "WD", "WO", "0x2000000"):
            with self.assertRaises(P.UnsafePath, msg=rights):
                check(f"O:{user}D:P(A;;{rights};;;WD)", readers_ok=True)


def icacls(path, *args):
    return subprocess.run(["icacls", str(path), *args], check=True, capture_output=True, text=True).stdout


class FakeWinApi:
    """Stands in for private_files._WindowsAcl and records the order of every step. Files and
    directories are real (descriptors stand in for handles)."""

    def __init__(self, fail=()):
        self.events, self.fail, self.files, self.pins = [], set(fail), {}, set()

    def _event(self, name, detail=None):
        self.events.append((name, detail))
        if name in self.fail:
            raise P.UnsafePath(f"{name} refused")

    def check_path(self, path):
        self._event("check_path", Path(path).name)

    def check_handle(self, handle, readers_ok=False):
        self._event("check_handle", (handle, readers_ok))

    def check_single_file(self, handle):
        self._event("check_single_file", handle)

    def create_new(self, path):
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.files[handle] = [path, False]
        self.events.append(("create_new", Path(path).name))
        return handle

    def write_all(self, handle, data):
        self._event("write_all", handle)
        os.write(handle, data)

    def delete_handle(self, handle):
        self.events.append(("delete_handle", handle))
        self.files[handle][1] = True

    def open_directory(self, path):
        handle = os.open(path, os.O_RDONLY)
        self.pins.add(handle)
        self.events.append(("open_directory", path))
        return handle

    def close(self, handle):
        self.events.append(("close_pin" if handle in self.pins else "close", handle))
        self.pins.discard(handle)
        os.close(handle)
        path, delete = self.files.pop(handle, (None, False))
        if delete:
            os.unlink(path)

    def names(self):
        return [name for name, _detail in self.events]


class CreationOrderTests(Base):
    """No secret byte is written before the opened object is validated; a failure removes only
    the object that was opened."""

    def windows(self, fake):
        stack = mock.patch.multiple(P, IS_WINDOWS=True, _windows=lambda: fake)
        stack.start()
        self.addCleanup(stack.stop)

    def test_windows_create_validates_the_handle_before_writing(self):
        self.bridge.mkdir(parents=True)
        fake = FakeWinApi()
        self.windows(fake)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        self.assertEqual(fake.names(), ["create_new", "check_handle", "check_single_file", "write_all", "close"])
        for step in ("check_handle", "check_single_file"):
            fake.events.clear()
            fake.fail = {step}
            with self.assertRaises(P.UnsafePath):
                P.create_private_file(self.bridge / ("refused-" + step), b"secret")
            self.assertNotIn("write_all", fake.names())
            self.assertEqual(fake.names()[-2:], ["delete_handle", "close"])
            self.assertFalse(os.path.lexists(self.bridge / ("refused-" + step)))

    @unittest.skipUnless(POSIX, "POSIX descriptors")
    def test_posix_create_checks_the_descriptor_before_writing(self):
        P.ensure_private_dir(self.bridge)
        events = []
        real_fstat, real_lstat, real_write = os.fstat, os.lstat, os.write
        with mock.patch.object(P.os, "fstat", lambda fd: events.append("check") or real_fstat(fd)), \
                mock.patch.object(P.os, "lstat", lambda p: events.append("check") or real_lstat(p)), \
                mock.patch.object(P.os, "write", lambda fd, b: events.append("write") or real_write(fd, b)):
            P.create_private_file(self.bridge / "token", b"t" * 40)
        self.assertIn("write", events)
        self.assertLess(events.index("check"), events.index("write"), events)

        def shared(fd):  # the opened object turns out to be readable by others
            info = real_fstat(fd)
            return os.stat_result((info.st_mode | 0o044,) + tuple(info)[1:])
        with mock.patch.object(P.os, "fstat", shared), mock.patch.object(P.os, "write") as write, \
                self.assertRaises(P.UnsafePath):
            P.create_private_file(self.bridge / "refused", b"secret")
        write.assert_not_called()
        self.assertFalse(os.path.lexists(self.bridge / "refused"))

        def foreign(fd):  # owned by another account
            info = real_fstat(fd)
            return os.stat_result(tuple(info)[:4] + (os.geteuid() + 1,) + tuple(info)[5:])
        with mock.patch.object(P.os, "fstat", foreign), mock.patch.object(P.os, "write") as write, \
                self.assertRaises(P.UnsafePath):
            P.create_private_file(self.bridge / "foreign", b"secret")
        write.assert_not_called()

    @unittest.skipUnless(POSIX, "POSIX descriptors")
    def test_posix_write_failure_removes_the_file_but_never_a_substituted_name(self):
        P.ensure_private_dir(self.bridge)
        path = self.bridge / "token"
        with mock.patch.object(P.os, "write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            P.create_private_file(path, b"t" * 40)
        self.assertFalse(os.path.lexists(path))
        substitute = self.bridge / "substitute"
        substitute.write_bytes(b"someone else's file")

        def swap_then_fail(_fd, _data):
            os.replace(substitute, path)
            raise OSError("disk full")
        with mock.patch.object(P.os, "write", swap_then_fail), self.assertRaises(OSError):
            P.create_private_file(path, b"t" * 40)
        self.assertEqual(path.read_bytes(), b"someone else's file")

    def test_bridge_refuses_to_start_when_the_token_cannot_be_created_privately(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_extension_bridge import BRIDGE
        with mock.patch.object(BRIDGE, "profile_bridge_dir", lambda _ctx: self.bridge), \
                mock.patch.object(BRIDGE.private_files, "create_private_file", side_effect=P.UnsafePath("refused")), \
                self.assertRaises(RuntimeError) as caught:
            BRIDGE.load_startup_config(object())
        self.assertIn("could not be created privately", str(caught.exception))


class OpenedObjectTests(Base):
    """read_private_bytes re-checks type and link count on the object it actually opened."""

    def test_hard_link_added_between_check_and_open_is_refused(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        real_open = os.open

        def linking_open(path, *args, **kwargs):
            if Path(path).name == "token":
                os.link(self.bridge / "token", self.bridge / "second-name")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(P.os, "open", linking_open), self.assertRaises(P.UnsafePath) as caught:
            P.read_private_bytes(self.bridge / "token", 4096)
        self.assertIn("single regular file", str(caught.exception))

    def test_link_count_change_reported_only_by_the_open_handle_is_refused(self):
        """Windows: GetFileInformationByHandle on the opened handle (faked here)."""
        self.bridge.mkdir(parents=True)
        (self.bridge / "token").write_bytes(b"t" * 40)
        fake = FakeWinApi(fail={"check_single_file"})
        msvcrt = types.SimpleNamespace(get_osfhandle=lambda fd: fd)
        with mock.patch.multiple(P, IS_WINDOWS=True, _windows=lambda: fake), \
                mock.patch.dict(sys.modules, {"msvcrt": msvcrt}), self.assertRaises(P.UnsafePath):
            P.read_private_bytes(self.bridge / "token", 4096)
        self.assertEqual(fake.names(), ["check_path", "check_handle", "check_single_file"])

    @unittest.skipUnless(POSIX, "POSIX ownership")
    def test_wrong_owner_on_the_opened_file_is_refused(self):
        P.ensure_private_dir(self.bridge)
        P.create_private_file(self.bridge / "token", b"t" * 40)
        real_fstat = os.fstat

        def foreign(fd):
            info = real_fstat(fd)
            return os.stat_result(tuple(info)[:4] + (os.geteuid() + 1,) + tuple(info)[5:])
        with mock.patch.object(P.os, "fstat", foreign), self.assertRaises(P.UnsafePath) as caught:
            P.read_private_bytes(self.bridge / "token", 4096)
        self.assertIn("another user", str(caught.exception))


class PinnedPreferenceTests(Base):
    """The portable (Windows) preference path: trusted root, every ancestor checked and pinned.
    Runs on Windows for real and on POSIX with dir_fd turned off."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(REG, "FD_WALK", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = self.root / "profiles"
        self.user = self.base / "a" / "user"
        self.user.mkdir(parents=True)
        self.folder = self.user / "hermes-bridge"

    def link_dir(self, link, target):
        if POSIX:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(os.unlink, link)
        else:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
            self.addCleanup(os.rmdir, link)

    def test_round_trip_through_the_pinned_path(self):
        self.assertEqual(REG.read_theme(self.folder), "dark")
        REG.write_theme(self.folder, "light")
        self.assertEqual(REG.read_theme(self.folder), "light")
        REG.write_theme(self.folder, "dark")
        self.assertEqual(REG.read_theme(self.folder), "dark")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [REG.PREFERENCE_NAME])

    def test_higher_ancestor_link_is_refused_for_reads_and_writes(self):
        real = self.base / "real" / "user"
        real.mkdir(parents=True)
        REG.write_theme(real / "hermes-bridge", "light")
        self.link_dir(self.base / "link", self.base / "real")
        linked = self.base / "link" / "user" / "hermes-bridge"
        self.assertEqual(REG.read_theme(linked), "dark")
        with self.assertRaises(OSError):
            REG.write_theme(linked, "dark")
        self.assertEqual(REG.read_theme(real / "hermes-bridge"), "light")
        fresh = self.base / "fresh" / "user"
        fresh.mkdir(parents=True)
        self.link_dir(self.base / "fresh-link", self.base / "fresh")
        with self.assertRaises(OSError):
            REG.write_theme(self.base / "fresh-link" / "user" / "hermes-bridge", "light")
        self.assertFalse(os.path.lexists(fresh / "hermes-bridge"))

    def test_ancestor_switched_after_the_checks_never_redirects_the_write(self):
        REG.write_theme(self.folder, "dark")
        real_create, outcome = P.create_private_file, {}

        def switching_create(path, data):
            try:
                os.rename(self.base / "a", self.base / "a-moved")
            except OSError as exc:
                outcome["rename"] = type(exc).__name__
            else:
                outcome["rename"] = "done"
                (self.base / "a" / "user" / "hermes-bridge").mkdir(parents=True, mode=0o700)  # look-alike
            return real_create(path, data)
        with mock.patch.object(P, "create_private_file", switching_create):
            if POSIX:
                with self.assertRaises(P.UnsafePath):
                    REG.write_theme(self.folder, "light")
            else:
                REG.write_theme(self.folder, "light")
        if POSIX:  # a descriptor cannot stop the rename, so the switch is detected and refused
            self.assertEqual(outcome["rename"], "done")
            self.assertFalse(os.path.lexists(self.folder / REG.PREFERENCE_NAME))
            original = self.base / "a-moved" / "user" / "hermes-bridge" / REG.PREFERENCE_NAME
            self.assertEqual(json.loads(original.read_text()), {"theme": "dark"})
        else:  # the held handles make the rename itself fail
            self.assertEqual(outcome["rename"], "PermissionError")
            self.assertEqual(REG.read_theme(self.folder), "light")

    def test_trusted_root_writable_by_others_is_refused_but_readable_is_not(self):
        if POSIX:
            os.chmod(self.user, 0o755)
        else:
            icacls(self.user, "/grant", "*S-1-1-0:(RX)")
        REG.write_theme(self.folder, "light")
        self.assertEqual(REG.read_theme(self.folder), "light")
        os.unlink(self.folder / REG.PREFERENCE_NAME)
        os.rmdir(self.folder)
        if POSIX:
            os.chmod(self.user, 0o777)
        else:
            icacls(self.user, "/grant", "*S-1-1-0:(W)")
        with self.assertRaises(OSError):
            REG.write_theme(self.folder, "light")
        self.assertFalse(os.path.lexists(self.folder))

    @unittest.skipUnless(POSIX, "fake handles are POSIX descriptors")
    def test_windows_order_pins_every_component_before_creating_and_until_cleanup(self):
        fake = FakeWinApi()
        real_replace = os.replace

        def recorded_replace(*args, **kwargs):
            fake.events.append(("replace", None))
            return real_replace(*args, **kwargs)
        with mock.patch.multiple(P, IS_WINDOWS=True, _windows=lambda: fake), \
                mock.patch.object(REG.os, "replace", recorded_replace):
            REG.write_theme(self.folder, "light")
        components = [Path(self.folder.anchor)]
        for part in self.folder.parts[1:]:
            components.append(components[-1] / part)
        opened = [detail for name, detail in fake.events if name == "open_directory"]
        self.assertEqual([Path(p) for p in opened], components)
        names = fake.names()
        last_open = max(i for i, n in enumerate(names) if n == "open_directory")
        first_release = names.index("close_pin")
        self.assertLess(last_open, names.index("create_new"))
        self.assertLess(names.index("replace"), first_release)
        self.assertEqual(names[first_release:], ["close_pin"] * len(components))
        self.assertEqual(fake.pins, set())
        # the trusted root and the folder were checked on their pinned handles
        checks = [detail[1] for name, detail in fake.events[:names.index("create_new")] if name == "check_handle"]
        self.assertEqual(checks, [True, False])  # root: readers allowed; folder: private
        self.assertEqual(json.loads((self.folder / REG.PREFERENCE_NAME).read_text()), {"theme": "light"})


if __name__ == "__main__":
    unittest.main()
