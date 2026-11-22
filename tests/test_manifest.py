# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
from __future__ import annotations

import json
from pathlib import Path
import unittest

import plugin

ROOT = Path(__file__).resolve().parents[1]


def manifest_list(field: str) -> list[str]:
    lines = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{field}:") + 1
    values = []
    for line in lines[start:]:
        if line.startswith("  - "):
            values.append(line[4:])
        else:
            break
    return values


class FakeContext:
    def __init__(self):
        self.tools = []

    def get_config(self, _key, default=None):
        return default

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


class ManifestRegistrationTests(unittest.TestCase):
    def test_schema_manifest_and_registration_names_match(self):
        schemas = json.loads((ROOT / "plugin" / "tool_schemas.json").read_text(encoding="utf-8"))
        schema_names = [schema["name"] for schema in schemas]
        for schema in schemas:
            self.assertIs(schema["parameters"]["additionalProperties"], False)
        ctx = FakeContext()
        plugin.register(ctx)
        registered = [entry["name"] for entry in ctx.tools]
        self.assertEqual(manifest_list("provides_tools"), schema_names)
        self.assertEqual(registered, schema_names)
        self.assertEqual({entry["toolset"] for entry in ctx.tools}, {"sidecar-libreoffice"})
        self.assertTrue(all(entry["override"] is False for entry in ctx.tools))

    def test_manifest_declares_secret_and_port_settings(self):
        text = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("type: secret", text)
        self.assertIn("env: LIBREOFFICE_LIVE_BRIDGE_TOKEN", text)
        self.assertIn("bridge_port:", text)
        self.assertIn('requires_hermes: \">=0.23.1\"', text)
        self.assertIn("env: API_SERVER_KEY", text)
        self.assertIn("license: Apache-2.0", text)
        self.assertIn("platforms: [macos, windows]", text)
        # A required secret makes Hermes warn at every load, because secrets live in .env, not
        # in the settings mapping the check reads.
        self.assertNotIn("required: true", text)

    def test_the_chat_port_default_lives_in_one_place_and_is_off(self):
        from plugin import chat_bridge
        lines = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8").splitlines()
        block = lines[lines.index("  chat_port:"):][:3]
        self.assertEqual(block[1:3], ["    type: int", f"    default: {chat_bridge.DEFAULT_CHAT_PORT}"])
        self.assertEqual(chat_bridge.DEFAULT_CHAT_PORT, 0)

    def test_every_disclosure_says_the_sidebar_chat_is_off_by_default(self):
        files = ["README.md", "plugin/README.md", "plugin/after-install.md", "SECURITY.md", "CHANGELOG.md",
                 "catalog/atlas-sidecar-libreoffice.yaml", "plugin/plugin.yaml"]
        for name in files:
            text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
            with self.subTest(file=name):
                self.assertIn("off by default", text)
        enable = "hermes config set plugins.entries.atlas-sidecar-libreoffice.settings.chat_port 8661"
        for name in ("README.md", "plugin/README.md", "plugin/after-install.md"):
            with self.subTest(file=name):
                self.assertIn(enable, (ROOT / name).read_text(encoding="utf-8"))

    def test_display_branding_in_catalog_extension_and_readme(self):
        name = "Sidecar for LibreOffice"
        self.assertIn(f'title: "{name}"', (ROOT / "catalog" / "atlas-sidecar-libreoffice.yaml").read_text(encoding="utf-8"))
        self.assertIn(f"{name} — Copyright 2026 Atlas Omnia", (ROOT / "NOTICE").read_text(encoding="utf-8"))
        self.assertIn(f'<name lang="en">{name}</name>', (ROOT / "extension" / "description.xml").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "README.md").read_text(encoding="utf-8").startswith(f"# {name}\n"))
        for path in ("README.md", "docs/demo.md", "catalog/atlas-sidecar-libreoffice.yaml", "extension/README.txt"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for old in ("LibreOffice Live", "Hermes LibreOffice Bridge"):
                self.assertNotIn(old, text, path)

    def test_versions_match_release(self):
        manifest = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8")
        extension = (ROOT / "extension" / "description.xml").read_text(encoding="utf-8")
        catalog = (ROOT / "catalog" / "atlas-sidecar-libreoffice.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.5.1", manifest)
        self.assertIn('<version value="0.5.1"/>', extension)
        self.assertIn('version: "0.5.1"', catalog)
        self.assertIn("/tree/v0.5.1#readme", catalog)
        # The text shipped inside the .oxt names the same version.
        self.assertTrue((ROOT / "extension" / "README.txt").read_text(encoding="utf-8").startswith("Sidecar for LibreOffice 0.5.1 "))
        # The extension bridge's Server header names the same minor version.
        self.assertIn('server_version = "SidecarLibreOfficeBridge/0.5"',
                      (ROOT / "extension" / "python" / "hermes_bridge.py").read_text(encoding="utf-8"))
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(changelog, r"\n## \[0\.5\.1\] - (Unreleased|\d{4}-\d{2}-\d{2})\n")

    def test_catalog_setup_tag_matches_the_release_version(self):
        """The catalog card sends users to build the extension at a tag: it must be this release's."""
        import re
        manifest = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version: (\S+)$", manifest, re.M).group(1)
        lines = (ROOT / "catalog" / "atlas-sidecar-libreoffice.yaml").read_text(encoding="utf-8").splitlines()
        description = next(line for line in lines if line.startswith("description: "))
        self.assertEqual(re.findall(r"\bat tag v(\d+\.\d+\.\d+)", description), [version])
        self.assertEqual(set(re.findall(r"\btag v(\d+\.\d+\.\d+)", "\n".join(lines))), {version})

    def test_shipped_files_name_no_private_host_or_home_path(self):
        """No private host name, home path or volume path in shipped source."""
        import re
        pattern = re.compile(r"\bATLAS\b|/Users/[A-Za-z]|/Volumes/|C:\\Users\\")
        hits = []
        for top in ("extension", "plugin", "scripts", "tests", "docs/assets", "catalog"):
            for path in (ROOT / top).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".md", ".txt", ".xml", ".xcu", ".xcs", ".yaml", ".json", ".svg", ".xdl"}:
                    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if pattern.search(line) and "pattern = re.compile" not in line:
                            hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(hits, [])


