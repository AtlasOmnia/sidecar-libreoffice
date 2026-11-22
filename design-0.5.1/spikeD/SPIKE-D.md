# Spike D — macOS Sidecar window following

## Decision

**GO for optional “Follow LibreOffice” (soft following); NO-GO for claiming a genuinely docked window.** Keep the pure-UNO → authenticated plugin → page `resizeTo` then `moveTo` architecture. Do not introduce in-process native geometry, AX control of LibreOffice, or an osascript bounds setter in the extension.

The listener path works for normal moves and resizes. It is visibly asynchronous rather than a single attached window: the measured push prototype takes roughly 0.1 s, with tails around 0.2 s. Polling at 100 ms adds noticeable delay. Minimise and full-screen semantics are **not** solved by geometry delivery: an independent browser window cannot be minimised into, or attached to, LibreOffice's macOS Space through the page's standard window APIs.

**Safest 0.5.1 shipping subset:** opt-in normal-window following, explicit Undock/Reattach, suspend on minimisation/full screen, retain the original document/frame binding. Preserve the existing connection budget using finite authenticated bounds/state polls, not an additional permanent long-poll for every window. Treat lower-latency push as a separate transport change with a connection-budget gate.

## Scope and setup

Read the requested 0.5.1 protocol and launcher/sidebar/plugin/page sources, plus the 0.5.0 final plan and Day-0 S2 findings. Scratch only:

`/Users/jonathan/.hermes/profiles/omh/cache/scratch/051/spikeD/`

- Copied the tree there; never edited or committed to the 0.5.1 worktree. Final `git diff --exit-code` for the six requested source/protocol files returned 0.
- No everyday `soffice` process was present at baseline. Unrelated Chrome PIDs 5712, 12442 and 94247 were not controlled and remained alive after cleanup.
- Disposable LibreOffice `-env:UserInstallation`, pre-created private bridge folder and ephemeral port file; separate ephemeral real `ChatBridge` endpoint from `scripts/web_smoke/plugin_factory.make_endpoint`, with its real web routes and `FakeRunsAPI`.
- A UNO user script ran inside LibreOffice, directly invoked the real `sidecar_launcher.LAUNCHER` (with only the adapter cache directory redirected), and retained pure-UNO `XWindowListener` + `XTopWindowListener` objects on the document frame's container window. This is a geometry/launcher spike, **not a requalification of the installed .oxt/sidebar button or document-tool bridge**. No fake model responses were substituted for window measurements; no chat turn was needed.
- The listener copies `getPosSize()` into a latest-value mailbox. A single worker drains it on a 50 ms periodic tick, uses the real `LauncherClient` HMAC implementation, and renews the channel before its call cap. This is a periodic coalescer, not a trailing-edge debounce. It continues delivering during movement instead of waiting until a drag stops.
- Scratch plugin adds signed `/launcher/bounds` and session-authenticated `/api/dock`. The latter either parks until the sequence changes (push experiment) or immediately returns a latest snapshot, with the page waiting 100 ms before the next request (poll experiment). The page uses the existing alignment function.
- Window truth: a separate Swift process calls `CGWindowListCopyWindowInfo`, restricted to the exact test LO/Chrome PIDs. No native calls were loaded into LibreOffice. No Accessibility traversal of LibreOffice was used. Chrome title-bar dragging used guarded CGEvents after checking the test browser was frontmost.
- One attached screen: **1920×1080 at 1×**. Second display, Retina/2×, Intel, and display-reconfiguration tests were unavailable.

Final measurement PIDs: LibreOffice 38977, Chrome 39097, plugin harness 38968. All stopped; disposable LO/browser profiles, token, and the exact Chrome socket directory were deleted. See `evidence/cleanup.json`.

## Measured normal-window following

Latency starts at the first UNO move/resize callback for a commanded placement and ends at the first sampled Chrome outer rectangle matching the requested target **exactly**. 32 pt is added to height and subtracted from client Y, consistent with S2 and the existing page correction. Successful landings were 0 pt off the target at the sampled point.

| Transport | Operation | Commands | Exact targets seen before next command | Median callback → CG bounds | p95 | Max |
|---|---|---:|---:|---:|---:|---:|
| Coalesced listener + parked page request | Move | 50 | 50 | 113.65 ms | 188.99 ms | 202.30 ms |
| Coalesced listener + parked page request | Resize | 20 | 20 | 87.58 ms | 207.64 ms | 207.64 ms |
| Same listener + 100 ms page polling | Move | 50 | 47 | 217.81 ms | 325.54 ms | 407.97 ms |
| Same listener + 100 ms page polling | Resize | 20 | 19 | 179.42 ms | 284.63 ms | 284.63 ms |

