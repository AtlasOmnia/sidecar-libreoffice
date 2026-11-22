# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Deterministic transport gates and reusable actual-listener timing harness.

Integrator: import measure_callbacks; pass a zero-argument callable invoking
C1's actual UNO listener (including its real geometry/state reads). Persist the
returned durations and require assert_callback_budget(result). Running the
handoff-only test below is deliberately NOT an actual-UNO acceptance result.
"""
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'extension' / 'python'))
import window_follow as follow
import sidecar_launcher as launcher

RECT = (100, 200, 700, 600)
AREA = (0, 0, 1920, 1080)


def measure_callbacks(callback, count=1000):
    """Time the ENTIRE caller-supplied callback, not just mailbox publication.

    No warmup, trimming, percentiles hiding outliers or outlier exemptions.
    callback may rotate actual window events/reads through a closure. Arrange
    disposable LO/PID ownership in the native harness, not in this module.
    """
    if count < 1000:
        raise ValueError('at least 1000 callbacks required')
    durations = []
    for _ in range(count):
        start = time.perf_counter_ns()
        callback()
        durations.append((time.perf_counter_ns() - start) / 1_000_000)
    return {'count': count, 'p100_ms': max(durations), 'durations_ms': durations}


def assert_callback_budget(result):
    if result['count'] < 1000 or len(result['durations_ms']) != result['count']:
        raise AssertionError('insufficient callback observations')
    if max(result['durations_ms']) > 5.0 or result['p100_ms'] != max(result['durations_ms']):
        raise AssertionError('actual callback maximum exceeds 5 ms or summary disagrees')


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds=0.100001):
        self.now += seconds


class Client:
    def __init__(self, port, read_token, *, timeout):
        self.port, self.timeout = port, timeout
        self.calls = []
        self.effects = []

    def hello(self):
        self.calls.append(('hello', None))
        return {}

    def call(self, method, path, payload):
        self.calls.append((path, payload))
        if self.effects:
            effect = self.effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            if callable(effect):
                return effect(payload)
            return effect
        return 200, {'ok': True, 'seq': payload['seq']}


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clients = []
        self.starts = []

        def factory(port, read_token, **kwargs):
            client = Client(port, read_token, **kwargs)
            self.clients.append(client)
            original_hello, original_call = client.hello, client.call
            def hello():
                self.starts.append(self.clock())
                return original_hello()
            def call(*args):
                self.starts.append(self.clock())
                return original_call(*args)
            client.hello, client.call = hello, call
            return client
        self.engine = follow.Follower(lambda: 'not-read-in-test', client_factory=factory,
                                      clock=self.clock, autostart=False)
        self.addCleanup(self.engine.stop)
        self.handle = self.engine.attach('doc')
        self.handle.update(RECT, 'normal', AREA)
        self.handle.bind('A' * 22, 12345)

    def pump(self):
        return self.engine.run_once()

    def send_first(self):
        self.pump()  # independent channel hello, immediately
        self.clock.advance()
        self.pump()  # first bounds at earliest request-budget admission
        return self.clients[-1].calls[-1][1]

    def change(self, x):
        self.handle.update((x, 200, 700, 600), 'normal', AREA)

    def test_one_handle_per_document_and_attach_after_close(self):
        self.assertIs(self.handle, self.engine.attach('doc'))
        self.handle.close()
        self.assertIsNot(self.handle, self.engine.attach('doc'))

    def test_unbound_has_no_io_and_latest_is_bounded(self):
        self.handle._binding = None
        for x in range(2000):
            self.change(x)
        self.assertIsNone(self.pump())
        self.assertEqual(self.clients, [])
        self.assertEqual(self.handle._latest[0][0], 1999)
        self.handle.bind('B' * 22, 12345)
        self.assertEqual(self.send_first()['seq'], 1)

    def test_immediate_global_spacing_trailing_and_unchanged(self):
        self.assertAlmostEqual(self.pump(), .1)
        self.assertEqual(self.starts, [100.0])
        self.change(200)
        self.change(300)
        self.assertGreater(self.pump(), 0)
        self.assertEqual(len(self.starts), 1)
        self.clock.advance()
        self.pump()
        body = self.clients[-1].calls[-1][1]
        self.assertEqual(body['bounds']['x'], 1000)
        for _ in range(5):
            self.change(300)
            self.clock.advance()
            self.pump()
        self.assertEqual(len(self.starts), 2)
        self.change(400)
        self.pump()
        self.assertEqual(self.clients[-1].calls[-1][1]['seq'], 2)
        self.assertTrue(all(b - a >= .1 for a, b in zip(self.starts, self.starts[1:])))

    def test_multiple_windows_share_pacing_and_progress(self):
        second = self.engine.attach('other')
        second.update(RECT, 'normal', AREA)
        second.bind('B' * 22, 12345)
        for _ in range(8):
            self.pump()
            self.clock.advance()
        payloads = [p for c in self.clients for route, p in c.calls if p]
        self.assertEqual({p['launch_id'] for p in payloads}, {'A' * 22, 'B' * 22})
        self.assertTrue(all(b - a >= .1 for a, b in zip(self.starts, self.starts[1:])))

    def test_renew_before_48_calls(self):
        self.send_first()
        for i in range(1, 48):
            self.change(100 + i)
            self.clock.advance()
            self.pump()
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len(self.clients[0].calls), 48)  # hello + 47 signed calls
        self.clock.advance()
        self.pump()
        self.assertEqual(self.clients[1].calls[-1][1]['seq'], 48)

    def test_age_renewal_before_90_seconds_and_no_idle_requests(self):
        self.send_first()
        self.clock.advance(100)
        self.assertIsNone(self.pump())
        self.change(301)
        self.pump()
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[-1].calls[0][0], 'hello')

    def test_timeout_is_one_second_on_independent_client(self):
        self.send_first()
        self.assertEqual(self.clients[0].timeout, 1.0)
        self.assertIsNotNone(self.engine._client)
        actual = follow.Follower(lambda: 'token', autostart=False)
        self.addCleanup(actual.stop)
        client = actual._new_client(12345)
        self.assertIsInstance(client, launcher.LauncherClient)
        self.assertEqual(client._timeout, 1.0)

    def test_busy_backoff_retains_latest_drops_channel_and_fifth_unbinds(self):
        self.pump()
        for attempt, delay in enumerate((.25, .5, 1., 1., 1.), 1):
            self.clients[-1].effects.append(launcher.LauncherBusy())
            self.clock.advance()
            self.change(100 + attempt)
            self.pump()
            self.assertIsNone(self.engine._client)
            self.assertEqual(self.handle._busy, attempt)
            self.assertAlmostEqual(self.handle._ready, self.clock() + delay)
            self.assertIsNone(self.handle._sent)
            if attempt < 5:
                count = len(self.starts)
                self.pump()
                self.assertEqual(len(self.starts), count)
                self.clock.advance(delay)
                self.pump()  # replacement hello
        self.assertIsNone(self.handle._binding)
        self.clock.advance(10)
        self.assertIsNone(self.pump())

    def test_success_resets_busy_and_keeps_inflight_trailing_value(self):
        self.send_first()
        self.handle._busy = 3
        self.change(200)
        def during_io(payload):
            self.change(300)
            return 200, {'ok': True, 'seq': payload['seq']}
        self.clients[-1].effects.append(during_io)
        self.clock.advance()
        self.pump()
        self.assertEqual(self.handle._busy, 0)
        self.clock.advance()
        self.pump()
        self.assertEqual(self.clients[-1].calls[-1][1]['bounds']['x'], 1000)

    def test_all_failures_unbind_and_rebind_resets_seq(self):
        for effect in (ConnectionRefusedError(), launcher.LauncherError('proof'),
                       TimeoutError(), (409, {'code': 'attachment_stale'}),
                       (200, {'ok': True, 'seq': 999}), (400, {})):
            self.handle.bind('A' * 22, 12345)
            self.engine._client = None
            self.clock.advance()
            self.pump()
            self.clients[-1].effects.append(effect)
            self.clock.advance()
            self.pump()
            self.assertIsNone(self.handle._binding)
            self.assertIsNone(self.engine._client)
            count = len(self.starts)
            self.change(302)
            self.clock.advance()
            self.pump()
            self.assertEqual(len(self.starts), count)
        self.handle.bind('B' * 22, 12345)
        self.assertEqual(self.send_first()['seq'], 1)

    def test_close_stop_and_rebind_during_io_fence_completion(self):
        self.send_first()
        self.change(201)
        def rebind(payload):
            self.handle.bind('B' * 22, 12345)
            return 200, {'ok': True, 'seq': payload['seq']}
        self.clients[-1].effects.append(rebind)
        self.clock.advance()
        self.pump()
        self.assertIsNone(self.handle._sent)
        self.clock.advance()
        self.pump()
        self.assertEqual(self.clients[-1].calls[-1][1]['launch_id'], 'B' * 22)
        self.assertEqual(self.clients[-1].calls[-1][1]['seq'], 1)
        self.handle.close()
        self.change(500)
        self.clock.advance()
        count = len(self.starts)
        self.pump()
        self.engine.stop()
        self.assertFalse(self.handle.alive)
        self.assertFalse(self.engine.attach('after-stop').alive)
        self.pump()
        self.assertEqual(len(self.starts), count)

    def test_no_worker_io_lock_or_uno_read_and_one_inflight(self):
        self.send_first()
        entered, release = threading.Event(), threading.Event()
        def blocked(payload):
            entered.set()
            if not release.wait(2):
                raise TimeoutError()
            return 200, {'ok': True, 'seq': payload['seq']}
        self.clients[-1].effects.append(blocked)
        self.change(200)
        self.clock.advance()
        worker = threading.Thread(target=self.pump)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            result = measure_callbacks(lambda: self.change(300))
            assert_callback_budget(result)
            self.handoff_result = result
            self.handle.close()
            self.engine.stop()
            self.assertTrue(worker.is_alive())  # stop did not wait for I/O
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_geometry_floor_paused_states_and_other_display(self):
        self.assertEqual(self.send_first()['bounds'], dict(x=800, y=200, width=400, height=600))
        for state in ('hidden', 'minimized', 'window_closed'):
            self.handle.update(None, state, None)
            self.clock.advance()
            self.pump()
            self.assertIsNone(self.clients[-1].calls[-1][1]['bounds'])
        before = len(self.starts)
        self.handle.update((1, 2, 200, 600), 'normal', AREA)
        self.clock.advance()
        self.pump()
        self.assertEqual(len(self.starts), before)
        self.handle.update((2000, 200, 700, 600), 'normal', AREA)
        self.pump()
        self.assertEqual(self.clients[-1].calls[-1][1]['bounds']['x'], 2700)
        self.handle.update((1500, 200, 400, 600), 'normal', AREA)
        self.clock.advance()
        self.pump()
        self.assertEqual(self.clients[-1].calls[-1][1]['bounds']['x'], 1520)

    def test_copy_input_and_nonblocking_callback_harness_rejects_outlier(self):
        rect = list(RECT)
        self.handle.update(rect, 'normal', list(AREA))
        rect[0] = 999
        self.assertEqual(self.handle._latest[0][0], 100)
        with self.assertRaises(ValueError):
            measure_callbacks(lambda: None, 999)
        with self.assertRaises(AssertionError):
            assert_callback_budget({'count': 1000, 'p100_ms': 11.4262,
                                    'durations_ms': [0.] * 999 + [11.4262]})

    def test_different_ports_do_not_hello_each_other_forever(self):
        second = self.engine.attach('other')
        second.update(RECT, 'normal', AREA)
        second.bind('B' * 22, 12346)
        for i in range(12):
            self.change(100 + i)
            self.pump()
            self.clock.advance()
        payloads = [p for c in self.clients for route, p in c.calls if p]
        self.assertEqual({p['launch_id'] for p in payloads}, {'A' * 22, 'B' * 22})
        self.assertTrue(all(b - a >= .1 for a, b in zip(self.starts, self.starts[1:])))

    def test_hello_failure_and_busy_are_fenced(self):
        self.engine._factory = lambda *args, **kwargs: mock.Mock(
            hello=mock.Mock(side_effect=ConnectionRefusedError()))
        self.pump()
        self.assertIsNone(self.handle._binding)
        self.assertIsNone(self.engine._client)
        self.handle.bind('B' * 22, 12345)
        self.clock.advance()
        self.engine._factory = lambda *args, **kwargs: mock.Mock(
            hello=mock.Mock(side_effect=launcher.LauncherBusy()))
        self.pump()
        self.assertEqual(self.handle._busy, 1)
        self.assertAlmostEqual(self.handle._ready, self.clock() + .25)
        self.assertIsNone(self.engine._client)

    def test_sequence_exhaustion_unbinds_instead_of_wrapping(self):
        self.send_first()
        self.handle._seq = 2147483647
        self.change(204)
        self.clock.advance()
        count = len(self.starts)
        self.pump()
        self.assertIsNone(self.handle._binding)
        self.assertEqual(len(self.starts), count)

    def test_real_client_trickle_has_one_whole_call_deadline(self):
        import socket
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        server.settimeout(2)
        quit_event = threading.Event()
        def trickle():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    connection.recv(16384)
                    for byte in b'HTTP/1.1 200 OK\r\n':
                        connection.sendall(bytes([byte]))
                        if quit_event.wait(.2):
                            return
            except OSError:
                pass
        thread = threading.Thread(target=trickle)
        thread.start()
        actual = self.engine._new_client if self.engine._factory is None else None
        real = follow.Follower(lambda: 'test-token', autostart=False)
        start = time.monotonic()
        try:
            client = real._new_client(server.getsockname()[1])
            with self.assertRaises(launcher.LauncherError):
                client.hello()
            self.assertLess(time.monotonic() - start, 1.8)
        finally:
            quit_event.set()
            server.close()
            thread.join(3)
            real.stop()
        self.assertFalse(thread.is_alive())

    def test_actual_worker_lifecycle(self):
        engine = follow.Follower(autostart=True)
        engine.stop()
        engine._thread.join(1)
        self.assertFalse(engine._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
