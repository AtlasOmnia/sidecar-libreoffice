# Workflow acceptance

How Calc, Writer and Impress workflows are accepted. A scenario passes only when an
**independent bridge readback** proves the outcome; what the model says about its own work is
recorded and never used as evidence that a change happened. W4, a read-only scenario, is the one
judged on the reply itself: it must report a figure that sits only in a table.

## How the runs were made

The scenarios run through an internal harness that is not part of this repository. For each
scenario it builds the synthetic fixtures (`python3 scripts/build_demo_fixtures.py --scenarios
--out-dir <dir>`, never into `docs/fixtures`), opens copies of the documents the scenario names
(target, witness and any source document) in an owned, disposable LibreOffice profile with the
witness document in front, and checks the everyday LibreOffice profile before and after
(`scripts/guarded_run.py`). A scenario fails if the witness changes.

- **Scripted self-test (no model):** the scenario's fixed bridge calls replace the model. This
  proves the readback, witness, undo and verdict machinery on real documents. It is **not** model
  acceptance.
- **Model-driven:** one bounded `hermes chat` in a disposable Hermes home with this plugin, restricted
  to the `sidecar-libreoffice` toolset (plus `skills`, so the model could load the bundled guidance
  with `skill_view`), using whatever model that home's profile is configured with; the runs recorded
  below used a frontier model. Turns and run time are capped, the sidebar chat endpoint is off
  (`chat_port: 0`), and after each run every file under the disposable home and work directory is
  scanned for copies of the run's credentials; a copy makes the run fail.

Each run records the baseline, after and post-undo readbacks, the witness readbacks, a tool trace
summary (including whether `libreoffice-guidance` or `calc-plans` was loaded through `skill_view`),
the model turns, tool outcomes and categories, and the verdict.

## Verdict rules

| Situation | Verdict |
|---|---|
| Expectation met on readback, witness unchanged, every change on the bound `document_id`, undo back to the baseline | PASS |
| Model claims success but readback disagrees | FAIL |
| A baseline, after, witness or post-undo readback is missing | not PASS |
| A change targeted another document, or the witness changed | FAIL |
| Chat timed out and no readback is available | UNCERTAIN |
| Chat timed out but readback resolves the outcome | judged on the readback, flagged `timeout_resolved_by_readback` |
| Undo refused, partial, or not back to the baseline | FAIL |

After a run the harness undoes the scenario's Hermes steps (`undo: "trace"` counts successful
changes in the trace) and reads back again.

## Scenarios on the current tools

| ID | App | Job | Oracle |
|---|---|---|---|
| C1 | Calc | Summary sheet with SUMIFS per category, one plan | Totals equal the fixture's; SUMIFS formulas over the Inputs sheet (`Inputs.` or `$Inputs.`); Inputs unchanged; one undo removes Summary |
| C3 | Calc | Read, harness edits the cell, model writes with `if_match` | Cell holds the harness value (write refused as `Stale`) |
| W1 | Writer | Title, three headings and bodies in an empty document | Exact text; Title / Heading 1 / non-heading body styles; undo to empty |
| W2 | Writer | Fix five seeded typos (one repeated) | Exact corrected text, nothing else changed |
| W4 | Writer | Key figure that sits only in a table after paragraph 300 | Reply must give the figure (4,321) and the document must stay unchanged; a reply that only discloses what it could not see is recorded as FAIL; read-only |
| I1 | Impress | Six outline sections to six slides | Titles and bodies exact, in order, existing slides untouched |
| I2 | Impress | Replace a product name across the deck | Every occurrence replaced, nothing else changed |
| I3 | Impress | Delete slide 3 | Remaining slides exact; undo restores |

## Scenarios on the 0.4.0 tools

| ID | App | Job | Oracle |
|---|---|---|---|
| C4 | Calc | `calc_import_csv` with `typing: numbers` into Inputs!E1 | E1:G4 exact: `007`, `=1+1` and `+5` stay text, 3 / 4.5 / -2 numbers; A1:C61 unchanged; undo back to the baseline (number formats included) |
| C5 | Calc | Sort A1:C61 by Amount descending, then filter Category = Travel | Rows in sorted order, kept together; hidden rows (host readback) are exactly the non-Travel rows; undo shows every row again |
| C6 | Calc | Column chart of B1:C9 at E2, titled | One new chart: column, title, range `Inputs.B1:C9`, top-left at E2; values unchanged |
| C7 | Calc | Conditional format B2:B7, formula `B2>C2`, style Bad (`plan.ods`) | One rule: formula `B2>C2`, Bad, anchor B2; values unchanged |
| W5 | Writer | One `writer_insert_paragraphs` (Heading 1 + two Body Text), then one `writer_apply_edits` batch with an ambiguous `Note:` | Paragraphs and styles exact; the batch refused as `Validation` and never applied; undo to empty |
| W6 | Writer | Comment on the user's selection (selection made by `dev_select_text` before the run) | One new comment with the exact text anchored to `moved to May`; text unchanged |
| I4 | Impress | Slide 4 to `title_only`, then a rounded rectangle with text | Layout `title_only`, title and body text kept, one new shape at 2/5 cm, 10×3 cm, `#FFCC00`, `Thank you`; slides 1-3 unchanged |
| S1 | Writer | `writer_apply_edits` with `if_match`; the harness edits the document right after the read | Text holds only the harness edit; the model's edit refused as `Stale` and not retried |

## Scenarios for the tools no model had called

User-style prompts (no tool names) aimed at the 29 tools that no model run had called. Fixtures
`tracker.ods`, `sales.ods` and `ledger.ods` are new; `host:*` reads and setup steps go through a host
channel in the owned LibreOffice, for state the bridge has no tool for.

