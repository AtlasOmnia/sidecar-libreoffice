# Feature parity with Sidecar

[Sidecar](https://github.com/AtlasOmnia/sidecar) is the sister project for Microsoft Excel, Word and PowerPoint. This page maps each Sidecar tool to the Sidecar for LibreOffice tool that does the same job, with its status and the evidence for it. It is pinned to Sidecar's `tool_schemas.json` at commit `74a6c41`; the schemas and skills are unchanged at `7a565c9`.

Counts: Sidecar has 55 tools (Excel 28, Word 12, PowerPoint 15). Sidecar for LibreOffice registers 55 (`plugin/tool_schemas.json`). Of Sidecar's 55, 52 have a LibreOffice counterpart: Excel 27/28, Word 11/12, PowerPoint 14/15.

Status values:
- **Supported, verified natively**: the tool exists and its behaviour was read back on LibreOffice 26.8.2.2 (macOS) through the bridge, in an owned disposable profile.
- **Blocked**: attempted; LibreOffice offers no safe way to do it (see the evidence).
- **Deferred**: not attempted yet.
- **Excluded**: left out by design.

Evidence kinds (the raw readbacks are not part of this repository):
- **native check**: the tool was called at the development bridge in an owned disposable profile on LibreOffice 26.8.2.2 (macOS), and its result was read back (and, for changes, undone).
- **format round trip**: the `.xlsx`/`.docx`/`.pptx` round trip described under *Microsoft file formats*; **locale check**: the same harness with a German (decimal-comma) locale and AutoCalculate off.
- **installed package**: the plugin-handler matrix run against an installed 0.4.0 package (the release candidate, or this release's build on macOS).
- **scenario**: a workflow scenario from `docs/workflow-acceptance.md` (scripted or model-driven).
- **0.3.0 live checks**: `scripts/live_extended_check.py` and `scripts/live_undo_tool_check.py` at the bridge (README, *Tested versions*).

## Excel → Calc

| Sidecar tool | LibreOffice tool | Status | Evidence |
|---|---|---|---|
| `get_workbook_overview` | `calc_get_overview` | Supported, verified natively | format round trip (sheets after reopen) |
| `read_range` | `calc_get_range` | Supported, verified natively | format round trip |
| `summarize_range_stats` | `calc_range_stats` | Supported, verified natively | 0.3.0 live checks |
| `write_range` | `calc_set_range` | Supported, verified natively | format round trip, locale check |
| `format_range` | `calc_format_range` | Supported, verified natively. `italic` and English-notation number-format codes converted to the document locale were fixed in 0.4.0 (decimal-comma and en-US checked) | format round trip, native check (de-DE and en-US locales) |
| `conditional_format_range` | `calc_add_conditional_format` | Supported, verified natively (built-in styles; no colour scales or data bars). Formula conditions are anchored at the range's top-left cell; evaluated highlighting checked on two sheets and from the installed package | native check, installed package, format round trip |
| `freeze_panes` | `calc_freeze_panes` | Supported, verified natively (document in one window) | native check, format round trip |
| `set_data_validation` | `calc_set_validation` | Supported, verified natively (list, whole number, decimal; no date or text length) | native check, format round trip |
| `create_sheet` | `calc_create_sheet` | Supported, verified natively | format round trip |
| `delete_sheet` | `calc_delete_sheet` | Supported, verified natively | 0.3.0 live checks |
| `activate_sheet` | `calc_activate_sheet` | Supported, verified natively (view change, `undoable: false`) | native check |
| `insert_table` | `calc_insert_table` | Supported, verified natively (a database range: no structured references or totals row) | native check, format round trip |
| `sort_table` | `calc_sort_range` | Supported, verified natively | native check |
| `filter_table` | `calc_filter_range` | Supported, verified natively (no `empty` operator) | native check, format round trip |
| `create_chart` | `calc_create_chart` (+ `calc_list_charts`) | Supported, verified natively (no styling) | native check, format round trip |
| `find_replace` | `calc_find_replace` | Supported, verified natively | 0.3.0 live checks |
| `clear_range` | `calc_clear_range` | Supported, verified natively | 0.3.0 live checks |
| `autofill_formula` | `calc_autofill` | Supported, verified natively | 0.3.0 live checks |
| `rename_sheet` | `calc_rename_sheet` | Supported, verified natively | 0.3.0 live checks |
| `copy_sheet` | `calc_copy_sheet` | Supported, verified natively | 0.3.0 live checks |
| `protect_sheet` | `calc_protect_sheet` (`protect: true`) | Supported, verified natively (password-free only; does not block the bridge's own writes) | native check, format round trip |
| `unprotect_sheet` | `calc_protect_sheet` (`protect: false`) | Supported, verified natively (never removes a password) | native check |
| `create_pivot_table` | — | Deferred | — |
| `run_calculation` | `calc_recalculate` | Supported, verified natively, including with AutoCalculate off | native check, locale check |
| `add_comment` | `calc_add_comment` (+ `calc_get_comments`) | Supported, verified natively | format round trip |
| `clear_comments` | `calc_clear_comments` | Supported, verified natively | 0.3.0 live checks |
| `import_csv_to_sheet` | `calc_import_csv` | Supported, verified natively (pasted text only, no file path) | native check, locale check |
| `execute_plan` | `calc_execute_plan` | Supported, verified natively (WorkbookPlan V1 subset) | scenario C1 (scripted), locale check |

LibreOffice-only Calc reads: `calc_get_rules` (conditional formats, validation, protection, freeze), `calc_list_charts`, `calc_get_comments`.

## Word → Writer

| Sidecar tool | LibreOffice tool | Status | Evidence |
|---|---|---|---|
| `get_document_overview` | `calc_get_overview` + `writer_get_structure` | Supported, verified natively | native check |
| `read_selection` | `writer_get_selection` | Supported, verified natively (also returns the selection's formatting) | native check, format round trip |
| `read_document_text` | `writer_get_text` (+ `writer_get_table`) | Supported, verified natively (paged; headers, footers, footnotes and frames are not read) | native check |
| `apply_edit_plan` | `writer_apply_edits` | Supported, verified natively | native check, format round trip |
| `insert_text` | `writer_insert_paragraphs`, `writer_insert_text` | Supported, verified natively | native check, format round trip; 0.3.0 live checks |
| `replace_selection` | `writer_replace_selection` | Supported, verified natively | 0.3.0 live checks |
| `find_replace` | `writer_find_replace` | Supported, verified natively | 0.3.0 live checks |
| `format_selection` | `writer_format_selection` | Supported, verified natively | native check, format round trip |
| `get_document_structure` | `writer_get_structure` | Supported, verified natively | native check, format round trip |
| `apply_style` | `writer_set_paragraph_style` | Supported, verified natively | format round trip; 0.3.0 live checks |
| `insert_ooxml` | — | Excluded (no raw OOXML by design) | — |
| `insert_comment` | `writer_add_comment` (+ `writer_get_comments`) | Supported, verified natively | native check, format round trip |

## PowerPoint → Impress

| Sidecar tool | LibreOffice tool | Status | Evidence |
|---|---|---|---|
| `get_presentation_overview` | `calc_get_overview` + `impress_list_slides` | Supported, verified natively | format round trip |
| `read_selected_slides` | `impress_get_current_slide` | Supported, verified natively (single selection; multi-selection not exercised) | native check |
| `read_all_slides` | `impress_list_slides` | Supported, verified natively | format round trip |
| `create_slide` | `impress_create_slide` | Supported, verified natively | scenario I1 (scripted), format round trip |
| `create_text_slide` | `impress_create_slide` with `title` and `body` | Supported, verified natively | format round trip |
| `add_text_box` | `impress_add_text_box` | Supported, verified natively | format round trip, close-after-edit check |
| `update_slide_text` | `impress_set_shape_text` | Supported, verified natively | 0.3.0 live checks, close-after-edit check |
| `add_shape` | `impress_add_shape` | Supported, verified natively (rectangle, rounded rectangle, ellipse, line) | native check, format round trip |
| `read_slide_content` | `impress_get_slide` | Supported, verified natively | native check, format round trip |
| `delete_slide` | `impress_delete_slide` | Supported, verified natively | scenario I3 (scripted) |
| `reorder_slide` | — | **Blocked**: no identity-preserving UNO move on LibreOffice 26.8 | probes of LibreOffice's slide-move and dispatch APIs |
| `find_and_replace_text` | `impress_find_replace` | Supported, verified natively | scenario I2 (scripted), format round trip |
| `list_slide_layouts` | `impress_list_layouts` | Supported, verified natively | native check, format round trip |
| `set_slide_layout` | `impress_set_slide_layout` | Supported, verified natively (refuses changes undo could not restore) | native check, format round trip |
| `format_text` | `impress_format_text` | Supported, verified natively (whole shape; refuses mixed properties) | native check, format round trip |

## Microsoft file formats

Sidecar works inside Microsoft Office on its own files. Here, LibreOffice opens and saves `.xlsx`, `.docx` and `.pptx`; the plugin has no tool to open, save or export files. A scripted check covered the round trip on macOS: the host (in-process `storeAsURL`/`loadComponentFromURL` with LibreOffice's Excel, Word and PowerPoint 2007-365 filters, standing in for the user's File → Save As and Open) converted a fixture and reopened it, the bridge edited it, the host saved, closed and reopened it, and the bridge read everything back. The same run on ODF copies is the control; it changed nothing except the currency code's written form and a validation operator's reported value.

| Format | Preserved | Changed or lost |
|---|---|---|
| `.xlsx` | values, formulas (SUMIFS, SUMPRODUCT, COUNTIF, DATE, EDATE, cross-sheet), cell types, CSV literal text (`=1+1`, `007`, `@literal`), number formats, bold/font/fill colours, comment text, chart type/title/range/legend/position, conditional format rule, list and whole-number validation, frozen row, table (database range with filter buttons) and its filtered rows, sheet protection; formulas still recalculate after reopening | chart name `Chart 1` → `Object 1`; conditional-format style `Good` → `ConditionalStyle_1` (look not compared); currency code rewritten in an equivalent form. Comment text read back empty until the `calc_get_comments` fix in 0.4.0 |
| `.docx` | text, paragraph styles (Title, Heading 1–3, Body Text), bold, italic, underline, strikethrough, font, size, colour, highlight, comments with author and anchor | `Quotations` → `Block Quotation` |
| `.pptx` | slide order and text, layouts, title bold/size/colour, geometry within 0.01 cm, text-box text | shape fill and line colours saved as the theme accent; basic shapes and text boxes reopen as custom shapes (no fill/line in `impress_get_slide`); Liberation Sans → Arial in new shapes; master slides split and renamed; existing body placeholders slightly shorter |

Not exercised: files made by Microsoft Office itself (every `.xlsx`/`.docx`/`.pptx` here was written by LibreOffice), password-protected files, macros, pivot tables, tracked changes, images.

## What Sidecar for LibreOffice has that Sidecar does not

- `libreoffice_undo`: Hermes undoes its own changes from the chat and refuses to undo yours. Sidecar relies on the Office Undo button.
- Every write is one named undo step (`Hermes: …`); view changes and recalculation say `undoable: false`.
- Writes are bound to an explicit `document_id`, so several open documents can be edited without bringing them to the front. Sidecar works on the active document.
- `if_match` fingerprints (Calc ranges, charts, tables, Writer edit batches) and `expected_text` anchors refuse a write when the target changed since it was read.

## Outside the tool list

- Sidecar puts a chat sidebar inside each Office application. Sidecar for LibreOffice opens its chat using the same pinned Omnia styling (not a claim of identical computed layout) in its own window beside LibreOffice, from a launcher in the sidebar: formatted replies, Stop and New chat, but no streaming, no live tool rows and no per-edit approval cards.
- Sidecar bundles Office guidance skills with each plugin. This plugin bundles two adapted from them (`libreoffice-guidance` and `calc-plans`); `scripts/check_guidance_tools.py` fails when the guidance names a tool, argument or plan kind the plugin does not have. The Word OOXML guidance has no counterpart here.
- Sidecar is three plugins, one per application. This is one plugin for all three.
- Platforms: native evidence from macOS (LibreOffice 26.8.2.2) and Windows 11 (LibreOffice 26.8.3.1) on the 0.4.0 release candidate: installed into disposable profiles, plugin-handler matrix, CSV import with undo, cold restarts and clean quits; on Windows also a headless start (an earlier 0.4.0 build's run there also covered owner-only ACLs). The release candidate differs from 0.4.0 in one sidebar message, source comments, the plugin id, the extension's ids, the security changes and the packaging listed in `CHANGELOG.md`. The 0.4.0 `.oxt` passed the plugin-handler matrix, CSV import with undo and a clean quit on macOS and builds byte for byte the same on Windows; a compressed build of the same extension files was installed through the Extension Manager on Windows (synthetic input) and on macOS (README, *Tested versions*). The 0.5.0 window results are in the README’s historical tested-versions section. Optional macOS following is off by default in the 0.5.1 candidate; its fresh evidence remains pending in `docs/release-0.5.1.md`. Windows 0.5.1 keeps the default-browser tab; Linux has no native Sidecar-window support. Linux has had unit tests only.
- Model-driven acceptance: 29 scenarios with a frontier model against the development bridge, together calling all 55 tools; 27 PASS as recorded; C1 passes on re-judging with the corrected oracle; C4 found the CSV-import undo defect, since fixed; the scenario passes after the fix. The bundled guidance skills were rarely loaded by the model (calc-plans once across all runs, libreoffice-guidance never); it relied on tool schemas.

## Open items

1. `.pptx`: shape colours set by `impress_add_shape` are saved as theme colours.
2. Pivot tables (deferred) and slide reorder (blocked).
3. Model-driven runs: one model, one run per scenario, on the development bridge; the model rarely loads the bundled guidance skills.
4. Linux native runs.
