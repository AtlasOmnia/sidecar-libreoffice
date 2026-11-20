<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Atlas Omnia -->
# web_smoke: contract stub and acceptance harness for the 0.5.0 Sidecar window

Developer and CI tools; nothing here ships in the plugin or the `.oxt`. Stdlib Python 3.9+ and an
installed Google Chrome (headless, throwaway profile). No installs, no network.

| File | What it is |
| --- | --- |
| `contract.py` | Byte formats, headers, codes and texts of `docs/web-protocol.md`; `check_vectors()` reproduces section 3.3 |
| `stub_server.py` | Stub of the plugin's window endpoint (every `/launcher/*`, `/ui/*`, `/api/*` route, gates, headers, sessions, R18 channel, turn long-poll), driving turns through the real `ChatBridge` A0 seam and a fake Runs API |
| `fake_runs_api.py` | Fake Hermes Runs API with scripted runs (`[md]`, `[inject]`, `[hang]`, `[slow:S]`, `[fail]`, `[unknown]`, `[unresolved]`, `[approval]`, `[refuse]`, `[long:N]`, `[noreply]`) |
| `corpus.py` | Markdown reply and the R1 injection corpus |
| `httpraw.py` | Raw HTTP with exact headers, and a reference R18 launcher client |
| `targets.py` | What the harness runs against: the stub, the real plugin (lane A factory), or an external endpoint |
| `probes.py` | Black-box probe matrix (G1 rows, connection budget, unload, turn semantics) |
| `cdp.py`, `browser_smoke.py` | Headless Chrome over `--remote-debugging-pipe`; page checks (G3, R1 corpus, G5 keyboard/AX) |
| `fixture/` | A reference page written from section 7 of the contract, used until the real bundle exists |
| `run_gates.py` | Runs everything and writes evidence (`<evidence>/<gate>/…`) |
| `selftest.py` | Unit checks of the harness itself (vectors, corpus, fixture assembly) |

## Run the stub for page work

```sh
python3 scripts/web_smoke/stub_server.py serve --ui-dir plugin/sidebar --state-dir "$TMPDIR/stub"
python3 scripts/web_smoke/stub_server.py open --state-dir "$TMPDIR/stub" --chrome      # one window
python3 scripts/web_smoke/stub_server.py ctl --state-dir "$TMPDIR/stub" ready no_api_key
python3 scripts/web_smoke/stub_server.py ctl --state-dir "$TMPDIR/stub" release all
python3 scripts/web_smoke/stub_server.py ctl --state-dir "$TMPDIR/stub" unload
```

`serve` never prints the pairing token; it is in `<state-dir>/pairing-token` (0600). Ports are
ephemeral; 8642, 8660 and 8661 are refused.

## Run the gates

```sh
python3 scripts/web_smoke/run_gates.py --evidence <folder> [--target stub|plugin] [--ui-dir plugin/sidebar] \
    [--vendor-from <folder with vendor/marked.umd.js and vendor/purify.js>] [--slow]
```

Without `--ui-dir` the page checks use `fixture/` with the vendored libraries copied from
`--vendor-from` (hash-checked). `--target plugin` needs lane A's test factory (see `targets.py`).
