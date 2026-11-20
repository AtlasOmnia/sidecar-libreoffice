# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Re-derive the origin of every third-party file in plugin/sidebar/ from its public source.

    python3 scripts/verify_vendor.py            # check vendor/VENDOR.json and UPSTREAM.json
    python3 scripts/verify_vendor.py --vendor   # only the npm packages
    python3 scripts/verify_vendor.py --upstream # only the three Omnia CSS files

For each package in plugin/sidebar/vendor/VENDOR.json it asks the npm registry for the package
version, checks that the registry's tarball URL and integrity equal the recorded ones, downloads
the tarball, checks its SHA-512 against that integrity, and checks that every recorded member's
SHA-256 equals both the recorded value and the shipped file. For plugin/sidebar/UPSTREAM.json it
downloads each pinned file at the pinned commit and checks that it equals the shipped copy byte for
byte (SHA-256 and git blob id).

Network access is needed, so this runs in its own CI job (.github/workflows/vendor.yml), never in
the unit suite and never from the plugin. It writes nothing. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDEBAR = ROOT / "plugin" / "sidebar"
REGISTRY = "https://registry.npmjs.org"
RAW = "https://raw.githubusercontent.com"
TIMEOUT = 60
MAX_DOWNLOAD = 32 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing a non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "sidecar-libreoffice-verify-vendor"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (https only, fixed hosts)
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"download larger than {MAX_DOWNLOAD} bytes: {url}")
    return data


def sri_sha512(data: bytes) -> str:
    """The npm `integrity` form of data: 'sha512-' + base64(SHA-512)."""
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 (git object id, not security)


def tar_member(tarball: bytes, member: str) -> bytes:
    """The bytes of one regular file in a .tgz, or ValueError."""
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        try:
            info = archive.getmember(member)
        except KeyError:
            raise ValueError(f"{member} is not in the tarball") from None
        if not info.isfile():
            raise ValueError(f"{member} is not a regular file")
        handle = archive.extractfile(info)
        if handle is None:
            raise ValueError(f"{member} cannot be read")
        return handle.read()


def check_package(package: dict, sidebar: Path = SIDEBAR, get=fetch) -> list[str]:
    name, version = package.get("name", ""), package.get("version", "")
    label = f"{name}@{version}"
    if not NAME_RE.match(name) or not VERSION_RE.match(version):
        return [f"{label}: name or version is not a plain npm name and exact version"]
    problems = []
    meta = json.loads(get(f"{REGISTRY}/{urllib.parse.quote(name)}/{version}").decode("utf-8"))
    dist = meta.get("dist") or {}
    if dist.get("tarball") != package.get("tarball"):
        problems.append(f"{label}: registry tarball URL {dist.get('tarball')!r} differs from the record")
    if dist.get("integrity") != package.get("integrity"):
        problems.append(f"{label}: registry integrity differs from the record")
    tarball = get(package["tarball"])
    if sri_sha512(tarball) != package.get("integrity"):
        problems.append(f"{label}: downloaded tarball does not match the recorded integrity")
        return problems
    for entry in package.get("files", []):
        member, rel, recorded = entry.get("member", ""), entry.get("path", ""), entry.get("sha256", "")
        try:
            upstream = hashlib.sha256(tar_member(tarball, member)).hexdigest()
        except ValueError as exc:
            problems.append(f"{label}: {exc}")
            continue
        shipped_path = sidebar / rel
        shipped = hashlib.sha256(shipped_path.read_bytes()).hexdigest() if shipped_path.is_file() else None
        if upstream != recorded:
            problems.append(f"{label}: {member} in the tarball differs from the recorded SHA-256")
        if shipped != recorded:
            problems.append(f"{label}: shipped {rel} differs from the recorded SHA-256")
    return problems


def check_upstream(record: dict, sidebar: Path = SIDEBAR, get=fetch) -> list[str]:
    repo, commit = record.get("repository", ""), record.get("commit", "")
    if not REPO_RE.match(repo) or not COMMIT_RE.match(commit):
        return ["UPSTREAM.json: repository or commit (40 hex) is malformed"]
    problems = []
    for entry in record.get("files", []):
        rel, upstream_path = entry.get("path", ""), entry.get("upstream_path", "")
        data = get(f"{RAW}/{repo}/{commit}/{urllib.parse.quote(upstream_path)}")
        shipped = (sidebar / rel).read_bytes() if (sidebar / rel).is_file() else None
        if hashlib.sha256(data).hexdigest() != entry.get("sha256") or git_blob_id(data) != entry.get("git_blob"):
            problems.append(f"{upstream_path} at {commit[:12]} differs from the recorded hashes")
        if shipped != data:
            problems.append(f"shipped {rel} is not byte-identical to {upstream_path} at {commit[:12]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the origin of plugin/sidebar's third-party files.")
    parser.add_argument("--vendor", action="store_true", help="only check vendor/VENDOR.json")
    parser.add_argument("--upstream", action="store_true", help="only check UPSTREAM.json")
    args = parser.parse_args(argv)
    both = not args.vendor and not args.upstream
    problems: list[str] = []
    checked = 0
    if args.vendor or both:
        record = json.loads((SIDEBAR / "vendor" / "VENDOR.json").read_text(encoding="utf-8"))
        for package in record.get("packages", []):
            problems += check_package(package)
            checked += len(package.get("files", []))
    if args.upstream or both:
        record = json.loads((SIDEBAR / "UPSTREAM.json").read_text(encoding="utf-8"))
        problems += check_upstream(record)
        checked += len(record.get("files", []))
    for problem in problems:
        print(f"verify_vendor: {problem}", file=sys.stderr)
    if not problems:
        print(f"verify_vendor: {checked} files match their recorded origin")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
