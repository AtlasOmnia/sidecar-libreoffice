# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The sidebar chat endpoint's lifecycle inside Hermes, and the secrets it holds.

``LedgerContext`` models what Hermes's plugin manager does with a plugin's registrations
(hermes_cli/plugins_ledger.py): each one is a handle, and unloading (disable, reload, a failed
load) disposes them in REVERSE acquisition order, ``on_unload`` callbacks included. The
endpoint must be started last and its cleanup must run while the tool fence still exists.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
from test_chat_bridge import API_KEY, SESSION, TOKEN, FakeApiServer  # noqa: E402

import plugin  # noqa: E402
from plugin import chat_bridge  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class UnscopedSecretError(RuntimeError):
    pass


class SecretStore:
    """A stand-in for Hermes's ``agent.secret_scope`` with a switchable unscoped refusal."""

    def __init__(self, values):
        self.values, self.unscoped = dict(values), False
        module = types.ModuleType("agent.secret_scope")
        module.UnscopedSecretError = UnscopedSecretError
        module.get_secret = self.get_secret
        package = types.ModuleType("agent")
        package.secret_scope = module
        self.patch = mock.patch.dict(sys.modules, {"agent": package, "agent.secret_scope": module})

    def get_secret(self, name, default=None):
        if self.unscoped:
            raise UnscopedSecretError(name)
        return self.values.get(name, default)


