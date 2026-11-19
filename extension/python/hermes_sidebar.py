# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Hermes chat panel in LibreOffice's sidebar.

A sidebar deck ("Hermes") with one panel: a read-only transcript, an input box, and Send and New
chat buttons, built from ``dialogs/SidecarChatPanel.xdl``. Sending runs on a worker thread and the
reply is written back on LibreOffice's main thread.

Replies come from the chat endpoint that the Hermes plugin runs on loopback (``chat_bridge.py`` in
the plugin). The panel authenticates with the pairing token in this LibreOffice profile; it holds
no Hermes credential. LibreOffice sidebar panels are native controls, so replies are plain text.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Any, Callable
import uuid

import uno
import unohelper
from com.sun.star.awt import XActionListener, XCallback, XKeyListener, XTextListener, XWindowListener
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement, XUIElementFactory

# LibreOffice loads this component by URI; make the sibling helper importable (see hermes_bridge).
_COMPONENT_DIR = str(Path(__file__).resolve().parent)
if _COMPONENT_DIR not in sys.path:
    sys.path.insert(0, _COMPONENT_DIR)
import panel_registry  # noqa: E402
import private_files  # noqa: E402

FACTORY_IMPLEMENTATION = "com.atlasomnia.sidecar.libreoffice.SidebarFactory"
DIALOG_URL = "vnd.sun.star.extension://com.atlasomnia.sidecar.libreoffice/dialogs/SidecarChatPanel.xdl"
# com.sun.star.ui.UIElementType.TOOLPANEL and com.sun.star.awt.PosSize.POSSIZE
TOOLPANEL = 7
POSSIZE = 15
MARGIN = 12
GAP = 8
INPUT_HEIGHT = 84
BUTTON_HEIGHT = 32
BUTTON_WIDTH = 96
STATUS_HEIGHT = 40
COMPACT_STATUS_HEIGHT = 18
COMPOSER_HEIGHT = 202
INPUT_HINT = "Ask Hermes about this document…"
MINIMUM_HEIGHT = 420
GROUND = 0x0C1117
SURFACE = 0x101923
RAISED = 0x14212D
TEXT = 0xEEF2F6
MUTED = 0xAEB9C5
PRIMARY = 0x78D6C2
ERROR = 0xFFB3A7
RUNNING = 0xC9B8FF
EMPTY_TRANSCRIPT = (
    "Ask Hermes about this document.\n\n"
    "Try: Summarize this document, or ask a question about its contents.\n\n"
    "Replies appear here. Review document changes before sending again if a reply fails."
)
APP_COPY = {
    "calc": ("Spreadsheet assistant", "Ask Hermes about this spreadsheet.",
             "Try: Explain the selected cells, or summarize the sheet."),
    "writer": ("Writer assistant", "Ask Hermes about your writing.",
               "Try: Summarize this document, or rewrite a paragraph."),
    "impress": ("Impress assistant", "Ask Hermes about your presentation.",
                "Try: Review the slide outline, or improve a slide title."),
}


def application_copy(document: Any) -> tuple[str, str]:
    kind = document.get("type") if isinstance(document, dict) else None
    copy = APP_COPY.get(kind) if isinstance(kind, str) else None
    if copy is None:
        return "Document assistant", EMPTY_TRANSCRIPT
    subtitle, opening, example = copy
    return subtitle, (opening + "\n\n" + example + "\n\n"
                      "Replies appear here. Review document changes before sending again if a reply fails.")


READY_STATUS = "Ready for your message."
COMPOSER_HELP = "Enter sends · Shift+Enter adds a line"
MAX_MESSAGE = 8000
MINIMAL_WIDTH = 300
DEFAULT_CHAT_PORT = 8661
CHAT_TIMEOUT_SECONDS = 620.0
MAX_REPLY_BYTES = 1024 * 1024
HEALTH_TIMEOUT_SECONDS = 2.0
MAX_HEALTH_BYTES = 4096
TOKEN_HEADER = "X-Hermes-Plugin-Key"
# com.sun.star.awt.Key.RETURN and com.sun.star.awt.KeyModifier.SHIFT
KEY_RETURN = 1280
MODIFIER_SHIFT = 1
BORDER_COLOR = 0x5B7083
PALETTES = {
    "dark": dict(ground=GROUND, surface=SURFACE, raised=RAISED, text=TEXT,
                 muted=MUTED, primary=PRIMARY, on_primary=GROUND, secondary=TEXT,
                 border=BORDER_COLOR, error=ERROR, running=RUNNING),
    "light": dict(ground=0xF4F7F9, surface=0xFFFFFF, raised=0xEAF0F4, text=0x0C1117,
                  muted=0x4A5B6B, primary=0x1B7563, on_primary=0xFFFFFF,
                  secondary=0xEAF0F4, border=0x6F8292, error=0x9B2C1F, running=0x5B3FB5),
}


