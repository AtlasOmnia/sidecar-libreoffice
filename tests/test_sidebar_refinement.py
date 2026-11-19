# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Track A native layout, non-input hints and bounded health at UNO/HTTP boundaries."""
import threading
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from test_sidebar import SIDEBAR, Context, Parent, ROOT, panel


class RefinedLayoutTests(unittest.TestCase):
    def assert_no_fixed_text_paint_overlap(self, boxes, controls):
        """Aqua paints fixed-text siblings independently of XML/tab ordering.

        Include EVERY fixed-text background, not just named foreground labels.
        Button backings may cover their genuine buttons, never another fixed text
        or the input. This oracle intentionally rejects the old full-area plates.
        """
        ns = "{http://openoffice.org/2000/dialog}"
        fixed = [name for name in boxes if controls[name].tag == ns + "text"]
        composer_y = boxes["ComposerLabel"][1] - 8
        for i, name in enumerate(fixed):
            for other in fixed[i + 1:] + ["Input"]:
                x, y, w, h = boxes[name]
                a, b, c, d = boxes[other]
                if max(y, b) < composer_y or min(h, d) <= 0:
                    continue
                self.assertFalse(x < a + c and a < x + w and y < b + d and b < y + h,
                                 (name, other, boxes[name], boxes[other]))

    def test_runtime_composer_has_no_fixed_text_paint_overlap_in_any_allocation(self):
        ns = "{http://openoffice.org/2000/dialog}"
        controls = {node.attrib[ns + "id"]: node for node in
                    ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot().find(ns + "bulletinboard")}
        for width in (300, 380, 465, 520):
            for height in (420, 650, 692, 900):
                for notice in (False, True):
                    for status_height in (18, 40):
                        with self.subTest(width=width, height=height, notice=notice, status_height=status_height):
                            boxes = SIDEBAR.plan_layout(width, height, notice=notice, status_height=status_height)
                            self.assert_no_fixed_text_paint_overlap(boxes, controls)

    def test_packaged_composer_has_no_fixed_text_paint_overlap_before_layout(self):
        ns = "{http://openoffice.org/2000/dialog}"
        controls = {node.attrib[ns + "id"]: node for node in
                    ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot().find(ns + "bulletinboard")}
        boxes = {name: tuple(int(node.attrib[ns + key]) for key in ("left", "top", "width", "height"))
                 for name, node in controls.items()}
        self.assert_no_fixed_text_paint_overlap(boxes, controls)

    def test_composer_tiles_cover_one_frame_without_dead_background_controls(self):
        ns = "{http://openoffice.org/2000/dialog}"
        board = ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot().find(ns + "bulletinboard")
        assert board is not None
        controls = {node.attrib[ns + "id"]: node for node in board}
        self.assertNotIn("InputBackplate", controls)
        self.assertNotIn("ComposerSurface", controls)
        for width in (300, 380, 465, 520):
            boxes = SIDEBAR.plan_layout(width, 692)
            self.assertEqual(set(controls), set(boxes))
            names = [name for name in boxes if name.startswith("Composer") or
                     name in ("InputHint", "Input", "NewChatBackplate", "SendBackplate")]
            top, bottom = boxes["ComposerBorderTop"], boxes["ComposerBorderBottom"]
            self.assertEqual(sum(boxes[name][2] * boxes[name][3] for name in names),
                             top[2] * (bottom[1] + bottom[3] - top[1]))
            self.assert_no_fixed_text_paint_overlap(boxes, controls)

    def test_composer_border_fill_and_text_theme_roles_survive_resize(self):
        item = panel(lambda *args: "ok")
        for theme in ("light", "dark"):
            item._apply_theme(theme)
            item._input.setText("draft")
            item._input.setSelection((1, 3))
            item._layout(300, 692)
            for name, box in SIDEBAR.plan_layout(300, 692).items():
                if name.startswith("Composer") or name == "InputHint":
                    role = "border" if name.startswith("ComposerBorder") else "raised"
                    control = item.Window.getControl(name)
                    self.assertEqual(control.model.values["BackgroundColor"], SIDEBAR.PALETTES[theme][role])
                    self.assertEqual(control.rectangles[-1], box)
            self.assertEqual((item._input.text, item._input.selection), ("draft", (1, 3)))

    def test_header_is_compact_with_mark_and_neutral_title(self):
        item = panel(lambda *args: "ok", width=300)
        boxes = SIDEBAR.plan_layout(300, 692)
        self.assertLessEqual(boxes["Transcript"][1], 100)
        self.assertIn("BrandMark", boxes)
        self.assertIn(boxes["BrandMark"][2], range(24, 29))
        self.assertLessEqual(boxes["BrandMark"][0] + boxes["BrandMark"][2], boxes["Title"][0])
        self.assertLess(boxes["ThemeToggle"][2], 140)
        self.assertGreaterEqual(boxes["ThemeToggle"][3], 28)
        self.assertLess(boxes["Status"][1], boxes["Transcript"][1])
        self.assertLess(boxes["Connectivity"][1], boxes["Transcript"][1])
        values = item.Window.getControl("Title").model.values
        self.assertEqual(values["TextColor"], SIDEBAR.TEXT)
        self.assertEqual(values["FontDescriptor"].Height, 12)
        self.assertGreaterEqual(values["FontDescriptor"].Weight, 150)

    def test_unified_composer_contains_genuine_input_actions_label_and_nonoverlay_hint(self):
        boxes = SIDEBAR.plan_layout(300, 692)
        top, bottom = boxes["ComposerBorderTop"], boxes["ComposerBorderBottom"]
        outer = (top[0], top[1], top[2], bottom[1] + bottom[3] - top[1])
        self.assertNotIn("InputBackplate", boxes)
        self.assertNotIn("ComposerSurface", boxes)
        self.assertEqual(boxes["Input"][3], 84)
        for name in ("ComposerLabel", "InputHint", "Input", "Send", "NewChat", "ComposerHelp"):
            self.assertIn(name, boxes)
            x, y, w, h = boxes[name]
            self.assertGreater(x, outer[0], name)
            self.assertGreater(y, outer[1], name)
            self.assertLess(x + w, outer[0] + outer[2], name)
            self.assertLess(y + h, outer[1] + outer[3], name)
        self.assertLessEqual(boxes["InputHint"][1] + boxes["InputHint"][3], boxes["Input"][1])
        self.assertLessEqual(boxes["Input"][1] + boxes["Input"][3], boxes["Send"][1])
        self.assertGreater(boxes["Transcript"][3], 350)

    def test_notice_space_and_error_space_are_conditional_without_state_loss(self):
        item = panel(lambda *args: "ok", width=300)
        item._input.setText("retained draft")
        item._input.setSelection((1, 4))
        notice = item.Window.getControl("ThemeNotice")
        self.assertFalse(notice.visible)
        initial = item._transcript.rectangles[-1]
        item._apply_theme("light", "Theme not saved; switch again to retry.")
        self.assertTrue(notice.visible)
        self.assertGreater(item._transcript.rectangles[-1][1], initial[1])
        item._set_status("Reply failed — check conversation and document.", "error")
        self.assertGreater(item._status.rectangles[-1][3], 18)
        item._apply_theme("dark")
        self.assertFalse(notice.visible)
        item._set_status(SIDEBAR.READY_STATUS)
        self.assertEqual(item._transcript.rectangles[-1], initial)
        self.assertEqual((item._input.text, item._input.selection), ("retained draft", (1, 4)))

    def test_all_visible_nonbackground_rectangles_are_disjoint_narrow_wide_short_and_notice(self):
        self.assertIn("InputHint", SIDEBAR.plan_layout(300, 692))
        for width in (300, 380, 465, 520):
            for height in (420, 650, 692, 900):
                for notice in (False, True):
                    for error in (False, True):
                        boxes = SIDEBAR.plan_layout(width, height, notice=notice, status_height=40 if error else 18)
                        names = ("BrandMark", "Title", "Subtitle", "ThemeToggle", "Connectivity", "Status",
                                 "Transcript", "ComposerLabel", "InputHint", "Input", "NewChat", "Send", "ComposerHelp")
                        if notice:
                            names += ("ThemeNotice",)
                        for i, name in enumerate(names):
                            x, y, w, h = boxes[name]
                            self.assertTrue(x >= 0 and y >= 0 and w > 0 and h > 0, (width, height, name))
                            self.assertLessEqual(x + w, width)
                            self.assertLessEqual(y + h, height)
                            for other in names[i + 1:]:
                                a, b, c, d = boxes[other]
                                self.assertFalse(x < a + c and a < x + w and y < b + d and b < y + h,
                                                 (width, height, notice, error, name, other))

    def test_packaged_mark_is_native_and_uses_the_sidecar_asset(self):
        ns = "{http://openoffice.org/2000/dialog}"
        root = ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot()
        board = root.find(ns + "bulletinboard")
        assert board is not None
        controls = {node.attrib[ns + "id"]: node for node in board}
        self.assertIn("BrandMark", controls)
        self.assertEqual(controls["BrandMark"].tag, ns + "img")
        self.assertTrue(controls["BrandMark"].attrib[ns + "src"].endswith("/icons/sidecar-deck.png"))
        self.assertEqual(controls["InputHint"].tag, ns + "text")
        self.assertNotIn(ns + "value", controls["Input"].attrib)
        self.assertIn("Your message", controls["ComposerLabel"].attrib[ns + "value"])


