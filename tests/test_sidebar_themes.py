# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Explicit native theme selection, safe profile preference IO and display identity."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from test_sidebar import SIDEBAR, Context, Parent, ROOT, panel

REGISTRY = SIDEBAR.panel_registry


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.registry_patch = patch.object(REGISTRY, "_PANELS", [])
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.contexts = []

    def make_panel(self, folder=None, responder=None):
        ctx = Context()
        self.addCleanup(ctx.temporary.cleanup)
        if folder is not None:
            ctx.folder = folder
        self.contexts.append(ctx)
        return panel(responder or (lambda *args: "ok"), ctx=ctx, register=True)

    def test_native_toggle_persists_reopens_and_recolors_every_model(self):
        item = self.make_panel()
        self.assertEqual(item._theme, "dark")
        toggle = item.Window.getControl("ThemeToggle")
        self.assertEqual(toggle.model.values["Label"], "Theme: Dark")
        self.assertIn("Switch to Light", toggle.model.values["HelpText"])
        toggle.actions[0].actionPerformed(None)
        self.assertEqual(item._theme, "light")
        self.assertEqual(toggle.model.values["Label"], "Theme: Light")
        self.assertIn("Switch to Dark", toggle.model.values["HelpText"])
        self.assertEqual(REGISTRY.read_theme(item._theme_folder), "light")
        preference = item._theme_folder / REGISTRY.PREFERENCE_NAME
        self.assertEqual(json.loads(preference.read_text()), {"theme": "light"})
        if os.name != "nt":  # Windows privacy is the DACL (test_private_files), not mode bits
            self.assertEqual(stat.S_IMODE(preference.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(item._theme_folder.stat().st_mode), 0o700)
        for name, role in (("Transcript", "surface"), ("Input", "raised"),
                           ("SendBackplate", "primary"), ("NewChatBackplate", "secondary"),
                           ("ThemeBackplate", "secondary"), ("ComposerBorderTop", "border")):
            self.assertEqual(item.Window.getControl(name).model.values["BackgroundColor"],
                             SIDEBAR.PALETTES["light"][role])
        self.assertEqual(item.Window.getModel().values["BackgroundColor"], 0xF4F7F9)
        self.assertEqual(item._input.model.values["TextColor"], 0x0C1117)
        self.assertEqual(item._send.model.values["TextColor"], 0xFFFFFF)
        item.disposing(None)
        reopened = self.make_panel(item._theme_folder)
        self.assertEqual(reopened._theme, "light")
        reopened.Window.getControl("ThemeToggle").actions[0].actionPerformed(None)
        self.assertEqual(REGISTRY.read_theme(item._theme_folder), "dark")
        self.assertEqual(reopened.Window.getModel().values["BackgroundColor"], SIDEBAR.GROUND)
        self.assertEqual([p.name for p in item._theme_folder.iterdir()], [REGISTRY.PREFERENCE_NAME])

    def test_siblings_propagate_but_other_profiles_and_disposed_controls_do_not(self):
        first = self.make_panel()
        sibling = self.make_panel(first._theme_folder)
        disposed = self.make_panel(first._theme_folder)
        other = self.make_panel()
        disposed.disposing(None)
        with patch.object(disposed, "_style_controls", side_effect=AssertionError("dead controls")):
            first.change_theme("light")
        self.assertEqual((first._theme, sibling._theme, other._theme, disposed._theme),
                         ("light", "light", "dark", "dark"))
        self.assertNotIn(disposed, REGISTRY.live())
        self.assertIs(REGISTRY.latest(), other)

    def test_busy_and_error_state_draft_history_identity_and_selection_are_preserved(self):
        release = threading.Event()
        self.addCleanup(release.set)
        item = self.make_panel(responder=lambda *args: release.wait(5) and "reply")
        item.submit("question")
        item._input.setText("unsent 日本語")
        item._input.setSelection((2, 4))
        before = (list(item._lines), item._transcript.text, item._session_id,
                  item._generation, item._status.text, item._input.selection)
        with patch.object(SIDEBAR, "ask_hermes", side_effect=AssertionError("theme made request")):
            item.change_theme("light")
            self.assertEqual((list(item._lines), item._transcript.text, item._session_id,
                              item._generation, item._status.text, item._input.selection), before)
            self.assertEqual(item._input.text, "unsent 日本語")
            self.assertTrue(item._busy)
            self.assertFalse(item._send.enabled)
            self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.PALETTES["light"]["running"])
            item._set_status("Reply failed — check conversation and document.", "error")
            error = item._status.text
            item.change_theme("dark")
            self.assertEqual(item._status.text, error)
            self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.ERROR)
            item.change_theme("light")
            self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.PALETTES["light"]["error"])
        release.set()
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        self.assertEqual(item._input.text, "unsent 日本語")
        self.assertEqual(item._lines, ["You: question", "Hermes: reply"])
        for control in item.Window.controls.values():
            self.assertNotIn("SidecarSidebarChat", control.writes)

    def test_save_failure_separate_notice_preserves_error_and_content_and_retry_clears_notice(self):
        item = self.make_panel()
        sibling = self.make_panel(item._theme_folder)
        item._append("Hermes", "earlier reply")
        item._input.setText("draft")
        item._set_status("Uncertain outcome: check document.", "error")
        before = item.snapshot()
        with patch.object(REGISTRY, "write_theme", side_effect=PermissionError("private path not exposed")):
            item.change_theme("light")
        self.assertEqual(item._theme, "light")
        self.assertEqual(item._input.text, "draft")
        self.assertEqual(item._lines, before["lines"])
        self.assertEqual(item._session_id, before["session_id"])
        self.assertEqual(item._status.text, before["status"])
        self.assertIn("Theme not saved", item.Window.getControl("ThemeNotice").text)
        self.assertEqual(item.Window.getControl("ThemeNotice").text, sibling.Window.getControl("ThemeNotice").text)
        self.assertEqual(REGISTRY.read_theme(item._theme_folder), "dark")
        item.change_theme("dark")
        self.assertEqual(item.Window.getControl("ThemeNotice").text, "")
        self.assertEqual(sibling.Window.getControl("ThemeNotice").text, "")

    def test_invalid_selection_does_not_change_any_state(self):
        item = self.make_panel()
        before = item.snapshot()
        with self.assertRaises(ValueError):
            item.change_theme("auto")
        self.assertEqual(item.snapshot(), before)
        self.assertFalse(item._theme_folder.exists())

    def test_constructor_falls_back_without_overwriting_malformed_saved_preference(self):
        ctx = Context()
        self.addCleanup(ctx.temporary.cleanup)
        ctx.folder.mkdir()
        preference = ctx.folder / REGISTRY.PREFERENCE_NAME
        preference.write_text('{"theme":"auto"}')
        item = self.make_panel(ctx.folder)
        self.assertEqual(item._theme, "dark")
        self.assertEqual(preference.read_text(), '{"theme":"auto"}')

    def test_actual_validation_and_request_errors_keep_their_text_across_switches(self):
        def unavailable(*args):
            raise SIDEBAR.ChatUnavailable("Connection lost; check the document before resending.")
        item = self.make_panel(responder=unavailable)
        item.submit("x" * (SIDEBAR.MAX_MESSAGE + 1))
        validation = item._status.text
        item.change_theme("light")
        self.assertEqual(item._status.text, validation)
        self.assertEqual(len(item._input.text), SIDEBAR.MAX_MESSAGE + 1)
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item._async.run()
        error = item._status.text
        history = item._transcript.text
        item.change_theme("dark")
        item.change_theme("light")
        self.assertEqual(item._status.text, error)
        self.assertEqual(item._transcript.text, history)
        self.assertEqual(item._status.model.values["TextColor"], SIDEBAR.PALETTES["light"]["error"])
        self.assertTrue(item._send.enabled)
        self.assertFalse(item._busy)

    def test_disposed_panel_ignores_queued_reply_and_theme_actions(self):
        item = self.make_panel()
        item.submit("question")
        self.assertTrue(item._async.posted.wait(5))
        item.disposing(None)
        with patch.object(item, "_append", side_effect=AssertionError("disposed")):
            item._async.run()
            item.change_theme("light")
        self.assertFalse(item._theme_folder.exists())

    def test_all_actual_model_text_pairs_meet_aa_in_both_themes_and_states(self):
        def luminance(color):
            channels = [(color >> shift & 255) / 255 for shift in (16, 8, 0)]
            values = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
            return sum(c * w for c, w in zip(values, (.2126, .7152, .0722)))
        item = self.make_panel()
        for theme in ("light", "dark"):
            item.change_theme(theme)
            for role in ("muted", "running", "error"):
                item._set_status("fixture", role)
                for name, control in item.Window.controls.items():
                    values = control.model.values
                    if values["TextColor"] is None:
                        continue
                    a, b = sorted((luminance(values["TextColor"]), luminance(values["BackgroundColor"])))
                    self.assertGreaterEqual((b + .05) / (a + .05), 4.5, (theme, role, name))

    def test_theme_layout_and_resize_preserve_focus_and_contents(self):
        item = self.make_panel()
        item._input.setText("draft")
        for theme in ("light", "dark"):
            item.change_theme(theme)
            for width in (300, 380, 520):
                for height in (420, 650, 900):
                    boxes = SIDEBAR.plan_layout(width, height)
                    names = ("Title", "Subtitle", "ThemeToggle", "Status", "TranscriptBackplate",
                             "ComposerLabel", "InputHint", "Input", "Send", "ComposerHelp")
                    for upper, lower in zip(names, names[1:]):
                        self.assertLessEqual(boxes[upper][1] + boxes[upper][3], boxes[lower][1])
                    item._layout(width, height)
                    self.assertEqual(item._input.text, "draft")
                    self.assertGreaterEqual(boxes["ThemeToggle"][3], 28)
        # Fake boundary intentionally has no setFocus method: resize/theme must not call it.


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / "hermes-bridge"
        self.folder.mkdir(mode=0o700)
        self.path = self.folder / REGISTRY.PREFERENCE_NAME

    def test_missing_malformed_oversize_and_wrong_shapes_fall_back_without_rewrite(self):
        self.assertEqual(REGISTRY.read_theme(self.folder), "dark")
        for raw in (b"{", b"[]", b"null", b'{"theme":"auto"}', b'{"theme":[]}',
                    b'{"theme":"Light"}', b'{"theme":"light","token":"not-a-token"}',
                    b'\xff', b" " * 129):
            self.path.write_bytes(raw)
            self.assertEqual(REGISTRY.read_theme(self.folder), "dark", raw)
            self.assertEqual(self.path.read_bytes(), raw)
        with self.assertRaises(ValueError):
            REGISTRY.write_theme(self.folder, "auto")

    def test_file_symlink_hardlink_fifo_and_directory_are_not_read_or_overwritten(self):
        target = self.root / "unrelated"
        target.write_text('{"theme":"light"}')
        self.path.symlink_to(target)
        self.assertEqual(REGISTRY.read_theme(self.folder), "dark")
        with self.assertRaises(OSError):
            REGISTRY.write_theme(self.folder, "light")
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(REGISTRY.read_theme(self.folder), "dark")
        with self.assertRaises(OSError):
            REGISTRY.write_theme(self.folder, "light")
        self.path.unlink()
        if hasattr(os, "mkfifo"):  # no FIFOs on Windows
            os.mkfifo(self.path)
            self.assertEqual(REGISTRY.read_theme(self.folder), "dark")
            with self.assertRaises(OSError):
                REGISTRY.write_theme(self.folder, "light")
            self.path.unlink()
        self.path.mkdir()
        self.assertEqual(REGISTRY.read_theme(self.folder), "dark")
        with self.assertRaises(OSError):
            REGISTRY.write_theme(self.folder, "light")
        self.assertEqual(target.read_text(), '{"theme":"light"}')

    def test_symlink_directory_and_ancestor_cannot_redirect_preferences(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for folder in (alias / "hermes-bridge", self.root / "other" / "hermes-bridge"):
            if folder.parent.name == "other":
                folder.parent.mkdir()
                folder.symlink_to(self.folder, target_is_directory=True)
            self.assertEqual(REGISTRY.read_theme(folder), "dark")
            with self.assertRaises(OSError):
                REGISTRY.write_theme(folder, "light")
        self.assertFalse(self.path.exists())

    def test_failed_atomic_replace_preserves_previous_preference_and_cleans_temporary(self):
        REGISTRY.write_theme(self.folder, "light")
        with patch.object(REGISTRY.os, "replace", side_effect=PermissionError("read-only")):
            with self.assertRaises(OSError):
                REGISTRY.write_theme(self.folder, "dark")
        self.assertEqual(REGISTRY.read_theme(self.folder), "light")
        self.assertEqual([p.name for p in self.folder.iterdir()], [REGISTRY.PREFERENCE_NAME])


class BrandingTests(unittest.TestCase):
    def test_display_metadata_changes_without_internal_identifier_or_icon_changes(self):
        addons = (ROOT / "extension/Addons.xcu").read_text()
        self.assertIn("Open Sidecar chat", addons)
        self.assertIn("Copy Sidecar pairing token", addons)
        self.assertIn("vnd.atlasomnia.sidecar:open-chat", addons)
        self.assertIn("vnd.atlasomnia.sidecar:copy-token", addons)
        sidebar = (ROOT / "extension/Sidebar.xcu").read_text()
        self.assertIn(">Sidecar</value>", sidebar)
        self.assertIn("icons/sidecar-deck.png", sidebar)
        description = ET.parse(ROOT / "extension/description.xml").getroot()
        ns = "{http://openoffice.org/extensions/description/2006}"
        identifier = description.find(ns + "identifier")
        display_name = description.find(ns + "display-name/" + ns + "name")
        assert identifier is not None and display_name is not None
        self.assertEqual(identifier.attrib["value"], "com.atlasomnia.sidecar.libreoffice")
        self.assertEqual(display_name.text, "Sidecar for LibreOffice")
        manifest = (ROOT / "plugin/plugin.yaml").read_text()
        self.assertIn("name: atlas-sidecar-libreoffice\n", manifest)
        self.assertIn("Sidecar for LibreOffice by Atlas Omnia", manifest)
        for document in (None, {}, {"type": []}, {"type": "word"}):
            self.assertEqual(SIDEBAR.display_title(document), "Sidecar for LibreOffice")
