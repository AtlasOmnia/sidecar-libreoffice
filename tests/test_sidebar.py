# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The launcher panel in LibreOffice's sidebar (extension/python/hermes_sidebar.py): its layout
file, the status line and Open Sidecar button, main-thread reporting, what it hands the launcher,
the one-time read of the 0.4.0 theme, and its packaging. The 0.4.0 native transcript, input and
theme toggle are gone."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
sys.path.insert(0, str(EXTENSION / "python"))
import panel_registry  # noqa: E402
import sidecar_launcher  # noqa: E402

DLG = "{http://openoffice.org/2000/dialog}"
OOR = "{http://openoffice.org/2001/registry}"


def load_sidebar():
    uno = types.ModuleType("uno")
    uno.createUnoStruct = lambda _name: types.SimpleNamespace()
    uno.fileUrlToSystemPath = lambda value: value
    unohelper = types.ModuleType("unohelper")
    unohelper.Base = type("UnoBase", (), {})
    unohelper.ImplementationHelper = lambda: types.SimpleNamespace(addImplementation=lambda *args: None)
    awt = types.ModuleType("com.sun.star.awt")
    ui = types.ModuleType("com.sun.star.ui")
    for module, names in ((awt, ("XActionListener", "XCallback", "XWindowListener")),
                          (ui, ("XSidebarPanel", "XToolPanel", "XUIElement", "XUIElementFactory"))):
        for name in names:
            setattr(module, name, type(name, (), {}))
    modules = {"uno": uno, "unohelper": unohelper, "com": types.ModuleType("com"), "com.sun": types.ModuleType("com.sun"),
               "com.sun.star": types.ModuleType("com.sun.star"), "com.sun.star.awt": awt, "com.sun.star.ui": ui}
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        spec = importlib.util.spec_from_file_location("tested_hermes_sidebar", EXTENSION / "python" / "hermes_sidebar.py")
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


def ask_hermes_0_4_0(port: int, token: str, message: str, session_id: str, document=None) -> str:
    """The 0.4.0 panel's chat client, verbatim in behaviour. 0.4.0 extensions keep sending it to
    the deprecated ``/chat`` route through 0.5.x (owner decision D2), so the plugin suites still
    exercise it; the 0.5.0 extension no longer ships it."""
    payload = {"message": message, "session_id": session_id}
    if document:
        payload["document"] = document
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=620.0)
    try:
        try:
            connection.request("POST", "/chat", body=body, headers={
                "Content-Type": "application/json", "Content-Length": str(len(body)),
                "Host": f"127.0.0.1:{port}", "X-Hermes-Plugin-Key": token, "Connection": "close",
            })
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
        except ConnectionRefusedError:
            raise SIDEBAR.ChatUnavailable(
                f"Hermes is not answering on 127.0.0.1:{port}. Start the Hermes gateway with the "
                "atlas-sidecar-libreoffice plugin enabled, then send again.") from None
        except (OSError, http.client.HTTPException):
            raise SIDEBAR.ChatUnavailable("The connection to Hermes was lost before a reply arrived. "
                                          "Hermes may still have changed the document: check it before sending again.") from None
    finally:
        connection.close()
    try:
        value = json.loads(raw.decode("utf-8")) if len(raw) <= 1024 * 1024 else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        raise SIDEBAR.ChatUnavailable(f"Hermes sent a reply this panel could not read (HTTP {response.status}).")
    if response.status == 200 and value.get("ok") is True and isinstance(value.get("reply"), str):
        return value["reply"]
    error = value.get("error")
    raise SIDEBAR.ChatUnavailable(str(error)[:500] if isinstance(error, str) and error
                                  else f"Hermes answered with HTTP {response.status}.")


# test_chat_bridge, test_chat_lifecycle and test_private_files import SIDEBAR from here.
SIDEBAR.ask_hermes = ask_hermes_0_4_0


class Model:
    def __init__(self):
        self.values = {"Label": "", "HelpText": ""}

    def setPropertyValue(self, name, value):
        if name not in self.values:
            raise ValueError("Unsupported UNO property: " + name)
        self.values[name] = value

    def getPropertyValue(self, name):
        return self.values[name]


class Control:
    def __init__(self, text=""):
        self.text, self.enabled, self.model = text, True, Model()
        self.actions, self.rectangles = [], []

    def getModel(self):
        return self.model

    def getText(self):
        return self.text

    def setText(self, value):
        self.text = value

    def setEnable(self, value):
        self.enabled = value

    def addActionListener(self, listener):
        self.actions.append(listener)

    def setPosSize(self, x, y, width, height, flags):
        self.rectangles.append((x, y, width, height))


