# Sidecar icon provenance

Sidecar for LibreOffice uses a shared **S** icon in the existing Sidecar style.

The rounded tile, orbital strokes, and bold-letter composition are adapted from Atlas Omnia's Apache-2.0 Sidecar Word source, `word/assets/icon.svg` in https://github.com/AtlasOmnia/sidecar at `74a6c41`. The source used a Word-specific W and blue palette. This adaptation changes the letter to S, applies the Sidecar teal family, and removes the drop shadow. The small-size variant deliberately omits orbit details and enlarges S to stay legible at 16–32px. This is new artwork for LibreOffice, not a previously existing generic Sidecar logo.

- `sidecar.svg`: full vector master, 48px and larger.
- `sidecar-small.svg`: simplified vector master, 16–32px.
- `extension/icons/sidecar-deck.png`: 32px RGBA export of the small master (named `hermes-deck.png` before 0.4.0; the deck is `SidecarDeck`).

Rasterization: macOS `sips -s format png`, then Pillow LANCZOS reduction. Actual rendered samples were inspected on light and dark grounds at 16, 24, 32, 48 and 256px. Both masters retain Apache-2.0 SPDX notices; repository LICENSE applies.