def display_title(document: Any) -> str:
    kind = document.get("type") if isinstance(document, dict) else None
    app = {"calc": "Calc", "writer": "Writer", "impress": "Impress"}.get(kind) if isinstance(kind, str) else None
    return "Sidecar for " + (app or "LibreOffice")


class ChatUnavailable(Exception):
    """A failure with a message that is safe and useful to show in the panel."""


def chat_port() -> int:
    value = os.environ.get("HERMES_LO_CHAT_PORT", "")
    return int(value) if value.isdigit() and 1024 <= int(value) <= 65535 else DEFAULT_CHAT_PORT


def read_pairing_token(path: Path) -> str:
    """The pairing token the bridge wrote in this profile; refuses a file others can read
    (POSIX permission bits, or the owner and DACL on Windows; see private_files)."""
    try:
        token = private_files.read_private_bytes(path, 4096).decode("utf-8").strip()
    except private_files.UnsafePath:
        raise ChatUnavailable("The pairing token file has unsafe permissions; restart LibreOffice.") from None
    except (OSError, UnicodeDecodeError):
        raise ChatUnavailable("The pairing token is missing; restart LibreOffice.") from None
    if len(token) < 32:
        raise ChatUnavailable("The pairing token is invalid; restart LibreOffice.")
    return token


def ask_hermes(port: int, token: str, message: str, session_id: str, document: Any = None) -> str:
    """Send one message to the plugin's loopback chat endpoint and return the reply text."""
    payload: dict[str, Any] = {"message": message, "session_id": session_id}
    if document:
        payload["document"] = document
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CHAT_TIMEOUT_SECONDS)
    try:
        try:
            connection.request("POST", "/chat", body=body, headers={
                "Content-Type": "application/json", "Content-Length": str(len(body)),
                "Host": f"127.0.0.1:{port}", TOKEN_HEADER: token, "Connection": "close",
            })
            response = connection.getresponse()
            raw = response.read(MAX_REPLY_BYTES + 1)
        except ConnectionRefusedError:
            raise ChatUnavailable(
                f"Hermes is not answering on 127.0.0.1:{port}. Start the Hermes gateway with the "
                "atlas-sidecar-libreoffice plugin enabled, then send again.") from None
        except (OSError, http.client.HTTPException):
            raise ChatUnavailable("The connection to Hermes was lost before a reply arrived. "
                                  "Hermes may still have changed the document: check it before sending again.") from None
    finally:
        connection.close()
    try:
        value = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_REPLY_BYTES else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        raise ChatUnavailable(f"Hermes sent a reply this panel could not read (HTTP {response.status}).")
    if response.status == 200 and value.get("ok") is True and isinstance(value.get("reply"), str):
        return value["reply"]
    error = value.get("error")
    raise ChatUnavailable(str(error)[:500] if isinstance(error, str) and error else f"Hermes answered with HTTP {response.status}.")


