<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Atlas Omnia -->
# Sidecar web window: protocol contract (0.5.0)

This is the frozen contract between the four parts of the 0.5.0 Sidecar window:

- the **plugin** (the chat endpoint in `plugin/chat_bridge.py` and the modules it loads),
- the **launcher** (the LibreOffice extension: `sidecar_launcher.py` and the per-OS adapters),
- the **page** (`plugin/sidebar/`, served by the plugin and shown in a browser), and
- the **verification stub** that imitates the plugin for page work.

Every byte format below is normative. A change after the freeze is a new revision of this file,
agreed by the plugin and launcher owners, with the test vectors updated in the same commit.
Labels such as R18, D1 or S1 are the requirement, owner-decision and spike numbers of the 0.5.0
design review; the spike results that fixed a detail are quoted where they apply.

Words: *client rectangle* is a window's content area without its title bar. *Screen points* are
the coordinates LibreOffice's `XWindow.getPosSize()` returns on a top-level window: origin at the
top-left of the main display, y downwards, in points (not device pixels).

## 1. Listener and origins

- The plugin listens on `127.0.0.1:<chat_port>` only (0 means off: no listener at all).
- `ORIGIN` is `http://127.0.0.1:<chat_port>`. `HOST` is `127.0.0.1:<chat_port>`.
- Every response closes its connection (`Connection: close`). No route sets or reads a cookie;
  no response ever carries `Set-Cookie`.
- Every response names a fixed log label (section 11); logs never carry paths, ids, titles,
  messages, tokens, codes or keys.

### 1.1 Route table

| Method | Path | Caller | Credential | Label |
| --- | --- | --- | --- | --- |
| GET | `/health` | 0.4.0 extension | pairing token (bearer) | `health` |
| POST | `/chat` | 0.4.0 extension | pairing token (bearer) | `chat` |
| POST | `/launcher/hello` | 0.5.0 launcher | none (it opens a channel) | `launcher-hello` |
| POST | `/launcher/launch` | 0.5.0 launcher | channel MAC | `launcher-launch` |
| GET | `/launcher/launch/<launch_id>` | 0.5.0 launcher | channel MAC | `launcher-status` |
| POST | `/launcher/release` | 0.5.0 launcher | channel MAC | `launcher-release` |
| GET | `/ui/<file>` | browser | none (inert files) | `ui-static` |
| POST | `/api/session` | page | launch code in the body | `api-session` |
| GET | `/api/state` | page | session header | `api-state` |
| POST | `/api/chat` | page | session header | `api-chat` |
| GET | `/api/turn?id=…&seq=…&have=…` | page | session header | `api-turn` |
| POST | `/api/turn/stop` | page | session header | `api-stop` |
| POST | `/api/new-chat` | page | session header | `api-new-chat` |
| POST | `/api/session/close` | page | session header | `api-close` |

`/health` and `/chat` are kept unchanged through 0.5.x for 0.4.0 extensions and are deprecated
for removal in 0.6.0 (owner decision D2). Any other path answers 404; any other method on a
listed path answers 405 (section 10). `OPTIONS` and `HEAD` are never answered with CORS headers.

### 1.2 Request gates, in this order

1. **Host.** Exactly one `Host` header, equal to `HOST`. Otherwise 403 `bad_host`.
2. **Role separation.** Each route family accepts only its own credential headers and refuses
   a request that carries a foreign one with 403 `forbidden_credential`:

   | Family | Must not carry |
   | --- | --- |
   | `/health`, `/chat` | `Origin`, any `Sec-Fetch-*`, `X-Sidecar-Session`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |
   | `/launcher/*` | `Origin`, any `Sec-Fetch-*`, `X-Sidecar-Session`, `X-Hermes-Plugin-Key` |
   | `/ui/*` | `X-Hermes-Plugin-Key`, `X-Sidecar-Session`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |
   | `/api/*` | `X-Hermes-Plugin-Key`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |

   (The legacy pair keeps its existing `Origin` refusal: 403 "requests with an Origin are refused".)
3. **Fetch metadata on `/api/*`.** If `Sec-Fetch-Site` is present it must be `same-origin`
   (else 403 `cross_site`). On a POST, exactly one `Origin` equal to `ORIGIN` is required (else
   403 `bad_origin`). On a GET, an `Origin` if present must equal `ORIGIN`; if `Origin` is absent,
   `Sec-Fetch-Site: same-origin` must be present (else 403 `bad_origin`). S1 observed that both
   Chrome 158 and Safari 27 send `Sec-Fetch-Site: same-origin` and no `Origin` on same-origin
   `fetch` GETs, and both `Origin` and `Sec-Fetch-Site` on POSTs.
4. **Body.** Every POST needs `Content-Type: application/json` (charset parameter allowed; else
   415), a `Content-Length` (else 411), at most 16 KiB (64 KiB on `/api/chat` and `/chat`; else
   413), arriving whole within the request's single read deadline (else 408 / 400). The body must
   be a JSON object with exactly the keys listed for the route (else 400 `bad_request`). GET
   requests carry no body.
5. **Credential** (session header, channel MAC, bearer token), per route family.

Duplicate `Host`, `Origin`, `X-Sidecar-Session`, `X-Sidecar-Channel`, `X-Sidecar-Counter` or
`X-Sidecar-MAC` headers are refused as if the value were wrong.

