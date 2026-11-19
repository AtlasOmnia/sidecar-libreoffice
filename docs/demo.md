<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Atlas Omnia -->
# Sidecar for LibreOffice — 80-second demo

A shot list for recording a short demo. It adds up to 80 seconds; the hard cap for the final cut is 85 seconds.

## Setup

Do this before recording; none of it is on camera.

- The extension is installed through the Extension Manager and Hermes is paired (README, *Install*).
- Make fresh copies of the three demo documents outside the repository (the demo edits them):

  ```sh
  python3 scripts/build_demo_fixtures.py --out-dir ~/hermes-demo
  open -a LibreOffice ~/hermes-demo/demo-sales.ods ~/hermes-demo/demo-notes.odt ~/hermes-demo/demo-deck.odp
  ```

  The script writes the same files that are committed in `docs/fixtures/`, byte for byte. They contain only this made-up data:
  - `demo-sales.ods` → Sheet1 A1:B5 reads Region/Units: North 120, South 95, East 143, West 88. D1:E4 is empty.
  - `demo-notes.odt` → one paragraph: "Hermes Writer original text."
  - `demo-deck.odp` → one slide: "Hermes Impress Proof / Original slide content".
- To keep your regular LibreOffice profile out of the recording, start LibreOffice with an isolated profile instead: `scripts/launch_isolated.sh ~/hermes-demo/demo-sales.ods ~/hermes-demo/demo-notes.odt ~/hermes-demo/demo-deck.odp` (it keeps the profile under `$HERMES_LO_DEV_ROOT`, default `~/.cache/hermes-libreoffice-dev`, and is written for macOS).
- Hermes runs in a terminal beside LibreOffice, restricted to the LibreOffice toolset: `hermes chat -t sidecar-libreoffice`.
- Layout: LibreOffice on the left two-thirds of the screen, the terminal on the right third. Use a large font. Do not use a real document or account.

## Script and shot list

| Time | Shot | On screen | Voice-over |
|---|---|---|---|
| 0:00–0:05 | 1. Title | Title card: "Sidecar for LibreOffice", subtitle "Calc · Writer · Impress, live, local". | "Hermes, working inside the documents you already have open in LibreOffice." |
| 0:05–0:10 | 2. Calc, before | `demo-sales.ods`: Region/Units in A1:B5, D1:E4 empty. | "A small sales sheet." |
| 0:10–0:30 | 3. Calc | Type the Calc prompt (sped up, labelled). Tool calls scroll by: `calc_get_overview`, `calc_get_range`, `calc_set_range`, `calc_format_range`, `calc_get_range`. D1:E4 fills in; the header turns white on dark blue. Click E4: the formula bar shows `=SUM(B2:B5)`, the cell 446. | "It reads the data, writes a summary table with a real SUM formula, styles the header, and reads it back. 446 is a live formula, not a pasted number." |
| 0:30–0:46 | 4. Writer, from the sheet | Switch to `demo-notes.odt` and type the Writer prompt. It contains no numbers. Tool calls: `calc_get_range` (D1:E4 of the sales sheet), `writer_get_text`, `writer_insert_text` with `as_paragraph`, `writer_get_text`. A new paragraph appears under the original one. | "Now Writer. I don't give it the numbers: it reads them from the sheet it just built and adds them to my notes as a new paragraph." |
| 0:46–0:56 | 5. Undo from the chat | Type "Undo that." `libreoffice_undo` runs; the paragraph disappears and no blank line is left. Optionally open **Edit** to show the Redo entry for the Hermes step (check the wording in rehearsal), then close it. | "Every change is one named undo step. Hermes can undo its own changes, and only its own." |
| 0:56–1:05 | 6. Impress | Switch to `demo-deck.odp` and type the Impress prompt. `impress_list_slides` runs; Hermes answers: 1 slide, with its text. | "In Impress it reads the deck. This one is read-only." |
| 1:05–1:15 | 7. Safety | Static card: "Loopback only · pairing token · writes go to the document you name · one undo step per change · nothing sent beyond your model provider". | "The bridge listens only on this machine, needs a pairing token, and writes only to the document you name." |
| 1:15–1:20 | 8. Close | The three documents side by side, then the repo name. | "Sidecar for LibreOffice. Live, local, undoable." |

## Prompts

Calc (the prompt from the end-to-end live run):

> Use only the LibreOffice tools. In the open LibreOffice Calc document "demo-sales.ods" (sheet "Sheet1"): 1. Read the range A1:B5 and tell me what it contains. 2. Write a small summary table into D1:E4: header row "Metric","Value"; then "Regions" with the count of regions; "Top region" with the region that has the most units; "Total units" with the formula =SUM(B2:B5). 3. Format the header D1:E1: bold, white font (#FFFFFF), dark blue fill (#1F4E79). 4. Re-read D1:E4 and report the computed total.

Writer (new; it names no figures, so the sentence has to come from the sheet):

> Use only the LibreOffice tools. Read the summary table in D1:E4 of Sheet1 in "demo-sales.ods". Then add one sentence to the end of the open Writer document "demo-notes.odt", as its own paragraph (writer_insert_text with as_paragraph true), that states the number of regions, the total units and the top region, using only the values you just read. Read the Writer document back and confirm the new paragraph.

Undo:

> Undo that.

Impress:

> Use only the LibreOffice tools. List the slides in the open LibreOffice Impress presentation "demo-deck.odp" and tell me how many slides it has and what slide 1 says. Do not change anything.

## Recording notes

- Calc, Writer and the undo run in one `hermes chat` session, so "Undo that." refers to the Writer paragraph. The Writer prompt reads the sheet itself, so the sentence still comes from the sheet if Writer is a separate take.
- Before the final take, check the grounding once: change West to 90 in a scratch copy of `demo-sales.ods`, run the Calc and Writer prompts again, and confirm the paragraph says 448 units. Then run `python3 scripts/build_demo_fixtures.py --out-dir ~/hermes-demo` again (with the documents closed) to get the original copies back.
- Model latency varies. Trim the wait between each prompt and its first tool call, and label sped-up typing. Do not cut away the tool calls themselves.
- `libreoffice_undo` undoes only `Hermes: …` steps. If you type in the document between the insert and "Undo that.", it refuses and points you to Edit → Undo; that is correct behaviour, not a failure to hide.
- The Impress prompt above only reads. Slide-editing tools exist (create, edit text, add a text box, find and replace, delete) and were checked live at the bridge. In the 0.4.0 model-driven scenarios a model created slides, replaced text, deleted a slide, changed a layout and added a shape, each passing on readback (`docs/workflow-acceptance.md`, I1-I4); this demo has not rehearsed a slide edit, so do not present one as demonstrated in it.
- Before the final take, close every unrelated window and notification. No credentials, tokens or personal documents should appear on screen.
