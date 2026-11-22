# Sidecar for LibreOffice 0.5.1 — draft plan

**Status: BACKLOG + PLAN DRAFT. Not an implementation authorization or a release commitment.** Jonathan has chosen the two headlines below. Their mechanisms, supported cases and effort remain pending spikes **D** (macOS docking/follow) and **W** (Windows app window). Do not treat a spike recommendation as a shipped feature or guess its outcome.

Baseline inspected read-only: `/Volumes/Development SSD/Repositories/sidecar-libreoffice-0.5.1`, branch `dev/0.5.1`, commit `4d65e1ecc5eba83470cc7f81f0fa78a8af172b33`. Public 0.5.0 is the supplied release baseline. Backlog sources and reconciliations are in [BACKLOG.md](BACKLOG.md). Only this design folder was writable for this planning task.

## 1. Product goal and success

Make Sidecar feel attached to the document rather than like an unrelated browser window, without losing the safety and lifecycle work in 0.5.0.

Success means:

1. On the supported macOS reference setup, moving/resizing a LibreOffice document window makes its existing Sidecar follow according to the approved D contract, without another window, focus theft or UI stalls.
2. On ATLAS, clicking Open Sidecar opens one usable app-style window rather than a normal browser tab, using the approved W design. Repeated clicks address the same document/window; quit and document close remain safe.
3. Existing real chat, Stop, lost-Send recovery, document identity, rendering, accessibility, off-by-default and ownership guarantees still pass on the exact candidate.
4. Documentation says what was actually tested and names unsupported cases. Every accepted gate has an evidence path, exact candidate/plugin/extension identity and observed result; anything waived has an explicit owner decision.

The 0.5.0 evidence is the baseline, not automatic 0.5.1 acceptance. The owner-reported real Calc write/read/Stop/quit is not a complete real-model matrix, and automated AX is not VoiceOver/NVDA.

## 2. Non-goals and invariants

- No Linux native window support or live text/tool-status streaming unless Jonathan separately approves a demonstrated cheap, safe addition. Current recommendation: neither in this release.
- No persistent transcript/history, distributed exactly-once New chat, settings migration redesign, general host-liveness/crash-recovery protocol, or browser-policy workarounds.
- Do not add auto-open when the panel appears or a browser offline page while chat is off. Launch stays click-only and `chat_port: 0` starts no process.
- Keep deprecated `/chat` and `/health` compatible through 0.5.x; removal belongs to 0.6.0.
- No broad refactor of run ownership or unload. Preserve original-key Stop, unresolved-run accounting, terminal-only/idempotent release and the accepted Hermes fence-removal-on-unload disclosure.
- No pairing/session credentials in process arguments, URLs used for geometry, logs or screenshots. Keep owner-only bootstrap files, proof-checked launcher responses, memory-only page session tokens, strict CSP/static hashes and renderer allow-list.
- Never manipulate everyday LibreOffice/browser profiles as a test shortcut. Native installs, screen-reader sessions, browser installs and provider/model use need the appropriate explicit authorization.
- Keep `tests/test_run_ownership.py` and `tests/test_chat_lifecycle.py` byte-identical. A fake-clock harness change needs a separate owner decision; do not widen assertions to make a new production defect disappear.

## 3. Headline A — follow LibreOffice (E01, E07)

**Placeholder; depends on spike D's report in `design-0.5.1/` (exact report path/decision to be supplied by the parent after the spike returns).** Do not reserve or overwrite the spike worker's files.

D must answer, with native observations:

- How LibreOffice move/resize events are observed safely, and whether delivery is on an appropriate thread.
- How the exact document's Sidecar is identified when browser processes are shared; application activation alone is not document-window targeting.
- Whether existing browser/page operations can move/resize an already-open window reliably, without unsafe in-process Objective-C geometry or accessibility permissions being silently assumed.
- Coordinate units and title-bar/work-area handling across 1×/2×, multiple displays and negative coordinates; throttling/coalescing so a drag does not flood requests or lag LibreOffice.
- Behavior on minimize, maximize/full-screen, display changes, manual Sidecar repositioning, browser/tab fallback, disabled native support, close and quit.
- Permission requirements, kill switch/fallback and cleanup of observers/threads. No retained observer may move a closed/replaced document's window.

