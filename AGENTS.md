# AGENTS.md

Notes for coding agents (and people) working in this repository.

## Scope

Sidecar for LibreOffice. A LibreOffice extension (`extension/`, Python-UNO running inside LibreOffice) that edits the live open Calc, Writer and Impress documents, a Hermes plugin (`plugin/`) exposing `calc_*`, `writer_*`, `impress_*` and `libreoffice_undo` tools over an authenticated loopback bridge, and their tests and scripts.

## License

Apache-2.0 (see `LICENSE` and `NOTICE`). Keep SPDX headers on new source files; a test enforces them. Do not add or copy third-party content without a license check.

## Rules that protect the user's machine

- Never touch the everyday LibreOffice profile (`~/Library/Application Support/LibreOffice` on macOS). Live checks use the isolated profile under `$HERMES_LO_DEV_ROOT` (see `scripts/dev_paths.py`).
- Never use `unopkg add`: its helper ignores `-env:UserInstallation` and can write to a profile you did not choose. Install only by opening the `.oxt` in the isolated instance (`scripts/install_isolated.sh`).
- Wrap live runs in `scripts/guarded_run.py`, which fails if the everyday profile changed.
- Do not touch the system clipboard outside the single Tools-menu token check.
- Kill only LibreOffice processes you started, by exact PID.
- Never commit credentials or pairing tokens. Nothing in this repository reads Hermes's `.env`, a credential store or another tool's login; the model-driven scenario harness is internal and is not part of this repository.

## Design rules

- The plugin and the extension are stdlib-only. Do not add dependencies.
- The bridge listens on loopback only and requires the pairing token on every request; its log carries route labels, never request paths.
- The extension's LibreOffice ids (`com.atlasomnia.sidecar.libreoffice`, `vnd.atlasomnia.sidecar:`, `SidecarDeck`, `SidecarChatPanel`) and the pairing folder `user/hermes-bridge/` are public contract: renaming them orphans installs or pairings.
- Every supported change to a document is one named undo step (`Hermes: …`); a change LibreOffice records no undo step for reports `undoable: false`. Requests are sent once and never retried; an unknown outcome is reported as `ExecutionUncertain`, never as success.
- Writes go to the explicit `document_id`, never to whichever window is in front.
- `scripts/build_oxt.py` stores every member uncompressed with fixed metadata, so the `.oxt` hash is the same on every platform. A change to `extension/`, `LICENSE` or `NOTICE` needs the new SHA-256 in `README.md` and `plugin/README.md`; `tests/test_packaging.py` checks it.

## Before every commit

Run the full suite and keep it green:

```sh
python3 -m unittest discover -s tests
```

Releases, tags and catalog pull requests are made by the project maintainers.
