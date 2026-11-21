# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Sidecar window routes through a real ``ChatBridge`` listener (docs/web-protocol.md 1, 4,
6, 9, 10, 11): the gate matrix of section 1.2 in both directions, launch codes and sessions over
their whole life, error shapes, headers and log labels. Section G1 of the 0.5.0 plan."""
from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import (PAIRING_TOKEN, LauncherClient, PageClient, WebCase, code_of,  # noqa: E402
                                   eventually, raw_request)

from plugin import chat_bridge, web_api, web_auth  # noqa: E402

API_HEADER_SET = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin", "Referrer-Policy": "no-referrer",
}


class GateMatrixTests(WebCase):
    def setUp(self):
        super().setUp()
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.page_client = self.page()

    def send(self, method, path, body=None, headers=None):
        status, answer_headers, raw = raw_request(self.port, method, path, body, headers)
        return status, dict(answer_headers), (json.loads(raw) if raw else None)

    def browser(self, method="GET", session=True, **override):
        base = {"Host": self.host, "Sec-Fetch-Site": "same-origin"}
        if method == "POST":
            base.update({"Origin": self.origin, "Content-Type": "application/json"})
        if session:
            base["X-Sidecar-Session"] = self.page_client.session
        base.update(override)
        return [(name, value) for name, value in base.items() if value is not None]

    def test_host_variants_are_refused_on_every_family(self):
        bad_hosts = [[("Host", f"localhost:{self.port}")], [("Host", f"attacker.example:{self.port}")],
                     [("Host", "127.0.0.1")], [("Host", self.host), ("Host", self.host)], []]
        for path in ("/api/state", "/ui/index.html", "/launcher/hello", "/favicon.ico", "/"):
            for hosts in bad_hosts:
                headers = [pair for pair in self.browser() if pair[0] != "Host"] + hosts
                status, _headers, value = self.send("GET", path, headers=headers)
                self.assertEqual((status, value["code"]), (403, "bad_host"), (path, hosts))

    def test_origin_and_fetch_metadata_on_api(self):
        body = b"{}"
        cases = [
            ("POST", {"Origin": None}, 403, "bad_origin"),
            ("POST", {"Origin": "http://evil.example"}, 403, "bad_origin"),
            ("POST", {"Origin": "null"}, 403, "bad_origin"),
            ("POST", {"Origin": f"http://localhost:{self.port}"}, 403, "bad_origin"),
            ("POST", {"Sec-Fetch-Site": "same-site"}, 403, "cross_site"),
            ("POST", {"Sec-Fetch-Site": "cross-site"}, 403, "cross_site"),
            ("GET", {"Sec-Fetch-Site": None}, 403, "bad_origin"),
            ("GET", {"Sec-Fetch-Site": "cross-site"}, 403, "cross_site"),
            ("GET", {"Sec-Fetch-Site": "none"}, 403, "cross_site"),
            ("GET", {"Origin": "http://evil.example"}, 403, "bad_origin"),
        ]
        for method, override, status, code in cases:
            path = "/api/new-chat" if method == "POST" else "/api/state"
            got, _headers, value = self.send(method, path, body if method == "POST" else None,
                                            self.browser(method, **override))
            self.assertEqual((got, value["code"]), (status, code), (method, override))
        duplicate = self.browser("POST") + [("Origin", self.origin)]
        self.assertEqual(self.send("POST", "/api/new-chat", body, duplicate)[0], 403)
        self.assertEqual(self.send("GET", "/api/state", headers=self.browser())[0], 200)
        self.assertEqual(self.send("GET", "/api/state", headers=self.browser(**{"Sec-Fetch-Site": None,
                                                                              "Origin": self.origin}))[0], 200)

    def test_session_travels_only_in_its_header(self):
        cookie = self.browser(session=False) + [("Cookie", f"session={self.page_client.session}")]
        status, _headers, value = self.send("GET", "/api/state", headers=cookie)
        self.assertEqual((status, value["code"], value["ended"]), (401, "no_session", None))
        doubled = self.browser() + [("X-Sidecar-Session", self.page_client.session)]
        self.assertEqual(self.send("GET", "/api/state", headers=doubled)[0], 401)
        bad = self.browser(**{"X-Sidecar-Session": "A" * 43})
        self.assertEqual(self.send("GET", "/api/state", headers=bad)[2]["ended"], None)

    def test_no_route_sets_a_cookie_and_api_answers_carry_the_fixed_headers(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        answers = [raw_request(self.port, "GET", "/api/state", headers=self.browser()),
                   raw_request(self.port, "GET", "/ui/index.html", headers=[("Host", self.host)]),
                   raw_request(self.port, "GET", "/api/nothing", headers=self.browser()),
                   raw_request(self.port, "GET", "/health", headers=[("Host", self.host),
                                                                     (chat_bridge.TOKEN_HEADER, PAIRING_TOKEN)])]
        for status, headers, _raw in answers:
            names = {name.lower() for name, _value in headers}
            self.assertNotIn("set-cookie", names, status)
            self.assertNotIn("access-control-allow-origin", names)
        for status, headers, _raw in answers[:1] + answers[2:3]:
            headers = dict(headers)
            for name, value in API_HEADER_SET.items():
                self.assertEqual(headers.get(name), value, name)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(headers["Connection"], "close")

    def test_other_methods_answer_json_405_with_allow_and_no_cors(self):
        for method, path, allow in (("OPTIONS", "/api/state", "GET"), ("HEAD", "/api/state", "GET"),
                                    ("PUT", "/api/chat", "POST"), ("DELETE", "/api/session", "POST"),
                                    ("GET", "/api/chat", "POST"), ("OPTIONS", "/launcher/launch", "POST"),
                                    ("POST", "/ui/index.html", "GET"), ("PATCH", "/launcher/hello", "POST")):
            headers = [("Host", self.host), ("Origin", "http://evil.example"),
                       ("Access-Control-Request-Method", "POST")]
            status, answer_headers, raw = raw_request(self.port, method, path, None, headers)
            answer_headers = dict(answer_headers)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(answer_headers["Allow"], allow)
            self.assertFalse(any(name.lower().startswith("access-control") for name in answer_headers))
            if method != "HEAD":
                self.assertEqual(json.loads(raw)["code"], "method_not_allowed")

    def test_unknown_paths_and_query_strings(self):
        for path in ("/api", "/api/", "/api/state?x=1", "/api/sessions", "/launcher", "/launcher/launch?x",
                     "/favicon.ico", "/apple-touch-icon.png", "/"):
            status, _headers, value = self.send("GET", path, headers=self.browser())
            self.assertEqual((status, value["code"]), (404, "not_found"), path)

    def test_api_refuses_pairing_token_and_launcher_headers(self):
        for name, value in ((chat_bridge.TOKEN_HEADER, PAIRING_TOKEN), ("X-Sidecar-Channel", "c" * 43),
                            ("X-Sidecar-Counter", "1"), ("X-Sidecar-MAC", "m" * 43)):
            status, _headers, answer = self.send("GET", "/api/state", headers=self.browser() + [(name, value)])
            self.assertEqual((status, answer["code"]), (403, "forbidden_credential"), name)

    def test_legacy_routes_keep_working_and_refuse_window_credentials(self):
        token = [("Host", self.host), (chat_bridge.TOKEN_HEADER, PAIRING_TOKEN)]
        self.assertEqual(self.send("GET", "/health", headers=token)[0], 200)
        for extra in ([("X-Sidecar-Session", self.page_client.session)], [("Sec-Fetch-Site", "same-origin")],
                      [("Sec-Fetch-Mode", "navigate")], [("X-Sidecar-Channel", "c")], [("X-Sidecar-Counter", "1")],
                      [("X-Sidecar-MAC", "m")], [("Host", self.host)]):
            status, _headers, value = self.send("GET", "/health", headers=token + extra)
            self.assertEqual((status, value["ok"]), (403, False), extra)
        status, _headers, value = self.send("GET", "/health", headers=token + [("Origin", self.origin)])
        self.assertEqual((status, value["error"]), (403, "requests with an Origin are refused"))
        self.api.finish.set()
        body = json.dumps({"message": "hi", "session_id": "lo-" + "c" * 32}).encode()
        status, _headers, value = self.send("POST", "/chat", body, token + [("Content-Type", "application/json")])
        self.assertEqual((status, value["ok"], value["reply"]), (200, True, self.api.reply))
        self.assertEqual(self.send("GET", "/v1/elsewhere", headers=token)[0], 404, "0.4.0's unknown route")

    def test_body_gates(self):
        path = "/api/new-chat"
        cases = [
            (self.browser("POST", **{"Content-Type": "text/plain"}), b"{}", 415, "unsupported_media_type"),
            (self.browser("POST", **{"Content-Type": "application/x-www-form-urlencoded"}), b"{}", 415,
             "unsupported_media_type"),
            (self.browser("POST") + [("Content-Length", "+2")], b"{}", 411, "length_required"),
            (self.browser("POST"), b'{"extra":1}', 400, "bad_request"),
            (self.browser("POST"), b"[]", 400, "bad_request"),
            (self.browser("POST"), b"x" * (web_api.MAX_POST + 1), 413, "too_large"),
            (self.browser("POST") + [("Content-Length", "0")], b"", 413, "too_large"),
        ]
        for headers, body, status, code in cases:
            got, _headers, value = self.send("POST", path, body, headers)
            self.assertEqual((got, value["code"]), (status, code), (headers[-1], body[:20]))
        status, _headers, value = self.send("POST", "/api/session", b'{"launch":"x","launch":"y"}',
                                           self.browser("POST", session=False))
        self.assertEqual((status, value["code"]), (400, "bad_request"))
        status, _headers, value = self.send("GET", "/api/state", b"{}", self.browser())
        self.assertEqual((status, value["code"]), (400, "bad_request"), "GET requests carry no body")


class LaunchLifecycleTests(WebCase):
    fake_clock = True

    def launch(self, document_id="lo:5f0c:1", mode="app", launcher=None):
        launcher = launcher or LauncherClient(self.port)
        if launcher.channel is None:
            launcher.hello()
        status, value, _mac = launcher.launch({"document_id": document_id, "title": "t.ods", "type": "calc"},
                                              mode=mode)
        return status, value, launcher

    def status(self, launcher, launch_id):
        return launcher.call("GET", f"/launcher/launch/{launch_id}")[1]["state"]

    def test_unauthenticated_hellos_never_end_a_channel_in_use(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        self.assertEqual(launcher.call("GET", "/launcher/launch/" + "A" * 22)[0], 404)   # MAC-verified: in use
        flood = [LauncherClient(self.port, token="not-the-pairing-token-" + "x" * 30) for _ in range(40)]
        for other in flood:
            try:
                other.hello()   # answered, though the proof cannot verify for a caller without the token
            except AssertionError:
                pass
        self.assertEqual(len(self.web.channels), web_auth.MAX_CHANNELS + 1)
        status, _value, answer_mac = launcher.call("GET", "/launcher/launch/" + "A" * 22)
        self.assertEqual(status, 404, "the launcher's channel survived the flood")
        self.assertIsNotNone(answer_mac)

    def test_a_non_ascii_launcher_path_is_an_unsigned_404_and_leaves_the_channel_alone(self):
        # critic N6 / critic2 N4: the MAC covers the path as ASCII; such a path used to reach the
        # MAC computation and answer 500 with an error log line.
        import socket
        launcher = LauncherClient(self.port)
        launcher.hello()
        head = ("GET /launcher/launch/caf\xe9 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Sidecar-Channel: %s\r\n"
                "X-Sidecar-Counter: 1\r\nX-Sidecar-MAC: %s\r\nConnection: close\r\n\r\n"
                % (self.port, launcher.channel, "A" * 43)).encode("latin-1")
        errors = []
        handler = logging.Handler(logging.ERROR)
        handler.emit = errors.append
        logging.getLogger("plugin").addHandler(handler)
        self.addCleanup(logging.getLogger("plugin").removeHandler, handler)
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as conn:
            conn.sendall(head)
            raw = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
        status_line, _sep, rest = raw.partition(b"\r\n")
        headers, _sep, body = rest.partition(b"\r\n\r\n")
        self.assertIn(b" 404 ", status_line + b" ")
        self.assertEqual(json.loads(body)["code"], "not_found")
        self.assertNotIn(b"x-sidecar-mac", headers.lower(), "refused before any MAC")
        status, _value, answer_mac = launcher.call("GET", "/launcher/launch/" + "A" * 22)
        self.assertEqual(status, 404, "the channel was not dropped and its counter did not move")
        self.assertEqual(errors, [], "no error log line")
        self.assertIsNotNone(answer_mac)

    def test_a_malformed_launch_id_gets_a_signed_404_like_an_unknown_one(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        for launch_id in ("short", "", "A" * 23, "A" * 21 + "%", "a/" + "A" * 20, "B" * 22):
            status, value, answer_mac = launcher.call("GET", f"/launcher/launch/{launch_id}")   # checks the MAC
            self.assertEqual((status, value["code"]), (404, "not_found"), launch_id)
            self.assertIsNotNone(answer_mac, launch_id)

    def test_a_code_is_single_use_bound_to_its_launch_and_expires(self):
        status, value, launcher = self.launch(mode="tab")
        self.assertEqual((status, value["window_live"], value["expires_in"]), (201, False, 30))
        page = PageClient(self.port)
        status, session = page.exchange(code_of(value["url"]))
        self.assertEqual(status, 200)
        self.assertEqual((session["document"]["document_id"], session["mode"], session["bounds"], session["host_scheme"]),
                         ("lo:5f0c:1", "tab", None, "light"))
        self.assertEqual(session["idle_timeout"], 43200)
        self.assertEqual(session["endpoint"], f"http://127.0.0.1:{self.port}")
        self.assertEqual(session["limits"], {"max_message": chat_bridge.MAX_MESSAGE})
        self.assertRegex(session["chat_label"], r"^lo-[0-9a-f]{8}$")
        self.assertEqual(self.status(launcher, value["launch_id"]), "exchanged")
        status, value, launcher = self.launch(document_id="other")
        self.clock.advance(web_auth.CODE_SECONDS)
        status, refused = PageClient(self.port).exchange(code_of(value["url"]))
        self.assertEqual((status, refused["code"]), (401, "launch_expired"))
        self.assertNotIn("ended", refused)
        self.assertEqual(self.status(launcher, value["launch_id"]), "expired")
        self.clock.advance(web_auth.LAUNCH_RECORD_SECONDS)
        launcher.hello()   # the first channel lived 120 s too
        self.assertEqual(launcher.call("GET", f"/launcher/launch/{value['launch_id']}")[0], 404)

    def test_a_second_presentation_revokes_the_session_and_stops_its_run(self):
        status, value, launcher = self.launch()
        code = code_of(value["url"])
        page = PageClient(self.port)
        page.exchange(code)
        turn = self.start_turn(page)
        self.assertTrue(self.api.wait_for(lambda: self.api.polls()))
        with self.assertLogs(web_api.log, level=logging.WARNING) as logs:
            status, refused = PageClient(self.port).exchange(code)
        self.assertEqual((status, refused["code"]), (401, "launch_double_use"))
        self.assertNotIn(code, "\n".join(logs.output))
        status, value_after = page.state()
        self.assertEqual((status, value_after["code"], value_after["ended"]), (401, "no_session", "double_use"))
        self.assertTrue(self.api.wait_for(lambda: self.api.stops), "the revoked session's run is stopped")
        self.assertEqual(self.status(launcher, value["launch_id"]), "double_use")
        self.assertTrue(eventually(lambda: self.bridge.owned_runs() == 0), "the stopped run is released")
        self.assertIsNotNone(turn)
        self.clock.advance(web_auth.CODE_SECONDS)   # record purged: a history replay is just expired
        status, replay = PageClient(self.port).exchange(code)
        self.assertEqual((status, replay["code"]), (401, "launch_expired"))

    def test_a_replay_after_the_record_is_purged_never_disconnects_a_live_window(self):
        _status, value, _launcher = self.launch()
        page = PageClient(self.port)
        page.exchange(code_of(value["url"]))
        self.clock.advance(web_auth.CODE_SECONDS)
        self.assertEqual(PageClient(self.port).exchange(code_of(value["url"]))[1]["code"], "launch_expired")
        self.assertEqual(page.state()[0], 200)

    def test_window_live_answers_without_a_code(self):
        _status, value, launcher = self.launch()
        PageClient(self.port).exchange(code_of(value["url"]))
        status, live, _mac = launcher.launch({"document_id": "lo:5f0c:1", "title": "t.ods", "type": "calc"})
        self.assertEqual((status, live), (200, {"ok": True, "launch_id": None, "url": None, "window_live": True,
                                                "expires_in": 0}))
        self.clock.advance(web_auth.WINDOW_LIVE_SECONDS)
        self.assertEqual(launcher.launch({"document_id": "lo:5f0c:1", "title": "t.ods", "type": "calc"})[0], 201)

    def test_a_newer_session_replaces_the_older_one(self):
        _status, value, launcher = self.launch()
        first = PageClient(self.port)
        first.exchange(code_of(value["url"]))
        self.clock.advance(web_auth.WINDOW_LIVE_SECONDS)
        status, value, _mac = launcher.launch({"document_id": "lo:5f0c:1", "title": "t.ods", "type": "calc"})
        second = PageClient(self.port)
        self.assertEqual(second.exchange(code_of(value["url"]))[0], 200)
        self.assertEqual(first.state()[1]["ended"], "replaced")
        self.assertEqual(second.state()[0], 200)

    def test_outstanding_codes_are_capped(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        for index in range(web_auth.MAX_CODES):
            self.assertEqual(self.launch(document_id=f"d{index}", launcher=launcher)[0], 201)
        status, value, _launcher = self.launch(document_id="one-more", launcher=launcher)
        self.assertEqual((status, value["code"]), (429, "too_many_launches"))
        self.clock.advance(web_auth.CODE_SECONDS)
        self.assertEqual(self.launch(document_id="later", launcher=launcher)[0], 201, "purged at expiry")

    def test_session_cap_evicts_the_least_recently_seen_idle_session_else_refuses(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        pages = []
        for index in range(web_auth.MAX_SESSIONS):
            _status, value, _launcher = self.launch(document_id=f"d{index}", launcher=launcher)
            page = PageClient(self.port)
            page.exchange(code_of(value["url"]))
            pages.append(page)
            self.clock.advance(1)
        pages[0].state()   # d1 is now the least recently seen
        status, value, _launcher = self.launch(document_id="ninth", launcher=launcher)
        self.assertEqual(status, 201)
        self.assertEqual(pages[1].state()[1]["ended"], "evicted")
        ninth = PageClient(self.port)
        self.assertEqual(ninth.exchange(code_of(value["url"]))[0], 200)
        live = [page for page in pages if page.state()[0] == 200] + [ninth]
        self.assertEqual(len(live), web_auth.MAX_SESSIONS)
        driven = mock_driven(self.web, True)
        try:
            status, value, _launcher = self.launch(document_id="tenth", launcher=launcher)
        finally:
            driven()
        self.assertEqual((status, value["code"]), (429, "session_cap"))

    def test_release_ends_sessions_for_a_document_or_all_and_purges_their_codes(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        pages = {}
        for document_id in ("a", "b", "c"):
            _status, value, _launcher = self.launch(document_id=document_id, launcher=launcher)
            pages[document_id] = PageClient(self.port)
            pages[document_id].exchange(code_of(value["url"]))
        self.clock.advance(web_auth.WINDOW_LIVE_SECONDS)
        _status, pending, _launcher = self.launch(document_id="a", launcher=launcher)
        status, value, _mac = launcher.call("POST", "/launcher/release", {"document_id": "a", "reason": "document_closed"})
        self.assertEqual((status, value), (200, {"ok": True, "released": 1}))
        self.assertEqual(pages["a"].state()[1]["ended"], "host_gone")
        self.assertEqual(PageClient(self.port).exchange(code_of(pending["url"]))[1]["code"], "launch_expired")
        self.assertEqual(pages["b"].state()[0], 200)
        status, value, _mac = launcher.call("POST", "/launcher/release", {"document_id": None, "reason": "host_quit"})
        self.assertEqual(value["released"], 2)
        self.assertEqual({pages[d].state()[1]["ended"] for d in ("b", "c")}, {"host_gone"})

    def test_close_and_idle_end_sessions_and_the_reason_is_kept_ten_minutes(self):
        _status, value, launcher = self.launch(document_id="x")
        page = PageClient(self.port)
        page.exchange(code_of(value["url"]))
        self.assertEqual(page.close(), (200, {"ok": True}))
        self.assertEqual(page.state()[1]["ended"], "closed")
        self.clock.advance(web_auth.ENDED_SECONDS)
        self.assertIsNone(page.state()[1]["ended"])
        _status, value, _launcher = self.launch(document_id="y")
        idle = PageClient(self.port)
        idle.exchange(code_of(value["url"]))
        self.clock.advance(web_auth.SESSION_IDLE_SECONDS)
        self.web.sweep()
        self.assertEqual(idle.state()[1]["ended"], "idle")

    def test_launch_bodies_are_strict(self):
        launcher = LauncherClient(self.port)
        launcher.hello()
        document = {"document_id": "d", "title": "t", "type": "calc"}
        bounds = {"x": 0, "y": 0, "width": 400, "height": 800}
        for body in ({"document": document, "host_scheme": "light", "mode": "tab", "bounds": bounds},
                     {"document": document, "host_scheme": "light", "mode": "app", "bounds": None},
                     {"document": document, "host_scheme": "blue", "mode": "tab", "bounds": None},
                     {"document": document, "host_scheme": None, "mode": "window", "bounds": None},
                     {"document": dict(document, type="base"), "host_scheme": None, "mode": "tab", "bounds": None},
                     {"document": document, "host_scheme": None, "mode": "app",
                      "bounds": dict(bounds, width=True)},
                     {"document": document, "mode": "tab", "bounds": None}):
            status, value, _mac = launcher.call("POST", "/launcher/launch", body)
            self.assertEqual((status, value["code"]), (400, "bad_request"), body)


def mock_driven(web, value):
    original = web._driven
    web._driven = lambda session: value
    return lambda: setattr(web, "_driven", original)


class LogLabelTests(WebCase):
    def test_logs_carry_fixed_labels_and_no_path_id_title_message_or_secret(self):
        bridge_log = logging.getLogger(chat_bridge.__name__)
        with self.assertLogs(bridge_log, level=logging.DEBUG) as captured:
            launcher = LauncherClient(self.port)
            launcher.hello()
            status, launch, _mac = launcher.launch({"document_id": "lo:secretdoc:1", "title": "Quarterly-secret.ods",
                                                    "type": "calc"})
            page = PageClient(self.port)
            page.exchange(code_of(launch["url"]))
            turn = self.start_turn(page, "please total the hidden-message column")
            self.api.finish.set()
            page.wait_done(turn)
            page.new_chat()
            page.stop(turn)
            raw_request(self.port, "GET", "/ui/index.html", headers=[("Host", f"127.0.0.1:{self.port}")])
            raw_request(self.port, "OPTIONS", "/api/state", headers=[("Host", f"127.0.0.1:{self.port}")])
            page.close()
            eventually(lambda: any(" api-close -> " in line for line in captured.output))
        text = "\n".join(captured.output)
        for label in ("launcher-hello", "launcher-launch", "api-session", "api-chat", "api-turn", "api-new-chat",
                      "api-stop", "ui-static", "method", "api-close"):
            self.assertIn(f" {label} -> ", text, label)
        for secret in (PAIRING_TOKEN, page.session, code_of(launch["url"]), launch["launch_id"], turn,
                       "secretdoc", "Quarterly", "hidden-message", "/ui/index.html", "/api/", "/launcher/",
                       launcher.channel):
            self.assertNotIn(secret, text)

    def test_state_polls_and_static_files_are_logged_at_debug_refusals_at_info(self):
        bridge_log = logging.getLogger(chat_bridge.__name__)
        page = self.page()
        with self.assertLogs(bridge_log, level=logging.DEBUG) as captured:
            for _ in range(3):   # an idle window polls its state every 3 s
                self.assertEqual(page.state()[0], 200)
            raw_request(self.port, "GET", "/ui/index.html", headers=[("Host", f"127.0.0.1:{self.port}")])
            raw_request(self.port, "GET", "/ui/missing.js", headers=[("Host", f"127.0.0.1:{self.port}")])
            raw_request(self.port, "GET", "/api/state", headers=[(("Host", f"127.0.0.1:{self.port}"))])
            eventually(lambda: sum(" -> " in line for line in captured.output) >= 6)
        lines = [line.split(":", 2) for line in captured.output]
        levels = {}
        for level, _name, message in lines:
            if " -> " not in message:
                continue
            label_status = message.split(" ", 3)[3].rsplit(" in ", 1)[0]
            levels.setdefault(label_status, set()).add(level)
        self.assertEqual(levels.get("api-state -> 200"), {"DEBUG"})
        self.assertEqual(levels.get("ui-static -> 200"), {"DEBUG"})
        self.assertEqual(levels.get("ui-static -> 404"), {"INFO"}, "a refusal stays at INFO")
        self.assertEqual(levels.get("api-state -> 403"), {"INFO"}, "a refused poll stays at INFO")
        status, value, _mac = self.launcher().launch({"document_id": "other", "title": "t.ods", "type": "calc"})
        with self.assertLogs(bridge_log, level=logging.INFO) as captured:
            PageClient(self.port).exchange(code_of(value["url"]))
            eventually(lambda: any(" api-session -> " in line for line in captured.output))
        self.assertTrue(any(line.startswith("INFO:") and " api-session -> 200" in line for line in captured.output),
                        "other answers stay at INFO")


if __name__ == "__main__":
    unittest.main()
