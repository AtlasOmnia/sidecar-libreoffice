# AGENTS.md

Notes for coding agents (and people) working in this repository.

## Scope

Sidecar for LibreOffice. A LibreOffice extension (`extension/`, Python-UNO running inside LibreOffice) that edits the live open Calc, Writer and Impress documents and opens the Sidecar window from its sidebar panel, a Hermes plugin (`plugin/`) exposing `calc_*`, `writer_*`, `impress_*` and `libreoffice_undo` tools over an authenticated loopback bridge and serving the Sidecar window's web page (`plugin/sidebar/`) from its optional chat endpoint, and their tests and scripts. `docs/web-protocol.md` is the contract between the plugin, the extension and the page.

## License

Apache-2.0 (see `LICENSE` and `NOTICE`). Keep SPDX headers on new source files, including the page's own `.js`, `.css` and `.html` files in `plugin/sidebar/`; a test enforces them. Vendored files in `plugin/sidebar/vendor/` and the three Omnia stylesheets pinned in `plugin/sidebar/UPSTREAM.json` stay byte-identical to their upstream and carry no header. Do not add or copy third-party content without a license check, and name it in `NOTICE`.

## Rules that protect the user's machine

- Never touch the everyday LibreOffice profile (`~/Library/Application Support/LibreOffice` on macOS). Live checks use the isolated profile under `$HERMES_LO_DEV_ROOT` (see `scripts/dev_paths.py`).
- Never use `unopkg add`: its helper ignores `-env:UserInstallation` and can write to a profile you did not choose. Install only by opening the `.oxt` in the isolated instance (`scripts/install_isolated.sh`).
- Wrap live runs in `scripts/guarded_run.py`, which fails if the everyday profile changed.
- Do not touch the system clipboard outside the single Tools-menu token check.
- Kill only LibreOffice processes you started, by exact PID. The same holds for browsers: test the Sidecar window only with a browser instance you started, with a throwaway `--user-data-dir`, and never your everyday browser profile.
- Never commit credentials or pairing tokens. Nothing in this repository reads Hermes's `.env`, a credential store, a browser profile or another tool's login; the model-driven scenario harness is internal and is not part of this repository.

## Design rules

- The plugin and the extension are stdlib-only Python. Do not add dependencies. The only third-party code is the two browser libraries vendored unminified in `plugin/sidebar/vendor/` (recorded in `VENDOR.json`, never fetched at runtime).
- The bridge listens on loopback only and requires the pairing token on every request; its log carries route labels, never request paths.
- The chat endpoint's route families (`/launcher/*`, `/ui/*`, `/api/*`, legacy `/chat` and `/health`) each accept only their own credential, as `docs/web-protocol.md` specifies. The pairing token never travels to the chat endpoint from the 0.5.0 extension, and never reaches the browser.
- The extension starts a browser only from an **Open Sidecar** click, after the plugin accepted the launch, with an argument list and never a shell; the launch code never goes on a command line. The plugin starts no process. Any change here changes the disclosures in `README.md`, `plugin/README.md`, `SECURITY.md` and the catalog template.
- The extension's LibreOffice ids (`com.atlasomnia.sidecar.libreoffice`, `vnd.atlasomnia.sidecar:`, `SidecarDeck`, `SidecarChatPanel`) and the pairing folder `user/hermes-bridge/` are public contract: renaming them orphans installs or pairings.
- Every supported change to a document is one named undo step (`Hermes: …`); a change LibreOffice records no undo step for reports `undoable: false`. Requests are sent once and never retried; an unknown outcome is reported as `ExecutionUncertain`, never as success.
- Writes go to the explicit `document_id`, never to whichever window is in front.
- `scripts/build_oxt.py` stores every member uncompressed with fixed metadata, so the `.oxt` hash is the same on every platform. A change to `extension/`, `LICENSE` or `NOTICE` needs the new SHA-256 in `README.md` and `plugin/README.md`; `tests/test_packaging.py` checks it.

## Following and evidence

- `docs/web-protocol.md` revision 5 is authoritative. Follow LibreOffice is off by default, remembered only by the page as `sidecar.follow`; absent/invalid/unavailable storage means off. Capability negotiation is independent of that preference. Windows/tab mode must not negotiate following, poll window geometry or move a browser window.
- Hand movement or resize undocks until Reattach/new session; Reattach restores 400 points wide. Never focus/activate a window during following. No linked minimise or following across Spaces; fullscreen is an ordinary large window. Keep second-display/Retina claims explicitly untested until hardware evidence exists.
- Retain the completed-read-back guard as well as the 400 ms settle interval; timer expiry alone is not a baseline. No geometry in logs/storage/URLs or from model/document text. Keep main-thread callbacks bounded and free of I/O; workers never read UNO.
- Do not turn historical, scratch, fake-upstream or unit results into installed/current-candidate claims. Manual screen-reader work is not required for 0.5.1; automated accessibility remains required. Real gateway/model runs require owner authorization, not an agent’s inferred permission.
- During an Unreleased 0.5.1 draft, preserve both archive-hash placeholders. `build_oxt.py --check` may accept that exact draft seam but says NOT a release hash match. Replace both only after extension/page freeze; then date the release, verify deterministic hashes and rerun candidate gates. Public catalog pins remain pending publication.

## Before every commit

Run the full suite and keep it green:

```sh
env -u TMPDIR python3 -m unittest discover -s tests
```

Releases, tags and catalog pull requests are made by the project maintainers.
