# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""``PluginTarget`` factory for the real plugin: a ``ChatBridge`` with the Sidecar window routes
(``plugin/web_api.py``) attached to a given page bundle, the same way the plugin's own tests build
it. Contract timings apply (the plugin has no overrides), so probes that would wait longer than
40 s are skipped unless ``--slow``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def make_endpoint(*, sidebar_dir: Path, api_port: int, token: Callable[[], Optional[str]],
                  api_key: Callable[[], Optional[str]], fence_active: Callable[[], bool], timing: dict) -> Any:
    from plugin import chat_bridge, web_api   # imported late: the target puts the repository on sys.path

    bridge = chat_bridge.ChatBridge(port=0, token=token, api_key=api_key, api_port=api_port,
                                    fence_active=fence_active, web_routes=False)
    bridge.web = web_api.attach(bridge, root=Path(sidebar_dir))
    bridge.start()
    bridge.web_timing = {}
    return bridge
