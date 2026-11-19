# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The sidebar chat panel: layout arithmetic, the send/reply cycle and its packaging."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import weakref
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_sidebar():
    uno = types.ModuleType("uno")
    uno.createUnoStruct = lambda _name: types.SimpleNamespace()
    uno.fileUrlToSystemPath = lambda value: value
    unohelper = types.ModuleType("unohelper")
    unohelper.Base = type("UnoBase", (), {})
    unohelper.ImplementationHelper = lambda: types.SimpleNamespace(addImplementation=lambda *args: None)
    awt = types.ModuleType("com.sun.star.awt")
    ui = types.ModuleType("com.sun.star.ui")
    for module, names in ((awt, ("XActionListener", "XCallback", "XKeyListener", "XWindowListener", "XTextListener")),
                          (ui, ("XSidebarPanel", "XToolPanel", "XUIElement", "XUIElementFactory"))):
        for name in names:
            setattr(module, name, type(name, (), {}))
    modules = {"uno": uno, "unohelper": unohelper, "com": types.ModuleType("com"), "com.sun": types.ModuleType("com.sun"),
               "com.sun.star": types.ModuleType("com.sun.star"), "com.sun.star.awt": awt, "com.sun.star.ui": ui}
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        spec = importlib.util.spec_from_file_location("tested_hermes_sidebar", ROOT / "extension" / "python" / "hermes_sidebar.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


SIDEBAR = load_sidebar()


class Model:
    """UNO property boundary; reject misspelled/new properties rather than magic mocks."""
    def __init__(self):
        self.values = {"BackgroundColor": None, "TextColor": None, "HelpText": "", "Label": "",
                       "FontDescriptor": types.SimpleNamespace(Name="Native UI", Height=0, Weight=100)}

    def getPropertyValue(self, name):
        return self.values[name]

    def getPropertySetInfo(self):
        return types.SimpleNamespace(hasPropertyByName=lambda name: name in self.values)

    def setPropertyValue(self, name, value):
        if name not in self.values:
            raise ValueError("Unsupported UNO property: " + name)
        self.values[name] = value


class Control:
    def __init__(self, text=""):
        self.text, self.enabled, self.selection = text, True, None
        self.model = Model()
        self.actions, self.keys, self.rectangles, self.writes = [], [], [], []
        self.text_listeners, self.visible = [], True

    def getModel(self):
        return self.model

    def getText(self):
        return self.text

    def setText(self, value):
        self.writes.append(threading.current_thread().name)
        self.text = value
        for listener in self.text_listeners:
            listener.textChanged(types.SimpleNamespace(Source=self))

    def setVisible(self, value):
        self.visible = value
        self.writes.append(threading.current_thread().name)

    def addTextListener(self, listener):
        self.text_listeners.append(listener)

    def setEnable(self, value):
        self.enabled = value

    def setSelection(self, value):
        self.selection = value

    def addActionListener(self, listener):
        self.actions.append(listener)

    def addKeyListener(self, listener):
        self.keys.append(listener)

    def setPosSize(self, x, y, width, height, flags):
        self.rectangles.append((x, y, width, height))


class Window(Control):
    def __init__(self):
        super().__init__()
        ns = "{http://openoffice.org/2000/dialog}"
        root = ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot()
        self.controls = {node.attrib[ns + "id"]: Control(node.attrib.get(ns + "value", ""))
                         for node in root.find(ns + "bulletinboard")}
        for name in ("Input", "Transcript"):
            self.controls[name].model.values.update(AutoVScroll=False, VScroll=True)
        self.visible = False

    def getControl(self, name):
        return self.controls[name]

    def setVisible(self, value):
        self.visible = value


class Parent:
    def __init__(self, width=380, height=652):
        self.area = types.SimpleNamespace(Width=width, Height=height)
        self.listeners = []

    def getPosSize(self):
        return self.area

    def addWindowListener(self, listener):
        self.listeners.append(listener)


class Context:
    def __init__(self, window=None):
        self.window, self.async_queue = window or Window(), QueuedAsync()
        self.temporary = tempfile.TemporaryDirectory()
        weakref.finalize(self, self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve() / "hermes-bridge"
        self.container_calls = []

    def getServiceManager(self):
        return self

    def createInstanceWithContext(self, name, ctx):
        assert ctx is self
        if name == "com.sun.star.awt.AsyncCallback":
            return self.async_queue
        if name == "com.sun.star.awt.ContainerWindowProvider":
            return self
        if name == "com.sun.star.util.PathSubstitution":
            return self
        raise AssertionError("Unexpected service: " + name)

    def substituteVariables(self, value, required):
        assert value == "$(user)/hermes-bridge" and required is True
        return str(self.folder)

    def createContainerWindow(self, *args):
        self.container_calls.append(args)
        return self.window



class QueuedAsync:
    """Holds main-thread callbacks until the test runs them, like LibreOffice's event loop."""

    def __init__(self):
        self.queued, self.posted = [], threading.Event()

    def addCallback(self, callback, _data):
        self.queued.append(callback)
        self.posted.set()

    def run(self):
        callbacks, self.queued = self.queued, []
        self.posted.clear()
        for callback in callbacks:
            callback.notify(None)


def panel(responder, width=380, height=652, frame=None, ctx=None, check_health=True, register=False):
    ctx = ctx or Context()
    from contextlib import ExitStack
    with ExitStack() as stack:
        if not register:
            stack.enter_context(patch.object(SIDEBAR.panel_registry, "register"))
        if check_health:
            # Fake only token/HTTP boundaries; exercise the real health worker/callback.
            stack.enter_context(patch.object(SIDEBAR, "read_pairing_token", return_value="x" * 32))
            stack.enter_context(patch.object(SIDEBAR, "probe_health", return_value=True))
        item = SIDEBAR.ChatPanel(ctx, frame, Parent(width, height), "private:hermes", responder)
        if check_health:
            if not ctx.async_queue.posted.wait(5):
                raise AssertionError("initial health worker did not finish")
            ctx.async_queue.run()
        return item


class LayoutTests(unittest.TestCase):
    def test_controls_stay_inside_the_panel_and_do_not_overlap(self):
        for width in (300, 380, 520):
            for height in (420, 480, 652, 900):
                with self.subTest(width=width, height=height):
                    item = panel(lambda *args: "ok", width, height)
                    boxes = SIDEBAR.plan_layout(width, height)
                    for name, (x, y, w, h) in boxes.items():
                        if name == "ThemeNotice":
                            self.assertFalse(item.Window.getControl(name).visible)
                            continue
                        self.assertTrue(x >= 0 and y >= 0 and w > 0 and h > 0, name)
                        self.assertLessEqual(x + w, width, name)
                        self.assertLessEqual(y + h, height, name)
                        self.assertEqual(item.Window.getControl(name).rectangles[-1], (x, y, w, h))
                    order = [boxes[name] for name in (
                        "Title", "Subtitle", "Status", "TranscriptBackplate", "ComposerBorderTop")]
                    for upper, lower in zip(order, order[1:]):
                        self.assertLessEqual(upper[1] + upper[3], lower[1])
                    self.assertLessEqual(boxes["NewChat"][0] + boxes["NewChat"][2], boxes["Send"][0])
                    self.assertGreaterEqual(boxes["Send"][3], 28)
                    for name in ("Transcript",):
                        outer, inner = boxes[name + "Backplate"], boxes[name]
                        self.assertEqual(inner, (outer[0] + 2, outer[1] + 2, outer[2] - 4, outer[3] - 4))

    def test_declared_minimum_matches_layout_and_resize_keeps_text(self):
        item = panel(lambda *args: "ok", 300, 220)
        self.assertEqual(item.getMinimalWidth(), 300)
        size = item.getHeightForWidth(300)
        self.assertEqual(size.Minimum, SIDEBAR.MINIMUM_HEIGHT)
        self.assertEqual(size.Maximum, -1)
        self.assertGreaterEqual(size.Preferred, size.Minimum)
        self.assertEqual(SIDEBAR.plan_layout(300, 220), SIDEBAR.plan_layout(300, size.Minimum))
        item._input.setText("draft 日本語")
        for width in (300, 380, 520):
            item._parent.area.Width, item._parent.area.Height = width, size.Minimum
            item.windowResized(None)
            self.assertEqual(item._input.text, "draft 日本語")
            self.assertEqual(item._transcript.text, SIDEBAR.EMPTY_TRANSCRIPT)


    def test_sidebar_measurement_shrinks_a_previously_wide_dialog(self):
        item = panel(lambda *args: "ok", 520, 652)
        item._input.setText("retained draft")
        item.getHeightForWidth(300)
        item._async.run()
        for name, rectangle in SIDEBAR.plan_layout(300, 652).items():
            if name == "ThemeNotice":
                continue
            self.assertEqual(item.Window.getControl(name).rectangles[-1], rectangle)
        item.windowResized(types.SimpleNamespace(Width=520, Height=652))
        self.assertEqual(item.Window.getControl("Send").rectangles[-1],
                         SIDEBAR.plan_layout(300, 652)["Send"])
        self.assertEqual(item._input.text, "retained draft")

    def test_resize_uses_event_allocation_when_parent_size_is_stale(self):
        item = panel(lambda *args: "ok", 520, 652)
        item._input.setText("retained draft")
        item.windowResized(types.SimpleNamespace(Width=300, Height=480))
        expected = SIDEBAR.plan_layout(300, 480)
        for name, rectangle in expected.items():
            if name == "ThemeNotice":
                continue
            self.assertEqual(item.Window.getControl(name).rectangles[-1], rectangle)
        self.assertEqual(item._input.text, "retained draft")


class ConstructorTests(unittest.TestCase):
    def test_app_specific_guidance_and_new_chat_match_bound_document(self):
        cases = (("calc", "Spreadsheet assistant", "cells"),
                 ("writer", "Writer assistant", "paragraph"),
                 ("impress", "Impress assistant", "slide"))
        for kind, subtitle, word in cases:
            with self.subTest(kind=kind):
                document = {"document_id": "fixture-" + kind, "type": kind}
                frame = types.SimpleNamespace(getController=lambda: types.SimpleNamespace(getModel=lambda: document))
                with patch.object(SIDEBAR.panel_registry, "identify", return_value=document, create=True):
                    item = panel(lambda *args: "ok", frame=frame)
                self.assertEqual(item.Window.getControl("Subtitle").text, "by Atlas Omnia")
                self.assertEqual(item.Window.getControl("Title").text, "Sidecar for " + kind.title())
                self.assertIn(word, item._transcript.text)
                if kind != "calc":
                    self.assertNotIn("cells", item._transcript.text)
                original = item._transcript.text
                item._append("You", "hello")
                item.new_chat()
                self.assertEqual(item._transcript.text, original)

    def test_unknown_document_guidance_is_neutral(self):
        for document in (None, {}, {"type": "unknown"}, {"type": []}):
            self.assertEqual(SIDEBAR.application_copy(document),
                             ("Document assistant", SIDEBAR.EMPTY_TRANSCRIPT))

    def test_real_constructor_wires_controls_and_applies_palette_font_and_help(self):
        ctx = Context()
        item = panel(lambda *args: "ok", ctx=ctx)
        self.assertEqual(ctx.container_calls, [(SIDEBAR.DIALOG_URL, "", item._parent, None)])
        self.assertTrue(item.Window.visible)
        self.assertEqual(item._parent.listeners, [item])
        self.assertEqual(item._send.actions, [item])
        self.assertEqual(item._input.keys, [item])
        self.assertEqual(item.Window.getModel().values["BackgroundColor"], SIDEBAR.GROUND)
        for name, background in (("Transcript", SIDEBAR.SURFACE), ("Input", SIDEBAR.RAISED)):
            values = item.Window.getControl(name).getModel().values
            self.assertEqual(values["BackgroundColor"], background)
            self.assertEqual(values["TextColor"], SIDEBAR.TEXT)
            self.assertEqual(values["FontDescriptor"].Height, 11)
            self.assertEqual(values["FontDescriptor"].Name, "Native UI")
            self.assertTrue(values["HelpText"])
        self.assertEqual(item.Window.getControl("Title").model.values["TextColor"], SIDEBAR.TEXT)
        self.assertEqual(item.Window.getControl("Title").model.values["FontDescriptor"].Height, 12)
        for name in ("TranscriptBackplate", "ComposerBorderTop"):
            self.assertEqual(item.Window.getControl(name).model.values["BackgroundColor"], SIDEBAR.BORDER_COLOR)
        for name, color in (("Send", SIDEBAR.PRIMARY), ("NewChat", SIDEBAR.TEXT)):
            button = item.Window.getControl(name)
            backplate = item.Window.getControl(name + "Backplate")
            self.assertEqual(button.model.values["TextColor"], SIDEBAR.GROUND)
            self.assertEqual(button.model.values["BackgroundColor"], color)
            self.assertEqual(backplate.model.values["BackgroundColor"], color)
            self.assertEqual(button.rectangles[-1], backplate.rectangles[-1])
        self.assertEqual(item._input.text, "")
        self.assertIn("Conversation", item._transcript.model.values["HelpText"])
        self.assertEqual(item.Window.getControl("ComposerLabel").text, "Your message")
        self.assertEqual(item.Window.getControl("ComposerHelp").text, SIDEBAR.COMPOSER_HELP)
        self.assertEqual(item._transcript.text, SIDEBAR.EMPTY_TRANSCRIPT)
        self.assertEqual(item.snapshot()["lines"], [])
        self.assertNotIn("Connected", item._status.text)
        self.assertEqual(item._status.text, SIDEBAR.READY_STATUS)

    def test_text_palette_pairs_meet_aa_contrast(self):
        def luminance(color):
            channels = [(color >> shift & 255) / 255 for shift in (16, 8, 0)]
            linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        for foreground, background in ((SIDEBAR.TEXT, SIDEBAR.SURFACE), (SIDEBAR.TEXT, SIDEBAR.RAISED),
                                       (SIDEBAR.MUTED, SIDEBAR.GROUND), (SIDEBAR.PRIMARY, SIDEBAR.GROUND),
                                       (SIDEBAR.ERROR, SIDEBAR.GROUND), (SIDEBAR.RUNNING, SIDEBAR.GROUND)):
            ratio = (luminance(foreground) + 0.05) / (luminance(background) + 0.05)
            self.assertGreaterEqual(ratio, 4.5, (foreground, background, ratio))

    def test_styling_property_failure_is_not_silently_ignored(self):
        ctx = Context()
        with patch.object(ctx.window.getControl("Input").model, "setPropertyValue", side_effect=ValueError("unsupported")):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                panel(lambda *args: "ok", ctx=ctx)

    def test_xdl_sets_styles_before_peer_creation_and_keeps_native_controls(self):
        ns = "{http://openoffice.org/2000/dialog}"
        root = ET.parse(ROOT / "extension/dialogs/SidecarChatPanel.xdl").getroot()
        styles = {node.attrib[ns + "style-id"]: node.attrib for node in root.find(ns + "styles")}
        controls = {node.attrib[ns + "id"]: node for node in root.find(ns + "bulletinboard")}
        for name, background in (("Transcript", SIDEBAR.SURFACE), ("Input", SIDEBAR.RAISED)):
            node = controls[name]
            self.assertEqual(node.tag, ns + "textfield")
            style = styles[node.attrib[ns + "style-id"]]
            self.assertEqual(int(style[ns + "background-color"], 16), background)
            self.assertEqual(style[ns + "border"], "none")
            self.assertEqual(node.attrib[ns + "multiline"], "true")
            self.assertEqual(node.attrib[ns + "vscroll"], "true")
        self.assertEqual(controls["Transcript"].attrib[ns + "readonly"], "true")
        self.assertNotIn(ns + "value", controls["Input"].attrib)
        # Aqua may omit button fills; real buttons stay over visible backplates.
        for name, color in (("Send", SIDEBAR.PRIMARY), ("NewChat", SIDEBAR.TEXT)):
            self.assertEqual(controls[name].tag, ns + "button")
            style = styles[controls[name].attrib[ns + "style-id"]]
            self.assertEqual(int(style[ns + "background-color"], 16), color)
            self.assertEqual(int(style[ns + "text-color"], 16), SIDEBAR.GROUND)
            self.assertEqual(controls[name + "Backplate"].tag, ns + "text")
            self.assertLess(list(controls).index(name + "Backplate"), list(controls).index(name))
        # Button/transcript-only backings retain ordering; composer strips are
        # geometrically disjoint (the paint-overlap regression checks that).
        order = list(controls)
        self.assertLess(order.index("ComposerBorderTop"), order.index("Input"))
        self.assertLess(order.index("TranscriptBackplate"), order.index("Transcript"))


class ChatCycleTests(unittest.TestCase):
    def test_send_shows_the_message_then_the_reply_arrives_on_the_main_thread(self):
        seen = []
        item = panel(lambda message, session, document: seen.append((message, session, document, threading.current_thread().name)) or "Done.")
        session = item._session_id
        item.submit("  Add a total row  ")
        self.assertEqual((item._lines, item._input.text, item._busy, item._send.enabled, item._status.text),
                         (["You: Add a total row"], "", True, False, "Hermes is working…"))
        self.assertTrue(item._async.posted.wait(5))
        self.assertEqual(item._lines, ["You: Add a total row"])   # nothing touches the window off the main thread
        item._async.run()
        self.assertEqual(item._lines, ["You: Add a total row", "Hermes: Done."])
        self.assertEqual((item._busy, item._send.enabled, item._status.text), (False, True, SIDEBAR.READY_STATUS))
        self.assertEqual(seen, [("Add a total row", session, None, "SidecarSidebarChat")])
        self.assertRegex(session, r"^lo-[0-9a-f]{32}$")
        self.assertEqual(item.snapshot()["lines"], item._lines)

    def test_empty_overlong_and_second_message_while_busy_are_not_sent(self):
        calls = []
        item = panel(lambda message, _session, _document: calls.append(message) or "ok")
        item.submit("   ")
        item.submit("x" * (SIDEBAR.MAX_MESSAGE + 1))
        self.assertEqual((item._lines, item._busy), ([], False))
        self.assertIn("too long", item._status.text)
        item.submit("first")
        item.submit("second")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(calls, ["first"])
        self.assertEqual(item._input.text, "second")   # kept for the user to send next

    def test_a_failing_responder_is_reported_and_the_panel_stays_usable(self):
        def boom(_message, _session, _document):
            raise OSError("refused")
        item = panel(boom)
        item.submit("hello")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item._lines[-1], "Request error: The panel failed while asking Hermes (OSError). "
                         "Check the document before sending again; the outcome may be unknown.")
        self.assertTrue(item._send.enabled)

        def unavailable(_message, _session, _document):
            raise SIDEBAR.ChatUnavailable("Hermes is not answering.")
        item = panel(unavailable)
        item.submit("hello")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item._lines[-1], "Request error: Hermes is not answering.")
        self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.ERROR)
        self.assertIn("check conversation", item._status.text)

    def test_new_chat_changes_the_session_clears_the_transcript_and_drops_a_late_reply(self):
        release = threading.Event()
        item = panel(lambda _message, _session, _document: release.wait(5) and "late reply")
        first = item._session_id
        item.submit("question")
        item.new_chat()
        self.assertNotEqual(item._session_id, first)
        self.assertEqual((item._lines, item._busy, item._send.enabled), ([], True, False))
        self.assertEqual(item._transcript.text, SIDEBAR.EMPTY_TRANSCRIPT)
        self.assertEqual(item._status.text, "Previous request still running…")
        item.submit("next draft")
        release.set()
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item._lines, [])
        self.assertEqual((item._busy, item._send.enabled), (False, True))
        self.assertEqual(item._input.text, "next draft")
        self.assertEqual(item._transcript.text, SIDEBAR.EMPTY_TRANSCRIPT)

    def test_history_unicode_draft_and_bound_document_survive_multiple_turns(self):
        document = {"document_id": "explicit-document"}
        frame = types.SimpleNamespace(getController=lambda: types.SimpleNamespace(getModel=lambda: document))
        seen = []
        with patch.object(SIDEBAR.panel_registry, "identify", return_value=document, create=True):
            item = panel(lambda message, session, binding: seen.append(binding) or "Réponse 日本語\nsecond line", frame=frame)
            for message in ("First — α", "Second"):
                item._input.setText(message)
                item._send.actions[0].actionPerformed(None)
                self.assertTrue(item._async.posted.wait(5))
                item._input.setText("draft to keep")
                item._async.run()
                item._async.posted.clear()
                self.assertEqual(item._input.text, "draft to keep")
        self.assertEqual(seen, [document, document])
        self.assertEqual(item._transcript.text, "\n\n".join(item._lines))
        self.assertIn("You: First — α", item._transcript.text)
        self.assertIn("You: Second", item._transcript.text)
        self.assertEqual(item._transcript.text.count("Réponse 日本語"), 2)
        self.assertEqual(item._transcript.selection.Max, len(item._transcript.text))
        for control in item.Window.controls.values():
            self.assertNotIn("SidecarSidebarChat", control.writes)

    def test_uncertain_error_is_visible_without_retry_or_draft_loss(self):
        calls = []
        def uncertain(message, session, document):
            calls.append(message)
            raise SIDEBAR.ChatUnavailable("Connection lost. Hermes may still have changed the document: check it before sending again.")
        item = panel(uncertain)
        item.submit("change something")
        self.assertTrue(item._async.posted.wait(5))
        item._input.setText("new unsent draft")
        item._async.run()
        self.assertEqual(calls, ["change something"])
        self.assertEqual(item._input.text, "new unsent draft")
        self.assertIn("may still have changed", item._transcript.text)
        self.assertIn("Request error:", item._transcript.text)
        self.assertNotIn("Not sent", item._transcript.text)
        self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.ERROR)
        self.assertFalse(item._busy)
        self.assertTrue(item._send.enabled)
        item.windowResized(None)
        self.assertEqual(calls, ["change something"])

    def test_idle_new_chat_button_restores_empty_state_without_erasing_draft(self):
        item = panel(lambda *args: "ok")
        item._append("Hermes", "Earlier reply")
        item._input.setText("draft")
        previous_session = item._session_id
        item._new_chat.actions[0].actionPerformed(None)
        self.assertEqual(item._transcript.text, SIDEBAR.EMPTY_TRANSCRIPT)
        self.assertEqual(item._lines, [])
        self.assertEqual(item._input.text, "draft")
        self.assertNotEqual(item._session_id, previous_session)
        self.assertEqual(item._status.text, SIDEBAR.READY_STATUS)
        self.assertTrue(item._send.enabled)

    def test_enter_sends_and_shift_enter_does_not(self):
        calls = []
        item = panel(lambda message, _session, _document: calls.append(message) or "ok")
        item._input.setText("line one\n")
        item.keyReleased(types.SimpleNamespace(KeyCode=SIDEBAR.KEY_RETURN, Modifiers=SIDEBAR.MODIFIER_SHIFT))
        item.keyReleased(types.SimpleNamespace(KeyCode=65, Modifiers=0))
        self.assertEqual((item._lines, item._input.text), ([], "line one\n"))
        item.keyReleased(types.SimpleNamespace(KeyCode=SIDEBAR.KEY_RETURN, Modifiers=0))
        self.assertEqual(item._lines, ["You: line one"])
        self.assertTrue(item._async.posted.wait(5))


