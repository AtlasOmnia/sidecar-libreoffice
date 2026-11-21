# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""N1: unload and immediately register on the same port, inside the event loop."""
import asyncio
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
import plugin
from plugin import chat_bridge
from test_chat_bridge import API_KEY, TOKEN
from test_chat_lifecycle import LedgerContext, SecretStore, free_port


class ReloadOnEventLoopTests(unittest.TestCase):
    ROUNDS = 200

    def setUp(self):
        store = SecretStore({"LIBREOFFICE_LIVE_BRIDGE_TOKEN": TOKEN, "API_SERVER_KEY": API_KEY})
        store.patch.start()
        self.addCleanup(store.patch.stop)
        gateway = mock.patch.object(plugin, "_in_gateway_process", return_value=True)
        gateway.start()
        self.addCleanup(gateway.stop)

    def test_same_port_starts_after_event_loop_unload_200_rounds(self):
        not_started, elapsed = 0, []
        for _ in range(self.ROUNDS):
            port = free_port()
            first = LedgerContext({"chat_port": port, "api_server_port": 9}, [])
            second = LedgerContext({"chat_port": port, "api_server_port": 9}, [])

            async def reload():
                began = time.monotonic()
                first.unload()
                elapsed.append(time.monotonic() - began)
                plugin.register(second)   # no await, sleep or off-loop cleanup before the bind
                return plugin._CHAT is not None

            try:
                plugin.register(first)
                self.assertIsNotNone(plugin._CHAT, "the first endpoint started")
                not_started += not asyncio.run(reload())
            finally:
                first.unload()
                second.unload()
        print(f"event-loop reload: {not_started}/{self.ROUNDS} not started; "
              f"maximum unload {max(elapsed):.6f} s")
        self.assertEqual(not_started, 0)
        self.assertLessEqual(max(elapsed), 0.06, "event-loop close waits at most 0.05 s, plus overhead")

    def test_listener_wait_is_capped_by_remaining_budget_and_50ms(self):
        # Record the actual allowance independently of OS timer scheduling. Holding a fake
        # listener also exercises the cap when an accept thread cannot finish promptly.
        for budget in (0.0, 0.02, 1.0):
            with self.subTest(budget=budget):
                bridge = chat_bridge.ChatBridge(port=0, token=lambda: TOKEN, api_key=lambda: API_KEY)
                listener = mock.Mock()
                bridge._server = listener
                allowances = []
                listener.stop_serving.side_effect = allowances.append

                async def close():
                    began = time.monotonic()
                    bridge.close(budget)
                    return time.monotonic() - began

                self.assertLessEqual(asyncio.run(close()), 0.06)
                self.assertEqual(len(allowances), 1)
                self.assertGreaterEqual(allowances[0], 0.0)
                self.assertLessEqual(allowances[0], min(0.05, budget))
                if budget:
                    self.assertGreater(allowances[0], min(0.05, budget) - 0.01)


if __name__ == "__main__":
    unittest.main()
