# Sidecar for LibreOffice 0.5.1 — final plan

## Owner decisions (2026-11-21)

This section is binding and supersedes every pending question, default-on recommendation, fullscreen-pause promise and historical verdict in the preserved refined plan below. Jonathan: **“build, following off”**. Lanes are authorized to build; D2 is not a product acceptance pass.

- **Q1:** Follow LibreOffice ships **OFF by default on macOS**, with a Settings switch. Absent, invalid or unavailable stored preference means OFF; only `localStorage["sidecar.follow"] = "on"` opts in. Turning it off stores `"off"`. This is page preference, not capability: an eligible macOS app launch still negotiates `follow: true` unless the extension kill switch is set, so the Settings switch can be shown. Signed `follow: true` authorizes transport, not automatic page movement. No new plugin setting. A future default-on promotion requires the retests to pass **and Jonathan's approval**.
- **Q2:** Median ≤150 ms and p95 ≤250 ms with 1–2 windows; up to about 600 ms for fifth and later windows. Preserve the remaining G6 limits and measured sampling intervals.
- **Q3:** Any hand move OR resize undocks. Sticky until Reattach or a new session; Reattach restores the 400-point width.
- **Q4:** Yes, the Settings preference is remembered across sessions in the dedicated browser profile.
- **Q5:** No second or Retina display is available. Ship explicitly untested, with pause-and-Reattach behaviour; do not imply hardware verification.
- **Q6:** Approve §6's 22 in / 9 out. Windows keeps the default-browser tab; Chrome-first app-window work is a 0.5.2 spike. Manual VoiceOver and NVDA are not required; automated accessibility remains required.
- **Q7:** Jonathan runs one bounded real-gateway chat on macOS on the candidate (G9). This Day-0 task does not run a gateway or model.
- **Q8:** If following is not green at integration, hold up to two working days; then ship with following present but OFF, provided safety gates pass. OFF is the shipping default regardless.

### D2 outcomes and frozen constants

The supplied `day0/D2-REPORT.md` is investigation evidence, not installed-product acceptance. (a) Shared transport passed its bounded stress: park cap **4**, turns first/preempt a window waiter; normal unparked retry **250 ms**; poll-only fallback **100 ms**. (b) Measured one-window and fifth-reader latency passed; a distinct two-window native run was not performed. Worker: immediate first send, subsequent **100 ms minimum spacing**, one in flight, latest-value trailing edge; own channel renewed before 48 calls or 90 s; whole-call deadline 1 s. (c) Timestamp-only settle falsely undocked; corrected read-back guard passed 140 placements plus a burst and 10/10 Chrome hand-style changes/Reattach. Undock is **>8 points**, unchanged screen measurements, **400 ms PLUS completed-read-back guard**. Suppress comparisons while the placement read-back is pending; cancel/restart it on each own placement. (d) No reliable pure-UNO signal qualified both full-screen modes: **treat fullscreen as an ordinary large window**, never promise a fullscreen pause or Space coupling. (e) 1,866 callbacks, max **11.4262 ms** (two >5 ms): NOT PASS. The extension listener must do minimal O(1) main-thread work and hand off to the worker, with no network/file/logging or worker-I/O lock and no worker UNO access. C1/C2 must measure actual callback wall time: **p100 ≤5 ms over ≥1000 callbacks**; do not waive scheduling outliers. (f) Actual document drags **0/3 displaced**, unqualified due to Welcome/floating-window interception; V must dismiss Welcome, dock floating windows and prove actual document displacement in its retest harness. (g) Second display/Retina unavailable. Geometry floor **240×240**; minimized/hidden carry null bounds, hidden latched until shown. Final no-AX quit **exit 255 unexplained**: V must capture exact-PID exit 0/no crash/no restore prompt; absence of a crash report does not pass quit.

Protected tests stay byte-identical. Retain all stop rules; the owner authorizes optional/off development despite D2(e), not a waiver of listener cost, real-drag or clean-quit acceptance. G6 starts by verifying the OFF default and explicitly opting in; ordinary fullscreen behaviour replaces its fullscreen-pause expectation. Following OFF must issue no page window polls or geometry changes. Lane ownership is exactly §7.2. All six lanes fork only at the completed Day-0 head.

---

# Sidecar for LibreOffice 0.5.1 — refined plan (Dev Critic)

- **Mode:** CRITIC (refinement of `PLAN-DRAFT.md`, `BACKLOG.md`, `spikeD/SPIKE-D.md`, `spikeW/SPIKE-W.md`). Analysis only; this file is the only thing written.
- **Date:** 2026-11-21. **Reviewed against:** worktree `sidecar-libreoffice-0.5.1`, branch `dev/0.5.1`, commit `4d65e1ecc5eba83470cc7f81f0fa78a8af172b33` (= published v0.5.0), clean before and after my read; `docs/web-protocol.md` revision 4; the 0.5.0 `PLAN-FINAL.md` and `LANE-RULES.md`, whose binding constraints still hold.
- **Evidence tags:** `[OBS]` observed by me today, `[SRC]` read in source at `4d65e1e`, `[SPIKE]` measured by spike D or W (not re-run by me), `[DOC]` current primary documentation, `[ASM]` assumption, `[UNK]` unknown.

---

## 0. Plain-English summary for Jonathan

**What you get in 0.5.1.** On a Mac, the Sidecar window follows its LibreOffice window when you move or resize it. It is a companion window that catches up in about a tenth of a second, not one glued-together window. Drag Sidecar away and it stays where you put it; a Reattach button brings it back. While LibreOffice is minimised or full screen, Sidecar stays put and says following is paused. Windows keeps the browser tab exactly as in 0.5.0. Alongside this come 22 small fixes, checks and documentation updates.

**What I changed in the design, and why.**

1. **The fast version from the spike would have broken Stop with three or more windows.** The 114 ms figure you were quoted came from a prototype that holds one extra open connection per window. The browser allows six in total, shared by all Sidecar windows. I kept the fast path but made it share the four waiting slots that already exist, with replies first. One or two windows get the fast path; with many windows open, the extra ones follow in quarter-second steps. Stop keeps the same headroom as in 0.5.0.
2. **A new extension must still work with an old plugin, and the other way round.** People update the two halves separately. The spike's message change would have made a 0.5.1 extension unable to open Sidecar at all against a 0.5.0 plugin. I fixed the order: the plugin first says it can do following, and only then does the extension ask for it.
3. **"Drag away to undock" was not built or tested in the spike.** With following on by default, getting this wrong means the window snaps back and fights you. I specified a simple rule and made it a Day-0 test that must pass before the lanes start.
4. **Minimising LibreOffice made the prototype jump to the screen edge.** Fixed by never sending a "window has no size" position.
5. **Two displays will probably not follow across.** Chrome itself keeps a page from moving its window to another display unless the user grants a permission. The safe behaviour is: Sidecar pauses, tells you LibreOffice is on another display, and you move it there once and press Reattach. Nobody has tested this on real hardware.
6. **From the 31 backlog items: keep 22, move 9 out.** Out: the Windows app window and everything that only made sense with it, the manual screen-reader passes you dropped, and three checks that need installs or setting changes on your machines.

**Pending your confirmation (the plan assumes yes):** (1) follow on by default on macOS, drag-to-undock, Reattach, pause on minimise and full screen; (2) Windows stays a tab, Chrome-first app window is a 0.5.2 spike; (3) the backlog set, now 22 items.

**Not verified by anyone:** two displays, Retina (2×) screens, the green-button full screen, Intel Macs, Edge and Brave on macOS, and how a long hand-drag feels (the spike moved the window by script).