## 2. Encodings and identifiers

- `b64u(bytes)`: base64url (RFC 4648 section 5) without `=` padding.
- **Launch code**: `b64u(32 random bytes)`, 43 characters, `^[A-Za-z0-9_-]{43}$`.
- **Session token**: `b64u(32 random bytes)`, 43 characters, same pattern.
- **Launch id**: `b64u(16 random bytes)`, 22 characters, `^[A-Za-z0-9_-]{22}$`.
- **Turn id**: `b64u(16 random bytes)`, 22 characters, same pattern. Minted by the plugin; the
  browser never sees a Hermes run id or a chat session id (`lo-<32 hex>`).
- **Nonces** (`client_nonce`, `server_nonce`): `b64u(32 random bytes)`, 43 characters.
- The plugin stores launch codes and session tokens only as SHA-256 digests and compares them in
  constant time.
- **Document**: `{"document_id": str 1..512, "title": str 0..1000, "type": "calc"|"writer"|"impress"}`
  (the same shape `/chat` accepts today).
- **Rectangle**: `{"x": int, "y": int, "width": int, "height": int}`, screen points;
  `-32768 <= x, y <= 32767`, `240 <= width, height <= 8192`. JSON booleans are not integers.

## 3. Launcher channel (R18): the launcher never sends the pairing token

Key: `K` = the pairing token's UTF-8 bytes, exactly as the token file holds it with surrounding
whitespace removed (the same string 0.4.0 sends as `X-Hermes-Plugin-Key`). All MACs are
HMAC-SHA256. `PORT` is the listener port in decimal. Strings below are ASCII, joined with a
single LF (`\n`), with no trailing LF. `hex()` is lowercase hexadecimal.

### 3.1 Hello

Request: `POST /launcher/hello`, body `{"v": 1, "client_nonce": "<43>"}`.

The plugin mints `server_nonce`, computes

```
server_proof = b64u(HMAC(K, "sidecar-launcher/1 hello" \n PORT \n client_nonce \n server_nonce))
channel_key  =      HMAC(K, "sidecar-launcher/1 channel" \n PORT \n client_nonce \n server_nonce)   (32 raw bytes)
```

stores the channel `{server_nonce -> channel_key, next_counter = 1, expires = now + 120 s}` and
answers 200:

```json
{"ok": true, "v": 1, "server_nonce": "<43>", "server_proof": "<43>", "expires_in": 120,
 "chat": {"ready": true, "reason": null}}
```

The launcher recomputes `server_proof` and compares in constant time. A mismatch, a missing
field, or any status other than 200 means the program on that port is not this user's Hermes:
the launcher sends nothing more and shows the *not your Hermes* text (section 8.2).

Without a pairing token the plugin answers 503 `no_pairing`. A refused connection means chat is
off or the gateway is down (section 8.2). At most 16 live channels; the oldest is dropped for a
new hello. A server nonce opens exactly one channel and is never reissued.

### 3.2 Calls on a channel

Every later launcher request carries:

```
X-Sidecar-Channel: <server_nonce>
X-Sidecar-Counter: <n>              decimal, no leading zeros; 1 for the first call, then +1
X-Sidecar-MAC:     b64u(HMAC(channel_key,
                     "sidecar-launcher/1 request" \n METHOD \n PATH \n n \n hex(SHA256(body))))
```

`METHOD` is upper case; `PATH` is the request target exactly as sent (no query strings are used
on this family); `body` is the exact body bytes (empty for GET, so its hash is
`e3b0c442…b855`). The plugin accepts a call only for a live channel, with `n` equal to the
channel's `next_counter` and a matching MAC; anything else answers 401 `launcher_auth` and
**drops the channel**. A channel allows at most 64 calls and lives 120 s from its hello. The
launcher opens a new channel for each user action (one click; one release).

Every answer to an accepted call (any status) carries

```
X-Sidecar-MAC: b64u(HMAC(channel_key, "sidecar-launcher/1 response" \n n \n STATUS \n hex(SHA256(response body))))
```

and the launcher refuses an answer whose MAC is missing or wrong (treated as *not your Hermes*).
401 answers to refused calls carry no MAC.

### 3.3 Test vectors

Both sides must reproduce these exactly (unit tests in `tests/test_web_auth.py` and
`tests/test_launcher.py`):

```
token          example-pairing-token-0123456789abcdefghijklmnopq
PORT           8661
client_nonce   AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8      (bytes 0..31)
server_nonce   ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8      (bytes 32..63)
server_proof   6gAIEWKHsH5HilYgR4IFZrAzSBZsyxB9kUYyFBW3DZk
channel_key    49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1   (hex)

request 1: POST /launcher/launch, counter 1, body (no trailing newline)
  {"document":{"document_id":"lo:5f0c:1","title":"sales.ods","type":"calc"},"host_scheme":"light","mode":"app","bounds":{"x":1240,"y":102,"width":400,"height":848}}
  SHA256(body)  3220ea4ed994980c359a102c7b2d2d604bb604de9885a619bedc17373995bbba
  request MAC   PmCfNapRjDIVbyHqzSrGnkU_mqgxbEr4XLWu0iQocX8
response to request 1: status 201, body {"ok":true}
  SHA256(body)  4062edaf750fb8074e7e83e0c9028c94e32468a8b6f1614774328ef045150f93
  response MAC  ZgH8usVq9byBTZB7tp2Fi7PfEpJk8YBIw-EfdEN6FRc
request 2: GET /launcher/launch/AAAAAAAAAAAAAAAAAAAAAA, counter 2, empty body
  SHA256(body)  e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  request MAC   pz9hSDdfVxWMTMKc772QPcntSZE8IV0T3jHUv836CuI
```

