# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""What the harness runs against.

- ``StubTarget``: ``stub_server.Stub`` in process (all controls; timings may be shortened).
- ``PluginTarget``: the real plugin through a factory (default ``plugin_factory.make_endpoint``):
  ``factory(*, sidebar_dir, api_port, token, api_key, fence_active, timing) -> started ChatBridge``
  where ``token``, ``api_key`` and ``fence_active`` are callables and ``timing`` is a dict of
  overrides the plugin may ignore (the harness then skips probes that would wait too long).
- ``ExternalTarget``: any running endpoint, given its port and pairing-token file (black-box only:
  no unload, readiness or fake Runs API checks).
"""
from __future__ import annotations

import importlib
import logging
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fake_runs_api import FakeRunsAPI  # noqa: E402
import fixture  # noqa: E402

CONTRACT_TIMING = {"code_ttl": 30.0, "launch_record_ttl": 120.0, "channel_ttl": 120.0, "channel_calls": 64,
                   "max_channels": 16, "max_codes": 16, "max_sessions": 8, "idle": 43200.0, "ended_keep": 600.0,
                   "ended_max": 64, "window_live": 90.0, "window_gone": 90.0, "longpoll": 25.0}
# Shortened on the stub so lifetime probes finish in seconds (the stub's own Timing honours them).
FAST_TIMING = {"code_ttl": 3.0, "launch_record_ttl": 8.0, "channel_ttl": 4.0, "idle": 4.0, "window_gone": 3.0,
               "window_live": 2.0, "longpoll": 4.0}


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{record.name} {record.levelname} {record.getMessage()}")
        except Exception:  # noqa: BLE001 - a log line that cannot be formatted is recorded as such
            self.lines.append(f"{record.name} {record.levelname} <unformattable>")


class Target:
    name = "target"
    port = 0
    token: Optional[str] = None
    timing: dict[str, float] = CONTRACT_TIMING
    capabilities: frozenset = frozenset()
    fake: Optional[FakeRunsAPI] = None
    ui_dir: Optional[Path] = None
    logs: Optional[LogCapture] = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def unload(self, budget: Optional[float] = None) -> int:
        raise NotImplementedError

    def owned_runs(self) -> int:
        raise NotImplementedError

    def set_ready(self, reason: Optional[str]) -> None:
        raise NotImplementedError

    def set_token(self, on: bool) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StubTarget(Target):
    name = "stub"

    def __init__(self, bundle: Path, *, fast: bool = True, repo: Optional[Path] = None) -> None:
        import stub_server
        self.workdir = Path(tempfile.mkdtemp(prefix="web-smoke-stub-"))
        self.ui_dir = fixture.copy_bundle(bundle, self.workdir / "ui")
        timing = dict(CONTRACT_TIMING, **(FAST_TIMING if fast else {}))
        self.timing = timing
        self.logs = LogCapture()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.INFO)
        self.stub = stub_server.Stub(self.ui_dir, repo=repo, timing=stub_server.Timing(**timing))
        self.fake = self.stub.fake
        self.port = self.stub.start()
        self.token = self.stub.token
        self._token = self.token
        self.capabilities = frozenset({"unload", "ready", "token", "fake", "logs", "ui_dir", "fast_timing"}
                                      if fast else {"unload", "ready", "token", "fake", "logs", "ui_dir"})

    def unload(self, budget: Optional[float] = None) -> int:
        return self.stub.close(budget)

    def owned_runs(self) -> int:
        return self.stub.bridge.owned_runs()

    def set_ready(self, reason: Optional[str]) -> None:
        self.stub.set_ready(reason)

    def set_token(self, on: bool) -> None:
        self.stub.token = self._token if on else None

    def close(self) -> None:
        if not self.stub.bridge.closed:
            self.stub.close(2.0)
        logging.getLogger().removeHandler(self.logs)
        shutil.rmtree(self.workdir, ignore_errors=True)


class PluginTarget(Target):
    name = "plugin"

    def __init__(self, bundle: Path, factory_spec: str, repo: Path) -> None:
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        module_name, _, func_name = factory_spec.partition(":")
        factory: Callable[..., Any] = getattr(importlib.import_module(module_name), func_name)
        self.workdir = Path(tempfile.mkdtemp(prefix="web-smoke-plugin-"))
        self.ui_dir = fixture.copy_bundle(bundle, self.workdir / "ui")
        self.fake = FakeRunsAPI(secrets.token_urlsafe(24))
        self.fake.start()
        self._token = secrets.token_urlsafe(32)
        self.token = self._token
        self.api_key_value: Optional[str] = self.fake.api_key
        self.fence = True
        self.logs = LogCapture()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.INFO)
        self.bridge = factory(sidebar_dir=self.ui_dir, api_port=self.fake.port, token=lambda: self.token,
                              api_key=lambda: self.api_key_value, fence_active=lambda: self.fence,
                              timing=dict(FAST_TIMING))
        self.port = self.bridge.port
        self.timing = dict(CONTRACT_TIMING, **getattr(self.bridge, "web_timing", {}))
        fast = all(self.timing.get(k) == v for k, v in FAST_TIMING.items())
        self.capabilities = frozenset({"unload", "ready", "token", "fake", "logs", "ui_dir"}
                                      | ({"fast_timing"} if fast else set()))

    def unload(self, budget: Optional[float] = None) -> int:
        return self.bridge.close(budget)

    def owned_runs(self) -> int:
        return self.bridge.owned_runs()

    def set_ready(self, reason: Optional[str]) -> None:
        self.fence = reason != "plugin_inactive"
        # bad_api_key: a key that cannot be sent as a header value (the plugin's own test of it)
        self.api_key_value = {"no_api_key": None, "bad_api_key": "unusable key"}.get(reason or "", self.fake.api_key)

    def set_token(self, on: bool) -> None:
        self.token = self._token if on else None

    def close(self) -> None:
        if not self.bridge.closed:
            self.bridge.close(2.0)
        self.fake.close()
        logging.getLogger().removeHandler(self.logs)
        shutil.rmtree(self.workdir, ignore_errors=True)


class ExternalTarget(Target):
    name = "external"

    def __init__(self, port: int, token_file: Path) -> None:
        self.port = port
        self.token = Path(token_file).read_text(encoding="utf-8").strip()
        self.capabilities = frozenset()
