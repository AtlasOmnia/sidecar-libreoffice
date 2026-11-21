# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Unload with Sidecar windows attached (plan R12, gate G2): ``close()`` drops every session,
code and channel first, wakes parked long-polls (they answer once, 503 ``turned_off``), stops
driven turns through the 0.4.0 ownership path within the one close budget, and afterwards no
handler of these routes writes to a socket. With ``chat_port`` 0 nothing starts at all."""
from __future__ import annotations

import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import (API_KEY, PAIRING_TOKEN, FakeRunsApi, LauncherClient, PageClient,  # noqa: E402
                                   WebCase, code_of, eventually, make_fixture)

import plugin  # noqa: E402
from plugin import chat_bridge, web_api  # noqa: E402


def in_background(function, *args, **kwargs):
    results = []

    def run():
        try:
            results.append((time.monotonic(), function(*args, **kwargs)))
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            results.append((time.monotonic(), exc))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results


class CloseTests(WebCase):
    def bridge_options(self):
        return {"poll_interval": 0.05, "long_poll_seconds": 20.0}

    def park(self, page, turn):
        seq = self.followed_seq(page, turn)
        return in_background(page.turn, turn, seq, 0, 30.0)

    def test_parked_long_polls_answer_turned_off_before_close_returns_and_runs_drain(self):
        pages = [self.page(document_id=f"d{index}") for index in range(chat_bridge.MAX_ACTIVE)]
        turns = [self.start_turn(page) for page in pages]
        self.assertTrue(self.api.wait_for(lambda: len({c[1] for c in self.api.polls()}) >= len(turns)))
        parked = [self.park(page, turn) for page, turn in zip(pages, turns)]
        self.assertTrue(eventually(lambda: self.web._parked == chat_bridge.MAX_ACTIVE))
        records = [page_turn for page_turn in (self.web.sessions.all())]
        started = time.monotonic()
        remaining = self.bridge.close(5.0)
        returned = time.monotonic()
        self.assertEqual((remaining, self.bridge.owned_runs()), (0, 0))
        self.assertLess(returned - started, 5.0)
        for thread, results in parked:
            thread.join(5)
            answered_at, (status, value) = results[0]
            self.assertEqual((status, value["code"], value["ended"]), (503, "turned_off", "turned_off"))
            self.assertLessEqual(answered_at, returned + 0.05, "answered before close() returned")
        self.assertEqual(len(self.api.stops), chat_bridge.MAX_ACTIVE)
        self.assertTrue(all(at <= returned for _run, at in self.api.stops), "every stop was sent under the fence")
        kinds = {session.turn.outcome.kind for session in records if session.turn is not None and
                 session.turn.outcome is not None}
        self.assertTrue(eventually(lambda: all(s.turn.outcome is not None for s in records)))
        kinds = {session.turn.outcome.kind for session in records}
        self.assertEqual(kinds, {"turned_off"})
        self.assertEqual((len(self.web.sessions), self.web.launches.held_codes(), len(self.web.channels)), (0, 0, 0))
        self.assertTrue(self.web._finished)
        self.assertEqual(self.web._in_flight, 0)

    def test_codes_and_channels_are_dropped_and_the_listener_is_gone(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        _status, value, _mac = launcher.launch()
        self.assertEqual(self.bridge.close(2.0), 0)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        self.assertIsNone(self.web.launches.find_code(code_of(value["url"])))

    def test_a_handler_still_running_at_close_never_writes_afterwards(self):
        page = self.page()
        gate = threading.Event()
        self.bridge.api_key = lambda: (gate.wait(10), API_KEY)[1]
        request = (f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nSec-Fetch-Site: same-origin\r\n"
                   f"X-Sidecar-Session: {page.session}\r\n\r\n").encode()
        client = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(request)
        self.assertTrue(eventually(lambda: self.web._in_flight == 1))
        started = time.monotonic()
        self.bridge.close(0.5)
        self.assertLess(time.monotonic() - started, 2.0)
        gate.set()
        received = b""
        try:
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                received += chunk
        except OSError:
            pass
        self.assertEqual(received, b"", "nothing was written after close() returned")
        self.assertTrue(eventually(lambda: self.web._in_flight == 0))

    def test_a_request_reaching_the_routes_after_close_is_cut_without_an_answer(self):
        self.bridge.close(1.0)
        connection = mock.Mock()
        request = types.SimpleNamespace(path="/api/state", connection=connection, close_connection=False,
                                        respond=mock.Mock())
        self.assertTrue(self.web.handle(request))
        connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        request.respond.assert_not_called()
        self.assertTrue(request.close_connection)

    def test_the_housekeeping_thread_ends_with_close(self):
        self.page()
        janitor = self.web._janitor
        self.assertIsNotNone(janitor)
        self.bridge.close(1.0)
        janitor.join(5)
        self.assertFalse(janitor.is_alive())


class PluginUnloadTests(unittest.TestCase):
    """Through the plugin's own registration and unload, as Hermes runs it."""

    def setUp(self):
        from test_chat_lifecycle import SecretStore
        self.api = FakeRunsApi()
        self.addCleanup(self.api.close)
        store = SecretStore({"LIBREOFFICE_LIVE_BRIDGE_TOKEN": PAIRING_TOKEN, "API_SERVER_KEY": API_KEY})
        for patch in (store.patch, mock.patch.object(plugin, "_in_gateway_process", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def register(self, config):
        from test_chat_lifecycle import LedgerContext
        events = []
        ctx = LedgerContext(config, events)
        plugin.register(ctx)
        self.addCleanup(ctx.unload)
        return ctx, events

    def test_chat_port_zero_starts_no_listener_no_routes_and_no_thread(self):
        before = {thread.name for thread in threading.enumerate()}
        for config in ({}, {"chat_port": 0}):
            ctx, _events = self.register(dict(config, api_server_port=self.api.port))
            self.assertIsNone(plugin._CHAT)
            ctx.unload()
        after = {thread.name for thread in threading.enumerate()}
        self.assertFalse({name for name in after - before if name.startswith("HermesLibreOffice")})

    def test_unload_answers_a_parked_long_poll_and_stops_the_turn_before_the_fence_goes(self):
        from test_chat_lifecycle import free_port
        port = free_port()
        ctx, events = self.register({"chat_port": port, "api_server_port": self.api.port})
        bridge = plugin._CHAT
        self.assertIsInstance(bridge.web, web_api.WebApi)
        folder = make_fixture()
        self.addCleanup(__import__("shutil").rmtree, folder)
        bridge.web.static = web_api.StaticFiles(folder)
        launcher = LauncherClient(port)
        launcher.hello()
        _status, value, _mac = launcher.launch()
        page = PageClient(port)
        self.assertEqual(page.exchange(code_of(value["url"]))[0], 200)
        status, chat = page.chat()
        self.assertEqual(status, 202)
        turn = chat["turn"]["id"]
        self.assertTrue(eventually(lambda: bridge.web.sessions.all()[0].turn.run.hermes_state == "running"),
                        "the bridge noted the run's first status poll")
        seq = page.turn(turn, 0)[1]["turn"]["seq"]
        bridge.web.long_poll_seconds = 20.0
        thread, results = in_background(page.turn, turn, seq, 0, 30.0)
        self.assertTrue(eventually(lambda: bridge.web._parked == 1))
        original = bridge.close

        def recorded(*args):
            remaining = original(*args)
            events.append(("close_returned", remaining))
            return remaining
        bridge.close = recorded
        ctx.unload()
        thread.join(5)
        _at, (status, value) = results[0]
        self.assertEqual((status, value["code"], value["ended"]), (503, "turned_off", "turned_off"))
        kinds = [kind for kind, _name in events]
        self.assertIn(("close_returned", 0), events)
        self.assertLess(kinds.index("close_returned"), kinds.index("hook_removed"))
        self.assertEqual(len(self.api.stops), 1)
        self.assertEqual(bridge.owned_runs(), 0)


if __name__ == "__main__":
    unittest.main()
