# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Credentials of the Sidecar window (docs/web-protocol.md 2, 3, 4, 6.8): the R18 launcher
channel against the contract's test vectors, launch codes, sessions and the request gates.

Pure parts are tested on ``plugin.web_auth`` with a settable clock; the channel is also tested
end to end against a real ``ChatBridge`` listener.
"""
from __future__ import annotations

import email.message
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import (PAIRING_TOKEN, FakeClock, LauncherClient, WebCase, b64u,  # noqa: E402
                                   raw_request)

from plugin import chat_bridge, web_auth  # noqa: E402

VECTOR_TOKEN = "example-pairing-token-0123456789abcdefghijklmnopq"
VECTOR_CLIENT = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
VECTOR_SERVER = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"
VECTOR_BODY = (b'{"document":{"document_id":"lo:5f0c:1","title":"sales.ods","type":"calc"},"host_scheme":"light",'
               b'"mode":"app","bounds":{"x":1240,"y":102,"width":400,"height":848}}')
DOCUMENT = {"document_id": "lo:5f0c:1", "title": "sales.ods", "type": "calc"}


def headers(*pairs):
    message = email.message.Message()
    for name, value in pairs:
        message[name] = value
    return message


class VectorTests(unittest.TestCase):
    """Section 3.3, byte for byte."""

    def test_the_nonces_are_the_documented_bytes(self):
        self.assertEqual(b64u(bytes(range(32))), VECTOR_CLIENT)
        self.assertEqual(b64u(bytes(range(32, 64))), VECTOR_SERVER)

    def test_server_proof_and_channel_key(self):
        key = VECTOR_TOKEN.encode()
        self.assertEqual(web_auth.hello_proof(key, 8661, VECTOR_CLIENT, VECTOR_SERVER),
                         "6gAIEWKHsH5HilYgR4IFZrAzSBZsyxB9kUYyFBW3DZk")
        self.assertEqual(web_auth.channel_key(key, 8661, VECTOR_CLIENT, VECTOR_SERVER).hex(),
                         "49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1")

    def test_request_and_response_macs(self):
        channel = web_auth.channel_key(VECTOR_TOKEN.encode(), 8661, VECTOR_CLIENT, VECTOR_SERVER)
        self.assertEqual(hashlib.sha256(VECTOR_BODY).hexdigest(),
                         "3220ea4ed994980c359a102c7b2d2d604bb604de9885a619bedc17373995bbba")
        self.assertEqual(web_auth.request_mac(channel, "POST", "/launcher/launch", 1, VECTOR_BODY),
                         "PmCfNapRjDIVbyHqzSrGnkU_mqgxbEr4XLWu0iQocX8")
        self.assertEqual(hashlib.sha256(b'{"ok":true}').hexdigest(),
                         "4062edaf750fb8074e7e83e0c9028c94e32468a8b6f1614774328ef045150f93")
        self.assertEqual(web_auth.response_mac(channel, 1, 201, b'{"ok":true}'),
                         "ZgH8usVq9byBTZB7tp2Fi7PfEpJk8YBIw-EfdEN6FRc")
        self.assertEqual(hashlib.sha256(b"").hexdigest(),
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(web_auth.request_mac(channel, "GET", "/launcher/launch/AAAAAAAAAAAAAAAAAAAAAA", 2, b""),
                         "pz9hSDdfVxWMTMKc772QPcntSZE8IV0T3jHUv836CuI")

    def test_the_vectors_verify_through_a_channel(self):
        channels = web_auth.Channels(FakeClock())
        key = web_auth.channel_key(VECTOR_TOKEN.encode(), 8661, VECTOR_CLIENT, VECTOR_SERVER)
        channels._live[VECTOR_SERVER] = web_auth.Channel(VECTOR_SERVER, key, 10 ** 9)
        accepted = channels.verify([VECTOR_SERVER], ["1"], ["PmCfNapRjDIVbyHqzSrGnkU_mqgxbEr4XLWu0iQocX8"],
                                   "POST", "/launcher/launch", VECTOR_BODY)
        self.assertEqual(accepted, (key, 1))
        accepted = channels.verify([VECTOR_SERVER], ["2"], ["pz9hSDdfVxWMTMKc772QPcntSZE8IV0T3jHUv836CuI"],
                                   "GET", "/launcher/launch/AAAAAAAAAAAAAAAAAAAAAA", b"")
        self.assertEqual(accepted, (key, 2))


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.channels = web_auth.Channels(self.clock)
        self.key = PAIRING_TOKEN.encode()

    def open(self):
        nonce, proof = self.channels.open(self.key, 8661, VECTOR_CLIENT)
        self.assertEqual(proof, web_auth.hello_proof(self.key, 8661, VECTOR_CLIENT, nonce))
        return nonce, web_auth.channel_key(self.key, 8661, VECTOR_CLIENT, nonce)

    def call(self, nonce, key, n, body=b"{}", path="/launcher/release"):
        return self.channels.verify([nonce], [str(n)], [web_auth.request_mac(key, "POST", path, n, body)],
                                    "POST", path, body)

    def test_counter_must_be_exactly_next_and_any_failure_drops_the_channel(self):
        nonce, key = self.open()
        self.assertIsNotNone(self.call(nonce, key, 1))
        self.assertIsNone(self.call(nonce, key, 1), "a replayed counter is refused")
        self.assertIsNone(self.call(nonce, key, 2), "and the channel is gone")
        nonce, key = self.open()
        self.assertIsNone(self.call(nonce, key, 2), "skipping ahead is refused")
        self.assertEqual(len(self.channels), 0)

    def test_wrong_mac_body_path_or_format_is_refused_and_drops(self):
        cases = [
            lambda n, k: self.channels.verify([n], ["1"], [web_auth.request_mac(k, "POST", "/x", 1, b"{}")],
                                              "POST", "/launcher/release", b"{}"),
            lambda n, k: self.channels.verify([n], ["1"], [web_auth.request_mac(k, "POST", "/launcher/release", 1,
                                                                                b"{}")], "POST", "/launcher/release",
                                              b'{"a":1}'),
            lambda n, k: self.channels.verify([n], ["01"], [web_auth.request_mac(k, "POST", "/launcher/release", 1,
                                                                                 b"{}")], "POST", "/launcher/release",
                                              b"{}"),
            lambda n, k: self.channels.verify([n, n], ["1"], ["x"], "POST", "/launcher/release", b"{}"),
            lambda n, k: self.channels.verify([n], ["1", "1"], ["x"], "POST", "/launcher/release", b"{}"),
            lambda n, k: self.channels.verify([n], ["1"], None, "POST", "/launcher/release", b"{}"),
        ]
        for case in cases:
            nonce, key = self.open()
            self.assertIsNone(case(nonce, key))
            self.assertIsNone(self.call(nonce, key, 1), "the channel was dropped")

    def test_lifetime_call_limit_and_live_cap(self):
        nonce, key = self.open()
        self.clock.advance(web_auth.CHANNEL_SECONDS)
        self.assertIsNone(self.call(nonce, key, 1), "120 s from hello")
        nonce, key = self.open()
        for n in range(1, web_auth.CHANNEL_CALLS + 1):
            self.assertIsNotNone(self.call(nonce, key, n))
        self.assertIsNone(self.call(nonce, key, web_auth.CHANNEL_CALLS + 1), "at most 64 calls")
        first = self.open()
        opened = [self.open() for _ in range(web_auth.MAX_CHANNELS)]
        self.assertEqual(len(self.channels), web_auth.MAX_CHANNELS)
        self.assertIsNone(self.call(*first, 1), "the oldest unused channel is dropped for a new hello")
        self.assertIsNotNone(self.call(*opened[-1], 1))
        nonces = {nonce for nonce, _key in opened}
        self.assertEqual(len(nonces), len(opened), "a server nonce is never reissued")

    def test_a_hello_never_displaces_a_channel_its_launcher_has_used(self):
        used = self.open()
        self.assertIsNotNone(self.call(*used, 1))         # a MAC-verified call: the channel is in use
        for _ in range(10 * web_auth.MAX_CHANNELS):        # a flood of unauthenticated hellos
            self.open()
        self.assertEqual(len(self.channels), web_auth.MAX_CHANNELS + 1, "16 unused (the newest) and the used one")
        self.assertIsNotNone(self.call(*used, 2), "the used channel survives any number of hellos")

    def test_only_a_verified_call_displaces_a_used_channel_the_least_recently_called_first(self):
        used = [self.open() for _ in range(web_auth.MAX_CHANNELS)]
        for channel in used:
            self.assertIsNotNone(self.call(*channel, 1))
            self.clock.advance(0.1)
        self.assertIsNotNone(self.call(*used[0], 2), "the first is now the most recently called")
        for _ in range(3 * web_auth.MAX_CHANNELS):
            self.open()
        self.assertEqual(len(self.channels), 2 * web_auth.MAX_CHANNELS, "16 used, 16 unused: both pools are bounded")
        self.assertTrue(all(nonce in self.channels._live for nonce, _key in used), "hellos displaced no used channel")
        self.clock.advance(0.1)
        newcomer = self.open()
        self.assertIsNotNone(self.call(*newcomer, 1), "a 17th launcher's first verified call")
        self.assertIsNone(self.call(*used[1], 2), "displaced the least recently called used channel")
        self.assertIsNotNone(self.call(*used[2], 2))
        self.assertIsNotNone(self.call(*used[0], 3))
        self.assertEqual(sum(1 for c in self.channels._live.values() if c.calls), web_auth.MAX_CHANNELS)

class LaunchAndSessionTableTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.launches = web_auth.Launches(self.clock)
        self.sessions = web_auth.Sessions(self.clock)

    def test_codes_are_random_hashed_bound_and_expire_after_30_seconds(self):
        code, launch = self.launches.mint(DOCUMENT, "dark", "app", {"x": 0, "y": 0, "width": 400, "height": 800})
        self.assertRegex(code, r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(launch.launch_id, r"^[A-Za-z0-9_-]{22}$")
        stored = repr([vars(self.launches)]) + repr([getattr(launch, name) for name in launch.__slots__])
        self.assertNotIn(code, stored, "only the digest is stored")
        self.assertEqual(launch.code_digest, hashlib.sha256(code.encode()).digest())
        self.assertIs(self.launches.find_code(code), launch)
        self.assertEqual((launch.document, launch.host_scheme, launch.mode), (DOCUMENT, "dark", "app"))
        self.clock.advance(web_auth.CODE_SECONDS - 0.1)
        self.assertIs(self.launches.find_code(code), launch)
        self.clock.advance(0.1)
        self.assertIsNone(self.launches.find_code(code), "purged at 30 s")
        self.assertEqual(launch.state, "expired")
        self.assertIs(self.launches.get(launch.launch_id), launch, "status kept 120 s")
        self.clock.advance(web_auth.LAUNCH_RECORD_SECONDS)
        self.assertIsNone(self.launches.get(launch.launch_id))

    def test_outstanding_counts_only_pending_codes_and_release_purges_them(self):
        minted = [self.launches.mint(dict(DOCUMENT, document_id=f"d{i}"), None, "tab", None) for i in range(3)]
        self.assertEqual(self.launches.outstanding(), 3)
        minted[0][1].state = "exchanged"
        self.assertEqual(self.launches.outstanding(), 2)
        self.assertEqual(self.launches.revoke_pending("d1"), 1)
        self.assertIsNone(self.launches.find_code(minted[1][0]))
        self.assertEqual(self.launches.revoke_pending(None), 1)
        self.assertEqual(self.launches.outstanding(), 0)

    def test_sessions_are_hashed_ended_reasons_are_remembered_ten_minutes(self):
        _code, launch = self.launches.mint(DOCUMENT, None, "tab", None)
        token, session = self.sessions.mint(launch)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(session.chat_session_id, chat_bridge.SESSION_RE.pattern)
        self.assertNotIn(token, repr([getattr(session, name) for name in session.__slots__]))
        self.assertEqual(self.sessions.lookup([token]), (session, None))
        self.assertEqual(self.sessions.lookup([token, token]), (None, None), "duplicates are refused")
        self.assertEqual(self.sessions.lookup(["x" * 43]), (None, None))
        self.assertTrue(self.sessions.end(session, "closed"))
        self.assertFalse(self.sessions.end(session, "closed"))
        self.assertEqual(self.sessions.lookup([token]), (None, "closed"))
        self.clock.advance(web_auth.ENDED_SECONDS)
        self.assertEqual(self.sessions.lookup([token]), (None, None))
        with self.assertRaises(ValueError):
            self.sessions.end(session, "because")

    def test_ended_memory_is_capped(self):
        tokens = []
        for _ in range(web_auth.MAX_ENDED + 1):
            _code, launch = self.launches.mint(DOCUMENT, None, "tab", None)
            token, session = self.sessions.mint(launch)
            self.sessions.end(session, "replaced")
            tokens.append(token)
        self.assertEqual(self.sessions.lookup([tokens[0]]), (None, None))
        self.assertEqual(self.sessions.lookup([tokens[-1]]), (None, "replaced"))

    def test_eviction_picks_the_least_recently_seen_session_without_a_driven_run(self):
        made = []
        for index in range(3):
            _code, launch = self.launches.mint(dict(DOCUMENT, document_id=f"d{index}"), None, "tab", None)
            made.append(self.sessions.mint(launch)[1])
            self.clock.advance(1)
        made[1].last_seen = made[0].last_seen - 1
        self.assertIs(self.sessions.evictable(lambda session: False), made[1])
        self.assertIs(self.sessions.evictable(lambda session: session is made[1]), made[0])
        self.assertIsNone(self.sessions.evictable(lambda session: True))
        self.clock.advance(web_auth.SESSION_IDLE_SECONDS)
        self.assertEqual(len(self.sessions.idle()), 3)

    def test_chat_session_ids_use_the_panel_shape(self):
        ids = {web_auth.new_chat_session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(chat_bridge.SESSION_RE.fullmatch(value) for value in ids))


class GateTests(unittest.TestCase):
    ORIGIN = "http://127.0.0.1:8661"

    def test_host_must_be_exactly_one_loopback_host(self):
        self.assertTrue(web_auth.host_ok(headers(("Host", "127.0.0.1:8661")), "127.0.0.1:8661"))
        for bad in ([("Host", "localhost:8661")], [("Host", "attacker.example:8661")], [("Host", "127.0.0.1")],
                    [("Host", "127.0.0.1:8661"), ("Host", "127.0.0.1:8661")], []):
            self.assertFalse(web_auth.host_ok(headers(*bad), "127.0.0.1:8661"), bad)

    def test_role_separation_table(self):
        refuse = {
            "launcher": ["Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "X-Sidecar-Session", "X-Hermes-Plugin-Key"],
            "ui": ["X-Hermes-Plugin-Key", "X-Sidecar-Session", "X-Sidecar-Channel", "X-Sidecar-Counter",
                   "X-Sidecar-MAC"],
            "api": ["X-Hermes-Plugin-Key", "X-Sidecar-Channel", "X-Sidecar-Counter", "x-sidecar-mac"],
        }
        allow = {"launcher": ["X-Sidecar-Channel", "X-Sidecar-Counter", "X-Sidecar-MAC", "Content-Type"],
                 "ui": ["Sec-Fetch-Site", "Origin"], "api": ["X-Sidecar-Session", "Origin", "Sec-Fetch-Site"]}
        for family, names in refuse.items():
            for name in names:
                self.assertTrue(web_auth.carries_foreign_credential(headers((name, "x")), family), (family, name))
            for name in allow[family]:
                self.assertFalse(web_auth.carries_foreign_credential(headers((name, "x")), family), (family, name))

    def test_api_fetch_metadata(self):
        same = ("Sec-Fetch-Site", "same-origin")
        origin = ("Origin", self.ORIGIN)
        cases = [
            ("GET", [same], None), ("GET", [same, origin], None), ("GET", [], "bad_origin"),
            ("GET", [origin], None), ("GET", [("Origin", "null"), same], "bad_origin"),
            ("GET", [("Sec-Fetch-Site", "same-site")], "cross_site"), ("GET", [("Sec-Fetch-Site", "cross-site")], "cross_site"),
            ("GET", [("Sec-Fetch-Site", "none")], "cross_site"), ("GET", [same, same], "cross_site"),
            ("POST", [same, origin], None), ("POST", [origin], None), ("POST", [same], "bad_origin"),
            ("POST", [same, ("Origin", "http://127.0.0.1:9999")], "bad_origin"),
            ("POST", [same, ("Origin", "http://localhost:8661")], "bad_origin"),
            ("POST", [same, ("Origin", "null")], "bad_origin"), ("POST", [same, origin, origin], "bad_origin"),
            ("POST", [("Sec-Fetch-Site", "cross-site"), origin], "cross_site"),
        ]
        for method, pairs, expected in cases:
            self.assertEqual(web_auth.api_fetch_metadata(headers(*pairs), method, self.ORIGIN), expected, (method, pairs))

    def test_static_fetch_metadata(self):
        navigate = [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]
        self.assertTrue(web_auth.static_fetch_allowed(headers(*navigate), True))
        self.assertTrue(web_auth.static_fetch_allowed(headers(), True))
        self.assertFalse(web_auth.static_fetch_allowed(headers(*navigate), False))
        self.assertFalse(web_auth.static_fetch_allowed(
            headers(("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "script")), True))
        self.assertFalse(web_auth.static_fetch_allowed(
            headers(("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "iframe")), True))
        self.assertTrue(web_auth.static_fetch_allowed(headers(("Sec-Fetch-Site", "same-origin")), False))
        self.assertTrue(web_auth.static_fetch_allowed(headers(), False))
        self.assertFalse(web_auth.static_fetch_allowed(headers(("Sec-Fetch-Site", "same-site")), False))

    def test_body_shapes(self):
        self.assertTrue(web_auth.json_content_type(headers(("Content-Type", "application/json"))))
        self.assertTrue(web_auth.json_content_type(headers(("Content-Type", "Application/JSON; charset=UTF-8"))))
        for bad in ("text/plain", "application/json; boundary=x", "application/jsonx", "multipart/form-data"):
            self.assertFalse(web_auth.json_content_type(headers(("Content-Type", bad))), bad)
        self.assertEqual(web_auth.parse_object(b'{"a":1}', ("a",)), {"a": 1})
        for raw in (b'{"a":1,"a":2}', b'{"a":1,"b":2}', b"[1]", b"{}", b"\xff", b'{"a":'):
            self.assertIsNone(web_auth.parse_object(raw, ("a",)), raw)

    def test_documents_and_rectangles(self):
        self.assertTrue(web_auth.valid_document(DOCUMENT))
        for bad in (dict(DOCUMENT, type="base"), dict(DOCUMENT, document_id=""), dict(DOCUMENT, extra="x"),
                    dict(DOCUMENT, title="t" * 1001), {"document_id": "a", "title": "t"}, None):
            self.assertFalse(web_auth.valid_document(bad), bad)
        good = {"x": -32768, "y": 32767, "width": 240, "height": 8192}
        self.assertTrue(web_auth.valid_rectangle(good))
        for bad in (dict(good, x=True), dict(good, width=239), dict(good, height=8193), dict(good, y=1.5),
                    dict(good, x=-32769), {"x": 0, "y": 0, "width": 400}):
            self.assertFalse(web_auth.valid_rectangle(bad), bad)


class LauncherChannelEndToEnd(WebCase):
    """Section 3 through the real listener: the pairing token never travels."""

    def test_hello_proves_the_token_and_calls_carry_and_get_macs(self):
        client = LauncherClient(self.port)
        status, value = client.hello()
        self.assertEqual(status, 200)
        self.assertEqual(set(value), {"ok", "v", "server_nonce", "server_proof", "expires_in", "chat", "caps"})
        self.assertEqual((value["v"], value["expires_in"], value["chat"]), (1, 120, {"ready": True, "reason": None}))
        status, answer, mac = client.call("GET", "/launcher/launch/" + "A" * 22)
        self.assertEqual((status, answer["code"]), (404, "not_found"))
        self.assertIsNotNone(mac, "an accepted call's refusal still carries the response MAC")
        status, answer, _mac = client.call("POST", "/launcher/release", {"document_id": None, "reason": "nope"})
        self.assertEqual((status, answer["code"]), (400, "bad_request"))
        status, answer, _mac = client.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"})
        self.assertEqual((status, answer), (200, {"ok": True, "released": 0}))

    def test_refused_calls_answer_401_without_a_mac_and_drop_the_channel(self):
        client = LauncherClient(self.port)
        client.hello()
        self.assertEqual(client.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"})[0], 200)
        status, answer, mac = client.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"},
                                          counter=1)
        self.assertEqual((status, answer["code"], mac), (401, "launcher_auth", None))
        status, _answer, _mac = client.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"},
                                            counter=2)
        self.assertEqual(status, 401, "the channel is gone after a replay")
        client.hello()
        self.assertEqual(client.call("POST", "/launcher/launch", {}, tamper=True)[0], 401)
        self.assertEqual(client.call("POST", "/launcher/launch", {}, counter=2)[0], 401)
        status, _headers, raw = raw_request(self.port, "GET", "/launcher/launch/" + "A" * 22,
                                            headers=[("Host", f"127.0.0.1:{self.port}")])
        self.assertEqual((status, json.loads(raw)["code"]), (401, "launcher_auth"))

    def test_without_a_pairing_token_hello_answers_no_pairing(self):
        self.bridge.token = lambda: None
        status, value = LauncherClient(self.port).hello()
        self.assertEqual((status, value["code"]), (503, "no_pairing"))

    def test_a_wrong_token_cannot_verify_the_proof(self):
        with self.assertRaises(AssertionError):
            LauncherClient(self.port, token="someone-else-" + "z" * 30).hello()

    def test_bad_hello_bodies(self):
        host = [("Host", f"127.0.0.1:{self.port}"), ("Content-Type", "application/json")]
        for body in ({"v": 2, "client_nonce": VECTOR_CLIENT}, {"v": True, "client_nonce": VECTOR_CLIENT},
                     {"v": 1, "client_nonce": "short"}, {"v": 1}, {"v": 1, "client_nonce": VECTOR_CLIENT, "x": 1}):
            status, _headers, raw = raw_request(self.port, "POST", "/launcher/hello", json.dumps(body).encode(), host)
            self.assertEqual((status, json.loads(raw)["code"]), (400, "bad_request"), body)

    def test_launcher_routes_refuse_browser_and_foreign_credentials(self):
        client = LauncherClient(self.port)
        client.hello()
        for extra in ([("Origin", f"http://127.0.0.1:{self.port}")], [("Sec-Fetch-Site", "same-origin")],
                      [("X-Sidecar-Session", "s" * 43)], [(chat_bridge.TOKEN_HEADER, PAIRING_TOKEN)]):
            status, answer, _mac = client.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"},
                                               extra=extra, check=False)
            self.assertEqual((status, answer["code"]), (403, "forbidden_credential"), extra)

    def test_the_launcher_never_needs_the_bearer_token(self):
        client = LauncherClient(self.port)
        client.hello()
        status, value, _mac = client.launch()
        self.assertEqual(status, 201)
        self.assertRegex(value["url"], rf"^http://127\.0\.0\.1:{self.port}/ui/index\.html#launch=[A-Za-z0-9_-]{{43}}$")
        status, state, _mac = client.call("GET", f"/launcher/launch/{value['launch_id']}")
        self.assertEqual((status, state), (200, {"ok": True, "state": "pending"}))


if __name__ == "__main__":
    unittest.main()
