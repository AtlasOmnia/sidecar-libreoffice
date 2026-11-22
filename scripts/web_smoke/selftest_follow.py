# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Concurrency and follow-stub regression checks, without native UI."""
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import follow_probe
import httpraw
import stub_server


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = stub_server.Stub(Path(self.tmp.name), timing=stub_server.Timing(longpoll=2))
        self.stub.start()
        assert self.stub.token is not None
        self.client = httpraw.LauncherClient(self.stub.port, self.stub.token)
        self.client.hello()

    def tearDown(self):
        if not self.stub.bridge.closed: self.stub.close(3)
        self.tmp.cleanup()

    def session(self, n):
        r = self.client.launch(httpraw.doc(n), follow=True).json()
        token = self.post('/api/session', {'launch': r['url'].split('#launch=')[1]}).json()['session']
        return r['launch_id'], token

    def post(self, path, payload, token=None):
        h = [('Origin', self.stub.origin), ('Content-Type', 'application/json')]
        if token: h.append(('X-Sidecar-Session', token))
        return httpraw.request(self.stub.port, 'POST', path, h, httpraw.jbody(payload))

    def poll(self, token, seq=0):
        return httpraw.request(self.stub.port, 'GET', '/api/window?seq='+str(seq),
                               [('Sec-Fetch-Site','same-origin'), ('X-Sidecar-Session',token)], timeout=5)

    def wait_parked(self, n):
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            with self.stub.cv:
                if self.stub.parked == n: return
            time.sleep(.005)
        self.fail('parked count did not reach '+str(n))

    def test_black_box(self):
        target = type('Endpoint', (), {'port': self.stub.port, 'token': self.stub.token, 'origin': self.stub.origin})()
        checks = follow_probe.run(target)
        self.assertTrue(all(c['ok'] for c in checks), checks)

    def test_four_slots_and_turn_preemption(self):
        sessions = [self.session(n) for n in range(8)]
        turn = self.post('/api/chat', {'message':'[hang]'}, sessions[4][1]).json()['turn']
        time.sleep(.2)
        state = httpraw.request(self.stub.port,'GET','/api/state',
                  [('Sec-Fetch-Site','same-origin'),('X-Sidecar-Session',sessions[4][1])]).json()['turn']
        with ThreadPoolExecutor(max_workers=5) as pool:
            pending = [pool.submit(self.poll, tok) for _,tok in sessions[:4]]
            self.wait_parked(4)
            full = self.poll(sessions[5][1])
            self.assertEqual(full.json()['retry_ms'],250)
            headers = [('Sec-Fetch-Site','same-origin'),('X-Sidecar-Session',sessions[4][1])]
            turnpoll = pool.submit(httpraw.request,self.stub.port,'GET',
                '/api/turn?id='+turn['id']+'&seq='+str(state['seq'])+'&have=0',headers)
            deadline = time.monotonic()+1
            while not any(f.done() for f in pending) and time.monotonic()<deadline: time.sleep(.01)
            finished = [f for f in pending if f.done()]
            self.assertEqual(len(finished),1)
            self.assertEqual(finished[0].result().json()['retry_ms'],250)
            self.wait_parked(4)
            self.assertIn(self.post('/api/turn/stop',{'turn':turn['id']},sessions[4][1]).status,(200,202))
            self.assertEqual(turnpoll.result(4).status,200)
            for _,token in sessions[:4]: self.post('/api/session/close',{},token)
            for future in pending: self.assertIn(future.result(4).status,(200,401))
        self.wait_parked(0)

    def test_supersede_and_unload(self):
        _, token = self.session(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.poll,token)
            self.wait_parked(1)
            second = pool.submit(self.poll,token)
            self.assertTrue(first.result(2).json()['superseded'])
            self.wait_parked(1)
            self.stub.close(3)
            result = second.result(3)
            self.assertEqual((result.status,result.code),(503,'turned_off'))
            self.assertEqual(result.json()['ended'],'turned_off')
        self.wait_parked(0)

    def test_poll_only_and_ended_snapshot_drop(self):
        lid, token = self.session(1)
        self.stub.poll_only = True
        self.assertEqual(self.poll(token).json()['retry_ms'],100)
        self.assertEqual(self.stub.parked,0)
        with self.stub.cv: session = next(iter(self.stub.sessions.values()))
        self.post('/api/session/close',{},token)
        self.assertIsNone(session.window)
        self.assertEqual(len(self.stub.sessions),0)


if __name__ == '__main__': unittest.main()