**Timing.** One Day 0 (contract, three small shared commits, one more short spike), then six lanes in parallel. Five working days if nothing needs a second round; plan for seven, because 0.5.0 needed four review rounds.

**Your decisions:** section 13, eight questions, each with my recommendation.

---

## 1. Frozen scope

| | |
| --- | --- |
| Outcome | 0.5.1 = macOS "Follow LibreOffice" (E01) + 22 bounded backlog items; Windows behaviour unchanged |
| Owner decisions, **pending confirmation** | (1) follow ON by default, drag undocks, Reattach, suspend on minimise/full screen, disclosed that Sidecar does not minimise with LibreOffice; (2) Windows keeps the tab, Chrome-first app window is a 0.5.2 spike; (3) the recommended backlog set |
| Binding | Manual VoiceOver not required; automated accessibility checks stay. `tests/test_run_ownership.py` and `tests/test_chat_lifecycle.py` byte-unchanged. Parallel lanes. All 0.5.0 `PLAN-FINAL.md` constraints (R1–R20, D1–D4, lane rules) unless changed here |
| Exclusions | Windows app window, Linux, live streaming, history, in-process native geometry, osascript or Accessibility bounds setting, any new helper binary, any new browser permission prompt |

---

## 2. Forecast

**Unchanged proposal** (lanes build spike D's "proposed protocol additions" as written, with the draft's 31 items): **LIKELY_FAIL**, moderate-to-high confidence. It fails at the mixed-version gate (F2), at the connection budget with three or more windows (F1), and ships an unqualified undock rule as the default experience (F5).

**With sections 4–5 applied and Day-0 spike D2 green:** **CONDITIONAL_PASS**, moderate confidence. Invalidated if D2 cannot keep Stop under one second at 8 sessions and 4 turns in either park mode, or if the undock rule produces a false undock or a snap-back in the D2 run.

---

## 3. What I verified today

| # | Fact | How |
| --- | --- | --- |
| V1 | `/launcher/launch` accepts exactly the keys `document, host_scheme, mode, bounds`; any other key answers a signed 400. Only `/api/chat` has an optional key (`_OPTIONAL_KEYS`). | `[SRC]` `plugin/web_api.py` `_BODY_KEYS`, `_shape`; `plugin/web_auth.py` `parse_object` |
| V2 | The 0.5.0 launcher reads hello and launch answers with `.get`, so extra answer fields are ignored. | `[SRC]` `sidecar_launcher.py` `hello()`, `launch()` |
| V3 | A channel allows 64 calls and 120 s. The 65th call is refused with an unsigned 401 and the channel is deleted; the launcher shows that as *not your Hermes* unless it is the first call. | `[SRC]` `web_auth.Channels.verify`, `LauncherClient.call` |
| V4 | At most `MAX_ACTIVE` (4) requests are parked at once; a fifth turn long-poll is answered at once, unparked. Parked waits share one condition variable and are woken at unload. | `[SRC]` `web_api._long_poll`, `close_begin`, `close_end` |
| V5 | Only successful `api-state` and `ui-static` answers log at DEBUG. The list (`QUIET_LABELS`) lives in `plugin/chat_bridge.py`. Every other answer logs one INFO line. | `[SRC]` `chat_bridge.py` lines 109–111, 1349 |
| V6 | `compute_bounds` replaces a frame with zero width or height by a work-area fallback. A minimised window reports 0×0 `[SPIKE]`, so the prototype jumped to the screen edge. | `[SRC]` `sidecar_launcher.compute_bounds` |
| V7 | The page has exactly one `resizeTo` and one `moveTo` call site (`alignWindow`), pinned by `tests/test_web_bundle.py`, which also forbids `localStorage` in `app.js` and pins the set of `/api/` routes the page names. | `[SRC]` `test_web_bundle.py` lines 42, 382–389, 446–448 |
| V8 | The `.oxt` file list is pinned three ways: `scripts/build_oxt.py`, `manifest.xml`, and protocol section 8.1 (compared by `test_packaging.py`). The README hash test has a placeholder mode worded for v0.5.0. | `[SRC]` |
| V9 | The document-close hook calls the launcher from a UNO document event (`OnUnload`); the panel object, not the frame, currently owns the only window listener, and the panel can be disposed while the Sidecar window lives. | `[SRC]` `hermes_bridge._install_launcher_hooks`, `hermes_sidebar.LauncherPanel` |
| V10 | Window Management specification: without the `window-management` permission, "requests to place content on specific screens will be clamped to the current screen"; a Chromium bug quoted on Stack Overflow says Chrome clamps `moveTo` to the current screen. | `[DOC]` W3C Working Draft 2026-11-10, sections 3.8 and 4. Not tested on hardware. |
| V11 | Chrome does not throttle timers in visible pages; hidden pages get one wake-up per second, and once per minute after five minutes. | `[DOC]` developer.chrome.com, timer throttling in Chrome 88 |
| V12 | My own implementation of the section 3.3 formulas reproduces all five published vectors, so the new vectors in 5.4 are computed the same way. | `[OBS]` in-memory Python, nothing written |
| V13 | Protected files today: `a600cb01d6be05ee3025410fd222929678636f41fed2a410f2e59cad3e11d860` (`test_run_ownership.py`), `259b1e2e4225967a8889418e0074ac785b9a794cdc6f515e1423d9ae10c01227` (`test_chat_lifecycle.py`). Same as the 0.5.0 final gates. | `[OBS]` `shasum -a 256` |

Not run by me: the test suite (it would write caches into the read-only worktree), any LibreOffice, any browser, anything on ATLAS. Spike numbers are quoted from the spike reports and `spikeD/evidence/analysis.json`, which I read.

---

## 4. The docking design, challenged and fixed

Decisions are numbered F1–F12. Section 5 turns them into contract text.

### F1. Connection budget: one page behaviour, the plugin chooses push or poll

- **Problem.** The 114/189 ms numbers are from a separate parked request per window (`/api/dock`) `[SPIKE]`. All Sidecar windows share one browser profile and its six connections per origin. Eight windows would park eight follow requests on top of four turn long-polls; Stop and Send then queue behind them. The spike's safe variant polls `/api/state` every 100 ms per window forever: 80 requests a second at eight windows, each a new connection and thread, each running the janitor sweep and the readiness check under the global lock, for 218/326 ms and 3 of 50 placements not landed in time `[SPIKE]`.
- **Decision.** A new page route `GET /api/window?seq=<n>`. The plugin answers at once when it has a newer snapshot. Otherwise it parks the request for at most 25 s **only if a slot is free in the existing pool of 4 parked requests**, which turn long-polls and window polls now share. A turn long-poll always gets a slot: if the pool is full, one parked window poll is answered at once to make room. A window poll that gets no slot is answered at once with `retry_ms: 250`, and the page waits that long before asking again.
- **Why this shape.** The numbers in protocol section 12 do not change: never more than 4 parked, so at least 2 free browser connections and at most 4 parked handler threads. `/api/turn`, `/api/state` and `/api/chat` are not touched, so the most-reviewed code of 0.5.0 keeps its behaviour. The page does the same thing in both modes (it obeys `retry_ms`), so the plugin can fall back to poll-only by a constant, without a page change.
- **Launcher side.** One follow worker per LibreOffice process, one request in flight at a time, at most 10 a second, zero when nothing moves.
- **Proof needed.** D2 on Day 0, then G2: 8 sessions, 4 hanging turns, 30 s of bounds at 10 Hz. Every Stop answered within 1 s, a ninth page load served, zero `endpoint_busy`.