DISCLOSURE_FILES = ("README.md", "plugin/README.md", "SECURITY.md", "catalog/atlas-sidecar-libreoffice.yaml")
CURRENT_STATE_FILES = DISCLOSURE_FILES + ("plugin/after-install.md", "plugin/plugin.yaml", "extension/README.txt")
# Every file lane D owns may carry a D-FINAL marker during the draft; none may once 0.5.1 is dated.
LANE_D_FILES = CURRENT_STATE_FILES + ("CHANGELOG.md", "NOTICE", "plugin/NOTICE", "AGENTS.md", "CONTRIBUTING.md",
                                      "extension/description.xml", "docs/sidebar-design.md", "docs/sidecar-parity.md",
                                      "docs/demo.md", "docs/workflow-acceptance.md")


def flat(name: str) -> str:
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def released() -> bool:
    return "## [0.5.1] - Unreleased" not in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


class WindowDisclosureTests(unittest.TestCase):
    """Catalog rule 13: what the Sidecar window does on the user's machine is stated in every disclosure."""

    def test_every_disclosure_names_the_web_page_the_browser_and_what_a_tab_exposes(self):
        facts = (
            "also serves the Sidecar web page",            # the chat endpoint serves files and an API
            "checked against a hash list on every request",
            "Open Sidecar",                                 # the only trigger for a browser start
            "cookies or keychain",                          # never read
            "/usr/bin/osascript",                           # the programs the extension starts besides the browser
            "/usr/bin/open",
            "file association",                             # Windows: the default handler for .html files
            "It runs no shell.",
            "The plugin starts no process.",
            "one-time",                                     # launch code
            "session token",
            "challenge-response",                           # R18: the 0.5.1 extension never sends the token
            "0.4.0 extension",                              # the legacy routes keep the old limit
            "browser extensions can read the page",         # normal tab
            "history keeps the",                            # spent one-time link
            "managed",                                      # policies on the dedicated profile
            "MB",                                           # memory per window
            "about 60 MB",                                  # the dedicated browser profile
            "Linux",
        )
        for name in DISCLOSURE_FILES:
            text = flat(name)
            for fact in facts:
                with self.subTest(file=name, fact=fact):
                    self.assertTrue(fact in text, f"{name} must say {fact!r}")

    def test_no_current_document_still_claims_no_shell_commands(self):
        for name in CURRENT_STATE_FILES:
            text = flat(name).lower()
            for claim in ("no shell commands", "spawns no shell", "no browser is embedded"):
                with self.subTest(file=name, claim=claim):
                    self.assertNotIn(claim, text)

    def test_readme_has_the_sections_the_panel_texts_point_to(self):
        """docs/web-protocol.md section 8.2 panel texts say 'See README ▸ <section>'."""
        import re
        protocol = (ROOT / "docs" / "web-protocol.md").read_text(encoding="utf-8")
        named = set(re.findall(r"README ▸ ([A-Z][a-z]+(?: [a-z]+)*)", protocol))
        self.assertEqual(named, {"Sidebar chat", "Pairing", "Which browser"})
        headings = {line.lstrip("#").strip() for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
                    if line.startswith("#")}
        for section in named:
            with self.subTest(section=section):
                self.assertIn(section, headings)

    def test_draft_markers_are_gone_once_the_release_is_dated(self):
        markers = [name for name in LANE_D_FILES if "D-FINAL" in (ROOT / name).read_text(encoding="utf-8")]
        if not released():
            self.skipTest(f"0.5.1 is unreleased; D-FINAL markers left in: {', '.join(markers) or 'none'}")
        self.assertEqual(markers, [])