class HintAndScrollbarTests(unittest.TestCase):
    def test_hint_background_tile_remains_painted_for_empty_and_typed_drafts(self):
        for theme in ("dark", "light"):
            item = panel(lambda *args: "ok")
            item._apply_theme(theme)
            hint = item.Window.getControl("InputHint")
            for draft in ("", "typed draft", "   ", ""):
                with self.subTest(theme=theme, draft=draft):
                    item._input.setText(draft)
                    self.assertTrue(hint.visible)
                    self.assertEqual(hint.text, SIDEBAR.INPUT_HINT if draft == "" else "")
                    self.assertEqual(hint.model.values["BackgroundColor"], SIDEBAR.PALETTES[theme]["raised"])
                    self.assertEqual(item._input.text, draft)

    def test_hint_uses_native_listener_and_never_becomes_input_or_payload(self):
        calls = []
        item = panel(lambda message, *args: calls.append(message) or "ok")
        self.assertIn("InputHint", item.Window.controls)
        hint = item.Window.getControl("InputHint")
        self.assertIn(item, item._input.text_listeners)
        self.assertTrue(hint.visible)
        self.assertEqual(item._input.text, "")
        item.actionPerformed(None)
        self.assertEqual(calls, [])
        item._input.setText("real draft 日本語")
        self.assertTrue(hint.visible)
        self.assertEqual(hint.text, "")
        item._input.setText("")
        self.assertTrue(hint.visible)
        item._input.setText("   ")
        self.assertTrue(hint.visible)
        self.assertEqual(hint.text, "")  # empty, not merely whitespace
        item.submit("real message")
        self.assertTrue(hint.visible)
        self.assertTrue(item._async.posted.wait(5))
        item._input.setText("busy typed draft")
        item._async.run()
        self.assertTrue(hint.visible)
        self.assertEqual(hint.text, "")
        self.assertEqual(calls, ["real message"])
        item.disposing(None)
        with patch.object(hint, "setVisible", side_effect=AssertionError("disposed hint")):
            item.textChanged(None)

    def test_auto_vscroll_is_requested_only_when_supported(self):
        item = panel(lambda *args: "ok")
        for control in (item._input, item._transcript):
            self.assertTrue(control.model.values["AutoVScroll"])
            self.assertFalse(control.model.values["VScroll"])
        self.assertEqual(item.snapshot()["scrollbars"], {"Input": "auto-requested", "Transcript": "auto-requested"})

    def test_missing_optional_scroll_property_keeps_honest_always_scrollbar(self):
        ctx = Context()
        del ctx.window.getControl("Input").model.values["AutoVScroll"]
        item = panel(lambda *args: "ok", ctx=ctx)
        self.assertIn("scrollbars", item.snapshot())
        self.assertEqual(item.snapshot()["scrollbars"]["Input"], "native-always")
        self.assertTrue(item._input.model.values["VScroll"])

    def test_rejected_optional_scroll_setter_logs_and_falls_back_without_blank_panel(self):
        ctx = Context()
        model = ctx.window.getControl("Input").model
        real = model.setPropertyValue
        def reject_auto(name, value):
            if name == "AutoVScroll" and value is True:
                raise ValueError("not supported by peer")
            real(name, value)
        with patch.object(model, "setPropertyValue", side_effect=reject_auto), patch.object(SIDEBAR, "_log") as log:
            item = panel(lambda *args: "ok", ctx=ctx)
        self.assertTrue(item.Window.visible)
        self.assertIn("scrollbars", item.snapshot())
        self.assertEqual(item.snapshot()["scrollbars"]["Input"], "native-always")
        self.assertTrue(model.values["VScroll"])
        self.assertTrue(log.called)


class HealthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(SIDEBAR, "probe_health", None)), "bounded health implementation is required")

    def run_probe(self, raw=b'{"ok":true,"status":"ok"}', status=200, error=None):
        response = types.SimpleNamespace(status=status, read=lambda limit: raw[:limit])
        from unittest.mock import Mock
        connection = Mock()
        connection.getresponse.return_value = response
        if error:
            connection.request.side_effect = error
        with patch.object(SIDEBAR.http.client, "HTTPConnection", return_value=connection) as factory:
            ready = SIDEBAR.probe_health(18661, "fixture-token")
        return ready, connection, factory

    def test_good_authenticated_get_has_short_timeout_bounded_read_and_closes(self):
        from unittest.mock import Mock
        response, connection = Mock(status=200), Mock()
        response.read.return_value = b'{"ok":true,"status":"ok"}'
        connection.getresponse.return_value = response
        with patch.object(SIDEBAR.http.client, "HTTPConnection", return_value=connection) as factory:
            self.assertTrue(SIDEBAR.probe_health(18661, "fixture-token"))
        factory.assert_called_once_with("127.0.0.1", 18661, timeout=2.0)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/health"))
        self.assertEqual(kwargs["headers"][SIDEBAR.TOKEN_HEADER], "fixture-token")
        self.assertEqual(kwargs["headers"]["Host"], "127.0.0.1:18661")
        self.assertNotIn("Origin", kwargs["headers"])
        response.read.assert_called_once_with(SIDEBAR.MAX_HEALTH_BYTES + 1)
        connection.close.assert_called_once()

    def test_bad_malformed_wrong_shape_invalid_utf8_and_oversize_are_not_ready(self):
        cases = [(b'{"ok":true,"status":"ok"}', 401), (b'{"ok":false,"status":"ok"}', 200),
                 (b'{"ok":1,"status":"ok"}', 200), (b'{"ok":true}', 200),
                 (b'{"ok":true,"status":"working"}', 200), (b'[]', 200), (b'null', 200),
                 (b'{', 200), (b'\xff', 200), (b' ' * (SIDEBAR.MAX_HEALTH_BYTES + 1), 200)]
        for raw, status in cases:
            with self.subTest(raw=raw[:40], status=status):
                ready, connection, _ = self.run_probe(raw, status)
                self.assertFalse(ready)
                connection.close.assert_called_once()

    def test_connection_and_http_exceptions_are_unavailable_and_close(self):
        for exc in (ConnectionRefusedError(), TimeoutError(), SIDEBAR.http.client.HTTPException("bad")):
            ready, connection, _ = self.run_probe(error=exc)
            self.assertFalse(ready)
            connection.close.assert_called_once()


class HealthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(SIDEBAR.ChatPanel, "_start_health_probe", None)), "health lifecycle is required")
        self.ctx = Context()
        self.release, self.entered = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        self.calls = []
        self.token_patch = patch.object(SIDEBAR, "read_pairing_token", return_value="x" * 32)
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def create(self, result: object = True, responder=None):
        def probe(*args):
            self.calls.append((args, threading.current_thread().name))
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("test did not release health worker")
            if isinstance(result, Exception):
                raise result
            return result
        self.health_patch = patch.object(SIDEBAR, "probe_health", side_effect=probe)
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        with patch.object(SIDEBAR.panel_registry, "register"):
            item = SIDEBAR.ChatPanel(self.ctx, None, Parent(300, 692), "private:hermes", responder or (lambda *args: "ok"))
        self.assertTrue(self.entered.wait(5))
        self.assertEqual(item.Window.getControl("Connectivity").text, "Not checked")
        return item

    def finish_health(self, item):
        self.release.set()
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()

    def state(self, item):
        return (item._input.text, list(item._lines), item._busy, item._send.enabled, item._session_id,
                item._generation, item._status.text, item._input.selection)

    def test_once_on_open_off_main_thread_and_never_chat_or_timer(self):
        with patch.object(SIDEBAR, "ask_hermes", side_effect=AssertionError("health sent chat")):
            item = self.create()
            self.finish_health(item)
            self.assertEqual(item.Window.getControl("Connectivity").text, "Local bridge ready")
            item.Window.getControl("ThemeToggle").actions[0].actionPerformed(None)
            item._apply_theme("light")
            item.new_chat()
            item.windowResized(None)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "SidecarSidebarHealth")
        for control in item.Window.controls.values():
            self.assertNotIn("SidecarSidebarHealth", control.writes)

    def test_unavailable_and_unexpected_exception_are_not_model_readiness(self):
        for result in (False, RuntimeError("never expose private details")):
            self.calls.clear()
            self.entered.clear()
            item = self.create(result)
            self.finish_health(item)
            self.assertEqual(item.Window.getControl("Connectivity").text, "Bridge unavailable")
            self.assertEqual(item._status.text, SIDEBAR.READY_STATUS)
            self.health_patch.stop()

    def test_missing_pairing_token_prevents_network(self):
        self.token_patch.stop()
        with patch.object(SIDEBAR, "probe_health", side_effect=AssertionError("token missing")) as probe:
            item = panel(lambda *args: "ok", ctx=self.ctx, check_health=False)
            self.assertTrue(item._async.posted.wait(5))
            item._async.run()
        probe.assert_not_called()
        self.assertEqual(item.Window.getControl("Connectivity").text, "Bridge unavailable")

    def test_delayed_health_does_not_mask_busy_draft_theme_history_or_identity(self):
        chat_release = threading.Event()
        self.addCleanup(chat_release.set)
        item = self.create(responder=lambda *args: chat_release.wait(5) and "reply")
        item.submit("question")
        item._input.setText("draft while busy")
        item._input.setSelection((1, 3))
        item._apply_theme("light")
        before = self.state(item)
        self.finish_health(item)
        self.assertEqual(self.state(item), before)
        self.assertEqual(item.Window.getControl("Connectivity").text, "Local bridge ready")
        chat_release.set()
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()

    def test_delayed_health_cannot_erase_error_and_after_failure_probes_once(self):
        item = self.create()
        item._set_status("Uncertain outcome: check document.", "error")
        item._input.setText("draft")
        before = self.state(item)
        self.finish_health(item)
        self.assertEqual(self.state(item), before)
        def failure(*args):
            raise SIDEBAR.ChatUnavailable("check document")
        item._responder = failure
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        # Health completion is independently queued after the failed chat callback.
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertIn("Reply failed", item._status.text)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(item.Window.getControl("Connectivity").text, "Local bridge ready")

    def test_new_chat_and_disposal_fence_late_health(self):
        item = self.create()
        item._input.setText("draft")
        item.new_chat()
        before = self.state(item)
        self.finish_health(item)
        self.assertEqual(self.state(item), before)
        self.assertEqual(item.Window.getControl("Connectivity").text, "Not checked")
        self.release.clear()
        item._start_health_probe()
        item.disposing(None)
        self.release.set()
        self.assertTrue(item._async.posted.wait(5))
        with patch.object(item.Window.getControl("Connectivity"), "setText", side_effect=AssertionError("disposed")):
            item._async.run()
            item._start_health_probe()
        self.assertEqual(len(self.calls), 2)

    def test_successful_chat_recovers_unavailable_without_an_extra_probe(self):
        item = self.create(False)
        self.finish_health(item)
        self.assertEqual(item.snapshot()["connectivity"], "Bridge unavailable")
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item._input.setText("retained busy draft")
        item._input.setSelection((1, 3))
        item._async.run()
        self.assertEqual(item.snapshot()["connectivity"], "Local bridge ready")
        self.assertEqual(item.Window.getControl("Connectivity").text, "Local bridge ready")
        self.assertEqual((item._input.text, item._input.selection), ("retained busy draft", (1, 3)))
        self.assertEqual(len(self.calls), 1)

    def test_success_fences_older_unavailable_health_and_cancels_deferred_probe(self):
        item = self.create(False)
        item._start_health_probe(after_failure=True)
        self.assertTrue(item._health_pending)
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item.snapshot()["connectivity"], "Local bridge ready")
        self.assertTrue(item._health_inflight, "success must not release a still-running health worker")
        self.assertFalse(item._health_pending)
        item._input.setText("retained draft")
        item._set_status("Message is too long.", "error")
        before = self.state(item)
        self.finish_health(item)
        self.assertEqual(item.snapshot()["connectivity"], "Local bridge ready")
        self.assertEqual(self.state(item), before)
        self.assertFalse(item._health_inflight)
        self.assertEqual(len(self.calls), 1)
        # A genuinely newer failure must still be able to request a fresh check.
        item._start_health_probe(after_failure=True)
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item.snapshot()["connectivity"], "Bridge unavailable")
        self.assertEqual(self.state(item), before)
        self.assertEqual(len(self.calls), 2)

    def test_late_success_after_new_chat_or_disposal_does_not_update_connectivity(self):
        for dispose in (False, True):
            with self.subTest(dispose=dispose):
                self.calls.clear()
                item = self.create(False)
                self.finish_health(item)
                release_chat = threading.Event()
                self.addCleanup(release_chat.set)
                item._responder = lambda *args: release_chat.wait(5) and "old reply"
                item.submit("old question")
                if dispose:
                    item.disposing(None)
                else:
                    item.new_chat()
                release_chat.set()
                self.assertTrue(item._async.posted.wait(5))
                item._async.run()
                self.assertEqual(item.snapshot()["connectivity"], "Bridge unavailable")
                self.assertEqual(item.Window.getControl("Connectivity").text, "Bridge unavailable")
                self.assertEqual(len(self.calls), 1)
                self.health_patch.stop()

    def test_inflight_probe_is_single_flight_and_old_generation_cannot_win(self):
        item = self.create()
        item._start_health_probe()
        self.assertEqual(len(self.calls), 1)
        item._health_generation += 1
        self.finish_health(item)
        self.assertEqual(item.Window.getControl("Connectivity").text, "Not checked")

    def test_failure_during_initial_probe_defers_one_fresh_check_without_parallel_workers(self):
        def failure(*args):
            raise SIDEBAR.ChatUnavailable("check document")
        item = self.create(responder=failure)
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertIn("Reply failed", item._status.text)
        self.assertEqual(len(self.calls), 1)
        self.finish_health(item)
        self.assertTrue(item._async.posted.wait(5), "failure needs one fresh check after the in-flight probe")
        item._async.run()
        self.assertEqual(len(self.calls), 2)
        self.assertIn("Reply failed", item._status.text)
        self.assertEqual(item.Window.getControl("Connectivity").text, "Local bridge ready")

    def test_new_chat_and_disposal_cancel_deferred_health_work(self):
        for dispose in (False, True):
            self.release.clear()
            self.entered.clear()
            self.calls.clear()
            item = self.create()
            item._start_health_probe(after_failure=True)
            self.assertTrue(item._health_pending)
            if dispose:
                item.disposing(None)
            else:
                item.new_chat()
            self.assertFalse(item._health_pending)
            self.finish_health(item)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(item.Window.getControl("Connectivity").text, "Not checked")
            self.health_patch.stop()


if __name__ == "__main__":
    unittest.main()