**Recommended starting UX, subject to D:** follow the associated LibreOffice window while in supported app mode; preserve a usable overlap when there is insufficient space; no focus changes during movement; tab fallback remains an ordinary tab and clearly does not dock. Prefer a simple stop-following rule over repeatedly fighting a user's manual movement. Do not commit a toggle/indicator until D establishes it is needed.

**Fill after D:** selected mechanism; new files; contract changes; OXT effect; supported/unsupported cases; measured update latency and throttle; proof matrix; effort; owner choices. A quantitative follow-latency threshold must be agreed from the spike measurement before implementation starts, not invented here.

## 4. Headline B — Windows app window (E02)

**Placeholder; depends on spike W, whose reserved output area is `design-0.5.1/spikeW/` (final report/decision pending).** The earlier S3 result said Edge app mode was not viable for 0.5.0; its constraints are inputs, not proof the new approach works.

W must demonstrate:

- A real LibreOffice-triggered app-style window under ATLAS's normal limited interactive user, with an isolated profile and existing user browsers left alone.
- Correct browser discovery/profile ownership, no unexpected first-run/sign-in UI, and an explanation of Startup Boost/shared process behavior. Launch success is authenticated page exchange, not a child-process exit code.
- One window per document and repeat-click behavior; bounds and placement at 100%/150%; document-specific raise if available; multi-document close/quit safety.
- No launch secret on the command line; owner-only bootstrap/cache folders and no inheritance of LibreOffice runtime environment pollution.
- Clear behavior if the selected browser is absent/managed or app mode fails. No automatic duplicate launch or policy bypass.
- Whether an out-of-process helper/runtime is required. If so, supply packaging, provenance, architecture, scan/update and disclosure implications before adoption.

**Recommendation, subject to W:** use the smallest already-installed browser-based app-window path that can pass native evidence. Retain an explicit, documented normal-tab fallback rather than claiming universal app mode. Do not select a new packaged runtime merely because it looks more native.

**Fill after W:** selected approach/browser(s); exact files/contract; dependencies; profile isolation; fallback policy; permission/scaling constraints; lifecycle evidence; size/effort and owner decisions. Windows docking/follow is not silently promised by proving a Windows app window; D/W must say whether it is also feasible and in scope.

## 5. Recommended backlog subset

These are recommendations, not approval of the entire backlog. Backlog IDs are the source of truth; no hidden “while here” work.

### Product changes to carry with the headlines

- **E03–E06:** bounded document-close release retry, accurate unsafe-cache failure text, whole-string launcher validation, exited hand-off child cleanup. Small extension corrections while the archive necessarily changes.
- **E07:** exact document/window targeting as needed by docking. If it cannot be made reliable without a large native surface, obtain an explicit owner amendment; do not quietly substitute app-wide activation.
- **E16:** exact-address bind comment while regenerating the extension archive.
- **P02–P03:** clear delayed-Send/deduplication wording and the stale failed-poll banner after Try again. Regenerate sidebar sums and rerun page gates.

### Verification and documentation enabling that release

- **T02–T03, T05–T06:** real-plugin/fixture parity, truthful observed fields, bounded browser gate and ExternalTarget validation, exact-PID native/crash/cleanup evidence. If supporting parity work becomes large, prioritize affected scenarios and explicitly mark the remaining fixture gaps; never use the fixture as substitute product proof.
- **D01–D05:** current screenshots, reconciled catalog text, accurate evidence ledger, address-specific language and retained browser/focus/failure limitations.
- **V01–V03, V05–V06, V09–V11, V13–V14, V16–V17:** actual accessibility, named fallback browsers, scaled/multi-display headline behavior, complete real-gateway scenarios, upgrade, native rebind, fallback lifecycle, disposable-profile prior-service-worker analysis, appearance claim and final release gates.
- **T07 prerequisite only:** deterministic expiry against the real session implementation; the literal 12-hour soak stays later and explicitly unrun.
- Add a native accented/non-Latin cache/profile-path test with no relaxation of private-path checks. The older malformed non-ASCII HTTP-path bug is already fixed and remains a regression, not a new implementation task.

### Deliberately leave out

