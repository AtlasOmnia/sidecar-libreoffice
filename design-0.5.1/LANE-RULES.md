# Sidecar for LibreOffice 0.5.1 — lane rules

Binding: `design-0.5.1/PLAN-FINAL.md`, especially Owner decisions and §7.2; contract `docs/web-protocol.md` revision 5. Follow LibreOffice is OFF by default; opt in with `sidecar.follow=on`. No lane may promote the default.

Only Day-0 owner/integrator writes `/Volumes/Development SSD/Repositories/sidecar-libreoffice-0.5.1` on `dev/0.5.1`. Public clone and scrub planning clone are read-only. Quote all paths. After Day 0 is complete, resolve and record its head from `dev/0.5.1` and clone at that exact SHA:

```sh
git clone --branch dev/0.5.1 "/Volumes/Development SSD/Repositories/sidecar-libreoffice-0.5.1" "/Users/jonathan/.hermes/profiles/omh/cache/scratch/051/lane-<X>/repo"
# Verify HEAD equals the Day-0 head in the dispatch; stop if it differs.
git -c user.name="Atlas Omnia" -c user.email="110573478+AtlasOmnia@users.noreply.github.com" commit ...
git format-patch <day0-head>..HEAD --output-directory "<integration>/design-0.5.1/lanes/<X>/"
```

Deliver those patches and `REPORT.md` into `design-0.5.1/lanes/<X>/` (delivery artifacts are the only allowed writes outside the clone). Report base/head, changed files, commands/counts, actual results, limitations and cross-file requests. No amends, pushes, tags, installs or public GitHub writes. Reject patches outside ownership; ask the integrator, never fix another lane's file. No software installs or model/provider runs. Use fake Runs APIs; Jonathan owns G9.

## Exclusive file ownership (PLAN-FINAL §7.2)

