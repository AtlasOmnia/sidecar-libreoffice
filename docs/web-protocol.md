<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Atlas Omnia -->
# Sidecar web window: protocol contract (0.5.1)

This is the frozen contract between the four parts of the 0.5.1 Sidecar window:

- the **plugin** (the chat endpoint in `plugin/chat_bridge.py` and the modules it loads),
- the **launcher** (the LibreOffice extension: `sidecar_launcher.py` and the per-OS adapters),
- the **page** (`plugin/sidebar/`, served by the plugin and shown in a browser), and
- the **verification stub** that imitates the plugin for page work.

Every byte format below is normative. A change after the freeze is a new revision of this file,
agreed by the plugin and launcher owners, with the test vectors updated in the same commit.
**Revision 2 (integration of the 0.5.0 lanes)** settles what the lanes reported: the gate order
with 404/405 (1.2), the legacy and unload refusals, the browser probe paths, the `chat_label`
form, leading zeros, malformed launch ids, the sanitizer option, the theme script's name, the
panel texts outside 8.2, the macOS helper programs and the Windows tab-only path (13.2, 14).
**Revision 3 (fix round 1 of the 0.5.0 review)**: `/api/chat` takes an optional `send_id` and a
retry of the same Send answers with the turn it started (6.3), the turn view names it (6.4), and
the page follows an unseen turn also when it has already ended (7.2); a user Stop is sent to
Hermes at once, also while a status poll is in flight (6.5); a hello displaces only a channel
that has not been used yet, and only a verified call displaces a used one (3.1); the prepared refusal of a full
endpoint carries the code `endpoint_busy` (9.1, 12); a turn's `seq` grows only when what the page
is shown changes (6.4); successful state polls and static files log at DEBUG (11); on Windows
the listener binds its port exclusively (1); methods outside the table are described as the code
answers them (10). On the launcher side: every launcher request has one deadline for the whole
call, and quitting has one budget (3.2); closing a document or quitting cancels its launch in
flight (4.3); a bootstrap file's 30 s removal timer starts when it is written (4.2); the busy
refusal, the plugin's coded hello refusals and an unsigned refusal of the first call get their
own panel texts (8.2); `terminate` takes a `wait` (13.2); and on Windows the extension's
listener binds its port exclusively too (1). Revision 2 callers keep working: a body without
`send_id` is accepted.
**Revision 4 (fix round 2 of the 0.5.0 review)**: one `send_id` starts at most one run: a
request that repeats a Send still being admitted waits for that admission and answers with the
same turn, the reservation and the published turn change in one step, and an id whose turn is
no longer current (of the last 64 kept) is refused with the new code `already_sent` (6.3, 9.1); the page never concludes
that a Send did not arrive, ignores state answers older than a Send or New chat, keeps Send and
New chat off while a Send is undecided and offers Try again with the same `send_id` (6.3, 6.6,
7.2); a user Stop's thread is registered before it starts, its whole call has one deadline, and
unload cuts it off and waits for it within one shared cut-off; unload asks known runs to stop
before it waits for the listener, which it ends at once and waits for within the budget (6.5,
13.1); the Windows exclusive bind covers 127.0.0.1 on the port, not every address (1); a
non-ASCII path is 404 before any MAC (3.2); the busy refusal is best effort (12).
**Revision 5 (0.5.1)** freezes capability-negotiated optional macOS following,
`/launcher/bounds`, `/api/window`, a shared four-slot park pool (turns first), worker
handoff and renewal, sticky manual undock/Reattach and the remembered Settings switch.
Following is **OFF by default**, including when the stored preference is absent, invalid or
unavailable. Only `sidecar.follow = "on"` enables page following. Capability negotiation is
independent of this preference: eligible macOS app launches negotiate follow so the switch
is available; the page does no window polling or follow placement until opted in.
D2 constants: retry 250 ms (poll-only fallback 100 ms), immediate send then 100 ms spacing,
settle 400 ms **and completed read-back**, undock >8 points, rectangle floor 240×240.
Fullscreen is treated as an ordinary large window: no reliable pure-UNO signal qualified
both native modes. Default-on promotion needs passing retests and explicit owner approval.

**Revision 5.1 (B1 corrective amendment, 0.5.1)** adds the `attachment_release`
hello capability and optional `launch_id` for document-close releases. The launcher retains
signed mint ids with their generations (including unexchanged codes); all retries capture
the same id. Release and exchange share the server lock. Late old releases cannot end a
replacement attachment or revoke its pending code. Legacy pairs retain document-wide,
best-effort close with no replacement-isolation guarantee; host quit is unchanged.

Labels such as R18, D1 or S1 are the requirement, owner-decision and spike numbers of the 0.5.0
design review; the spike results that fixed a detail are quoted where they apply.

Words: *client rectangle* is a window's content area without its title bar. *Screen points* are
the coordinates LibreOffice's `XWindow.getPosSize()` returns on a top-level window: origin at the
top-left of the main display, y downwards, in points (not device pixels).

## 1. Listener and origins

- The plugin listens on `127.0.0.1:<chat_port>` only (0 means off: no listener at all). On
  Windows the port is bound with `SO_EXCLUSIVEADDRUSE` and without `SO_REUSEADDR`, so no other
  socket, of any user account, can bind that address and port (a wildcard bind to the same port
  number still succeeds and, per Microsoft, covers every address but this one; not checked)
  while the listener is up; elsewhere `SO_REUSEADDR`
  stays (it allows a restart over `TIME_WAIT` connections; a second bind still fails). The
  extension's listener (8660, `hermes_bridge.BoundedHTTPServer`) binds the same way (revision 3).
  Checked on Windows 11 for the extension's listener: with `SO_REUSEADDR` (earlier 0.5.0 builds),
  a socket of another account (`LOCAL SERVICE`) could bind its port while it listened; with
  `SO_EXCLUSIVEADDRUSE` that bind is refused (WSAEACCES). The listener ran in an administrator
  session there, not with LibreOffice's limited token, and whether a second bind would have
  received connections was not tested. The plugin's listener was checked against a second
  socket of the same account only.
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
| POST | `/launcher/bounds` | 0.5.1 launcher | channel MAC | `launcher-bounds` |
| GET | `/ui/<file>` | browser | none (inert files) | `ui-static` |
| POST | `/api/session` | page | launch code in the body | `api-session` |
| GET | `/api/state` | page | session header | `api-state` |
| GET | `/api/window?seq=…` | page | session header | `api-window` |
| POST | `/api/chat` | page | session header | `api-chat` |
| GET | `/api/turn?id=…&seq=…&have=…` | page | session header | `api-turn` |
| POST | `/api/turn/stop` | page | session header | `api-stop` |
| POST | `/api/new-chat` | page | session header | `api-new-chat` |
| POST | `/api/session/close` | page | session header | `api-close` |

`/health` and `/chat` are kept compatible through 0.5.x for 0.4.0 extensions (same requests and
answers; the Host gate and the refusals of 1.2 are new) and are deprecated for removal in 0.6.0
(owner decision D2). Any other path answers 404; `OPTIONS`, `HEAD`, `PUT`, `DELETE`, `PATCH`, and
`GET` or `POST` where not listed, answer 405 on a listed path (section 10). `OPTIONS` and `HEAD` are never answered with CORS headers.
The paths browsers ask for on their own, `/`, `/favicon.ico` and `/apple-touch-icon*`, get the
JSON 404 `not_found` of the new routes with label `unknown` (not the legacy 404 "unknown route").

