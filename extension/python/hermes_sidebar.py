# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Sidecar launcher panel in LibreOffice's sidebar.

A sidebar deck ("Sidecar") with one panel, built from ``dialogs/SidecarChatPanel.xdl``: the
brand line, one status line and an **Open Sidecar** button. The chat itself is the Sidecar web
page that the Hermes plugin serves on loopback, shown in a browser window beside LibreOffice;
``sidecar_launcher.py`` opens it. The panel holds no Hermes credential and starts a browser only
when the button is pressed.
"""
from __future__ import annotations

from pathlib import Path
import sys
import os
from typing import Any, Callable

import uno
import unohelper
from com.sun.star.awt import XActionListener, XCallback, XWindowListener, XTopWindowListener
from com.sun.star.frame import XFrameActionListener
from com.sun.star.document import XDocumentEventListener
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement, XUIElementFactory

# LibreOffice loads this component by URI; make the sibling helpers importable (see hermes_bridge).
_COMPONENT_DIR = str(Path(__file__).resolve().parent)
if _COMPONENT_DIR not in sys.path:
    sys.path.insert(0, _COMPONENT_DIR)
import panel_registry  # noqa: E402
import sidecar_launcher  # noqa: E402

FACTORY_IMPLEMENTATION = "com.atlasomnia.sidecar.libreoffice.SidebarFactory"
DIALOG_URL = "vnd.sun.star.extension://com.atlasomnia.sidecar.libreoffice/dialogs/SidecarChatPanel.xdl"
# com.sun.star.ui.UIElementType.TOOLPANEL and com.sun.star.awt.PosSize.POSSIZE
TOOLPANEL = 7
POSSIZE = 15
MARGIN = 12
MINIMAL_WIDTH = 240
STATUS_TOP = 48
STATUS_HEIGHT = 64
BUTTON_HEIGHT = 32
BUTTON_MAX_WIDTH = 220
PANEL_HEIGHT = STATUS_TOP + STATUS_HEIGHT + 8 + BUTTON_HEIGHT + MARGIN
BUTTON_LABEL = "Open Sidecar"
BUTTON_HELP = "Open the Sidecar chat window for this document beside LibreOffice."
CONTROLS = ("BrandMark", "Title", "Subtitle", "Status", "OpenSidecar")
# Kept names other modules and suites rely on: the default chat port the plugin documents, and the
# token reader with its error type (the launcher's own).
DEFAULT_CHAT_PORT = sidecar_launcher.DEFAULT_CHAT_PORT
ChatUnavailable = sidecar_launcher.LauncherError
read_pairing_token = sidecar_launcher.read_pairing_token


def display_title(document: Any) -> str:
    kind = document.get("type") if isinstance(document, dict) else None
    app = {"calc": "Calc", "writer": "Writer", "impress": "Impress"}.get(kind) if isinstance(kind, str) else None
    return "Sidecar for " + (app or "LibreOffice")


def plan_layout(width: int) -> dict[str, tuple[int, int, int, int]]:
    """Native pixel rectangles for a panel ``width`` wide (never narrower than the minimum)."""
    width = max(int(width), MINIMAL_WIDTH)
    inner = width - 2 * MARGIN
    button_y = STATUS_TOP + STATUS_HEIGHT + 8
    return {
        "BrandMark": (MARGIN, 10, 26, 26),
        "Title": (MARGIN + 34, 8, inner - 34, 20),
        "Subtitle": (MARGIN + 34, 28, inner - 34, 16),
        "Status": (MARGIN, STATUS_TOP, inner, STATUS_HEIGHT),
        "OpenSidecar": (MARGIN, button_y, min(inner, BUTTON_MAX_WIDTH), BUTTON_HEIGHT),
    }


def scheme_from_color(color: Any) -> str | None:
    """``dark`` or ``light`` from a UNO colour (0xRRGGBB) by its relative luminance."""
    if not isinstance(color, int) or color < 0:
        return None
    red, green, blue = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    return "dark" if (0.2126 * red + 0.7152 * green + 0.0722 * blue) < 128 else "light"


class _OnMainThread(unohelper.Base, XCallback):
    def __init__(self, fn: Callable[[], None]) -> None:
        self._fn = fn

    def notify(self, _data: Any) -> None:
        self._fn()


# Document-owned, never panel-owned. All UNO calls here stay on the callback/main thread.
_FOLLOW_LISTENERS: dict[str, Any] = {}


class _FollowListener(unohelper.Base, XWindowListener, XTopWindowListener,
                      XFrameActionListener, XDocumentEventListener):
    """Copy only geometry/state into C2's nonblocking mailbox; no worker reads UNO."""

    def __init__(self, document_id: str, frame: Any, handle: Any, work_area: Any,
                 launcher: Any) -> None:
        self.document_id, self.frame, self.handle = document_id, frame, handle
        self.launcher, self.work_area = launcher, work_area
        self.window = frame.getContainerWindow()
        self.model = frame.getController().getModel()
        self.hidden = False
        self.closed = False
        self._registered = []
        try:
            for owner, add, remove in (
                (self.window, "addWindowListener", "removeWindowListener"),
                (self.window, "addTopWindowListener", "removeTopWindowListener"),
                (frame, "addFrameActionListener", "removeFrameActionListener"),
                (self.model, "addDocumentEventListener", "removeDocumentEventListener"),
            ):
                getattr(owner, add)(self)
                self._registered.append((owner, remove))
            self._copy()
        except Exception:
            self.close()
            raise

    def _copy(self) -> None:
        if self.closed:
            return
        if not self.handle.alive:
            self.close()
            return
        try:
            # Hidden is latched: move/resize/deactivate cannot reveal a hidden frame.
            if self.hidden:
                self.handle.update(None, "hidden", self.work_area)
                return
            if self.window.IsMinimized:
                self.handle.update(None, "minimized", self.work_area)
                return
            area = self.window.getPosSize()
            rect = (int(area.X), int(area.Y), int(area.Width), int(area.Height))
            if rect[2] < 240 or rect[3] < 240:
                self.handle.update(None, "hidden", self.work_area)
                return
            self.handle.update(rect, "normal", self.work_area)
        except Exception:
            # Disposed UNO objects are not normal/fallback geometry.
            self.close()

    def close(self, *, release: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self.handle.update(None, "window_closed", self.work_area)
        self.handle.close()
        if _FOLLOW_LISTENERS.get(self.document_id) is self:
            del _FOLLOW_LISTENERS[self.document_id]
        for owner, remove in self._registered:
            try:
                getattr(owner, remove)(self)
            except Exception:
                pass  # disposed owner: no listener can remain attached to a live window
        self._registered.clear()
        if release:
            self.launcher.on_document_closed(self.document_id)

    def windowMoved(self, _event: Any) -> None:
        self._copy()

    def windowResized(self, _event: Any) -> None:
        self._copy()

    def windowHidden(self, _event: Any) -> None:
        self.hidden = True
        self._copy()

    def windowShown(self, _event: Any) -> None:
        self.hidden = False
        self._copy()

    def windowMinimized(self, _event: Any) -> None:
        if not self.closed and self.handle.alive:
            self.handle.update(None, "minimized", self.work_area)

    def windowNormalized(self, _event: Any) -> None:
        self._copy()

    def windowOpened(self, _event: Any) -> None:
        self._copy()

    def windowActivated(self, _event: Any) -> None:
        self._copy()

    def windowDeactivated(self, _event: Any) -> None:
        return None

    def windowClosing(self, _event: Any) -> None:
        # Closing may be vetoed. Do not end the binding until the actual closed event.
        return None

    def windowClosed(self, _event: Any) -> None:
        self.close(release=True)

    def frameAction(self, event: Any) -> None:
        action = getattr(event.Action, "value", event.Action)
        if action == "COMPONENT_DETACHING":
            self.close(release=True)

    def documentEventOccured(self, event: Any) -> None:
        if event.EventName == "OnUnload":
            self.close(release=True)

    def disposing(self, _event: Any) -> None:
        self.close(release=True)


def _attach_follow(document: Any, frame: Any, work_area: Any, launcher: Any) -> Any:
    if (sys.platform != "darwin" or os.environ.get("HERMES_LO_SIDECAR_NO_FOLLOW") == "1"
            or not isinstance(document, dict)):
        return None
    key = document["document_id"]
    existing = _FOLLOW_LISTENERS.get(key)
    if existing is not None and not existing.closed and existing.handle.alive:
        # Another view of the same model must never take over the launching frame.
        return existing.handle
    if existing is not None:
        existing.close()
    factory = getattr(launcher, "follow_handle", None)
    handle = factory(key) if factory else None
    if handle is None or not handle.alive:
        return None
    try:
        listener = _FollowListener(key, frame, handle, work_area, launcher)
    except Exception:
        handle.close()
        return None
    _FOLLOW_LISTENERS[key] = listener
    launcher.register_follow_listener(key, listener.close)
    return handle


class LauncherPanel(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel, XActionListener, XWindowListener):
    def __init__(self, ctx: Any, frame: Any, parent: Any, url: str, launcher: Any = None) -> None:
        self.ctx = ctx
        self.Frame = frame
        self.ResourceURL = url
        self.Type = TOOLPANEL
        self._launcher = launcher or sidecar_launcher.LAUNCHER
        self._disposed = False
        self._busy = False
        self._action = 0
        self._width_hint: int | None = None
        self._status_text = ""
        manager = ctx.getServiceManager()
        paths = manager.createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
        self._bridge_folder = Path(uno.fileUrlToSystemPath(paths.substituteVariables("$(user)/hermes-bridge", True)))
        token_path = self._bridge_folder / "token"
        self._launcher.configure(lambda: read_pairing_token(token_path))
        self._async = manager.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
        provider = manager.createInstanceWithContext("com.sun.star.awt.ContainerWindowProvider", ctx)
        self.Window = provider.createContainerWindow(DIALOG_URL, "", parent, None)
        self._parent = parent
        self._status = self.Window.getControl("Status")
        self._button = self.Window.getControl("OpenSidecar")
        self._button.addActionListener(self)
        self._button.getModel().setPropertyValue("Label", BUTTON_LABEL)
        self._button.getModel().setPropertyValue("HelpText", BUTTON_HELP)
        parent.addWindowListener(self)
        self.Window.getControl("Title").setText(display_title(self._document()))
        self.Window.getControl("Subtitle").setText("by Atlas Omnia")
        self._show(sidecar_launcher.CHECKING, False)
        self._layout()
        self.Window.setVisible(True)
        panel_registry.register(self)
        self._launcher.probe(self._reporter(probe=True))

    # status
    def _show(self, text: str, busy: bool) -> None:
        if self._disposed:
            return
        self._busy = busy
        self._status_text = text
        self._status.setText(text)
        self._button.setEnable(not busy)

    def _reporter(self, *, probe: bool = False) -> Callable[[str, bool], None]:
        """A thread-safe report callback for one action; a newer click fences older results."""
        action = self._action

        def report(text: str, busy: bool) -> None:
            def show() -> None:
                if self._disposed or action != self._action or (probe and self._busy):
                    return
                self._show(text, busy)
            self._async.addCallback(_OnMainThread(show), None)
        return report

    # the button
    def actionPerformed(self, _event: Any) -> None:
        self.open_sidecar()

    def open_sidecar(self) -> bool:
        """What the Open Sidecar button does (main thread). True when a launch was started."""
        if self._disposed or self._busy:
            return False
        self._action += 1
        document, work_area = self._document(), self._work_area()
        handle = _attach_follow(document, self.Frame, work_area, self._launcher)
        args = (document, self._host_scheme(), self._frame_rect(), work_area, self._reporter())
        return bool(self._launcher.open(*args, follow=handle) if handle is not None else
                    self._launcher.open(*args))

    def _document(self) -> Any:
        """Identity of the document this panel's window shows, as the bridge names it."""
        try:
            model = self.Frame.getController().getModel()
            identify = getattr(panel_registry, "identify", None)
            return identify(model) if identify and model is not None else None
        except Exception:
            return None

    def _frame_rect(self) -> tuple[int, int, int, int] | None:
        """The document window's client rectangle in screen points (contract 8.3)."""
        try:
            area = self.Frame.getContainerWindow().getPosSize()
            return (int(area.X), int(area.Y), int(area.Width), int(area.Height))
        except Exception:
            return None

    def _work_area(self) -> tuple[int, int, int, int] | None:
        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
            area = toolkit.getWorkArea()
            return (int(area.X), int(area.Y), int(area.Width), int(area.Height))
        except Exception:
            return None

    def _host_scheme(self) -> str | None:
        """The appearance the page's Auto setting follows: the theme saved by the 0.4.0 panel
        when there is one (read once), else LibreOffice's own window colour."""
        saved = panel_registry.saved_theme(self._bridge_folder)
        if saved is not None:
            return saved
        try:
            return scheme_from_color(self._parent.StyleSettings.WindowColor)  # an IDL attribute in PyUNO
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        return {"status": self._status_text, "busy": self._busy, "button_enabled": not self._busy,
                "button_label": BUTTON_LABEL, "title": str(self.Window.getControl("Title").getText()),
                "document": self._document(), "frame": self._frame_rect(), "work_area": self._work_area(),
                "host_scheme": self._host_scheme(),
                "layout": {"requested_width": self._width_hint,
                           "parent_width": self._parent.getPosSize().Width}}

    # XUIElement / XToolPanel / XSidebarPanel
    def getRealInterface(self) -> Any:
        return self

    def createAccessible(self, _parent: Any) -> Any:
        return self.Window

    def getHeightForWidth(self, width: int) -> Any:
        if width > 0 and width != self._width_hint:
            self._width_hint = width
            self._async.addCallback(_OnMainThread(self._layout), None)
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        size.Minimum, size.Maximum, size.Preferred = PANEL_HEIGHT, PANEL_HEIGHT, PANEL_HEIGHT
        return size

    def getMinimalWidth(self) -> int:
        return MINIMAL_WIDTH

    # layout
    def _layout(self, width: int | None = None) -> None:
        if self._disposed:
            return
        allocated = (self._width_hint if self._width_hint is not None else
                     width if isinstance(width, int) and width > 0 else int(self._parent.getPosSize().Width))
        allocated = max(allocated, MINIMAL_WIDTH)
        self.Window.setPosSize(0, 0, allocated, PANEL_HEIGHT, POSSIZE)
        for name, (x, y, w, h) in plan_layout(allocated).items():
            self.Window.getControl(name).setPosSize(x, y, w, h, POSSIZE)

    def windowResized(self, event: Any) -> None:
        self._layout(getattr(event, "Width", None))

    def windowMoved(self, _event: Any) -> None:
        return None

    def windowShown(self, _event: Any) -> None:
        self._layout()

    def windowHidden(self, _event: Any) -> None:
        return None

    def disposing(self, _event: Any) -> None:
        self._disposed = True
        panel_registry.unregister(self)


def _log(ctx: Any, line: str) -> None:
    """Append one line to <profile>/user/hermes-bridge/sidebar.log; never raises."""
    try:
        paths = ctx.getServiceManager().createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
        folder = uno.fileUrlToSystemPath(paths.substituteVariables("$(user)/hermes-bridge", True))
        with open(f"{folder}/sidebar.log", "a", encoding="utf-8") as stream:
            stream.write(line.rstrip() + "\n")
    except Exception:
        return None


class PanelFactory(unohelper.Base, XUIElementFactory):
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def createUIElement(self, url: str, arguments: Any) -> Any:
        try:
            values = {argument.Name: argument.Value for argument in arguments}
            return LauncherPanel(self.ctx, values.get("Frame"), values.get("ParentWindow"), url)
        except Exception as exc:
            _log(self.ctx, f"panel creation failed: {type(exc).__name__}")
            raise


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PanelFactory, FACTORY_IMPLEMENTATION, ("com.sun.star.ui.UIElementFactory",))
