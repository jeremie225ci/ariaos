from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOCAL_SECRET_STAMP = Path.home() / ".config" / "ariaos" / "local_secret_stamp"
LOCAL_SECRET_FILE = Path.home() / ".config" / "ariaos" / "local_agent_secrets.json"
LEGACY_ENCRYPTED_FILE = Path.home() / ".config" / "ariaos" / "user_secrets.enc"
_SENSITIVE_FIELDS = frozenset({"openai_api_key"})
_BLOCKED_FIELDS = frozenset({"vm_admin_password"})


def sensitive_fields() -> set[str]:
    return set(_SENSITIVE_FIELDS)


def secret_tool_available() -> bool:
    return False


def get_secret(name: str) -> str:
    key = str(name or "").strip()
    if not key:
        return ""
    if key in _BLOCKED_FIELDS:
        clear_secret(key)
        return ""
    _migrate_legacy_if_needed()
    return str(_load_store().get(key) or "").strip()


def set_secret(name: str, value: str, *, label: str) -> bool:
    del label
    key = str(name or "").strip()
    if not key:
        return False
    if key in _BLOCKED_FIELDS:
        clear_secret(key)
        return True
    secret_value = str(value or "")
    if not secret_value:
        return clear_secret(key)
    payload = _load_store()
    payload[key] = secret_value
    return _save_store(payload)


def clear_secret(name: str) -> bool:
    key = str(name or "").strip()
    if not key:
        return False
    payload = _load_store()
    if key not in payload:
        return False
    payload.pop(key, None)
    return _save_store(payload)


def _touch_stamp() -> None:
    try:
        LOCAL_SECRET_STAMP.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_SECRET_STAMP.write_text(str(time.time()), encoding="utf-8")
        os.chmod(LOCAL_SECRET_STAMP, 0o600)
    except Exception:
        pass


def _load_store() -> dict[str, str]:
    if not LOCAL_SECRET_FILE.exists():
        return {}
    try:
        payload = json.loads(LOCAL_SECRET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    sanitized = _sanitize_store(payload)
    if sanitized != payload:
        _save_store(sanitized)
    return sanitized


def _save_store(payload: dict[str, str]) -> bool:
    try:
        sanitized = _sanitize_store(payload)
        LOCAL_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_SECRET_FILE.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(LOCAL_SECRET_FILE, 0o600)
        _touch_stamp()
        return True
    except Exception:
        return False


def _sanitize_store(payload: dict[str, str]) -> dict[str, str]:
    # The public VM keeps only API credentials here. Privileged access is
    # granted through sudoers, so any leftover admin password is scrubbed.
    return {
        str(key): str(value or "")
        for key, value in dict(payload or {}).items()
        if str(key or "").strip() and str(key) not in _BLOCKED_FIELDS
    }


def _migrate_legacy_if_needed() -> None:
    if LOCAL_SECRET_FILE.exists() or not LEGACY_ENCRYPTED_FILE.exists():
        return
    try:
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except Exception:
        return

    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except Exception:
        machine_id = "ariaos-machine"
    material = f"{machine_id}|{Path.home()}|ariaos-local-secrets-v1".encode("utf-8")
    salt = b"ariaos-local-secret-salt"

    try:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390000)
        key = base64.urlsafe_b64encode(kdf.derive(material))
        fernet = Fernet(key)
        encrypted = LEGACY_ENCRYPTED_FILE.read_bytes()
        if not encrypted:
            return
        payload = json.loads(fernet.decrypt(encrypted).decode("utf-8"))
        if isinstance(payload, dict):
            _save_store({str(k): str(v or "") for k, v in payload.items()})
    except Exception:
        return
