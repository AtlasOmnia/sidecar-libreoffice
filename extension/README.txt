Sidecar for LibreOffice 0.5.0 (LibreOffice extension)
by Atlas Omnia

Starts an authenticated loopback-only bridge inside LibreOffice. Hermes tools operate only on explicitly bound open Calc, Writer, or Impress documents.

The Sidecar panel in the sidebar has an Open Sidecar button. When the sidebar chat is turned on in Hermes, a click opens the Sidecar window beside LibreOffice: on macOS an installed Chrome, Edge, Brave or Chromium is started in app mode with its own empty profile in your cache folder; otherwise, and on Windows in this release, the page opens as a normal tab in your default browser. A browser is started only on that click. The extension never reads your browser profiles, cookies or keychain. Besides the browser it starts only /usr/bin/osascript on macOS, to bring an open Sidecar window to the front, and /usr/bin/open on macOS, to open a normal tab; on Windows the tab opens through the file association for .html files. It runs no shell. See the README of the Sidecar for LibreOffice repository for the risk disclosures.

Licensed under the Apache License 2.0; see LICENSE and NOTICE in this package.
