# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Independent revision-5 formulas, published text, plugin and launcher must agree."""
import base64
import hashlib
import hmac
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "extension/python"))
from plugin import web_auth
import sidecar_launcher


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mac(key, *parts):
    return b64(hmac.new(key, "\n".join(parts).encode("ascii"), hashlib.sha256).digest())


def main():
    token = "example-pairing-token-0123456789abcdefghijklmnopq"
    cn, sn = b64(bytes(range(32))), b64(bytes(range(32, 64)))
    key = hmac.new(token.encode(), "\n".join(("sidecar-launcher/1 channel", "8661", cn, sn)).encode(), hashlib.sha256).digest()
    assert key.hex() == "49b917b673bf4f129f826f3e5680411bd328a6712a6a000b133a1cda36d5a1f1"
    assert web_auth.channel_key(token.encode(), 8661, cn, sn) == key
    assert sidecar_launcher.channel_key(token, 8661, cn, sn) == key
    text = (ROOT / "docs/web-protocol.md").read_text()
    vectors = [
        (3, b'{"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":1,"state":"normal","bounds":{"x":1240,"y":102,"width":400,"height":848}}', "22f9e39c4973f6720ea42b8e720a5f5993d2b3d578682559edf6d4ae654ac807", "v30WcmVm1K7joHamsLRFyRhnU4E5g6y47-bhQB8TPKE", None),
        (3, b'{"ok":true,"seq":1}', "c87ef1c0ab45dc8b9e285d32cd005ac8e66110a4dde2e68dfaa64991dcc2570c", "uB4aMcV9-01j7t7iVhTqS1q74kMyjkur7kCGft1U-dQ", 200),
        (4, b'{"launch_id":"AAAAAAAAAAAAAAAAAAAAAA","seq":2,"state":"minimized","bounds":null}', "cda42e6aeb2b78e01ce7ef5c07e3515c219f8a2161bff327b77662c9a9d7b254", "zekmULHpr-_hN52XDlsmycbEFggZ24vGwBNHTRKBHv8", None),
    ]
    for n, body, digest, expected, status in vectors:
        actual_hash = hashlib.sha256(body).hexdigest()
        assert actual_hash == digest
        if status is None:
            actual = mac(key, "sidecar-launcher/1 request", "POST", "/launcher/bounds", str(n), actual_hash)
            for impl in (web_auth, sidecar_launcher):
                assert impl.request_mac(key, "POST", "/launcher/bounds", n, body) == actual
        else:
            actual = mac(key, "sidecar-launcher/1 response", str(n), str(status), actual_hash)
            for impl in (web_auth, sidecar_launcher):
                assert impl.response_mac(key, n, status, body) == actual
        assert actual == expected
        assert body.decode() in text and digest in text and expected in text
        print(f"{'response' if status else 'request'} {n}: SHA256 {digest}; MAC {actual}; reference + text + plugin + launcher PASS")
    print("3/3 revision-5 vectors verified")


if __name__ == "__main__":
    main()
