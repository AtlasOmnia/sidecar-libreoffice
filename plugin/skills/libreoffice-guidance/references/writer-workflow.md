<!-- SPDX-License-Identifier: Apache-2.0 -->
# Writer: safe, editable menus, briefs and documents

Read `writer_get_text` and `writer_get_structure` first. Both read the body one
page at a time, in order: up to 500 blocks (a block is a paragraph or a table)
and, for `writer_get_text`, 50,000 characters. A table's cells come back in
`tables`, with the table's block number; its entry in `paragraphs` is empty.
`complete_document: true` means the whole body was returned. Otherwise, when
`next_offset` is set, call again with the `continuation` it returned until
`next_offset` is null. If the document changes between pages the next call is
refused as `Stale`: start again from the first page rather than stitching pages
together, so nothing is skipped or read twice. A single huge paragraph is cut
at 50,000 characters and listed in `clipped_blocks`. A page is also kept to
about 1 MB: a table that does not fit starts the next page, and a table too
big for one page returns its first cells with cells_truncated true; pass its
next_cell_offset as `cell_offset` to `writer_get_table`. `writer_get_table`
reads one table by name or block number, up to 2,000 cells (about 1 MB) at a
time, and says where to continue the same way. Headers, footers,
footnotes and frames are not read; say so when they may matter. Comments are
not part of the text: read them with `writer_get_comments`.
`writer_get_structure` lists each paragraph with its number, paragraph style,
outline level and a 200-character preview; a preview is not the complete text.
Before summarising or quoting a long document, say how much of it you actually
read. Preserve surrounding content and settle where new text goes before
writing. Review or proofread alone is read-only: give the source text, the
correction and the reason before applying edits.

## Adding text

For structured drafting use `writer_insert_paragraphs`: up to 50 paragraphs in
one call, each with its own paragraph style, at the `start`, the `end`, or
`after` a paragraph number from `writer_get_structure` (pass that paragraph's
full text as `expected_text`; a moved or edited anchor is refused as `Stale`).
Every style is checked before anything is written, so an unknown style changes
nothing. The whole insert is one undo step, and the result lists the new
paragraph numbers with their styles as read back and `verified`. A paragraph
given without a style inherits its neighbour's style; name the style whenever
it matters. Each item is one paragraph without line breaks.

`writer_insert_text` adds plain text at the `start` or `end`. With
`as_paragraph: true` the text becomes its own paragraph, and newlines inside it
start further paragraphs; the break and the text are one undo step. Without it
the text joins the first or last paragraph. To change text inside a paragraph,
use the selection: ask the user to place the cursor or select the text, then
follow *Working with the selection* below.

## Paragraph styles

Structure a document with paragraph styles rather than manual formatting.
`writer_set_paragraph_style` applies an existing style to one paragraph by its
number from `writer_get_structure`. Use the name shown in LibreOffice's style
list, with spaces. Checked live on LibreOffice 26.8: `Heading 1` to
`Heading 10`, `Title`, `Subtitle`, `Body Text`, `Default Paragraph Style`,
`Quotations`, `List 1` and `Caption` are accepted; `Heading1`, `Normal`,
`Text Body` and `List Bullet` are refused. A style that does not exist in the
document is refused as `Validation`, and a localized or custom document may
use other names, so read the styles already in use from the structure first.
`writer_get_structure` and the tool's result report LibreOffice's internal
names, which differ for two common styles: `Default Paragraph Style` reads
back as `Standard` and `Body Text` as `Text body`. Both forms are accepted.

Paragraph numbers shift when text is added or removed. Pass the paragraph's
full text as `expected_text` so a moved or edited paragraph is refused as
`Stale` instead of styling the wrong one, and read the structure again after
every insert. Apply one style, read back, then the next. A style name does not
guarantee font, size or spacing: those come from the document's style
definitions.

## Targeted corrections

For corrections such as typos, use `writer_apply_edits`: up to 50 exact,
literal `{find, replace}` edits in one undo step, all or nothing. Every edit is
located first; if one is missing, appears more than once without an
`occurrence`, or overlaps another, the whole batch is refused with the reasons
and nothing changes. Pick `find` strings that are unique, or count the matches
in the text you read and give `occurrence` (1-based, in document order). Pass
the `fingerprint` from `writer_get_text` as `if_match` so a document that
changed since you read it is refused as `Stale`. Report each edit from the
result; never say all were applied unless `verified` is true.

## Find and replace

`writer_find_replace` replaces every match in the whole document and returns
the count. It is not limited to a selection or a paragraph. Use it only when
every occurrence should change. Read the text first, choose a search string
long enough to be unique to what should change, use `match_case` and
`whole_words` to narrow it, and compare the returned count with the number of
matches you expected. If the count is higher than expected, undo with
`libreoffice_undo` and narrow the search. It matches literal text, not
patterns.

## Working with the selection

`writer_get_selection` reads what is selected in that document's window. Do not
assume a previous write left its result selected. `writer_replace_selection`
replaces the single selection, or inserts at the cursor when nothing is
selected; it needs `expected_text` equal to the selection as you last read it
and is refused as `Stale` when the user has moved on. Read the selection
immediately before replacing it, and confirm the scope with the user when the
selection is long.

## Character formatting and comments

Both act on the single selection, so read it with `writer_get_selection` first
and pass it unchanged as `expected_text`; a changed selection is refused as
`Stale`. Ask the user to select the words when nothing is selected.
`writer_format_selection` sets bold, italic, underline, strikethrough,
`font_name`, `font_size`, `color` and `highlight` (`none` clears it) on the
selected characters only and returns the formatting before and after. Prefer
paragraph styles for whole headings and bodies; use character formatting for
words and phrases. `writer_add_comment` anchors a review comment to the
selected text without changing it, and `writer_get_comments` lists the
comments (up to 500 and about 1 MB; `truncated` says some were left out) with
their author, text and anchored text. Each change is one undo step.

## Documents that come from or go to Word (.docx)

The tools work the same on a document LibreOffice opened from `.docx`;
opening, saving and choosing the format are the user's actions, and no tool
opens, saves or exports a file. Checked live by saving through LibreOffice in
Word 2007-365 format, closing and reopening: the text, the paragraph styles
(Title, Heading 1 to 3, Body Text), character formatting (bold, italic,
underline, strikethrough, font, size, colour, highlight) and comments with
their author and anchored text all came back. `Quotations` came back as
`Block Quotation`, so after a reopen read the style names again from
`writer_get_structure` instead of reusing earlier ones.

## What this plugin cannot do in Writer

There is no tool for tracked changes, creating or editing tables, images, page
margins, headers and footers, lists, hyperlinks, deleting comments or
inserting rich content. Do not invent calls for them. Ask the user to make
those adjustments in LibreOffice, or agree a design that the available styles
and formatting can carry. Several unrelated changes are several calls: make
one, read back, then the next, and never claim all were applied when any was
refused.

## Check the result

After each change read the text or structure back and compare with what you
intended and with the unchanged neighbours. Example of adding a short menu at
the end (fictional copy), one call and one undo step:

1. `writer_insert_paragraphs` with `position: "end"` and
   `paragraphs: [{"text": "Sample menu", "style": "Heading 1"},
   {"text": "Coffee — USD 2.50 (fictional)", "style": "Body Text"}]`.
2. Check `verified` and the returned styles, or read `writer_get_structure`.

Pagination, clipping, price alignment and contrast need a look in LibreOffice.
Text and style readback are not visual acceptance; say when the look has not
been checked.
