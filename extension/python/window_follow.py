# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Latest-value follow transport, with no UNO or main-thread I/O.

Use one Follower per process, configured with the launcher's token reader.
Rectangles/work areas passed to update are four-int tuples, never UNO objects.
Only the producer writes ``_latest``; CPython's atomic reference assignment is
our mailbox publication (LibreOffice's Python runtime). No worker lock is
acquired by update, and no lock is held during a client operation.
"""
import threading
import time


class FollowHandle:
    """A frame-owned mailbox; a failed binding needs an explicit bind to resume."""

    def __init__(self, owner, document_id):
        self._owner = owner
        self._document_id = document_id
        self._alive = True
        self._latest = None
        self._sent = None
        self._binding = None
        self._seq = 0
        self._busy = 0
        self._ready = 0.0
        self._coalesce_until = 0.0

    @property
    def alive(self):
        return self._alive and not self._owner._stopped

    def update(self, rect, state, work_area):
        """Publish copied primitive values in O(1), including while I/O stalls."""
        if not self.alive:
            return
        value = (None if rect is None else tuple(rect), state,
                 None if work_area is None else tuple(work_area))
        if value != self._latest:
            previous = self._latest
            if (previous is not None and previous is self._sent
                    and state == previous[1] == 'normal'
                    and value[0] is not None and previous[0] is not None
                    and min(previous[0][2:]) >= 240
                    and value[0][2:] != previous[0][2:]):
                # macOS can emit the resized rectangle at the OLD position,
                # then the correct move a few ms later. One fixed-size batch
                # avoids spending a 100 ms admission on that intermediate value.
                # Never extend this deadline during a continuous drag.
                self._coalesce_until = self._owner._clock() + .016
            self._latest = value
            self._owner._wake.set()

    def bind(self, launch_id, port):
        """Only after the launch worker verified follow and observed exchanged."""
        with self._owner._gate:
            if not self.alive:
                return
            self._binding = (launch_id, port, object())
            self._seq = self._busy = 0
            self._ready = 0.0
            self._coalesce_until = 0.0
            self._sent = None
        self._owner._wake.set()

    def close(self):
        """Fence future admissions; an already admitted call may finish."""
        with self._owner._gate:
            self._alive = False
            self._binding = None
            if self._owner._handles.get(self._document_id) is self:
                self._owner._handles.pop(self._document_id)
        self._owner._wake.set()


def _bounds(value):
    rect, state, area = value
    if state != 'normal':
        if state not in ('hidden', 'minimized', 'fullscreen', 'window_closed'):
            raise ValueError('invalid window state')
        return None
    if rect is None or area is None:
        raise ValueError('missing normal geometry')
    x, y, width, height = rect
    ax, ay, aw, ah = area
    if any(type(n) is not int for n in rect + area) or min(width, height) < 240:
        raise ValueError('invalid normal geometry')
    target = x + width
    if (ax <= x + width / 2 < ax + aw and ay <= y + height / 2 < ay + ah
            and target + 400 > ax + aw):
        target = ax + aw - 400
    return dict(x=max(-32768, min(32767, target)),
                y=max(-32768, min(32767, y)), width=400,
                height=min(8192, height))


class Follower:
    """One serialized worker. stop is nonwaiting, including during stalled I/O.

    client_factory has LauncherClient's (port, read_token, timeout=...) shape.
    clock/autostart are deterministic-test seams; run_once admits at most one
    request. Bounds starts share 100 ms pacing; the first bounds after hello
    need not wait for hello's spacing. All starts share a rolling 10/second cap.
    Each new binding restarts its wire sequence; rebind generations fence old
    completions. Failed calls never reuse a channel or retry a wire sequence.
    """

    def __init__(self, read_token=None, *, client_factory=None,
                 clock=time.monotonic, autostart=True):
        self._read_token = read_token
        self._factory = client_factory
        self._clock = clock
        self._handles = {}
        self._gate = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._next = 0.0
        self._starts = []  # At most ten admissions, including failed hellos.
        self._client = None
        self._port = None
        self._calls = 0
        self._opened = 0.0
        self._cursor = 0
        self._preferred = None
        self._hello_binding = None
        self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._run, name='sidecar-follow', daemon=True)
            self._thread.start()

    def configure(self, read_token):
        """Supply the existing safe token reader, never a token value."""
        self._read_token = read_token

    def attach(self, document_id):
        with self._gate:
            handle = self._handles.get(document_id)
            if handle is None or not handle.alive:
                handle = FollowHandle(self, document_id)
                if not self._stopped:
                    self._handles[document_id] = handle
                else:
                    handle._alive = False
            return handle

    def stop(self):
        with self._gate:
            self._stopped = True
            for handle in self._handles.values():
                handle._alive = False
                handle._binding = None
            self._handles.clear()
        self._wake.set()

    def _new_client(self, port):
        factory = self._factory
        if factory is None:
            from sidecar_launcher import LauncherClient
            factory = LauncherClient
        reader = self._read_token
        if reader is None:
            raise RuntimeError('follow token reader is not configured')
        return factory(port, reader, timeout=1.0)

    def run_once(self):
        """Worker-only pump. Return seconds until the next useful admission."""
        now = self._clock()
        with self._gate:
            if self._stopped:
                return None
            handles = tuple(self._handles.values())
            pending = [h for h in handles if h.alive and h._binding is not None
                       and h._latest is not None and h._latest is not h._sent]
            if not pending:
                return None
            self._starts = [start for start in self._starts if now - start < 1.0]
            budget = self._starts[0] + 1.0 if len(self._starts) == 10 else now
            eligible = [h for h in pending if max(h._ready, h._coalesce_until) <= now]
            paired = (self._preferred is not None and self._hello_binding is not None
                      and self._preferred in eligible and self._client is not None
                      and self._preferred._binding is self._hello_binding
                      and self._port == self._hello_binding[1]
                      and self._calls == 0 and now - self._opened < 89.0)
            delay = max(0.0, (now if paired else self._next) - now, budget - now)
            if delay or not eligible:
                return max(delay, min(max(h._ready, h._coalesce_until) for h in pending) - now, 0.0)
            # Round-robin across windows, not a queue of geometry updates.
            ordered = handles[self._cursor:] + handles[:self._cursor]
            handle = (self._preferred if self._preferred is not None
                      and self._preferred in eligible else next(h for h in ordered if h in eligible))
            self._preferred = None
            self._cursor = (handles.index(handle) + 1) % len(handles)
            binding = handle._binding
            value = handle._latest
            try:
                bounds = _bounds(value)
            except (ValueError, TypeError):
                handle._sent = value
                return 0.0
            hello = (self._client is None or self._port != binding[1]
                     or self._calls >= 47 or now - self._opened >= 89.0)
            # Admission linearizes under the close/stop gate, but I/O never
            # holds it. close/stop cancel all *subsequent* admissions.
            # A hello must still wait for the previous bounds admission, but
            # must not add 100 ms to its own first bounds. Count BOTH requests
            # in the process-wide rolling budget (also on failures/rebinds).
            self._starts.append(now)
            self._next = now + 0.1
            if not hello:
                if handle._seq >= 2147483647:
                    handle._binding = None
                    return 0.0
                handle._seq += 1
                payload = dict(launch_id=binding[0], seq=handle._seq,
                               state=value[1], bounds=bounds)
        try:
            if hello:
                self._client = self._new_client(binding[1])
                self._port = binding[1]
                self._opened = now
                self._calls = 0
                self._client.hello()
                outcome = 'hello'
            else:
                self._calls += 1
                status, answer = self._client.call('POST', '/launcher/bounds', payload)
                if status == 503 and answer.get('code') == 'endpoint_busy':
                    outcome = 'busy'
                elif status == 200 and answer.get('ok') is True and answer.get('seq') == payload['seq']:
                    outcome = 'ok'
                else:
                    outcome = 'failed'
        except Exception as exc:
            # LauncherBusy is imported lazily alongside the default client;
            # no error text is exposed to the panel or written to a log.
            from sidecar_launcher import LauncherBusy
            outcome = 'busy' if isinstance(exc, LauncherBusy) else 'failed'
        with self._gate:
            if outcome in ('busy', 'failed'):
                self._client = None
            if not handle.alive or handle._binding is not binding:
                return 0.0
            if outcome == 'hello':
                # Complete a hello/bounds pair before switching endpoint. Without
                # this, two ports could renew each other forever without sending.
                self._preferred = handle
                self._hello_binding = binding
            if outcome == 'busy':
                handle._busy += 1
                handle._ready = self._clock() + (0.25, 0.5, 1.0)[min(handle._busy - 1, 2)]
                if handle._busy >= 5:
                    handle._binding = None
            elif outcome == 'failed':
                handle._binding = None
            elif outcome == 'ok':
                handle._busy = 0
                handle._sent = value
        return 0.0 if outcome == 'hello' else max(0.0, self._next - self._clock())

    def _run(self):
        while not self._stopped:
            self._wake.clear()
            delay = self.run_once()
            if self._stopped:
                break
            # Bound timed sleeps while pending: macOS can heavily coalesce a
            # background process's long timed wait. Recheck the SAME admission
            # deadlines in short sleeps; idle still blocks indefinitely and no
            # extra network request is admitted by these wakeups.
            self._wake.wait(None if delay is None else min(delay, .01))
