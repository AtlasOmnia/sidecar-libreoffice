# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Day-0 seam for optional macOS following (web-protocol.md revision 5, 13.2).

Lane C2 implements this pure-Python interface; no UNO imports or worker UNO reads.
The UNO listener in hermes_sidebar hands off minimal O(1) callback work here.
No I/O, threads or following is enabled by this intentionally inert seam.
"""


class FollowHandle:
    """One document binding; Day-0 stub remains inactive."""

    alive = False

    def update(self, rect, state, work_area):
        """Main-thread, nonblocking latest-value hand-off; unchanged values dropped."""

    def bind(self, launch_id, port):
        """Launch worker after signed follow true and exchanged; restart seq at 1."""

    def close(self):
        """Document close, window disposal or quit: no later send."""


class Follower:
    """Process-wide serialized follow worker interface (implementation in lane C2)."""

    def attach(self, document_id):
        """Main-thread click: one handle per document in the implementation."""
        return FollowHandle()

    def stop(self):
        """Quit: no further request may start."""
