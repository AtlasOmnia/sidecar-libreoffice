# Sidecar for LibreOffice: next steps

The plugin needs its companion **LibreOffice extension**, which Hermes does not install.

1. **Restart Hermes** so the plugin loads: `hermes gateway restart`, or reopen Hermes Desktop. Upgrading from a plugin build before 0.4.0 (`libreoffice-live`)? Remove it (`hermes plugins remove libreoffice-live`) and restart the gateway once: its chat listener keeps port 8661 until the gateway restarts.
2. **Build the extension from the release tag** (Python 3.9+; Windows: use `py` and `scripts\build_oxt.py`):

   ```
   git clone --depth 1 --branch v0.5.1 https://github.com/AtlasOmnia/sidecar-libreoffice.git
   cd sidecar-libreoffice
   python3 scripts/build_oxt.py --check
   ```

   `--check` prints the SHA-256 and compares it with the published 0.5.1 archive hash in the README. The [v0.5.1 release](https://github.com/AtlasOmnia/sidecar-libreoffice/releases/tag/v0.5.1) also supplies the `.oxt` and `SHA256SUMS`. The 0.5.1 plugin catalog update is pending review in NousResearch/hermes-agent#158979; the merged catalog listing is still 0.4.1.

3. **Install it in LibreOffice:** Tools → Extensions… → Add… → `dist/sidecar-libreoffice.oxt`, then Close and Restart Now (or Restart Later, then quit and reopen LibreOffice). Upgrading from 0.4.0 or 0.5.0: remove nothing; the 0.5.1 extension has the same identifier and replaces it, and your pairing and chat settings stay. Installed 0.5.0→0.5.1 replacement passed in a disposable profile; the public `hermes plugins update` command remains untested. Older than 0.4.0: remove the earlier Sidecar or Hermes for LibreOffice extension in the same dialog, because LibreOffice installs the new one alongside it and both would use port 8660. If the sidebar then shows the Properties deck, click the green S tab once.
4. **Pair:** in LibreOffice choose Tools → Copy Sidecar pairing token, then run
   `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN "$(pbpaste)"` (macOS) or
   `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN (Get-Clipboard)` (Windows PowerShell),
   or paste it into the plugin's bridge_token field in Hermes Desktop.
5. **Try it:** `hermes chat -t sidecar-libreoffice`, then ask which LibreOffice documents are open.

**The sidebar chat is off by default.** Until you turn it on, the Sidecar panel shows "Chat is off in Hermes." and starts no browser; that is expected. It is unsupported with an external-agent model runtime (for example `codex_app_server`): keep it off in such a profile. To turn it on in a profile with a Hermes-managed model runtime, enable the Hermes API server with an `API_SERVER_KEY`, then run

    hermes config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661

and restart the gateway. **Follow LibreOffice** is a separate macOS app-window option, off by default and remembered in Settings. Moving or resizing Sidecar by hand undocks it; Reattach resumes following at 400 points wide. It does not minimise with LibreOffice or follow across Spaces. Second-display and Retina placement are untested; if another-display pause appears, move Sidecar there yourself, then Reattach. Windows stays a default-browser tab with no following.

**Open Sidecar** in the Sidecar panel then opens the Sidecar window beside LibreOffice: an app window on macOS when Chrome, Edge, Brave or Chromium is installed, otherwise (and always on Windows in 0.5.1) a normal tab in your default browser, where your browser extensions can read the page. The full steps, the runtime requirement, the dedicated-profile option and the risk disclosures (loopback listeners and the Sidecar web page, the browser the extension starts, what a Sidecar chat can do) are in this plugin's README.md and at https://github.com/AtlasOmnia/sidecar-libreoffice/tree/v0.5.1#readme
