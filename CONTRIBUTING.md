# Contributing

Thanks for helping with Sidecar for LibreOffice. Bug reports, test results on other platforms and pull requests are all welcome.

## Reporting a bug

Open an issue with your operating system, LibreOffice version (**Help → About**), Hermes Agent version (`hermes --version`), the tool that failed and the error category it returned (see the README's *Error categories*). Never paste your pairing token. For security problems, follow `SECURITY.md` instead of opening a public issue.

Results from Linux, from Windows (the 0.4.0 Extension Manager install and sidebar panel were driven there only by synthetic window messages on a locked console, and the `hermes` commands, double-click install and **Restart Now** have not been run there), from browsers other than the ones the README's *Tested versions* lists for the Sidecar window (and on managed computers), and from LibreOffice versions other than the ones in that table are especially useful. For a Sidecar window problem, include the browser and its version, and whether it opened as an app window or a tab.

## Development setup

You need `python3` (3.9 or later; CI runs 3.11 and 3.13 on macOS, Ubuntu and Windows) and, for live checks, LibreOffice. There is nothing to install: the plugin, the extension and the tests use the standard library only. The Sidecar page's two JavaScript libraries are vendored in `plugin/sidebar/vendor/`; nothing is downloaded at build or run time.

```sh
python3 -m unittest discover -s tests                                   # unit tests
python3 scripts/build_oxt.py --check                                    # builds dist/sidecar-libreoffice.oxt and checks the README's SHA-256 (Windows: py scripts\build_oxt.py --check)
python3 scripts/sidebar_sums.py --check                                 # the page's files match plugin/sidebar/SHA256SUMS
HERMES_HOME="$(mktemp -d)" hermes plugins validate plugin --install-deps
```

A change to a served file in `plugin/sidebar/` needs `python3 scripts/sidebar_sums.py --write`. Updating a vendored library is its own commit that changes the file, `vendor/VENDOR.json`, `SHA256SUMS` and `NOTICE`, and passes `python3 scripts/verify_vendor.py` (it downloads from the npm registry, so it runs in its own CI job, not in the unit suite).

## Live checks

Live checks run against an isolated LibreOffice profile, never your everyday one. Paths come from `scripts/dev_paths.py`; set `HERMES_LO_DEV_ROOT` to move them (default `~/.cache/hermes-libreoffice-dev`).

```sh
scripts/install_isolated.sh dist/sidecar-libreoffice.oxt   # opens the extension in the isolated instance; accept, then restart it
scripts/launch_isolated.sh                                       # starts the isolated instance
python3 scripts/guarded_run.py -- python3 scripts/live_client.py \
  --token-file ~/.cache/hermes-libreoffice-dev/profile/user/hermes-bridge/token health
```

`scripts/guarded_run.py` snapshots your everyday LibreOffice profile before and after the command and fails if anything in it changed. The launch scripts are written for macOS; set `SOFFICE` to your `soffice` binary on other systems.

## Pull requests

- Keep the plugin and the extension stdlib-only.
- Keep `README.md`, `plugin/README.md`, `SECURITY.md` and `catalog/atlas-sidecar-libreoffice.yaml` truthful about what the software does on the user's machine: a new route, file, process or network call is a disclosure change in the same pull request.
- Add or update tests with every behaviour change, and run the full suite before you push.
- Keep the SPDX header on new source files (a test checks it).
- A change to `extension/`, `LICENSE` or `NOTICE` changes the `.oxt` SHA-256: put the new hash in `README.md` and `plugin/README.md` (a test checks both).
- Say in the pull request what you ran live and on which LibreOffice version, and what you did not run. Do not describe something as tested live when only unit tests cover it.
- Update `CHANGELOG.md` under *Unreleased*.

By contributing you agree that your contribution is licensed under the Apache License 2.0.