### 1.2 Request gates, in this order

1. **Host.** Exactly one `Host` header, equal to `HOST`. Otherwise 403 `bad_host`.
   - While the plugin unloads, every request that passed the Host gate answers 503 `turned_off`.
     On `/api/*` the answer adds `"ended": "turned_off"`; on the other families it has no `ended`.
   - **Route and method** come next: an unknown path answers 404 `not_found` and a known path
     with another method 405 `method_not_allowed` (section 10), before any credential is
     looked at. A foreign credential therefore never outranks 404 or 405.
2. **Role separation.** Each route family accepts only its own credential headers and refuses
   a request that carries a foreign one with 403 `forbidden_credential`:

   | Family | Must not carry |
   | --- | --- |
   | `/health`, `/chat` | `Origin`, any `Sec-Fetch-*`, `X-Sidecar-Session`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |
   | `/launcher/*` | `Origin`, any `Sec-Fetch-*`, `X-Sidecar-Session`, `X-Hermes-Plugin-Key` |
   | `/ui/*` | `X-Hermes-Plugin-Key`, `X-Sidecar-Session`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |
   | `/api/*` | `X-Hermes-Plugin-Key`, `X-Sidecar-Channel`, `X-Sidecar-Counter`, `X-Sidecar-MAC` |

   (The legacy pair checks `Host` first, then keeps its existing `Origin` refusal: 403 "requests
   with an Origin are refused". A legacy request carrying any `Sec-Fetch-*` header or a Sidecar
   credential header gets 403 "requests with Sidecar window credentials are refused here".)
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
5. **Credential** (session header, channel MAC, bearer token), per route family. On
   `/launcher/launch/<launch_id>` the id is checked only after the channel MAC (4.2).

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

- **Window state**: `"normal" | "minimized" | "fullscreen" | "hidden" | "window_closed"`.
- **Window snapshot**: `{"seq": int, "state": <window state>, "bounds": <Rectangle> | null}`.
  `bounds` is non-null exactly when `state` is `normal`; `seq` is 0 before the first
  accepted bounds call, otherwise 1..2147483647. Integers are never booleans.
  Initial snapshot: `{"seq": 0, "state": "hidden", "bounds": null}` (not placed).

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

The 200 answer may carry `"caps": [<strings>]`; revision 5.1 sends
`["window_follow", "attachment_release"]`. These are unsigned hints only: `window_follow`
permits an eligible macOS app launcher to send optional `follow` (4.1), while
`attachment_release` permits optional `launch_id` on `document_closed` (4.3), on every OS
and in app or tab mode. Ignore unknown entries; missing/non-list fields or any non-string
entry mean no capabilities. Only the signed launch echo authorizes following; only an
authenticated release acts. Hints are not downgrade-proof availability guarantees.
Old launchers ignore extra answer fields. Against 0.5.0 or revision-5 plugins without
`attachment_release`, new launchers send the legacy release shape with no extra key.
If a mint negotiated scoped release but a retry's fresh hello lacks the capability, stop
without sending: never downgrade that captured scoped operation to document-wide.

The launcher recomputes `server_proof` and compares in constant time. A mismatch, a missing
field, or any status other than 200 means the program on that port is not this user's Hermes:
the launcher sends nothing more and shows the *not your Hermes* text (section 8.2).

Without a pairing token the plugin answers 503 `no_pairing`. A refused connection means chat is
off or the gateway is down (section 8.2). Live channels are kept in two pools of at most 16
each (revision 3). Hello has no credential, so a new hello displaces only a channel that has not
been used yet (no MAC-verified call), the oldest such first. A channel's first MAC-verified call
moves it to the used pool; when that pool already holds 16, the least recently called used
channel is dropped. So hellos from a program without the token can end only a channel whose
launcher has not made its first call yet, never one it is working with; a used channel ends by
its own failure, its expiry, its 64-call limit, or 16 newer launchers' first calls. A server
nonce opens exactly one channel and is never reissued. Hellos can still displace a channel in
the moment between its hello and its first call; that call then gets the unsigned 401
`launcher_auth`, which the launcher shows as *did not answer, try again* (8.2), not as *not your
Hermes*: the hello had already proved the plugin.

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
401 answers to refused calls carry no MAC. Two unsigned answers are told apart, and only to
choose a panel text (8.2): the busy refusal of section 12, and a 401 `launcher_auth` to the
first call after the hello. Neither authorises anything.

**Paths (revision 4).** The MAC covers the path as ASCII. A request whose path is not printable
ASCII is refused as an unknown path (404 `not_found`, unsigned) before any MAC is computed; the
channel it names is not touched.

**Deadlines (revision 3).** Every launcher request is bounded by one deadline for the whole call
(connect, send, status line, headers, body): 5 s per call, and a status poll never runs past
the 20 s outcome limit. At quit the launcher's hello and `host_quit` share one 2 s deadline, and
everything it does at quit (including waiting for the browser) is bounded by 4 s (an application
budget for its requests and waits; a stalled file operation is not interrupted by it).

**Follow worker's channel (revision 5).** One serialized worker per LibreOffice process,
one request in flight, at most ten per second total, zero when nothing changes. The UNO
main-thread listener only copies rectangle/state/work area into a bounded latest-value slot
and wakes the worker, dropping unchanged values. No network, file or logging work, no lock
held by the worker during I/O; the worker never reads UNO. D2 measured a callback maximum
11.4262 ms over 1,866 callbacks (NOT PASS); actual production callback wall time must be
measured at p100 ≤5 ms over ≥1000 callbacks. A hand-off design is required, not a waiver.
Send the first change immediately, then bounds starts at least 100 ms apart with a trailing
latest value. **Resize-burst amendment (0.5.1 fix L):** a size change from a previously
sent valid normal rectangle opens one fixed 16 ms latest-value batch. The native move
notification can follow the intermediate resized-at-old-position notification; do not
spend an admission on that intermediate geometry. Later updates do not extend this batch,
other windows remain eligible, pure moves and initial/cold snapshots remain immediate.
While work is pending, timed worker sleeps are at most 10 ms before rechecking the same
admission deadlines (background native timers may oversleep); idle waits remain indefinite.
This changes neither the four-slot turns-first park policy nor its 250 ms unparked retry,
and adds no requests or threads. **Cold-channel amendment (0.5.1 fix A):** all request admissions remain
at least 100 ms apart except the first bounds on a successfully opened channel, for the
same still-live binding. That bounds may start immediately after its hello completes.
An orphaned or failed hello does not exempt a different hello or binding from spacing.
Every request start, including successful or failed hellos, counts toward
a process-wide maximum of ten starts in any rolling one-second interval. Budget exhaustion
delays even that first bounds; neither rebind nor channel failure resets the budget. There is
still only one request in flight and no idle prewarming. This replaces conservative spacing
of every hello/bounds pair, not the total request budget, channel proofs or lifetime fences.
Use an independent channel (not click/release/quit), opening a new hello before 48 calls or
90 s; every whole call has a 1 s deadline. No in-place retry. Every failure drops the channel.
Refused connection, signed 409 or failed proof unbinds that window. To recover a terminal
follow failure, close the Sidecar window and click Open Sidecar again (a live-window click
only raises it). An unanswered call retains the latest value, renews the channel and advances
seq rather than reusing an uncertain counter. Busy and unanswered calls back off 250 ms,
500 ms, 1 s; five consecutive such failures unbind. Each window's seq only grows. No follow failure changes panel text or starts/raises
anything; quit/close starts no further request.

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

