Sidecar for LibreOffice 0.5.1 (LibreOffice extension)
by Atlas Omnia

Starts an authenticated loopback-only bridge inside LibreOffice. Hermes tools operate only on explicitly bound open Calc, Writer, or Impress documents.

The Sidecar panel in the sidebar has an Open Sidecar button. When the sidebar chat is turned on in Hermes, a click opens the Sidecar window beside LibreOffice: on macOS an installed Chrome, Edge, Brave or Chromium is started in app mode with its own empty profile in your cache folder; otherwise, and on Windows in this release, the page opens as a normal tab in your default browser. A browser is started only on that click. The extension never reads your browser profiles, cookies or keychain. Besides the browser it starts only /usr/bin/osascript on macOS, to bring an open Sidecar window to the front, and /usr/bin/open on macOS, to open a normal tab; on Windows the tab opens through the file association for .html files. It runs no shell. See the README of the Sidecar for LibreOffice repository for the risk disclosures.

Optional Follow LibreOffice is off by default in eligible macOS app windows, separately from turning chat on. Settings remembers the preference in the dedicated browser profile; missing, invalid or unavailable storage means off. A hand move or resize undocks Sidecar until Reattach or a new session; Reattach restores 400 points wide. Open Sidecar raises the browser and does not reattach or guarantee document-specific focus. Following is delayed, not rigid docking; its 0.5.1 acceptance and release hashes are pending in this draft.

Sidecar does not minimise with LibreOffice: it stays where it is and following pauses while LibreOffice is minimised or hidden. There is no linked minimise or following across Spaces. Fullscreen is an ordinary large window. Second-display, negative-coordinate and Retina/mixed-scale placement are explicitly untested; on an other-display pause, move Sidecar there by hand, then Reattach. Switch following off if placement is wrong. A hand drag during the 400 ms settling interval can snap back once. Windows keeps the default-browser tab with no following; Chrome-first app-window work is a 0.5.2 investigation, not a promised feature.

Licensed under the Apache License 2.0; see LICENSE and NOTICE in this package.