class Window(Control):
    def __init__(self):
        super().__init__()
        root = ET.parse(EXTENSION / "dialogs" / "SidecarChatPanel.xdl").getroot()
        self.controls = {node.attrib[DLG + "id"]: Control(node.attrib.get(DLG + "value", ""))
                         for node in root.find(DLG + "bulletinboard")}
        self.visible = False

    def getControl(self, name):
        return self.controls[name]

    def setVisible(self, value):
        self.visible = value


class Rect(types.SimpleNamespace):
    pass


class Parent:
    def __init__(self, width=320, window_color=0x1E1E1E):
        self.area = Rect(X=0, Y=0, Width=width, Height=500)
        self.listeners = []
        self.window_color = window_color

    def getPosSize(self):
        return self.area

    def addWindowListener(self, listener):
        self.listeners.append(listener)

    @property
    def StyleSettings(self):  # XStyleSettingsSupplier's attribute, as PyUNO exposes it
        return types.SimpleNamespace(WindowColor=self.window_color)


class Async:
    def __init__(self):
        self.queue = []

    def addCallback(self, callback, data):
        self.queue.append(callback)

    def run(self):
        while self.queue:
            self.queue.pop(0).notify(None)


class Context:
    def __init__(self, folder: Path):
        self.window = Window()
        self.async_ = Async()
        self.folder = folder
        outer = self

        class Manager:
            def createInstanceWithContext(self, name, _ctx):
                if name == "com.sun.star.util.PathSubstitution":
                    return types.SimpleNamespace(substituteVariables=lambda value, _flag: str(outer.folder))
                if name == "com.sun.star.awt.AsyncCallback":
                    return outer.async_
                if name == "com.sun.star.awt.ContainerWindowProvider":
                    return types.SimpleNamespace(createContainerWindow=lambda *args: outer.window)
                if name == "com.sun.star.awt.Toolkit":
                    return types.SimpleNamespace(getWorkArea=lambda: Rect(X=0, Y=25, Width=2560, Height=1055))
                raise AssertionError(name)
        self.manager = Manager()

    def getServiceManager(self):
        return self.manager


class Frame:
    def __init__(self, model="doc"):
        self.model = model

    def getController(self):
        return types.SimpleNamespace(getModel=lambda: self.model)

    def getContainerWindow(self):
        return types.SimpleNamespace(getPosSize=lambda: Rect(X=100, Y=80, Width=1000, Height=700))


class FakeLauncher:
    def __init__(self):
        self.read_token = None
        self.probes, self.opens = [], []

    def configure(self, read_token):
        self.read_token = read_token

    def probe(self, report):
        self.probes.append(report)

    def open(self, document, host_scheme, frame, work_area, report):
        self.opens.append((document, host_scheme, frame, work_area, report))
        report(sidecar_launcher.OPENING, True)
        return True


DOCUMENT = {"document_id": "lo:1:2", "title": "sales.ods", "type": "calc"}


class LauncherPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "hermes-bridge"
        self.folder.mkdir(mode=0o700)
        self.ctx = Context(self.folder)
        self.launcher = FakeLauncher()
        self.identify = getattr(panel_registry, "identify", None)
        panel_registry.identify = lambda model: dict(DOCUMENT) if model == "doc" else None
        self.addCleanup(self.restore_identify)
        panel_registry._SAVED_THEMES.clear()
        self.addCleanup(panel_registry._SAVED_THEMES.clear)

    def restore_identify(self):
        if self.identify is None:
            panel_registry.__dict__.pop("identify", None)
        else:
            panel_registry.identify = self.identify

    def panel(self, parent=None, frame=None):
        panel = SIDEBAR.LauncherPanel(self.ctx, frame or Frame(), parent or Parent(), "private:resource/x",
                                      launcher=self.launcher)
        self.addCleanup(panel.disposing, None)
        return panel

    def test_layout_file_holds_only_the_launcher_controls(self):
        root = ET.parse(EXTENSION / "dialogs" / "SidecarChatPanel.xdl").getroot()
        ids = [node.attrib[DLG + "id"] for node in root.find(DLG + "bulletinboard")]
        self.assertEqual(ids, list(SIDEBAR.CONTROLS))
        for gone in ("Transcript", "Input", "Send", "NewChat", "ThemeToggle", "ThemeNotice", "Connectivity"):
            self.assertNotIn(gone, ids)
        button = root.find(f"{DLG}bulletinboard/{DLG}button")
        self.assertEqual(button.attrib[DLG + "value"], "Open Sidecar")
        self.assertEqual(button.attrib[DLG + "tab-index"], "0")  # first stop for keyboard users
        self.assertNotIn("background-color", (EXTENSION / "dialogs" / "SidecarChatPanel.xdl").read_text(encoding="utf-8"))

    def test_construction_shows_checking_and_probes_once(self):
        panel = self.panel()
        window = self.ctx.window
        self.assertTrue(window.visible)
        self.assertEqual(window.controls["Title"].text, "Sidecar for Calc")
        self.assertEqual(window.controls["Subtitle"].text, "by Atlas Omnia")
        self.assertEqual(window.controls["Status"].text, sidecar_launcher.CHECKING)
        self.assertEqual(window.controls["OpenSidecar"].model.values["Label"], "Open Sidecar")
        self.assertTrue(window.controls["OpenSidecar"].model.values["HelpText"])
        self.assertEqual(len(self.launcher.probes), 1)
        self.assertEqual(self.launcher.opens, [])
        self.assertIs(panel_registry.latest(), panel)

    def test_reports_are_applied_on_the_main_thread_only(self):
        self.panel()
        self.launcher.probes[0](sidecar_launcher.CHAT_OFF, False)
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.CHECKING)
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.CHAT_OFF)
        self.assertTrue(self.ctx.window.controls["OpenSidecar"].enabled)

    def test_the_button_hands_document_frame_work_area_and_scheme_to_the_launcher(self):
        panel = self.panel(parent=Parent(window_color=0xF0F0F0))
        self.ctx.window.controls["OpenSidecar"].actions[0].actionPerformed(None)
        document, scheme, frame, work_area, report = self.launcher.opens[0]
        self.assertEqual(document, DOCUMENT)
        self.assertEqual(scheme, "light")
        self.assertEqual(frame, (100, 80, 1000, 700))
        self.assertEqual(work_area, (0, 25, 2560, 1055))
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.OPENING)
        self.assertFalse(self.ctx.window.controls["OpenSidecar"].enabled)
        self.assertFalse(panel.open_sidecar())  # busy: a second press starts nothing
        self.assertEqual(len(self.launcher.opens), 1)
        report(sidecar_launcher.OPEN, False)
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.OPEN)
        self.assertTrue(self.ctx.window.controls["OpenSidecar"].enabled)

    def test_a_late_probe_never_overwrites_a_launch(self):
        self.panel()
        probe = self.launcher.probes[0]
        self.ctx.window.controls["OpenSidecar"].actions[0].actionPerformed(None)
        probe(sidecar_launcher.IDLE, False)
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.OPENING)

    def test_a_disposed_panel_ignores_reports_and_clicks(self):
        panel = self.panel()
        report = self.launcher.probes[0]
        panel.disposing(None)
        report(sidecar_launcher.IDLE, False)
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["Status"].text, sidecar_launcher.CHECKING)
        self.assertFalse(panel.open_sidecar())
        self.assertIsNone(panel_registry.latest())

    def test_the_token_is_read_from_this_profiles_bridge_folder(self):
        self.panel()
        (self.folder / "token").write_text("k" * 43 + "\n")
        os.chmod(self.folder / "token", 0o600)
        self.assertEqual(self.launcher.read_token(), "k" * 43)

    def test_the_saved_0_4_0_theme_is_the_initial_appearance(self):
        (self.folder / panel_registry.PREFERENCE_NAME).write_text(json.dumps({"theme": "light"}) + "\n")
        os.chmod(self.folder / panel_registry.PREFERENCE_NAME, 0o600)
        panel = self.panel(parent=Parent(window_color=0x101010))
        self.assertEqual(panel._host_scheme(), "light")
        (self.folder / panel_registry.PREFERENCE_NAME).write_text(json.dumps({"theme": "dark"}) + "\n")
        self.assertEqual(panel._host_scheme(), "light")  # read once per LibreOffice process
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [panel_registry.PREFERENCE_NAME])

    def test_without_a_saved_theme_libreoffice_colours_decide(self):
        self.assertEqual(self.panel(parent=Parent(window_color=0x1C1C1E))._host_scheme(), "dark")
        panel_registry._SAVED_THEMES.clear()
        self.assertEqual(self.panel(parent=Parent(window_color=0xFFFFFF))._host_scheme(), "light")
        self.assertFalse((self.folder / panel_registry.PREFERENCE_NAME).exists())
        class NoStyle(Parent):
            @property
            def StyleSettings(self):
                raise AttributeError("StyleSettings")
        panel_registry._SAVED_THEMES.clear()
        self.assertIsNone(self.panel(parent=NoStyle())._host_scheme())
        self.assertEqual(SIDEBAR.scheme_from_color(None), None)
        self.assertEqual(SIDEBAR.scheme_from_color(-1), None)

    def test_layout_fits_every_width_without_overlap(self):
        for width in (200, 240, 300, 420, 800):
            boxes = SIDEBAR.plan_layout(width)
            allocated = max(width, SIDEBAR.MINIMAL_WIDTH)
            for name, (x, y, w, h) in boxes.items():
                self.assertGreaterEqual(x, 0, name)
                self.assertLessEqual(x + w, allocated, name)
                self.assertLessEqual(y + h, SIDEBAR.PANEL_HEIGHT, name)
            stacked = sorted((boxes[n] for n in ("Subtitle", "Status", "OpenSidecar")), key=lambda r: r[1])
            for upper, lower in zip(stacked, stacked[1:]):
                self.assertLessEqual(upper[1] + upper[3], lower[1])

    def test_sidebar_host_size(self):
        panel = self.panel()
        size = panel.getHeightForWidth(360)
        self.assertEqual((size.Minimum, size.Maximum, size.Preferred), (SIDEBAR.PANEL_HEIGHT,) * 3)
        self.assertEqual(panel.getMinimalWidth(), SIDEBAR.MINIMAL_WIDTH)
        self.ctx.async_.run()
        self.assertEqual(self.ctx.window.controls["OpenSidecar"].rectangles[-1],
                         SIDEBAR.plan_layout(360)["OpenSidecar"])

    def test_snapshot_for_the_development_hook(self):
        snapshot = self.panel().snapshot()
        self.assertEqual(snapshot["status"], sidecar_launcher.CHECKING)
        self.assertEqual(snapshot["document"], DOCUMENT)
        self.assertEqual(snapshot["frame"], (100, 80, 1000, 700))
        self.assertTrue(snapshot["button_enabled"])

    def test_display_title(self):
        self.assertEqual(SIDEBAR.display_title({"type": "writer"}), "Sidecar for Writer")
        self.assertEqual(SIDEBAR.display_title({"type": "impress"}), "Sidecar for Impress")
        self.assertEqual(SIDEBAR.display_title(None), "Sidecar for LibreOffice")

    def test_no_chat_code_left_in_the_panel(self):
        source = (EXTENSION / "python" / "hermes_sidebar.py").read_text(encoding="utf-8")
        for gone in ("/chat", "/health", "X-Hermes-Plugin-Key", "http.client", "Transcript", "write_theme",
                     "ThemeToggle", "XKeyListener"):
            self.assertNotIn(gone, source)


