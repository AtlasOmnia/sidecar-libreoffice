# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Credentials and request gates of the Sidecar window (docs/web-protocol.md 1.2, 2, 3, 4, 6.8).

No sockets and no threads here: ``web_api.py`` calls these classes with its own lock held, so
none of them is thread-safe on its own. Every time value is a ``clock()`` reading in seconds.

- ``Channels``: the launcher's challenge-response channel (R18). The launcher never sends the
  pairing token; it proves knowledge of it with HMAC-SHA256 and every later call carries a MAC
  over method, path, counter and body hash.
- ``Launches``: one-time launch codes (32 random bytes, stored only as SHA-256 digests, 30 s,
  single use, at most 16 outstanding) and the launch status records the launcher polls (120 s).
- ``Sessions``: session tokens (32 random bytes, digests only, memory only), at most 8, with the
  reasons ended tokens are remembered by for 10 minutes (at most 64).
- Gate helpers that read request headers (an ``email.message.Message`` as the stdlib HTTP server
  gives them) and return a machine code from section 9.1 or None.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Callable, Optional

from .chat_bridge import DOCUMENT_TYPES, SESSION_RE

CODE_SECONDS = 30.0            # a launch code is valid this long from minting, once
LAUNCH_RECORD_SECONDS = 120.0  # the launcher may poll a launch's state this long
MAX_CODES = 16                 # unexchanged launch codes outstanding
CHANNEL_SECONDS = 120.0
CHANNEL_CALLS = 64
MAX_CHANNELS = 16
SESSION_IDLE_SECONDS = 12 * 3600.0
MAX_SESSIONS = 8
ENDED_SECONDS = 600.0
MAX_ENDED = 64
WINDOW_LIVE_SECONDS = 90.0     # a session seen this recently (or holding a long-poll) is a live window

END_REASONS = ("closed", "replaced", "host_gone", "turned_off", "idle", "double_use", "evicted")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")       # launch codes, session tokens, nonces
SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")    # launch ids, turn ids
COUNTER_RE = re.compile(r"^[1-9][0-9]{0,9}$")
PROTOCOL = "sidecar-launcher/1"

# Credential headers per route family that a request must NOT carry (1.2, gate 2). ``sec-fetch-*``
# stands for every header whose name starts with ``Sec-Fetch-``.
FOREIGN = {
    "launcher": ("origin", "sec-fetch-*", "x-sidecar-session", "x-hermes-plugin-key"),
    "ui": ("x-hermes-plugin-key", "x-sidecar-session", "x-sidecar-channel", "x-sidecar-counter", "x-sidecar-mac"),
    "api": ("x-hermes-plugin-key", "x-sidecar-channel", "x-sidecar-counter", "x-sidecar-mac"),
}


def b64u(raw: bytes) -> str:
    """base64url without padding (RFC 4648 section 5)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def new_token() -> str:
    return b64u(secrets.token_bytes(32))


def new_short_id() -> str:
    return b64u(secrets.token_bytes(16))


def new_chat_session_id() -> str:
    """A chat session id in the panel's ``lo-<32 hex>`` shape; the fence keys on ``SESSION_RE``."""
    value = "lo-" + secrets.token_hex(16)
    if not SESSION_RE.fullmatch(value):   # SESSION_RE stays the one definition of the shape
        raise AssertionError("chat session id shape")
    return value


# -- R18 launcher channel (3) --

def _mac(key: bytes, *parts: str) -> bytes:
    return hmac.new(key, "\n".join(parts).encode("ascii"), hashlib.sha256).digest()


def hello_proof(token_key: bytes, port: int, client_nonce: str, server_nonce: str) -> str:
    return b64u(_mac(token_key, f"{PROTOCOL} hello", str(port), client_nonce, server_nonce))


def channel_key(token_key: bytes, port: int, client_nonce: str, server_nonce: str) -> bytes:
    return _mac(token_key, f"{PROTOCOL} channel", str(port), client_nonce, server_nonce)


def request_mac(key: bytes, method: str, path: str, counter: int, body: bytes) -> str:
    return b64u(_mac(key, f"{PROTOCOL} request", method, path, str(counter), hashlib.sha256(body).hexdigest()))


