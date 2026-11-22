# Follow LibreOffice

0.5.1 candidate: this page describes the implemented behavior, not an unconditional acceptance pass. See [release evidence](release-0.5.1.md) for what has and has not run.

## Turn it on — or keep it off

The sidebar chat is off by default. Enabling chat does **not** enable following. In a supported macOS app window, **Settings → Follow LibreOffice** is also **off by default**. Turn it on to follow the document window that opened Sidecar. The dedicated browser profile remembers the preference across sessions. Only `sidecar.follow=on` opts in; absent, invalid or unavailable storage means off. Turning it off stores `off` and stops follow placement and window polling. It does not close the chat.

The switch is shown only when the extension and plugin negotiated following. A 0.5.0 extension with a 0.5.1 plugin, or the reverse, opens the window without following. A normal browser tab never follows. Windows 0.5.1 keeps its default-browser tab; Chrome-first app-window work belongs to a 0.5.2 investigation. There is no new plugin setting. For support, start LibreOffice with `HERMES_LO_SIDECAR_NO_FOLLOW=1` in its environment to disable negotiation; remove it and restart to make the option available again.

## Move it yourself; Reattach when ready

Hand-moving **or resizing** Sidecar undocks it. The page says **Sidecar is not following LibreOffice.** It stays undocked until **Reattach** or a new session; moving LibreOffice again does not reattach it. **Reattach restores the 400-point width**, asks for the newest document-window position and resumes following. A hand-chosen width is not remembered while following.

**Open Sidecar** in LibreOffice only raises the browser application; it does not reattach and does not guarantee that the right document's browser window is selected. Following itself never raises or focuses the browser.

This is delayed following, not rigid docking. Candidate acceptance thresholds are median ≤150 ms, 95th percentile ≤250 ms and maximum ≤400 ms for one or two windows, and up to about 600 ms for fifth and later windows. The integrated candidate measured median/p95 79.738/110.343 ms (first), 70.497/102.404 ms (second), and 66.141/97.431 ms (fifth), 30 native moves each; 60 forced-renewal cold moves measured 45.823/91.806 ms. These are sampled observations on the tested Mac, not a general platform guarantee. After its own placement the page allows 400 ms to settle and completes a read-back before detecting a hand move; more than 8 points of position or size change then undocks it. **Hand changes made before placement and its read-back settle can be overwritten; further host placements restart that interval.** If the window fights your placement, turn following off.

A single unanswered bounds call is retried on a fresh channel with bounded back-off and the latest geometry. After five consecutive busy/unanswered calls, or a terminal follow failure, close Sidecar and click Open Sidecar again; clicking it while the old window is still live only raises it.

Even with the switch off, the extension still posts the launching window’s rectangle and state to the local plugin, up to ten times a second during changes. `HERMES_LO_SIDECAR_NO_FOLLOW=1` stops that transport.

## Minimise, fullscreen and displays

- **No linked minimise.** Sidecar does not minimise with LibreOffice; it stays where it is and following pauses. It resumes on normal geometry after restore. Hidden LibreOffice windows pause following too; hiding the Sidecar page stops its window polling and placement until it is visible and eligible again.
- **No following across Spaces.** Native green-button fullscreen/Space detection is not qualified. Fullscreen is treated as an ordinary large window, possibly overlapping its right edge; Sidecar can remain in the old Space. It does not activate itself to follow you. Stage Manager and tiling are untested.
- **Second-display, negative-coordinate, Retina (2×) and mixed-scale placement are explicitly untested on hardware.** When the browser reports an extended desktop and the target is on another display, the intended safe behavior is to pause with **LibreOffice is on another display. Move this window there, then choose Reattach.** Move Sidecar there yourself before Reattach. It never asks for window-management permission. Detection and coordinate units are not hardware-qualified; if placement is wrong, move/resize to undock or switch following off.
- Adding/removing a display or changing resolution is untested. Changed screen measurements re-baseline hand-move detection rather than automatically counting as a hand undock; this is not evidence that display changes work correctly.

## Document and browser limits

Following belongs to the document window that launched Sidecar, not whichever LibreOffice window is frontmost. Closing that window, disposing it or loading a different document ends its following. A same-document view switch, including Print Preview, keeps Sidecar and its reply open and keeps following attached. A second frame on the same document does not take over. If that second frame keeps the document open when the launching frame closes, Sidecar and its reply stay open, but following stops; close Sidecar and click Open Sidecar from the remaining frame to follow it. At most eight sessions are supported; more than eight windows are untested. After a LibreOffice crash the old Sidecar cannot rebind: close it and click Open Sidecar again after restarting LibreOffice.

A browser crash can leave **Sidecar is already open** in the panel for about two minutes. That is not proof a window is alive, nor a guarantee every still-polling session expires at 90 seconds. Document-close releases retry within a fixed budget, but all attempts can fail. Hidden/minimised pages can notice a release up to about a minute late.

macOS fallback uses the `.html` association; an editor association cannot complete a browser launch. Browser-level focus may select the wrong window. Managed sign-in, extension, file/loopback URL or proxy policies can prevent connection; Sidecar neither bypasses them nor tries another browser after a failed connection. An unsafe cache is refused with **The Sidecar cache folder is unsafe. See README ▸ Which browser.** Do not loosen its permissions or bypass the private-file check.

Chrome is the reference browser in the macOS evidence. Edge, Brave and Chromium on macOS, Intel Macs, macOS versions other than 27.2.1 and LibreOffice versions other than 26.8.x are unverified. A future browser can stop honoring app-window movement; Sidecar may then stay where it launched. The [ledger](release-0.5.1.md) also identifies untested screen readers, Windows handlers, literal 12-hour soak and managed environments.