```
request 3: POST /launcher/bounds, counter 3, body (no trailing newline)
  {"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":1,"state":"normal","bounds":{"x":1240,"y":102,"width":400,"height":848}}
  SHA256(body)  22f9e39c4973f6720ea42b8e720a5f5993d2b3d578682559edf6d4ae654ac807
  request MAC   v30WcmVm1K7joHamsLRFyRhnU4E5g6y47-bhQB8TPKE
response to request 3: status 200, body {"ok":true,"seq":1}
  SHA256(body)  c87ef1c0ab45dc8b9e285d32cd005ac8e66110a4dde2e68dfaa64991dcc2570c
  response MAC  uB4aMcV9-01j7t7iVhTqS1q74kMyjkur7kCGft1U-dQ
request 4: POST /launcher/bounds, counter 4, body
  {"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":2,"state":"minimized","bounds":null}
  SHA256(body)  cda42e6aeb2b78e01ce7ef5c07e3515c219f8a2161bff327b77662c9a9d7b254
  request MAC   zekmULHpr-_hN52XDlsmycbEFggZ24vGwBNHTRKBHv8
```

Revision-5.1 additional vector (same key, no trailing newline):

```
request 5: POST /launcher/release, counter 5, body
  {"document_id":"lo:5f0c:1","reason":"document_closed","launch_id":"AAAAAAAAAAAAAAAAAAAAAA"}
  SHA256(body)  edd5b96cc51ddc55fa3b250d2fa4441236bc6dd896b07bde176445c24ff11fce
  request MAC   XG6rpjVqIsPE0sseVFTmdhIcIcGyaHl5VKAgzYdlfqA
response to request 5: status 200, body {"ok":true,"released":1}
  SHA256(body)  686fc326f0646e1df50bbc2a23b6ff53a6f7b348b05b9dbf32dff6d8805adf75
  response MAC  dKnjQxvRpzu_tdrhnZeMYctHUyDxU84_X3ycxwaRNIU
```

## 4. Launcher routes

### 4.1 `POST /launcher/launch`

Body (exactly these required keys, plus optional `follow` below):

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

- Optional `"follow": true`: only after hello advertises `window_follow`, on macOS in app
  mode, unless `HERMES_LO_SIDECAR_NO_FOLLOW=1`. Absent otherwise; `false`, another value or
  `follow` with tab mode is 400 `bad_request`. Page preference does not suppress negotiation.
  The signed 201 answer adds `"follow": true | false`; only true authorizes the follow worker.
  The 200 `window_live` answer always adds `"follow": false` and keeps `launch_id: null`.
  No launch-id listing exists. The id already on the session is the attachment; no `frame_id`
  or `attachment_id` is added. Moving needs both the pairing token and this id.

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
and `bounds` and `follow` given here. Unexchanged codes are purged at expiry.

### 4.2 `GET /launcher/launch/<launch_id>`

200 `{"ok": true, "state": "pending" | "exchanged" | "expired" | "double_use"}`; 404 `not_found`
for an unknown id. Any request target `/launcher/launch/<anything>` without a query string is this
route, so a malformed id (wrong length, other characters, an extra `/`) is answered after the
channel MAC like an unknown id: a **signed** 404 `not_found`. A query string makes it an unknown
path (unsigned 404, before the channel is checked). Records are kept 120 s from minting. The launcher polls every 500 ms on a
worker thread for up to 20 s (R11). Each bootstrap file is deleted at the first state other
than `pending`, or 30 s after it was written by a timer started when it was written, whichever
comes first; at quit every bootstrap file this LibreOffice wrote is deleted, and at start and
quit any `launch-*.html` older than 60 s (revision 3).

### 4.3 `POST /launcher/release`

Revision 5.1 scoped body (exactly these three keys):
`{"document_id": "<id>", "reason": "document_closed", "launch_id": "<22>"}`.
Send only when the mint's hello and this release's hello advertise `attachment_release`.
The id must be a full section-2 launch id; null, wrong type/shape, wrong document association,
extra keys, or `launch_id` with `host_quit` are signed 400 `bad_request` after authentication.
Under the same lock as exchange, end only the live session with that launch id (reason
`host_gone`, ask only its driven run to stop), and revoke only that launch's pending code.
Check document association against retained launch records and live sessions independently:
a session can outlive its 120 s launch record. Unknown/already-ended ids are idempotent
200 no-ops, never a lookup of the document's current session. Answer signed 200
`{"ok": true, "released": <number of sessions ended>}`; revoking a pending code counts zero.
No route enumerates ids or returns them to pages.

Legacy body `{"document_id": "<id>", "reason": "document_closed"}` remains accepted for
old launchers and new launchers paired with old/no-capability plugins. It ends every current
session and revokes all pending codes for that document: **best effort only, not attachment
safe**. An already transmitted legacy release can end a reopened document's replacement;
a local generation check or client timeout does not cancel server execution. This compatibility
fallback retains the old retry policy, explicitly without the new non-interference guarantee.

`{"document_id": null, "reason": "host_quit"}` is unchanged: all documents' sessions and
pending codes, with no launch id permitted. It is intentionally host-wide, not scoped.

**Cancel on close and quit (revision 3).** Closing a document, or quitting LibreOffice, cancels
that document's launch in flight: no browser is started after it; a code the plugin already
minted is released (`document_closed`, or `host_quit` at quit) on the launch's own channel
after its launch call, which revokes it; its bootstrap file is deleted; nothing is reported. If
the browser was already being started, quit waits for it (bounded) and asks it to quit, and a
close quits it when no other document has a window. `host_quit` is sent when a window was
opened or a launch is in flight.

A `document_closed` release with no answer or unsigned busy is retried on fresh channels
at absolute offsets 0.5 s, 1.5 s and 3.5 s after close (initial attempt at 0), at most four
attempts per attachment. Hello and release share each attempt's whole-call deadline,
0.5 s for the initial attempt and min(0.5 s, remaining four-second close budget) for retries.
The initial attempt still runs once if a mint or worker stalls past four seconds;
never admit a retry at/after 4 s. This does not restart the retry budget.
Never after quit begins or any signed answer, including refusal. No main-thread join or
I/O-gate wait is added at close/quit. Each scoped retry uses the **same captured launch_id**,
never the newest attachment for that document. Scoped old-attachment cleanup may continue
after reopen, safely; legacy retries cancel locally when the generation changes, but already
sent legacy handlers remain unsafe. Retain every signed mint with its launch/opened-document
generation, even pending/not yet exchanged, until close/quit. Claim its close worker once,
including when close races mint, exchange, browser spawn or outcome polling. A cancelled mint
that returns late releases its own captured id and must not overwrite replacement bookkeeping.
Delivery remains best effort if all attempts fail; no general 90 s session expiry is promised.

### 4.4 `POST /launcher/bounds`

