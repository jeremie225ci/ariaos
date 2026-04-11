from __future__ import annotations

import os


def _stitch(*parts: str) -> str:
    return "".join(parts)


def local_hub_url() -> str:
    raw = str(os.environ.get("ARIA_CONTROL_TOWER_URL") or _stitch("http://", "10.0.2.2", ":3000")).strip()
    return raw or _stitch("http://", "10.0.2.2", ":3000")


def public_hub_url() -> str:
    raw = str(os.environ.get("ARIA_PUBLIC_HUB_URL") or os.environ.get("ARIA_CONTROL_TOWER_URL") or _stitch("https://", "get", "aria", "os", ".com")).strip()
    return raw or _stitch("https://", "get", "aria", "os", ".com")


def default_remote_ws() -> str:
    raw = str(os.environ.get("ARIA_PUBLIC_GATEWAY_URL") or _stitch("https://", "gateway.", "get", "aria", "os", ".com")).strip()
    if not raw:
        raw = _stitch("https://", "gateway.", "get", "aria", "os", ".com")
    if raw.startswith("wss://") or raw.startswith("ws://"):
        return raw.rstrip("/") + "/ws/client"
    if raw.startswith("https://"):
        return "wss://" + raw[len("https://"):].rstrip("/") + "/ws/client"
    if raw.startswith("http://"):
        return "ws://" + raw[len("http://"):].rstrip("/") + "/ws/client"
    return "wss://" + raw.rstrip("/") + "/ws/client"


def default_model_api() -> str:
    return _stitch("https://", "api.", "open", "ai", ".com", "/v1")


def endpoint_path(base_url: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/api/r"
