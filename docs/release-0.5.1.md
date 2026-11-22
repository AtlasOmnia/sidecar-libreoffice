# 0.5.1 candidate release and evidence ledger

**Status: Unreleased; not a publication receipt.** This ledger separates the accepted candidate contract, historical observations and candidate gates. The README commands target the prospective v0.5.1 tag and must not be used until publication. Both README hashes now name the deterministic candidate archive: `5e9bcf95532ea8e5787a48779b335532e04650223352e86e0763da801b85b837`. Matching build checks do not imply native acceptance or release approval.

## Historical evidence — do not promote to 0.5.1

| Evidence | Scope and limits |
| --- | --- |
| 0.5.0 macOS native, candidate `797aeb5` | macOS 27.2.1 Apple silicon, LibreOffice 26.8.2.2, Chrome 158.0.8127.36: actual app window through the panel, real plugin, fake Runs API; open/reply/Stop/New chat, browser-level raise, close-during-launch and quit. No gateway/model; no optional following. |
| 0.5.0 Safari 27.2 | Tab fallback through the real launcher outside LibreOffice, app-browser discovery disabled; basic open/formatted turn/lost Send/Try again/Stop/host-quit close. Not an installed Safari lifecycle matrix. |
| 0.5.0 Windows 11 | LibreOffice 26.8.3.1, Firefox 160 as default HTML handler: installed extension, panel-opened tab, real plugin, fake Runs API, reply/lost Send/Try again/Stop/quit. No native app-window or following evidence; no gateway/model. |
| Owner-reported 0.5.0 macOS, 2026-11-21 | Plugin `797aeb5`, same extension bytes; Extension Manager replaced 0.4.0; Hermes 0.23.1 gateway/configured model wrote/read a Calc cell, Stop interrupted a reply, quit closed Sidecar. One reported chat, not a complete scenario matrix or public plugin-update test. |
| 0.5.0 automated accessibility | Eight headless-Chrome scenarios against the real plugin. Not VoiceOver/NVDA and not the new 0.5.1 controls. |
| Historical private CI | Six OS/Python jobs on candidate `352f7fe`. Linux units are not Linux native-window support. No final 0.5.1 CI pass implied. |
| Windows socket probes | 0.4.1 socket-level same-account bind/rebind probes; later 0.5.0 extension-listener administrator/LOCAL SERVICE cross-account checks. These are not native polling/restart tests with a normal limited-token holder or a second interactive standard account. Exact wildcard routing qualifications remain in SECURITY.md. |
| D2 scratch follow investigation | One 1× display, macOS 27.2.1, LibreOffice 26.8.2.2, Chrome 158.0.8127.36. Shared transport and guarded browser hand-move tests were bounded observations; callback maximum 11.4262 ms exceeded the 5 ms rule and actual LibreOffice drag displacement was not qualified. Not production or installed acceptance; not a default-on endorsement. |
| Q255 follow-up | The D2 255 exit reproduced without an extension: harness SIGTERM after first-run Welcome UI, not normal Quit. With Welcome suppressed, normal quit with D2 listeners exited 0. This resolves that exit-code attribution, not every possible shutdown race; it is not a clean-exit pass for the fresh candidate quit gate. |

The original 0.4.0 and 0.5.0 README/CHANGELOG results retain their dates, versions and owner attribution. Their earlier “not run” lists describe those releases, not fresh failures in 0.5.1.

## Initial integration observations — superseded candidate

The following retained results belong to initial integration head `8bac5df` and archive `0e23e3571b5b77bbb953e6d8c27a206e1ff48fee2cf686653674cd53529b5d02`, not the new round-2 digest above. Their failures are historical observations, not claims about the merged fixes. Current round-2 results appear below. Raw receipts are kept in private integration evidence, not shipped in this repository.