Body exactly `{"launch_id": "<22>", "seq": <int>, "state": <window state>, "bounds": <Rectangle> | null}`.
`seq` is 1..2147483647, not boolean; bounds is non-null exactly for normal, with section 2's
rectangle limits and 16 KiB POST limit. All launcher gates apply: exact Host, no Origin,
Sec-Fetch-* or session header, channel MAC/replay counters, signed accepted-call answers.
A live session must hold that launch_id, have launched with follow, and seq must exceed its
last seq. Replace its latest snapshot, wake its window poll; signed 200 `{"ok": true, "seq": <n>}`.
Otherwise signed 409 `attachment_stale` (unknown id, ended session, launch without follow,
non-increasing seq), or signed 400 `bad_request` for shape/type errors. Never revive a session.
Store latest only, at most eight snapshots, dropped at end/unload. First send is after launch
state becomes `exchanged`. An old plugin has no route (unsigned 404); never call it without
the signed follow echo. No browser route writes bounds; document/model text cannot control it.

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
5. **Removal**: as in 4.2: at the first state other than `pending`, or 30 s after the file was
   written (a timer started when it is written), whichever comes first; at quit every bootstrap
   file this LibreOffice wrote; at LibreOffice start and quit any `launch-*.html` in that folder
   older than 60 s.
6. **Windows**: this section replaces R2(b) (the URL on Edge's command line). In 0.5.0 Windows has
   no app mode at all (owner decision D3 after S3): it always uses tab mode (4).

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

- The exchange adds `"follow": true | false`, true only for an app launch that asked for it.
- `bounds` is `null` in tab mode. `chat_label` is display-only: `lo-` followed by the first 8 hex
  digits of the plugin's chat session id (`lo-<32 hex>`), always 11 characters matching
  `^lo-[0-9a-f]{8}$`; the page never sends it back. The same form is used in 6.2 and 6.6.
- A new session always starts a new chat (a new `lo-<32 hex>` id); a reconnected window does not
  continue the previous conversation.
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

Body `{"message": "<1..8000 characters, not only whitespace>", "send_id": "<22>"}`; `send_id` is
optional (a revision 2 body has only `message`). The plugin composes the Hermes turn with the
session's document (`compose_message`), claims synchronously and starts one worker
(`ChatBridge.begin_turn`). 202 `{"ok": true, "turn": <turn view>, "replayed": false}`. Refusals
are synchronous: 409 `busy` ("this chat is still waiting for a reply"), 409 `unresolved` (the
0.4.0 text that ends "Start a New chat to continue."), 429 `too_many`, 503 `plugin_inactive`, 503
`worker_failed`, 400 `message_invalid`. A new message clears the previous turn record.

**Idempotent Send (revision 3).** The page makes one random `send_id` (16 bytes, base64url, 22
characters) per Send and uses it for every attempt of that Send. When the session's current turn
was started with the same `send_id`, the plugin answers 200 `{"ok": true, "turn": <turn view>,
"replayed": true}` with that turn, working or ended, and sends nothing to Hermes; with the same
`send_id` and another message (compared as a SHA-256 digest of the trimmed text) it answers 400
`bad_request`. A malformed `send_id` is 400 `bad_request`.

**One admission per Send (revision 4).** From the moment a request with a new `send_id` reserves
the session's admission until the turn is published (or the admission is refused) is one
transition: the reservation is released in the same critical section that publishes the turn,
so no request ever sees neither. A request with the `send_id` being admitted (and the same
message) waits for that admission, at most one long-poll (25 s), and then answers 200 with the
published turn (`replayed: true`), or the same refusal the admission got, or 409 `busy` if it is
still not decided; with another message it answers 400 `bad_request` at once. Any other
`/api/chat` while an admission is in progress answers 409 `busy`. The plugin keeps the
`send_id` values of the session's last 64 published turns, also across New chat: a request
with one of them whose turn is no longer the current one answers 409 `already_sent` ("This message
was already sent to Hermes; it is not sent again.") and starts nothing. A refused admission
records nothing: the same `send_id` may be sent again. The page:

- retries a Send whose answer did not arrive (network error) or was 503 `endpoint_busy` (nothing
  was read), and a retry answered 409 `busy`, at most twice, 1 s apart, with the same `send_id`;
- after three unanswered attempts sends nothing more on its own and says it is not known yet
  whether Hermes received the message. The Send stays **undecided**: Send and New chat stay
  disabled (no fresh `send_id` can be sent), and **Try again** (`desk-connect-button`) sends the
  same request once more. Only that Send's own turn decides it: a state answer whose turn has
  this `send_id` is followed (7.2); a state answer without it proves nothing (the first request
  may still be in admission, or the answer may have left the plugin before the request arrived)
  and the page never says the message did not arrive. A refusal of the same `send_id` decides
  it as not started (the message goes back to the input box), except `endpoint_busy` (nothing was
  read) and a `busy` answered to a retry (the first request may still be in admission), which
  leave it undecided; a `busy` answered to the first request is a refusal, because nothing of
  that Send can be in admission yet. `already_sent` decides it as sent earlier;
- ignores a state answer to a request sent before the latest Send or New chat began.

### 6.4 `GET /api/turn?id=<turn>&seq=<n>&have=<n>` (cursor long-poll)

- Query: exactly the keys `id`, `seq`, `have`, each once; `seq` and `have` are decimal integers
  `0 <= n <= 200000` without leading zeros (`^(0|[1-9][0-9]{0,5})$`); `seq=01` answers 400
  `bad_request`. `seq` is the last record version the page has; `have` is how many
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
          "send_id": "<22>" | null,
          "text_from": 0, "text": "<reply characters from text_from on>", "text_length": 1234,
          "activity": [],
          "outcome": null | {"kind": "completed" | "user" | "uncertain" | "turned_off" | "error",
                             "message": "<sentence>" | null}}}
```

- `hermes_state` is the last Runs API state the plugin observed for the run (display-only): the
  Runs API state `started` is reported as `running`; any state not listed here is `null`. `seq`
  grows when `state`, `outcome` or the reported `hermes_state` changes, not when the Runs API
  state changes behind the same reported value (`started` to `running`).
- `send_id` is the `send_id` of the `/api/chat` request that started the turn, `null` without one.
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

Body `{"turn": "<22>"}`. `ChatBridge.request_stop` sets `stop_requested` and, once the run's id is
known, sends the stop to Hermes at once from a short-lived thread with the run's own key, so a
status poll in flight does not delay it; before the id is known the worker sends it as soon as
creation answers. Revision 4: that thread and its request are registered with the endpoint
before the thread starts; the whole request (connect, send, status line, headers, body) has one
deadline, 10 s or the unload budget's end, whichever is first, re-read before every send and
read (an answer trickled byte by byte does not extend it); unload cuts it off (nothing more is
connected or sent on it; the socket it reads from is shut down even after `http.client` dropped
it for `Connection: close`). Unload then waits for all user Stop threads together until ONE
cut-off, 0.25 s after the cut-off and so at most 0.25 s past the budget (not 0.25 s per
thread), and joins only threads that have started. A thread registered but not started yet, or
still inside an operating-system connect (which cannot be interrupted), is left behind and
logged; when it goes on it connects or sends nothing. So unload guarantees no Stop payload after
the cut-off, not that every Stop thread has ended. The stop of a run already seen to settle holds
unload up for that grace at most, whatever the budget. These limits are the waits the plugin
allocates, not a real-time bound: the operating system can return from a wait a little late.
On an asyncio event-loop thread unload waits at most 0.05 s for the listener, within the
remaining budget (13.1); runs and Stop threads are not waited for, and the Stop requests are
cut off only when the budget ends, so until then a Stop that
has not connected yet can still be sent after unload returned. One stop per run is in flight
at a time.
Only an observed settled state releases an admitted run, once, through the same ownership
check: the worker driving the run reports outcome `user` when it sees the run settle; when no
worker drives an unresolved run any more, a settled answer to its stop releases it. A stop's
acknowledgement alone releases nothing, and a run Hermes reports completed before the stop took
effect still reports what Hermes reported. 202 with the turn view (state `stopping`), or 200
with the final view when the turn already ended. 404 `unknown_turn`.

### 6.6 `POST /api/new-chat`

Body `{}`. Refused with 409 `turn_running` ("Wait for the reply, or press Stop first.") while the
session's run is **driven** (`ChatBridge.session_state(...) == "driven"`) or a Send is being
admitted. The page sends New chat only with no turn, no Send in flight and no undecided Send,
sends no Send while it waits for the answer, and ignores state answers to requests sent before
New chat was answered (revision 4). This orders New chat within one page; it is not a
transaction across a lost or delayed request. With an unresolved run it is allowed: the session
gets a new `lo-<32 hex>` id from the same generator, the old run stays
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

The window snapshot is dropped when its session ends.

### 6.9 `GET /api/window?seq=<n>`

Query exactly `seq` once; decimal with no leading zeros, as 6.4, but range 0..2147483647
to cover the window sequence range in section 2. 200:

```json
{"ok": true, "superseded": false, "retry_ms": 0,
 "window": {"seq": 17, "state": "normal", "bounds": {"x": 1190, "y": 260, "width": 400, "height": 560}}}
