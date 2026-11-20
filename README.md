# Sidecar for LibreOffice

by Atlas Omnia

Hermes reads and edits the Calc, Writer and Impress documents you already have open in LibreOffice, and, once you turn the chat on, you can chat with it in the **Sidecar window**: click **Open Sidecar** in LibreOffice's sidebar and the Sidecar chat you know from Sidecar for Excel opens in its own window beside LibreOffice (the sidebar chat is off by default). There is no upload step and no copy-paste; what Hermes reads from a document goes, with your chat, to the model Hermes is configured with. Supported edits are one named undo step (`Hermes: …`), and Hermes can undo its own changes from the chat; `calc_activate_sheet` and `calc_recalculate` are not undoable, and some Impress undo restores text without its mixed formatting (see *Undo in Impress*).

![Calc with the Sidecar sidebar: Hermes built a summary table and a column chart from the sales data, and its reply is in the panel](docs/assets/screenshots/calc-summary-chart.png)

<!-- D-FINAL: replace these three screenshots with 0.5.0 captures of the Sidecar window, or keep them and keep the sentence below. -->
These screenshots show the native sidebar panel of 0.4.0. In 0.5.0 the conversation moves to the Sidecar window, which looks like Sidecar for Excel; the panel keeps a status line and the **Open Sidecar** button.

*Calc: one sidebar message built the D1:E4 summary (formulas over the sales data, dark-blue header) and the chart, then read the values back. Real chat with the model configured in Hermes, on the made-up demo data in `docs/fixtures/`.*