Polling latency statistics exclude the four commands without a sampled exact landing before the next command. **Do not read these as 100% polling delivery.** This experiment compares page transport while holding the UNO snapshot source constant; it does not poll UNO remotely from the page or add a continuously polling UNO worker.

Measurement limitations:

- CG collection was requested every 5 ms, but actual median spacing was **33.53 ms**, p95 **48.05 ms**, max **63.24 ms**. These are observed upper-bound timestamps with roughly one sampling interval of uncertainty, not millisecond-precision compositor timing.
- Nominal command spacing was 280 ms; actual command timestamps are recorded. File writes and the host environment add scheduling delay. Reported p95 uses the sorted sample at index `int(n * .95)`.
- No stable position oscillation was sampled after an exact target landed: **0 subsequent deviations** within those command windows. That does not rule out sub-sample visual jitter or transients during `resizeTo` + `moveTo`.
- The rapid-sequence test issued **60 commands over 5.608 s**, observed **36 Chrome rectangle transitions**, and the last target settled **149.21 ms** after the last command timestamp. Despite the script's requested 60 Hz sleep, this was **not a 60 Hz real drag benchmark**. Coalescing is visible and intentional; no 60 fps claim is justified.

### CPU (bounded claim)

Median sampled `ps %CPU` during the successful normal-operation runs:

| Mode | LO process | Chrome main process | Plugin harness process |
|---|---:|---:|---:|
| Push | 0.95% | 0.80% | 0.00% |
| 100 ms polling | 1.40% | 0.85% | 0.20% |

These are `ps` samples, not a CPU profile or an incremental idle-subtracted cost. Chrome renderers/GPU and the CG observer are excluded. They support only “no high CPU in the measured main processes,” not a total browser-resource estimate.

### Does it feel docked?

**No, not in the native-window sense.** Normal following reaches the correct rectangle without sampled settled jitter, but ~114 ms median move lag and ~189 ms p95 are enough to separate the two windows during movement. At 100 ms polling the median move lag is ~218 ms. This is a trailing companion window, not a single docked frame. There was no human usability panel or subjective smoothness trial; this judgement follows the timing and geometry traces.

## Alternative: osascript bounds by PID

A separate, throwaway process invoked `/usr/bin/osascript` → System Events → process selected by exact Unix PID → window 1 position/size, for 20 placements. All calls exited 0, median call duration **133.99 ms**, max **304.52 ms**. **CG observed 0/20 requested app-window rectangles.** The app window stayed at its old position until a subsequent UNO movement drove the page path.

Thus these call durations are **not successful follow latencies**. This AX setter variant did not establish useful movement, and exit 0 was not proof of effect. It was not repaired into a shipping mechanism. Independently, bounds-setting exceeds the binding 0.5.0 restriction that the extension may use osascript only to raise the browser; it introduces Accessibility/TCC dependencies, subprocess overhead, window-selection ambiguity with multiple app windows, and new disclosures. Reject it for 0.5.1.

## Lifecycle and user interactions

### Minimise/restore — callbacks work, geometry-only following is wrong

Setting the UNO `IsMinimized` property delivered `windowMinimized` approximately 0.57 s after the request, after animation; its `getPosSize()` was **[395,220,0,0]**. The naive existing `compute_bounds` fallback turned this into a full-height screen-edge target. The browser remained onscreen and jumped to the right edge. Never run zero-sized/minimised geometry through the normal fallback.

Restore delivered `windowNormalized` with **[395,220,850,560]** about 0.57 s after the restore request and the browser returned to **[1245,188,400,592]** outer bounds.

Production rule: mark host state `minimized`, suppress geometry, keep the last valid normal rectangle, and mark following suspended. On `normalized`, take a fresh main-thread snapshot and resume only if the user has not undocked. Page JavaScript has no standard API to minimise/restore its own OS app window, so **do not promise the companion minimises/restores with LO**. Hiding page contents is not hiding a window; closing it would end the chat/session and is not an acceptable substitute.

### Full screen

`.uno:FullScreen` expanded the LO outer rectangle to **[0,0,1920,1080]**; the browser remained a separate onscreen window clamped near the right edge, not part of LO. Exiting restored the normal host and Sidecar rectangles. This tested **LibreOffice's UNO full-screen command**, not the green-button macOS native full-screen Space transition; the latter remains unverified.

