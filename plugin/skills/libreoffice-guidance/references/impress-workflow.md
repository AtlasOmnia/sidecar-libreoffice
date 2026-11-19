<!-- SPDX-License-Identifier: Apache-2.0 -->
# Impress: editable, readable decks

Read `impress_list_slides` for the slide count, titles and text (up to 200
slides), then `impress_get_slide` for the slide you will work on. Slides are
numbered from **1**. `impress_get_slide` returns the slide's size in
centimetres, its layout number and every shape with its number, type, text,
position and size, and marks the title and body placeholders. Shape numbers
are positions on the slide and change when shapes are added or removed, so
read the slide again before each write that names a shape. Pass what you read
so a moved slide or shape is caught: `expected_title` (the slide's title text
as last read) with `impress_set_slide_layout`, `impress_add_shape` and
`impress_format_text`, and `expected_text` (the shape's full text from
`impress_get_slide`) with `impress_format_text`. If either no longer matches,
the call is refused as Stale and nothing changes; read the slide again and
retry with the current numbers. Preserve unrelated
slides, wording and order. Establish audience, purpose, facts and brand first.
Use the design playbook for typography, grid, spacing and contrast.

Make each slide carry one message with one dominant title and supporting
evidence. Keep editable text as text. Reduce or split content rather than
shrinking everything; a sparse slide is not a reason for filler.

## Supported tools, not assumed Impress features

- `impress_create_slide` adds a slide after `after_slide` (at the end when
  omitted) with a layout: `title` (title and subtitle), `title_content` (title
  and body), `title_only` or `blank`. `title` and `body` go into the layout's
  placeholders; newlines in `body` separate points. The layout must have a
  placeholder for the text you pass. Check `title_set` and `body_set` in the
  result, then read the slide before adding anything else.
- `impress_set_shape_text` replaces the whole text of one shape. Name it by
  `placeholder` (`title` or `body`) when you can, otherwise by `shape` number
  from a fresh `impress_get_slide`. The result includes `previous_text`.
- `impress_add_text_box` takes `x_cm`, `y_cm`, `width_cm` and `height_cm` in
  centimetres from the slide's top-left corner. Read the slide size first and
  keep the box inside it and clear of other shapes.
- `impress_find_replace` changes every matching shape on one slide, or on every
  slide when `slide` is omitted: inspect the matches and decide the scope
  before writing, and compare the returned count with what you expected.
- `impress_delete_slide` removes one slide; the last slide cannot be deleted.
  Confirm a deletion first. Slide numbers after it shift down by one.
- `impress_set_slide_layout` switches an existing slide to `title`,
  `title_content`, `title_two_content`, `title_only` or `blank`. Placeholders
  that already hold text are kept even when the new layout has no slot for
  them; check `text_preserved` and read the slide, because a kept placeholder
  may need moving or deleting by the user. It refuses, changing nothing, to
  turn a filled body into a subtitle or back (`title` against the content
  layouts) and to change a slide whose placeholders do not match its layout,
  because undo could not restore those exactly.
- `impress_format_text` sets bold, italic, underline, `font_name`,
  `font_size` and `color` on all the text of one shape. It refuses a property
  that is mixed inside the shape (one bold word, two sizes), because undo could
  not put that back; format a simpler shape or ask the user.
- `impress_add_shape` adds a `rectangle`, `rounded_rectangle`, `ellipse` or
  `line` with optional `fill_color`, `line_color` and `text`, in centimetres
  like a text box. Read the slide size first and keep shapes clear of text.
- `impress_get_current_slide` says which slide the user is on (and the slide
  sorter selection when it is showing), so "this slide" can be resolved
  before reading or editing; `impress_list_layouts` lists the layouts above,
  the master slides and each slide's current layout.
- There is no tool to reorder slides, move or resize a shape, insert images,
  or edit speaker notes, animations or the master. Do not invent calls. Ask
  the user to do those in Impress or agree a simpler slide.

## Decks that come from or go to PowerPoint (.pptx)

The tools work the same on a deck LibreOffice opened from `.pptx`; opening,
saving and choosing the format are the user's actions, and no tool opens,
saves or exports a file. Checked live by saving through LibreOffice in
PowerPoint 2007-365 format, closing and reopening: slide order and text,
each slide's layout, title formatting (bold, size, colour), shape positions
and sizes (within 0.01 cm) and text-box text came back. Lost or changed:
fill and line colours set with `impress_add_shape` were saved as the theme's
accent colour; rectangles, rounded rectangles, ellipses and text boxes came
back as custom shapes, for which `impress_get_slide` reports no fill or line
colour; text in new shapes changed from Liberation Sans to Arial; master
slides were split and renamed; body placeholders on existing slides came back
slightly shorter. For a deck that will be saved as `.pptx`, tell the user
that shape colours need checking in Impress after saving, or keep the deck in
the ODF format.

## Outline to deck

Turn an outline into slides with one `impress_create_slide` per section, in
order, with `layout: "title_content"`, the heading as `title` and its points as
`body`. Check `title_set` and `body_set` each time and read the slide list at
the end. N slides are N undo steps, and `libreoffice_undo` takes at most 10 per
call.

## Undo in Impress

Every changing tool can be undone with `libreoffice_undo`, newest first.
For new slides, text boxes, shapes, layout changes, text formatting, shape
text and find-and-replace the undo step is supplied by the plugin; for shape
text and find-and-replace it puts previous shape text back as plain text: if
a shape mixed several character formats (one bold word, two colours), undo
restores the words but not the mixed formatting. Before replacing the text of
a richly formatted shape, tell the user, or prefer a new text box.

## Check the result

After each change read the slide list or the slide and check the exact text,
slide count and order, and unchanged neighbours. On `ExecutionUncertain` or a
timeout, read before any retry; never create a second slide just to retry.

Readback is not visual QA. Text that overflows a placeholder, overlaps,
contrast and balance are visible only in Impress, at full size and in the
slide sorter. If you cannot see the deck, say that visual acceptance is
pending.
