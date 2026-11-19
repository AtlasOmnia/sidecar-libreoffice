# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The bundled guidance skills name only tools this plugin registers and link only files it ships."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

import plugin

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugin" / "skills"
TOOLS = {schema["name"] for schema in json.loads((ROOT / "plugin" / "tool_schemas.json").read_text(encoding="utf-8"))}
TOOL_WORD = re.compile(r"`((?:calc|writer|impress|libreoffice)_[a-z_]+)`")


class SkillContext:
    def __init__(self):
        self.skills, self.tools = [], []

    def get_config(self, _key, default=None):
        return default

    def register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])

    def register_skill(self, name, path, description=""):
        self.skills.append((name, Path(path), description))


class BundledSkillTests(unittest.TestCase):
    def test_plugin_registers_each_skill_with_its_own_frontmatter(self):
        ctx = SkillContext()
        plugin.register(ctx)
        self.assertEqual([name for name, _path, _d in ctx.skills], ["libreoffice-guidance", "calc-plans"])
        self.assertEqual(sorted(path.parent.name for path in SKILLS.glob("*/SKILL.md")), ["calc-plans", "libreoffice-guidance"])
        for name, path, description in ctx.skills:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(f"---\nname: {name}\ndescription: {description}\n"), name)
            self.assertIn("license: Apache-2.0", text)
        self.assertEqual(len(ctx.tools), len(TOOLS))

    def test_a_host_without_skill_support_still_gets_the_tools(self):
        class NoSkills:
            tools: list[str] = []

            def get_config(self, _key, default=None):
                return default

            def register_tool(self, **kwargs):
                self.tools.append(kwargs["name"])
        ctx = NoSkills()
        plugin.register(ctx)
        self.assertEqual(len(ctx.tools), len(TOOLS))

    def test_guidance_names_only_registered_tools(self):
        files = sorted(SKILLS.rglob("*.md"))
        self.assertEqual(len(files), 7)
        mentioned = set()
        for path in files:
            for name in TOOL_WORD.findall(path.read_text(encoding="utf-8")):
                if name.endswith("_"):   # a prefix such as `calc_*`
                    continue
                self.assertIn(name, TOOLS, f"{path.name} names a tool that is not registered: {name}")
                mentioned.add(name)
        # Every changing tool family is covered by the guidance.
        self.assertEqual(TOOLS - mentioned, set())

    def test_guidance_does_not_carry_the_source_products_tools_or_names(self):
        for path in SKILLS.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("sidecar_", text, path.name)
            self.assertNotIn("Office.js", text, path.name)
            if path.name != "provenance.md":
                text = text.replace("Sidecar for LibreOffice", "")   # this product's own name
                for word in ("Sidecar", "Workbench", "OOXML"):
                    self.assertNotIn(word, text, path.name)

    def test_relative_links_resolve_inside_the_skill(self):
        for path in SKILLS.rglob("*.md"):
            for target in re.findall(r"\]\(([^)#]+)\)", path.read_text(encoding="utf-8")):
                if target.startswith("http"):
                    continue
                self.assertTrue((path.parent / target).is_file(), f"{path.name}: {target}")


if __name__ == "__main__":
    unittest.main()