class PackagingTests(unittest.TestCase):
    def test_extension_ships_and_registers_the_sidebar(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import build_oxt
        finally:
            sys.path.pop(0)
        for name in ("Sidebar.xcu", "Factories.xcu", "dialogs/SidecarChatPanel.xdl", "icons/sidecar-deck.png",
                     "python/hermes_sidebar.py", "python/panel_registry.py", "python/office_ops.py"):
            self.assertIn(name, build_oxt.FILES)
            self.assertTrue((ROOT / "extension" / name).is_file(), name)
        manifest = (ROOT / "extension" / "META-INF" / "manifest.xml").read_text(encoding="utf-8")
        for name in ("Sidebar.xcu", "Factories.xcu", "python/hermes_sidebar.py"):
            self.assertIn(f'manifest:full-path="{name}"', manifest)
        dialog = (ROOT / "extension" / "dialogs" / "SidecarChatPanel.xdl").read_text(encoding="utf-8")
        for control in SIDEBAR.plan_layout(300, 600):
            self.assertIn(f'dlg:id="{control}"', dialog)
        self.assertIn("vnd.atlasomnia.sidecar:open-chat", (ROOT / "extension" / "Addons.xcu").read_text(encoding="utf-8"))
        factories = (ROOT / "extension" / "Factories.xcu").read_text(encoding="utf-8")
        sidebar = (ROOT / "extension" / "Sidebar.xcu").read_text(encoding="utf-8")
        self.assertIn(SIDEBAR.FACTORY_IMPLEMENTATION, factories)
        self.assertIn("private:resource/toolpanel/SidecarSidebar/SidecarChatPanel", sidebar)
        self.assertIn("<value>SidecarSidebar</value>", factories)
        self.assertIn(SIDEBAR.DIALOG_URL.split("/", 3)[2], (ROOT / "extension" / "description.xml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
