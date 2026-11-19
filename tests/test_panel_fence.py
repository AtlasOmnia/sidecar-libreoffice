# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Which turns the panel tool fence restricts.

The fence must hold for the whole panel conversation, not only while Hermes still calls it by
the panel's own id: compression gives the running agent a new physical ``session_id``. It keys
on every identity Hermes hands a ``pre_tool_call`` hook or binds for the turn, and on nothing
else, so chats that did not start in the panel are never restricted.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from plugin import _panel_tool_fence

PANEL = "lo-" + "b" * 32
ROTATED = "20261119_120000_a1b2c3"   # the shape Hermes mints for a compression continuation


def session_context(**values):
    """A stand-in for Hermes's documented ``gateway.session_context`` module."""
    module = types.ModuleType("gateway.session_context")
    module.get_session_env = lambda name, default="": values.get(name, default)
    package = types.ModuleType("gateway")
    package.session_context = module
    return mock.patch.dict(sys.modules, {"gateway": package, "gateway.session_context": module})


def fence(tool, *, session_id=ROTATED, **ids):
    return _panel_tool_fence(tool_name=tool, args={}, session_id=session_id, task_id="t", turn_id="u", **ids)


class CompressionRotationTests(unittest.TestCase):
    def test_a_rotated_session_keeps_the_fence_through_its_conversation_id(self):
        for tool in ("terminal", "write_file", "delegate_task", "execute_code"):
            self.assertEqual(fence(tool, conversation_id=PANEL)["action"], "block", tool)
        self.assertIsNone(fence("calc_get_range", conversation_id=PANEL))

    def test_a_turn_hermes_binds_to_the_panel_session_keeps_the_fence(self):
        """The canonical session owner binds the conversation's root id as the turn's chat id and
        as the last part of its route key; either is enough, even with no conversation_id."""
        for values in ({"HERMES_SESSION_CHAT_ID": PANEL},
                       {"HERMES_SESSION_KEY": f"agent:main:api_server:dm:{PANEL}"},
                       {"HERMES_SESSION_KEY": PANEL}):
            with session_context(**values):
                self.assertEqual(fence("terminal")["action"], "block", values)
                self.assertIsNone(fence("writer_get_text"), values)

    def test_generic_dispatch_inside_a_rotated_panel_session_is_checked_call_by_call(self):
        bad = {"calls": [{"name": "calc_get_range"}, {"name": "terminal"}]}
        result = _panel_tool_fence(tool_name="tool_call", args=bad, session_id=ROTATED, conversation_id=PANEL)
        self.assertEqual(result["action"], "block")


class UnrelatedChatTests(unittest.TestCase):
    def test_chats_that_did_not_start_in_the_panel_are_never_restricted(self):
        with session_context(HERMES_SESSION_CHAT_ID="1234567", HERMES_SESSION_KEY="agent:main:telegram:dm:1234567"):
            self.assertIsNone(fence("terminal", conversation_id="20261119_110000_ffffff"))
        with session_context():
            self.assertIsNone(fence("terminal", conversation_id=""))
        for odd in ("lo-short", "lo-" + "g" * 32, None, 7):
            self.assertIsNone(fence("terminal", conversation_id=odd), odd)

    def test_a_broken_session_context_never_breaks_other_chats(self):
        module = types.ModuleType("gateway.session_context")

        def boom(*_args, **_kwargs):
            raise RuntimeError("no context")
        module.get_session_env = boom
        package = types.ModuleType("gateway")
        package.session_context = module
        with mock.patch.dict(sys.modules, {"gateway": package, "gateway.session_context": module}):
            self.assertIsNone(fence("terminal"))
            self.assertEqual(fence("terminal", conversation_id=PANEL)["action"], "block")


if __name__ == "__main__":
    unittest.main()