### F2. Capability negotiation: a new extension must work with an old plugin

- **Problem.** Spike D adds `frame_id` to the launch request. A 0.5.0 plugin answers any extra key with 400 (V1), so a 0.5.1 extension could not open Sidecar against a 0.5.0 plugin. The catalog updates the plugin; the user updates the `.oxt` by hand. Mixed pairs are the normal state for days.
- **Decision.** Three steps, each safe on its own:
  1. The hello answer gains `"caps": ["window_follow"]`. It is an unsigned hint, like `chat` in the same answer. Old launchers ignore it (V2).
  2. Only when the hint is present, on macOS, in app mode, and following is not switched off, the launcher adds the optional key `"follow": true` to the launch body.
  3. The **signed** launch answer carries `"follow": true`. Only that starts following. A forged hint therefore changes nothing: the forger cannot sign the answer.
- **Pairs.**

| Extension | Plugin | Result |
| --- | --- | --- |
| 0.5.1 | 0.5.1 | follows |
| 0.5.1 | 0.5.0 | no `caps`, no new key, exactly 0.5.0 behaviour |
| 0.5.0 | 0.5.1 | no `follow` key; the exchange says `follow: false`; the page shows no follow control and never calls `/api/window` |
| 0.4.0 | 0.5.1 | legacy `/chat` only, unchanged |

The page and the plugin are always the same version (the plugin serves the page), so there is no page skew `[SRC]`.

### F3. No new identifiers on the wire

- **Problem.** The spike adds `frame_id` (extension-minted) and `attachment_id` (plugin-minted) with binding rules between them.
- **Decision.** The attachment is the **`launch_id`** that already exists: random 128 bits, minted per launch, returned only in a signed answer, already stored on the session (`Session.launch_id` `[SRC]`), never shown to the page. The launcher keeps `launch_id → frame` locally. `frame_id` is dropped: the plugin has no use for it.
- **Consequence that is a security property:** moving a window needs both the pairing token (to sign) and the `launch_id` of that window. No route lists launch ids, and the `window_live` answer must keep returning `launch_id: null`.

### F4. Event coalescing and the channel