| Gate | Observed result and limits |
| --- | --- |
| Authentication and boundaries | Real-plugin revision-5 probe: 50/50; legacy HTTP matrix: 39 passed after the three bounded slow probes. The literal 12-hour soak remains not run. Protected ownership/lifecycle files retain their published hashes. |
| Stop, Send, unload and shared pool | Integrated suite stress: eight readers progress, 2,400 signed bounds, maximum four parked, eight Stops (maximum 6.698 ms), two page loads and zero HTTP errors in 30.139 s. This is synthetic transport load, not eight visible native readers. |
| Real-plugin shipped page | Existing browser matrix 21/21; follow page 16/16; supplemental follow/accessibility policy 43/43; held late-state/Try again regression 7/7. Recorder geometry qualifies page policy, not native placement. ExternalTarget: five passes, one informational browser record and a static-mutability capability skip, not an all-green external matrix. |
| Styling and screenshots | Seven origin-pinned files verified and served sums match. [Following](assets/screenshots/0.5.1/following-light.png), [undocked](assets/screenshots/0.5.1/undocked-light.png) and [paused](assets/screenshots/0.5.1/paused-light.png) screenshots have corresponding `-dark.png` files: actual shipped-page 400-pixel headless renders with recorder geometry. Vision inspection found readable, unclipped controls in all six. Separate native following/undocked captures were also inspected. |
| Automated accessibility | Existing browser checks and supplemental named/keyboard switch/Reattach, polite undock and no-follow-announcement/focus-move checks passed. Manual VoiceOver/NVDA: not run, not required. |
| Native macOS — partial, not G6 PASS | Disposable installed extension, real plugin/fake Runs API: default OFF before opt-in; all 50 moves and 20 resizes land, median 28.628 ms, p95 67.674 ms, max 214.716 ms. Actual CG sample intervals median 36.962 ms / p95 52.599 ms. Three real LibreOffice title-bar drags displaced the document and following landed. All ten attempted Sidecar hand changes undocked/stayed and Reattach landed, but only nine complete pointer trajectories passed displacement guards; the tenth is not a qualified full input trajectory. Minimize/restore and a supplemental ordinary-large-window fullscreen check passed. Full G6 remains incomplete, including focus, independent two-window/fifth-window latency, replacement/failure scenarios and burst timing. |
| Cold-channel first bounds — FAIL | Ten real-plugin transport-only cold starts after the local suite finished: median 186.105 ms, p95 238.130 ms, max 249.312 ms. Median exceeds the 150 ms target; this does not invalidate the separately measured established-channel native movement results, but the cold-start integration obligation remains open. An earlier concurrent-suite run also exceeded the median target (218.287 ms); neither result is waived or relabelled a pass. |
| Callback cost and clean Quit | A separate installed, bound listener trial measured 1,948 actual move/resize callbacks: raw p100 0.155875 ms, no outlier waiver. Cost includes real UNO reads and handoff; page preference remained OFF in this cost-only trial. Three-second native settling after measurement is outside timed samples. Exact-PID normal Quit returned 0, browser gone, no owned crash or restore prompt; no forced cleanup. The native runner's overall status alone is not taken as proof of its nested checks. |
| Five-window native capacity | Five real LibreOffice documents, five native Sidecar windows/pages, four hanging turns: all four Stops delivered, max 5.621 ms. Pages obey visibility, so this is not eight simultaneous visible follow readers. Capacity Quit returned 0. Burst final position was exact/stable, but its cross-runtime elapsed-time calculation was invalid and is not accepted as latency evidence. |
| Windows regression — BLOCKED | Candidate installed and booted in a disposable profile in the limited interactive console session. The UI Automation harness could not find the owned Open Sidecar button after two input-discovery attempts; no candidate tab/chat regression pass is claimed. Setup and measured cleanup Quit returned 0. Fresh SSH verified all run files/tasks deleted, no soffice remaining, and `%APPDATA%\\LibreOffice` absent. No-follow observations before any launch do not establish the required Windows no-follow-launch gate. |
| Upgrade and mixed pairs | Installed 0.5.0 extension + 0.5.1 real plugin, and installed 0.5.1 extension + 0.5.0 real plugin: pages connected; no follow key, no negotiated following; clean Quit. Same disposable profile upgraded from identifier `com.atlasomnia.sidecar.libreoffice` 0.5.0 to 0.5.1 through ExtensionManager approval, pairing preserved and page connected after switching real plugin implementations. This is not a public `hermes plugins update`, catalog upgrade or persisted plugin-config migration test. |
| Packaging and private CI | Two byte-identical builds and `build_oxt.py --check` match both README hashes. Isolated `hermes plugins validate --install-deps` passed, home deleted; payload scanner passed on a fresh Git archive. Final-head six-job private CI is tracked in the private integration report, not inferred from historical CI. |
| Real gateway/model | Pending one bounded owner-run macOS candidate chat (G9), performed by Jonathan. No gateway/model was started by integration. |
| Catalog publication/admission | Pending publication, full public pin, separate catalog submission and public update check. Local YAML remains a template. |


## Integration round 2 — merged fix packages

The deterministic archive at the top of this ledger combines cold-channel latency (A), native resize batching (L), attachment-scoped release (B1, protocol 5.1), and Print Preview lifecycle/recovery (PP). Following remains OFF by default. This is a candidate, not publication or unconditional release acceptance. The private `INTEGRATION-2-REPORT.md` records exact head, commands, native latency and mixed-pair receipts, CI URL, all failures and cleanup.