def response_mac(key: bytes, counter: int, status: int, body: bytes) -> str:
    return b64u(_mac(key, f"{PROTOCOL} response", str(counter), str(status), hashlib.sha256(body).hexdigest()))


class Channel:
    __slots__ = ("nonce", "key", "next_counter", "calls", "expires")

    def __init__(self, nonce: str, key: bytes, expires: float) -> None:
        self.nonce, self.key, self.next_counter, self.calls, self.expires = nonce, key, 1, 0, expires


class Channels:
    """Live launcher channels by server nonce (at most ``MAX_CHANNELS``; a new hello drops the
    oldest). A nonce opens exactly one channel and is never reissued."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._live: "OrderedDict[str, Channel]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._live)

    def open(self, token_key: bytes, port: int, client_nonce: str) -> tuple[str, str]:
        """-> ``(server_nonce, server_proof)``."""
        self.expire()
        server_nonce = new_token()
        while server_nonce in self._live:   # 256-bit random: never in practice
            server_nonce = new_token()
        while len(self._live) >= MAX_CHANNELS:
            self._live.popitem(last=False)
        self._live[server_nonce] = Channel(server_nonce, channel_key(token_key, port, client_nonce, server_nonce),
                                           self._clock() + CHANNEL_SECONDS)
        return server_nonce, hello_proof(token_key, port, client_nonce, server_nonce)

    def verify(self, nonces: Optional[list], counters: Optional[list], macs: Optional[list],
               method: str, path: str, body: bytes) -> Optional[tuple[bytes, int]]:
        """Check one call. -> ``(channel_key, counter)`` and the channel advances, or None: then
        the call is refused (401 ``launcher_auth``) and a live channel it named is dropped."""
        self.expire()
        if not nonces:
            return None
        if len(nonces) != 1:   # a duplicated header is refused as a wrong value: drop what it names
            for nonce in nonces:
                self._live.pop(nonce, None)
            return None
        channel = self._live.get(nonces[0])
        if channel is None:
            return None
        ok = (counters is not None and len(counters) == 1 and COUNTER_RE.fullmatch(counters[0]) is not None
              and int(counters[0]) == channel.next_counter and channel.calls < CHANNEL_CALLS
              and macs is not None and len(macs) == 1
              and hmac.compare_digest(macs[0].encode("ascii", "replace"),
                                      request_mac(channel.key, method, path, channel.next_counter,
                                                  body).encode("ascii")))
        if not ok:
            del self._live[channel.nonce]
            return None
        counter = channel.next_counter
        channel.next_counter += 1
        channel.calls += 1
        return channel.key, counter

    def expire(self) -> None:
        now = self._clock()
        for nonce in [nonce for nonce, channel in self._live.items() if channel.expires <= now]:
            del self._live[nonce]

    def clear(self) -> None:
        self._live.clear()


# -- launch codes (4.1, 4.2, 6.1) --

class Launch:
    """One minted launch. ``state``: pending, exchanged, expired or double_use."""

    __slots__ = ("launch_id", "code_digest", "minted", "document", "host_scheme", "mode", "bounds", "state",
                 "session_digest")

    def __init__(self, launch_id: str, code_digest: bytes, minted: float, document: dict, host_scheme: Optional[str],
                 mode: str, bounds: Optional[dict]) -> None:
        self.launch_id, self.code_digest, self.minted = launch_id, code_digest, minted
        self.document, self.host_scheme, self.mode, self.bounds = document, host_scheme, mode, bounds
        self.state = "pending"
        self.session_digest: Optional[bytes] = None


class Launches:
    """Code records (by code digest, held ``CODE_SECONDS`` from minting whatever their state, so a
    second presentation within that time is a double use) and launch records (by id, kept
    ``LAUNCH_RECORD_SECONDS`` for the launcher's status poll)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._codes: "OrderedDict[bytes, Launch]" = OrderedDict()
        self._launches: "OrderedDict[str, Launch]" = OrderedDict()

    def outstanding(self) -> int:
        self.expire()
        return sum(1 for launch in self._codes.values() if launch.state == "pending")

    def held_codes(self) -> int:
        return len(self._codes)

    def mint(self, document: dict, host_scheme: Optional[str], mode: str, bounds: Optional[dict]) -> tuple[str, Launch]:
        """-> ``(code, launch)``; the caller checks ``outstanding() < MAX_CODES`` first."""
        self.expire()
        code = new_token()
        launch_id = new_short_id()
        while launch_id in self._launches:
            launch_id = new_short_id()
        launch = Launch(launch_id, digest(code), self._clock(), document, host_scheme, mode, bounds)
        self._codes[launch.code_digest] = launch
        self._launches[launch_id] = launch
        return code, launch

    def find_code(self, code: str) -> Optional[Launch]:
        """The record of a code still held (30 s from minting), compared by digest."""
        self.expire()
        wanted = digest(code)
        for held, launch in self._codes.items():   # at most a few dozen; constant-time per entry
            if hmac.compare_digest(held, wanted):
                return launch
        return None

    def get(self, launch_id: str) -> Optional[Launch]:
        self.expire()
        return self._launches.get(launch_id)

    def revoke_pending(self, document_id: Optional[str]) -> int:
        """Purge unexchanged codes for one document (or all): the launcher released it."""
        gone = [key for key, launch in self._codes.items() if launch.state == "pending"
                and (document_id is None or launch.document["document_id"] == document_id)]
        for key in gone:
            self._codes[key].state = "expired"
            del self._codes[key]
        return len(gone)

    def expire(self) -> None:
        now = self._clock()
        for key in [key for key, launch in self._codes.items() if launch.minted + CODE_SECONDS <= now]:
            launch = self._codes.pop(key)
            if launch.state == "pending":
                launch.state = "expired"
        for launch_id in [launch_id for launch_id, launch in self._launches.items()
                          if launch.minted + LAUNCH_RECORD_SECONDS <= now]:
            del self._launches[launch_id]

    def clear(self) -> None:
        for launch in self._codes.values():
            if launch.state == "pending":
                launch.state = "expired"
        self._codes.clear()
        self._launches.clear()


# -- sessions (6.1, 6.8) --

class Session:
    """One Sidecar window's session. ``turn`` and the long-poll fields belong to web_api."""

    __slots__ = ("digest", "document", "host_scheme", "mode", "bounds", "chat_session_id", "created", "last_seen",
                 "launch_id", "turn", "parked", "starting", "ended")

    def __init__(self, token_digest: bytes, launch: Launch, now: float) -> None:
        self.digest = token_digest
        self.document, self.host_scheme, self.mode, self.bounds = (launch.document, launch.host_scheme,
                                                                    launch.mode, launch.bounds)
        self.chat_session_id = new_chat_session_id()
        self.created = self.last_seen = now
        self.launch_id = launch.launch_id
        self.turn: Any = None
        self.parked = 0          # long-polls of this session in flight
        self.starting = False    # an /api/chat is claiming a run for this session right now
        self.ended: Optional[str] = None


class Sessions:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._live: "OrderedDict[bytes, Session]" = OrderedDict()
        self._ended: "OrderedDict[bytes, tuple[str, float]]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._live)

    def all(self) -> list:
        return list(self._live.values())

    def mint(self, launch: Launch) -> tuple[str, Session]:
        token = new_token()
        session = Session(digest(token), launch, self._clock())
        self._live[session.digest] = session
        return token, session

    def lookup(self, values: Optional[list]) -> tuple[Optional[Session], Optional[str]]:
        """The live session named by the ``X-Sidecar-Session`` values, else ``(None, reason)``
        with the reason a remembered ended token ended for (None when unknown)."""
        self._forget_old()
        if not values or len(values) != 1 or not TOKEN_RE.fullmatch(values[0]):
            return None, None
        wanted = digest(values[0])
        found = None
        for held, session in self._live.items():   # at most MAX_SESSIONS
            if hmac.compare_digest(held, wanted):
                found = session
        if found is not None:
            return found, None
        for held, (reason, _at) in self._ended.items():
            if hmac.compare_digest(held, wanted):
                return None, reason
        return None, None

    def for_document(self, document_id: str) -> list:
        return [session for session in self._live.values() if session.document["document_id"] == document_id]

    def by_digest(self, token_digest: Optional[bytes]) -> Optional[Session]:
        return self._live.get(token_digest) if token_digest is not None else None

    def end(self, session: Session, reason: str) -> bool:
        """Remove a live session and remember why (10 min, at most 64). False if not live."""
        if reason not in END_REASONS:
            raise ValueError("unknown end reason")
        if self._live.get(session.digest) is not session:
            return False
        del self._live[session.digest]
        session.ended = reason
        self._ended[session.digest] = (reason, self._clock())
        self._ended.move_to_end(session.digest)
        while len(self._ended) > MAX_ENDED:
            self._ended.popitem(last=False)
        return True

    def evictable(self, driven: Callable[[Session], bool]) -> Optional[Session]:
        """The least recently seen session without a driven run, or None."""
        candidates = [session for session in self._live.values() if not driven(session)]
        return min(candidates, key=lambda session: session.last_seen) if candidates else None

    def idle(self) -> list:
        now = self._clock()
        return [session for session in self._live.values() if session.last_seen + SESSION_IDLE_SECONDS <= now]

    def _forget_old(self) -> None:
        now = self._clock()
        for key in [key for key, (_reason, at) in self._ended.items() if at + ENDED_SECONDS <= now]:
            del self._ended[key]


# -- gates (1.2) --

def header_values(headers: Any, name: str) -> list:
    return list(headers.get_all(name) or [])


def host_ok(headers: Any, host: str) -> bool:
    return header_values(headers, "Host") == [host]


def carries_foreign_credential(headers: Any, family: str) -> bool:
    forbidden = FOREIGN[family]
    for name in headers.keys():
        lowered = name.lower()
        if lowered in forbidden or (lowered.startswith("sec-fetch-") and "sec-fetch-*" in forbidden):
            return True
    return False


def api_fetch_metadata(headers: Any, method: str, origin: str) -> Optional[str]:
    """Gate 3 for ``/api/*``: None, ``cross_site`` or ``bad_origin``."""
    sites = header_values(headers, "Sec-Fetch-Site")
    if sites and sites != ["same-origin"]:
        return "cross_site"
    origins = header_values(headers, "Origin")
    if method == "POST":
        return None if origins == [origin] else "bad_origin"
    if origins:
        return None if origins == [origin] else "bad_origin"
    return None if sites == ["same-origin"] else "bad_origin"


def static_fetch_allowed(headers: Any, top_level_document: bool) -> bool:
    """Gate 3 for ``/ui/*`` (7): ``index.html`` to a top-level navigation whatever its site;
    everything else only same-origin or without fetch metadata."""
    sites = header_values(headers, "Sec-Fetch-Site")
    if top_level_document:
        modes = header_values(headers, "Sec-Fetch-Mode")
        dests = header_values(headers, "Sec-Fetch-Dest")
        if modes in ([], ["navigate"]) and dests in ([], ["document"]):
            return True
    return sites in ([], ["same-origin"])


def json_content_type(headers: Any) -> bool:
    values = header_values(headers, "Content-Type")
    if len(values) != 1:
        return False
    media, _, params = values[0].partition(";")
    if media.strip().lower() != "application/json":
        return False
    params = params.strip()
    return not params or re.fullmatch(r"charset\s*=\s*\"?utf-8\"?", params, re.IGNORECASE) is not None


def parse_object(raw: bytes, keys: tuple) -> Optional[dict]:
    """A JSON object with exactly ``keys`` (no duplicate names), else None."""
    def no_duplicates(pairs: list) -> dict:
        names = [name for name, _value in pairs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate key")
        return dict(pairs)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != set(keys):
        return None
    return value


def valid_document(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"document_id", "title", "type"}
            and all(isinstance(value[key], str) for key in value)
            and 0 < len(value["document_id"]) <= 512 and len(value["title"]) <= 1000
            and value["type"] in DOCUMENT_TYPES)


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_rectangle(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"x", "y", "width", "height"}
            and all(_int(value[key]) for key in value)
            and -32768 <= value["x"] <= 32767 and -32768 <= value["y"] <= 32767
            and 240 <= value["width"] <= 8192 and 240 <= value["height"] <= 8192)