| Writer | Impress (Dark theme) |
|---|---|
| ![Writer with the Sidecar sidebar: Hermes added a sentence written from the spreadsheet's summary](docs/assets/screenshots/writer-from-sheet.png) | ![Impress with the Sidecar sidebar in its Dark theme: Hermes added slide 2, Units by region, from the spreadsheet](docs/assets/screenshots/impress-new-slide-dark.png) |
| A sentence written from the sheet's summary, added as its own paragraph. | A new slide built from the sheet, highest region first. |

Two parts, both in this repository:

- a **LibreOffice extension** (`extension/`, built into `dist/sidecar-libreoffice.oxt`, identifier `com.atlasomnia.sidecar.libreoffice`) that runs Python-UNO inside LibreOffice, marshals document work onto LibreOffice's main thread, listens only on `127.0.0.1:8660` behind a pairing token, and opens the Sidecar window in a browser when you click **Open Sidecar**;
- a **Hermes plugin** in stdlib-only Python (`plugin/`, id `atlas-sidecar-libreoffice`, toolset `sidecar-libreoffice`) that talks to it over loopback HTTP, can run the chat endpoint on `127.0.0.1:8661` (off by default), which also serves the Sidecar window's web page, and starts no process.

Platforms: macOS and Windows. The Sidecar window opens as an app window on macOS when Chrome, Edge, Brave or Chromium is installed, otherwise as a normal tab in your default browser; on Windows 0.5.0 always uses a normal tab in your default browser (see *Which browser*). On Linux only the unit tests run (in CI), and the Sidecar window is not supported there. Read the *Risk disclosures* below before you install.

## Install

You need:

- [LibreOffice](https://www.libreoffice.org/download/) with Python-UNO. The standard macOS and Windows installers include it.
- Hermes Agent 0.23.1 or later.
- `git` and Python 3.9 or later (stdlib only) to build the extension and the demo documents. macOS: `git` and `python3` come with the Xcode Command Line Tools (`xcode-select --install` if `git --version` asks for them). Windows: [Git for Windows](https://git-scm.com/download/win) and Python from [python.org](https://www.python.org/downloads/windows/), run as `py`.

**1. Install the Hermes plugin** from the Hermes plugin catalog:

```sh
hermes plugins install atlas-sidecar-libreoffice --enable
```

Restart Hermes so the plugin loads: `hermes gateway restart` if you run a gateway, or quit and reopen Hermes Desktop.

**Upgrading from 0.4.0:** remove nothing. Update the plugin first (`hermes plugins update atlas-sidecar-libreoffice`, or install it again as above) and restart the gateway, then build and add the 0.5.0 extension as in step 2. Its identifier is the same as 0.4.0's, so LibreOffice replaces 0.4.0 instead of installing a second copy. Your pairing token, `chat_port`, `API_SERVER_KEY` and the other settings stay as they are: if the sidebar chat was off it stays off, and if it was on it stays on and the Sidecar panel now shows **Open Sidecar** instead of the chat. A 0.5.0 plugin keeps answering a 0.4.0 extension's panel until 0.6.0, so the order matters only the other way round: a 0.5.0 extension with a 0.4.0 plugin shows *Something other than your Hermes answered on port 8661. Sidecar did not open.* until the plugin is updated. <!-- D-FINAL: the Extension Manager replacing 0.4.0 in place, and `hermes plugins update` after the catalog SHA bump, are designed but not yet run. -->

**Upgrading from a plugin build before 0.4.0** (id `libreoffice-live`): remove it (`hermes plugins remove libreoffice-live`), install this one and restart the gateway once. The old plugin's chat listener keeps `127.0.0.1:8661` until the gateway process restarts; if you turn this release's sidebar chat on before that, it logs `libreoffice chat endpoint not started on 127.0.0.1:8661: OSError` and the panel cannot reach it. From 0.4.0 on, disabling or reloading the plugin closes its listener.

From a clone instead (macOS; the clone from step 2): run `hermes plugins install "file://$PWD#plugin" --enable` in the clone's folder. Hermes clones the committed tree, installs its `plugin/` subdirectory as `atlas-sidecar-libreoffice` and warns that a local `file://` source is not from the catalog; that is expected. This route was not tried on Windows; use the catalog install there.

**2. Build the LibreOffice extension from the release tag and install it.** Hermes does not install the extension.

macOS (Terminal):

```sh
cd ~
git clone --depth 1 --branch v0.5.0 https://github.com/AtlasOmnia/sidecar-libreoffice.git
cd sidecar-libreoffice
python3 scripts/build_oxt.py              # writes dist/sidecar-libreoffice.oxt
shasum -a 256 dist/sidecar-libreoffice.oxt
```

Windows (PowerShell):

```powershell
cd $env:USERPROFILE
git clone --depth 1 --branch v0.5.0 https://github.com/AtlasOmnia/sidecar-libreoffice.git
cd sidecar-libreoffice
py scripts\build_oxt.py                   # writes dist\sidecar-libreoffice.oxt
Get-FileHash dist\sidecar-libreoffice.oxt -Algorithm SHA256
```

Expected SHA-256 of `sidecar-libreoffice.oxt` for v0.5.0: `D-FINAL-OXT-SHA256`. The archive holds only `extension/` files plus `LICENSE` and `NOTICE`, stored uncompressed with fixed timestamps, permissions and order, so the hash does not depend on the zlib your Python uses or on the operating system: the build is reproducible with any CPython 3.9 or later on macOS and Windows (the method was checked for 0.4.0 with 3.9 to 3.14 on macOS and with python.org 3.11 to 3.14 on Windows 11). <!-- D-FINAL: re-run the cross-platform build check for v0.5.0 and say so here. --> The repository checks out with LF line endings on every platform (`.gitattributes`), also with `core.autocrlf=true`, so a Windows clone builds the same bytes. A build from a commit other than the `v0.5.0` tag can have another hash.

In LibreOffice choose **Tools → Extensions…** (**Extension Manager…** in older versions) **→ Add…**, pick `dist/sidecar-libreoffice.oxt` (Windows: `%USERPROFILE%\sidecar-libreoffice\dist\sidecar-libreoffice.oxt`) and accept any prompt. Opening the `.oxt` with LibreOffice (double-click it) does the same. Check that **Sidecar for LibreOffice** 0.5.0 by Atlas Omnia is listed and enabled, then choose **Close**. LibreOffice asks to restart: choose **Restart Now**, or **Restart Later** and then quit and reopen LibreOffice. Do not use `unopkg` (see *Platform notes*).

**Upgrading from an extension older than 0.4.0** (0.3.x or a 0.4.0 pre-release, listed as **Sidecar for LibreOffice** or **Hermes for LibreOffice**): since 0.4.0 the extension has a new identifier, so LibreOffice treats it as a different extension and installs it alongside the old one, and both would try to listen on port 8660. Remove the old one in **Tools → Extensions…**, before adding the new one or right after: select it, **Remove**, then **Close**, **Restart Later**, and quit and reopen LibreOffice. A 0.4.0 pre-release has the same name and version, so the dialog can show two identical **Sidecar for LibreOffice 0.4.0** rows; tell them apart by the description of the selected row, which for this release ends with the Apache License line. After the upgrade LibreOffice may first show its Properties deck in the sidebar; click the green **S** tab once to open Sidecar. The pairing token stays in the profile's `user/hermes-bridge/` folder, which keeps its name for that reason, so an existing pairing keeps working.

**3. Pair Hermes with LibreOffice.** In LibreOffice choose **Tools → Copy Sidecar pairing token**. The token goes to the clipboard and is never shown. Give it to Hermes in one of these ways:

- **Hermes Desktop:** **Settings → Plugins → atlas-sidecar-libreoffice**, paste into the **bridge_token** field, **Save**.
- **macOS:** `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN "$(pbpaste)"`
- **Windows (PowerShell):** `hermes config set LIBREOFFICE_LIVE_BRIDGE_TOKEN (Get-Clipboard)`

The shell reads the clipboard, so the token is not typed, shown or kept in your shell history. All three store it as `LIBREOFFICE_LIVE_BRIDGE_TOKEN` in the profile's `.env` (`$HERMES_HOME/.env`), not in `config.yaml`. Start a new chat afterwards, and restart the gateway if you run one.

**4. First read.** Make a copy of the demo spreadsheet (Sheet1 A1:B5 holds Region/Units for four made-up regions), open it in LibreOffice, and start a chat restricted to the LibreOffice tools.

macOS:

```sh
python3 scripts/build_demo_fixtures.py --out-dir ~/hermes-demo
open -a LibreOffice ~/hermes-demo/demo-sales.ods
hermes chat -t sidecar-libreoffice
```

Windows (PowerShell):

```powershell
py scripts\build_demo_fixtures.py --out-dir "$env:USERPROFILE\hermes-demo"
Invoke-Item "$env:USERPROFILE\hermes-demo\demo-sales.ods"   # or File → Open in LibreOffice
hermes chat -t sidecar-libreoffice
```

The script writes the same `demo-sales.ods`, `demo-notes.odt` and `demo-deck.odp` that are committed in `docs/fixtures/`; working on copies keeps the repository clean. `docs/demo.md` uses all three.

> Which LibreOffice documents are open? Read A1:B5 of Sheet1 in the spreadsheet and tell me what it contains.

Hermes calls `calc_get_overview`, then `calc_get_range`.

**5. First edit and undo.**

> Write "Hello from Hermes" into Sheet1!D1 of that spreadsheet.

The cell changes, and **Edit → Undo** in LibreOffice now lists `Hermes: set D1`. Then:

> Undo that.

Hermes calls `libreoffice_undo` and D1 is empty again. Ask it to read D1 back if you want the proof in the chat.

If a tool returns `Auth`, the token is missing or stale: repeat step 3 (and restart the gateway if you run one). If it returns `Unavailable`, LibreOffice is not running or the extension is not enabled: repeat step 2.

**6. Optional: the Sidecar window (sidebar chat).** It is off by default. To use it, follow *Turn on the sidebar chat* below; it needs the Hermes API server, a profile whose model runtime is Hermes-managed, and a browser (see *Which browser*).

## Tested versions

<!-- D-FINAL: replace this paragraph with what the 0.5.0 gates ran (browsers and versions, macOS and Windows, a real gateway and model), and nothing more. -->
**0.5.0, the Sidecar window: not yet verified as a release.** The launch method was tried in design spikes with prototype code, not with this release: on macOS 27.2.1 (Apple silicon), Chrome 158 opened the page in an app window from the launch file with the requested bounds and no address bar, and Safari 27 opened it as a tab; in both, the one-time code never appeared in the process list and the page removed it from its address. On Windows 11, Edge 158 opened the same launch file. Edge and Brave on macOS, Firefox, Windows' default-browser tab, a real gateway and model, 2× and multi-display placement and managed browsers have not been run with the Sidecar window.

The table and paragraphs below are the **0.4.0** results; "this release" in them means 0.4.0. The 55 document tools, the document bridge on port 8660 and the tool fence are unchanged in 0.5.0.

| Component | Version | What was verified |
|---|---|---|
| LibreOffice | 26.8.2.2 (macOS, Apple silicon) | Live, in an isolated LibreOffice profile (0.3.0 live checks): every Calc tool of that release with undo back to baseline, Writer read and insert, Impress listing, document binding with another window in front, bridge undo 40/40 with LibreOffice hidden and 30/30 visible, the Tools menu item, Extension Manager install. The 0.4.0 release candidate, still under the extension's earlier identifier, was installed through the Extension Manager dialog in a disposable profile as an upgrade over 0.3.0, rolled back to 0.3.0 and upgraded again; after each cold restart the panels, the saved theme and the bridge were checked. The `docs/fixtures/` demo documents load with the expected cells, paragraph and slide title (headless conversion). |
| macOS | 27.2 (Apple silicon) | Host for all macOS live runs (Windows 11: see below). |
| Hermes Agent | 0.23.1 | The version this release was tested with. `hermes plugins validate plugin --install-deps` passes with no warnings. `hermes plugins install "file://…#plugin" --enable` installs and enables the plugin and prints the next steps. `hermes config set` writes the pairing token to `.env`, where the plugin's secret lookup finds it. All in throwaway `HERMES_HOME`s. `hermes config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661` writes the integer setting the plugin reads. The chat endpoint's load, unload and reload, and unload with Runs API polls that fail, ran through the real 0.23.1 plugin manager against a fake Runs API, without a model. One real sidebar chat ran through a 0.23.1 gateway with a model on macOS (below), with an earlier build of the plugin whose chat endpoint was on by default and had no run reconciliation. |
| Hermes Agent | 0.23.3 | Source of the `v0.23.3` tag only, run with the 0.23.1 installation's Python environment, no gateway and no model: plugin validation (15 checks) passes with no warnings, and the plugin-manager probe (default off, turning it on, failing Runs API polls, unload) gives the same results as on 0.23.1. The 0.23.3 launcher and a 0.23.3 gateway were not run. |
| Hermes Agent | 0.23.0 | The sidebar chat ran live with a throwaway gateway and a model (see *Sidebar chat*), before the 0.4.0 security changes. |

0.4.0 on macOS (LibreOffice 26.8.2.2): the 0.4.0 `.oxt` (SHA-256 `9322ca9cbb2acc17bd874c9dbef1a11f8a35b3d57a27d38cd97e027be579ab8d`) was installed into a new disposable profile through LibreOffice's deployment API. Its installed files matched the build, the sidebar deck, panel and commands were registered under their new ids and the Sidecar deck opened, and the plugin-handler matrix (15 checks), CSV import with one-step undo and a clean quit passed. In an everyday macOS install, an earlier build of the same extension files (compressed, so with another archive hash) was opened over the 0.4.0 pre-release as described under *Install*: two identical rows, the old one removed, **Close**, **Restart Later**, quit and reopen, then one click on the **S** tab. With that extension and a Hermes Agent 0.23.1 gateway running an earlier build of this release's plugin (before the run-ownership changes, with the chat endpoint on by default), one real chat from the Calc sidebar with the profile's configured model called `calc_get_overview`, `calc_set_range`, `calc_get_range`, `libreoffice_undo` and `calc_get_range` (the cell was empty again), and a request to run `ls ~` in the terminal was refused by the panel's tool fence with the plugin's message. That was one run, without a forced compaction.

Windows 11 with LibreOffice 26.8.3.1: an earlier build of this release's extension files (compressed with the zlib of python.org Python 3.14, so with another archive hash) was built with `py scripts\build_oxt.py` and installed through the Extension Manager dialog (**Tools → Extensions… → Add…**, **Close**, **Restart Later**, quit, reopen) into a disposable profile. The Sidecar panel showed in Calc, Writer and Impress in Dark and Light, in its not-connected state (**Bridge unavailable**), and the theme persisted across a restart. **Tools → Copy Sidecar pairing token** put the token on the clipboard, where `(Get-Clipboard)` read it as one 43-character line. The 18-check plugin-handler matrix, CSV import with one-step undo, wrong-token refusal, a cold restart and clean quits passed, and that tree's unit suite passed (336 tests, 16 POSIX-only skips). The GUI was driven by synthetic window messages on a locked console, not by a person with a mouse and keyboard. The 0.4.0 `.oxt` builds there with that SHA-256 (python.org Python 3.11 to 3.14), and a command-line `unopkg add` into a disposable profile registered it. Before that, the 0.4.0 release candidate under the extension's earlier identifier passed the same matrix (covering 25 of the 55 tools), two cold restarts and a headless start there, and an earlier 0.4.0 build's run checked owner-only ACLs on the bridge files. CI is set up to run the current suite on `windows-latest` as well. Not exercised on Windows: Hermes itself (the plugin has not been installed or loaded by Hermes there, and the sidebar chat has not connected; the panel was seen only in its not-connected state), the clone from GitHub, opening the `.oxt` by double-click, **Restart Now**, the sidebar's **S** tab route and real mouse and keyboard input. Not tested anywhere: Linux, other LibreOffice versions, port 8660 already taken by another program. The **bridge_token** field in Hermes Desktop was checked against the Desktop settings source, not clicked through. `libreoffice_undo` and `as_paragraph` ran live through the plugin's own handlers against LibreOffice 26.8.2.2 (`scripts/live_undo_tool_check.py`): one- and two-step Calc undo restored the cell, and a paragraph appended at the end or start of a Writer document was its own paragraph and one undo left the original paragraphs exactly. That run did not go through a model-driven chat, and the refusal to undo a non-Hermes step is covered by unit tests only. The 20 sheet, comment, find-and-replace, style, selection and slide tools added in 0.3.0 ran live at the bridge (`scripts/live_extended_check.py`): each change was read back, undone and read back again. All of them were later called by a model in the 0.4.0 model-driven scenarios below.

The tools added after 0.3.0 (paged Writer reads and tables, styled paragraph inserts, guarded edit batches, selection formatting and comments, Impress layouts, text formatting and shapes, CSV import, sort and filter, charts, conditional formatting, validation, freeze panes, protection, tables, sheet activation, recalculation, current slide and layouts) were each accepted at the development bridge in an owned disposable profile on LibreOffice 26.8.2.2, with readback and undo, and with the regular LibreOffice profile checked unchanged. The same setup saved documents through LibreOffice as `.xlsx`, `.docx` and `.pptx`, reopened them and compared the readback, and ran Calc with a German (decimal-comma) locale and with AutoCalculate off; `docs/sidecar-parity.md` lists what survives each format and what does not. Separately, 29 model-driven scenarios ran once each (C4 a second time after the fix) against the development bridge, through an internal harness that is not part of this repository, with the frontier model configured in the harness's disposable Hermes profile. Each was judged by document readback (W4, a read-only scenario, on the figure in its reply); together they called all 55 tools. 27 passed as recorded, C1 failed only on an over-strict oracle, and C4 found the CSV-import undo defect, which is fixed; the scenario passes after the fix. The model rarely loaded the bundled guidance skills and worked from the tool schemas. See `docs/workflow-acceptance.md`.

Known limits: shape colours set by `impress_add_shape` are saved to `.pptx` as theme colours; slide reorder is unavailable (no identity-preserving LibreOffice API); protection does not block the bridge's own writes.

The release candidate used for the earlier macOS and Windows runs, the macOS Extension Manager upgrade and rollback test and the model-driven runs differs from this release in the sidebar's "Hermes is not answering" message, which now names the plugin; in source comments; in the plugin and toolset id (then `libreoffice-live`); in the extension's identifier, file name, command URLs and sidebar ids, now Atlas Omnia names; in the security changes listed in `CHANGELOG.md`; in the `LICENSE` and `NOTICE` files now inside the `.oxt`; and in the archive, which is now stored uncompressed.

## Tools

| Tool | What it does | Changes the document? |
|---|---|---|
| `calc_get_overview` | Lists open documents of all three applications (with their `document_id`) and Calc sheet metadata | No |
| `calc_get_range` | Reads values, formulas, number formats and a fingerprint (5,000 cells max) | No |
| `calc_set_range` | Writes values or formulas; an optional `if_match` fingerprint refuses a stale write | Yes, one undo step |
| `calc_import_csv` | Imports pasted CSV text into empty cells as data, never as formulas; `typing` `text` or `numbers` (plain dot-decimals only) | Yes, one undo step |
| `calc_format_range` | Bold, italic, colours, number format, borders, alignment, font | Yes, one undo step |
| `calc_clear_range`, `calc_create_sheet` | Clear contents; add a sheet | Yes, one undo step |
| `calc_rename_sheet`, `calc_copy_sheet`, `calc_delete_sheet` | Rename, copy or delete a sheet; the last sheet cannot be deleted | Yes, one undo step |
| `calc_activate_sheet` | Brings a sheet to the front of the document's window | View only (`undoable: false`) |
| `calc_find_replace` | Find and replace in one sheet, optionally whole cells only | Yes, one undo step |
| `calc_autofill` | Fill down or right from the first rows or columns of a range, as the fill handle does | Yes, one undo step |
| `calc_range_stats` | Count, sum, average, min, max and blank count for a range (10,000 cells max) | No |
| `calc_sort_range`, `calc_filter_range` | Sort whole rows by up to three keys; one standard filter per sheet | Yes, one undo step |
| `calc_create_chart`, `calc_list_charts` | Column, bar, line, area, pie or scatter chart from a range; list a sheet's charts | Create: one undo step |
| `calc_add_conditional_format`, `calc_set_validation` | Highlight with a built-in cell style; list or number validation | Yes, one undo step |
| `calc_freeze_panes`, `calc_protect_sheet` | Freeze rows/columns; password-free protection (does not block Hermes' own writes) | Yes, one undo step |
| `calc_get_rules` | Reads conditional formats, validation, protection and freeze state | No |
| `calc_insert_table` | Named table (a LibreOffice database range) with filter buttons and an optional style | Yes, one undo step |
| `calc_recalculate` | Recalculates formulas; reports whether AutoCalculate is on | Results only (`undoable: false`) |
| `calc_add_comment`, `calc_clear_comments` | Add a comment to a cell; delete the comments in a range | Yes, one undo step |
| `calc_get_comments` | Lists a sheet's comments with their cells | No |
| `calc_execute_plan` | A validated WorkbookPlan V1 subset, sent once, with verification rules | Yes, one undo step |
| `writer_get_text`, `writer_get_structure` | Reads the body page by page (500 blocks / 50,000 characters per page) with a continuation token; structure lists each paragraph's style and outline level | No |
| `writer_get_table` | Reads one table's cells | No |
| `writer_insert_paragraphs` | Up to 50 styled paragraphs at the start, the end or after a paragraph | Yes, one undo step |
| `writer_insert_text` | Inserts text at the start or end; `as_paragraph: true` makes it its own paragraph | Yes, one undo step |
| `writer_apply_edits` | Up to 50 exact find/replace edits, all or nothing | Yes, one undo step |
| `writer_set_paragraph_style` | Applies an existing paragraph style, by displayed or internal name, to one paragraph; `expected_text` refuses a stale target | Yes, one undo step |
| `writer_find_replace` | Find and replace throughout the document | Yes, one undo step |
| `writer_get_selection` | Reads the text selected in the document's window and its formatting | No |
| `writer_replace_selection`, `writer_format_selection` | Replace or format the single selection; `expected_text` must match what was read | Yes, one undo step |
| `writer_add_comment`, `writer_get_comments` | Comment on the selected text; list comments with author and anchor | Add: one undo step |
| `impress_list_slides`, `impress_get_slide` | Lists slide titles and text; reads one slide's layout, size and shapes | No |
| `impress_get_current_slide`, `impress_list_layouts` | The slide the user is on; the supported layouts, masters and each slide's layout | No |
| `impress_create_slide`, `impress_delete_slide` | Adds a slide with a layout and optional title and body; deletes one (not the last) | Yes, one undo step |
| `impress_set_shape_text`, `impress_find_replace` | Replaces a shape's text; find and replace in one slide or all | Yes, one undo step |
| `impress_add_text_box`, `impress_add_shape` | Adds a text box, rectangle, rounded rectangle, ellipse or line, in centimetres | Yes, one undo step |
| `impress_set_slide_layout`, `impress_format_text` | Changes a slide's layout; formats all text of one shape | Yes, one undo step |
| `libreoffice_undo` | Undoes the last `steps` (1–10, default 1) **Hermes** changes in one document, newest first | Yes |

There is no tool to open, save, export or convert files, to make pivot tables, or to reorder slides.

Every document tool needs a `document_id` from `calc_get_overview`. It is derived from LibreOffice's `RuntimeUID` and is valid for the current LibreOffice session. Writes go to that exact document and never fall back to whichever window is in front.

`libreoffice_undo` only undoes steps titled `Hermes: …`. If your own edit is on top of the undo stack, it refuses, undoes nothing, and tells you to use **Edit → Undo**. It sends one request per step, never retries, and stops at the first step that is refused or uncertain; the result lists the steps it undid. The plugin has no separate approval prompt for writes, and `libreoffice_undo` is treated like every other write: it needs the explicit `document_id` and the pairing token, and it reports an unknown outcome as `ExecutionUncertain`.

Every changing tool returns `undoable`: whether LibreOffice recorded the step. Treat a change with `"undoable": false` as one `libreoffice_undo` cannot take back. `calc_activate_sheet` and `calc_recalculate` always report `"undoable": false`.

### Undo in Impress

Impress records no undo step for a slide, a text box or shape text changed through its document API: in live testing, undoing a new slide emptied its text and left the slide in place. For `impress_create_slide`, `impress_add_text_box`, `impress_set_shape_text` and `impress_find_replace` the bridge therefore adds its own undo step, which removes what was added or puts the previous text back, and rebuilds it on redo. The previous text comes back as plain text: if a shape mixed several character formats, undo restores the words but not the mixed formatting. `impress_delete_slide` uses LibreOffice's own undo step.

### Why undo brings the document to the front

LibreOffice applies parts of an undo through the focused window rather than only the document the step belongs to. In live testing with another Calc document in front, undoing a sheet creation left the sheet in place, and undoing a plan that wrote a formula added an empty sheet to the front document. The bridge therefore undoes only while the bound document is LibreOffice's focused view, which it tracks from LibreOffice's `OnFocus`/`OnUnfocus` events. When the document is in the background, the bridge activates it first and confirms the focus event. If it cannot confirm focus, it refuses with `Validation` (`state: not_started`) and undoes nothing. A changed undo stack is refused as `Stale`.

## Sidebar chat

The sidebar chat is off by default. Once you turn it on (*Turn on the sidebar chat* below), you chat with Hermes in the **Sidecar window**: the chat window of Sidecar for Excel, opened beside LibreOffice and tied to one document.

**Opening it.** In Calc, Writer or Impress, click the green **S** tab in the sidebar (**View → Sidebar** shows the sidebar). The **Sidecar** panel, titled **Sidecar for Calc**, **Sidecar for Writer** or **Sidecar for Impress** with an Atlas Omnia byline, shows a status line and an **Open Sidecar** button. Click **Open Sidecar**. The panel says *Opening Sidecar…*, then *Sidecar is open.* A window opens only when you click; it never opens by itself. <!-- D-FINAL: name the Tools menu command that does the same, as lane C1 ships it. -->

**What the window shows.** The Sidecar chat from Sidecar for Excel, built from the same stylesheets: the Sidecar header with **Settings** and the connection status, the conversation, and the composer with **Send**, **Stop** and **New chat**. Enter sends; Shift+Enter adds a line. Replies are formatted: headings, lists, tables, code and quotes. A link in a reply is never opened when you click it: the window shows the full address with a **Copy link** button. Images are never loaded. **Settings** offers **Palette** (Teal, Blue, Gold, Red) and **Appearance** (**Auto**, **Dark**, **Light**); **Auto** follows LibreOffice's light or dark appearance. The browser keeps that choice for the Sidecar page.

**One window per document.** The window works on the document whose sidebar you clicked, and Hermes is told which document that is. Clicking **Open Sidecar** again for the same document brings its window to the front (macOS app window), or the panel says *Sidecar is already open. Switch to it from the Dock or taskbar.* Each open document can have its own window.

**While Hermes works.** The status line shows *Hermes is working…*. In 0.5.0 a reply appears whole when it is finished; there is no word-by-word text and no live list of tool calls. **Stop** asks Hermes to stop; the window then says *Stopped. Check the document for changes made before stopping.* If Hermes waits for an approval, the window says *Hermes asked for an approval that Sidecar cannot give. Press Stop and ask in Hermes itself.*: Sidecar cannot answer approvals.

**Closing, reloading, quitting.**

- Closing or reloading the Sidecar window ends its connection and stops a reply in progress; what Hermes already changed stays in the document, one undo step per change. A reloaded window shows *Click Open Sidecar in LibreOffice to reconnect.* A reconnected window does not show the earlier messages.
- Closing the document closes its Sidecar window (*LibreOffice closed this document.*). Quitting LibreOffice closes the Sidecar windows, and on macOS the extension asks the browser it started to quit. A window that is hidden or minimized can notice this late, up to about a minute.
- If no request from a window reaches the plugin for 90 seconds while a reply is running (the browser was closed without notice, for example), the plugin stops that reply.
- If LibreOffice crashes, the Sidecar window stays open: chat may still answer, but the document tools fail until LibreOffice runs again.

**While the sidebar chat is off (the default),** the panel shows *Chat is off in Hermes.* and *See README ▸ Sidebar chat.*, and no browser is started. Nothing is broken: the chat endpoint is simply not running. The document tools work from any Hermes chat (`hermes chat -t sidecar-libreoffice`).

Other panel messages:

| The panel says | Meaning |
|---|---|
| *Hermes is not paired with LibreOffice yet. See README ▸ Pairing.* | Hermes has no pairing token yet; see *Pairing*. |
| *Something other than your Hermes answered on port 8661. Sidecar did not open.* | The program on the chat port could not prove that it knows your pairing token: another program, or a plugin older than 0.5.0. Nothing was sent to it and no window opened. |
| *The Sidecar window did not connect. Your browser may be managed by your organization; see README ▸ Which browser.* | The page did not connect within 20 seconds. Sidecar does not try another browser. |
| *Another program used this launch. Click Open Sidecar again.* | The one-time launch link was used twice; the connection made with it was closed and its reply stopped. |

### Which browser

The Sidecar window is a web page served by the plugin on `127.0.0.1:8661` and shown in a browser.

- **macOS.** The extension looks for Google Chrome, Microsoft Edge, Brave Browser and Chromium, in that order, in `/Applications` and `~/Applications`, and uses the first one it finds. It starts it as a separate instance in app mode: a window with a title bar and no address bar, tabs or toolbar, placed to the right of the LibreOffice window when 400 points fit there and otherwise over its right side. That instance uses its own empty profile in `~/Library/Caches/Sidecar for LibreOffice/browser/` and starts with `--no-first-run`, `--no-default-browser-check`, `--disable-extensions`, `--disable-sync`, `--disable-background-networking`, `--disable-component-update`, `--disable-default-apps` and `--use-mock-keychain`. Its Dock icon and menu bar are the browser's, and the icon can stay in the Dock until LibreOffice quits. Without one of those browsers, **Open Sidecar** opens the page as a normal tab in your default browser (Safari, for example), with no window placement.
- **Windows (0.5.0).** **Open Sidecar** opens the page as a normal tab in your default browser, the program Windows opens `.html` files with. An app window like the macOS one is not part of 0.5.0. If `.html` files open in a program that is not a browser, the window does not connect.
- **Linux.** The Sidecar window is not supported; the panel says so.
- **Managed computers.** Browser policies set by an organization also apply to the dedicated profile. A forced sign-in, a blocked `127.0.0.1` or `file://` address, a proxy without a loopback exception or administrator-installed extensions can keep the window from connecting; the panel then shows *The Sidecar window did not connect…*. Use `hermes chat -t sidecar-libreoffice` there.
- **Memory.** Each Sidecar window is a browser window. In a Windows 11 measurement of Edge running as a dedicated app-window instance (the setup planned for a later release), one or two windows used about 190–280 MB of private memory (about 390 MB at a cold start); on macOS the dedicated browser instance uses a few hundred MB. In a normal tab the cost is one more tab of your browser. These figures come from design measurements, not from the release build. <!-- D-FINAL: replace with the G6/G7 measurement if one is taken. -->

### Turn on the sidebar chat

The sidebar chat is off by default: the plugin starts no chat endpoint until you set `chat_port`. The Sidecar panel and the Sidecar window talk to that endpoint, which forwards your messages to the Hermes API server inside the Hermes gateway. See the Hermes guide, [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).

**Runtime requirement, before anything else.** External-agent runtimes are unsupported for restricted sidebar chat. Keep `chat_port: 0` when using one. The dedicated-profile recipe restricts Hermes tools only; it does not disable an external runtime's built-in tools. Use a Hermes-managed model/tool loop for that recipe, explicitly configure it in the dedicated profile, disable MCP and other toolsets there, and verify the resulting configuration before enabling sidebar forwarding. A panel chat in a profile with an external-agent runtime would not be fenced. Check the profile's model runtime: for example `hermes config get model.openai_runtime` must not be `codex_app_server`, and the model provider must not be an external agent CLI.

1. **Enable the API server and give it a key**, in the profile where the plugin is enabled. These commands generate a random key, so it is never shown:
   - macOS: `hermes config set API_SERVER_ENABLED true`, then `hermes config set API_SERVER_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"`
   - Windows (PowerShell): `hermes config set API_SERVER_ENABLED true`, then `hermes config set API_SERVER_KEY (py -c "import secrets; print(secrets.token_urlsafe(32))")`

   An existing `API_SERVER_KEY` works too. Hermes Desktop also shows it as this plugin's **api_server_key** field. Keep `API_SERVER_HOST` at its default `127.0.0.1` so the API server stays on loopback.
2. **Turn the chat endpoint on**, in the same profile (8661 is the port the Sidecar panel calls):

   ```sh
   hermes config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661
   ```

   The setting lives under `plugins.entries.atlas-sidecar-libreoffice.settings` in `config.yaml` (Hermes Desktop: the plugin's **chat_port** field). `0` turns it off again.
3. **Restart the gateway** (`hermes gateway restart`). The plugin then listens on `127.0.0.1:8661`, serves the Sidecar window from there, and forwards each message to the API server on `127.0.0.1:8642` (`api_server_port`) through its Runs API. It calls the API server of the profile whose gateway loaded it (`/p/<profile>/` for a named profile) with that profile's `API_SERVER_KEY`. Whether Hermes Desktop's own backend starts the endpoint has not been checked.
4. **Isolation mode.** The chat endpoint starts only when plugins run in the gateway process, `plugins.isolation: in_process`, the Hermes default. Under `plugins.isolation: host` the document tools still work, but the panel shows *Chat is off in Hermes.* In a gateway that serves several profiles, the first profile to bind a given chat port gets it; turn the chat on in one profile only.

The extension and the chat endpoint prove to each other that they know the pairing token from step 3 of *Install*, without sending it (see *Pairing*); no Hermes credential is stored in LibreOffice or in the browser.

A chat in the Sidecar window is a chat with your full Hermes agent (its model, memory and persona), but a `pre_tool_call` hook limits its tools to this plugin's 55 tools and the read-only `skill_view`, `skills_list`, `tool_search` and `tool_describe`. Every other tool (terminal, files, web, delegation and so on) is refused in that chat. The hook cannot reach tools that never pass through Hermes, which is why external-agent runtimes are unsupported here. `SECURITY.md` lists the remaining gaps.

When Hermes's answer about a run goes missing (a status poll times out, fails or cannot be read, or the API key stops working), the endpoint asks Hermes to stop that run with the key it was started with and keeps the chat busy until it sees the run settle. If it cannot confirm that, the window says it is not known whether Hermes carried out the request: check the document before you send it again, and do not resend edits blindly. That chat accepts no new message until a recheck, made each time you send to it again, sees the run settle; the endpoint then frees it. **New chat** starts another chat while fewer than 4 runs are tracked (unresolved runs count against that limit). **Stop** goes the same way: the plugin asks Hermes to stop the run and reports *Stopped…* only once Hermes confirms it; otherwise the outcome is reported as not known. A plugin reload forgets the run, but that does not show the run stopped: check the document. Turning the chat off or reloading the plugin while a chat runs is covered under *Risk disclosures*.

**Dedicated profile (restricts Hermes tools only).** To have the API server offer the panel only the document tools, run the panel against a dedicated profile with its own model setup. This does not make an external-agent runtime safe: the profile must use a Hermes-managed model/tool loop that you configure in it yourself.

```sh
hermes profile create sidecar-lo                       # not --clone: see below
hermes -p sidecar-lo model                              # a Hermes-managed provider and model; no external-agent runtime
hermes -p sidecar-lo plugins install atlas-sidecar-libreoffice --enable
hermes -p sidecar-lo config set platform_toolsets.api_server '["sidecar-libreoffice", "no_mcp"]'
hermes -p sidecar-lo config get model.openai_runtime    # must not be codex_app_server
hermes -p sidecar-lo config show                        # check runtime, toolsets and MCP servers first
hermes -p sidecar-lo config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661   # last
```

Do not create it with `--clone`: a clone copies the source profile's model and runtime, persona, memory and secrets, so it does not satisfy the runtime requirement and does not separate sensitive context. Give the profile its own `API_SERVER_ENABLED`, `API_SERVER_KEY` and the pairing token, disable MCP servers and other toolsets there, and check `hermes -p sidecar-lo config show` before enabling sidebar forwarding (the last command). Leave `chat_port` at `0` in every other profile, so only the dedicated profile owns `127.0.0.1:8661`. `platform_toolsets.api_server` (the API server's tool list) and `no_mcp` (no MCP servers) are Hermes 0.23.1 configuration; this setup was not run end to end.

<!-- D-FINAL: add what ran live with the 0.5.0 Sidecar window (gate G9) or keep the sentence that it has not. -->
The 0.5.0 Sidecar window has not yet run with a real gateway and model. The rest of this paragraph is about the native panel of 0.4.0 and earlier. Run live on LibreOffice 26.8.2.2 with Hermes Agent 0.23.0 and a throwaway gateway, before the 0.4.0 security changes: from the panel, Hermes read a sheet, added a formula row and made it bold, undid it when asked in the next message, and was refused when asked to run a shell command. That run entered messages through a development hook. Typing in the panel by hand was later checked in Calc (read, edit, undo). The current three-app UI was exercised with a labeled local test responder: native typing, Enter/Shift+Enter, busy drafts, theme changes and error/recovery; no Writer or Impress edit and undo with a live model through the panel is claimed. The 0.4.0 chat endpoint (Runs API forwarding, unload and reload, and unload while Runs API status polls fail) was checked through the real Hermes 0.23.1 plugin manager against a fake Runs API, without a model. One real chat, with an earlier build of this release's plugin, then ran from the Calc panel through a Hermes 0.23.1 gateway with a model on macOS: read, edit, `libreoffice_undo` with readback, and a refused terminal command (*Tested versions*). Stopping an in-flight panel run on unload, recovery from a lost Runs API answer, and a panel chat across a compaction have not run with a real gateway and model.

### Changing ports

The defaults need no setting. If you change a port, change both sides:

- `bridge_port` (default 8660): the extension reads its port from the file `port` in its pairing folder: one number from 1024 to 65535, in a file only you can write. The folder is `user/hermes-bridge/` in the LibreOffice profile, `~/Library/Application Support/LibreOffice/4/user/hermes-bridge/` on macOS and `%APPDATA%\LibreOffice\4\user\hermes-bridge\` on Windows. Restart LibreOffice afterwards.
- `chat_port` (default `0`, off; turn it on with 8661, see *Turn on the sidebar chat*): the Sidecar panel calls 8661 unless the LibreOffice process has the environment variable `HERMES_LO_CHAT_PORT`, so for another port LibreOffice must be started with it set. The Sidecar window is served from the same port.

## Guidance skills

The plugin bundles two read-only skills, available while it is enabled:

- `atlas-sidecar-libreoffice:libreoffice-guidance`: how to work in each application with these tools. It has a workflow for Calc, Writer and Impress (including LibreOffice's formula syntax, which differs from Excel's), a design playbook, and the rules that hold everywhere: read first, read back, check `undoable`, never resend an uncertain change.
- `atlas-sidecar-libreoffice:calc-plans`: the plan shapes `calc_execute_plan` accepts and how to use them.

They are adapted from the guidance skills of [Sidecar](https://github.com/AtlasOmnia/sidecar); `plugin/skills/libreoffice-guidance/references/provenance.md` says what came from where. They add no tools.

## Error categories

Every failed tool call returns `{"ok": false, "error": {"category", "message"}}`. Requests are sent once and never retried.

| Category | Meaning | Document changed? |
|---|---|---|
| `Validation` | Bad arguments, address, shape, format key or plan; a tool used on the wrong document type; an undo whose top step is not a Hermes change, or whose document could not be confirmed as the focused window | No |
| `NotFound` | The `document_id`, sheet or undo step does not exist | No |
| `Stale` | `if_match` no longer matches the range, or the undo stack changed | No |
| `Auth` | Missing or wrong pairing token, or a forbidden `Origin`/`Host` | No |
| `Unavailable` | LibreOffice is not running, the bridge is down, or a read failed | No |
| `Timeout` | LibreOffice was busy; the call was cancelled before it started (`state: not_started`) or a read did not finish | No |
| `Protocol` / `Capacity` | Malformed or oversize request/response on a read | No |
| `ExecutionUncertain` | A change was sent or had begun and its outcome is unknown | Maybe — re-read before retrying; Hermes steps appear in Edit → Undo |

A `libreoffice_undo` that fails after undoing some steps also carries `"undone": [...]`, the steps it did undo.

## Pairing

To pair, follow step 3 of *Install*: **Tools → Copy Sidecar pairing token** in LibreOffice, then store the token in Hermes as `LIBREOFFICE_LIVE_BRIDGE_TOKEN`.

On first run the extension creates `<UserInstallation>/user/hermes-bridge/token` with mode `0600` inside a `0700` directory; broader permissions fail closed. **Tools → Copy Sidecar pairing token** copies it and shows a short confirmation without the secret. The plugin reads the token only through Hermes's `agent.secret_scope.get_secret`; it never reads the LibreOffice profile or another client's credentials. The folder keeps its `hermes-bridge` name from earlier releases so that existing pairings survive the extension's new identifier.

**The token never goes to the chat endpoint from a 0.5.0 extension.** When you click **Open Sidecar**, the extension opens a short-lived channel to `127.0.0.1:8661`: it sends a random challenge, and the endpoint must answer with an HMAC-SHA256 of it keyed by the pairing token. Every later call on that channel, and every answer, carries such a MAC, with a counter. A program on that port that does not know the token learns nothing about it and cannot make Sidecar open a window. The plugin still sends the token to the extension's own listener on 8660 for the document tools, as in 0.4.0, and a 0.4.0 extension still sends it to the chat endpoint's legacy routes (see *Risk disclosures*).

### Platform security (`extension/python/private_files.py`)

- **What is protected:** other *unprivileged* local accounts cannot read the token or replace the token, port or theme files. SYSTEM, Administrators and accounts with backup, restore, take-ownership or debug privileges are trusted and can bypass these checks. This protects the files only; see *Risk disclosures* for the loopback ports.
- **macOS and Linux:** owner, file type, single link and mode bits (no group/other bits for the token, no group/other write for the port file). Extended ACLs are not inspected.
- **Windows:** owner (you or Administrators), no reparse point, single link, and a DACL whose effective allow entries name only you, SYSTEM, Administrators and OWNER RIGHTS. The token is created with `CreateFileW(CREATE_NEW)` and an explicit protected DACL, so nothing inheritable on the folder applies to it. The open handle is checked before the first byte is written.
- **Reads** check the opened file, not just the path. Only the last path component is checked for links: a read that reaches a genuinely private file through a linked ancestor folder is allowed.
<!-- D-FINAL: keep or drop the theme-file bullets below, depending on whether the 0.5.0 extension still writes sidebar-theme.json (lane C1). -->
- **Theme preference on Windows:** written only below the LibreOffice user directory, which must be yours and not writable by others. No folder from the drive root down may be a junction or symlink, and every folder stays open without delete sharing until the write and cleanup finish, so none can be renamed or swapped meanwhile. macOS and Linux use a `dir_fd` walk that never follows a symlink.
- **Not covered:** `bridge.log` is created with the folder's inherited permissions and is outside this guarantee. It never contains the token or request paths (only a fixed route label and the status), but it can contain error text.

## Platform notes

- On notarized macOS LibreOffice builds the bundled Python and `uno` executables have a parent launch constraint, so standalone Python-UNO and remote-UNO clients are unsupported. This project loads Python inside `soffice`. Do not strip quarantine or re-sign LibreOffice.
- Do not install with `unopkg add`: on macOS its helper was seen to ignore `-env:UserInstallation` and register the extension in the everyday profile instead of the one chosen. On Windows 11 a command-line `unopkg add` with `-env:UserInstallation` registered it in the chosen disposable profile, but only the Extension Manager route is documented here. Use the Extension Manager.
- The extension's listener stays up while LibreOffice runs, on port 8660 by default (see *Changing ports*).

## Risk disclosures

- **A LibreOffice extension you install yourself.** It runs Python inside LibreOffice, in the LibreOffice profile you install it into. Hermes does not install or update it.
- **Loopback listeners.** By default only the extension's listener runs: `127.0.0.1:8660` inside LibreOffice, from LibreOffice's first window until LibreOffice quits. It accepts a request only with the pairing token, refuses any `Origin` header, requires `Host` to be the loopback address and port, and bounds how many connections it serves and how long a request may take to arrive. The plugin's chat endpoint is off by default; once you set `chat_port` (8661) it listens on `127.0.0.1:8661` inside the Hermes gateway process, from when the gateway loads the plugin until the plugin is disabled or reloaded or Hermes exits. Its routes are separated by caller, and each accepts only its own credential: `/launcher/*` for the LibreOffice extension (a challenge-response keyed by the pairing token, see *Pairing*), `/ui/*` for the Sidecar page's files (no credential; the files are inert), `/api/*` for the Sidecar page (a session token in a request header, the page's exact origin and same-origin fetch metadata), and the legacy `/chat` and `/health` for 0.4.0 extensions (the pairing token, no `Origin`). Every route requires `Host` to be exactly `127.0.0.1:<port>`, no route uses cookies, and connections and read time are bounded as before.
- **The Sidecar web page.** When you set `chat_port`, the plugin's loopback endpoint also serves the Sidecar web page (static files shipped in the plugin under `plugin/sidebar/`, checked against a hash list on every request) and a small API used only by that page. The page loads nothing from the internet: its content security policy allows only its own loopback origin, and the two bundled libraries (marked and DOMPurify, see *License*) ship with the plugin.
- **The extension starts a browser.** When you click **Open Sidecar**, the LibreOffice extension opens the Sidecar window in a browser. On macOS with Chrome, Edge, Brave or Chromium installed, it starts that browser as a separate process in app mode with its own empty profile in `~/Library/Caches/Sidecar for LibreOffice/browser/`, and asks that browser to quit when LibreOffice quits; to remove the profile, delete `~/Library/Caches/Sidecar for LibreOffice`. Otherwise (other Macs, and Windows in 0.5.0) it asks the system to open the page in your default browser as a normal tab: on macOS by running `/usr/bin/open`, on Windows through the file association for `.html` files. It does this only on that click, only while the sidebar chat is on, and only after the plugin accepted the launch. It never reads your own browser profile, cookies or keychain. The extension runs no other program and no shell. The plugin starts no process.
- **One-time launch code and session.** The page authenticates with a one-time code handed over at launch and a session token it keeps in memory only. The code is 32 random bytes, valid for 30 seconds and for one use; it reaches the browser through a file only you can read (`launch-<random>.html` in the `launch` folder of `Sidecar for LibreOffice` in your user cache folder: `~/Library/Caches` on macOS, `%LOCALAPPDATA%` on Windows), which is deleted within 30 seconds, so the code never appears on a process command line. If a code is presented a second time while it is still on record, the session made with it is closed and its reply stopped. The page uses no cookies. Closing or reloading the window ends the session and stops a reply in progress. At most 8 Sidecar sessions are open at once, and a session ends after 12 hours without use.
- **In a normal browser tab** (Windows in 0.5.0, and Macs without Chrome, Edge, Brave or Chromium) your browser extensions can read the page (any extension allowed to read web pages), including your messages and Hermes's replies, and can act in it while it is open. The browser's history keeps the one-time launch link: it is spent once the window has connected and it expires after 30 seconds, so it cannot be used again. Use a browser profile whose extensions you trust.
- **The dedicated browser profile** (macOS app window) runs without extensions and without background updates of its own components. Its history can also keep the spent launch link. On a managed computer, administrator-installed extensions or policies may still apply, and the window may fail to connect; whether administrator-forced extensions load despite `--disable-extensions` has not been checked. The Sidecar page never navigates away from itself, but the browser's own menus can still open other pages in that instance, which then run without the browser's component updates.
- **Replies.** Replies are shown as formatted text. Links are never opened for you: you can copy them. Images are not loaded. A reply can still contain misleading text, for example when a document steered the model; check a link before you open it.
- **Memory.** Each Sidecar window is a browser window: about 190–280 MB of private memory for a dedicated Edge instance with one or two windows in a Windows 11 design measurement, and a few hundred MB for the dedicated browser on macOS; in a normal tab, one more tab of your browser. See *Which browser*.
- **Plain HTTP with bearer secrets.** Both listeners are plain HTTP on loopback and authenticate the client only. The plugin sends the pairing token to whatever listens on port 8660, and the chat endpoint sends `API_SERVER_KEY` to whatever listens on the API server's port. A 0.5.0 extension never sends the pairing token to the chat endpoint (see *Pairing*); a 0.4.0 extension still sends it to whatever listens on 8661 (when its panel opens and when you send a message, also while the sidebar chat is off), through the legacy routes the plugin keeps for it until 0.6.0. A local process that takes one of those ports while the real listener is down could receive that secret. If the gateway stops while a Sidecar window is open and another local program then takes port 8661, the window's later requests, including messages you send, go to that program, and reloading the window would show that program's page. Use this on a computer whose other accounts and programs you trust.
- **Stored secrets.** One pairing token in the LibreOffice profile at the path and modes described above, and one copy in Hermes's secret store (`LIBREOFFICE_LIVE_BRIDGE_TOKEN` in the profile's `.env`). The plugin reads that and `API_SERVER_KEY` only through Hermes's secret lookup, keeps them in memory, never logs them, and never sends the API key to LibreOffice or the browser. The browser holds no Sidecar credential on disk: the session token lives only in the page's memory, and the page stores only your palette and appearance choice in the browser's local storage.
- **What a Sidecar chat can do.** It is your full Hermes agent with its tools limited, by a `pre_tool_call` hook, to the document tools and read-only skill and tool lookup. Anything that can read the pairing token file as your user can chat with your Hermes model (using your model credits) and read or edit your open documents through it. External-agent runtimes (such as `codex_app_server`) are unsupported for restricted sidebar chat: the hook does not reach their built-in tools, so keep `chat_port: 0` there; the dedicated-profile recipe above restricts Hermes tools only. The Sidecar window cannot answer approvals, steer a run or reach any other Hermes API. Other known gaps are in `SECURITY.md`.
- **Disabling or reloading the plugin while a sidebar chat runs.** The endpoint first asks the runs whose IDs it knows to stop, each with its own key, and waits for them for at most 10 seconds (not at all if Hermes unloads the plugin on its event loop); a run whose creation was never answered has no ID and cannot be asked. When the plugin is disabled or reloaded, Hermes removes its tool restriction (the `pre_tool_call` hook) as soon as the unload callback returns, which can be before the 10-second budget has run out. A sidebar chat that was not confirmed stopped by then keeps running without the restriction, with your full Hermes tools. This is an accepted, disclosed limitation of 0.4.0, unchanged in 0.5.0: the Sidecar window does not change it. It applies only after you turn the sidebar chat on, and fixing it needs a Hermes-side per-run tool restriction. The window then says *The LibreOffice plugin was turned off in Hermes.* A gateway shutdown or restart is not this case: Hermes 0.23.1 does not unload plugins when the gateway stops; it drains its API-server runs, interrupts those still going, and they end with the gateway process. If a sidebar chat may still be running, wait for its answer or restart the gateway before you disable or reload plugins (`hermes plugins enable` and `disable` make a running gateway reload every plugin).
- **Document access.** Tools read and modify the open documents the agent names by `document_id`, and can undo their own changes there. There is no per-document or per-edit approval prompt. `calc_get_overview` lists the titles and file locations of every open LibreOffice document.
- **Undo.** Supported edits are one named undo step. `calc_activate_sheet` and `calc_recalculate` are not undoable (`undoable: false`), and some Impress undo restores text without its mixed formatting. Protection does not block the bridge's own writes.
- **Network.** There is no manual upload step, but the document content Hermes reads and your chat go through Hermes to the model it is configured with; local transport is not a promise of local-only inference. The extension and the plugin make no third-party network calls themselves, and the Sidecar page loads nothing from the internet. LibreOffice can still reach the network when it evaluates document content (external links or web functions in formulas the agent writes or reads), and nothing here prevents that. A browser started for the Sidecar window may contact its vendor's services on its own (sign-in or policy checks on managed computers, for example); the extension turns off what its flags allow.
- **No telemetry, no self-updater** in the extension, the plugin or the Sidecar page. The repository's development scripts (`scripts/`) are separate and do run commands; they are not installed.

## Development

```sh
python3 -m unittest discover -s tests        # stdlib only (Windows: py -m unittest discover -s tests); python3 -m pytest tests also works
python3 scripts/build_oxt.py --check         # builds the .oxt, prints its SHA-256 and compares it with the one under Install
HERMES_HOME="$(mktemp -d)" hermes plugins validate plugin --install-deps
python3 scripts/sidebar_sums.py --check      # the Sidecar page's files match plugin/sidebar/SHA256SUMS
python3 scripts/verify_vendor.py            # network: re-derives the vendored libraries from the npm registry (CI job, not the unit suite)
```

A test fails when the build no longer matches the SHA-256 under *Install*: a change to `extension/`, `LICENSE` or `NOTICE` needs the new hash in this README and in `plugin/README.md`.

Validation runs with `HERMES_HOME` pointing at a throwaway directory so it does not touch your Hermes setup. `python3 scripts/build_demo_fixtures.py` rebuilds `docs/fixtures/` byte for byte; a test fails if the committed files drift from the script. Repository live checks never use your everyday LibreOffice profile: `scripts/launch_isolated.sh` starts LibreOffice with an isolated profile, `scripts/install_isolated.sh dist/sidecar-libreoffice.oxt` opens the extension in that instance's Extension Manager, `scripts/guarded_run.py -- <command>` wraps a live check in a before/after sentinel of the real profile, and `scripts/verify_isolated_install.py` hash-compares the installed extension with the build. They keep everything under `$HERMES_LO_DEV_ROOT` (default `~/.cache/hermes-libreoffice-dev`; see `scripts/dev_paths.py`) and are written for macOS: set `SOFFICE` to your `soffice` binary elsewhere. The model-driven scenario harness is internal and not part of this repository; `docs/workflow-acceptance.md` describes the scenarios and results. See `CONTRIBUTING.md` for the workflow and `SECURITY.md` for how to report a vulnerability.

## License

Licensed under the Apache License 2.0. See `LICENSE` and `NOTICE`; the plugin directory and the built `.oxt` carry copies.

The Sidecar page bundles two third-party libraries, unminified and with their licences, in `plugin/sidebar/vendor/`: [marked](https://github.com/markedjs/marked) 15.0.12 (MIT) and [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.12 (Apache-2.0 or MPL-2.0; used here under Apache-2.0). `plugin/sidebar/vendor/VENDOR.json` records where each file came from on the npm registry and its hashes. The page's three Omnia stylesheets are copies of the ones in [Sidecar](https://github.com/AtlasOmnia/sidecar), pinned in `plugin/sidebar/UPSTREAM.json`.

Sidecar is developed by Atlas Omnia. It is not affiliated with or endorsed by The Document Foundation, Microsoft or Nous Research. LibreOffice is a trademark of The Document Foundation; Microsoft, Excel, Word and PowerPoint are trademarks of the Microsoft group of companies.
