# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import importlib.util
import logging
import os
import socket
from pathlib import Path
import stat
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]


def load_bridge_module():
    uno = types.ModuleType("uno")
    uno.fileUrlToSystemPath = lambda value: value
    uno.createUnoStruct = lambda _name: types.SimpleNamespace()
    uno.getTypeByName = lambda name: name
    uno.getConstantByName = lambda _name: 1
    uno.Enum = lambda type_name, value: (type_name, value)
    unohelper = types.ModuleType("unohelper")
    unohelper.Base = type("UnoBase", (), {})
    unohelper.ImplementationHelper = lambda: types.SimpleNamespace(addImplementation=lambda *args: None)
    callback_module = types.ModuleType("com.sun.star.awt")
    callback_module.XCallback = type("XCallback", (), {})
    transfer_module = types.ModuleType("com.sun.star.datatransfer")
    transfer_module.XTransferable = type("XTransferable", (), {})
    frame_module = types.ModuleType("com.sun.star.frame")
    frame_module.XDispatch = type("XDispatch", (), {})
    frame_module.XDispatchProvider = type("XDispatchProvider", (), {})
    task_module = types.ModuleType("com.sun.star.task")
    task_module.XJob = type("XJob", (), {})
    modules = {
        "uno": uno,
        "unohelper": unohelper,
        "com": types.ModuleType("com"),
        "com.sun": types.ModuleType("com.sun"),
        "com.sun.star": types.ModuleType("com.sun.star"),
        "com.sun.star.awt": callback_module,
        "com.sun.star.datatransfer": transfer_module,
        "com.sun.star.frame": frame_module,
        "com.sun.star.task": task_module,
    }
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    extension_python = str(ROOT / "extension" / "python")
    sys.path.insert(0, extension_python)
    try:
        spec = importlib.util.spec_from_file_location(
            "tested_hermes_bridge", ROOT / "extension" / "python" / "hermes_bridge.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(extension_python)
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


BRIDGE = load_bridge_module()


class ImmediateCallback:
    def addCallback(self, callback, _data):
        callback.notify(None)


class NeverCallback:
    def addCallback(self, _callback, _data):
        return None


class LateCallback:
    """Queues callbacks and runs them only when the test says so (after a timeout)."""

    def __init__(self):
        self.queued = []

    def addCallback(self, callback, _data):
        self.queued.append(callback)

    def run_late(self):
        for callback in self.queued:
            callback.notify(None)


class ThreadCallback:
    def addCallback(self, callback, _data):
        threading.Thread(target=callback.notify, args=(None,), daemon=True).start()


class LostWakeupCallback:
    """Models the macOS VCL lost wakeup seen live on LibreOffice 26.8 (hidden window, bridge undo).

    The first posted callback is queued but its event-loop wakeup is lost, so it does not run.
    Any later post wakes the loop, which then runs every queued callback in FIFO order on a
    separate "main" thread. With no later post the first callback waits forever.
    """

    def __init__(self):
        self.queued = []
        self.posts = 0
        self.lock = threading.Lock()

    def addCallback(self, callback, _data):
        with self.lock:
            self.posts += 1
            self.queued.append(callback)
            if self.posts == 1:
                return
            pending, self.queued = self.queued, []
        threading.Thread(target=lambda: [item.notify(None) for item in pending], daemon=True).start()


class FakeUndoManager:
    def __init__(self):
        self.possible = True
        self.contexts = []

    def enterUndoContext(self, title):
        self.contexts.append(title)

    def leaveUndoContext(self):
        return None

    def isUndoPossible(self):
        return self.possible

    def getCurrentUndoActionTitle(self):
        return "Hermes: test"

    def undo(self):
        return None


class FakeDocument:
    def __init__(self, uid: str, kind: str):
        self.RuntimeUID = uid
        self.kind = kind
        self.Title = uid
        self.URL = ""

    def getPropertyValue(self, name):
        if name != "RuntimeUID":
            raise KeyError(name)
        return self.RuntimeUID

    def supportsService(self, service):
        return service == {
            "calc": "com.sun.star.sheet.SpreadsheetDocument",
            "writer": "com.sun.star.text.TextDocument",
            "impress": "com.sun.star.presentation.PresentationDocument",
        }[self.kind]


class FakeWriter(FakeDocument):
    def __init__(self, uid):
        super().__init__(uid, "writer")
        self.undo_manager = FakeUndoManager()
        self.fail_insert = False
        outer = self

        class Text:
            def createTextCursor(self):
                return types.SimpleNamespace(gotoStart=lambda _x: None, gotoEnd=lambda _x: None)

            def insertString(self, _cursor, _text, _absorb):
                if outer.fail_insert:
                    raise RuntimeError("insert failed midway")

        self.text = Text()

    def getText(self):
        return self.text

    def getUndoManager(self):
        return self.undo_manager


class BridgeLogicTests(unittest.TestCase):
    def runtime(self, callback=None):
        runtime = BRIDGE.BridgeRuntime.__new__(BRIDGE.BridgeRuntime)
        runtime.async_callback = callback or ImmediateCallback()
        runtime.logger = logging.getLogger("bridge-test")
        runtime.session_nonce = "1234abcd"
        runtime._documents = {}
        return runtime

    def test_runtime_uid_is_stable_and_session_scoped(self):
        runtime = self.runtime()
        doc = FakeDocument("runtime-1", "calc")
        self.assertEqual(runtime._id_for_document(doc), "lo:1234abcd:runtime-1")
        self.assertEqual(runtime._id_for_document(doc), "lo:1234abcd:runtime-1")

    def test_resolve_binds_exact_document_and_validates_type(self):
        runtime = self.runtime()
        calc = FakeDocument("calc-1", "calc")
        writer = FakeDocument("writer-1", "writer")
        runtime._enumerate_documents = lambda: [calc, writer]
        calc_id = runtime._id_for_document(calc)
        self.assertIs(runtime.resolve_document(calc_id, "calc"), calc)
        with self.assertRaises(BRIDGE.BridgeFault) as wrong_type:
            runtime.resolve_document(calc_id, "writer")
        self.assertEqual(wrong_type.exception.category, "Validation")
        with self.assertRaises(BRIDGE.BridgeFault) as missing:
            runtime.resolve_document("lo:1234abcd:missing", "calc")
        self.assertEqual(missing.exception.category, "NotFound")

    def test_read_failure_is_never_execution_uncertain(self):
        runtime = self.runtime()
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
        self.assertEqual(caught.exception.category, "Unavailable")

    def test_mutation_failure_after_begin_is_execution_uncertain(self):
        runtime = self.runtime()

        def write(begin):
            begin()
            raise RuntimeError("write failed")

        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(write, mutation=True)
        self.assertEqual(caught.exception.category, "ExecutionUncertain")
        self.assertEqual(caught.exception.state, "started")

    def test_validation_type_error_after_begin_is_not_downgraded(self):
        runtime = self.runtime()

        def write(begin):
            begin()
            raise ValueError("half written")

        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(write, mutation=True)
        self.assertEqual(caught.exception.category, "ExecutionUncertain")

    def test_mutation_failure_before_begin_is_not_uncertain(self):
        runtime = self.runtime()
        cases = [
            (RuntimeError("uno read failed"), "Unavailable"),
            (ValueError("bad"), "Validation"),
            (BRIDGE.BridgeFault("NotFound", "gone", 404), "NotFound"),
            (BRIDGE.BridgeFault("Stale", "changed", 409), "Stale"),
        ]
        for error, expected in cases:
            def write(_begin, error=error):
                raise error
            with self.subTest(expected=expected), self.assertRaises(BRIDGE.BridgeFault) as caught:
                runtime.dispatch(write, mutation=True)
            self.assertEqual(caught.exception.category, expected)
            self.assertEqual(caught.exception.state, "not_started")

    def test_first_run_generates_secure_token_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge_dir = Path(directory) / "hermes-bridge"
            original = BRIDGE.profile_bridge_dir
            BRIDGE.profile_bridge_dir = lambda _ctx: bridge_dir
            logger = logging.getLogger("sidecar_libreoffice_bridge")
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            try:
                _path, token, _port, _logger = BRIDGE.load_startup_config(object())
                _path2, token2, _port2, _logger2 = BRIDGE.load_startup_config(object())
            finally:
                BRIDGE.profile_bridge_dir = original
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
            self.assertEqual(token, token2)
            self.assertGreaterEqual(len(token.encode("utf-8")), 32)
            if os.name != "nt":  # Windows privacy is the DACL (test_private_files), not mode bits
                self.assertEqual(stat.S_IMODE(bridge_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((bridge_dir / "token").stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "chmod does not change Windows access; see RealWindowsAclTests")
    def test_insecure_existing_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge_dir = Path(directory) / "hermes-bridge"
            bridge_dir.mkdir(mode=0o700)
            token_path = bridge_dir / "token"
            token_path.write_text("x" * 48, encoding="utf-8")
            os.chmod(token_path, 0o644)
            original = BRIDGE.profile_bridge_dir
            BRIDGE.profile_bridge_dir = lambda _ctx: bridge_dir
            try:
                with self.assertRaisesRegex(RuntimeError, "permissions are insecure"):
                    BRIDGE.load_startup_config(object())
            finally:
                BRIDGE.profile_bridge_dir = original

    def _with_timeout(self, seconds):
        previous = BRIDGE.CALLBACK_TIMEOUT
        BRIDGE.CALLBACK_TIMEOUT = seconds
        self.addCleanup(setattr, BRIDGE, "CALLBACK_TIMEOUT", previous)

    def test_callback_never_started_is_cancelled_timeout(self):
        callback = LateCallback()
        runtime = self.runtime(callback)
        self._with_timeout(0)
        ran = []

        def write(begin):
            begin()
            ran.append(True)

        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(write, mutation=True)
        self.assertEqual(caught.exception.category, "Timeout")
        self.assertEqual(caught.exception.state, "not_started")
        callback.run_late()
        self.assertEqual(ran, [], "a cancelled mutation must never run later")

    def test_started_mutation_that_times_out_is_uncertain(self):
        runtime = self.runtime(ThreadCallback())
        self._with_timeout(0.05)
        release = threading.Event()

        def write(begin):
            begin()
            release.wait(2)

        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(write, mutation=True)
        release.set()
        self.assertEqual(caught.exception.category, "ExecutionUncertain")

    def test_started_read_that_times_out_is_timeout_not_uncertain(self):
        runtime = self.runtime(ThreadCallback())
        self._with_timeout(0.05)
        release = threading.Event()
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.dispatch(lambda: release.wait(2))
        release.set()
        self.assertEqual(caught.exception.category, "Timeout")
        self.assertEqual(caught.exception.state, "started")

    def test_lost_wakeup_is_recovered_by_reposting_a_no_op_wakeup(self):
        callback = LostWakeupCallback()
        runtime = self.runtime(callback)
        self._with_timeout(2.0)
        ran = []

        def write(begin):
            begin()
            ran.append(True)
            return "done"

        result = {}

        def run():
            result["value"] = runtime.dispatch(write, mutation=True)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(3.0)
        self.assertEqual(result.get("value"), "done", "a lost wakeup must not turn into a Timeout")
        self.assertEqual(ran, [True], "the queued call runs exactly once")
        self.assertGreaterEqual(callback.posts, 2, "the bridge re-posts a wakeup while the call is queued")

    def test_wakeup_reposts_stop_once_the_call_has_started(self):
        posts = []

        class CountingImmediate(ImmediateCallback):
            def addCallback(self, callback, data):
                posts.append(callback)
                super().addCallback(callback, data)

        runtime = self.runtime(CountingImmediate())
        self._with_timeout(1.0)
        self.assertEqual(runtime.dispatch(lambda: 7), 7)
        self.assertEqual(len(posts), 1, "no extra wakeups when LibreOffice picks the call up at once")

    def test_wakeup_reposts_are_no_ops(self):
        wakeup = BRIDGE.MainThreadWakeup()
        self.assertIsNone(wakeup.notify(None))

    def test_list_documents_reports_frontmost_without_using_it(self):
        runtime = self.runtime()
        calc = FakeDocument("calc-1", "calc")
        writer = FakeDocument("writer-1", "writer")
        runtime._enumerate_documents = lambda: [calc, writer]
        runtime.desktop = types.SimpleNamespace(getCurrentComponent=lambda: writer)
        documents = runtime.list_documents()["documents"]
        self.assertEqual([item["frontmost"] for item in documents], [False, True])
        calc_id = documents[0]["document_id"]
        self.assertIs(runtime.resolve_document(calc_id, "calc"), calc)

    def test_undo_and_writer_insert_mark_mutation_start(self):
        runtime = self.runtime()
        writer = FakeWriter("writer-1")
        runtime._enumerate_documents = lambda: [writer]
        writer_id = runtime._id_for_document(writer)
        writer.fail_insert = True
        with self.assertRaises(BRIDGE.BridgeFault) as caught:
            runtime.writer_insert_text({"document_id": writer_id, "text": "x", "position": "end"})
        self.assertEqual(caught.exception.category, "ExecutionUncertain")
        writer.undo_manager.possible = False
        with self.assertRaises(BRIDGE.BridgeFault) as nothing:
            runtime.undo({"document_id": writer_id})
        self.assertEqual(nothing.exception.category, "NotFound")
        self.assertEqual(nothing.exception.state, "not_started")


class ExclusiveListener(unittest.TestCase):
    """On Windows SO_REUSEADDR lets another socket (even another account's) bind the
    bridge's address and port while it listens. The listener must use SO_EXCLUSIVEADDRUSE there."""

    class Recorder:
        def __init__(self):
            self.options = []

        def setsockopt(self, level, name, value):
            self.options.append((level, name, value))

        def bind(self, address):
            self.bound = address

        def getsockname(self):
            return ("127.0.0.1", 1)

    def bind_as(self, windows):
        server = BRIDGE.BoundedHTTPServer.__new__(BRIDGE.BoundedHTTPServer)
        server.socket = self.Recorder()
        server.server_address = ("127.0.0.1", 0)
        exclusive = getattr(BRIDGE.socket, "SO_EXCLUSIVEADDRUSE", -5)
        with unittest.mock.patch.object(BRIDGE, "_WINDOWS", windows, create=True), \
                unittest.mock.patch.object(BRIDGE.socket, "SO_EXCLUSIVEADDRUSE", exclusive, create=True):
            server.server_bind()
        return server.socket.options, exclusive

    def test_windows_binds_exclusively_without_reuse(self):
        options, exclusive = self.bind_as(True)
        names = [name for _level, name, _value in options]
        self.assertIn(exclusive, names)
        self.assertNotIn(BRIDGE.socket.SO_REUSEADDR, names)

    def test_other_systems_keep_the_stdlib_reuse(self):
        options, exclusive = self.bind_as(False)
        names = [name for _level, name, _value in options]
        self.assertNotIn(exclusive, names)
        self.assertIn(BRIDGE.socket.SO_REUSEADDR, names)

    def test_a_second_socket_cannot_bind_the_listening_port(self):
        runtime = types.SimpleNamespace(token="t" * 43, port=0, logger=logging.getLogger("test-bridge"),
                                        invoke=lambda op, args: {})
        server = BRIDGE.make_server(runtime, 0)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                other.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
