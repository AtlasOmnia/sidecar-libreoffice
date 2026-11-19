# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""The Tools-menu merge must target an entry inside the Tools menu.

Observed live on LibreOffice 26.8: the item was silently dropped when the merge
properties sat directly under the extension node (LibreOffice expects one more level,
a merge-instruction node such as ``S1``), and a bare ``.uno:ToolsMenu`` merge point asks
for a top-level sibling of the Tools menu. The merge point must be a path to a command
inside the Tools menu that exists in Calc, Writer and Impress.
"""
from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OOR = "{http://openoffice.org/2001/registry}"
# Present in the Tools menu of scalc, swriter and simpress menubar.xml (LibreOffice 26.8).
TOOLS_ENTRY = ".uno:ExtensionManager"


def props(node):
    out = {}
    for prop in node.findall("prop"):
        value = prop.find("value")
        out[prop.get(f"{OOR}name")] = "" if value is None or value.text is None else value.text
    return out


class ToolsMenuMergeTests(unittest.TestCase):
    def setUp(self):
        tree = ET.parse(ROOT / "extension" / "Addons.xcu")
        merging = tree.getroot().find("node[@oor:name='AddonUI']/node[@oor:name='OfficeMenuBarMerging']",
                                      {"oor": OOR[1:-1]})
        self.assertIsNotNone(merging)
        extensions = list(merging)
        self.assertEqual(len(extensions), 1)
        self.assertEqual(extensions[0].findall("prop"), [], "merge props must sit in an instruction node")
        self.entries = list(extensions[0])
        self.assertEqual([node.get(f"{OOR}name") for node in self.entries], ["S1"])

    def test_merge_point_is_inside_tools_menu(self):
        entry = props(self.entries[0])
        self.assertEqual(entry["MergePoint"], ".uno:ToolsMenu\\" + TOOLS_ENTRY)
        self.assertIn(entry["MergeCommand"], {"AddAfter", "AddBefore"})
        self.assertEqual(entry["MergeFallback"], "AddPath")

    def test_copy_token_item(self):
        items = self.entries[0].find("node[@oor:name='MenuItems']", {"oor": OOR[1:-1]})
        self.assertIsNotNone(items)
        item = props(items.find("node[@oor:name='copy-token']", {"oor": OOR[1:-1]}))
        self.assertEqual(item["URL"], "vnd.atlasomnia.sidecar:copy-token")
        title = items.find("node[@oor:name='copy-token']/prop[@oor:name='Title']/value", {"oor": OOR[1:-1]})
        self.assertEqual(title.text, "Copy Sidecar pairing token")


if __name__ == "__main__":
    unittest.main()