E08–E13, P01, P04–P12, T01/T04 and the literal T07 soak, D06, V04/V07–V08/V12/V15 remain later; E14–E15 are dropped as standalone feature directions. E01's geometry transport supersedes re-snap/focus-push ideas, not a second feature stream. P12 remains a host-side security dependency with explicit disclosure. Do not turn this into an ownership rewrite, settings redesign or six-browser support project.

## 6. Lane split and single-file ownership

Reuse the 0.5.0 model, with an explicit contract owner before parallel product work. These are planning assignments. New helper filenames from D/W are assigned at the contract freeze; no two lanes edit the same file. A lane requests cross-boundary changes from the owner rather than editing them itself.

| Lane | Owns | Deliverable / dependency |
|---|---|---|
| **Contract owner / integrator (C0)** | `docs/web-protocol.md`; design decision/acceptance ledger; final integration sequence. | Resolve D/W, document geometry/window-target identity, message shapes/statuses/version compatibility, budgets/fallback/permissions and native helper boundaries. Freeze before plugin/page/extension implementation; record later amendments. Own no overlapping product files. |
| **A — Plugin** | `plugin/chat_bridge.py`, `plugin/web_api.py`, `plugin/web_auth.py`; `plugin/__init__.py` only if justified; `tests/test_web_{api,auth,static,turns,send,unload}.py`, `tests/test_stop_lifetime.py`, `tests/test_drive_seam.py`, `tests/support/fake_runs_api.py`. | Minimal authenticated geometry/state changes only if D needs them; keep handler budgets/ownership intact. Add tests for changed contract; coordinate admitted retries/capacity regressions. Depends on C0. No optional Stop-lifetime redesign. |
| **B — Page** | `plugin/sidebar/**`; `scripts/sidebar_sums.py`, `scripts/verify_vendor.py`; `tests/test_web_bundle.py`; `.github/workflows/vendor.yml`. | D-approved movement consumption if needed; P02/P03; accessibility/renderer unchanged except proven fixes; sums. Works against C0/V stub but must pass real-plugin/page gates. Upstream CSS remains pinned, not redesigned. |
| **C — Extension** | `extension/python/sidecar_launcher.py`, `launcher_macos.py`, `launcher_windows.py`, `hermes_sidebar.py`, `panel_registry.py`; `extension/dialogs/**`, `Sidebar.xcu`, `Addons.xcu`, `META-INF/manifest.xml`; `scripts/build_oxt.py`; launcher/sidebar/menu/Windows tests. In `hermes_bridge.py`, only document-close/terminate/dev-launch hooks and E16 bind comment unless a separate amendment is approved. | Follow observers/window targeting, W adapter, E03–E07/E16. Platform subworkers may own macOS and Windows adapters separately, but one extension lead owns common state and packaging. New helper filenames and their tests assigned here at C0. |
| **D — Docs/release** | `README.md`, `plugin/README.md`, `plugin/after-install.md`, `SECURITY.md`, `CHANGELOG.md`, `NOTICE`, `plugin/NOTICE`, `AGENTS.md`, `CONTRIBUTING.md`; `docs/**` except `web-protocol.md`; catalog template, `plugin/plugin.yaml`, `extension/description.xml`, `extension/README.txt`; manifest/packaging/license tests. | Draft early; finalize version, supported cases, disclosures, screenshots and both README extension hashes only after extension/page freeze. New native helper disclosures require observed facts from C. |
| **V — Independent verification** | `scripts/web_smoke/**` including stub/fixture and parity/evidence repairs; `.github/workflows/tests.yml` and a new bounded browser workflow if approved; new cross-boundary verification-only scripts/tests with filenames reserved before editing; evidence under `design-0.5.1/acceptance/`. No product-file edits. | Real-plugin matrix, deterministic expiry, native macOS + ATLAS runs, G1–G9 ledger, load/failure probes, accessibility and crash hygiene. Renders/screenshots against the shipped bundle, not only the fixture. |

`tests/test_run_ownership.py` and `tests/test_chat_lifecycle.py` are owned by nobody for edits. Verification can run and hash them. Lane D's version/description changes alter OXT bytes: freeze those before building native-test artifacts, not after declaring native gates passed.