class LedgerContext:
    def __init__(self, config, events):
        self.config, self.events, self.handles, self.hooks = config, events, [], {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, **kwargs):
        self.events.append(("tool", kwargs["name"]))
        self.handles.append(lambda: None)

    def register_skill(self, name, _path, description=""):
        self.events.append(("skill", name))
        self.handles.append(lambda: None)

    def register_hook(self, name, callback):
        self.hooks[name] = callback
        self.events.append(("hook", name))

        def release():
            self.events.append(("hook_removed", name))
            self.hooks.pop(name, None)
        self.handles.append(release)

    def on_unload(self, callback):
        self.events.append(("on_unload", callback.__name__))
        self.handles.append(callback)

    def unload(self):
        for release in reversed(self.handles):
            release()
        self.handles.clear()


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.api = FakeApiServer(self.events)
        self.addCleanup(self.api.close)
        self.store = SecretStore({"LIBREOFFICE_LIVE_BRIDGE_TOKEN": TOKEN, "API_SERVER_KEY": API_KEY})
        self.store.patch.start()
        self.addCleanup(self.store.patch.stop)
        gateway = mock.patch.object(plugin, "_in_gateway_process", return_value=True)
        gateway.start()
        self.addCleanup(gateway.stop)
        self.port = free_port()
        self.contexts = []
        self.addCleanup(self._unload_all)

    def _unload_all(self):
        for ctx in self.contexts:
            ctx.unload()

    def load(self):
        ctx = LedgerContext({"chat_port": self.port, "api_server_port": self.api.port}, self.events)
        self.contexts.append(ctx)
        plugin.register(ctx)
        return ctx

    def request(self, method="GET", path="/health", body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Host": f"127.0.0.1:{self.port}", chat_bridge.TOKEN_HEADER: TOKEN}
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class RegistrationOrderTests(LifecycleCase):
    def test_tools_and_fence_come_first_then_the_unload_cleanup_and_the_endpoint_last(self):
        original = chat_bridge.ChatBridge.start

        def start(bridge):
            self.events.append(("endpoint_started", None))
            return original(bridge)
        with mock.patch.object(chat_bridge.ChatBridge, "start", start):
            self.load()
        kinds = [kind for kind, _name in self.events]
        self.assertIn("endpoint_started", kinds)
        last_tool = max(i for i, kind in enumerate(kinds) if kind == "tool")
        self.assertLess(last_tool, kinds.index("hook"))
        self.assertLess(kinds.index("hook"), kinds.index("on_unload"))
        self.assertLess(kinds.index("on_unload"), kinds.index("endpoint_started"))

    def test_without_an_unload_api_the_endpoint_never_starts(self):
        class NoUnload(LedgerContext):
            on_unload = None
        ctx = NoUnload({"chat_port": self.port, "api_server_port": self.api.port}, self.events)
        self.contexts.append(ctx)
        plugin.register(ctx)
        self.assertFalse(port_open(self.port))
        self.assertIsNone(plugin._CHAT)

    def test_a_registration_that_fails_leaves_no_listener(self):
        ctx = LedgerContext({"chat_port": self.port, "api_server_port": self.api.port}, self.events)
        original = ctx.on_unload

        def failing_on_unload(callback):
            original(callback)
            raise RuntimeError("host refused the registration")
        ctx.on_unload = failing_on_unload
        self.contexts.append(ctx)
        with self.assertRaises(RuntimeError):
            plugin.register(ctx)
        self.assertFalse(port_open(self.port))
        self.assertIsNone(plugin._CHAT)
        # ...and the same when the endpoint started and the HOST then failed the load: it
        # disposes everything the plugin registered (plugins_loader: failed load path).
        ctx2 = self.load()
        self.assertTrue(port_open(self.port))
        ctx2.unload()
        self.assertFalse(port_open(self.port))


class DefaultOffTests(LifecycleCase):
    """The sidebar chat ships off: only an explicit, valid ``chat_port`` starts the endpoint."""

    def started(self, config):
        calls = []
        original = chat_bridge.ChatBridge.start

        def start(bridge):
            calls.append(bridge.port)
            return original(bridge)
        ctx = LedgerContext(config, self.events)
        self.contexts.append(ctx)
        with mock.patch.object(chat_bridge.ChatBridge, "start", start):
            plugin.register(ctx)
        return calls

    def test_the_default_configuration_starts_no_chat_listener(self):
        self.assertEqual(chat_bridge.DEFAULT_CHAT_PORT, 0)
        for config in ({}, {"api_server_port": self.api.port}, {"chat_port": 0}, {"chat_port": True},
                       {"chat_port": "8661"}, {"chat_port": 80}, {"chat_port": None}):
            with self.subTest(config=config):
                self.assertEqual(self.started(config), [])
                self.assertIsNone(plugin._CHAT)
        self.assertIn(("hook", "pre_tool_call"), self.events)   # the document tools and the fence still load

    def test_an_explicit_chat_port_starts_it_after_the_fence_and_unload_cleanup(self):
        self.assertEqual(self.started({"chat_port": self.port, "api_server_port": self.api.port}), [self.port])
        self.assertTrue(port_open(self.port))
        kinds = [kind for kind, _name in self.events]
        self.assertLess(kinds.index("hook"), kinds.index("on_unload"))
        self.assertEqual(self.request(), (200, {"ok": True, "status": "ok"}))

    def test_the_panel_and_the_documented_opt_in_port_agree(self):
        from test_sidebar import SIDEBAR
        self.assertEqual(SIDEBAR.DEFAULT_CHAT_PORT, chat_bridge.PANEL_CHAT_PORT)


class UnloadTests(LifecycleCase):
    def test_unload_closes_the_listener_before_the_fence_goes_and_a_reload_rebinds(self):
        ctx = self.load()
        self.assertEqual(self.request(), (200, {"ok": True, "status": "ok"}))
        seen_at_fence_removal = []
        original_unload = ctx.unload

        def unload():
            for index, release in enumerate(ctx.handles):
                if getattr(release, "__qualname__", "").endswith("register_hook.<locals>.release"):
                    def checked(release=release):
                        seen_at_fence_removal.append(port_open(self.port))
                        release()
                    ctx.handles[index] = checked
            original_unload()
        unload()
        self.assertEqual(seen_at_fence_removal, [False])
        self.assertFalse(port_open(self.port))
        self.assertIsNone(plugin._CHAT)
        self.load()                                   # reload: a fresh registration binds again
        self.assertEqual(self.request(), (200, {"ok": True, "status": "ok"}))

    def test_an_in_flight_run_is_stopped_and_settled_before_the_fence_is_removed(self):
        self.api.hold = threading.Event()             # the run keeps running until it is stopped
        ctx = self.load()
        results = []

        def ask():
            status, value = self.request("POST", "/chat", {"message": "Sum column B", "session_id": SESSION})
            self.events.append(("chat_answered", status))
            results.append((status, value))
        asker = threading.Thread(target=ask)
        asker.start()
        for _ in range(250):
            if self.api.posts():
                break
            threading.Event().wait(0.02)
        self.assertTrue(self.api.posts(), "the chat never reached the API server")
        ctx.unload()
        asker.join(20)
        kinds = [kind for kind, _name in self.events]
        self.assertIn("stop", kinds)
        # stopped, and seen to have settled, while the fence was still registered
        self.assertLess(kinds.index("stop"), kinds.index("settled"))
        self.assertLess(kinds.index("settled"), kinds.index("hook_removed"))
        self.assertEqual(results[0][0], 503)
        self.assertNotIn(API_KEY, json.dumps(results[0][1]))

    def test_a_listener_without_its_fence_refuses_to_forward(self):
        bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: API_KEY,
                                        api_port=self.api.port, fence_active=lambda: False)
        port = bridge.start()
        self.addCleanup(bridge.close)
        body = json.dumps({"message": "hi", "session_id": SESSION}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("POST", "/chat", body=body, headers={
            "Host": f"127.0.0.1:{port}", chat_bridge.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 503)
        connection.close()
        self.assertEqual(self.api.requests, [])


class SecretTests(LifecycleCase):
    def test_an_authoritative_empty_read_clears_the_cached_secret(self):
        values = iter(["first", "", None, "  "])
        provider = chat_bridge.SecretProvider(lambda: next(values))
        self.assertEqual([provider(), provider(), provider(), provider()], ["first", None, None, None])

    def test_the_plugin_keeps_a_secret_only_while_hermes_refuses_an_unscoped_read(self):
        plugin._TOKEN.refresh()
        self.assertEqual(plugin._TOKEN(), TOKEN)
        self.store.unscoped = True                   # a worker thread of a multiplexed gateway
        self.assertEqual(plugin._TOKEN(), TOKEN)
        self.store.unscoped = False
        self.store.values["LIBREOFFICE_LIVE_BRIDGE_TOKEN"] = ""   # the user removed the token
        self.assertIsNone(plugin._TOKEN())
        self.store.values["API_SERVER_KEY"] = ""
        self.assertIsNone(plugin._API_KEY())

    def test_a_removed_pairing_token_stops_the_endpoint_accepting_it(self):
        self.load()
        self.assertEqual(self.request()[0], 200)
        self.store.values["LIBREOFFICE_LIVE_BRIDGE_TOKEN"] = ""
        self.assertEqual(self.request()[0], 503)

    def test_unload_forgets_the_cached_secrets(self):
        ctx = self.load()
        self.assertEqual(plugin._TOKEN._value, TOKEN)
        ctx.unload()
        self.assertIsNone(plugin._TOKEN._value)
        self.assertIsNone(plugin._API_KEY._value)

    def test_a_key_that_is_not_a_header_value_never_reaches_logs_or_the_panel(self):
        marker = "sk-MARKER-" + "z" * 20
        bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: marker + "\r\nX-Injected: 1",
                                        api_port=self.api.port)
        port = bridge.start()
        self.addCleanup(bridge.close)
        root = logging.getLogger()
        with self.assertLogs(root, level=logging.DEBUG) as captured:
            logging.getLogger("probe").info("start")
            body = json.dumps({"message": "hi", "session_id": SESSION}).encode()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("POST", "/chat", body=body, headers={
                "Host": f"127.0.0.1:{port}", chat_bridge.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"})
            response = connection.getresponse()
            text = response.read().decode()
            connection.close()
            for _ in range(100):
                if any("chat" in line and "->" in line for line in captured.output):
                    break
                threading.Event().wait(0.02)
        self.assertEqual(response.status, 503)
        self.assertNotIn("MARKER", text)
        self.assertNotIn("MARKER", "\n".join(captured.output))
        self.assertEqual(self.api.requests, [])

    def test_logs_name_routes_by_label_never_by_the_requested_path(self):
        self.load()
        with self.assertLogs(chat_bridge.log, level=logging.INFO) as captured:
            self.assertEqual(self.request("GET", "/private-payroll-q3?x=1")[0], 404)
            self.assertEqual(self.request("POST", "/also-private", {"message": "hi", "session_id": SESSION})[0], 404)
            for _ in range(100):
                if sum("->" in line for line in captured.output) >= 2:
                    break
                threading.Event().wait(0.02)
        text = "\n".join(captured.output)
        self.assertIn("unknown -> 404", text)
        self.assertNotIn("private", text)


if __name__ == "__main__":
    unittest.main()