## 4. Launcher routes

### 4.1 `POST /launcher/launch`

Body (exactly these keys):

```json
{"document": {"document_id": "…", "title": "…", "type": "calc"},
 "host_scheme": "light",
 "mode": "app",
 "bounds": {"x": 1240, "y": 102, "width": 400, "height": 848}}
```

- `host_scheme`: `"light"`, `"dark"` or `null` (LibreOffice's appearance; the page's *Auto*
  follows it).
- `mode`: `"app"` (a Chromium app window; `bounds` required) or `"tab"` (owner decision D1: no
  Chromium browser is installed, so the default browser opens a normal tab; `bounds` must be
  `null`).
- `bounds`: the **client rectangle the Sidecar window should have**, in screen points (section
  8.3 says how the launcher computes it and how the page applies it).

Answers:

- 201 `{"ok": true, "launch_id": "<22>", "url": "<ORIGIN>/ui/index.html#launch=<43>", "window_live": false, "expires_in": 30}`
- 200 `{"ok": true, "launch_id": null, "url": null, "window_live": true, "expires_in": 0}` when a
  session for this `document_id` is live (seen within 90 s, or holding a long-poll). No code is
  minted; the launcher raises the existing window (app mode) or shows the *already open* text.
- 429 `too_many_launches` (16 unexchanged codes outstanding) or `session_cap` (8 sessions, none
  evictable). 400 / 401 / 403 / 413 / 415 as in section 1.2.

The launcher accepts `url` only if it matches
`^http://127\.0\.0\.1:<PORT>/ui/index\.html#launch=[A-Za-z0-9_-]{43}$` for the port it called.

A code is valid for 30 s from minting, once. It is bound to the document, `host_scheme`, `mode`
and `bounds` given here. Unexchanged codes are purged at expiry.

### 4.2 `GET /launcher/launch/<launch_id>`

200 `{"ok": true, "state": "pending" | "exchanged" | "expired" | "double_use"}`; 404 `not_found`
for an unknown id. Records are kept 120 s from minting. The launcher polls every 500 ms on a
worker thread for up to 20 s (R11) and deletes the bootstrap file at the first state other than
`pending`, or after 30 s, whichever comes first.

### 4.3 `POST /launcher/release`

Body `{"document_id": "<id>", "reason": "document_closed"}` or
`{"document_id": null, "reason": "host_quit"}` (all documents). Ends every matching session with
reason `host_gone`, asks their driven runs to stop, answers 200 `{"ok": true, "released": <n>}`.

## 5. Bootstrap-file launch (R2, D1): the code is never on a command line

The same mechanism serves the app window and the D1 fallback tab, on every platform.

1. **Folder**: `<cache>/Sidecar for LibreOffice/launch/`, created mode 0700 and checked with the
   extension's `private_files` rules before every use. `<cache>` is `~/Library/Caches` on macOS
   and `%LOCALAPPDATA%` on Windows. The browser profile for app mode lives beside it in
   `<cache>/Sidecar for LibreOffice/browser/` (0700).
2. **File**: `launch-<32 lowercase hex>.html` (16 random bytes), created exclusively
   (`O_CREAT|O_EXCL`, no symlink follow) with mode 0600. Content, UTF-8, no trailing newline,
   with `<URL>` the validated `url` from 4.1 inserted verbatim (its characters are limited to
   `[A-Za-z0-9:/._#=-]`, so no escaping is needed):

   ```html
   <!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="0;url=<URL>"><title>Opening Sidecar…</title>
   ```

3. **App mode** (Chrome, Edge, Brave, Chromium): the browser's argument list carries
   `--app=<file URI of the bootstrap file>` (`Path.as_uri()`), never the launch URL, plus the
   frozen flag list `--user-data-dir=<cache>/Sidecar for LibreOffice/browser`, `--no-first-run`,
   `--no-default-browser-check`, `--disable-extensions`, `--disable-sync`,
   `--disable-background-networking`, `--disable-component-update`, `--disable-default-apps`, and on
   macOS only `--use-mock-keychain`, and `--window-position` / `--window-size` from section 8.3. No shell is involved; the environment drops `PYTHON*`, `DYLD_*`, `URE_*`, `SAL_*`.
4. **Tab mode** (D1 fallback, no Chromium browser): macOS runs `/usr/bin/open <bootstrap path>`
   (argument list, no shell); Windows calls `os.startfile(<bootstrap path>)`. The default handler
   for `.html` files opens it; if that is not a browser, the launch shows *did not connect*.
5. **Removal**: as in 4.2. On LibreOffice start and quit the launcher also deletes any
   `launch-*.html` in that folder older than 60 s.