## 7. Sequence and integration

1. Receive D/W full reports. C0 records chosen mechanisms and unresolved owner decisions, quantified follow threshold, platform matrix and helper file ownership. No capacity/date commitment before this.
2. Freeze the shared contract and test vectors. V prepares the stub/parity assertions; A/B/C work in isolated trees. D drafts text with explicit placeholders, not guessed pass claims.
3. Integrate A+B against the real plugin with a fake Runs API. Then C on disposable native LibreOffice profiles. Exercise multiple documents before single-window polish is accepted.
4. Freeze extension, version labels and all packaging inputs; build and identify exact OXT bytes. Freeze page bytes/sums. Run the full gate pass and collect failures as well as successes.
5. Address criterion-cited failures narrowly; changed inputs invalidate only the affected gates plus shared regression gates. Retain superseded evidence with the source commit. Run separately authorized real-gateway/model and screen-reader sessions.
6. Jonathan accepts or explicitly amends remaining gates; release owner handles final private CI, publication and catalog pin validation. Planning does not authorize a push, tag, catalog update or install.

## 8. Acceptance gates (G1–G9 adapted from 0.5.0)

Evidence path: `design-0.5.1/acceptance/<gate>/<run>/`. Include commit, OXT hash, page sums, OS/LibreOffice/browser/Hermes/Python versions, exact target PID/profile, real vs fake upstream, observed result and cleanup. **PASS**, **FAIL**, **NOT RUN**, and **OWNER-AMENDED** are different states; successful dispatch is not a pass. Avoid counting fixture copies or repeated checks as independent product scenarios.

