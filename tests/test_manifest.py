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
        self.assertIn("version: 0.4.0", manifest)
        self.assertIn('<version value="0.4.0"/>', extension)
        self.assertIn('version: "0.4.0"', catalog)
        # The text shipped inside the .oxt names the same version.
        self.assertTrue((ROOT / "extension" / "README.txt").read_text(encoding="utf-8").startswith("Sidecar for LibreOffice 0.4.0 "))
        self.assertIn('server_version = "SidecarLibreOfficeBridge/0.4"',
                      (ROOT / "extension" / "python" / "hermes_bridge.py").read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