- **Problem.** The prototype's 50 ms tick uses up a 64-call channel in 3.2 s; the 65th call would be shown as *not your Hermes* (V3). Each accepted call would log an INFO line (V5). The pipeline delivered about 6 placements a second end to end `[SPIKE]`, so 20 a second buys nothing.
- **Decision.**
  - The UNO callback (LibreOffice's main thread) only copies the rectangle and the state into a latest-value slot and sets an event. No network, no file, no lock that a worker holds during I/O. An unchanged rectangle and state is dropped there.
  - The worker sends the first change at once, then at most one request every 100 ms, and always sends the last value (trailing edge). It never reads UNO.
  - The worker owns its own channel, never the click, release or quit channel. It opens a new one before 48 calls or 90 s. Whole-call deadline 1 s.
  - Any failure drops that channel. A refused connection, a signed 409, or a failed proof unbinds following for that window until the next Open Sidecar; nothing is shown in the panel. The busy refusal keeps the latest value and backs off (250 ms, 500 ms, 1 s; five in a row unbind).
  - Each send carries a per-window `seq` that only grows, so a request that arrives late cannot overwrite a newer one.
  - `launcher-bounds` and `api-window` join the DEBUG list. That is one line in `chat_bridge.py`; it lands in the Day-0 seam so the file stays frozen afterwards.

### F5. Undock detection (not built in the spike; default-on makes it critical)

- **Problem.** The prototype snapped a hand-moved window back at the next LibreOffice move `[SPIKE]`. The spike proposes a timer comparing positions but did not build it. Browsers have no "window moved" event.
- **Decision (page rule).**
  - After each of its own placements the page waits a settle time (400 ms, to be confirmed by D2) and then **reads back** where the window actually is, with the current screen's measurements. That is the *placed* rectangle. It is observed, never computed, so clamping by the system is already in it.
  - Before applying a newer snapshot, and on the existing 3 s tick and on `resize`, the page compares the window with *placed*. More than 8 px off in position or size, with unchanged screen measurements and no placement of its own in the settle time, means the user moved or resized it: the page **undocks** and does not move.
  - Changed screen measurements (resolution or display change) re-baseline instead of undocking.
  - Undocked is sticky until Reattach or a new session. While undocked the page calls neither `resizeTo` nor `moveTo` and stops asking `/api/window`.
  - Reattach asks `/api/window?seq=0`, which answers at once with the latest snapshot, applies it and docks again.
- **Residual, accepted and disclosed:** a drag that starts within the settle time of a LibreOffice move can be snapped back once.
- **Proof needed.** D2 and G6: zero false undocks in 70 scripted placements plus a burst; 10 of 10 hand drags leave the window where it was put; Reattach lands exactly.

### F6. Minimise, full screen, hidden

- Minimise: `windowMinimized` → state `minimized`, no rectangle. `windowNormalized` → a fresh `normal` snapshot. A rectangle narrower or lower than 240 is never sent as `normal` (this is also the existing rectangle rule). The launcher's work-area fallback is not used for following (V6).
- Hidden (`windowHidden` on the top window): state `hidden`.
- Full screen: state `fullscreen` **if D2 finds a reliable pure-UNO signal** for LibreOffice's own full screen and for the green button. If not, full screen is treated as an ordinary large window (Sidecar overlaps the right edge, which is what launch does today on a maximised window) and the release notes say so. The page never raises, focuses or activates itself, so it cannot switch Spaces.
- The page shows one status line for a paused state and does nothing else. It cannot minimise itself; the docs say Sidecar does not minimise with LibreOffice.

### F7. Coordinates, Retina, more than one display

- macOS uses one global point space for all displays at any scale, and both UNO `getPosSize` and the browser's window coordinates are in points on the one display tested `[SPIKE, 1920×1080 at 1×]`. Whether UNO still reports points on a 2× display is `[UNK]`.
- **Cross-display.** Expect Chrome to clamp a page's `moveTo` to the window's current display (V10). The page must not ask for the `window-management` permission (a prompt in the Sidecar window is not acceptable) and the `Permissions-Policy` header stays byte-identical.
  - Page rule: when `screen.isExtended` is true and the target's centre is outside the current screen, do not move; show "LibreOffice is on another display. Move this window there, then choose Reattach." Otherwise clamp to `screen.avail*` as today.
  - Launcher rule: the "does not fit, overlap the right edge" branch of `compute_bounds` applies only when the frame's centre lies inside the work area UNO reports. Otherwise the target is simply right of the frame. This stops a frame on a second display from being pulled to the main display's edge (S2 saw `getWorkArea()` return the one attached display, menu bar included; what it returns with two displays is `[UNK]`).
- All of this paragraph is unverified on hardware. See section 11.

### F8. Stale bounds after a document switch

- Following is bound to **the frame that launched the window**, never to "the current document". Switching between two LibreOffice windows produces events only from the window that moves.
- The listener is owned by a small registry keyed by document, not by the sidebar panel (V9): closing the Sidecar deck must not stop following.
- It ends on any of: the frame's window is disposed, the launcher is told the document closed (also when another document is loaded into the same frame), LibreOffice quits. The worker checks the binding under its lock right before every send. A send that still slips out gets a signed 409 because the session is gone.
- A second window on the same document (Window ▸ New Window) does not take over. If the followed window is closed while the document stays open, the state becomes `window_closed` and following ends for that Sidecar. Documented limit; same-document multi-frame was not tested `[SPIKE]`.
- After a LibreOffice crash the old Sidecar window cannot be re-bound (the new process does not know its `launch_id`). Documented: close it and open again.

### F9. Security of the new signed route

| Check | Rule |
| --- | --- |
| Gates | `/launcher/bounds` inherits the launcher family: exact Host, no `Origin`, no `Sec-Fetch-*`, no session header, channel MAC over method, path, counter and body hash, signed answers |
| Body | exactly `launch_id`, `seq`, `state`, `bounds`; integers are not booleans; the existing rectangle limits; `bounds` non-null exactly when `state` is `normal`; 16 KiB limit as for every POST |
| Authority | pairing token **and** knowledge of the window's `launch_id` (F3) |
| Replay and order | channel counter and MAC; `seq` must grow |
| Lifetime | only for a live session whose launch asked for `follow`; never revives an ended session; dropped at session end and at unload |
| Storage | latest value only, one per session (at most 8); no queue |
| Logs | fixed labels; no rectangle, id or title; DEBUG on success |
| No browser route writes bounds | the page can only read its own session's snapshot through `/api/window`, behind the `/api/*` gates |
| Model and document text | no path from a reply or a document to a window position |
| Old plugin | the route does not exist: unsigned 404 before any MAC; a 0.5.1 launcher never calls it without the signed `follow: true` |

A program that already holds the pairing token can do everything the user can (unchanged 0.5.0 trust statement), so the route adds no new class of actor.

### F10. What the page may and may not do with bounds

May: validate; call `resizeTo` then `moveTo` on its own window from the single existing call site, skipping a call whose values would not change; read back its own position; show a status line.

Must not: act in tab mode; act after its session ended; act while undocked, paused or switched off; act on an older `seq`; call `window.open`, `focus`, `blur`, `requestFullscreen` or `getScreenDetails`; navigate; write bounds to storage, the URL, the title, a log or any request; send its own position to the plugin; start a second timer chain for following (the parked request is re-issued from the network callback).

### F11. The user's switch

- Settings gains one checkbox, **Follow LibreOffice**, on by default, shown only when the exchange says `follow: true`. It is remembered in the dedicated browser profile as `localStorage["sidecar.follow"] = "off"` (absent means on). This needs the bundle test and protocol 7.2 to allow that one key.
- Extension kill switch for support: `HERMES_LO_SIDECAR_NO_FOLLOW=1` (the launcher then never sends the `follow` key), next to the existing `HERMES_LO_SIDECAR_NO_NATIVE`.
- No plugin setting and no `plugin.yaml` schema change.

### F12. What is deliberately not in 0.5.1

Remembering a user-chosen width while following; Open Sidecar in LibreOffice re-docking an undocked window; re-binding to another frame; following across Spaces; quietening the launcher while a window is undocked (the cost is a few requests during a drag).

---

## 5. Contract change list for `docs/web-protocol.md` (revision 5, frozen on Day 0)

Everything not listed stays byte-identical. Numbers marked *D2* are confirmed or changed by the Day-0 spike before the freeze.

**Head.** Add a "Revision 5 (0.5.1)" paragraph naming the changes below.

**1.1 Route table.** Two rows:

| Method | Path | Caller | Credential | Label |
| --- | --- | --- | --- | --- |
| POST | `/launcher/bounds` | 0.5.1 launcher | channel MAC | `launcher-bounds` |
| GET | `/api/window?seq=…` | page | session header | `api-window` |

**2 Identifiers.** Add: *Window state* `"normal" | "minimized" | "fullscreen" | "hidden" | "window_closed"`. *Window snapshot* `{"seq": int, "state": <window state>, "bounds": <Rectangle> | null}`; `bounds` is non-null exactly when `state` is `normal`; `seq` is `0` before the first accepted bounds call, otherwise `1..2147483647`.

**3.1 Hello.** The 200 answer may carry `"caps": [<strings>]`; 0.5.1 sends `["window_follow"]`. It is unsigned and only permits the launcher to send the optional `follow` key (4.1). Unknown entries, a missing field or a wrong type mean no capabilities.

**3.2 Calls.** Add "Follow worker's channel": its own channel, a new hello before 48 calls or 90 s, a 1 s whole-call deadline, no in-place retry, the failure rules of F4. State that a follow failure never changes a panel text and never starts or raises anything.

**3.3 Test vectors.** Add (same token, port, nonces and channel key as today; computed with the published formulas, V12; both sides must reproduce them on Day 0):

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

**4.1 Launch.** Optional body key `"follow": true` (present with that value or absent; `false`, or `follow` with `mode: "tab"`, is 400 `bad_request`). The 201 answer gains `"follow": true | false`; the 200 `window_live` answer gains `"follow": false` and still returns `launch_id: null`. The code is also bound to `follow`.

**4.3 Release.** Wire unchanged. Add the E03 rule: a `document_closed` release that got no answer or the busy refusal is sent again on a new channel 0.5 s, 1.5 s and 3.5 s after the close; never after quit began; never after a signed answer.

**4.4 (new) `POST /launcher/bounds`.** Body exactly `{"launch_id": "<22>", "seq": <int>, "state": <window state>, "bounds": <Rectangle> | null}`. Accepted when a live session has that `launch_id`, was launched with `follow`, and `seq` is greater than the session's last one: the plugin stores the snapshot (replacing the previous one), wakes that session's window poll, and answers signed 200 `{"ok": true, "seq": <n>}`. Otherwise signed 409 `attachment_stale` (unknown id, ended session, launch without `follow`, `seq` not greater) or signed 400 `bad_request`. The launcher sends the first snapshot when the launch state becomes `exchanged`.

**6.1 Exchange.** The answer gains `"follow": true | false` (true only for an app-mode launch that asked for it).

**6.9 (new) `GET /api/window?seq=<n>`.** Query exactly `seq`, once, same number form as 6.4. Answers 200:

```json
{"ok": true, "superseded": false, "retry_ms": 0,
 "window": {"seq": 17, "state": "normal", "bounds": {"x": 1190, "y": 260, "width": 400, "height": 560}}}
```

- At once when the session's snapshot `seq` is greater than the query's.
- Otherwise parked for at most 25 s if fewer than `MAX_ACTIVE` requests are parked (turn long-polls and window polls counted together), then answered with the unchanged snapshot and `retry_ms: 0`.
- Otherwise at once with the unchanged snapshot and `retry_ms: 250` *D2*.
- A turn long-poll that finds the pool full takes the slot of one parked window poll, which is answered at once with `retry_ms: 250`.
- One window poll per session: a newer one answers the older with `superseded: true`.
- `retry_ms: 0` means "ask again from the network callback"; the page clamps other values to 100–5000 ms.
- 404 `no_follow` for a session without `follow`. Session end, unload and the `/api/*` gates as on every page route.
- A parked window poll counts as the session being seen (4.1 `window_live`). It does not count as a turn long-poll for the 90 s *window gone* rule.

**6.8 Lifecycle.** The snapshot is dropped when the session ends.

**7.2 Page rules.** Add the rules of F5, F7 and F10 as normative text, with the constants: settle 400 ms *D2*, tolerance 8 px, one `resizeTo` and one `moveTo` call site, resize before move when both change. `localStorage` keeps the theme under `omnia.theme` and the follow preference under `sidecar.follow`, nothing else. The page asks `/api/window` only in app mode, with `follow: true`, the preference on, docked, and while visible.

**7 Headers.** Unchanged. State explicitly: the page never requests `window-management`.

**7.3 DOM ids.** Add `follow-status`, `reattach-button`, `follow-toggle`. They exist once, outside `#messages`.

**8.1 `.oxt` contents.** Add `python/window_follow.py   new: follow state, coalescing, worker (no UNO)`.

**8.2 Panel texts.** Add one row (E04): the launch or browser cache folder is refused as unsafe → a fixed sentence that says so and names a README section (the exact words are fixed on Day 0 with lane D, because `test_manifest.py` checks the section exists). No panel text exists for follow failures.

**8.3 Bounds.** Add: following uses the same formula for every event; the overlap branch only when the frame's centre is inside the work area (F7); a rectangle under 240 in either size is never sent as `normal`. Record spike D's numbers and the single-display limit.

**9.1 Codes.** Add `attachment_stale` (409), `no_follow` (404).

**9.2 Page texts.** Add: "Sidecar is not following LibreOffice." with the Reattach control; "Following is paused while LibreOffice is minimised."; "…is in full screen."; "…is hidden."; "The LibreOffice window this Sidecar followed was closed."; "LibreOffice is on another display. Move this window there, then choose Reattach." Lane B may propose shorter wording before the freeze; after it the strings are fixed.

**11 Log labels.** Add `launcher-bounds`, `api-window`; both at DEBUG when the status is below 400.

**12 Connection budget.** Replace the first two sentences: at most `MAX_ACTIVE` (4) requests are ever parked, turn long-polls and window polls together, turn long-polls first; the launcher's follow worker makes at most one request at a time and at most ten a second.

**13.1 / 13.2 Interfaces.** `chat_bridge.QUIET_LABELS` gains the two labels (no other change to that file). New module `window_follow.py`:

| Name | Contract |
| --- | --- |
| `Follower.attach(document_id) -> handle` | main thread, at the click; one handle per document |
| `handle.update(rect, state, work_area)` | main thread, from the UNO callbacks; O(1); never blocks; drops an unchanged value |
| `handle.bind(launch_id, port)` | from the launch worker, after the signed `follow: true` and state `exchanged`; a new bind replaces the old one and restarts `seq` at 1 |
| `handle.close()` | document closed, window disposed or quit; nothing is sent afterwards |
| `handle.alive` | the UNO listener removes itself when this is false |
| `Follower.stop()` | quit: no further request is started |

`Launcher.open(...)` gains `follow=handle | None`. The UNO listener class (`XWindowListener` + `XTopWindowListener`) lives in `hermes_sidebar.py`, in a registry keyed by document, not on the panel. The adapters of 13.2 do not change; on Windows nothing attaches and the `follow` key is never sent.

**14 Notes.** Replace the "S3 findings kept for 0.5.1" heading with "for 0.5.2" and add spike W's results (implicit sign-in flag works on the tested Edge build; placement exact by physical pixels at 112 % text scale; Startup Boost and raise unresolved; Chrome preferred). Add the list of section 11 of this plan.

---

## 6. The 31 backlog items: keep 22, move 9 out

Order within a lane is the order to build in.

### Keep (22)

| ID | Lane | Note |
| --- | --- | --- |
| E05 | C1 | First: whole-string matches for the URL, nonces, launch id and bootstrap name (`fullmatch`). No behaviour change for a correct plugin. |
| E04 | C1 | New panel sentence for an unsafe cache folder; the refusal itself stays. **Changes a text users see.** |
| E06 | C1 | Reap exited hand-off children; never the tracked live browser PID. |
| E01 | A, B, C1, C2, V, D | The headline. **Changes behaviour users rely on:** the window now moves by itself and, when following, returns to 400 points wide beside LibreOffice. The first hand move or resize stops that. |
| E03 | C1 | After the follow wiring, same file. Bounded retry of a lost close release (protocol 4.3). **Changes behaviour:** a Sidecar window whose close message was lost now closes within about 4 s instead of lingering. That is the intended fix. |
| E16 | C1 | Comment only; rides with the `.oxt` rebuild. |
| P03 | B | First page commit, alone, with its regression test, so a follow bug cannot be mistaken for it. |
| P02 | B | Second page commit. **Changes two sentences users see** and that the browser harness asserts; lane V updates its expected strings in the same integration step. |
| T02 | V | Trimmed: the stub's two new routes first (they unblock lane B on day 1), then the five known divergences, time-boxed to half a day. The real plugin stays the authority. |
| T03 | V | Evidence fields come from captured values. Needed for the latency numbers to count. |
| T05 | V | Trimmed: the real-plugin browser run becomes a mandatory local release gate. A CI browser job is **not** added in 0.5.1 (it would put Chrome flakiness between you and 6/6). Exercise the external-target path once, in G6. |
| T06 | V | Exact PIDs, crash records, content-based sentinels. Required by the native run. |
| D01 | D | After the page freeze. Windows screenshots do not change. |
| D02, D03, D04, D05 | D | As in the backlog. D05 gains the follow limits of section 11. |
| V09 | owner | G9, scoped in section 9. Needs your authorization. |
| V10 | V | Upgrade 0.5.0 → 0.5.1 plus the four mixed pairs of F2. The public `hermes plugins update` step stays pending until the pin exists. |
| V13 | V | Trimmed: reload, duplicate, back-forward cache and spent-code checks automated in Chrome tab mode; Safari keeps its open/turn/close smoke. |
| V14 | V | Time-boxed half day, in parallel, not blocking unless it reproduces. It matters more now that the tab stays the Windows path. |
| V17 | integrator | Private CI 6/6, packaging, scanner. Mandatory. |

Also taken although not among the 31, because they are cheap and the work touches the same code: the deterministic idle-expiry test against the real session code (T07's prerequisite; lane A, the clock is already injectable `[SRC]`), and P11 (the follow indicator), which is now part of E01.

### Move out (9)

| ID | To | Why |
| --- | --- | --- |
| E02 | 0.5.2 spike | Owner decision 2. Raise and Startup Boost are unresolved `[SPIKE]`. |
| E07 | 0.5.2 | Raising one document's window needs window targeting that neither spike demonstrated. A followed window sits beside its document anyway. Keep the precise "raises the browser" wording. |
| V01 | dropped | Your decision; automated accessibility checks stay and gain follow scenarios. |
| V02 | 0.5.2 | Its reason was the new Windows window. Record "not tested with NVDA". |
| V03 | later | Needs a Firefox install on the Mac. Record as untested unless you authorize it. |
| V05 | split | Windows 150 % goes with E02. macOS 2× and second display stay **explicitly unverified** unless hardware is available for one G6 pass (question Q5). |
| V06 | later | Needs changing the default browser on ATLAS. The regression run uses the handler that is set. |
| V11 | optional inside G7 | If the ATLAS session has time: disable and enable the plugin while a page polls. Not release-blocking. |
| V16 | wording | Do not run the computed-style comparison; lane D removes the word "exact" and says "the same pinned Omnia styling" (question Q6). |

---

## 7. Day 0 and the lanes

`design-0.5.0/LANE-RULES.md` applies unchanged except for the paths: integration worktree `sidecar-libreoffice-0.5.1` on `dev/0.5.1`; scratch under `…/cache/scratch/051/<lane>/`; evidence under `design-0.5.1/`. Only the integrator and the Day-0 owner commit to the worktree. Lanes deliver patches.

### 7.1 Day 0 (one owner; nothing forks before it is done)

| Step | Content | Gate |
| --- | --- | --- |
| **C0** contract freeze | `docs/web-protocol.md` revision 5 exactly as section 5, with the *D2* numbers filled in | both sides reproduce the three new vectors |
| **S1** plugin seam | `plugin/chat_bridge.py`: `QUIET_LABELS` gains `"launcher-bounds"`, `"api-window"`. One line. | both protected files byte-identical (V13 hashes) and green; full suite count recorded. After S1 the file is frozen for everyone. |
| **S2** extension seam | new `extension/python/window_follow.py` (licence header, docstring, the 13.2 names as stubs); its entry in `scripts/build_oxt.py`, `extension/META-INF/manifest.xml` and protocol 8.1 | `test_packaging.py` green |
| **S3** version seam | `extension/description.xml` and `plugin/plugin.yaml` to 0.5.1; both READMEs to the hash placeholder; `CHANGELOG.md` gets `## [0.5.1] - Unreleased`; the placeholder strings in `tests/test_packaging.py` say v0.5.1 | suite green. From here the `.oxt` bytes depend only on code, so native evidence can name a hash. |
| **D2** spike | In scratch copies, disposable LibreOffice profile, own Chrome profile, spike D's harness: (a) the shared park pool with 8 sessions, 4 hanging turns, 10 Hz bounds for 30 s; (b) latency with send-at-once plus 100 ms spacing, parked and unparked; (c) the undock rule; (d) what UNO reports for LibreOffice full screen and the green button; (e) the cost of one callback on LibreOffice's main thread; (f) three real pointer drags of the LibreOffice title bar, guarded by exact PID; (g) a 2× or second display if one can be attached | `design-0.5.1/day0/DAY0-REPORT.md` with a yes or no per item and the resulting constants |

D2 outcomes that change the plan: (a) fails → the plugin constant goes to poll-only (`retry_ms` 100) and (a) is repeated; fails again → stop rule 2. (c) fails → follow ships off by default or is cut (question Q1). (d) no signal → full screen is treated as a large window (F6). (e) more than 5 ms per callback → stop rule 3.

### 7.2 Lanes and file ownership (no file is in two lanes)

| Lane | Owns (only this lane edits) | Delivers |
| --- | --- | --- |
| **C0 / integrator** | `docs/web-protocol.md`; integration commits; the acceptance ledger | freeze, amendments, G8 |
| **A. Plugin** | `plugin/web_api.py`, `plugin/web_auth.py`; `tests/test_web_api.py`, `tests/test_web_auth.py`, `tests/test_web_turns.py`, `tests/test_web_unload.py`, `tests/test_web_send.py`, `tests/test_web_static.py`, new `tests/test_web_window.py`; `tests/support/fake_runs_api.py` | `caps`; optional `follow` and its echo; `/launcher/bounds`; `/api/window` with the shared pool and turn priority; exchange `follow`; unload with parked window polls; deterministic idle expiry; G1 and G2 rows |
| **B. Page** | `plugin/sidebar/app.js`, `plugin/sidebar/index.html`, `plugin/sidebar/sidecar-lo.css`, `plugin/sidebar/SHA256SUMS`; `tests/test_web_bundle.py` | commits in this order: P03, P02, follow (poll loop, undock rule, Reattach, toggle, paused texts, other-display rule); sums regenerated; works against V's stub from day 1 |
| **C1. Extension core** | `extension/python/sidecar_launcher.py`, `extension/python/hermes_sidebar.py`, `extension/python/launcher_macos.py`; in `extension/python/hermes_bridge.py` only the exclusive-bind comment (E16); `tests/test_launcher.py`, `tests/test_sidebar.py`, `tests/test_launcher_macos.py`, `tests/test_launcher_windows.py`, `tests/test_windows_regressions.py`, `tests/test_extension_menu.py` | commits in this order: E05, E04, E06, follow wiring (`caps`, `follow` key, signed echo, bind at `exchanged`, the UNO listener registry, the F7 bounds rule, kill switch), E03, E16; a test that on Windows nothing attaches and no `follow` key is sent |
| **C2. Follow engine** | `extension/python/window_follow.py`, new `tests/test_window_follow.py` | the pure module of 13.2: latest-value slot, send-at-once plus spacing plus trailing edge, channel renewal, failure and back-off rules, `seq`, stop at quit; unit-tested with a fake client and clock |
| **D. Docs and release** | `README.md`, `plugin/README.md`, `plugin/after-install.md`, `SECURITY.md`, `CHANGELOG.md`, `NOTICE`, `plugin/NOTICE`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/**` except `web-protocol.md`, `catalog/atlas-sidecar-libreoffice.yaml`, `plugin/plugin.yaml`, `extension/description.xml`, `extension/README.txt`, `tests/test_manifest.py`, `tests/test_packaging.py`, `tests/test_license_headers.py` | draft early; D01–D05; the follow disclosures; **D-final** (hashes, date) only after the extension and page freeze |
| **V. Verification** (no product files) | `scripts/web_smoke/**` including new `follow_probe.py` and `follow_page.py`; evidence under `design-0.5.1/acceptance/`; the native harness under `design-0.5.1/acceptance/harness/` | stub routes on day 1; T02, T03, T05, T06; G1–G7 runs; V10, V13, V14 |

**Frozen for everyone** (a change needs an integrator amendment recorded in the ledger): `tests/test_run_ownership.py`, `tests/test_chat_lifecycle.py`; `plugin/chat_bridge.py` after S1; `plugin/__init__.py`; `extension/python/private_files.py`, `panel_registry.py`, `office_ops.py`, `range_logic.py`, `launcher_windows.py`; `extension/*.xcu`, `extension/dialogs/**`; `scripts/build_oxt.py` and `manifest.xml` after S2; `plugin/sidebar/vendor/**`, `omnia-*.css`, `taskpane.css`, `omnia-theme.js`, `UPSTREAM.json`; `.github/workflows/**`; every test file not named above.

### 7.3 Order and estimate

1. **Day 0:** C0, S1–S3, D2. V ports the two routes into the stub the same day.
2. **Days 1–2, in parallel:** A, B, C1, C2, D-draft, V harness. C1 builds against C2's frozen interface with a fake.
3. **Day 3:** I1 (A + B through the real plugin and the fake Runs API), then I2 (+ C1 + C2 in a disposable LibreOffice profile). Freeze extension and page bytes; record the `.oxt` hash and the sums.
4. **Day 4:** G1–G7 on the frozen candidate; one fix round for failures that cite a gate.
5. **Day 5:** D-final, private CI, G9 if authorized, release candidate.

Engineer-days, estimated, not measured: Day 0 1.5; A 1.5; B 2; C1 2; C2 1; D 1; V 3; integration and fixes 2. About **14**; elapsed **5 working days best case, plan for 7**.

---

## 8. Traceability

| Success criterion | Tasks | Evidence |
| --- | --- | --- |
| Sidecar follows on move and resize, within the threshold, no focus theft | F1, F4; A, B, C1, C2 | G6 steps 2–4, 14 |
| Dragging away undocks; Reattach returns it | F5; B | D2 (c), G6 steps 5–6, G3 |
| Minimise and full screen pause; no jump | F6; B, C1 | G6 steps 7–8 |
| Stop, Send and unload guarantees unchanged | F1; A; S1 | G2, protected hashes |
| Mixed versions keep working | F2; A, C1 | G1, V10 in G6/G7 |
| Windows unchanged | C1 test; V | G7 |
| No new secret or position leak | F3, F9, F10 | G1, G6 step 15 |
| Docs say what was tested | D03, D05; D | G8, section 11 list present |

---

## 9. Acceptance gates

Evidence: `design-0.5.1/acceptance/<gate>/<run>/` with the commit, `.oxt` hash, page sums, versions, exact PIDs, real or fake upstream, observed result and clean-up. States are PASS, FAIL, NOT RUN and OWNER-AMENDED; a dispatched run is not a pass.

| Gate | Passes when |
| --- | --- |
| **G0 Day 0** | Revision 5 frozen; S1–S3 landed with the protected hashes unchanged; `DAY0-REPORT.md` has an answer for D2 (a)–(f); the owner decisions of section 13 that block the lanes (Q1, Q3, Q4) are answered. |
| **G1 Authentication and boundaries** | All 0.5.0 rows still pass against the real plugin. New rows for `/launcher/bounds`: no MAC, wrong MAC, wrong counter, replayed call, `Origin` or session header present, each wrong body shape, boolean for an integer, rectangle out of range, `bounds` with a non-normal state and the reverse, unknown `launch_id`, ended session, launch without `follow`, lower or equal `seq`, call after unload. New rows for `/api/window`: no session, ended session, cross-site, extra or malformed query, a session without `follow`. Launch: `follow: false`, `follow` with tab mode, the echo. Hello: `caps` present; a 0.5.0-shaped launcher still gets its answers. No rectangle, id or title in any log line; the two labels at DEBUG. The three new vectors reproduced on both sides. Deterministic idle expiry on the real session code; the 12-hour run stays labelled not run. |
| **G2 Ownership, lifecycle, capacity** | Both protected files byte-identical and green. Pool: 4 parked window polls plus one turn long-poll → the turn parks and one window poll is answered with `retry_ms`; never more than 4 parked. Stress: 8 sessions, 4 hanging turns, 30 s of bounds at 10 Hz → every Stop answered within 1 s, a ninth page load served, zero `endpoint_busy`, zero unsigned launcher answers. Unload with 4 parked window polls returns inside the 10 s budget, each answers 503 once, no handler writes afterwards. Follow engine: no request after `close()` or `stop()`; a new channel before call 49; a 65th call never happens; a stalled plugin delays quit by nothing (the quit budget test still passes with a hung bounds call). E03: three bounded retries, none after quit, none after a signed answer, no effect on a reopened document. |
| **G3 Shipped page in a browser** | Real plugin, shipped page, fake Runs API, headless Chrome. All 0.5.0 scenarios. P03 regression: a state poll that fails after a successful Try again does not bring the banner back. P02 strings. Follow logic with the window functions replaced by recorders: resize before move; an older `seq` ignored; nothing after session end; no request while hidden, undocked or switched off; tab mode never calls a window function and never asks `/api/window`; the undock rule on scripted positions, including the re-baseline on a screen change; the other-display pause; Reattach; the toggle persists and nothing else is stored. Zero CSP violation events. V13: reload, duplicate, back-forward cache, spent code in tab mode. |
| **G4 Appearance** | The three Omnia files and the vendor files match their pins; sums clean. Screenshots at 400 px, light and dark, of the docked, undocked and paused states with no clipped control. The wording no longer says "exact" (or V16 is run, if you choose that). |
| **G5 Accessibility, automated** | The 0.5.0 set of 8 scenarios passes. New: Reattach and the toggle are named and reachable by keyboard; undocking is announced once, politely; following itself writes nothing to a live region; focus does not move on dock, undock or pause. No manual screen-reader pass is required. |
| **G6 Native macOS docking run** | The exact candidate `.oxt` in a disposable LibreOffice profile, the real plugin with the fake Runs API on ephemeral ports, a Chrome that this run started, window positions from the window server by exact PID. Steps: (1) Open → one window, docked; no code in any process list. (2) 50 scripted moves and 20 resizes: every one lands exactly; **median ≤ 150 ms, 95th percentile ≤ 250 ms, maximum ≤ 400 ms**, with the sampling interval reported. (3) A burst of 60 placements in 5 s: the final rectangle is exact within 500 ms and never changes afterwards. (4) Three real pointer drags of LibreOffice: Sidecar ends docked within 500 ms of the release. (5) Ten hand drags of Sidecar: none is snapped back at the next LibreOffice move; the page says not following within 3 s; Reattach lands exactly within 500 ms. (6) A hand resize of Sidecar also undocks. (7) Minimise and restore: no movement while minimised; docked again after. (8) LibreOffice full screen, and the green button if D2 found it testable: no jump, no Space switch. (9) Two documents: moving A moves only A's Sidecar; closing A closes its Sidecar and B still follows. (10) Loading another document into the followed window: the old Sidecar closes; nothing follows the new document until Open Sidecar is clicked. (11) A slow turn while LibreOffice is dragged: the reply arrives; Stop answers within 1 s. (12) Quit during a burst: `soffice` exits 0, no crash report, the browser is gone, no restore prompt at relaunch. (13) The plugin stops mid-follow: LibreOffice does not stall, the panel text does not change, a new Open Sidecar works. (14) LibreOffice is the frontmost application before and after every placement: 0 changes in 70. (15) Logs hold no rectangle; fewer than 20 INFO lines from 30 s of dragging. (16) Second click raises as in 0.5.0. (17) With the kill switch set, nothing follows and nothing else changes. Then: the 0.5.0 `.oxt` against the 0.5.1 plugin, and the 0.5.1 `.oxt` against the v0.5.0 plugin, each open, one turn, close (V10). Safari tab mode: open, turn, close; no window call. Everyday LibreOffice and Chrome untouched by exact PID; sentinel unchanged; everything this run started is stopped and deleted. |
| **G7 ATLAS tab-mode regression** | Limited interactive user, the exact `.oxt`, real plugin, real panel click: a tab in the default handler; the exchange says `follow: false`; across the whole run the plugin's request log holds **no** `launcher-bounds` and **no** `api-window`; moving the LibreOffice window does nothing to the browser; one formatted reply; lost Send and Try again start exactly one run; Stop; a held close release is retried and the tab closes (E03); quit, `soffice` exit 0, tab closed; folder permissions as in 0.5.0; `%APPDATA%\LibreOffice` absent before and after. Optional: V11. |
| **G8 Reproducibility and private CI** | The full suite on the exact candidate. **Private CI: all six jobs** (macOS, Ubuntu, Windows × Python 3.11, 3.13) **green before any public push**; the vendor job green. Protected hashes unchanged. Deterministic build equals both README hashes. `hermes plugins validate` and the payload scan on a clean export: nothing dangerous, cautions listed. Version, date, hashes, catalog text and docs agree. Any change after CI means CI again. The public pin check happens after publication and is listed as pending until then. |
| **G9 Real gateway chat (needs your authorization)** | The candidate pair on macOS against a real Hermes gateway and model, with following on: read; edit, read back and undo; Stop during a tool; LibreOffice dragged during a reply; document closed during a reply; quit. Recorded: the stop used the run's own key, owned and unresolved counts, no traceback. Recommended in addition: a fenced refusal and `hermes plugins disable` during a reply. No claim that the unload fence limitation changed. |

For reruns the 0.5.0 rule stands: only the already-classified fake-clock millisecond signature in the protected suites may be rerun, with the first output kept.

---

## 10. Stop rules and rollback

1. A protected file's hash differs, or either suite needs an edit to pass: stop and revert.
2. D2 (a) or G2 stress shows a Stop slower than 1 s, an `endpoint_busy`, or a failed page load: switch the plugin to poll-only and re-measure. Still failing: following does not ship on by default; decision to you.
3. A LibreOffice crash, hang or main-thread stall over 5 ms per callback that points at the listener: following is cut from the release or ships off; decision to you. A crash report is kept, never explained away.
4. Any focus change or Space switch caused by following: stop.
5. Any false undock in the scripted run, or any snap-back after a hand drag: stop; fix, or ship off by default.
6. G6 latency over the threshold: not hidden. You choose between shipping with the measured numbers in the notes and holding.
7. Either mixed pair cannot open Sidecar: blocker.
8. G7 sees a bounds call, a window poll or any changed behaviour on Windows: blocker.
9. Private CI not 6/6: no public push.
10. A lane patch touches a file outside its column: rejected, not merged.
11. Contract change after C0: only as a recorded revision by the integrator, with the vectors and the stub updated in the same commit; the affected gates rerun.

**Rollback points.** After S1–S3 (revert three small commits). After I1 (the plugin's new routes are unreachable without a 0.5.1 extension that received the signed echo). Before D-final (no hash or date has moved). After release: the catalog pin stays on 0.5.0 until the bump merges; the 0.5.0 `.oxt` installs over 0.5.1 (same identifier); the Settings switch and `HERMES_LO_SIDECAR_NO_FOLLOW=1` turn following off without a reinstall.

---

## 11. What stays unverified (to be written into the notes as such)

| Item | Why | Safe behaviour meanwhile |
| --- | --- | --- |
| Second display, negative coordinates, a display above or left | one display attached in every run so far | pause with the other-display text (F7); never pulled to the main display |
| Retina (2×) and mixed scale | all measurements at 1× | unknown whether UNO reports points there; if not, the window lands wrong and the user can undock |
| Green-button full screen, Spaces, Stage Manager, window tiling | only LibreOffice's own full screen was tried `[SPIKE]` | the page never activates itself; worst case Sidecar stays in the old Space |
| Display added, removed or resolution changed while docked | not tried | re-baseline rule (F5) |
| A long hand drag as a person does it | spike used scripted placements | D2 (f) and G6 step 4 add three pointer drags; "feel" is still your judgement |
| Edge, Brave, Chromium on macOS | not installed on the test Mac | claim Chrome only |
| Intel Macs; macOS other than 27.2.1; LibreOffice other than 26.8.x | not available | named in the tested-versions list |
| More than 8 windows; two frames on one document | not tried | session cap; documented limit (F8) |
| A managed browser | not available | 0.5.0 text unchanged |
| A future Chrome that stops honouring `moveTo` in app windows | not a documented guarantee | G6 per tested version; the window then simply stays where launch put it |
| NVDA, VoiceOver | not required by you | "not tested with a screen reader" stays in the ledger |
| Firefox on macOS; Edge or Chrome as the Windows default | need installs or setting changes | recorded as untested |

---

## 12. Risks

| Risk | Signal | Mitigation |
| --- | --- | --- |
| Follow parks starve Stop | G2 stress, D2 (a) | shared pool of 4, turns first; poll-only fallback by constant |
| Mixed versions break Open Sidecar | G6 mixed pairs | F2; no request change without the hint; signed echo |
| Window fights the user | D2 (c), G6 step 5 | F5; sticky undock; off-by-default fallback |
| Listener destabilises LibreOffice (re-entrancy, disposal, quit) | crash report, exit code, D2 (e) | O(1) callback, no UNO on the worker, self-removing listener, kill switch |
| Channel exhaustion shown as *not your Hermes* | unit test at call 48, G2 | own channel, renewal, silent failure |
| Log flood | G6 step 15 | DEBUG labels in S1; stop on first 409 |
| Evidence does not match the shipped archive | hash in every evidence folder | S3 fixes the version on Day 0; gates rerun after any byte change |
| Page code for P03 and follow collide in `app.js` | review | one owner; P03 lands first, alone |
| Multi-display users see a stuck or oddly placed window | field report | F7 rules; explicit note; Reattach |
| The headline is read as "docked" | user expectation | docs and UI say "follows"; the measured delay is stated |
| Scope creep from 22 items | patches outside columns | stop rule 10; D2 and gates gate the headline only |
| V14 finds a real service-worker takeover in tab mode | V14 report | not fixed inside 0.5.1 without your decision; disclosure at minimum |

---

## 13. Questions for Jonathan, with my recommendation

| # | Question | Recommendation |
| --- | --- | --- |
| **Q1** | Following on by default: unconditional, or only if the no-fight checks pass? | On by default **only if** D2 (c) and G6 steps 5–6 pass with zero snap-backs and zero false undocks. Otherwise ship it off by default with the switch. Blocks the lanes. |
| **Q2** | What delay is acceptable for "follows"? | Median ≤ 150 ms and 95th percentile ≤ 250 ms with one or two windows (the spike measured 114 and 189); up to about 600 ms for the fifth and later windows. Call it "follows", never "docked". |
| **Q3** | Does resizing Sidecar by hand also undock it, like dragging? | Yes for 0.5.1: one rule, any hand change stops following; Reattach restores 400 points. Remembering your width is later work. Blocks the lanes. |
| **Q4** | A remembered "Follow LibreOffice" switch in Settings? | Yes. Without it a user who dislikes following must undock every new window. Blocks the lanes. |
| **Q5** | Two displays and Retina: ship unverified with the pause-and-Reattach behaviour, or test first? | If a second display or a Retina panel can be attached for half an hour, do one G6 pass on it. If not, ship with the explicit "not tested" note and the safe behaviour. |
| **Q6** | The pruned list: 22 in, 9 out (section 6)? In particular no Firefox install on the Mac, no default-browser change on ATLAS, and "exact look" reworded instead of measured. | Approve as listed. |
| **Q7** | Real-gateway chat (G9): who runs it, on which gateway, with what cost ceiling? | You authorize one bounded run on the candidate, the six steps of G9, on macOS only. |
| **Q8** | If following is not green on integration day: hold, or ship the fixes with following off? | Hold up to two working days. After that ship 0.5.1 with following present but off by default, provided the safety gates (G1, G2, no crash) pass. |

Also for your confirmation, no recommendation needed: the disclosure sentence "Sidecar does not minimise with LibreOffice; it stays where it is and following pauses", and that clicking Open Sidecar on an undocked window only raises it (F12).

---

## 14. Assumptions to verify

| Assumption | Verified by |
| --- | --- |
| Four parked requests shared by turns and window polls keep Stop under 1 s at 8 sessions | D2 (a), G2 |
| Send-at-once plus 100 ms spacing gives the spike's latency or better | D2 (b), G6 step 2 |
| Reading the window back 400 ms after a placement gives a stable rectangle; 8 px separates a user move from noise | D2 (c) |
| UNO signals LibreOffice full screen and the green button distinguishably | D2 (d) |
| One UNO callback costs well under 5 ms on the main thread | D2 (e) |
| `windowMoved` and `windowResized` arrive during a real pointer drag, not only after it | D2 (f) |
| A visible, unfocused app window keeps running timers and network callbacks | G6 steps 2–4 (LibreOffice is frontmost throughout) |
| Chrome clamps a page's cross-display `moveTo` | section 11; hardware run if Q5 says so |
| UNO reports points on a 2× display | not planned without hardware; disclosed |
| `localStorage` in the dedicated profile survives between launches | G3 |
| The 0.5.0 launcher ignores `caps` and `follow` in answers | G6 mixed pair |

---

## 15. Deferred improvements (not blockers)

1. Hand the fast slot to whichever window's LibreOffice frame moved last, so the fifth window also gets the short delay.
2. Remember a user-chosen Sidecar width while following.
3. Re-bind following from the LibreOffice side (another frame of the same document, or after a LibreOffice crash).

---

## 16. Verdict

The direction is sound and the spike proves the mechanism for one window on one display. The spike's protocol as written must not be built: it breaks mixed versions and the connection budget, and its default experience depends on an undock rule nobody has run. With sections 4–5 frozen on Day 0 and D2 green, the lanes can start in parallel.

READY_WITH_AMENDMENTS