```

Answer at once when snapshot seq exceeds the query. Otherwise park up to 25 s only if fewer
than MAX_ACTIVE (4) requests are parked, counting turn and window polls together; timeout
answers unchanged with retry_ms 0. Without a slot answer unchanged immediately, retry_ms 250.
A turn poll always gets a slot: preempt one parked window poll (answer it with retry_ms 250)
when needed. Poll-only fallback is a plugin constant: no window parks, retry_ms 100.
One window poll per session; a newer poll answers the older with superseded true.
Retry_ms 0 means ask again from the network callback; clamp other values to 100–5000 ms.
A no-follow session gets 404 `no_follow`. All /api gates and session-end/unload rules apply;
unload wakes parked window polls, each answers once 503 turned_off, no writes afterwards.
A parked window poll counts as seen for window_live, but never as a turn poll for the
90 s window-gone rule. The browser reads only its own snapshot, never sees launch_id.

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

The page never requests `window-management`; the Permissions-Policy above stays unchanged.

### 7.1 Files in `plugin/sidebar/`

| File | Served | Note |
| --- | --- | --- |
| `index.html` | yes | the page |
| `app.js` | yes | first script in `<head>` (7.2) |
| `omnia-theme.js` | yes | palette and appearance; *Auto* uses `host_scheme` from the exchange (the prototype's `omniaTheme.js`, renamed because the path rule above allows lower case only) |
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
  the theme under `omnia.theme` and follow preference under `sidecar.follow`, nothing else.)
- Every `fetch` uses `credentials: "omit"`; POSTs send `Content-Type: application/json`.
- App mode only, initially after the exchange (S1 showed `resizeTo` and `moveTo` work in a Chrome app
  window): `deco = outerHeight - innerHeight`; `w = bounds.width`, `h = bounds.height + deco`,
  `x = bounds.x`, `y = bounds.y - deco`; clamp `w`, `h` to `screen.availWidth/availHeight` and
  `x`, `y` into the `screen.avail*` rectangle; then **`resizeTo(w, h)` first and `moveTo(x, y)`
  second**. S1: calling `moveTo` first is clamped by the window server against the old, taller
  size (the window landed 38 points too high); in this order the window landed exactly on the
  requested client rectangle. Tab mode never moves or resizes.
- Replies are rendered only as `marked.parse(md, {async: false, breaks: true})` then
  `DOMPurify.sanitize` with `ALLOWED_TAGS` `p br strong em del code pre blockquote ul ol li h1 h2
  h3 h4 h5 h6 table thead tbody tr th td hr a`, `ALLOWED_ATTR` `href align start`,
  `ADD_URI_SAFE_ATTR: ["align", "start"]`, `ALLOW_DATA_ATTR: false`, `ALLOW_ARIA_ATTR: false`,
  `ALLOWED_URI_REGEXP` `^(https?|mailto):` and `RETURN_TRUSTED_TYPE: true` (R1, R19).
  `ADD_URI_SAFE_ATTR` is required: DOMPurify 3.4.12 tests every allowed attribute that is not
  URI-safe against `ALLOWED_URI_REGEXP`, so without it `align="right"` and `start="3"` are
  stripped and tables lose their alignment and ordered lists their numbering.
- The page may (and the shipped page does) give marked an `html` renderer that shows raw HTML
  written by the model as escaped text, keeping a bare `<br>` as a line break, before the
  sanitizer runs. This keeps hostile markup away from even the sanitizer's inert parse. Every `<a>` then becomes a non-navigating control
  that shows the URL with a **Copy link** button; nothing in the page navigates or opens a window.
  No `<img>`; no HTML sink other than the DOMPurify Trusted Types policy; status and activity text
  use `textContent`.
- Turns (revision 3): the page remembers the ids of the turns it has followed in this session
  (cleared by New chat). A state poll that reports a turn the page has not followed, working or
  already ended, makes the page follow it with `/api/turn` from the reported `seq` and `have=0`
  (an ended turn answers at once), so its reply or
  outcome is shown exactly once; a turn already followed is never shown again. Every Send carries
  a `send_id` (6.3). While a Send is undecided only a turn with that `send_id` is followed
  (revision 4).

**Following (revision 5).** Settings shows **Follow LibreOffice** only when exchange follow is
true. OFF by default; absent/invalid/unavailable `localStorage["sidecar.follow"]` means off;
only `"on"` enables, off stores `"off"`. Remember across sessions, no plugin setting. Initial
launch placement above remains unchanged; following off issues no window polls or follow
geometry calls. Ask /api/window only in app mode with follow true, preference on, docked and
visible. Stop polls while hidden, undocked, off or ended; resume only when eligible. One poll
chain, re-issued from network callback (no second timer chain).

After each own placement, cancel/restart a 400 ms settle/read-back callback. Suppress undock
comparisons both during the settle interval **and until that placement's read-back callback
has completed** (deadline expiry alone is insufficient). Read back actual x/y/outer sizes
and current screen measurements to establish the placed baseline; never compute it. Before
new snapshots, on the existing 3 s tick and on resize, a position OR size deviation **>8
points**, with unchanged screen measurements and no own pending placement/read-back, means
manual undock: stop movement and polling, sticky until Reattach/new session. Screen changes
re-baseline, not undock. Reattach requests seq=0, applies the newest snapshot and docks.
A drag begun during own settling can be snapped back once (accepted residual, not qualified
by D2). Ignore older seq. Validate bounds; one resizeTo and one moveTo call site, resize first,
skip unchanged calls. Never act in tab mode, ended, undocked, paused or off. Never open/focus/
blur/requestFullscreen/getScreenDetails/navigate; never store/log/send geometry, put it in a
URL/title/request, or send the page position to the plugin. No document/model-driven placement.

Minimized/hidden/window_closed states carry null bounds and pause; do not run the launch
fallback on them; keep last normal geometry. Hidden must latch until shown. No normal host
rectangle below 240×240. Fullscreen is ordinary large-window behaviour, not qualified native
Space detection; no automatic raise/activation or Space switch. Sidecar does not minimise
with LibreOffice; it stays where it is and following pauses on minimisation.

If screen.isExtended is true and target centre is outside the current screen, do not move:
show the other-display text. Otherwise clamp to screen.avail* as above. No permission prompt.
Second-display/negative-origin/Retina behaviour is explicitly hardware-untested.

### 7.3 DOM ids the page must keep

`app-title`, `settings-button`, `connection-status` (`role="status"`), `privacy-badge`,
`desk-connect`, `desk-connect-text`, `desk-connect-code`, `desk-connect-button`, `bridge-status`,
`bridge-session-id` (shows `chat_label`), `new-chat-button`, `messages` (`role="log"`),
`prompt-input`, `stop-button`, `send-button`, `settings-dialog`, `close-settings-button`,
`save-settings-button`, `bridge-url`, `document-line`, `follow-status`, `reattach-button`, `follow-toggle`; radio groups named `omnia-palette` and
`omnia-mode`. These ids exist once, outside `#messages`. Code finds anything inside `#messages`
by element reference, never by id; the sanitizer strips `id`, `name` and `class` from replies.