class FollowDisclosureTests(unittest.TestCase):
    """Draft documentation is not a native acceptance receipt; preserve the public contract."""

    def test_follow_disclosures_keep_the_opt_in_and_recovery_contract(self):
        for name in DISCLOSURE_FILES + ("extension/README.txt",):
            text = flat(name).lower()
            for fact in ("follow libreoffice", "off by default", "remember", "resize", "undock",
                         "reattach", "400 point", "spaces", "fullscreen", "retina", "untested"):
                with self.subTest(file=name, fact=fact):
                    # Markdown uses 400-point in the main README and 400 points elsewhere.
                    self.assertIn(fact, text.replace("400-point", "400 point"))

    def test_setup_commands_target_current_release_but_do_not_claim_publication(self):
        for name in ("README.md", "plugin/README.md", "plugin/after-install.md"):
            text = flat(name)
            with self.subTest(file=name):
                self.assertIn("--branch v0.5.1", text)
                self.assertNotIn("--branch v0.5.0", text)
                if not released():
                    self.assertIn("draft", text.lower())
                    self.assertIn("not published", text)
        catalog = flat("catalog/atlas-sidecar-libreoffice.yaml")
        if not released():
            self.assertIn("sha: TODO", catalog)
            self.assertIn("not a submitted/admitted catalog update", catalog)

    def test_unsafe_cache_text_matches_the_frozen_protocol(self):
        sentence = "The Sidecar cache folder is unsafe. See README ▸ Which browser."
        for name in ("docs/web-protocol.md", "README.md", "plugin/README.md"):
            with self.subTest(file=name):
                self.assertIn(sentence, flat(name))

    def test_current_styling_claim_is_pinned_not_computed_layout_equality(self):
        for name in ("README.md", "plugin/README.md", "docs/sidecar-parity.md"):
            text = flat(name)
            with self.subTest(file=name):
                self.assertIn("same pinned Omnia styling", text)
                self.assertIn("not a", text)
        readme = flat("README.md")
        self.assertNotIn("](docs/assets/screenshots/calc-summary-chart.png)", readme)
        self.assertIn("Historical 0.4.0 screenshots", readme)

    def test_evidence_ledger_preserves_scope_and_pending_candidate_gates(self):
        ledger = flat("docs/release-0.5.1.md")
        self.assertIn("2026-11-22" if released() else "Unreleased", ledger)
        if released():
            for fact in ("PASS 12/12", "exactly four", "Stop during a document tool",
                         "native-caption real-click close remains UNQUALIFIED",
                         "not full-contract G9 signoff", "Multi-display/Retina hardware untested"):
                with self.subTest(release_scope=fact):
                    self.assertIn(fact, ledger)
        for fact in ("797aeb5", "352f7fe", "fake Runs API", "owner-run",
                     "LOCAL SERVICE", "limited-token", "Linux units", "Manual VoiceOver",
                     "12-hour", "Q255", "not a clean-exit", "Pending", "publication"):
            with self.subTest(fact=fact):
                self.assertIn(fact, ledger)


if __name__ == "__main__":
    unittest.main()
