# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Self-tests of the web_smoke harness (run: python3 -m unittest discover -s scripts/web_smoke -p selftest.py)."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract  # noqa: E402
import corpus  # noqa: E402
import httpraw  # noqa: E402


class ContractTests(unittest.TestCase):
    def test_section_3_3_vectors(self):
        self.assertEqual(contract.check_vectors(), [])

    def test_bootstrap_bytes(self):
        url = "http://127.0.0.1:8661/ui/index.html#launch=" + "A" * 43
        self.assertEqual(contract.bootstrap_html(url),
                         '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                         f'<meta http-equiv="refresh" content="0;url={url}"><title>Opening Sidecar…</title>')
        with self.assertRaises(ValueError):
            contract.bootstrap_html(url + '"><script>')

    def test_sha256sums_format(self):
        good = "a" * 64 + "  app.js\n" + "b" * 64 + "  index.html\n"
        self.assertEqual(contract.parse_sha256sums(good), {"app.js": "a" * 64, "index.html": "b" * 64})
        self.assertIsNone(contract.parse_sha256sums(good[:-1]))                        # no final newline
        self.assertIsNone(contract.parse_sha256sums("b" * 64 + "  z.js\n" + "a" * 64 + "  a.js\n"))   # unsorted
        self.assertIsNone(contract.parse_sha256sums("a" * 64 + " app.js\n"))           # one space
        self.assertIsNone(contract.parse_sha256sums("a" * 64 + "  ../x.js\n"))

    def test_rectangle_rules(self):
        self.assertTrue(contract.valid_rectangle({"x": -32768, "y": 32767, "width": 240, "height": 8192}))
        self.assertFalse(contract.valid_rectangle({"x": True, "y": 0, "width": 400, "height": 400}))
        self.assertFalse(contract.valid_rectangle({"x": 0, "y": 0, "width": 239, "height": 400}))

    def test_launch_url_rule(self):
        rule = contract.launch_url_re(8661)
        self.assertTrue(rule.fullmatch("http://127.0.0.1:8661/ui/index.html#launch=" + "A" * 43))
        self.assertFalse(rule.fullmatch("http://127.0.0.1:8662/ui/index.html#launch=" + "A" * 43))
        self.assertFalse(rule.fullmatch("http://localhost:8661/ui/index.html#launch=" + "A" * 43))


class CorpusTests(unittest.TestCase):
    def test_every_injection_case_is_marked(self):
        for name, source in corpus.INJECTION_CASES:
            self.assertTrue("INJ" in source or "__INJ" in source, name)

    def test_long_reply_length(self):
        self.assertEqual(len(corpus.long_reply(150000)), 150000)


class StubSmokeTests(unittest.TestCase):
    def test_launch_exchange_state_and_unload(self):
        import stub_server
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp)
            (ui / "index.html").write_text("<!doctype html><title>t</title>")
            digest = hashlib.sha256((ui / "index.html").read_bytes()).hexdigest()
            (ui / "SHA256SUMS").write_bytes(f"{digest}  index.html\n".encode("ascii"))
            stub = stub_server.Stub(ui)
            port = stub.start()
            try:
                _lid, url = httpraw.open_launch(port, stub.token, httpraw.doc(1))
                code = url.split("#launch=")[1]
                post = [("Origin", stub.origin), ("Content-Type", "application/json")]
                ex = httpraw.request(port, "POST", "/api/session", post, httpraw.jbody({"launch": code}))
                self.assertEqual(ex.status, 200)
                token = ex.json()["session"]
                st = httpraw.request(port, "GET", "/api/state", [("Sec-Fetch-Site", "same-origin"),
                                                                  ("X-Sidecar-Session", token)])
                self.assertEqual((st.status, st.json()["chat"]), (200, {"ready": True, "reason": None}))
                self.assertEqual(httpraw.request(port, "GET", "/ui/index.html", []).status, 200)
            finally:
                self.assertEqual(stub.close(5.0), 0)
            self.assertTrue(httpraw.port_refuses(port))


if __name__ == "__main__":
    unittest.main()