## 8. Launcher side

### 8.1 `.oxt` contents (0.5.1)

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
python/window_follow.py          new: follow state, coalescing, worker (no UNO)
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
| hello answered 401 or 404 without an error `code` (a 0.4.0 plugin: it checks its bearer token before the path and has no codes), or 503 without any `code` from a port not yet known as 0.5.0 | "Your Hermes plugin is older than this extension. Update atlas-sidecar-libreoffice in Hermes, then click Open Sidecar again." |
| the plugin's busy refusal (section 12: 503 `endpoint_busy`, or the sentence *the chat endpoint is busy; try again shortly*, or a 503 without a `code` from a port that already answered as a 0.5.0 plugin), on hello or on any call (revision 3) | "Sidecar is busy, try again in a moment." A busy refusal on a channel call does not use up the counter; a status poll that gets it keeps waiting |
| hello answered 503 `turned_off` (the plugin is unloading) (revision 3) | "The LibreOffice plugin was turned off in Hermes." |
| hello answered 500 `internal` (revision 3) | "The chat endpoint failed. Click Open Sidecar to try again." |
| the first call after a hello answered 401 `launcher_auth` without a MAC (the channel was displaced or expired, 3.1) (revision 3) | the *no answer* text below |
| idle panel | "Open Sidecar to chat with Hermes about this document." |
| before hello | "Checking Hermes…" |
| no answer, or a broken connection | "Hermes did not answer on port <n>. Click Open Sidecar to try again." |
| not macOS or Windows | "Sidecar windows are not supported on this system." |
| no Calc, Writer or Impress document | "Sidecar opens for a Calc, Writer or Impress document." |
| launch or browser cache folder refused as unsafe | "The Sidecar cache folder is unsafe. See README ▸ Which browser." |
| the browser could not be started | "Sidecar could not start the browser. Click Open Sidecar to try again." |
| pairing token file unsafe, missing or invalid | "The pairing token file has unsafe permissions; restart LibreOffice." / "The pairing token is missing; restart LibreOffice." / "The pairing token is invalid; restart LibreOffice." |
| a refusal the plugin signed (any other status) | the plugin's own `error` sentence, at most 300 characters |

