# D2 — refined macOS follow design: NO-GO for default-on

## Decision

**Q1: no, do not enable following by default on this evidence.** The shared connection pool and normal-window latency work. The no-fight rule needs an explicit timer-completion guard, the callback's observed worst case exceeds the plan's 5 ms stop rule, and three actual document-window hand drags were **not qualified**. The final no-Accessibility quit run returned **255**, not 0. These are not hidden behind otherwise good median timings.

This is an investigation, not a product implementation or G6 acceptance. No product commits or edits were made in `sidecar-libreoffice-0.5.1`. All prototype changes and execution were under `/Users/jonathan/.hermes/profiles/omh/cache/scratch/051/d2/`.

## Per-item answer

| Item | Answer | Observed result |
|---|---|---|
| (a) Shared pool, eight sessions, four hanging turns, 30 s | **YES, bounded transport qualification** | Worst-case run: 31.175 s, **2,488 signed bounds calls, all 200**, nominal 10 Hz per session (stronger than one process worker's total 10/s). Four hangs at a time, two groups covering all eight Stop buttons: **8/8 HTTP 202**, median **2.25 ms**, maximum/p95 **3.80 ms**. Ninth static page load 200, 6,361 bytes. Maximum sampled parked count **4**. All eight window readers progressed: **95, 64, 67, 65, 93, 67, 65, 67** responses. No recorded HTTP error or endpoint-busy response. See the important hidden-page load seam below. |
| (b) Parked and unparked latency | **YES for the measured one-window and fifth-reader cases** | Every target landed in all three 50-move + 20-resize sets below. Fifth reader, with four actual hanging turn polls occupying the pool, was below 600 ms. A distinct two-window native latency run was not performed. |
| (c) No-fight rule | **NO as a timestamp-only implementation; YES after the read-back completion guard** | Original 400 ms implementation falsely undocked once in the unparked scripted run. Corrected guard: **0 false undocks in 140 placements plus a 60-placement burst**. **10/10** guarded Chrome hand-style changes (eight title-bar moves, two corner resizes) undocked, stayed where put after the next host movement, and **10/10 Reattach** operations landed exactly at `[1080,163,400,592]`. This is a required clarification to F5, not proof that the unqualified wording is safe. |
| (d) Full-screen signal | **NO for a qualified signal covering both requested native modes** | Pure UNO **does** expose `container.FullScreen` and `frame.LayoutManager.HideCurrentUI`: both false → true → false around `.uno:FullScreen`. Ctrl-Cmd-F was sent only to the owned, frontmost LO PID; it produced true, but a second shortcut did not restore false. A first-run Welcome window was present, so this is not independently qualified green-button/Space entry and exit. Do not advertise reliable native-fullscreen/Space detection from this run. |
| (e) One main-thread listener callback ≤5 ms | **NO** | **1,866 callbacks**: median **0.0504 ms**, p95 **0.1711 ms**, maximum **11.4262 ms**. Two exceeded 5 ms: **10.4865 and 11.4262 ms**. Measurement includes the pure-UNO geometry/state reads, changed-value test, mailbox copy and event wake; no logging or network occurs in the timed callback. These are wall-clock durations, not CPU-only self time. The outliers cannot be attributed more precisely from this instrumentation; they are not waived. |
| (f) Three real LO document title-bar drags | **NO / NOT QUALIFIED** | Three guarded pointer trajectories were posted, but the required actual document displacement was **0/3**. Initial selection could hit a floating LO toolbar; tightening the size filter still did not yield document movement. Window-server traces and inspected owned-window captures exposed a first-run Welcome window. After two unsuccessful qualification rounds, no additional acceptance rerun was claimed. This is a harness/input blocker, **not proof that UNO fails to emit events during a successful hand drag**. No AX traversal, including on Calc, was used. |
| (g) Second display or 2× | **NOT AVAILABLE** | `system_profiler SPDisplaysDataType`: one LG HDR WFHD, physical and UI resolution **2560×1080**, 75 Hz; observer reports 1×. No second display or 2× panel attached. |

## Latency results

Latency starts at the first retained UNO callback for the commanded rectangle and ends at the first CG sample matching the Chrome outer rectangle exactly. Window truth comes from a separate Swift CGWindowList observer restricted to owned PIDs; no native geometry is loaded into LO. Coordinates and decoration correction match spike D: client Y minus 32, height plus 32. The native worker sends immediately, spaces subsequent sends by 100 ms, keeps the trailing value, serializes HMAC calls and renews its channel before the call limit.

| Mode | Operation | Exact landings | Median | p95 | Maximum |
|---|---|---:|---:|---:|---:|
| Shared parked route, one native reader | Move | 50/50 | 21.60 ms | 38.93 ms | 40.22 ms |
| Same | Resize | 20/20 | 17.43 ms | 133.82 ms | 133.82 ms |
| Poll-only fallback, retry 100 ms | Move | 50/50 | 114.12 ms | 177.82 ms | 180.99 ms |
| Same | Resize | 20/20 | 110.63 ms | 190.19 ms | 190.19 ms |
| Fifth reader, four hanging turn polls, retry 250 ms | Move | 50/50 | 217.13 ms | 327.93 ms | 335.72 ms |
| Same | Resize | 20/20 | 275.41 ms | 403.04 ms | 403.04 ms |

The main observer's actual sample spacing was median **21.37 ms**, p95 **27.96 ms**, maximum **36.69 ms** (not the requested 5 ms). Thus these are sampled upper-bound landing timings, not compositor timings at millisecond precision. The unusually low parked median is a real measured result of removing spike D's periodic-worker delay; it is not a promise for every machine.

A failed *harness* attempt at the fifth-reader trial reset the plugin's snapshot sequence below an existing page's sequence and consequently produced zero landings. It is retained as `fifth-*` evidence, not used as latency data or a product finding. The accepted trial used a **fresh disposable LO/plugin/page binding**, never reset sequence, and is named `fresh-fifth-*`. Its four real turn polls occupied all four park slots before measurements began; sampled parked count remained at most four.

## Connection-pool experiment and limits

The real plugin was created through `scripts/web_smoke/plugin_factory.make_endpoint` and its real FakeRunsAPI. Scratch `web_api.py` implements a shared maximum-four pool; a turn reader preempts a window waiter before parking. `/api/window` otherwise returns unchanged state with `retry_ms:250`, or 100 in poll-only mode. The worker uses the real LauncherClient channel proofs and response MAC verification.

Eight dedicated-profile Chrome app sessions were opened. Chrome reported several occluded pages hidden, as the actual page's visibility policy requires. The ordinary native-window run still delivered all eight Stops (maximum 19.80 ms), but hidden pages stop following, so that alone was **not** an eight-reader saturation proof. The final worst-case run added one **synthetic window reader only while a page was hidden**, using that page's in-memory session header. Visible pages retained their real follow loop. This deliberately overrides the *load*, not the production visibility rule, to keep all eight sessions contending for the same origin/park pool. No token was saved in evidence. Browser responses and server pool samples are preserved in `stress-saturation.json`.

The saturation sender is deliberately stronger than the production worker: approximately 80 signed bounds requests/s across eight sessions, rather than a single LO process's total limit of ten/s. It is a transport test, not qualification of eight real UNO document listeners, per-window worker fairness, every production validation rule, or logging volume. Scratch bounds authority/shape checks are incomplete; this patch must not be merged.

## Undock and settle time

The failed timestamp-only trace is retained under `round1-settle-race/`: the undock and settle callback have the **same millisecond timestamp**, before the new rectangle has become the read-back baseline. Expiration of `performance.now() < settling` does **not** prove the timer's callback has run. A resize/network callback can check the old baseline first.

Required clarification: **suppress undock comparisons until the current placement's read-back callback has completed**, and cancel/restart that callback on each own placement. The corrected prototype uses `settleTimer !== null` as well as the deadline. Sticky undock stops placement and window reads; Reattach requests `seq=0` and applies the newest snapshot.

With this guard and 400 ms: zero false undocks in both 70-placement sets and the burst; all ten pointer changes and Reattach cases passed. Page apply → CG exact landing measured median **18.96 ms**, p95 **31.41 ms**, maximum **39.79 ms**, but **that is not the minimum necessary baseline delay**: browser coordinate read-back can update before the compositor.

A bounded sweep of **12 placements each at 0, 25, 50, 100, 200 and 400 ms** produced zero false undocks in each group after the guard. Consequently this hardware/run does **not establish a positive minimum required settle delay**. Keep **400 ms** as the conservative, fully exercised plan constant; do not present it as an empirically necessary minimum, and do not promote the short 0 ms sweep into a zero-delay shipping recommendation. The accepted residual—a drag begun during own settling can be snapped back once—remains untested here.

## Minimise, hidden and fullscreen

- Minimise's immediate command read-back was still normal; after animation, UNO reported `IsMinimized:true`, rectangle **[200,180,0,0]**. The listener sent state `minimized`, **bounds null**. Chrome stayed **[1050,148,400,592]**. Restore returned the normal rectangle. This demonstrates why immediate command acknowledgments are not state-transition evidence.
- `windowHidden` delivered state `hidden`, bounds null, without moving Chrome. `windowShown`/normal geometry resumed. The probe's explicit snapshot helper can overwrite a hidden state with normal because it does not retain a hidden-state latch; durable hidden-state behavior beyond these event traces is **not fully qualified**. Production must latch hidden until shown and suppress geometry throughout.
- Across captured sent snapshots, **zero normal bounds had width or height below 240**. Minimized/hidden snapshots carried null bounds; the normal fallback was not used for them.
- `.uno:FullScreen` gave **[0,32,2560,1048]** client geometry and the two pure-UNO booleans above. The prototype intentionally retained ordinary-large-window placement while probing signal availability; it did not implement or qualify a native-Space pause policy. The shortcut path did not demonstrate reliable exit and no direct green-button click was performed.

## Constants and plan consequences

| Constant/policy | Result |
|---|---|
| Global park cap | **4**, turn polls first; preempt a parked window reader before admitting a turn |
| Normal unparked retry | **250 ms** |
| Poll-only fallback retry | **100 ms**; measured fast enough in the one-window fallback trial; no stress fallback required because (a) passed |
| Worker | First change immediately; **100 ms minimum spacing**, one in flight, latest-value trailing edge; own channel renewal before 48 calls |
| Undock threshold | **More than 8 points**, unchanged screen measurements |
| Settle | **400 ms**, **and pending read-back completion guard**; minimum necessary delay not established |
| Undocked | Sticky until Reattach/new session; no geometry writes or window polling; Reattach `seq=0` |
| Rectangle floor | **240×240**; null geometry while minimized/hidden |
| Fullscreen | Use the plan's **ordinary-large-window fallback** until the native green-button/Space signal is qualified; own `.uno:FullScreen` property is available but is not a complete native-mode qualification |
| Default | **OFF / hold following**, not default-on |

Plan outcomes: (a) does not trigger poll-only fallback. F5 needs the timer-completion clarification before its contract freezes. **(e) triggers stop rule 3** under the stated wall-time criterion; cutting following or shipping it off requires the owner's decision, not this report's waiver. (f) remains an acceptance blocker. (d) cannot justify the broad fullscreen-pause promise. These conclusions stand even though corrected Chrome no-fight checks and ordinary follow latency are good.

## Environment, ownership, verification and cleanup

- macOS **27.2.1 / 26B131**, Apple M5 Max; LibreOffice **26.8.2.2**; dedicated Chrome **158.0.8127.36**. Display evidence in `environment.json`.
- Baseline had **no everyday soffice**. Unrelated Chrome main PIDs **5712, 12442, 94247** were never controlled and remained alive at cleanup. Inputs were guarded by the owned PID being frontmost and the topmost layer-zero window under the point belonging to that PID. All tested document content was disposable blank Calc; no AX was used.
- Main measurement owned LO **62878**, plugin **62869**; exact Chrome PID in `run-pids.json`. Final fresh trial LO **9633**, Chrome **9957**, plugin **9624**. Every recorded owned primary PID is absent; the final process-list filter found no remaining scratch-run processes.
- A final fresh-profile no-AX run exercised real following, the fifth-reader latency trial and PID-targeted quit. **LO returned 255**. The quit request was accepted, and no new soffice crash report appeared after the run's start, but **clean exit 0 is NOT passed**. Empty `lo.stdout` does not explain the return code. This needs investigation before installed acceptance.
- Disposable LO/Chrome profile trees, pairing token and known owned Chrome socket directory were deleted. Earlier setup socket paths were not retained, so this report does not claim their outside-profile socket directories were individually audited. No everyday browser profile/cache was deleted.
- Source worktree `git status --porcelain` remained empty. Protected test hashes stayed `a600cb01d6be05ee3025410fd222929678636f41fed2a410f2e59cad3e11d860` and `259b1e2e4225967a8889418e0074ac785b9a794cdc6f515e1423d9ae10c01227`. Harness Python compilation and page JS syntax checks passed; static sums were regenerated after page edits. No production test suite, installed extension acceptance, CI or release claim is made.
- Setup errors are retained separately: missing query-route allowance initially caused 404s; an early page insertion ran before body existed; same-document fragment navigation did not reload an edited app; repeated fetch instrumentation once recursed; initial Target.createTarget pages became hidden tabs. None of those runs contributes passing latency or capacity numbers. The original spike D harness was reused and extended, not replaced by invented output.

Evidence is under `evidence/D2/`: `final-analysis.json` is the aggregation entry point; native command/CG/UNO traces, all interaction outcomes, raw callback durations, settle sweep, final quit receipt, scratch-only patch and harness sources accompany it. Failed harness runs are retained and labeled. No profile, token or launch-code file is published.