6. **Windows**: this section replaces R2(b) (the URL on Edge's command line) unless spike S3 shows
   Edge cannot use a bootstrap file; only then does Windows app mode fall back to `--app=<url>`.

S1 (macOS 27.2.1, Chrome 158 and Safari 27, both started cold) observed for this exact file:

- the launch code appeared in **0 of 93** (Chrome) and **0 of 129** (Safari) continuous
  `ps -axww` samples taken from before the spawn until 3 s after the exchange;
- the redirected app window shows only a title bar: no location bar, no origin strip, no tabs;
  `matchMedia("(display-mode: standalone)")` is true; `window.locationbar.visible` is **true**
  even so (it cannot be used to detect app mode);
- the requested bounds were honoured exactly (`--window-size=420,820 --window-position=1700,120`
  gave a 420×820 window at 1700,120 by the window server's own list); the title bar is 32 points
  (`outerHeight - innerHeight`);
- the meta refresh **replaces** the history entry: `history.length` is 1 in both browsers, so
  `window.close()` works from the page in the app window and in the Safari tab;
- the bootstrap file was deleted right after the exchange and the page kept working;
- spawn to exchange took 0.6 s (Chrome) and 1.8 s (Safari); the 30 s code life is ample;
- the top-level navigation from the file arrives with `Sec-Fetch-Site: cross-site`,
  `Sec-Fetch-Mode: navigate` and no `Referer`;
- **Safari's global history keeps the full launch URL, fragment included**, as its own entry
  next to the replaced one (`replaceState` does not rewrite it). The code in it is spent; see the
  replay rule in 6.1.

## 6. Page routes (`/api/*`)

The session token travels only in `X-Sidecar-Session`. All answers: `Content-Type:
application/json`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`,
`Cross-Origin-Resource-Policy: same-origin`, `Referrer-Policy: no-referrer`.

Errors on new routes: `{"ok": false, "error": "<one English sentence for the user>", "code": "<machine code>"}`
(section 9.1); a 401 for a missing or ended session adds `"ended": <reason or null>`.

### 6.1 `POST /api/session` (exchange)

Body `{"launch": "<43>"}`; no session header. 200:

```json
{"ok": true, "session": "<43>", "idle_timeout": 43200,
 "document": {"document_id": "…", "title": "…", "type": "calc"},
 "host_scheme": "light", "mode": "app",
 "bounds": {"x": 1240, "y": 102, "width": 400, "height": 848},
 "chat": {"ready": true, "reason": null},
 "chat_label": "lo-1a2b3c4d",
 "endpoint": "http://127.0.0.1:8661",
 "limits": {"max_message": 8000}}
```

- `bounds` is `null` in tab mode. `chat_label` is display-only (the first 8 hex digits of the
  plugin's chat session id); the page never sends it back.
- A newer session for the same `document_id` ends the older one (reason `replaced`).
- Unknown, expired or purged code: 401 `launch_expired`, "This launch link was already used or
  has expired. Click Open Sidecar in LibreOffice again."
- **Second presentation of a used code while its record is still held (30 s from minting)**:
  401 `launch_double_use`, the session minted from it ends with reason `double_use`, its driven
  run is asked to stop, a warning is logged, and the launch state becomes `double_use`. After the
  record is purged a replay is just `launch_expired`, so reopening a spent link from browser
  history (S1: Safari keeps it) never disconnects a live window.

### 6.2 `GET /api/state`

200 `{"ok": true, "document": {…}, "chat": {"ready": bool, "reason": null | "plugin_inactive" | "no_api_key" | "bad_api_key"}, "chat_label": "…", "turn": <turn view without text> | null}`.

Idle windows poll it every 3 s while visible and at once on `focus` and `visibilitychange`;
they hold no other connection. A window with an active turn does not poll it.

### 6.3 `POST /api/chat`

Body `{"message": "<1..8000 characters, not only whitespace>"}`. The plugin composes the Hermes
turn with the session's document (`compose_message`), claims synchronously and starts one worker
(`ChatBridge.begin_turn`). 202 `{"ok": true, "turn": <turn view>}`. Refusals are synchronous:
409 `busy` ("this chat is still waiting for a reply"), 409 `unresolved` (the 0.4.0 text that
ends "Start a New chat to continue."), 429 `too_many`, 503 `plugin_inactive`, 503
`worker_failed`, 400 `message_invalid`. A new message clears the previous turn record.

### 6.4 `GET /api/turn?id=<turn>&seq=<n>&have=<n>` (cursor long-poll)

- Query: exactly the keys `id`, `seq`, `have`, each once; `seq` and `have` are decimal integers
  `0 <= n <= 200000`. `seq` is the last record version the page has; `have` is how many
  characters of the reply text it holds.
- The plugin answers **at once** when the record's `seq` is greater than the query's or the
  turn has ended; otherwise it parks the request for at most **25 s** and answers with the
  unchanged record.
- **One long-poll per turn**: a newer `/api/turn` for the same turn answers the older one at once
  with `"superseded": true`. The page re-issues its long-poll from the network callback, never
  from a timer (hidden windows throttle timers), and waits 1 s after a network error.
- Answer 200:

```json
{"ok": true, "superseded": false,
 "turn": {"id": "<22>", "seq": 7, "state": "working" | "stopping" | "done",
          "hermes_state": "queued" | "running" | "stopping" | "waiting_for_approval" | null,
          "text_from": 0, "text": "<reply characters from text_from on>", "text_length": 1234,
          "activity": [],
          "outcome": null | {"kind": "completed" | "user" | "uncertain" | "turned_off" | "error",
                             "message": "<sentence>" | null}}}
```

- `text` is the suffix `reply[have:]` (at most `MAX_REPLY` characters in total). In 0.5.0 (tier
  T1) the reply arrives whole when the turn completes; `activity` is always `[]` and is reserved
  for the T2 relay, which is out of 0.5.0.
- `outcome.kind` comes from `chat_bridge.TurnOutcome`; `message` is `null` for `completed`,
  `USER_STOPPED` for `user`, `OUTCOME_UNCERTAIN` for `uncertain`, `PLUGIN_TURNED_OFF` for
  `turned_off`, and the existing user-safe error text for `error`.
- The ended turn's record is kept until the next message, New chat, or the end of the session,
  so a long-poll cut off by a network error or by sleep still gets the reply when it is re-issued.
- 404 `unknown_turn` for an id that is not this session's current turn.
- **Window gone**: while a turn is driven, if no `/api/turn` request has been in flight or arrived
  for 90 s, the plugin asks for a stop (`request_stop`).
- Unload: parked long-polls are woken and answer once 503 `turned_off` with
  `"ended": "turned_off"`.

Turn view (used in 6.2 and 6.3) is the same `turn` object without `text` and `text_from`.

### 6.5 `POST /api/turn/stop`

Body `{"turn": "<22>"}`. Sets `stop_requested` only (`ChatBridge.request_stop`); the worker
driving the run sends the stop to Hermes and reports outcome `user`. 202 with the turn view (state
`stopping`), or 200 with the final view when the turn already ended. 404 `unknown_turn`.

### 6.6 `POST /api/new-chat`

Body `{}`. Refused with 409 `turn_running` ("Wait for the reply, or press Stop first.") while the
session's run is **driven** (`ChatBridge.session_state(...) == "driven"`). With an unresolved run
it is allowed: the session gets a new `lo-<32 hex>` id from the same generator, the old run stays
owned under the old id (it still counts against `MAX_ACTIVE`). 200 `{"ok": true, "chat_label": "…"}`.

### 6.7 `POST /api/session/close`

Body `{}`. Ends the session (reason `closed`) and asks its driven run to stop. The page sends it
from `pagehide` with `fetch(…, {keepalive: true, credentials: "omit"})`. 200 `{"ok": true}`.

### 6.8 Session lifecycle

At most 8 sessions; at the cap the least recently seen session without a driven run is evicted
(reason `evicted`), else `/launcher/launch` answers 429 `session_cap`. A session ends on: close
(`closed`), a newer session for its document (`replaced`), a launcher release (`host_gone`),
plugin unload (`turned_off`), 12 h without a request (`idle`), double use (`double_use`), or
eviction (`evicted`). Ending a session asks its driven run to stop. The plugin remembers the
digests of ended tokens and their reason for 10 minutes (at most 64) so a later request gets
`ended`; after that, `ended` is `null`.

## 7. Static files (`/ui/*`)

- Only files listed in `plugin/sidebar/SHA256SUMS` are served, and only when their bytes still
  match. Path: `/ui/<relpath>`, `relpath` matching
  `^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$`; any `%`, `..`, `\\`, query string or
  fragment-looking byte gives 404 `not_found`. `/`, `/ui` and `/ui/` give 404.
- `index.html` is served to a top-level navigation whatever its `Sec-Fetch-Site` (S1: the
  bootstrap navigation is `cross-site`) as long as `Sec-Fetch-Mode` is `navigate` or absent and
  `Sec-Fetch-Dest` is `document` or absent. Every other file needs `Sec-Fetch-Site` `same-origin`
  or absent (403 `cross_site`). Safari also asks for `/favicon.ico`,
  `/apple-touch-icon-precomposed.png` and `/apple-touch-icon.png` with no fetch metadata; they
  get 404.
- Methods: GET only (405 otherwise).
- Content types by suffix: `.html` `text/html; charset=utf-8`, `.js` `text/javascript;
  charset=utf-8`, `.css` `text/css; charset=utf-8`, `.svg` `image/svg+xml`. No other suffix is
  served.
- Headers on every static answer:

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; worker-src 'none'; manifest-src 'none'; require-trusted-types-for 'script'; trusted-types dompurify
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
X-Frame-Options: DENY
Permissions-Policy: accelerometer=(), camera=(), display-capture=(), fullscreen=(), geolocation=(), gyroscope=(), hid=(), magnetometer=(), microphone=(), midi=(), payment=(), serial=(), usb=(), clipboard-read=(), clipboard-write=(self)
Cache-Control: no-cache
```

### 7.1 Files in `plugin/sidebar/`

| File | Served | Note |
| --- | --- | --- |
| `index.html` | yes | the page |
| `app.js` | yes | first script in `<head>` (7.2) |
| `omniaTheme.js` | yes | palette and appearance; *Auto* uses `host_scheme` from the exchange |
| `omnia-tokens.css`, `omnia-pane.css`, `taskpane.css` | yes | byte-identical to the pinned upstream files |
| `sidecar-lo.css` | yes | LibreOffice-only additions |
| `assets/icon.svg` | yes | brand mark and `<link rel="icon">` |
| `vendor/marked.umd.js`, `vendor/purify.js` | yes | marked 15.0.12, DOMPurify 3.4.12, unminified |
| `vendor/marked.LICENSE.md`, `vendor/dompurify.LICENSE` | no | licences |
| `vendor/VENDOR.json` | no | registry origin and hashes |
| `UPSTREAM.json` | no | pins of the three Omnia CSS files |
| `SHA256SUMS` | no | one line per served file: `<64 lowercase hex>␠␠<relpath>`, sorted by path, LF, final newline |

### 7.2 Page rules the plugin relies on

- `app.js` is the first `<script>` in `<head>`, classic (not `defer`, not `async`). At top level,
  before any other script runs, it reads `location.hash`, accepts only
  `^#launch=[A-Za-z0-9_-]{43}$`, keeps the code in a closure, and calls
  `history.replaceState(null, "", "/ui/index.html")`. No other fragment parameter is read.
- The session token lives only in a closure: no `localStorage`, `sessionStorage`, cookie, URL or
  `window.name`. Reload or a duplicated tab shows the reconnect text. (`localStorage` keeps only
  the theme under `omnia.theme`.)
- Every `fetch` uses `credentials: "omit"`; POSTs send `Content-Type: application/json`.
- App mode only, once after the exchange (S1 showed `resizeTo` and `moveTo` work in a Chrome app
  window): `deco = outerHeight - innerHeight`; `w = bounds.width`, `h = bounds.height + deco`,
  `x = bounds.x`, `y = bounds.y - deco`; clamp `w`, `h` to `screen.availWidth/availHeight` and
  `x`, `y` into the `screen.avail*` rectangle; then **`resizeTo(w, h)` first and `moveTo(x, y)`
  second**. S1: calling `moveTo` first is clamped by the window server against the old, taller
  size (the window landed 38 points too high); in this order the window landed exactly on the
  requested client rectangle. Tab mode never moves or resizes.
- Replies are rendered only as `marked.parse(md, {async: false, breaks: true})` then
  `DOMPurify.sanitize` with `ALLOWED_TAGS` `p br strong em del code pre blockquote ul ol li h1 h2
  h3 h4 h5 h6 table thead tbody tr th td hr a`, `ALLOWED_ATTR` `href align start`,
  `ALLOW_DATA_ATTR: false`, `ALLOW_ARIA_ATTR: false`, `ALLOWED_URI_REGEXP` `^(https?|mailto):`
  and `RETURN_TRUSTED_TYPE: true` (R1, R19). Every `<a>` then becomes a non-navigating control
  that shows the URL with a **Copy link** button; nothing in the page navigates or opens a window.
  No `<img>`; no HTML sink other than the DOMPurify Trusted Types policy; status and activity text
  use `textContent`.

### 7.3 DOM ids the page must keep

`app-title`, `settings-button`, `connection-status` (`role="status"`), `privacy-badge`,
`desk-connect`, `desk-connect-text`, `desk-connect-code`, `desk-connect-button`, `bridge-status`,
`bridge-session-id` (shows `chat_label`), `new-chat-button`, `messages` (`role="log"`),
`prompt-input`, `stop-button`, `send-button`, `settings-dialog`, `close-settings-button`,
`save-settings-button`, `bridge-url`, `document-line`; radio groups named `omnia-palette` and
`omnia-mode`. These ids exist once, outside `#messages`. Code finds anything inside `#messages`
by element reference, never by id; the sanitizer strips `id`, `name` and `class` from replies.

## 8. Launcher side

### 8.1 `.oxt` contents (0.5.0)

```
META-INF/manifest.xml
description.xml
README.txt
Jobs.xcu
Addons.xcu
ProtocolHandler.xcu
Factories.xcu
Sidebar.xcu
python/hermes_bridge.py          uno-component (Python)
python/hermes_sidebar.py         uno-component (Python): the launcher panel
python/range_logic.py
python/office_ops.py
python/panel_registry.py
python/private_files.py
python/sidecar_launcher.py       new: state machine, channel client, bootstrap file, bounds, argv
python/launcher_macos.py         new: macOS adapter (shipped on every OS, imported on macOS only)
python/launcher_windows.py       new: Windows adapter (shipped on every OS, imported on Windows only)
dialogs/SidecarChatPanel.xdl     kept name: now the launcher panel's layout
icons/sidecar-deck.png
LICENSE
NOTICE
```

New Python helpers are `application/binary` entries in `manifest.xml`, like `range_logic.py`.
There is no Linux adapter (R16); on other systems the panel says Sidecar windows are not
supported there.

### 8.2 Panel texts (R10, R11)

| State | Text |
| --- | --- |
| hello refused (connection refused) | "Chat is off in Hermes." / "See README ▸ Sidebar chat." |
| `no_pairing` | "Hermes is not paired with LibreOffice yet. See README ▸ Pairing." |
| proof or response MAC wrong | "Something other than your Hermes answered on port <n>. Sidecar did not open." |
| opening | "Opening Sidecar…" |
| state `exchanged` | "Sidecar is open." |
| still `pending` after 20 s, or `expired` | "The Sidecar window did not connect. Your browser may be managed by your organization; see README ▸ Which browser." |
| `double_use` | "Another program used this launch. Click Open Sidecar again." |
| `window_live`, raise failed or tab mode | "Sidecar is already open. Switch to it from the Dock or taskbar." |

No automatic second attempt in another browser. A browser is spawned only from the panel button
or the Tools menu command, after `/launcher/launch` answered 201.

### 8.3 Bounds (S2)

S2 (disposable LibreOffice 26.8.2.2 profile, macOS 27.2.1, one 2560×1080 display at 1×) found:

- `XWindow.getPosSize()` on the frame's container window is the **client rectangle** in screen
  points, and `setPosSize` sets the same rectangle: for 600,240 820×560 the window server listed
  the window at 600,208 820×592 (a 32-point title bar above the client area).
- `XDevice.getInfo()` on that window reports `LeftInset = TopInset = RightInset = BottomInset = 0`
  (also `getOutputSize()` equals the client size). **The insets do not give the title bar**, so
  the launcher cannot learn LibreOffice's outer frame through UNO. R6's fallback applies: the
  page aligns itself with its own decoration height (7.2).
- `com.sun.star.awt.Toolkit.getWorkArea()` returned the whole display (0,0 2560×1080, menu bar
  included), so the page's `screen.avail*` clamp is the authoritative one.

The launcher computes `bounds` from the client rectangle `F` and the work area `A`:
`width = 400`, `height = F.height`, `y = F.y`; `x = F.x + F.width` when
`F.x + F.width + 400 <= A.x + A.width`, else `x = A.x + A.width - 400` (overlapping the frame's
right side). It starts the browser with `--window-position=<x>,<y - G>` and
`--window-size=<width>,<height + G>`, where `G` is the expected title bar (32 on macOS as measured;
0 on Windows until S3 measures Edge), and the page corrects the remainder. Not yet verified: 2×
displays, a second display, Windows scaling (G6, G7).

## 9. Error codes and user texts

### 9.1 Machine codes (`code`)

| Code | Status | Meaning |
| --- | --- | --- |
| `bad_host` | 403 | Host is not exactly `127.0.0.1:<port>` |
| `forbidden_credential` | 403 | a credential header of another route family (1.2) |
| `cross_site` | 403 | `Sec-Fetch-Site` other than `same-origin` |
| `bad_origin` | 403 | `Origin` missing or not `ORIGIN` where required |
| `method_not_allowed` | 405 | any other method on a listed path (`Allow` header lists the right one) |
| `unsupported_media_type`, `length_required`, `too_large`, `body_timeout`, `bad_request` | 415, 411, 413, 408, 400 | body rules (1.2) |
| `not_found` | 404 | unknown path, unlisted or changed static file, unknown launch id |
| `no_pairing` | 503 | no pairing token in Hermes yet |
| `launcher_auth` | 401 | channel unknown, expired, wrong counter or MAC |
| `channel_limit` | 429 | not used while the oldest channel can be dropped; reserved |
| `too_many_launches`, `session_cap` | 429 | 4.1 |
| `launch_expired`, `launch_double_use` | 401 | 6.1 |
| `no_session` | 401 | missing or ended session (`ended` gives the reason) |
| `busy`, `unresolved` | 409 | 6.3 |
| `too_many` | 429 | `MAX_ACTIVE` chats running or unresolved |
| `plugin_inactive`, `worker_failed` | 503 | 6.3 |
| `message_invalid` | 400 | 6.3 |
| `unknown_turn` | 404 | 6.4, 6.5 |
| `turn_running` | 409 | 6.6 |
| `turned_off` | 503 | the plugin is unloading |
| `internal` | 500 | "the chat endpoint failed" (never exception text) |

### 9.2 Page texts by `ended` reason

| `ended` | Text, then |
| --- | --- |
| `null` (reload, duplicate, unknown) | "Click Open Sidecar in LibreOffice to reconnect." |
| `replaced` | "This document is open in a newer Sidecar window. This one is disconnected." |
| `host_gone` | "LibreOffice closed this document." then `window.close()` |
| `turned_off` | "The LibreOffice plugin was turned off in Hermes." |
| `idle` | "This window was idle too long. Click Open Sidecar in LibreOffice again." |
| `double_use` | "This launch was used by another program; that connection was closed. Click Open Sidecar again." |
| `evicted` | "Too many Sidecar windows are open, so this one was disconnected. Click Open Sidecar in LibreOffice again." |

Turn status line: `working` "Hermes is working…"; `stopping` "Stopping…"; `hermes_state`
`waiting_for_approval`: "Hermes asked for an approval that Sidecar cannot give. Press Stop and
ask in Hermes itself." (R20). Outcome texts are the plugin's: `USER_STOPPED` = "Stopped. Check
the document for changes made before stopping.", `OUTCOME_UNCERTAIN`, `PLUGIN_TURNED_OFF`, and
the existing error sentences.

## 10. Methods outside the table

A method not listed for a known path answers 405 `method_not_allowed` as JSON with an `Allow`
header, no CORS headers, label `method`. Unknown paths answer 404 `not_found`. The legacy pair
keeps 0.4.0's answers (404 "unknown route"; other methods get the standard 501) unless the plugin
owner changes them in a later revision.

## 11. Log labels

`health`, `chat`, `launcher-hello`, `launcher-launch`, `launcher-status`, `launcher-release`,
`ui-static`, `api-session`, `api-state`, `api-chat`, `api-turn`, `api-stop`, `api-new-chat`,
`api-close`, `method`, `unknown`. A route handler sets `request.route_label` before it answers.

## 12. Connection budget (R4)

Per Sidecar window: no connection while idle except a state poll every 3 s; one `/api/turn`
long-poll while a turn is active. Plugin: at most `MAX_ACTIVE` (4) long-polls are ever parked
(one per driven turn), which stays below the browser's six connections per origin, and 4 + 6
parallel static or poll requests stay below `MAX_HANDLERS` (16).

## 13. Interfaces between lanes (Python)

### 13.1 `plugin/chat_bridge.py` (seam A0, frozen except for lane A)

| Name | Use |
| --- | --- |
| `ChatBridge.add_route_handler(handler)` | `handler(request) -> bool` sees every request before `/health` and `/chat`; returns True once it answered. A raised `ChatError` is answered as JSON; any other exception as 500 without its text. |
| `request.command`, `.path`, `.headers` | the request (stdlib `BaseHTTPRequestHandler`) |
| `request.read_body(limit)` | body bytes within the one read deadline (411/413/408/400 as `ChatError`) |
| `request.respond(status, data, start, content_type=…, headers=(…))` | one full answer, `Connection: close`, one metadata log line with `request.route_label` |
| `request.route_label` | set to a label from section 11 |
| `ChatBridge.begin_turn(session_id, content, on_done) -> run` | `_claim` now (raises 409/429/503), then one worker runs `_drive`; `on_done(run, TurnOutcome)` once |
| `ChatBridge.request_stop(run) -> bool` | user Stop and *window gone*: sets `stop_requested`; False when already settled |
| `ChatBridge.session_state(session_id)` | `"driven"`, `"unresolved"` or `None` (New chat rule) |
| `ChatBridge.owned_runs()`, `.closed`, `.close()` | unchanged |
| `compose_message(message, document)`, `SESSION_RE`, `MAX_MESSAGE`, `MAX_REPLY`, `MAX_ACTIVE` | unchanged |
| `TurnOutcome(kind, status, text)`, `TurnOutcome.KINDS`, `TurnOutcome.from_error(exc)` | outcome kinds of 6.4 |
| `ChatError`, `ChatStopped`, `USER_STOPPED`, `OUTCOME_UNCERTAIN`, `PLUGIN_TURNED_OFF`, `TURN_WORKER_FAILED` | errors and texts |

Lane A adds, in `chat_bridge.py` or its new modules: the last observed Runs API state on the run
(for `hermes_state`), the long-poll wake-up on `close()` (R12), and the role gates of 1.2 on the
legacy pair. `plugin/web_auth.py` (codes, sessions, channels; no sockets) and `plugin/web_api.py`
(routes, static files, turn records) register through `add_route_handler`.

### 13.2 Extension (`extension/python/`)

`sidecar_launcher.py` (lane C1) calls one adapter module, chosen by `sys.platform`; both adapters
expose the same functions:

| Function | Contract |
| --- | --- |
| `find_browsers() -> list[tuple[str, str]]` | `(name, executable)` in preference order (Chrome, Edge, Brave, Chromium on macOS; Edge, Chrome, Brave on Windows); fixed paths / `App Paths`, no LaunchServices |
| `cache_dir() -> pathlib.Path` | the `<cache>` of section 5 |
| `spawn(argv: list[str]) -> int` | starts the browser without a shell, cleaned environment, detached (`start_new_session` / `DETACHED_PROCESS`); returns the PID |
| `open_default(path: pathlib.Path) -> None` | D1 tab mode (5.4) |
| `raise_pid(pid: int) -> bool` | macOS: `NSRunningApplication` by PID, then activate; Windows: `AllowSetForegroundWindow`, the PID's visible top-level window, `SetForegroundWindow`; explicit `argtypes`, scalars only, main thread, off under `HERMES_LO_SIDECAR_NO_NATIVE=1`; False when it could not |
| `terminate(pid: int) -> None` | ask the browser this instance spawned to quit |

`sidecar_launcher.py` owns: `LauncherClient(port, read_token)` with `hello()`, `launch(document,
host_scheme, mode, bounds)`, `status(launch_id)`, `release(document_id, reason)` (section 3 and
4); `write_bootstrap(url) -> Path` and the sweep (section 5); `compute_bounds(frame, work_area)`
(8.3); `browser_argv(executable, profile_dir, target_uri, position, size, platform)` with the
frozen flag list. `hermes_bridge.py` calls `sidecar_launcher.on_document_closed(document_id)`
and `sidecar_launcher.on_terminate()`.

## 14. What the spikes did not cover

Edge and Brave (not installed on the test Mac), Firefox, Windows (S3 on the Windows test machine),
Intel Macs, 2× and multi-display placement, a managed browser, and whether an existing Chrome
instance with the same dedicated profile hands off correctly on a second click.