Production rule: suspend follow for full-screen geometry/state, keep the last normal rectangle, resume on return. A pure-UNO listener cannot guarantee shared Spaces or native split-view membership for a separate Chrome application. Do not auto-activate/raise Chrome on every geometry event: that steals focus and can switch Spaces.

### Switching documents/frames

Created a second Writer document and invoked activation of the original frame. The existing Sidecar stayed at the original Calc frame's geometry; there was no implicit chat/document retargeting. These scripted operations did not provide a complete activation/deactivation trace; a later application activation did deliver `windowActivated`. Therefore do not assume every scripted frame switch produces the same `XTopWindowListener` sequence.

Bind following to **the frame that launched that document's chat**, not `Desktop.getCurrentComponent()` on every event. Preserve the session's fenced document identity. One document can have multiple frames; a frame needs its own ephemeral generation identifier. Do not let a new frame silently take over an existing document session. Explicit Reattach can deliberately choose a new frame and generation. Same-document multi-frame and multiple simultaneous Sidecar-window runs remain untested.

### Moving Sidecar itself

A real, guarded title-bar drag moved the browser from **[1170,208,400,592]** to **[876,376,400,592]** while LO stayed fixed. It stayed there until the next LO movement. The naive prototype then snapped it back to **[1190,228,400,592]**. This is fighting user intent.

Recommend an explicit **Follow LibreOffice / Undock** control, with manual movement causing undock: compare actual `screenX/screenY/outerWidth/outerHeight` with the last acknowledged/clamped placement while the host snapshot is unchanged; after the self-move settling window, two consecutive deviations beyond an 8 pt tolerance suspend following. Never interpret expected OS clamping, pending own placement, minimisation, or display transitions as user intent. Reattach is explicit and immediately applies a fresh snapshot. Automatic manual-drag detection was **not implemented or qualified** here; the observed snap-back establishes why a policy is needed. The explicit control should be authoritative even if timers are throttled.

### Displays and coordinates

Only one 1× display was attached. The harness retained the old coarse 2560×1080 work-area fallback; measured normal targets were within the actual 1920×1080 screen, so this did not affect the normal-operation table. It did contribute to the deliberately naive minimise/full-screen edge fallback. This is not multi-display evidence.

The existing page clamps against **its current screen**, which can pin it to the old display when LO crosses displays. Fix before claiming multi-display following: pass target display work-area coordinates from a proven pure-UNO path if available, or explicitly declare cross-display follow unsupported. Do not obtain native pointers from UNO. If a future external native helper is proposed, that is a separate policy/security decision, not an incidental part of this design. Test negative origins, displays above the primary, different scale factors, menu-bar/Dock edges, disconnect/reconnect, and target-display transitions before enabling across displays. Existing `Toolkit.getWorkArea()` is not sufficient evidence of correct per-frame work areas.

## Proposed protocol additions (not implemented production contract)

Negotiate `window_follow: 1` in the authenticated hello readiness/capabilities response. Old plugin/extension/page combinations retain the existing launch-only alignment. Preserve all Host, role separation, fetch-metadata, HMAC, no-cookie and no-store rules.

1. Extend app-mode `/launcher/launch` with optional `frame_id` (32 lowercase hex, extension-generated per frame lifetime). Its answer adds `attachment_id` (22 URL-safe chars, plugin-generated, **session-lifetime**, not the 120 s launch-record lifetime). The plugin binds attachment → session + document + frame. Tab mode has no attachment. Do not silently rebind `window_live` to a different frame.
2. New **`POST /launcher/bounds`**, signed with the existing channel MAC and monotonic channel counter:

```json
{"attachment_id":"<22 URL-safe chars>","frame_id":"<32 lowercase hex>","seq":17,"state":"normal","bounds":{"x":1190,"y":260,"width":400,"height":560},"work_area":{"x":0,"y":30,"width":1920,"height":960}}
```

`state` is exactly `normal|minimized|fullscreen|hidden|closed`. `bounds` is a target **client** rectangle in existing screen-point coordinates, non-null only for `normal`; `work_area` is an explicit target work area or null when unavailable. Signed 200 `{"ok":true,"accepted_seq":17}`. Validate exact keys/types (bool is not int), rectangle limits, active app session, attachment/frame binding, monotonically increasing per-attachment sequence; reject mismatches with signed 409 `attachment_stale`, malformed input with signed 400 `bad_request`. Latest-value replacement, not an unbounded event queue. A duplicate sequence with identical contents may be acknowledged idempotently; divergent replay is refused. Never revive a released attachment.
3. Add a latest event snapshot to authenticated `/api/state`, and piggyback it on `/api/turn` responses:

```json
{"window":{"type":"bounds","seq":17,"frame_id":"<32 lowercase hex>","state":"normal","bounds":{"x":1190,"y":260,"width":400,"height":560},"work_area":{"x":0,"y":30,"width":1920,"height":960}}}
```

The page accepts only newer sequence numbers for its attachment generation, only in app mode, and only while follow is enabled. Drop stale responses. Placement is still resize-before-move, with measured own decoration and target-screen clamping. No tokens/codes in bounds, URLs, logs or argv.
4. For the safest finite-poll variant, allow one non-overlapping authenticated state/bounds request every 100 ms **only while app-mode following is enabled and visible**; suspend/back off when hidden, undocked, unsupported, or on errors. An active turn may still need a finite bounds poll because its existing 25 s long-poll is not guaranteed to wake for geometry. Either teach that same long-poll to wake on window sequence changes, or keep the additional request short and explicitly test Stop and handler limits. No second parked stream per page. All-window finite polling requires an 8-session stress gate; the one-window numbers here do not qualify it.
5. If low-latency push is chosen instead, **revise the transport contract first**. The prototype's separate `/api/dock` long-poll for each session violates the old budget at scale: eight sessions can exceed Chromium's six HTTP/1 connections and the handler budget with four active turn polls. A unified notification/turn poll with a proved foreground/session admission limit could work, but is not what this spike qualified.
6. One serialized geometry worker per launcher/process (or a proven bounded equivalent), latest snapshot per live attachment, no network/process calls from UNO callbacks, no UNO reads on the worker, detach listeners on disposing/close/quit, cancel pending sends. Existing channels expire after **120 s / 64 calls**: renew before either bound, serialize counters, and do not let a geometry channel displace/block release/quit work. Bounds failure must disable following, never affect the document or turn ownership.

The scratch route intentionally omits production validation/binding and uses a one-window latest mailbox; `evidence/scratch-only.patch` is a reproducible experiment, **not merge-ready implementation**. No production protocol file was edited.

## Risks, estimate, remaining gates

- Normal-window soft follow: approximately **2–3 engineer-days** for frame/listener lifecycle, bound authenticated snapshots, page undock/reattach state, errors, unit tests and finite-poll connection stress tests; **1–2 additional days** for native acceptance across lifecycle/display variants. Estimates, not measured implementation effort.
- Native-feeling docking with linked minimise/Spaces/multi-display: **not demonstrated within the frozen process/helper constraints**. Do not bury that as a small follow-up; it needs a separately approved native integration design or a reduced product promise.
- Reentrancy/disposal, duplicate callbacks, geometry storms, channel exhaustion, clamping feedback, browser timer throttling, focus stealing and automatic reattachment are the principal correctness risks.
- Tested: normal move/resize transport, minimisation/restore callbacks and naive failure, UNO full-screen geometry, second-document creation/original-frame activation, actual browser drag/snap-back, single-screen alignment, exact-PID cleanup.
- Not qualified: actual continuous human LO dragging, native macOS full-screen Spaces, multi-display/2×, same-document multi-frame ownership, eight browser sessions/four turns/Stop, managed browsers, automatic manual-undock detection, shutdown exit-code correctness of the installed extension, or secure production validation of the new route.

## Evidence and verification

`evidence/analysis.json` contains all 140 normal-operation command outcomes and latency statistics. `commands.json`, `uno.jsonl.gz`, `cg.jsonl.gz`, `cpu.json`, `additional-analysis.json`, `alternatives*.json*`, and `drag*.json*` preserve the measurements. Harness source and scratch-only diff are included; no live tokens or browser profile are copied.

One setup script initially found no current document and was changed to load Calc explicitly. The first transport measurement had a missing slot on the scratch session model; its zero-follow results were discarded, the slot was added, and the successful full run above was measured from fresh profiles. A first minimise call used a nonexistent setter; the successful run uses the IDL `IsMinimized` property. These are harness issues, not passing production evidence. A later JXA quit attempt returned a method/property invocation error and was not used as exit evidence; cleanup uses owned exact PIDs and reads their absence back.

Final verification: Python compile checks passed for the harness UNO script and modified plugin route; requested product source/protocol diff check passed; successful move/resize run had no UNO-script error rows; cleanup confirms all final owned primary PIDs absent and disposable profiles removed. This is a bounded spike result with explicit unsupported cases, not approval to label all requested lifecycle/display criteria complete.
