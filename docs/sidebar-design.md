# In-app chat sidebar: design and status

Goal: chat with Hermes from inside LibreOffice, in a sidebar deck next to the document, the way
Sidecar does in Microsoft Office.

## Status (first version, 0.3.0)

Historical note: this page describes the first sidebar (0.3.0); its *Not verified* and *Remaining work* lists are as of 0.3.0. In 0.5.0 the native chat panel described here is replaced: the sidebar panel becomes a launcher with an **Open Sidecar** button, and the conversation moves to the Sidecar window, a web page served by the plugin and shown in a browser (README, *Sidebar chat* and *Which browser*; the protocol is `docs/web-protocol.md`). The decisions below on credentials and the tool fence still hold; *One reply per message* holds for 0.5.0, while *Plain text* does not: the Sidecar window renders Markdown.

Current 0.5.1 draft: optional macOS following is off by default and remembered in Settings; hand move/resize undocks and Reattach restores 400 points wide. Windows keeps a default-browser tab. See [following](follow-libreoffice.md) and the [release evidence ledger](release-0.5.1.md); the historical design below is not current-candidate verification.

Built:

- A **Hermes** deck in the sidebar of Calc, Writer and Impress (`Sidebar.xcu`, `Factories.xcu`; deck
  `SidecarDeck` and panel `SidecarChatPanel` since 0.4.0),
  opened from the sidebar tab, **Tools → Open Hermes chat**, or the bridge operation
  `show_chat_panel`.
- A **Chat** panel (`python/hermes_sidebar.py`, `dialogs/SidecarChatPanel.xdl`): transcript,
  status line, input, Send and New chat. Enter sends, Shift+Enter adds a line. Each request runs
  on a worker thread and the reply is written on LibreOffice's main thread.
- The **chat endpoint** in the plugin (`plugin/chat_bridge.py`), started only in the Hermes
  gateway process. The panel posts `{message, session_id, document}` with the pairing token; the
  endpoint forwards to the Hermes API server (`/v1/chat/completions`, `X-Hermes-Session-Id` for
  continuity, `API_SERVER_KEY` from Hermes's secret store) and returns the reply. Since 0.4.0 it
  forwards through the Runs API instead (`POST /v1/runs` with the panel's `session_id`, then
  `GET /v1/runs/{id}`), so an unload can stop the runs it started; see `SECURITY.md`.
- The **tool fence**: a `pre_tool_call` hook that, in sessions whose id has the panel's shape
  (`lo-` and 32 hex digits), refuses every tool except this plugin's and the read-only skill and
  tool-lookup helpers.

Run live on LibreOffice 26.8.2.2 against a throwaway Hermes Agent 0.23.0 gateway and a real
model, from the panel:

| Message | Result |
| --- | --- |
| "What is in this spreadsheet?" | Read the sheet with the tools and described it (27 s) |
| "Add a Total row ... with a formula, and make that row bold." | A6:B6 written, `=SUM(B2:B5)` = 446, bold; read back from the document |
| "Undo that." (next message, same chat) | Row 6 empty again: the conversation carried over and undo worked |
| "Run the shell command ..." | Refused by the fence; the file was not created |

Not verified:

- Typing and clicking in the panel by hand. Messages were entered through the development-only
  bridge operation `dev_chat_panel` (present only when LibreOffice starts with `HERMES_LO_DEV=1`),
  because automated typing needs the window brought to the front. Enter-to-send, the New chat
  button and the menu item are covered by unit tests only.
- A long-running reply, a gateway restart in the middle of a chat, two panels at once, Writer
  and Impress conversations, and any platform other than macOS.
- Hermes Agent 0.22.5.

## Decisions

- **No Hermes credential in LibreOffice.** The panel authenticates with the pairing token; the
  API server key stays inside Hermes.
- **Panel chats are fenced to document tools.** The pairing token was designed to guard document
  edits. Letting it start an unrestricted agent turn would have turned "can read the token file"
  into "can run commands through Hermes".
- **One reply per message, no streaming.** Simple and robust; the status line shows that Hermes
  is working. Streaming is future work.
- **Plain text.** LibreOffice sidebar panels are native toolkit windows with no embedded browser,
  so Markdown is not rendered.

## Remaining work

1. Hand-typed acceptance on a real desktop session, including Enter-to-send and the menu item.
2. Streamed replies and a Stop button; today a slow turn only shows "Hermes is working…".
3. A setting in LibreOffice for the chat port (today: `HERMES_LO_CHAT_PORT` or the default 8661).
4. Clearer first-run guidance in the panel when the API server is off or Hermes is not paired.
5. Two profiles in one gateway: the first to bind a given chat port gets the endpoint.