| Gate | Observed round-2 result and boundary |
| --- | --- |
| Full suite | Twice: 709 tests, five skips, OK; 261.897 s and 260.948 s, with TMPDIR unset. Protected ownership/lifecycle bytes unchanged. Later documentation-only edits do not change measured extension/page/plugin executable bytes; final-head private CI is recorded separately. |
| E03 stalled release | RED: all four legacy/scoped × worker/abandoned-mint cases made zero attempts after a five-second stall. GREEN: exactly one bounded initial attempt, no expired retry or restarted budget. The real-handler attachment lifetime matrix also passes. |
| HTTP / independent stub | Real plugin 37 passes + four timing skips, then all three feasible slow rows pass; only the literal twelve-hour row remains unobserved. Stub 41/41 passes. |
| Browser / accessibility | Shipped sidebar against real plugin: 21/21 browser checks pass. Follow-page checks pass against real plugin and independent stub. The historical fixture lacks `follow-status`, `reattach-button`, `follow-toggle`: its two section-7.3 ID failures are retained separately, not a shipped-sidebar failure or passing expanded aggregate. |
| Eight-reader transport / Stop | Suite stress: 2,400 signed bounds in 30.061 s, all eight readers progress, maximum four parked, eight Stops, maximum Stop 6.295 ms, two page loads, zero HTTP errors. Synthetic HTTP, not eight native visible pages. |
| Native Print Preview | Installed final archive + real plugin/fake Runs API, disposable profiles: Writer and Calc OFF/ON preview open/close during slow replies; no Stop, replies render/complete, same-model listeners survive and ON following resumes. Writer second-view close preserves the reply/document. |
| Native callbacks / title-bar input | Calc actual installed bound listener: 1,950 callbacks, raw p100 0.092417 ms, below 5 ms. Three real guarded LO title-bar drags displace the known document and following lands. |
| Native close/reopen | Same Calc file closed and reopened while three authenticated old release handlers were held. Replacement page opens, reply completes, no replacement Stop. LibreOffice gives the reopened model a different document ID; the same-ID attachment guarantee is proven separately by the real-handler unit matrix, not inferred from this native run. |
| Windows / ATLAS — partial | Installed final archive in limited interactive session; real posted panel click opens default Firefox tab, formatted turn, lost Send/Try again (one run), Stop and no-follow pass. Last-document close did not remove the tab within the harness's three-second check; only host_quit release was observed, so independent document_closed release is not qualified. Later cleanup sees zero Sidecar tabs. Fresh SSH confirms no test files/tasks/soffice, and no roaming LibreOffice profile. This is not an all-green G7. |
| Publication / owner-only work | No public tag, push, release, catalog write or real gateway/model run. G9 remains owner-run. Manual screen-reader and hardware limitations below are not waived. |

## Explicitly unverified environments and behavior

- Second display; negative origins; displays above/left; Retina/2× or mixed scale; display added/removed or resolution changed. Intended fallback is pause, move Sidecar by hand, Reattach; no hardware pass.
- Green-button fullscreen, Spaces, Stage Manager and tiling. Fullscreen remains ordinary-large-window behavior, not a qualified Space signal.
- Long human hand-drag “feel”; scripted placement alone is not proof of an actual document drag.
- Edge, Brave and Chromium on macOS; Firefox on macOS; Edge or Chrome as Windows default; Windows 150% scaling.
- Intel Macs, macOS other than 27.2.1 and LibreOffice other than 26.8.x; more than eight windows; two frames on one document.
- Managed browsers, forced sign-in/extensions, URL/proxy blocks, browser discard/sleep, Store-packaged LibreOffice and future Chrome movement changes.
- Manual VoiceOver/NVDA (not required); literal 12-hour soak; normal limited-token versus a second interactive standard-account listener takeover/routing checks.
- Native Linux Sidecar window is unsupported, not merely awaiting a unit-test pass.

## D-final hand-off — only after extension and page freeze

1. Record exact candidate commit, extension digest, page sums, versions, real/fake upstream, observed results and cleanup receipts. Do not use a scratch/interim hash as a release hash.
2. Add real candidate light/dark window screenshots; retain the old panel-image provenance and do not fabricate replacement screenshots.
3. Build deterministically and keep **both** README hashes equal to the candidate digest. Keep CHANGELOG `## [0.5.1] - Unreleased` until release; date it only when publishing. Keep current 0.5.1 version pins and identifier.
4. Reconcile template description/known issues against the actual separate catalog submission, retaining its historical evidence and disclosure detail. Do not overwrite a submitted entry from this template or describe a source-only draft as published. Resolve SHA and pinned docs URL only after release commit/tag publication. No catalog write is performed in this lane.
5. The supported distribution instruction is building the companion extension from tagged source; Hermes installs only plugin/. Do not promise a prebuilt release asset unless its publication is observed. Ensure source-tag/release-asset instructions match what is actually published and that the plugin-only install carries README, notices and after-install instructions.
6. Run packaging, manifest/license checks, page sums, full suite and exact-candidate CI/validation/scanner. Update this ledger from actual receipts, leaving every omitted check not run. Public update/pin/admission remain pending until externally verified.
