<!-- SPDX-License-Identifier: Apache-2.0 -->
# Guidance provenance and license boundary

This guidance is distributed with Sidecar for LibreOffice under Apache-2.0. It
is adapted from the `office-guidance` and `workbook-plans` skills of Sidecar by
Atlas Omnia (https://github.com/AtlasOmnia/sidecar, Apache-2.0, same
maintainer), at commit `74a6c41`:

| File here | Source in Sidecar | What changed |
| --- | --- | --- |
| `design-playbook.md` | `design-playbook.md` (identical in its three plugins) | Six product mentions reworded; otherwise unchanged |
| `calc-workflow.md` | `excel-workflow.md` | Tool names, LibreOffice formula syntax, undo and stale-write rules; table, pivot, chart and plan-rollout passages replaced by what this plugin cannot do |
| `impress-workflow.md` | `powerpoint-workflow.md` | Rewritten for this plugin's tools, 1-based slides, centimetres and undo |
| `writer-workflow.md` | `word-workflow.md` | The read-first and native-style guidance adapted; the OOXML sections dropped (there is no such tool) |
| `calc-plans` skill | `workbook-plans` skill | Rewritten from this plugin's plan validator |

Sidecar's own provenance note applies to the inherited concepts: its guidance
is original synthesis of general ideas from several MIT-declared operator
guides (hermes-excel, hermes-word, hermes-powerpoint, hermes-office-addins,
marketing-collateral-design and claude-design; their authors are listed in
Sidecar's provenance note), with no expressive text, script, template or asset
copied from them. A
metadata declaration is not proof of a complete third-party license grant,
which is why concepts were re-expressed rather than copied.

No private endpoint, paid pack, font, image or proprietary template is
included. This plugin's schemas and bridge, not this guidance, are the
authority for what the tools do. The formula syntax and number-format notes in
`calc-workflow.md` marked as checked live were run on LibreOffice 26.8.2.2;
everything else is static guidance, not behaviour acceptance.