The older-plugin row is a plain version mismatch, not an impostor: nothing is sent after the
hello and no window opens. A wrong proof from a program that does answer 200 is still the *not
your Hermes* row. The hello answer is not signed, so for the coded refusals above the launcher
shows its own fixed texts (the plugin's sentences for those codes), never the answer's words;
any other coded refusal of the hello is the *not your Hermes* row.

No panel text exists for follow failures.

No automatic second attempt in another browser. A browser is spawned only from the panel's
**Open Sidecar** button, after `/launcher/launch` answered 201 (and, for tests, from the
`dev_open_sidecar` bridge operation, which exists only when LibreOffice runs with
`HERMES_LO_DEV=1`). The Tools menu command **Open Sidecar chat** only shows the Sidecar panel.

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
`--window-size=<width>,<height + G>`, where `G` is the expected title bar (32 on macOS as measured),
and the page corrects the remainder. Windows has no app mode in 0.5.0, so no bounds are sent
there. Not yet verified: 2× displays and a second display (G6).

Following uses this formula for every normal event, except the overlap branch applies only
when the frame centre lies inside work area A; otherwise put the target right of the frame.
Do not send normal geometry for a rectangle smaller than 240 in either dimension. No UNO
work-area fallback for minimized/hidden. Spike D's one-window parked move median/p95 were
113.65/188.99 ms; D2's immediate-send shared parked run 21.60/38.93 ms, fifth-reader unparked
217.13/327.93 ms. Sampled upper-bound timings on one 1× display, not a promise or hardware
qualification of second display/Retina. D2 real LO drags were unqualified. Q255 traced the
255 exit to harness SIGTERM cleanup; exact-PID normal Quit returned 0. Final-byte native
acceptance remains required.

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
| `channel_limit` | 429 | not used (3.1 always has room for a hello); reserved |
| `endpoint_busy` | 503 | every handler slot is taken; nothing was read (12) |
| `too_many_launches`, `session_cap` | 429 | 4.1 |
| `launch_expired`, `launch_double_use` | 401 | 6.1 |
| `no_session` | 401 | missing or ended session (`ended` gives the reason) |
| `busy`, `unresolved` | 409 | 6.3 |
| `too_many` | 429 | `MAX_ACTIVE` chats running or unresolved |
| `plugin_inactive`, `worker_failed` | 503 | 6.3 |
| `message_invalid` | 400 | 6.3 |
| `unknown_turn` | 404 | 6.4, 6.5 |
| `attachment_stale` | 409 | 4.4 |
| `no_follow` | 404 | 6.9 |
| `already_sent` | 409 | 6.3: an earlier `send_id` of this session whose turn is no longer current (revision 4) |
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

Chat readiness texts (`chat.reason`, written by the page):

| `chat.reason` | Text |
| --- | --- |
| `plugin_inactive` | "Chat is off: the LibreOffice plugin is not active in Hermes. Enable it, restart the Hermes gateway, then click Open Sidecar in LibreOffice again." |
| `no_api_key` | "Chat is off: Hermes's API server key is not available. Enable the API server, set API_SERVER_KEY, then click Open Sidecar in LibreOffice again." |
| `bad_api_key` | "Chat is off: Hermes's API server key cannot be used. Set API_SERVER_KEY again, then click Open Sidecar in LibreOffice again." |

Turn status line: `working` "Hermes is working…"; `stopping` "Stopping…"; `hermes_state`
`waiting_for_approval`: "Hermes asked for an approval that Sidecar cannot give. Press Stop and
ask in Hermes itself." (R20). Outcome texts are the plugin's: `USER_STOPPED` = "Stopped. Check
the document for changes made before stopping.", `OUTCOME_UNCERTAIN`, `PLUGIN_TURNED_OFF`, and
the existing error sentences.

Follow texts (fixed; outside #messages):

- "Sidecar is not following LibreOffice." with Reattach.
- "Following is paused while LibreOffice is minimised."
- "Following is paused while LibreOffice is in full screen." (reserved state text; no qualified full-screen detection ships).
- "Following is paused while LibreOffice is hidden."
- "The LibreOffice window this Sidecar followed was closed."
- "LibreOffice is on another display. Move this window there, then choose Reattach."

## 10. Methods outside the table

On a known path, `GET` or `POST` where not listed, `OPTIONS`, `HEAD`, `PUT`, `DELETE` and `PATCH`
answer 405 `method_not_allowed` as JSON with an `Allow` header, no CORS headers, label `method`.
Any other method (`TRACE`, `CONNECT`, `PROPFIND`, …) on any path gets the standard library's 501
HTML page, without CORS headers and without running any route. Unknown paths answer 404
`not_found`. The legacy pair keeps 0.4.0's answers (404 "unknown route") unless the plugin owner
changes them in a later revision.

## 11. Log labels

`health`, `chat`, `launcher-hello`, `launcher-launch`, `launcher-status`, `launcher-release`,
`ui-static`, `api-session`, `api-state`, `api-chat`, `api-turn`, `api-stop`, `api-new-chat`,
`api-close`, `launcher-bounds`, `api-window`, `method`, `unknown`. A route handler sets `request.route_label` before it answers.
Each answer is one log line: at DEBUG for a successful (status below 400) `api-state` or
`ui-static`, `launcher-bounds` or `api-window` answer (state polling every 3 s), at INFO for every other answer and
every refusal.

## 12. Connection budget (R4)

At most `MAX_ACTIVE` (4) requests are ever parked, turn and window polls together,
turn polls first. The follow worker has at most one request in flight and ten per second.
Idle non-following pages keep only their 3 s state polls; active turns use /api/turn.
The park cap stays below the browser's six connections per origin, and 4 + 6
parallel static or poll requests stay below `MAX_HANDLERS` (16). The listen backlog is 64. A
connection accepted while all 16 handler slots are taken gets the prepared answer 503
`{"ok": false, "error": "the chat endpoint is busy; try again shortly", "code": "endpoint_busy"}`
(unsigned, on every route, before the request is read): nothing was processed, the caller may
try again. It is best effort: the connection is closed right after it, so a client may see a
reset instead of the answer; the page treats a network error on Send the same way. The launcher shows it as busy (*Sidecar is busy, try again in a moment.*, 8.2), not as
an older plugin.

## 13. Interfaces between lanes (Python)

### 13.1 `plugin/chat_bridge.py` (frozen after Day-0 S1)

| Name | Use |
| --- | --- |
| `ChatBridge.add_route_handler(handler)` | `handler(request) -> bool` sees every request before `/health` and `/chat`; returns True once it answered. A raised `ChatError` is answered as JSON; any other exception as 500 without its text. |
| `request.command`, `.path`, `.headers` | the request (stdlib `BaseHTTPRequestHandler`) |
| `request.read_body(limit)` | body bytes within the one read deadline (411/413/408/400 as `ChatError`) |
| `request.respond(status, data, start, content_type=…, headers=(…))` | one full answer, `Connection: close`, one metadata log line with `request.route_label` |
| `request.route_label` | set to a label from section 11 |
| `ChatBridge.begin_turn(session_id, content, on_done) -> run` | `_claim` now (raises 409/429/503), then one worker runs `_drive`; `on_done(run, TurnOutcome)` once |
| `ChatBridge.request_stop(run) -> bool` | user Stop and *window gone*: sets `stop_requested` and sends the stop at once when the run id is known (6.5); False when already settled |
| `ChatBridge.session_state(session_id)` | `"driven"`, `"unresolved"` or `None` (New chat rule) |
| `ChatBridge.owned_runs()`, `.closed`, `.close()` | unchanged signatures; `close()` (revision 4) closes ingress, ends the web sessions, starts the stop threads for known runs, then ends the listener (`BoundedHTTPServer.stop_serving(timeout)`, whose deadline is taken when it is called: it wakes the accept loop with a non-blocking loopback connection, which adds no blocking connect wait, and the following wait uses the remaining deadline; close() passes what is left of its budget, capped at 0.05 s on an event loop), waits for the runs and user Stops, and finally cuts off every status poll and user Stop still in flight and waits for the user Stop threads until one cut-off 0.25 s later, joining only those that have started (6.5); on an event-loop thread it waits at most 0.05 s for the listener, within the remaining budget, then returns without waiting for runs or Stop threads; the cut-off comes when the budget ends and the threads are not waited for |
| `compose_message(message, document)`, `SESSION_RE`, `MAX_MESSAGE`, `MAX_REPLY`, `MAX_ACTIVE` | unchanged |
| `TurnOutcome(kind, status, text)`, `TurnOutcome.KINDS`, `TurnOutcome.from_error(exc)` | outcome kinds of 6.4 |
| `ChatError`, `ChatStopped`, `USER_STOPPED`, `OUTCOME_UNCERTAIN`, `PLUGIN_TURNED_OFF`, `TURN_WORKER_FAILED` | errors and texts |

Added in 0.5.0 (lane A), all no-ops for callers that do not use them:

| Name | Use |
| --- | --- |
| `ChatBridge(..., web_routes=True)` | `start()` attaches `plugin/web_api.py` (routes, sessions, close hooks) once; `False` leaves the bridge as in 0.4.0 (the stub and tests attach it themselves) |
| `ChatBridge.add_close_hook(begin, end)` | `begin()` runs when `close()` starts and must not block; `end(seconds_left)` runs just before `close()` returns, inside the same 10 s budget; both idempotent |
| `ChatBridge.run_state_listener` | `callable(run)` or `None`; called (never under the lock) when a run's `hermes_state` changes |
| `_Run.hermes_state` | the last observed Runs API state of a run, display-only (6.4 maps it) |
| `BoundedHTTPServer.request_queue_size = 64` | listen backlog, so a burst of page requests is queued rather than refused |
| `ChatBridge.request_stop(run)` | also wakes the thread following the run (its `_Run.wake` event), and sends the stop from a thread of its own, so a status poll in flight does not delay it (revision 3) |
| `BoundedHTTPServer` on Windows | `SO_EXCLUSIVEADDRUSE`, no `SO_REUSEADDR` (revision 3) |
| `chat_bridge.ENDPOINT_BUSY` | `"endpoint_busy"`, the code of the prepared busy refusal (revision 3) |

The role gates of 1.2 also apply to the legacy pair. `plugin/web_auth.py` (codes, sessions, channels; no sockets) and `plugin/web_api.py`
(routes, static files, turn records) register through `add_route_handler`.

`chat_bridge.QUIET_LABELS` includes `launcher-bounds` and `api-window`; no other seam change.

### 13.2 Extension (`extension/python/`)

`sidecar_launcher.py` (lane C1) calls one adapter module, chosen by `sys.platform`; both adapters
expose the same functions:

| Function | Contract |
| --- | --- |
| `find_browsers() -> list[tuple[str, str]]` | `(name, executable)` in preference order (Chrome, Edge, Brave, Chromium on macOS, at fixed paths in `/Applications` and `~/Applications`, no LaunchServices); on Windows in 0.5.0 always `[]`, so the launcher uses tab mode |
| `cache_dir() -> pathlib.Path` | the `<cache>` of section 5 |
| `spawn(argv: list[str]) -> int` | macOS: starts the browser without a shell, cleaned environment, detached (`start_new_session`); returns the PID. Windows 0.5.0: never called (raises) |
| `open_default(path: pathlib.Path) -> None` | D1 tab mode (5.4): macOS runs `/usr/bin/open <path>` (argument list, no shell, cleaned environment); Windows calls `os.startfile(<path>)`, which opens it with the default handler for `.html` files |
| `raise_pid(pid: int) -> bool` | macOS: runs `/usr/bin/osascript -l JavaScript -e <fixed script> <pid>` (argument list, no shell, cleaned environment, 5 s timeout); the fixed JXA script calls `NSRunningApplication.runningApplicationWithProcessIdentifier(pid).activateWithOptions(1)`, so no native code is loaded into LibreOffice (R6). Off under `HERMES_LO_SIDECAR_NO_NATIVE=1`. Windows 0.5.0: always False (tab mode cannot be raised). False when it could not |
| `terminate(pid: int, wait: float = 3.0) -> None` | macOS: SIGTERM to the browser this instance spawned (Chromium shuts down normally), waiting at most `min(wait, 3)` s; quit passes what is left of its 4 s budget (revision 3). Windows 0.5.0: nothing, `wait` is ignored (the page closes itself on `host_gone`) |

So the programs the extension starts are: the browser in app mode (macOS), `/usr/bin/osascript`
(raise, macOS), `/usr/bin/open` (tab mode, macOS), and on Windows whatever program the `.html`
file association names (normally the default browser). None goes through a shell.

`sidecar_launcher.py` owns: `LauncherClient(port, read_token)` with `hello()`, `launch(document,
host_scheme, mode, bounds)`, `status(launch_id)`, `release(document_id, reason, *, launch_id=None)` (section 3 and
4); `write_bootstrap(url) -> Path` and the sweep (section 5); `compute_bounds(frame, work_area)`
(8.3); `browser_argv(executable, profile_dir, target_uri, position, size, platform)` with the
frozen flag list. `hermes_bridge.py` calls `sidecar_launcher.on_document_closed(document_id)`
and `sidecar_launcher.on_terminate()`.

`window_follow.py` is pure Python, no UNO:

| Name | Contract |
| --- | --- |
| `Follower(read_token)` / `Follower.configure(read_token)` | supply the launcher's safe token-reader callable before any bind; never a token value |
| `Follower.attach(document_id) -> handle` | main thread at click; one handle per document |
| `handle.update(rect, state, work_area)` | main thread UNO callback; O(1), never blocks, unchanged dropped |
| `handle.bind(launch_id, port)` | launch worker after signed follow true and exchanged; replaces binding, seq restarts at 1 |
| `handle.close()` | close/dispose/quit; nothing sent afterwards |
| `handle.alive` | listener removes itself when false |
| `Follower.stop()` | quit: no further request started |

`Launcher.open(...)` gains `follow=handle | None`. UNO listener XWindowListener +
XTopWindowListener lives in hermes_sidebar.py, registry keyed by document, never panel-owned.
Bind to the launching frame, not current document. Stop on frame disposal, document close or
replacement in that frame, and quit; check binding immediately before send. A view switch
of the same document (including Print Preview) is not a replacement. Closing the deck
does not stop following. A second same-document frame never takes over; closing the followed
frame publishes window_closed locally and immediately fences following even if the document
remains open. This terminal mailbox value is best-effort, not a wire-delivery guarantee:
close cancels pending bounds admissions. Closing or disposing only the launching frame
does not release the document session: Sidecar and its reply stay open while another view
holds the document, but following stops. Actual document unload or replacement releases
the session. Never delay the main thread or permit post-close bounds requests to
flush a terminal snapshot. An already admitted request may finish, but cannot revive the
binding. A crash cannot rebind the old Sidecar; close/open again. On Windows nothing attaches or sends a follow key;
adapter interfaces unchanged. Support kill switch HERMES_LO_SIDECAR_NO_FOLLOW=1 suppresses
negotiation (independent of NO_NATIVE). Clicking Open Sidecar on an undocked window only raises.

**Integrator amendment (Print Preview / follow robustness, no wire-format change).**
Same-model view switches retain the listener; frame-only close stops following without
releasing a still-open document session. Unanswered bounds calls use the existing bounded
busy back-off on a fresh channel with increasing seq. The proof/MAC, route shapes and test
vectors are unchanged; see 3.2 and the lifecycle rules above.

## 14. What the spikes did not cover, and notes for 0.5.1

Not covered by the spikes: Edge and Brave on macOS (not installed on the test Mac), Intel Macs,
2× and multi-display placement, and a managed browser.

Observed after the spikes (0.5.0 lanes and integration):

- **macOS, Chrome 158 app window** (LibreOffice 26.8.2.2, disposable profile): open beside the
  frame, a second click raising the same window through `osascript` (no second window), quit
  ending the browser, a relaunch on the same dedicated profile showing no restore prompt.
- **Windows tab mode with Firefox 160** (the default `.html` handler on the test machine, Windows
  11): the launcher state machine (not LibreOffice) opened the bootstrap file through
  `os.startfile`; the page exchanged its code (`Sec-Fetch-Site: same-origin`), no command line
  carried the code or the bootstrap path, the second open answered `window_live`, and after
  `host_quit` the page closed its own tab with `window.close()`.
- **Windows 11, LibreOffice 26.8.3.1, tab mode with Firefox 160** (fix round 1, an earlier build
  of the 0.5.0 `.oxt` installed into a disposable profile, the real plugin with a fake Runs API):
  a click posted to the panel's **Open Sidecar** button (window messages, not the development
  hook) opened one Firefox tab, which connected; no
  command-line sample carried the code or the bootstrap path; one formatted reply and **Stop**
  worked; closing a document while the plugin held its launch released and revoked the code and
  opened no tab; quitting LibreOffice sent `host_quit`, soffice exited 0 and the tab closed
  itself. The launch folder's DACL granted only SYSTEM, Administrators and OWNER RIGHTS.

Revision-5 hardware/compatibility limits (not product acceptance): second display, negative
coordinates, display above/left, Retina/mixed scale, screen reconfiguration, green-button
Spaces/Stage Manager/tiling, long hand-drag feel, macOS Edge/Brave/Chromium, Intel/other macOS
and LO versions, >8 windows and same-document multi-frame, managed browsers, future Chrome
moveTo changes, manual NVDA/VoiceOver, macOS Firefox and alternative Windows default handlers
remain unverified. Pause/Reattach for another display; no native Space coupling; no screen
reader claim. D2 actual document drag was 0/3; G6 must retest. Q255 traced exit 255 to
harness SIGTERM cleanup, not normal Quit; exact-PID normal Quit returned 0.

Spike W (investigation, not a Windows 0.5.1 feature): its implicit-sign-in disabling flag
worked on the tested Edge build; placement was exact in physical pixels at 112% text scale.
Startup Boost and raise remain unresolved; Chrome preferred for the 0.5.2 spike.

S3 findings kept for the Windows app window in 0.5.2 (not used in 0.5.1):

- Edge signed a fresh `--user-data-dir` profile into the user's Microsoft account without a prompt,
  also with `--disable-sync`, `--inprivate` and `--guest`; no flag prevented it.
- Quitting the dedicated Edge instance started Edge's Startup Boost for the user's default profile
  (`--no-startup-window`, parented to our browser), which kept running.
- Edge reads `--window-size`/`--window-position` in DIPs: divide the UNO rectangle by
  `(monitor DPI / 96) × (TextScaleFactor / 100)`; a second launch into a running instance ignores
  the flags, so the page's own `resizeTo`/`moveTo` is the only way to place a second window.
- Raise from a background process failed (`AllowSetForegroundWindow` and `SetForegroundWindow` both
  refused, error 5); raising from a foreground LibreOffice was not observed. New windows did not
  steal the foreground.
- The bootstrap file works in Edge (no address bar, `history.length == 1`, `window.close()` works).