def probe_health(port: int, token: str) -> bool:
    """Check only the authenticated local bridge, never the model or chat endpoint.

    One bounded GET, no retry. Called on a worker; no UNO objects cross this boundary.
    A successful probe says nothing about provider availability or a chat's outcome.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT_SECONDS)
    try:
        connection.request("GET", "/health", headers={
            "Host": f"127.0.0.1:{port}", TOKEN_HEADER: token, "Connection": "close",
        })
        response = connection.getresponse()
        raw = response.read(MAX_HEALTH_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_HEALTH_BYTES:
            return False
        value = json.loads(raw.decode("utf-8"))
        return isinstance(value, dict) and value.get("ok") is True and value.get("status") == "ok"
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


def new_session_id() -> str:
    return f"lo-{uuid.uuid4().hex}"


def plan_layout(width: int, height: int, *, notice: bool = False,
                status_height: int = COMPACT_STATUS_HEIGHT) -> dict[str, tuple[int, int, int, int]]:
    """Native pixel rectangles; the sidebar host scrolls below our declared minimum.

    Never compress text or overlap the composer to fit an undersized host. The same
    minimum is returned by getHeightForWidth, so supported allocations fit exactly.
    Backplates are genuine fixed-text controls, not replacements for editable controls.
    """
    width, height = max(width, MINIMAL_WIDTH), max(height, MINIMUM_HEIGHT)
    inner = width - 2 * MARGIN
    composer_y = height - MARGIN - COMPOSER_HEIGHT
    button_y = composer_y + 140
    input_y = composer_y + 48
    # A second compact header row leaves the full title width available at 300px.
    # Only actual error/notice text consumes extra vertical space.
    transcript_y = 80 + status_height + (36 if notice else 0)
    transcript_height = composer_y - GAP - transcript_y
    theme_x = width - MARGIN - 106
    return {
        "BrandMark": (MARGIN, 10, 26, 26),
        "Title": (MARGIN + 34, 10, inner - 34, 24),
        "Subtitle": (MARGIN + 34, 34, inner - 34, 16),
        "ThemeBackplate": (theme_x, 50, 106, 28),
        "ThemeToggle": (theme_x, 50, 106, 28),
        "Connectivity": (MARGIN, 54, inner - 114, 20),
        "Status": (MARGIN, 80, inner, status_height),
        "ThemeNotice": (MARGIN, 80 + status_height, inner, 32 if notice else 0),
        "TranscriptBackplate": (MARGIN, transcript_y, inner, transcript_height),
        "Transcript": (MARGIN + 2, transcript_y + 2, inner - 4, transcript_height - 4),
        # Aqua may paint fixed-text siblings over one another regardless of XML
        # order. Tile the frame/interior; no full-area fixed-text backplate may
        # cover labels, hint, help, or the genuine edit. Button-only backings stay.
        "ComposerBorderTop": (MARGIN, composer_y, inner, 2),
        "ComposerBorderBottom": (MARGIN, composer_y + COMPOSER_HEIGHT - 2, inner, 2),
        "ComposerBorderLeft": (MARGIN, composer_y + 2, 2, COMPOSER_HEIGHT - 4),
        "ComposerBorderRight": (width - MARGIN - 2, composer_y + 2, 2, COMPOSER_HEIGHT - 4),
        "ComposerFillLeft": (MARGIN + 2, composer_y + 2, 6, COMPOSER_HEIGHT - 4),
        "ComposerFillRight": (width - MARGIN - 8, composer_y + 2, 6, COMPOSER_HEIGHT - 4),
        "ComposerFillTop": (MARGIN + 8, composer_y + 2, inner - 16, 6),
        "ComposerFillLabelGap": (MARGIN + 8, composer_y + 26, inner - 16, 2),
        "ComposerFillHintGap": (MARGIN + 8, composer_y + 46, inner - 16, 2),
        "ComposerFillInputGap": (MARGIN + 8, composer_y + 132, inner - 16, 8),
        "ComposerFillActionGap": (MARGIN + 8, composer_y + 172, inner - 16, 6),
        "ComposerFillBottom": (MARGIN + 8, composer_y + 196, inner - 16, 4),
        "ComposerFillActions": (MARGIN + 8 + BUTTON_WIDTH, button_y,
                                inner - 16 - 2 * BUTTON_WIDTH, BUTTON_HEIGHT),
        "ComposerLabel": (MARGIN + 8, composer_y + 8, inner - 16, 18),
        "InputHint": (MARGIN + 8, composer_y + 28, inner - 16, 18),
        "Input": (MARGIN + 8, input_y, inner - 16, INPUT_HEIGHT),
        "ComposerHelp": (MARGIN + 8, composer_y + 178, inner - 16, 18),
        "NewChatBackplate": (MARGIN + 8, button_y, BUTTON_WIDTH, BUTTON_HEIGHT),
        "SendBackplate": (width - MARGIN - 8 - BUTTON_WIDTH, button_y, BUTTON_WIDTH, BUTTON_HEIGHT),
        "NewChat": (MARGIN + 8, button_y, BUTTON_WIDTH, BUTTON_HEIGHT),
        "Send": (width - MARGIN - 8 - BUTTON_WIDTH, button_y, BUTTON_WIDTH, BUTTON_HEIGHT),
    }


class _OnMainThread(unohelper.Base, XCallback):
    def __init__(self, fn: Callable[[], None]) -> None:
        self._fn = fn

    def notify(self, _data: Any) -> None:
        self._fn()


class _NewChatListener(unohelper.Base, XActionListener):
    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def actionPerformed(self, _event: Any) -> None:
        self._panel.new_chat()

    def disposing(self, _event: Any) -> None:
        return None


class _ThemeListener(unohelper.Base, XActionListener):
    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def actionPerformed(self, _event: Any) -> None:
        self._panel.change_theme("light" if self._panel._theme == "dark" else "dark")

    def disposing(self, _event: Any) -> None:
        return None


class ChatPanel(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel, XActionListener, XWindowListener, XKeyListener, XTextListener):
    def __init__(self, ctx: Any, frame: Any, parent: Any, url: str,
                 responder: Callable[[str, str, Any], str] | None = None) -> None:
        self.ctx = ctx
        self.Frame = frame
        self.ResourceURL = url
        self.Type = TOOLPANEL
        self._responder = responder or self._ask_endpoint
        self._busy = False
        self._disposed = False
        self._status_role = "muted"
        self._session_id = new_session_id()
        self._generation = 0
        self._health_generation = 0
        self._health_inflight = False
        self._health_pending = False
        self._connectivity = "Not checked"
        self._theme_notice = ""
        self._scrollbars: dict[str, str] = {}
        self._width_hint: int | None = None
        manager = ctx.getServiceManager()
        paths = manager.createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
        self._theme_folder = Path(uno.fileUrlToSystemPath(paths.substituteVariables("$(user)/hermes-bridge", True)))
        self._theme = panel_registry.read_theme(self._theme_folder)
        self._async = manager.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
        provider = manager.createInstanceWithContext("com.sun.star.awt.ContainerWindowProvider", ctx)
        self.Window = provider.createContainerWindow(DIALOG_URL, "", parent, None)
        self._parent = parent
        self._transcript = self.Window.getControl("Transcript")
        self._status = self.Window.getControl("Status")
        self._input = self.Window.getControl("Input")
        self._send = self.Window.getControl("Send")
        self._new_chat = self.Window.getControl("NewChat")
        self._send.addActionListener(self)
        self._new_chat.addActionListener(_NewChatListener(self))
        self.Window.getControl("ThemeToggle").addActionListener(_ThemeListener(self))
        self._input.addKeyListener(self)
        self._input.addTextListener(self)
        self._configure_scrollbars()
        self._style_controls()
        parent.addWindowListener(self)
        self._lines: list[str] = []
        _, self._empty_transcript = application_copy(self._document())
        self.Window.getControl("Title").setText(display_title(self._document()))
        # Keep the byline short enough not to clip beside the brand mark.
        # App-specific guidance remains in the full, selectable empty transcript.
        self.Window.getControl("Subtitle").setText("by Atlas Omnia")
        self._transcript.setText(self._empty_transcript)
        self.Window.getControl("Connectivity").setText(self._connectivity)
        self.Window.getControl("InputHint").setText(INPUT_HINT)
        self._update_hint()
        self.Window.getControl("ThemeNotice").setVisible(False)
        self._set_status(READY_STATUS)
        self._layout()
        self.Window.setVisible(True)
        panel_registry.register(self)
        self._start_health_probe()

    def _configure_scrollbars(self) -> None:
        """Request documented optional AutoVScroll, with an honest native fallback.

        The XDL keeps VScroll enabled until support is established. 'auto-requested'
        records the model request, NOT proof that a particular native peer honours it.
        """
        for name in ("Input", "Transcript"):
            model = self.Window.getControl(name).getModel()
            mode = "native-always"
            if model.getPropertySetInfo().hasPropertyByName("AutoVScroll"):
                try:
                    model.setPropertyValue("AutoVScroll", True)
                    model.setPropertyValue("VScroll", False)
                    mode = "auto-requested"
                except Exception as exc:  # optional peer capability, not core construction
                    model.setPropertyValue("VScroll", True)
                    _log(self.ctx, f"{name}: AutoVScroll rejected ({type(exc).__name__}); native scrollbar retained")
            else:
                _log(self.ctx, f"{name}: AutoVScroll unsupported; native scrollbar retained")
            self._scrollbars[name] = mode

    def _update_hint(self) -> None:
        if not self._disposed:
            # Keep the raised background tile painted even when its hint text is empty.
            hint = self.Window.getControl("InputHint")
            hint.setText(INPUT_HINT if self._input.getText() == "" else "")
            hint.setVisible(True)

    def textChanged(self, _event: Any) -> None:
        self._update_hint()

    def _start_health_probe(self, *, after_failure: bool = False) -> None:
        """One single-flight check on open/failed reply; never a timer or chat retry.

        A failure during a check defers one fresh check until that worker finishes;
        this is an event-triggered follow-up, not polling/retry. Never overlap probes.
        New chat/disposal invalidate results and any deferred check.
        """
        if self._disposed:
            return
        if self._health_inflight:
            self._health_pending = self._health_pending or after_failure
            return
        self._health_inflight = True
        self._health_generation += 1
        generation, chat_generation = self._health_generation, self._generation
        token_path, port = self._theme_folder / "token", chat_port()

        def check() -> None:
            try:
                ready = probe_health(port, read_pairing_token(token_path))
            except Exception:  # failure is connectivity-only; never disclose token/exception text
                ready = False

            def show() -> None:
                if self._disposed:
                    return
                self._health_inflight = False
                if self._health_pending:
                    self._health_pending = False
                    self._start_health_probe()
                    return  # superseded by the post-failure check
                if generation != self._health_generation or chat_generation != self._generation:
                    return
                self._connectivity = "Local bridge ready" if ready else "Bridge unavailable"
                self.Window.getControl("Connectivity").setText(self._connectivity)
                # Never use _set_status here: error/busy belong to the chat, not health.
            self._async.addCallback(_OnMainThread(show), None)

        threading.Thread(target=check, name="SidecarSidebarHealth", daemon=True).start()

    def _style_controls(self) -> None:
        """Set supported UNO model properties; XDL also sets them before peer creation.

        Aqua can ignore edit borders or button fills. Fixed-text backplates provide
        real contrasting bounds; buttons retain native actions over light backplates.
        A rejected property is a construction error logged by PanelFactory, never
        silently accepted as successful styling.
        """
        palette = PALETTES[self._theme]
        ground, surface, raised, text, muted, primary = (palette[key] for key in
                                                       ("ground", "surface", "raised", "text", "muted", "primary"))
        self.Window.getModel().setPropertyValue("BackgroundColor", ground)
        styles = {
            "Title": (ground, text, 12, 150.0),
            "Subtitle": (ground, muted, 10, 100.0),
            "Connectivity": (ground, muted, 10, 100.0),
            "ComposerLabel": (raised, text, 10, 150.0),
            "ComposerHelp": (raised, muted, 10, 100.0),
            "InputHint": (raised, muted, 10, 100.0),
            "Status": (ground, palette[self._status_role], 10, 100.0),
            "ThemeNotice": (ground, palette["error"], 10, 100.0),
            "ThemeToggle": (palette["secondary"], 0x0C1117, 10, 100.0),
            "Transcript": (surface, text, 11, 100.0),
            "Input": (raised, text, 11, 100.0),
            "Send": (primary, palette["on_primary"], 11, 150.0),
            "NewChat": (palette["secondary"], 0x0C1117, 11, 100.0),
        }
        for name, (background, foreground, size, weight) in styles.items():
            model = self.Window.getControl(name).getModel()
            model.setPropertyValue("BackgroundColor", background)
            model.setPropertyValue("TextColor", foreground)
            font = model.getPropertyValue("FontDescriptor")
            # Retain the native UI font name instead of assuming a web font exists.
            font.Height, font.Weight = size, weight
            model.setPropertyValue("FontDescriptor", font)
        for name in ("TranscriptBackplate", "ComposerBorderTop", "ComposerBorderBottom",
                     "ComposerBorderLeft", "ComposerBorderRight"):
            self.Window.getControl(name).getModel().setPropertyValue("BackgroundColor", palette["border"])
        for name in plan_layout(MINIMAL_WIDTH, MINIMUM_HEIGHT):
            if name.startswith("ComposerFill"):
                self.Window.getControl(name).getModel().setPropertyValue("BackgroundColor", raised)
        for name, color in (("SendBackplate", primary), ("NewChatBackplate", palette["secondary"]),
                            ("ThemeBackplate", palette["secondary"]),
                            ("BrandMark", ground)):
            self.Window.getControl(name).getModel().setPropertyValue("BackgroundColor", color)
        for control, help_text in ((self._transcript, "Conversation — selectable reply history"),
                                   (self._input, "Your message. " + COMPOSER_HELP)):
            control.getModel().setPropertyValue("HelpText", help_text)
        self.Window.getControl("ComposerHelp").setText(COMPOSER_HELP)
        target = "Light" if self._theme == "dark" else "Dark"
        self.Window.getControl("ThemeToggle").getModel().setPropertyValue(
            "Label", "Theme: " + self._theme.title())
        self.Window.getControl("ThemeToggle").getModel().setPropertyValue(
            "HelpText", "Switch to " + target + ". Light/Dark only; applies to open Sidecar panels in this LibreOffice profile.")
        self.Window.getControl("Connectivity").getModel().setPropertyValue(
            "HelpText", "Authenticated local bridge check only; does not check model readiness. No automatic polling.")

    def _apply_theme(self, theme: str, notice: str = "") -> None:
        if self._disposed:
            return
        self._theme = theme
        self._style_controls()
        self._theme_notice = notice
        self.Window.getControl("ThemeNotice").setText(notice)
        self.Window.getControl("ThemeNotice").setVisible(bool(notice))
        self._layout()

    def change_theme(self, theme: str) -> None:
        """Native action, main thread only; never submits or mutates conversation state."""
        if self._disposed:
            return
        if theme not in PALETTES:
            raise ValueError("Invalid theme")
        notice = ""
        try:
            panel_registry.write_theme(self._theme_folder, theme)
        except OSError:
            notice = "Theme not saved; switch again to retry."
        panel_registry.apply_theme(self._theme_folder, theme, notice)

    def _set_status(self, text: str, role: str = "muted") -> None:
        previous_role = self._status_role
        self._status_role = role
        self._status.getModel().setPropertyValue("TextColor", PALETTES[self._theme][role])
        self._status.setText(text)
        if previous_role != role:
            self._layout()

    def submit(self, text: str) -> None:
        """Put ``text`` in the input box and send it, as the Send button does (main thread only)."""
        self._input.setText(text)
        self._update_hint()
        self.actionPerformed(None)

    def snapshot(self) -> dict[str, Any]:
        return {"lines": list(self._lines), "busy": self._busy, "status": str(self._status.getText()),
                "theme": self._theme, "connectivity": self._connectivity,
                "scrollbars": dict(self._scrollbars),
                "send_enabled": not self._busy, "session_id": self._session_id,
                "layout": {"requested_width": self._width_hint,
                           "parent_width": self._parent.getPosSize().Width,
                           "parent_height": self._parent.getPosSize().Height}}

    def new_chat(self) -> None:
        """Start a fresh Hermes session and clear the transcript. A reply still on its way is dropped."""
        self._generation += 1
        self._health_pending = False
        self._session_id = new_session_id()
        self._lines = []
        self._transcript.setText(self._empty_transcript)
        # New chat fences the reply, but cannot cancel a request already executing.
        # Keep the one-request guard until its main-thread completion arrives.
        if self._busy:
            self._set_status("Previous request still running…", "running")
        else:
            self._set_busy(False)

    def _document(self) -> Any:
        """Identity of the document this panel's window shows, as the bridge names it (main thread)."""
        try:
            model = self.Frame.getController().getModel()
            identify = getattr(panel_registry, "identify", None)
            return identify(model) if identify and model is not None else None
        except Exception:
            return None

    def _ask_endpoint(self, message: str, session_id: str, document: Any) -> str:
        paths = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.util.PathSubstitution", self.ctx)
        folder = uno.fileUrlToSystemPath(paths.substituteVariables("$(user)/hermes-bridge", True))
        return ask_hermes(chat_port(), read_pairing_token(Path(folder) / "token"), message, session_id, document)

    # XKeyListener: Enter sends, Shift+Enter adds a line
    def keyPressed(self, _event: Any) -> None:
        return None

    def keyReleased(self, event: Any) -> None:
        if getattr(event, "KeyCode", 0) == KEY_RETURN and not getattr(event, "Modifiers", 0) & MODIFIER_SHIFT:
            self.actionPerformed(None)

    # XUIElement / XToolPanel / XSidebarPanel
    def getRealInterface(self) -> Any:
        return self

    def createAccessible(self, _parent: Any) -> Any:
        return self.Window

    def getHeightForWidth(self, width: int) -> Any:
        # The sidebar allocation is authoritative. The embedded dialog can keep
        # its previous wider peer size while the host is narrowing the deck.
        if width > 0 and width != self._width_hint:
            self._width_hint = width
            self._async.addCallback(_OnMainThread(self._layout), None)
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        size.Minimum, size.Maximum, size.Preferred = MINIMUM_HEIGHT, -1, 600
        return size

    def getMinimalWidth(self) -> int:
        return MINIMAL_WIDTH

    # layout
    def _layout(self, width: int | None = None, height: int | None = None) -> None:
        area = self._parent.getPosSize()
        allocated_width = (self._width_hint if self._width_hint is not None else
                           width if isinstance(width, int) and width > 0 else int(area.Width))
        allocated_height = height if isinstance(height, int) and height > 0 else int(area.Height)
        self.Window.setPosSize(0, 0, max(allocated_width, MINIMAL_WIDTH),
                               max(allocated_height, MINIMUM_HEIGHT), POSSIZE)
        boxes = plan_layout(allocated_width, allocated_height, notice=bool(self._theme_notice),
                            status_height=STATUS_HEIGHT if self._status_role == "error" else COMPACT_STATUS_HEIGHT)
        for name, (x, y, w, h) in boxes.items():
            if name == "ThemeNotice" and not self._theme_notice:
                continue  # hidden controls reserve no layout space, nor a zero-height native peer
            self.Window.getControl(name).setPosSize(x, y, w, h, POSSIZE)

    def windowResized(self, event: Any) -> None:
        # Aqua can deliver the new allocation before getPosSize reflects it.
        self._layout(getattr(event, "Width", None), getattr(event, "Height", None))

    def windowMoved(self, _event: Any) -> None:
        return None

    def windowShown(self, _event: Any) -> None:
        self._layout()

    def windowHidden(self, _event: Any) -> None:
        return None

    def disposing(self, _event: Any) -> None:
        self._disposed = True
        self._health_generation += 1
        self._health_pending = False
        panel_registry.unregister(self)

    # chat
    def _append(self, who: str, text: str) -> None:
        self._lines.append(f"{who}: {text}")
        self._transcript.setText("\n\n".join(self._lines))
        end = len(self._transcript.getText())
        selection = uno.createUnoStruct("com.sun.star.awt.Selection")
        selection.Min, selection.Max = end, end
        self._transcript.setSelection(selection)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send.setEnable(not busy)
        self._set_status("Hermes is working…" if busy else READY_STATUS,
                         "running" if busy else "muted")

    def actionPerformed(self, _event: Any) -> None:
        message = self._input.getText().strip()
        if not message or self._busy:
            return
        if len(message) > MAX_MESSAGE:
            self._set_status(f"Message is too long (limit {MAX_MESSAGE} characters).", "error")
            return
        self._input.setText("")
        self._update_hint()
        self._append("You", message)
        self._set_busy(True)
        work = (message, self._session_id, self._document(), self._generation)
        threading.Thread(target=self._ask, args=work, name="SidecarSidebarChat", daemon=True).start()

    def _ask(self, message: str, session_id: str, document: Any, generation: int) -> None:
        try:
            who, reply = "Hermes", self._responder(message, session_id, document)
        except ChatUnavailable as exc:
            who, reply = "Request error", str(exc)
        except Exception as exc:  # shown to the user; the panel must stay usable
            who, reply = "Request error", (
                f"The panel failed while asking Hermes ({type(exc).__name__}). "
                "Check the document before sending again; the outcome may be unknown.")

        def show() -> None:
            if self._disposed:
                return
            if generation != self._generation:
                self._set_busy(False)
                return  # New chat was pressed while this reply was on its way
            self._append(who, reply)
            self._set_busy(False)
            if who == "Hermes":
                # A successful authenticated chat is newer bridge evidence than
                # any outstanding health probe. Fence its result but keep the
                # single-flight slot owned until that worker really completes.
                self._health_generation += 1
                self._health_pending = False
                self._connectivity = "Local bridge ready"
                self.Window.getControl("Connectivity").setText(self._connectivity)
            else:
                self._set_status("Reply failed — check conversation and document.", "error")
                self._start_health_probe(after_failure=True)
        self._async.addCallback(_OnMainThread(show), None)


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
            return ChatPanel(self.ctx, values.get("Frame"), values.get("ParentWindow"), url)
        except Exception as exc:
            import traceback
            _log(self.ctx, f"panel creation failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            raise


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PanelFactory, FACTORY_IMPLEMENTATION, ("com.sun.star.ui.UIElementFactory",))