| Lane | Owned files | Deliverables |
| --- | --- | --- |
| **A. Plugin** | `plugin/web_api.py`, `plugin/web_auth.py`; `tests/test_web_api.py`, `tests/test_web_auth.py`, `tests/test_web_turns.py`, `tests/test_web_unload.py`, `tests/test_web_send.py`, `tests/test_web_static.py`, new `tests/test_web_window.py`; `tests/support/fake_runs_api.py` | `caps`; optional `follow` and its echo; `/launcher/bounds`; `/api/window` with the shared pool and turn priority; exchange `follow`; unload with parked window polls; deterministic idle expiry; G1 and G2 rows |
| **B. Page** | `plugin/sidebar/app.js`, `plugin/sidebar/index.html`, `plugin/sidebar/sidecar-lo.css`, `plugin/sidebar/SHA256SUMS`; `tests/test_web_bundle.py` | commits in this order: P03, P02, follow (poll loop, undock rule, Reattach, toggle, paused texts, other-display rule); sums regenerated; works against V's stub from day 1 |
| **C1. Extension core** | `extension/python/sidecar_launcher.py`, `extension/python/hermes_sidebar.py`, `extension/python/launcher_macos.py`; in `extension/python/hermes_bridge.py` only the exclusive-bind comment (E16); `tests/test_launcher.py`, `tests/test_sidebar.py`, `tests/test_launcher_macos.py`, `tests/test_launcher_windows.py`, `tests/test_windows_regressions.py`, `tests/test_extension_menu.py` | commits in this order: E05, E04, E06, follow wiring (`caps`, `follow` key, signed echo, bind at `exchanged`, the UNO listener registry, the F7 bounds rule, kill switch), E03, E16; a test that on Windows nothing attaches and no `follow` key is sent |
| **C2. Follow engine** | `extension/python/window_follow.py`, new `tests/test_window_follow.py` | the pure module of 13.2: latest-value slot, send-at-once plus spacing plus trailing edge, channel renewal, failure and back-off rules, `seq`, stop at quit; unit-tested with a fake client and clock |
| **D. Docs and release** | `README.md`, `plugin/README.md`, `plugin/after-install.md`, `SECURITY.md`, `CHANGELOG.md`, `NOTICE`, `plugin/NOTICE`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/**` except `web-protocol.md`, `catalog/atlas-sidecar-libreoffice.yaml`, `plugin/plugin.yaml`, `extension/description.xml`, `extension/README.txt`, `tests/test_manifest.py`, `tests/test_packaging.py`, `tests/test_license_headers.py` | draft early; D01–D05; the follow disclosures; **D-final** (hashes, date) only after the extension and page freeze |
| **V. Verification** (no product files) | `scripts/web_smoke/**` including new `follow_probe.py` and `follow_page.py`; evidence under `design-0.5.1/acceptance/`; the native harness under `design-0.5.1/acceptance/harness/` | stub routes on day 1; T02, T03, T05, T06; G1–G7 runs; V10, V13, V14 |

**Frozen for everyone** (a change needs an integrator amendment recorded in the ledger): `tests/test_run_ownership.py`, `tests/test_chat_lifecycle.py`; `plugin/chat_bridge.py` after S1; `plugin/__init__.py`; `extension/python/private_files.py`, `panel_registry.py`, `office_ops.py`, `range_logic.py`, `launcher_windows.py`; `extension/*.xcu`, `extension/dialogs/**`; `scripts/build_oxt.py` and `manifest.xml` after S2; `plugin/sidebar/vendor/**`, `omnia-*.css`, `taskpane.css`, `omnia-theme.js`, `UPSTREAM.json`; `.github/workflows/**`; every test file not named above.

## Machine and process safety

Never touch, drive, query, quit or install into the everyday LibreOffice or Chrome/browser profiles. Never use `unopkg add`. Native runs only in disposable `-env:UserInstallation=file://…` profiles and dedicated browser `--user-data-dir` under the lane scratch directory. Follow AGENTS.md's guarded-run/sentinel rules. Target only captured, owned exact PIDs, including accessibility; stop **every process started**, record exit codes and remove disposable profiles and exact owned socket dirs. Do not use global process-name kills. D2 Welcome/floating windows must be cleared before real document drags; input posted is not displacement proved.

Do not read/change live Hermes config/plugins/profiles/gateways. Never use ports 8642, 8660, 8661 for tests; use ephemeral ports. Temporary Hermes homes are per-command `env HERMES_HOME=<fresh outside ~/.hermes> hermes …`, never exported, deleted afterwards; never start a throwaway gateway. Do not print/save secrets, session tokens or pairing tokens. Scratch is `/Users/jonathan/.hermes/profiles/omh/cache/scratch/051/lane-<X>/`; temporary Node work uses profile scratch, not system temp. For Mac unit gates **unset TMPDIR** (the Unix-socket path otherwise exceeds its length limit).

ATLAS only if dispatched: `ssh -o BatchMode=yes -o ConnectTimeout=8 jarvis 'hostname && whoami'`; expect ATLAS / atlas\jrive. Substantial PowerShell goes in transferred `.ps1` files via `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File`. No firewall/UAC/policy/default-browser changes. New folder under `C:\Users\Jrive\aow\`; delete at end; `%APPDATA%\LibreOffice` stays absent before/after. Exact PIDs only.

Stdlib-only Python 3.9+, SPDX Apache-2.0 and Copyright 2026 Atlas Omnia on new source. Plain-English shipped docs; no internal paths/hosts/names in product payload. `tests/test_run_ownership.py` and `tests/test_chat_lifecycle.py` are never edited. Expected SHA-256: `a600cb01d6be05ee3025410fd222929678636f41fed2a410f2e59cad3e11d860`, `259b1e2e4225967a8889418e0074ac785b9a794cdc6f515e1423d9ae10c01227` respectively.

## Verification / stop

Run `env -u TMPDIR python3 -m unittest discover -s tests` and lane gates before reporting; record counts and full failing output. Protected-suite reruns allowed only for the already-classified fake-clock millisecond signature, retained and explained. No assertion weakening. Full verification after state changes; maximum two fix-and-verify rounds, then report criterion-specific blockers. No claim that stub or scratch spike results are installed acceptance. Safety, Stop, mixed-pair, focus, Windows unchanged and CI stop rules from PLAN-FINAL §10 remain binding. C1/C2 share the callback hand-off contract, not files: p100 ≤5 ms over ≥1000 measured actual callbacks. Contract amendments go only through integrator with vectors and stub updated together.