| ID | App | Job | Oracle |
|---|---|---|---|
| C8 | Calc | Keep the header row visible, A1:C1 bold on light grey, A1:C61 a table Expenses with filter buttons (`budget.ods`) | `calc_get_rules` freeze 0 columns / 1 row; host cell format bold + light grey on A1:C1, A2 untouched; host database ranges exactly Expenses `A1:C61` with AutoFilter; values unchanged |
| C9 | Calc | Status dropdown, whole numbers 0-100 in Percent, protect the sheet without a password (`tracker.ods`) | List validation with exactly Open / In progress / Done (any order), rejecting; whole_number between 0 and 100, rejecting; sheet protected; values unchanged |
| C10 | Calc | Fill the D2 formula down to D13 "like the fill handle", report sum/average/min/max (`sales.ods` Q3) | D2:D13 hold `=Bn*Cn` with the right values; A1:C13 unchanged (the reported figures are not judged) |
| C11 | Calc | AutoCalculate off (host setup), the user corrected B2; bring the results up to date (`sales.ods` Q4) | Baseline is stale (precondition); after: line totals and grand total recomputed, formulas and inputs unchanged; no harness undo (recalculation has no undo step) |
| C12 | Calc | Read the notes, delete the resolved ones in column C, add a note on A5 (`ledger.ods`) | Comments exactly A3, B4 and the new A5 text; values unchanged |
| C13 | Calc | Rename Feb, delete the empty Old, copy Jan as "Jan backup", add Notes, show Notes (`ledger.ods`) | Sheet set exact; the copy equals Jan, February holds Feb's data, Notes empty; the window shows Notes. The active sheet is a view read left out of the undo check |
| C14 | Calc | Whole-cell rename of a vendor, clear the scratch area E1:F3, check for charts first (`ledger.ods`) | Costs A1:F6 exact (`Acme Ltd Services` untouched, E1:F3 empty); comments unchanged |
| C15 | Calc | Add a choice to an existing dropdown (set up through the bridge with a choice the data does not show) and keep every existing choice (`tracker.ods`) | List validation exactly Open / In progress / Done / On hold / Blocked, still rejecting; D2:D7 rules and values unchanged. Added after the first twelve runs to reach `calc_get_rules` |
| W7 | Writer | Bold + yellow highlight on the user's selection (`dev_select_text` setup) | Selection still `moved to May`, bold, yellow highlight; text unchanged |
| W8 | Writer | Say what the comments are, then replace the selected sentence | Paragraph text exact; comments unchanged |
| I5 | Impress | On the slide the user is looking at (`dev_set_current_slide` 3): add a text box, make the title bold dark red | Slide 3: one new text box (exact text, position within 0.3 cm), title bold `#C00000`, title/body text and body format unchanged; other slides unchanged |
| I6 | Impress | The harness makes two Hermes changes (title, then a text box); the user asks to undo the last one | Text box gone, title kept, Hermes undo history (host read) lost exactly its newest step; no harness undo |
| I7 | Impress | List the available layouts, then replace the last slide's body text | Slide 4 body exact, title kept, no shape added; slides 1-3 unchanged |

Setup steps and readbacks marked `host` go through the host channel; the active sheet is read from
`calc_get_overview`; reads of view state the bridge deliberately does not undo (the active sheet) are
left out of the undo-to-baseline comparison only.

Not yet automated: C2 (messy data clean-up — needs typed-entry guidance first), W3 and C3's
user-edit undo refusal (both need a native UI action to create a selection or a non-Hermes undo
step). Later feature slices add their own scenarios.

Format round trips are a separate scripted check, not a scenario above: in an owned
disposable profile, an in-process host script stands in for the user's File → Save As and Open
(LibreOffice's Excel, Word and PowerPoint 2007-365 filters, and ODF as the control), the
development bridge edits each document, the host saves, closes and reopens it, and the bridge
readback before and after is compared field by field. The same harness ran Calc with a de-DE
(decimal-comma) office locale and with AutoCalculate off. Results and the preserved/lossy lists
are in `docs/sidecar-parity.md`.

## Status

The run records are not part of this repository. The eight scenarios on the current tools first ran
in **scripted** mode on LibreOffice 26.8.2.2 (macOS); W4 correctly fails in scripted mode because it
judges a reply. The eight scenarios on the 0.4.0 tools also ran in scripted mode before any model run;
C4 failed there on the CSV-import undo defect described below.

Those 16 scenarios then ran once each, model-driven, against the owned development LibreOffice. 14 passed as recorded: 13 on readback,
and W4 on the figure in its reply with the document unchanged. C1 failed only on an oracle that
demanded `$Inputs.`, which the prompt never asked for; it passes when the same recorded readback is
judged with the corrected oracle. C4 failed because undo left the imported cells formatted as Text, a
product defect. It is fixed in 0.4.0: imported text cells are set to Text explicitly inside the
import's undo step, so one undo restores the formats; a reproduction on the development bridge
confirmed the fix, and a model-driven re-run of C4 after the fix passed. No credential copy was
found in any run.

The thirteen scenarios C8-C15, W7, W8 and I5-I7 then ran once each, model-driven, against the
owned development LibreOffice, after passing in scripted mode. All thirteen passed on readback. Together with the earlier runs, a model has
called every one of the 55 registered tools (29 scenarios in all). No credential copy was found in
any run.

`scripts/check_guidance_tools.py` audits the bundled guidance: every backticked snake_case name
must be a registered tool, a documented argument or enum value, a plan kind or a listed result
field, and every registered tool must be mentioned.
