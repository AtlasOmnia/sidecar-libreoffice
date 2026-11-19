# Sidecar for LibreOffice: next steps

The plugin is installed. It needs its companion **LibreOffice extension**, which Hermes does not install.

1. **Restart Hermes** so the plugin loads: `hermes gateway restart`, or reopen Hermes Desktop. Upgrading from a plugin build before 0.4.0 (`libreoffice-live`)? Remove it (`hermes plugins remove libreoffice-live`) and restart the gateway once: its chat listener keeps port 8661 until the gateway restarts.
2. **Build the extension from the release tag** (Python 3.9+; Windows: use `py` and `scripts\build_oxt.py`):

   ```
   git clone --depth 1 --branch v0.4.0 https://github.com/AtlasOmnia/sidecar-libreoffice.git
   cd sidecar-libreoffice
   python3 scripts/build_oxt.py --check
   ```

   `--check` prints the SHA-256 and compares it with the one in the README.

3. **Install it in LibreOffice:** Tools → Extensions… → Add… → `dist/sidecar-libreoffice.oxt`, then Close and Restart Now (or Restart Later, then quit and reopen LibreOffice). Remove any earlier Sidecar or Hermes for LibreOffice extension in the same dialog: LibreOffice installs the new one alongside it and both would use port 8660. Two identical "Sidecar for LibreOffice 0.4.0" rows are told apart by the description: this release's ends with the Apache License line. If the sidebar then shows the Properties deck, click the green S tab once.
4. **Pair:** in LibreOffice choose Tools → Copy Sidecar pairing token, then run
   `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN "$(pbpaste)"` (macOS) or
   `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN (Get-Clipboard)` (Windows PowerShell),
   or paste it into the plugin's bridge_token field in Hermes Desktop.
5. **Try it:** `hermes chat -t sidecar-libreoffice`, then ask which LibreOffice documents are open.

**The sidebar chat is off by default.** Until you turn it on, the panel shows **Bridge unavailable** and answers a message with "Hermes is not answering on 127.0.0.1:8661"; that is expected. It is unsupported with an external-agent model runtime (for example `codex_app_server`): keep it off in such a profile. To turn it on in a profile with a Hermes-managed model runtime, enable the Hermes API server with an `API_SERVER_KEY`, then run

    hermes config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661

and restart the gateway. The full steps, the runtime requirement, the dedicated-profile option and the risk disclosures (loopback listeners, what a panel chat can do) are in this plugin's README.md and at https://github.com/AtlasOmnia/sidecar-libreoffice/tree/v0.4.0#readme
