---
name: calc-plans
description: Use when a multi-step Calc change should be one verified undo step through calc_execute_plan.
version: 1.0.0
license: Apache-2.0
---
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Calc workbook plans

`calc_execute_plan` takes a `document_id` and one WorkbookPlan V1 object. The
whole plan is validated before anything changes, an execute plan runs as one
undo step, and it is sent once and never retried. Use it when several writes
belong together; use the ordinary `calc_*` tools for a single change. Formulas
follow LibreOffice syntax (semicolons between arguments, `Sheet.A1` for other
sheets): see the Calc workflow in `atlas-sidecar-libreoffice:libreoffice-guidance`.

## Plan shapes

A plan has exactly the fields of its outcome, and no others:

| `outcome` | Fields besides `version: 1` and `outcome` |
| --- | --- |
| `inspect` | `reason` (1,024 characters max), `reads` (1–50) |
| `execute` | `summary` (2,048 max), `operations` (1–30), `verification` (1–30) |
| `ask` | `message` (2,048 max) |
| `refuse` | `message` (2,048 max) |

Reads, each with `kind`, `sheet`, `address`: `read_range`,
`summarize_range_stats`.

Operations:

| `kind` | Other fields |
| --- | --- |
| `write_values` | `sheet`, `address`, `values` (2D matrix matching the address) |
| `write_formula` | `sheet`, `address` (one cell), `formula` (starts with `=`) |
| `format_range` | `sheet`, `address`, `format` (same keys as `calc_format_range`) |
| `clear_range` | `sheet`, `address` |
| `create_sheet` | `name` |

Verification rules, each with `sheet` and `address` except `sheet_exists`:

| `kind` | Other field |
| --- | --- |
| `range_values_equal` | `expectedValues` |
| `range_formulas_equal` | `expectedFormulas` |
| `range_number_format_equal` | `expectedNumberFormat` |
| `sentinel_unchanged` | `expected` (one cell, a scalar): a cell that must not change |
| `sheet_exists` | `name` (no `sheet` or `address`) |

Anything else is rejected, including other WorkbookPlan kinds such as tables,
charts and sheet deletion.

## How to use it

- When you need workbook facts first, send an `inspect` plan or use
  `calc_get_range`, and read the result before proposing an execute plan.
- Give every execute plan verification that would fail if the change went
  wrong, and a `sentinel_unchanged` rule for a nearby cell that must stay as it
  is. Read the verification results in the reply before saying it worked.
- Do not mix a plan with other changing tools in the same step, and send one
  plan at a time.
- On `ExecutionUncertain` do not send the plan again: an earlier attempt may
  already have changed the workbook. Read the affected ranges and report what
  they show.
- An executed plan is one step for `libreoffice_undo`. A plan that adds sheets
  is titled `Hermes: execute workbook plan (adds sheets)`.
- `ask` and `refuse` change nothing; use them to return a question or a refusal
  through the same channel.
