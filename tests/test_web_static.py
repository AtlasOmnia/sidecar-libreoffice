# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Static files of the Sidecar window (docs/web-protocol.md 7): only files listed in
``SHA256SUMS`` and still matching it, by exact path, with the fixed header set. The real bundle
is lane B's ``plugin/sidebar/``; these tests serve a fixture tree."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.fake_runs_api import FIXTURE_FILES, WebCase, raw_request, write_sums  # noqa: E402

from plugin import chat_bridge, web_api  # noqa: E402

STATIC_HEADER_SET = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
                                "form-action 'none'; worker-src 'none'; manifest-src 'none'; "
                                "require-trusted-types-for 'script'; trusted-types dompurify"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": ("accelerometer=(), camera=(), display-capture=(), fullscreen=(), geolocation=(), "
                           "gyroscope=(), hid=(), magnetometer=(), microphone=(), midi=(), payment=(), serial=(), "
                           "usb=(), clipboard-read=(), clipboard-write=(self)"),
    "Cache-Control": "no-cache",
}
SAME = [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "script")]
NAVIGATE = [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]


class StaticTests(WebCase):
    def get(self, path, extra=(), method="GET"):
        status, headers, raw = raw_request(self.port, method, path, None,
                                           [("Host", f"127.0.0.1:{self.port}")] + list(extra))
        return status, dict(headers), raw

    def code(self, path, extra=()):
        status, _headers, raw = self.get(path, extra)
        return status, json.loads(raw)["code"]

    def test_listed_files_are_served_with_their_type_and_the_exact_headers(self):
        for relpath, content_type in (("index.html", "text/html; charset=utf-8"),
                                      ("app.js", "text/javascript; charset=utf-8"),
                                      ("omnia-pane.css", "text/css; charset=utf-8"),
                                      ("assets/icon.svg", "image/svg+xml"),
                                      ("vendor/purify.js", "text/javascript; charset=utf-8")):
            status, headers, raw = self.get(f"/ui/{relpath}", SAME)
            self.assertEqual((status, raw), (200, FIXTURE_FILES[relpath]), relpath)
            self.assertEqual(headers["Content-Type"], content_type)
            for name, value in STATIC_HEADER_SET.items():
                self.assertEqual(headers.get(name), value, (relpath, name))
            self.assertNotIn("Set-Cookie", headers)
            self.assertNotIn("ETag", headers)
            self.assertNotIn("Last-Modified", headers)

    def test_unlisted_unserved_suffix_and_the_sums_file_are_not_found(self):
        for path in ("/ui/vendor/VENDOR.json", "/ui/notes.txt", "/ui/SHA256SUMS", "/ui/missing.js"):
            self.assertEqual(self.code(path, SAME), (404, "not_found"), path)

    def test_a_changed_file_is_no_longer_served(self):
        (self.root / "app.js").write_bytes(b"alert(1)\n")
        self.assertEqual(self.code("/ui/app.js", SAME), (404, "not_found"))
        self.assertEqual(self.get("/ui/index.html", SAME)[0], 200, "the others still are")

    def test_a_malformed_sums_file_serves_nothing(self):
        for content in (b"not a sums line\n", b"", b"%s  app.js" % (b"0" * 64),
                        b"%s *app.js\n" % (b"0" * 64), b"%s  ../app.js\n" % (b"0" * 64)):
            (self.root / "SHA256SUMS").write_bytes(content)
            self.assertEqual(self.code("/ui/index.html", SAME), (404, "not_found"), content)
        write_sums(self.root, ("app.js", "app.js"))
        self.assertEqual(self.code("/ui/app.js", SAME), (404, "not_found"), "a duplicated entry")

    def test_traversal_encoding_and_odd_paths_are_not_found(self):
        for path in ("/ui/../plugin.yaml", "/ui/..%2fplugin.yaml", "/ui/vendor%2fpurify.js", "/ui/%2e%2e/x.js",
                     "/ui/vendor\\purify.js", "/ui/app.js?v=1", "/ui/APP.JS", "/ui//app.js", "/ui/./app.js",
                     "/ui/app.js/", "/ui/.hidden.js", "/ui", "/ui/", "/", "/favicon.ico", "/apple-touch-icon.png",
                     "/apple-touch-icon-precomposed.png", "/ui/a..b.js"):
            self.assertEqual(self.get(path, SAME)[0], 404, path)

    @unittest.skipIf(not hasattr(os, "symlink") or sys.platform == "win32", "symlinks need privileges on Windows")
    def test_symlinks_are_never_followed(self):
        outside = self.root.parent / (self.root.name + "-outside.js")
        outside.write_bytes(b"outside\n")
        self.addCleanup(outside.unlink)
        os.symlink(outside, self.root / "linked.js")
        os.symlink(self.root / "vendor", self.root / "linkdir")
        write_sums(self.root, ("app.js", "index.html", "linked.js"))
        with open(self.root / "SHA256SUMS", "a", encoding="ascii") as sums:
            sums.write(f"{web_api.hashlib.sha256(b'/* vendored */' + bytes([10])).hexdigest()}  linkdir/purify.js\n")
        self.assertEqual(self.code("/ui/linked.js", SAME), (404, "not_found"))
        self.assertEqual(self.code("/ui/linkdir/purify.js", SAME), (404, "not_found"))
        self.assertEqual(self.get("/ui/app.js", SAME)[0], 200)

    def test_index_is_served_to_a_top_level_navigation_from_anywhere_and_nothing_else_cross_site(self):
        self.assertEqual(self.get("/ui/index.html", NAVIGATE)[0], 200, "the bootstrap navigation is cross-site")
        self.assertEqual(self.get("/ui/index.html")[0], 200, "no fetch metadata")
        self.assertEqual(self.code("/ui/index.html", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"),
                                                      ("Sec-Fetch-Dest", "iframe")]), (403, "cross_site"))
        self.assertEqual(self.code("/ui/index.html", [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "cors")]),
                         (403, "cross_site"))
        for site in ("cross-site", "same-site", "none"):
            self.assertEqual(self.code("/ui/app.js", [("Sec-Fetch-Site", site), ("Sec-Fetch-Mode", "no-cors"),
                                                      ("Sec-Fetch-Dest", "script")]), (403, "cross_site"), site)
        self.assertEqual(self.get("/ui/app.js")[0], 200, "no fetch metadata is allowed")

    def test_only_get_and_no_credentials_of_other_families(self):
        for method in ("HEAD", "POST", "OPTIONS", "PUT"):
            status, headers, _raw = self.get("/ui/index.html", method=method)
            self.assertEqual((status, headers["Allow"]), (405, "GET"), method)
        for name in (chat_bridge.TOKEN_HEADER, "X-Sidecar-Session", "X-Sidecar-Channel", "X-Sidecar-Counter",
                     "X-Sidecar-MAC"):
            self.assertEqual(self.code("/ui/index.html", [(name, "x" * 43)]), (403, "forbidden_credential"), name)

    def test_the_default_root_is_the_plugin_sidebar_folder(self):
        self.assertEqual(web_api.SIDEBAR, Path(chat_bridge.__file__).resolve().parent / "sidebar")
        self.assertEqual(web_api.StaticFiles(web_api.SIDEBAR).root, Path(os.path.realpath(web_api.SIDEBAR)))


if __name__ == "__main__":
    unittest.main()
