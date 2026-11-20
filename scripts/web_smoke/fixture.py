# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Assemble a servable page bundle for the harness: the fixture page, or a copy of a real bundle.

The vendored libraries are never committed here: they are copied from a bundle that has them
(``plugin/sidebar/vendor/`` once lane B lands, or any folder with the same two files) and
checked against the pins below (marked 15.0.12 ``lib/marked.umd.js`` and DOMPurify 3.4.12
``dist/purify.js``, as recorded in the 0.5.0 design review, V3).
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"
VENDOR_PINS = {
    "vendor/marked.umd.js": "d7931d1cd7bf727dd756c871637edcc9e0f8538003b927368400ec1ee47a9dd9",
    "vendor/purify.js": "0cb2ff0eb405f7d675fff04ae98ed277bb9fb10d3df33f29aa8be398e6e9f1b2",
}
SERVED_SUFFIXES = (".html", ".js", ".css", ".svg")


def find_vendor(source: Path) -> dict[str, Path]:
    source = Path(source)
    out = {}
    for rel, pin in VENDOR_PINS.items():
        for candidate in (source / rel, source / Path(rel).name):
            if candidate.is_file():
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if digest != pin:
                    raise ValueError(f"{candidate.name} does not match its pin ({digest[:12]}…)")
                out[rel] = candidate
                break
        else:
            raise FileNotFoundError(f"{rel} not found under {source}")
    return out


def write_sums(root: Path) -> str:
    lines = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and path.suffix in SERVED_SUFFIXES:
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    text = "\n".join(lines) + "\n"
    (root / "SHA256SUMS").write_bytes(text.encode("utf-8"))
    return text


def build_fixture(dest: Path, vendor_from: Path, *, unsafe: bool = False) -> Path:
    """The fixture page + pinned vendored libraries in ``dest`` with a fresh SHA256SUMS.
    ``unsafe`` builds the negative control: DOMPurify's default profile and no link pass."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "vendor").mkdir(parents=True)
    for name in ("index.html", "fixture.css", "app.js"):
        shutil.copyfile(FIXTURE / name, dest / name)
    if unsafe:
        app = (dest / "app.js").read_text(encoding="utf-8")
        flipped = app.replace("const STRICT_PROFILE = true;", "const STRICT_PROFILE = false;")
        assert flipped != app
        (dest / "app.js").write_text(flipped, encoding="utf-8")
    for rel, path in find_vendor(vendor_from).items():
        shutil.copyfile(path, dest / rel)
    write_sums(dest)
    return dest


def copy_bundle(source: Path, dest: Path) -> Path:
    """A private copy of a real bundle (so the static probes can tamper with it); its own
    SHA256SUMS is kept as shipped."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True)
    return dest


def bundle_is_contract_page(root: Path) -> Optional[str]:
    """None when ``root`` looks like a section 7.1 bundle, else the reason it does not."""
    root = Path(root)
    for name in ("index.html", "app.js", "SHA256SUMS", "vendor/marked.umd.js", "vendor/purify.js"):
        if not (root / name).is_file():
            return f"{name} missing"
    return None
