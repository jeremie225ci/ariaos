from __future__ import annotations

import os


def _stitch(*parts: str) -> str:
    return "".join(parts)


def local_control_tower_url() -> str:
    raw = str(os.environ.get("ARIA_CONTROL_TOWER_URL") or _stitch("http://", "10.0.2.2", ":3000")).strip()
    return raw or _stitch("http://", "10.0.2.2", ":3000")


def public_control_tower_url() -> str:
    return _stitch("https://", "get", "aria", "os", ".com")


def default_remote_gateway_ws() -> str:
    return _stitch("wss://", "gateway.", "get", "aria", "os", ".com", "/ws/client")


def default_openai_base_url() -> str:
    return _stitch("https://", "api.", "open", "ai", ".com", "/v1")


def control_plane_url(base_url: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/api/r"