class Packaging(unittest.TestCase):
    def test_sidebar_ids_are_kept(self):
        tree = ET.parse(EXTENSION / "Sidebar.xcu").getroot()
        names = [node.get(OOR + "name") for node in tree.iter("node")]
        self.assertIn("SidecarDeck", names)
        self.assertIn("SidecarChatPanel", names)
        text = (EXTENSION / "Sidebar.xcu").read_text(encoding="utf-8")
        self.assertIn("private:resource/toolpanel/SidecarSidebar/SidecarChatPanel", text)

    def test_manifest_and_build_list_carry_the_launcher_files(self):
        manifest = (EXTENSION / "META-INF" / "manifest.xml").read_text(encoding="utf-8")
        spec = importlib.util.spec_from_file_location("build_oxt_for_sidebar", ROOT / "scripts" / "build_oxt.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        for name in ("sidecar_launcher.py", "launcher_macos.py", "launcher_windows.py"):
            self.assertIn(f'manifest:full-path="python/{name}" manifest:media-type="application/binary"', manifest)
            self.assertIn(f"python/{name}", build.FILES)
        self.assertIn("dialogs/SidecarChatPanel.xdl", build.FILES)
        for name in build.FILES:
            self.assertTrue((EXTENSION / name).is_file(), name)
        self.assertEqual(len(build.FILES), len(set(build.FILES)))


if __name__ == "__main__":
    unittest.main()
