# 0.5.1 draft release and evidence ledger

**Status: Unreleased; not an acceptance or publication receipt.** This ledger separates the accepted candidate contract, historical observations and outstanding candidate gates. The README commands target the prospective v0.5.1 tag and must not be used until publication. Both expected archive hashes remain placeholders. A draft `build_oxt.py --check` prints an interim digest and explicitly says NOT a release hash match; it does not verify either final release hash.

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

## Frozen-candidate gates — pending evidence

| Gate | Current status and required receipt |
| --- | --- |
| Authentication, boundary and deterministic session-expiry checks | Pending combined candidate; include mixed-pair behavior and no new geometry/ID/title logging. Literal 12-hour wall-clock soak remains not run. |
| Stop, Send, unload and shared-pool capacity | Pending combined candidate; protected tests byte-identical and green, bounded close retries, eight-session/four-turn load, Stop under one second. Lane-local units do not replace this. |
| Shipped-page browser checks | Pending real-plugin page run with fake Runs API; capture actual observations, Try again/stale-poll regression, bounded Send wording, tab lifecycle and the external-target harness path. Stub-only results do not count as real-plugin acceptance. Mandatory local release gate; no added browser CI job. |
| Pinned styling and screenshots | Pending file-pin/sum checks and 400-point light/dark screenshots of following, undocked and paused states with readable controls. No identical-computed-layout claim or comparison required. Do not change Windows screenshots to imply an app window. |
| Automated accessibility | Pending existing scenarios plus named/keyboard-reachable Reattach and Settings toggle, one polite undock announcement, no focus movement or follow announcements. Manual VoiceOver and NVDA: not run, not required. |
| Native macOS | Pending exact candidate extension, real plugin/fake Runs API, disposable profiles and exact-PID observations: default off first, opt-in latency/capacity/hand-drag/resize/reattach/focus checks, actual document displacement, callback maximum ≤5 ms across ≥1000 actual callbacks, clean exit 0, no crash/restore prompt and cleanup. |
| Windows regression | Pending candidate default-browser tab, no follow negotiation/bounds calls/window polls, reply/retry/Stop/close/quit and limited-user private-file checks. Native listener rebind while a page polls is optional, not a new passed claim. |
| Upgrade and mixed pairs | Pending installed 0.5.0→0.5.1 replacement preserving identifier, pairing/settings/chat-off state and all declared old/new pair behaviors. Public `hermes plugins update` remains pending until a published pin exists. |
| Real gateway/model | Pending one bounded owner-run macOS candidate chat: read, edit/readback/undo, Stop during a tool, drag during reply, close during reply and quit; retain owned/unresolved counts. No model run is performed by the docs lane. |
| Packaging and private CI | Pending exact frozen-candidate deterministic build, both final README hashes, clean-export validation/scanner and all six private CI jobs plus vendor job. Re-run after candidate changes. |
| Catalog publication/admission | Pending release publication, actual full commit SHA, SHA-pinned docs link, separate catalog submission/update and public pin check. The local YAML is a template, not a submitted/admitted catalog entry. |

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
3. Build deterministically, then replace **both** README hash placeholders together with the same digest; date CHANGELOG only when that gate passes. Keep current 0.5.1 version pins and identifier.
4. Reconcile template description/known issues against the actual separate catalog submission, retaining its historical evidence and disclosure detail. Do not overwrite a submitted entry from this template or describe a source-only draft as published. Resolve SHA and pinned docs URL only after release commit/tag publication. No catalog write is performed in this lane.
5. The supported distribution instruction is building the companion extension from tagged source; Hermes installs only plugin/. Do not promise a prebuilt release asset unless its publication is observed. Ensure source-tag/release-asset instructions match what is actually published and that the plugin-only install carries README, notices and after-install instructions.
6. Run packaging, manifest/license checks, page sums, full suite and exact-candidate CI/validation/scanner. Update this ledger from actual receipts, leaving every omitted check not run. Public update/pin/admission remain pending until externally verified.