| Gate | Required observed result |
|---|---|
| **G0 — spike/contract prerequisite** | D and W reports read, chosen paths/limits and file ownership frozen, owner-only questions resolved. D's measured follow-latency threshold and supported coordinate/focus behavior written into C0; W's runtime/browser/fallback choice explicit. No implication that this is native release acceptance. |
| **G1 — authentication / boundaries** | Retain real-plugin Host/Origin/fetch/role/method/code/session/static/CSP/log negative controls and independent launcher vectors. Test all new geometry/window-target messages against tampering, stale document/session and unauthorized origin. No credential or geometry-control URL shortcuts. Malformed non-ASCII HTTP paths remain clean 404s. Bootstrap file permissions, channel counters, off mode and hashes intact. Deterministic real-session idle expiry tested; 12-hour wall-clock run separately labeled. |
| **G2 — ownership / lifecycle / capacity** | Both protected suites byte-unmodified and green; original-key Stop, truthful unresolved counts, one effective release, stale/late ID and thread-start failure regressions pass. Ordinary close shares one deadline and collective grace; event-loop exceptions stated. Multiple documents and active turns retain Stop responsiveness; include admission-wait retries plus parked polls under handler cap. E03 retry is bounded, targets only the closed document/generation, handles busy/no answer and shutdown races, and never resurrects a cancelled launch. Observers/workers stop after close/replacement/quit. |
| **G3 — shipped-page browser behavior** | Real plugin + shipped page + fake Runs API: Markdown/injection/CSP corpus, same-ID held admission/lost answer, first/retry busy/refusal/already_sent, unknown outcome/Try again, rejected text, stale state/New chat, Stop and one final outcome. New regression: a poll failing after successful Try again cannot restore the old unreachable banner. Distinguish local serialization from remote exactly-once. Recheck any D movement path and stub/fixture parity; actual observed evidence fields, validated ExternalTarget path. |
| **G4 — appearance / window fit** | Pinned CSS/vendor/sums remain valid; finish computed-style/layout comparison at 300/400/520 px light/dark or obtain explicit narrowed exact-look claim. Screenshots show new app window and docking at supported bounds without clipped composer/controls. D's native coordinate/fit results, not CSS hash equality, prove docking. |
| **G5 — accessibility** | Automated named-control/landmark/log/status and keyboard/modal/focus/reduced-motion gates plus actual VoiceOver and NVDA. Finished reply/status/unknown announcement once, Try again reachable with disabled composer, launcher F6/Tab and focus restore/Stop usable. Quit after native accessibility use must be observed; no-AX workaround does not satisfy it. If a screen reader causes a native crash, preserve report and seek a criterion-specific decision, not a fabricated pass. |
| **G6 — native macOS** | Exact candidate OXT/plugin in disposable LibreOffice; real panel click opens one Chrome-reference app window, repeat click targets the right document among multiple windows, D-approved move/resize follows without focus theft/jitter and within the agreed threshold. Test manual reposition/minimize/full-screen and fallback rules agreed at C0, insufficient space, close/reopen and close during launch; bounded lost-release retry; quit/relaunch/no restore prompt. 1×/2× and multi-display/negative coordinates supported or explicitly owner-limited. Accented/non-Latin profile/cache path checked; unsafe/symlink paths still safely refused. No launch secret in argv, no everyday-profile change, exact PID cleanup. Safari and macOS Firefox fallback matrix includes reply/lost Send/Stop/reload/duplicate/bfcache/host-gone with claims limited to actual methods. |
| **G7 — native Windows / ATLAS** | Limited interactive user, real LibreOffice panel, exact OXT and real plugin: W's app window rather than tab, authenticated exchange, formatted reply, lost Send/Try again once, Stop, New chat, multiple documents/repeat click, document close/cancel-in-flight, quit/relaunch. 100%/150% scaling and required display cases; no unsolicited sign-in/restore/duplicate windows; bootstrap ACLs/no secret argv and non-ASCII paths. Browser absence/managed failure and retained default-tab fallback observed for named handlers (Edge/Chrome/Firefox only as tested). Real polling-page plugin disable/enable/rebind and LibreOffice restart with accepted connections. NVDA through G5. Do not substitute old socket-only or administrator-holder evidence for limited native execution. |
| **G8 — reproducibility / private CI / release readiness** | Full exact-candidate suite and **private** CI macOS/Ubuntu/Windows × Python 3.11/3.13, all six jobs; vendor job and bounded real-plugin browser gate recorded. Protected digests unchanged, sidebar sums/vendor provenance clean, deterministic build matches both README hashes, manifest/disclosures consistent. Fresh authorized export/home plugin validation plus authoritative payload scan and recorded cautions, no dangerous finding. Final release identity, version/date, asset hashes, catalog/docs pin and submitted wording agree. Public pinned-entry verification occurs after authorized publication; do not call a placeholder pin admission-ready. Upgrade/plugin-update evidence in G6/G7 or linked ledger. |
| **G9 — real gateway / model, separately authorized** | Actual supported Hermes-managed chat: document read, edit/readback and undo, fenced refusal, real approval-state behavior (Sidecar never grants approval), Stop during a tool, close during reply, disable/reload during reply including immediately after Stop. Record actual unload callback context, stops with the run's original key and truthful owned/unresolved counts; verify no traceback or silent restart bind failure. No claim that existing host fence removal was fixed. Perform on macOS and a bounded ATLAS real-gateway chat for the new Windows feature, or obtain an explicit owner limitation for the latter. Provider use/cost requires authorization. |

For upgrades, test 0.5.0→0.5.1 with one extension row, same identity/token/settings and unchanged default-off behavior, plus the actual published-pin `hermes plugins update` path when available. A prospective version cannot prove its own public update before its pin exists; keep that post-publication gate explicitly pending or use a separately approved private install/update pin and still verify the public path later.

Failure handling: preserve raw failing output. Only the already-attributed protected shared-clock millisecond signature may be rerun with a recorded classification; any different identity, large overshoot, real-I/O wait, missing Stop or bind failure requires analysis. Do not change protected files without approval, or use a green rerun as the only record. Native crash evidence requires exact process/path attribution; absence of Python frames is not proof of harmlessness.

## 9. Risks and controls

| Risk | Control / decision boundary |
|---|---|
| D needs unsafe/native geometry or excessive permissions | Keep UNO-safe/out-of-process options preferred; no unchecked UNO-pointer Objective-C calls. If no safe path works, return the headline decision to Jonathan rather than quietly adding a large helper. |
| Shared browser process cannot target a document's window | Prove multi-document mapping before following/raising; otherwise owner narrows the claim. Never move whichever browser happens to be frontmost. |
| Windows S3 problems recur (DIP scaling, Startup Boost, silent sign-in) | W native evidence selects the path; use authenticated exchange as success; preserve explicit fallback and truthful browser-vendor disclosures. |
| Move events flood network/handlers or fight manual dragging | Coalesce/throttle, latest-valid-document generation only, bounded requests; agree manual-move policy and latency threshold after D. No new permanent parked stream by default. |
| Close-release retry races reopen/quit | Bound retries, keep generation/document identity, no locks held during network I/O, regression for stale release and already-cancelled launch. |
| Native evidence mismatches final archive | Version/description/helper files frozen before OXT hash and native gates; rerun affected gates after any extension-byte change. |
| Fixture passes hide real product divergence | Real plugin/page is authoritative; parity assertions and independent vectors; observed fields come from captured values. |
| Screen-reader testing collides with crash/keyboard ownership | Schedule explicit native sessions, preserve crash reports and separate accessibility from no-AX launch tests. Never take over everyday keyboard without approval. |
| Scope grows through housekeeping and evidence debts | Take the named subset; postpone theme/history/Stop refactor/Linux/streaming; owner-amended unsupported cases stay visible. |
| Origin/port takeover or host fence limitation is mistaken for solved | V14 bounded analysis plus unchanged disclosures; no new security guarantee merely because a window follows LibreOffice. |

## 10. Decisions for Jonathan (recommendations, not questions already answered)

| Decision | Recommendation |
|---|---|
| **Q1. If D or W fails the safe-feasibility gate, hold 0.5.1 or narrow a headline?** | Wait for both spike reports before selecting; prefer a short hold/bounded alternative over shipping two promised headlines with one silently missing. A failed spike is a decision, not permission to force unsafe native code. |
| **Q2. What exactly follows, and what happens after manually moving Sidecar?** | Start with macOS app-mode move/resize following; never move normal tabs. Prefer pause/detach after deliberate manual reposition and reattach through an explicit action if D can implement it simply. Confirm minimize/full-screen and Windows-follow scope after D/W. |
| **Q3. Is a new packaged helper/runtime acceptable on Windows?** | Prefer existing installed-browser app mode. Approve a new runtime only with W evidence, size/provenance/update plan and clear maintenance ownership; do not assume it is acceptable. |
| **Q4. Keep default-browser tab fallback?** | Yes, visibly label it as undocked fallback; keep click-only/no automatic second launch. If app mode cannot launch, explain first rather than opening a surprise duplicate tab. |
| **Q5. How much backlog ships with the headlines?** | Approve §5's bounded extension/page corrections and their test/docs dependencies. Exclude theme/history, Stop refactor, Linux and streaming; use the gates to prevent “small” fixes from bypassing review. |
| **Q6. May the protected fake-clock tests change?** | No for this milestone. Retain the known classification and add supplementary tests; authorize a separate harness correction later if CI cost justifies it. |
| **Q7. Accessibility/display/browser scope and permissions?** | Require manual VoiceOver/NVDA and scaled/multi-display headline coverage; authorize disposable Firefox installation if needed. Other macOS browsers/managed environments may stay explicitly untested. Any narrower accessibility/display claim needs your explicit acceptance. |
| **Q8. Real gateway/model runs and ATLAS gateway setup?** | Authorize isolated, bounded macOS full G9 plus ATLAS real-chat smoke with a cost ceiling. Keep fake API and owner-reported history distinct; do not assume permission or existing ATLAS gateway readiness. |
| **Q9. 12-hour soak or deterministic expiry?** | Deterministic real-implementation expiry for 0.5.1, with literal 12-hour soak labeled later/unrun. |
| **Q10. Exact-look claim?** | Run G4 if retaining “exact”; otherwise say pinned/shared Omnia styling and explicitly remove unsupported pixel/layout equivalence claims. |
| **Q11. Release timing/capacity?** | Set only after D/W select the mechanisms. No inherited eight-day 0.5.0 estimate or new day count is justified by this draft. |

## 11. Completion boundary

This draft and the backlog are the planning deliverables. No feature was implemented, platform gate executed, issue posted, CI dispatched, repository modified, release published or catalog submission updated by this unit. Next handoff is to the parent to merge D/W facts and obtain owner decisions; implementation lanes start only after that contract freeze.
