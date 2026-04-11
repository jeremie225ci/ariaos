from __future__ import annotations

import ast
import json
import os
import re
import socket
import hashlib
import math
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import webbrowser
import base64
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .local_secrets import (
    LOCAL_SECRET_STAMP,
    clear_secret,
    get_secret,
    sensitive_fields,
    set_secret,
)
from .bootstrap import (
    default_model_api,
    default_remote_ws,
    endpoint_path,
    local_hub_url,
    public_hub_url,
)

try:
    from .mesh import (
        ensure_access as _ensure_access,
        fetch_realtime_token as _fetch_realtime_token,
        login_access as _login_access,
        prepare_task as _prepare_task,
        refresh_access as _refresh_access,
        report_usage as _report_usage,
        create_access as _create_access,
        sync_device as _sync_device,
        sync_task as _sync_task,
    )
except Exception:
    JsonLoader = Callable[[Any], dict[str, Any]]
    JsonSaver = Callable[[Any, dict[str, Any]], None]
    HttpJson = Callable[..., tuple[int, dict[str, Any], str | None]]
    HttpUploadBinary = Callable[..., tuple[int, str]]

    _OP_LOGIN = "l"
    _OP_SIGNUP = "n"
    _OP_SESSION = "m"
    _OP_CLAIM = "c"
    _OP_PREPARE = "p"
    _OP_DEVICE = "d"
    _OP_REALTIME = "rt"
    _OP_SCREENSHOT = "k"
    _OP_SYNC = "s"
    _OP_USAGE = "u"

    def _plan_status_from_payload_local(plan: dict[str, Any], session: dict[str, Any]) -> tuple[str, str, str]:
        plan_key = str(plan.get("k") or plan.get("key") or session.get("plan_key") or "free")
        plan_name = str(plan.get("n") or plan.get("name") or session.get("plan_name") or "Free")
        plan_status = str(plan.get("s") or plan.get("status") or session.get("plan_status") or "trialing")
        return plan_key, plan_name, plan_status

    def _merge_plan_state_local(*, session: dict[str, Any], data: dict[str, Any], canonical: str, save_json: JsonSaver, session_file) -> None:
        plan = data.get("p") or data.get("plan") or {}
        plan_key, plan_name, plan_status = _plan_status_from_payload_local(plan if isinstance(plan, dict) else {}, session)
        if plan_status == "active" and plan_key in {"monthly_pro", "annual_pro"}:
            if str(session.get("activation_notice_seen_for") or "") != plan_key:
                session["activation_notice_pending"] = plan_key
        session.update(
            {
                "account_uid": str(data.get("u") or data.get("userId") or session.get("account_uid") or ""),
                "plan_status": plan_status,
                "plan_key": plan_key,
                "plan_name": plan_name,
                "prompts_remaining": data.get("r") if "r" in data else data.get("remainingPrompts"),
                "hub_url": canonical,
            }
        )
        save_json(session_file, session)

    def _runtime_call_local(
        *,
        canonical: str,
        http_json: HttpJson,
        op: str,
        auth_token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any], str | None]:
        body = {"o": op}
        if isinstance(payload, dict) and payload:
            body.update(payload)
        return http_json(endpoint_path(canonical), method="POST", auth_token=auth_token, payload=body)

    def _login_access(
        *,
        base_url: str,
        email: str,
        password: str,
        session_file,
        save_json: JsonSaver,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        refresh_session: Callable[[str | None], tuple[bool, str]],
    ) -> tuple[bool, str]:
        canonical = canonical_hub_url(base_url)
        status, body, auth_token = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_LOGIN,
            payload={"e": str(email or "").strip(), "p": str(password or "")},
        )
        if status >= 400 or not body.get("ok") or not auth_token:
            return False, str(body.get("error") or "Control Tower sign in failed.")
        user = dict((body.get("d") or {}).get("u") or {}) if isinstance(body.get("d"), dict) else {}
        save_json(
            session_file,
            {
                "account_email": str(user.get("email") or email),
                "account_uid": str(user.get("uid") or ""),
                "plan_status": "trialing",
                "plan_key": "free",
                "hub_url": canonical,
                "auth_token": auth_token,
                "plan_name": "AriaOS",
                "activation_notice_pending": "",
                "activation_notice_seen_for": "",
                "prompts_remaining": None,
            },
        )
        return refresh_session(canonical)

    def _create_access(
        *,
        base_url: str,
        email: str,
        password: str,
        display_name: str,
        company: str,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
    ) -> tuple[bool, str]:
        canonical = canonical_hub_url(base_url)
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_SIGNUP,
            payload={
                "e": str(email or "").strip(),
                "p": str(password or ""),
                "d": str(display_name or "").strip(),
                "c": str(company or "").strip(),
            },
        )
        if status >= 400 or not body.get("ok"):
            return False, str(body.get("error") or "Control Tower signup failed.")
        return True, "Account created. Verify your email from the link we sent, then sign in."

    def _refresh_access(
        *,
        base_url: str | None,
        session_file,
        load_json: JsonLoader,
        save_json: JsonSaver,
        hub_url: str,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
    ) -> tuple[bool, str]:
        session = load_json(session_file)
        canonical = canonical_hub_url(base_url or session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, "No local Control Tower session found."
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_SESSION,
            auth_token=auth_token,
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, str(body.get("error") or "Failed to refresh Control Tower state.")
        data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
        user = dict(data.get("u") or {}) if isinstance(data.get("u"), dict) else {}
        session.update(
            {
                "account_email": str(user.get("e") or user.get("email") or session.get("account_email") or "Connected"),
                "account_uid": str(user.get("i") or user.get("uid") or session.get("account_uid") or ""),
            }
        )
        _merge_plan_state_local(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
        return True, "Control Tower session refreshed."

    def _ensure_access(
        *,
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        save_json: JsonSaver,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
    ) -> tuple[bool, str]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, "Connect a Control Tower account first."
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_CLAIM,
            auth_token=auth_token,
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, str(body.get("error") or "Prompt access could not be verified.")
        data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
        _merge_plan_state_local(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
        if not bool(data.get("a")):
            return False, str(data.get("m") or "Prompt access could not be verified.")
        remaining = data.get("r")
        if remaining is None:
            return True, "Prompt access granted."
        return True, f"Prompt access granted. {remaining} free prompt(s) remaining."

    def _prepare_task(
        *,
        task_id: str,
        session_id: str,
        goal: str,
        requested_model: str,
        image: dict[str, Any] | None,
        max_budget_usd: float,
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        save_json: JsonSaver,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
        device: dict[str, str],
        runtime: dict[str, str],
        account_uid: str,
        active_app: str = "Terminal Aria",
        local_time: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, {"message": "Connect a Control Tower account first.", "taskId": task_id, "sessionId": session_id}
        task_model = str(requested_model or "").strip() or str(runtime.get("model") or "gpt-5.4")
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_PREPARE,
            auth_token=auth_token,
            payload={
                "t": str(task_id or "").strip(),
                "s": str(session_id or "").strip(),
                "g": str(goal or "").strip(),
                "x": task_model,
                "i": image if isinstance(image, dict) else None,
                "b": float(max_budget_usd or 0.0),
                "d": str(device.get("device_id") or ""),
                "h": str(device.get("machine_uid") or ""),
                "n": str(device.get("device_name") or "Aria VM"),
                "m": task_model,
                "a": str(active_app or "Terminal Aria"),
                "z": str(local_time or ""),
                "q": str(account_uid or ""),
            },
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, {"message": str(body.get("error") or "Remote task access denied."), "taskId": task_id, "sessionId": session_id}
        data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
        _merge_plan_state_local(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
        if not bool(data.get("a")):
            return False, {"message": str(data.get("m") or "Remote task access denied."), "taskId": task_id, "sessionId": session_id}
        envelope = data.get("x") if isinstance(data.get("x"), dict) else {}
        return True, dict(envelope or {})

    def _fetch_realtime_token(
        *,
        kind: str,
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
        device: dict[str, str],
    ) -> tuple[bool, dict[str, Any]]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, {"message": "Connect a Control Tower account first."}
        status, body, _cookie = http_json(
            f"{canonical}/api/runtime/realtime-token",
            method="POST",
            auth_token=auth_token,
            payload={
                "kind": "host" if str(kind or "").strip().lower() == "host" else "device",
                "deviceId": str(device.get("device_id") or ""),
                "machineUid": str(device.get("machine_uid") or ""),
                "deviceName": str(device.get("device_name") or "Aria VM"),
            },
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, {"message": str(body.get("error") or "Realtime token request failed.")}
        return True, {
            "token": str(body.get("token") or ""),
            "uid": str(body.get("uid") or session.get("account_uid") or ""),
            "device_id": str(body.get("deviceId") or device.get("device_id") or ""),
            "machine_uid": str(body.get("machineUid") or device.get("machine_uid") or ""),
            "device_name": str(body.get("deviceName") or device.get("device_name") or "Aria VM"),
            "kind": str(body.get("kind") or kind or "device"),
        }

    def _sync_device(
        *,
        current_task_id: str,
        vm_status: str,
        remote_enabled: bool,
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
        device: dict[str, str],
    ) -> tuple[bool, dict[str, Any]]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token or not str(session.get("account_uid") or "").strip():
            return False, {"message": "No linked Control Tower session."}
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_DEVICE,
            auth_token=auth_token,
            payload={
                "d": str(device.get("device_id") or ""),
                "h": str(device.get("machine_uid") or ""),
                "n": str(device.get("device_name") or "Aria VM"),
                "s": "gtk_terminal",
                "t": str(current_task_id or "").strip(),
                "v": str(vm_status or "ready").strip() or "ready",
                "r": bool(remote_enabled),
                "c": {"remoteTerminal": True, "computerUse": True, "vmTools": True},
            },
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, {"message": str(body.get("error") or "Remote device sync failed.")}
        data = body.get("d") if isinstance(body.get("d"), dict) else {}
        return True, dict(data or {})

    def _sync_task(
        *,
        task_id: str,
        status: str,
        session_id: str,
        latest_summary: str,
        latest_result: str,
        error: str,
        spent_usd: float | None,
        usage: dict[str, Any] | None,
        event_text: str,
        event_kind: str,
        ack_message_ids: list[str] | None,
        latest_screenshot_data_url: str,
        latest_screenshot_captured_at: str,
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        http_upload_binary: HttpUploadBinary,
        decode_data_url: Callable[[str], tuple[str, bytes]],
        clear_account_session: Callable[[], None],
    ) -> tuple[bool, dict[str, Any]]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, {"message": "No local Control Tower session found."}
        payload: dict[str, Any] = {}
        if status:
            payload["v"] = str(status).strip()
        if session_id:
            payload["s"] = str(session_id).strip()
        if latest_summary:
            payload["y"] = str(latest_summary).strip()
        if latest_result:
            payload["r"] = str(latest_result).strip()
        if error:
            payload["e"] = str(error).strip()
        if spent_usd is not None:
            payload["$"] = float(spent_usd)
        if isinstance(usage, dict) and usage:
            payload["u"] = dict(usage)
        if event_text:
            payload["x"] = str(event_text).strip()
        if event_kind:
            payload["k"] = str(event_kind).strip()
        if ack_message_ids:
            payload["a"] = [str(value).strip() for value in ack_message_ids if str(value).strip()]
        if latest_screenshot_captured_at:
            payload["z"] = str(latest_screenshot_captured_at).strip()
        if latest_screenshot_data_url:
            try:
                content_type, binary = decode_data_url(latest_screenshot_data_url)
                ticket_status, ticket_body, _cookie = _runtime_call_local(
                    canonical=canonical,
                    http_json=http_json,
                    op=_OP_SCREENSHOT,
                    auth_token=auth_token,
                    payload={"t": str(task_id).strip(), "c": content_type},
                )
                ticket = dict(ticket_body.get("d") or {}) if isinstance(ticket_body, dict) else {}
                upload_url = str(ticket.get("u") or "").strip()
                upload_headers = dict(ticket.get("h") or {}) if isinstance(ticket.get("h"), dict) else {}
                if ticket_status < 400 and upload_url:
                    upload_status, _upload_body = http_upload_binary(
                        upload_url,
                        method="PUT",
                        data=binary,
                        headers={str(key): str(value) for key, value in upload_headers.items() if str(key).strip()},
                    )
                    if 200 <= upload_status < 300:
                        payload["i"] = str(ticket.get("r") or "").strip()
                        payload["p"] = str(ticket.get("p") or "").strip()
                    else:
                        return False, {"message": f"Remote screenshot upload failed ({upload_status})."}
                else:
                    return False, {"message": str(ticket_body.get("error") or "Remote screenshot ticket request failed.")}
            except Exception as exc:
                return False, {"message": f"Remote screenshot upload failed: {exc}"}
        status_code, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_SYNC,
            auth_token=auth_token,
            payload={"t": str(task_id).strip(), "p": payload},
        )
        if status_code >= 400 or not body.get("ok"):
            if status_code == 401:
                clear_account_session()
            return False, {"message": str(body.get("error") or "Remote task sync failed.")}
        data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
        return True, dict(data.get("t") or {})

    def _report_usage(
        *,
        payload: dict[str, Any],
        session_file,
        hub_url: str,
        load_json: JsonLoader,
        canonical_hub_url: Callable[[str | None], str],
        http_json: HttpJson,
        clear_account_session: Callable[[], None],
    ) -> tuple[bool, str]:
        session = load_json(session_file)
        canonical = canonical_hub_url(session.get("hub_url") or hub_url)
        auth_token = str(session.get("auth_token") or "").strip()
        if not auth_token:
            return False, "No local Control Tower session found."
        status, body, _cookie = _runtime_call_local(
            canonical=canonical,
            http_json=http_json,
            op=_OP_USAGE,
            auth_token=auth_token,
            payload={"p": payload},
        )
        if status >= 400 or not body.get("ok"):
            if status == 401:
                clear_account_session()
            return False, str(body.get("error") or "Task usage could not be reported.")
        data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
        return True, str(data.get("m") or "Task usage reported.")


def _legacy_name(*parts: str) -> str:
    return "".join(parts)


CONFIG_DIR = Path.home() / ".config" / "ariaos"
SESSION_FILE = CONFIG_DIR / "account_session.json"
LEGACY_SESSION_FILE = CONFIG_DIR / _legacy_name("control", "_", "tower", "_", "session", ".json")
DEVICE_FILE = CONFIG_DIR / "device_identity.json"
SECRETS_FILE = CONFIG_DIR / "user_secrets.json"
DATA_DIR = Path.home() / ".ariaos" / "data"
LEGACY_DB_PATH = DATA_DIR / "aria_history.db"
ANONYMOUS_DB_PATH = DATA_DIR / "aria_history_anonymous.db"
MEMORY_INDEX_DB_PATH = DATA_DIR / "aria_memory_index.db"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos"
BACKEND_PID_PATH = STATE_DIR / "backend.pid"
BACKEND_LOG_PATH = STATE_DIR / "backend.log"
CLIENT_UPDATE_DIR = STATE_DIR / "client-update"
HUB_URL = local_hub_url()
PUBLIC_HUB_URL = public_hub_url()
DEFAULT_REMOTE_URL = default_remote_ws()
CLIENT_INSTALL_DIR = Path(os.environ.get("ARIA_CLIENT_DIR") or "/opt/aria-client")
PACKAGED_APP_DIR = CLIENT_INSTALL_DIR / "share" / "AriaApp"
DEFAULT_APP_DIR = Path.home() / "Desktop" / "AriaApp"
APP_DIR = PACKAGED_APP_DIR if PACKAGED_APP_DIR.exists() else DEFAULT_APP_DIR
LOGO_PATH = APP_DIR / "logo5.png"
LEGACY_TERMINAL = APP_DIR / "run_terminal_aria.sh"
GTK_SHELL_LAUNCHER = Path.home() / ".local" / "bin" / "aria-shell-launch"
PACKAGED_GTK_SHELL_LAUNCHER = CLIENT_INSTALL_DIR / "bin" / "run_aria_shell_gtk.sh"
CONFIG_PATH = CONFIG_DIR / "runtime_config.json"
WORKSPACE_DIR = Path(os.environ.get("ARIA_CLIENT_WORKSPACE_DIR") or str(Path.home() / "AriaWorkspace"))
LOCAL_LINK_HOST = "127.0.0.1"
LOCAL_LINK_PORT = 8765
_LINK_SERVER_STARTED = False
LEGACY_LOCAL_URL_KEY = _legacy_name("backend", "_", "ws", "_", "url")
LEGACY_REMOTE_URL_KEY = _legacy_name("remote", "_", "gateway", "_", "ws", "_", "url")
LEGACY_HUB_URL_KEY = _legacy_name("control", "_", "tower", "_", "url")
LEGACY_AUTH_KEY = _legacy_name("session", "_", "cookie")
DEFAULT_VOICE_NAME = "marin"
DEFAULT_VOICE_LANGUAGE = "en"
SUPPORTED_OPENAI_VOICE_NAMES = (
    "marin",
    "cedar",
    "coral",
    "alloy",
    "ash",
    "ballad",
    "echo",
    "sage",
    "shimmer",
    "verse",
)
SUPPORTED_VOICE_LANGUAGES = ("en", "fr", "es", "de", "pt")

DEFAULT_CONFIG = {
    "local_url": "ws://127.0.0.1:8080",
    "remote_url": "",
    "hub_url": "",
    "voice_name": (
        os.environ.get("ARIA_VOICE_NAME")
        or os.environ.get("ARIA_VOICE_DEFAULT_NAME")
        or DEFAULT_VOICE_NAME
    ).strip().lower() or DEFAULT_VOICE_NAME,
    "voice_language": (
        os.environ.get("ARIA_VOICE_LANGUAGE")
        or DEFAULT_VOICE_LANGUAGE
    ).strip().lower() or DEFAULT_VOICE_LANGUAGE,
    "vision_mode": "local",
    "vision_local_start_cmd": "bash -lc 'curl -fsS http://192.168.0.242:8000/health >/dev/null 2>&1 || true'",
    "vision_local_stop_cmd": "bash -lc 'true'",
    "vision_cloud_parse_url": "",
    "max_transcript_messages": 20,
}

DEFAULT_OPENAI_MODEL = "gpt-5.4"
SUPPORTED_OPENAI_MODELS = {"gpt-5.4", "gpt-5.4-mini"}
MEMORY_EMBED_MODEL = "text-embedding-3-large"
MEMORY_FILE_NAME = "Aria Memory.md"
MEMORY_MAX_FILE_TOKENS = 12000
MEMORY_MAX_PROMPT_TOKENS = 1200
MEMORY_MAX_PLAYBOOKS = 12
MEMORY_MAX_RECENT_SUMMARIES = 200
MEMORY_MAX_ARCHIVE_LINES = 6
MEMORY_MAX_PREFERENCE_CHARS = 120
MEMORY_MAX_PLAYBOOK_CHARS = 220
MEMORY_MAX_SUMMARY_CHARS = 320
MEMORY_VECTOR_TOP_K = 4
MEMORY_VECTOR_MIN_SCORE = 0.18
MEMORY_SECTION_PLACEHOLDERS = {
    "User Preferences": "- Add stable preferences here. Only keep facts you want Aria to remember.",
    "Verified Playbooks": "- Add durable, verified fixes here. Keep each entry short and factual.",
    "Recent Useful Task Summaries": "- Recent completed work will appear here automatically.",
    "Archive Summary": "- Older summaries will be compacted here when the file grows too large.",
}
MEMORY_PLACEHOLDER_LINES = {value.lstrip("- ").strip() for value in MEMORY_SECTION_PLACEHOLDERS.values()}
MEMORY_REJECTED_SUMMARY_MARKERS = (
    "i'm blocked",
    "i am blocked",
    "request stopped",
    "failed",
    "unable to",
    "i can't",
    "i cannot",
    "could not",
    "error:",
    "connection issue",
    "need the exact name",
    "need the exact app name",
    "need the exact file",
    "need the installer",
    "pass me the installer",
    "pass me the file",
    "not found locally",
    "not available to install",
    "no tengo confirmación",
    "no encuentro ninguna app",
    "sigo necesitando",
    "me dices el nombre exacto",
    "me pases el instalador",
)
MEMORY_PACKED_MARKERS = (
    "m:",
    "l:",
    "q:",
    "n:",
    "verified playbooks:",
    "recent useful task summaries:",
)
MEMORY_SMALLTALK_MARKERS = (
    "hi",
    "hello",
    "hey",
    "salut",
    "bonjour",
    "comment tu vas",
    "how are you",
    "ça va",
    "ca va",
)
MEMORY_PLAYBOOK_RESULT_MARKERS = (
    "direct url",
    "direct inbox url",
    "directly",
    "workaround",
    "worked around",
    "bypass",
    "bypassed",
    "quick fix",
    "fallback",
    "fixed by",
    "resolved by",
    "worked by",
    "worked using",
    "worked with",
    "using the",
    "use ",
    "using ",
    "set ",
    "disable ",
    "disabled ",
    "switched to",
    "open https://",
    "opened https://",
)
MEMORY_PLAYBOOK_GOAL_MARKERS = (
    "fix",
    "bug",
    "issue",
    "problem",
    "error",
    "broken",
    "break",
    "loop",
    "blocked",
    "bloqué",
    "workaround",
    "contourner",
    "bypass",
    "cannot",
    "can't",
    "unable",
    "fails",
    "failing",
    "crash",
    "login",
)
MEMORY_ADMIN_MARKERS = (
    "memory",
    "remember",
    "preferences",
    "preference",
    "playbook",
    "playbooks",
    "summary",
    "summaries",
    "forget",
    "delete",
    "remove",
    "update memory",
    "long term memory",
    "long-term memory",
    "what do you know",
    "what do you remember",
    "list my",
)
UPDATE_MANIFEST_PATH = "/api/client-update/manifest"


def _normalize_openai_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if normalized in SUPPORTED_OPENAI_MODELS:
        return normalized
    return DEFAULT_OPENAI_MODEL


def _normalize_voice_name(voice_name: str | None) -> str:
    normalized = str(voice_name or "").strip().lower()
    if normalized in SUPPORTED_OPENAI_VOICE_NAMES:
        return normalized
    return DEFAULT_VOICE_NAME


def _normalize_voice_language(language: str | None) -> str:
    normalized = str(language or "").strip().lower()
    if normalized in SUPPORTED_VOICE_LANGUAGES:
        return normalized
    return DEFAULT_VOICE_LANGUAGE


def _approx_token_count(text: str) -> int:
    return max(0, len(str(text or "")) // 4)


def _desktop_dir() -> Path:
    config_path = Path.home() / ".config" / "user-dirs.dirs"
    if config_path.exists():
        try:
            for raw_line in config_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line.startswith("XDG_DESKTOP_DIR="):
                    continue
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                path = Path(value).expanduser()
                if path.exists():
                    return path
                return path
        except Exception:
            pass
    for candidate in (Path.home() / "Desktop", Path.home() / "Bureau"):
        if candidate.exists():
            return candidate
    return Path.home() / "Desktop"


def _long_term_memory_path() -> Path:
    return _desktop_dir() / MEMORY_FILE_NAME


def _memory_index_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return MEMORY_INDEX_DB_PATH


def _default_memory_document() -> str:
    return (
        "# Aria Memory\n\n"
        "## User Preferences\n"
        f"{MEMORY_SECTION_PLACEHOLDERS['User Preferences']}\n\n"
        "## Verified Playbooks\n"
        f"{MEMORY_SECTION_PLACEHOLDERS['Verified Playbooks']}\n\n"
        "## Recent Useful Task Summaries\n"
        f"{MEMORY_SECTION_PLACEHOLDERS['Recent Useful Task Summaries']}\n\n"
        "## Archive Summary\n"
        f"{MEMORY_SECTION_PLACEHOLDERS['Archive Summary']}\n"
    )


def _ensure_long_term_memory_file() -> Path:
    path = _long_term_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_default_memory_document(), encoding="utf-8")
    try:
        _compact_memory_file(path)
    except Exception:
        pass
    return path


def _parse_memory_sections(text: str) -> tuple[dict[str, list[str]], list[str]]:
    sections = {
        "User Preferences": [],
        "Verified Playbooks": [],
        "Recent Useful Task Summaries": [],
        "Archive Summary": [],
    }
    extras: list[str] = []
    current: str | None = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped == "# Aria Memory":
            continue
        if current is None and stripped == "":
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            current = title if title in sections else None
            if current is None:
                extras.append(line)
            continue
        if current is None:
            extras.append(line)
            continue
        sections[current].append(line)
    return sections, extras


def _memory_content_lines(lines: list[str], *, drop_failed: bool = False) -> list[str]:
    cleaned: list[str] = []
    for raw in lines:
        line = raw.strip()
        normalized = line.lstrip("- ").strip()
        if not line or line == "-":
            continue
        if normalized in MEMORY_PLACEHOLDER_LINES:
            continue
        if drop_failed:
            lowered = normalized.lower().replace("’", "'")
            if any(marker in lowered for marker in MEMORY_REJECTED_SUMMARY_MARKERS):
                continue
            if any(marker in lowered for marker in MEMORY_PACKED_MARKERS):
                continue
            if "->" in lowered:
                summary_text = lowered.split("->", 1)[1].strip()
                goal_text = lowered.split(":", 1)[1].split("->", 1)[0].strip() if ":" in lowered else lowered.split("->", 1)[0].strip()
                if any(marker in summary_text for marker in MEMORY_SMALLTALK_MARKERS) and len(summary_text) < 100:
                    continue
                if any(marker in goal_text for marker in MEMORY_SMALLTALK_MARKERS) and len(goal_text) < 80:
                    continue
        cleaned.append(raw)
    return cleaned


def _serialize_memory_sections(sections: dict[str, list[str]], extras: list[str] | None = None) -> str:
    parts = ["# Aria Memory", ""]
    for title in ("User Preferences", "Verified Playbooks", "Recent Useful Task Summaries", "Archive Summary"):
        parts.append(f"## {title}")
        body = _memory_content_lines(sections.get(title) or [])
        if body:
            parts.extend(body)
        else:
            parts.append(MEMORY_SECTION_PLACEHOLDERS[title])
        parts.append("")
    if extras:
        for line in extras:
            if line.strip():
                parts.append(line)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _normalize_memory_line(text: str, *, max_len: int = 280) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


def _memory_looks_like_smalltalk(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(text or "")).strip().lower().replace("’", "'")
    if not lowered:
        return True
    return any(marker in lowered for marker in MEMORY_SMALLTALK_MARKERS) and len(lowered) < 80


def _memory_signature(text: str) -> str:
    normalized = str(text or "").strip().lower().replace("’", "'")
    normalized = re.sub(r"^\-\s*", "", normalized)
    normalized = re.sub(r"^\d{4}-\d{2}-\d{2}:\s*", "", normalized)
    if "->" in normalized:
        normalized = normalized.split("->", 1)[1].strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _dedupe_memory_lines(lines: list[str]) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for raw in reversed(lines):
        signature = _memory_signature(raw)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        kept.append(raw)
    kept.reverse()
    return kept


def _derive_verified_playbook(goal: str, result_summary: str) -> str:
    goal_line = _normalize_memory_line(goal, max_len=120)
    result_line = _normalize_memory_line(result_summary.replace("OBJECTIVE ACHIEVED:", "").strip(), max_len=220)
    if not goal_line or not result_line:
        return ""
    goal_lower = goal_line.lower().replace("’", "'")
    result_lower = result_line.lower().replace("’", "'")
    if _memory_looks_like_smalltalk(goal_line) or _memory_looks_like_smalltalk(result_line):
        return ""
    has_goal_marker = any(marker in goal_lower for marker in MEMORY_PLAYBOOK_GOAL_MARKERS)
    has_result_marker = any(marker in result_lower for marker in MEMORY_PLAYBOOK_RESULT_MARKERS)
    has_strong_result_marker = any(
        marker in result_lower
        for marker in (
            "workaround",
            "worked around",
            "bypass",
            "bypassed",
            "quick fix",
            "fallback",
            "fixed by",
            "resolved by",
            "switched to",
            "disabled ",
        )
    )
    if not has_result_marker:
        return ""
    if not (has_goal_marker or has_strong_result_marker):
        return ""
    return f"- {goal_line}: {result_line}"


def _looks_like_durable_user_fact(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(text or "")).strip().lower().replace("’", "'")
    if not lowered or _memory_looks_like_smalltalk(lowered):
        return False
    if any(
        marker in lowered
        for marker in (
            "my name is ",
            "je m'appelle ",
            "me llamo ",
            "mi nombre es ",
            "i prefer ",
            "je préfère ",
            "je prefere ",
            "prefiero ",
            "prefiro ",
            "speak to me in ",
            "respond in ",
            "réponds en ",
            "reponds en ",
        )
    ):
        return True
    if re.search(r"\bj'ai\s+\d{1,3}\s+ans\b", lowered):
        return True
    if re.search(r"\b(?:i am|i'm)\s+\d{1,3}\s+(?:years?\s+old|yo)\b", lowered):
        return True
    if re.search(r"\b(?:tengo|tenho)\s+\d{1,3}\s+(?:años|anos)\b", lowered):
        return True
    if re.search(r"\bje suis\s+[a-zà-ÿ][a-zà-ÿ' -]{1,40}\b", lowered) and re.search(r"\bj'ai\s+\d{1,3}\s+ans\b", lowered):
        return True
    if re.search(r"\b(?:i am|i'm)\s+[a-z][a-z' -]{1,40}\b", lowered) and re.search(r"\b(?:i am|i'm)\s+\d{1,3}\s+(?:years?\s+old|yo)\b", lowered):
        return True
    return False


def _extract_durable_user_preference_lines(goal: str, transcript_messages: list[Any] | None = None) -> list[str]:
    candidates: list[str] = []
    goal_text = _normalize_memory_line(goal, max_len=MEMORY_MAX_PREFERENCE_CHARS)
    if goal_text:
        candidates.append(goal_text)
    for message in list(transcript_messages or [])[-8:]:
        role = str(getattr(message, "role", "") or "").strip().lower()
        if role != "user":
            continue
        content = _normalize_memory_line(getattr(message, "content", ""), max_len=MEMORY_MAX_PREFERENCE_CHARS)
        if content:
            candidates.append(content)
    lines: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not _looks_like_durable_user_fact(raw):
            continue
        signature = _memory_signature(raw)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        lines.append(raw)
    return lines[:4]


def _append_user_preference_lines(lines: list[str]) -> bool:
    if not lines:
        return False
    memory_path = _ensure_long_term_memory_file()
    try:
        sections, extras = _parse_memory_sections(memory_path.read_text(encoding="utf-8"))
    except Exception:
        sections, extras = _parse_memory_sections(_default_memory_document())
    current = _memory_section_lines(sections, "User Preferences")
    changed = False
    for raw in lines:
        line = _normalize_memory_line(raw, max_len=MEMORY_MAX_PREFERENCE_CHARS)
        if not line:
            continue
        current, added = _upsert_memory_line(current, line)
        changed = changed or added
    if not changed:
        return False
    sections["User Preferences"] = current
    memory_path.write_text(_serialize_memory_sections(sections, extras), encoding="utf-8")
    _compact_memory_file(memory_path)
    return True


def _memory_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9_@./:-]+", str(text or "").lower())
    return {word for word in words if len(word) >= 3}


def _memory_is_admin_prompt(text: str) -> bool:
    lowered = str(text or "").strip().lower().replace("’", "'")
    if not lowered:
        return False
    return any(marker in lowered for marker in MEMORY_ADMIN_MARKERS)


def _memory_section_lines(
    sections: dict[str, list[str]],
    title: str,
    *,
    drop_failed: bool = False,
    dedupe: bool = True,
) -> list[str]:
    lines = _memory_content_lines(sections.get(title) or [], drop_failed=drop_failed)
    return _dedupe_memory_lines(lines) if dedupe else lines


def _trim_memory_lines(lines: list[str], *, token_budget: int) -> list[str]:
    kept: list[str] = []
    for line in lines:
        trial = "\n".join(kept + [line]).strip()
        if _approx_token_count(trial) > token_budget:
            break
        kept.append(line)
    return kept


def _render_memory_snapshot(
    sections: dict[str, list[str]],
    *,
    preferences_limit: int | None = None,
    playbooks_limit: int | None = None,
    summaries_limit: int | None = None,
    include_archive: bool = False,
    token_budget: int = MEMORY_MAX_PROMPT_TOKENS,
) -> str:
    selected: list[str] = []

    preferences = _memory_section_lines(sections, "User Preferences")
    if preferences_limit is not None:
        preferences = preferences[:preferences_limit]
    if preferences:
        selected.append("User preferences:")
        selected.extend(f"- {line.lstrip('- ').strip()}" for line in preferences)

    playbooks = _memory_section_lines(sections, "Verified Playbooks")
    if playbooks_limit is not None:
        playbooks = playbooks[:playbooks_limit]
    if playbooks:
        selected.append("Verified playbooks:")
        selected.extend(f"- {line.lstrip('- ').strip()}" for line in playbooks)

    summaries = _memory_section_lines(sections, "Recent Useful Task Summaries", drop_failed=True)
    if summaries_limit is not None:
        summaries = summaries[:summaries_limit]
    if summaries:
        selected.append("Recent useful task summaries:")
        selected.extend(f"- {line.lstrip('- ').strip()}" for line in summaries)

    if include_archive:
        archive = _memory_section_lines(sections, "Archive Summary", drop_failed=True)
        if archive:
            selected.append("Archive summary:")
            selected.extend(f"- {line.lstrip('- ').strip()}" for line in archive)

    trimmed = _trim_memory_lines([line for line in selected if line.strip()], token_budget=token_budget)
    return "\n".join(trimmed).strip()


def _extract_memory_context(path: Path, user_prompt: str) -> str:
    try:
        sections, _extras = _parse_memory_sections(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if _memory_is_admin_prompt(user_prompt):
        lowered = str(user_prompt or "").lower()
        include_playbooks = any(token in lowered for token in ("playbook", "playbooks", "memory", "remember"))
        include_summaries = any(
            token in lowered
            for token in ("summary", "summaries", "memory", "remember", "what did", "what have", "history", "done")
        )
        return _render_memory_snapshot(
            sections,
            preferences_limit=None,
            playbooks_limit=None if include_playbooks else 2,
            summaries_limit=12 if include_summaries else 4,
            include_archive=include_summaries,
            token_budget=MEMORY_MAX_PROMPT_TOKENS,
        )
    runtime = RuntimeStore.remote_openai_runtime()
    keywords = _memory_keywords(user_prompt)
    selected: list[str] = []
    selected.extend(line.strip() for line in _memory_section_lines(sections, "User Preferences"))

    def choose_relevant(lines: list[str], limit: int) -> list[str]:
        scored: list[tuple[int, str]] = []
        for raw in _memory_content_lines(lines):
            line = raw.strip()
            lowered = line.lower()
            score = 0
            for keyword in keywords:
                if keyword in lowered:
                    score += 1
            if score > 0:
                scored.append((score, line))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [line for _score, line in scored[:limit]]

    playbooks = [line.strip() for line in _memory_section_lines(sections, "Verified Playbooks")]
    summaries = _retrieve_semantic_memory_summaries(path, user_prompt, runtime=runtime, limit=MEMORY_VECTOR_TOP_K)
    if not summaries:
        summaries = choose_relevant(_memory_section_lines(sections, "Recent Useful Task Summaries", drop_failed=True), 3)
    if not summaries:
        summaries = [line.strip() for line in _memory_section_lines(sections, "Recent Useful Task Summaries", drop_failed=True)][:2]

    if playbooks:
        selected.append("Verified playbooks:")
        selected.extend(f"- {line.lstrip('- ').strip()}" for line in playbooks)
    if summaries:
        selected.append("Recent useful task summaries:")
        selected.extend(f"- {line.lstrip('- ').strip()}" for line in summaries)

    archive_lines = [line.strip() for line in _memory_section_lines(sections, "Archive Summary", drop_failed=True)]
    if archive_lines:
        relevant_archive = choose_relevant(archive_lines, 1)
        if relevant_archive:
            summary_signatures = {_memory_signature(line) for line in summaries}
            archive_to_add = [line for line in relevant_archive if _memory_signature(line) not in summary_signatures]
            if archive_to_add:
                selected.append("Archive summary:")
                selected.extend(f"- {line.lstrip('- ').strip()}" for line in archive_to_add)

    text = "\n".join(line for line in selected if line.strip()).strip()
    if _approx_token_count(text) <= MEMORY_MAX_PROMPT_TOKENS:
        return text
    lines = text.splitlines()
    trimmed: list[str] = []
    for line in lines:
        trial = "\n".join(trimmed + [line]).strip()
        if _approx_token_count(trial) > MEMORY_MAX_PROMPT_TOKENS:
            break
        trimmed.append(line)
    return "\n".join(trimmed).strip()


def _compact_memory_file(path: Path) -> None:
    try:
        sections, extras = _parse_memory_sections(path.read_text(encoding="utf-8"))
    except Exception:
        return

    preferences = _memory_content_lines(sections["User Preferences"])
    sections["User Preferences"] = preferences or [MEMORY_SECTION_PLACEHOLDERS["User Preferences"]]

    playbooks = _dedupe_memory_lines(_memory_content_lines(sections["Verified Playbooks"]))
    sections["Verified Playbooks"] = playbooks[:MEMORY_MAX_PLAYBOOKS] or [MEMORY_SECTION_PLACEHOLDERS["Verified Playbooks"]]

    summaries = _dedupe_memory_lines(_memory_content_lines(sections["Recent Useful Task Summaries"], drop_failed=True))
    archive = _dedupe_memory_lines(_memory_content_lines(sections["Archive Summary"], drop_failed=True))
    if len(summaries) > MEMORY_MAX_RECENT_SUMMARIES:
        overflow = summaries[:-MEMORY_MAX_RECENT_SUMMARIES]
        summaries = summaries[-MEMORY_MAX_RECENT_SUMMARIES:]
        archive_line = "- Earlier completed work included: " + "; ".join(line.lstrip("- ").strip() for line in overflow[:6])
        archive.append(_normalize_memory_line(archive_line, max_len=360))
    while archive and len(archive) > MEMORY_MAX_ARCHIVE_LINES:
        archive = archive[-MEMORY_MAX_ARCHIVE_LINES:]
    sections["Recent Useful Task Summaries"] = summaries or [MEMORY_SECTION_PLACEHOLDERS["Recent Useful Task Summaries"]]
    sections["Archive Summary"] = archive or [MEMORY_SECTION_PLACEHOLDERS["Archive Summary"]]

    rendered = _serialize_memory_sections(sections, extras)
    if _approx_token_count(rendered) > MEMORY_MAX_FILE_TOKENS and len(sections["Recent Useful Task Summaries"]) > 1:
        recent_lines = _memory_content_lines(sections["Recent Useful Task Summaries"], drop_failed=True)
        archive = _memory_content_lines(sections["Archive Summary"], drop_failed=True)
        while recent_lines and _approx_token_count(rendered) > MEMORY_MAX_FILE_TOKENS:
            moved = recent_lines.pop(0)
            archive.append(_normalize_memory_line(f"- Archived: {moved.lstrip('- ').strip()}", max_len=360))
            archive = archive[-MEMORY_MAX_ARCHIVE_LINES:]
            sections["Recent Useful Task Summaries"] = recent_lines or [MEMORY_SECTION_PLACEHOLDERS["Recent Useful Task Summaries"]]
            sections["Archive Summary"] = archive or [MEMORY_SECTION_PLACEHOLDERS["Archive Summary"]]
            rendered = _serialize_memory_sections(sections, extras)

    path.write_text(rendered, encoding="utf-8")


def _memory_index_connect() -> sqlite3.Connection:
    path = _memory_index_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_embeddings (
            content_hash TEXT PRIMARY KEY,
            section TEXT NOT NULL,
            line_text TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _memory_summary_entries(path: Path) -> list[tuple[str, str, str]]:
    try:
        sections, _extras = _parse_memory_sections(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for section_name in ("Recent Useful Task Summaries", "Archive Summary"):
        for raw in _memory_section_lines(sections, section_name, drop_failed=True):
            line_text = raw.strip()
            content_hash = hashlib.sha256(f"{section_name}\n{line_text}".encode("utf-8")).hexdigest()
            entries.append((content_hash, section_name, line_text))
    return entries


def _memory_summary_entries_hash(entries: list[tuple[str, str, str]]) -> str:
    payload = "\n".join(f"{content_hash}|{section}|{line}" for content_hash, section, line in entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _embed_text_batch_json(runtime: dict[str, str], texts: list[str]) -> list[list[float]]:
    api_key = str(runtime.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Embedding API key is missing.")
    cleaned = [str(text or "").strip() for text in texts if str(text or "").strip()]
    if not cleaned:
        return []
    base_url = str(runtime.get("base_url") or default_model_api()).strip() or default_model_api()
    endpoint = f"{base_url.rstrip('/')}/embeddings"
    payload = {
        "model": MEMORY_EMBED_MODEL,
        "input": cleaned,
        "encoding_format": "float",
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(body or f"Embeddings HTTP {exc.code}") from exc
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("Embeddings returned no data.")
    ordered = sorted(
        (item for item in data if isinstance(item, dict)),
        key=lambda item: int(item.get("index") or 0),
    )
    vectors: list[list[float]] = []
    for item in ordered:
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError("Embeddings payload missing vector.")
        vectors.append([float(value) for value in embedding])
    if len(vectors) != len(cleaned):
        raise RuntimeError("Embeddings count mismatch.")
    return vectors


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _ensure_memory_index_synced(path: Path, runtime: dict[str, str] | None) -> bool:
    entries = _memory_summary_entries(path)
    source_hash = _memory_summary_entries_hash(entries)
    try:
        with _memory_index_connect() as conn:
            meta_rows = conn.execute("SELECT key, value FROM memory_index_meta").fetchall()
            meta = {str(row["key"]): str(row["value"]) for row in meta_rows}
            if (
                meta.get("summaries_hash") == source_hash
                and meta.get("embedding_model") == MEMORY_EMBED_MODEL
            ):
                return True
            if not runtime or not str(runtime.get("api_key") or "").strip():
                return False
            existing_rows = conn.execute("SELECT content_hash FROM summary_embeddings").fetchall()
            existing_hashes = {str(row["content_hash"]) for row in existing_rows}
            current_hashes = {content_hash for content_hash, _section, _line in entries}
            stale_hashes = sorted(existing_hashes - current_hashes)
            if stale_hashes:
                conn.executemany(
                    "DELETE FROM summary_embeddings WHERE content_hash = ?",
                    [(value,) for value in stale_hashes],
                )
            missing = [(content_hash, section, line) for content_hash, section, line in entries if content_hash not in existing_hashes]
            if missing:
                vectors = _embed_text_batch_json(runtime, [line for _hash, _section, line in missing])
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO summary_embeddings (content_hash, section, line_text, embedding_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (content_hash, section, line, json.dumps(vector), stamp)
                        for (content_hash, section, line), vector in zip(missing, vectors)
                    ],
                )
            conn.execute(
                "INSERT OR REPLACE INTO memory_index_meta (key, value) VALUES (?, ?)",
                ("summaries_hash", source_hash),
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_index_meta (key, value) VALUES (?, ?)",
                ("embedding_model", MEMORY_EMBED_MODEL),
            )
            conn.commit()
            return True
    except Exception:
        return False


def _retrieve_semantic_memory_summaries(
    path: Path,
    user_prompt: str,
    *,
    runtime: dict[str, str] | None,
    limit: int,
) -> list[str]:
    query = str(user_prompt or "").strip()
    if not query or not runtime or not str(runtime.get("api_key") or "").strip():
        return []
    if not _ensure_memory_index_synced(path, runtime):
        return []
    try:
        query_vector = _embed_text_batch_json(runtime, [query])[0]
    except Exception:
        return []
    keywords = _memory_keywords(query)
    scored: list[tuple[float, str]] = []
    try:
        with _memory_index_connect() as conn:
            rows = conn.execute(
                "SELECT section, line_text, embedding_json FROM summary_embeddings"
            ).fetchall()
    except Exception:
        return []
    for row in rows:
        line_text = str(row["line_text"] or "").strip()
        try:
            embedding = json.loads(str(row["embedding_json"] or "[]"))
        except Exception:
            continue
        if not isinstance(embedding, list):
            continue
        score = _cosine_similarity(query_vector, [float(value) for value in embedding])
        overlap = len(keywords & _memory_keywords(line_text))
        if overlap:
            score += min(0.12, overlap * 0.03)
        if str(row["section"] or "") == "Recent Useful Task Summaries":
            score += 0.02
        if score >= MEMORY_VECTOR_MIN_SCORE or overlap > 0:
            scored.append((score, line_text))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score = scored[0][0] if scored else 0.0
    cutoff = max(MEMORY_VECTOR_MIN_SCORE, best_score * 0.65)
    selected: list[str] = []
    seen: set[str] = set()
    for score, line_text in scored:
        if score < cutoff:
            continue
        signature = _memory_signature(line_text)
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(line_text)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def _append_long_term_task_summary(goal: str, result_summary: str) -> bool:
    memory_path = _ensure_long_term_memory_file()
    try:
        sections, extras = _parse_memory_sections(memory_path.read_text(encoding="utf-8"))
    except Exception:
        sections, extras = _parse_memory_sections(_default_memory_document())
    goal_line = _normalize_memory_line(goal, max_len=120)
    result_line = _normalize_memory_line(result_summary, max_len=220)
    if not goal_line or not result_line:
        return False
    lowered_result = result_line.lower().replace("’", "'")
    if any(marker in lowered_result for marker in MEMORY_REJECTED_SUMMARY_MARKERS):
        return False
    if result_line.endswith("?"):
        return False
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
    entry = f"- {stamp}: {goal_line} -> {result_line}"
    recent = _dedupe_memory_lines(_memory_content_lines(sections["Recent Useful Task Summaries"], drop_failed=True))
    changed = False
    if recent and recent[-1] == entry:
        recent_exists = True
    else:
        recent_exists = False
    if not recent_exists:
        recent.append(entry)
        sections["Recent Useful Task Summaries"] = recent
        changed = True
    playbook = _derive_verified_playbook(goal_line, result_line)
    if playbook:
        playbooks = _dedupe_memory_lines(_memory_content_lines(sections["Verified Playbooks"]))
        if playbook not in playbooks:
            playbooks.append(playbook)
            sections["Verified Playbooks"] = playbooks
            changed = True
    if not changed:
        return False
    memory_path.write_text(_serialize_memory_sections(sections, extras), encoding="utf-8")
    _compact_memory_file(memory_path)
    return True


def _memory_transcript_excerpt(transcript_messages: list[Any], *, limit: int = 8) -> str:
    lines: list[str] = []
    for message in transcript_messages[-limit:]:
        role = str(getattr(message, "role", "") or "").strip().lower()
        content = re.sub(r"\s+", " ", str(getattr(message, "content", "") or "")).strip()
        if not role or not content:
            continue
        lines.append(f"{role}: {_normalize_memory_line(content, max_len=220)}")
    return "\n".join(lines).strip()


def _openai_chat_json(
    *,
    runtime: dict[str, str],
    messages: list[dict[str, str]],
    max_completion_tokens: int = 260,
) -> dict[str, Any]:
    api_key = str(runtime.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("OpenAI API key is missing.")
    base_url = str(runtime.get("base_url") or default_model_api()).strip() or default_model_api()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": _normalize_openai_model(runtime.get("model")),
        "messages": messages,
        "temperature": 0,
        "max_completion_tokens": int(max_completion_tokens),
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(body or f"OpenAI HTTP {exc.code}") from exc
    choices = raw.get("choices") if isinstance(raw, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI returned no choices.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = str((message or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("OpenAI returned empty JSON content.")
    return json.loads(content)


def _memory_notice_fallback(action: str, *, changed: bool) -> str:
    if not changed or action == "none":
        return "Long-term memory not updated."
    if action == "user_preference":
        return "Saved to long-term memory as a preference."
    if action == "verified_playbook":
        return "Saved to long-term memory as a verified playbook."
    if action == "useful_task_summary":
        return "Saved to long-term memory as a useful summary."
    if action == "remove_memory_item":
        return "Removed from long-term memory."
    if action == "update_memory_item":
        return "Updated long-term memory."
    if action == "compact_summaries":
        return "Compacted long-term memory summaries."
    return "Long-term memory updated."


def _memory_match_index(lines: list[str], match_text: str) -> int:
    target = _memory_signature(match_text)
    if not target:
        return -1
    best_index = -1
    best_score = -1
    target_words = _memory_keywords(target)
    for index, raw in enumerate(lines):
        signature = _memory_signature(raw)
        if not signature:
            continue
        score = 0
        if target in signature or signature in target:
            score += 10
        overlap = len(target_words & _memory_keywords(signature))
        score += overlap
        if score > best_score:
            best_score = score
            best_index = index
    return best_index if best_score > 0 else -1


def _upsert_memory_line(lines: list[str], content: str) -> tuple[list[str], bool]:
    normalized = _normalize_memory_line(content)
    if not normalized:
        return lines, False
    cleaned = _dedupe_memory_lines(list(lines))
    signatures = {_memory_signature(line) for line in cleaned}
    if _memory_signature(normalized) in signatures:
        return cleaned, False
    cleaned.append(f"- {normalized.lstrip('- ').strip()}")
    return cleaned, True


def _update_memory_line(lines: list[str], match_text: str, content: str) -> tuple[list[str], bool]:
    index = _memory_match_index(lines, match_text)
    normalized = _normalize_memory_line(content)
    if index < 0 or not normalized:
        return lines, False
    updated = list(lines)
    updated[index] = f"- {normalized.lstrip('- ').strip()}"
    return _dedupe_memory_lines(updated), True


def _remove_memory_line(lines: list[str], match_text: str) -> tuple[list[str], bool]:
    index = _memory_match_index(lines, match_text)
    if index < 0:
        return lines, False
    updated = list(lines)
    updated.pop(index)
    return _dedupe_memory_lines(updated), True


def _llm_compact_recent_summaries(path: Path, runtime: dict[str, str]) -> bool:
    try:
        sections, extras = _parse_memory_sections(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    summaries = _memory_section_lines(sections, "Recent Useful Task Summaries", drop_failed=True)
    if len(summaries) <= MEMORY_MAX_RECENT_SUMMARIES:
        return False
    overflow = summaries[:-MEMORY_MAX_RECENT_SUMMARIES]
    if not overflow:
        return False
    prompt_lines = "\n".join(f"- {line.lstrip('- ').strip()}" for line in overflow[:12])
    try:
        payload = _openai_chat_json(
            runtime=runtime,
            max_completion_tokens=180,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You compact old long-term memory summaries. "
                        "Return JSON only with key archive_lines as a list of 1 to 3 factual lines. "
                        "Each line must be short, durable, and under 160 characters. "
                        "Do not invent anything."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Compact these old useful task summaries into at most 3 archive lines.\n\n"
                        f"{prompt_lines}"
                    ),
                },
            ],
        )
    except Exception:
        return False
    archive_lines_raw = payload.get("archive_lines")
    if not isinstance(archive_lines_raw, list):
        return False
    archive_lines = [
        f"- {_normalize_memory_line(str(item or ''), max_len=160)}"
        for item in archive_lines_raw[:3]
        if _normalize_memory_line(str(item or ''), max_len=160)
    ]
    if not archive_lines:
        return False
    archive = _memory_section_lines(sections, "Archive Summary", drop_failed=True)
    archive.extend(archive_lines)
    sections["Archive Summary"] = _dedupe_memory_lines(archive)[-MEMORY_MAX_ARCHIVE_LINES:]
    sections["Recent Useful Task Summaries"] = summaries[-MEMORY_MAX_RECENT_SUMMARIES:]
    path.write_text(_serialize_memory_sections(sections, extras), encoding="utf-8")
    _compact_memory_file(path)
    return True


def _to_build_number(value: Any) -> int:
    digits = re.sub(r"\D+", "", str(value or "").strip())
    if not digits:
        return 0
    try:
        return max(0, int(digits))
    except Exception:
        return 0


def _client_version_path() -> Path:
    return PACKAGED_APP_DIR / "version.json"


def _client_release_info() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    path = _client_version_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
    build = _to_build_number(
        os.environ.get("ARIA_CLIENT_BUILD")
        or payload.get("build")
        or payload.get("build_number")
        or payload.get("buildNumber")
    )
    version = str(
        os.environ.get("ARIA_CLIENT_VERSION")
        or payload.get("version")
        or payload.get("app_version")
        or payload.get("appVersion")
        or (str(build) if build else "dev")
    ).strip() or "dev"
    return {
        "version": version,
        "build": build,
        "commit": str(payload.get("commit") or "").strip(),
    }


def _clear_update_gate_fields(payload: dict[str, Any]) -> bool:
    changed = False
    for key, fallback in (
        ("update_required", False),
        ("update_message", ""),
        ("update_download_url", ""),
        ("latest_version", ""),
        ("min_supported_build", 0),
        ("latest_build", 0),
    ):
        if payload.get(key) != fallback:
            payload[key] = fallback
            changed = True
    return changed


def _persist_update_gate(
    *,
    required: bool,
    message: str = "",
    download_url: str = "",
    latest_version: str = "",
    min_supported_build: int = 0,
    latest_build: int = 0,
) -> None:
    session = _load_json(SESSION_FILE)
    if not session and not required:
        return
    if required:
        session["update_required"] = True
        session["update_message"] = str(message or "").strip()
        session["update_download_url"] = str(download_url or "").strip()
        session["latest_version"] = str(latest_version or "").strip()
        session["min_supported_build"] = int(max(0, min_supported_build or 0))
        session["latest_build"] = int(max(0, latest_build or 0))
    else:
        _clear_update_gate_fields(session)
    _save_json(SESSION_FILE, session)


def _apply_update_gate_from_body(body: dict[str, Any] | None, *, clear_on_success: bool = False) -> None:
    payload = body if isinstance(body, dict) else {}
    code = str(payload.get("code") or "").strip().lower()
    details = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    if code == "upgrade_required" or bool(details.get("required")):
        _persist_update_gate(
            required=True,
            message=str(payload.get("error") or details.get("message") or "").strip(),
            download_url=str(details.get("downloadUrl") or "").strip(),
            latest_version=str(details.get("latestVersion") or "").strip(),
            min_supported_build=_to_build_number(details.get("minSupportedBuild")),
            latest_build=_to_build_number(details.get("latestBuild")),
        )
    elif clear_on_success:
        _persist_update_gate(required=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_binary(url: str, destination: Path, *, progress: Callable[[str], None] | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if progress and total > 0:
                    progress(f"Downloading update… {min(99, int((written / total) * 100))}%")
    if not destination.exists() or destination.stat().st_size <= 0:
        raise RuntimeError(f"Downloaded file is empty: {destination.name}")


def _download_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw or "{}")
    except Exception as exc:
        raise RuntimeError(f"Invalid update manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid update manifest payload.")
    return payload


def _sudo_run(password: str, args: list[str]) -> None:
    proc = subprocess.run(
        ["sudo", "-S", "-p", "", *args],
        input=(str(password or "") + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"sudo {' '.join(args)} failed")


def _client_bundle_import_smoke_test() -> None:
    env = dict(os.environ)
    env["ARIA_CLIENT_DIR"] = str(CLIENT_INSTALL_DIR)
    env["ARIA_CLIENT_WORKSPACE_DIR"] = str(WORKSPACE_DIR)
    py_path = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = f"{CLIENT_INSTALL_DIR / 'lib' / '.bundle.dat'}{os.pathsep + py_path if py_path else ''}"
    proc = subprocess.run(
        [
            "python3",
            "-c",
            "import importlib; importlib.import_module('aria_shell_gtk'); importlib.import_module('aria_shell_gtk.app')",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Updated AriaOS client failed the import smoke test.")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass




def _normalize_session_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    session = dict(payload or {})
    if not session:
        return {}, False

    changed = False

    auth_token = str(session.get("auth_token") or "").strip()
    legacy_auth_token = str(session.get(LEGACY_AUTH_KEY) or "").strip()
    if not auth_token and legacy_auth_token:
        session["auth_token"] = legacy_auth_token
        changed = True

    hub_url = str(session.get("hub_url") or "").strip()
    legacy_hub_url = str(session.get(LEGACY_HUB_URL_KEY) or "").strip()
    if not hub_url and legacy_hub_url:
        session["hub_url"] = legacy_hub_url
        changed = True

    raw_account_uid = session.get("account_uid")
    if isinstance(raw_account_uid, str):
        compact_uid = raw_account_uid.strip()
        if compact_uid.startswith("{") and compact_uid.endswith("}"):
            try:
                parsed_uid = ast.literal_eval(compact_uid)
            except Exception:
                parsed_uid = None
            if isinstance(parsed_uid, dict):
                resolved_uid = str(parsed_uid.get("i") or parsed_uid.get("uid") or "").strip()
                resolved_email = str(parsed_uid.get("e") or parsed_uid.get("email") or "").strip()
                if resolved_uid and resolved_uid != compact_uid:
                    session["account_uid"] = resolved_uid
                    changed = True
                if resolved_email and not str(session.get("account_email") or "").strip():
                    session["account_email"] = resolved_email
                    changed = True

    return session, changed


def _load_session_json() -> dict[str, Any]:
    payload = _load_json(SESSION_FILE)
    source = SESSION_FILE
    if not payload:
        payload = _load_json(LEGACY_SESSION_FILE)
        source = LEGACY_SESSION_FILE
    normalized, changed = _normalize_session_payload(payload)
    if normalized and bool(normalized.get("update_required")):
        min_supported_build = _to_build_number(normalized.get("min_supported_build"))
        if min_supported_build > 0:
            local_build = int(_client_release_info().get("build") or 0)
            if local_build >= min_supported_build:
                changed = _clear_update_gate_fields(normalized) or changed
    if normalized and (changed or source == LEGACY_SESSION_FILE):
        _save_session_json(normalized)
    return normalized


def _save_session_json(payload: dict[str, Any]) -> None:
    _save_json(SESSION_FILE, payload)
    if LEGACY_SESSION_FILE.exists():
        try:
            LEGACY_SESSION_FILE.unlink()
        except Exception:
            pass


def _load_account_payload(_path=None) -> dict[str, Any]:
    return _load_session_json()


def _save_account_payload(_path, payload: dict[str, Any]) -> None:
    _save_session_json(payload)


def _split_secret_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    sensitive = {}
    plain = dict(payload or {})
    for field in sensitive_fields():
        if field in plain:
            sensitive[field] = str(plain.pop(field) or "")
    return plain, sensitive


def _merge_local_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload or {})
    for field in sensitive_fields():
        value = get_secret(field)
        if value:
            merged[field] = value
    return merged


def _migrate_sensitive_fields_from_json() -> None:
    if not SECRETS_FILE.exists():
        return
    payload = _load_json(SECRETS_FILE)
    if not payload:
        return
    changed = False
    for field in sensitive_fields():
        current = str(payload.get(field) or "").strip()
        if not current:
            continue
        if get_secret(field):
            payload.pop(field, None)
            changed = True
            continue
        label = "AriaOS OpenAI API Key" if field == "openai_api_key" else "AriaOS VM Admin Password"
        if set_secret(field, current, label=label):
            payload.pop(field, None)
            changed = True
    if changed:
        _save_json(SECRETS_FILE, payload)


def _extract_auth_token(headers) -> str | None:
    values = []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Set-Cookie") or []
    else:
        single = headers.get("Set-Cookie")
        if single:
            values = [single]
    for item in values:
        cookie = SimpleCookie()
        try:
            cookie.load(item)
        except Exception:
            continue
        morsel = cookie.get("aria_session")
        if morsel is not None:
            return morsel.value
    return None


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    auth_token: str | None = None,
) -> tuple[int, dict[str, Any], str | None]:
    data = None
    headers = {"Accept": "application/json"}
    release = _client_release_info()
    headers["X-Aria-Client-Version"] = str(release.get("version") or "dev")
    headers["X-Aria-Client-Build"] = str(int(release.get("build") or 0))
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth_token:
        headers["Cookie"] = f"aria_session={auth_token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            body = json.loads(raw or "{}")
            _apply_update_gate_from_body(body, clear_on_success=False)
            return response.status, body, _extract_auth_token(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {"error": raw or f"HTTP {exc.code}"}
        _apply_update_gate_from_body(body, clear_on_success=False)
        return exc.code, body, _extract_auth_token(exc.headers)


def _http_upload_binary(
    url: str,
    *,
    method: str = "PUT",
    data: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    value = str(data_url or "").strip()
    match = re.match(r"^data:([^;]+);base64,(.+)$", value)
    if not match:
        raise ValueError("Invalid screenshot data URL.")
    return str(match.group(1) or "image/png").strip() or "image/png", base64.b64decode(match.group(2) or "")


def _canonical_hub_url(url: str | None) -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return PUBLIC_HUB_URL
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip().lower()
    if host in {"127.0.0.1", "localhost", "10.0.2.2"}:
        return PUBLIC_HUB_URL
    return raw


def load_runtime_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    legacy_config_path = APP_DIR / "aria_terminal_config.json"
    if legacy_config_path.exists():
        try:
            legacy = json.loads(legacy_config_path.read_text(encoding="utf-8"))
            if isinstance(legacy, dict):
                cfg.update(legacy)
        except Exception:
            pass
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not str(cfg.get("local_url") or "").strip() and str(cfg.get(LEGACY_LOCAL_URL_KEY) or "").strip():
        cfg["local_url"] = str(cfg.get(LEGACY_LOCAL_URL_KEY) or "").strip()
    if not str(cfg.get("remote_url") or "").strip() and str(cfg.get(LEGACY_REMOTE_URL_KEY) or "").strip():
        cfg["remote_url"] = str(cfg.get(LEGACY_REMOTE_URL_KEY) or "").strip()
    if not str(cfg.get("hub_url") or "").strip() and str(cfg.get(LEGACY_HUB_URL_KEY) or "").strip():
        cfg["hub_url"] = str(cfg.get(LEGACY_HUB_URL_KEY) or "").strip()
    cfg["remote_url"] = ""
    cfg["hub_url"] = ""
    cfg["vision_cloud_parse_url"] = ""
    return cfg


@dataclass
class RuntimeState:
    account_email: str
    account_uid: str
    plan_status: str
    plan_key: str
    api_key_ready: bool
    openai_model: str
    hub_url: str
    logo_path: Path
    history_db_path: Path
    local_url: str
    remote_url: str
    max_transcript_messages: int
    vision_mode: str
    vision_local_start_cmd: str
    vision_local_stop_cmd: str
    vision_cloud_parse_url: str
    auth_ready: bool
    plan_name: str
    activation_notice_plan: str | None
    prompts_remaining: int | None
    device_id: str
    device_name: str
    machine_uid: str
    voice_name: str
    voice_language: str
    app_version: str
    app_build: int
    update_required: bool
    update_message: str
    update_download_url: str
    latest_version: str
    min_supported_build: int
    latest_build: int


class RuntimeStore:
    @staticmethod
    def _open_url(url: str) -> bool:
        env = dict(os.environ)
        for command in (["gio", "open", url], ["xdg-open", url]):
            try:
                subprocess.Popen(command, env=env, start_new_session=True)
                return True
            except FileNotFoundError:
                continue
            except Exception:
                continue
        try:
            return bool(webbrowser.open(url, new=2))
        except Exception:
            return False

    @staticmethod
    def _device_identity() -> dict[str, str]:
        payload = _load_json(DEVICE_FILE)
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            device_id = f"dev_{uuid.uuid4().hex}"
        machine_uid = str(payload.get("machine_uid") or "").strip()
        if not machine_uid:
            machine_uid = f"machine_{uuid.uuid4().hex}"
        device_name = (
            str(payload.get("device_name") or "").strip()
            or str(os.environ.get("ARIA_DEVICE_NAME") or "").strip()
            or socket.gethostname().strip()
            or "Aria VM"
        )
        normalized = {
            "device_id": device_id,
            "machine_uid": machine_uid,
            "device_name": device_name[:120] or "Aria VM",
        }
        if normalized != payload:
            _save_json(DEVICE_FILE, normalized)
        return normalized

    @staticmethod
    def backend_ws_reachable(host: str = "127.0.0.1", port: int = 8080, timeout: float = 0.4) -> bool:
        sock = socket.socket()
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def read(self) -> RuntimeState:
        _migrate_sensitive_fields_from_json()
        secrets = self.user_secrets()
        config = load_runtime_config()
        device = self._device_identity()
        runtime = self.remote_openai_runtime()
        release = _client_release_info()
        api_key_ready = bool(str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip())
        return RuntimeState(
            account_email="Local Community",
            account_uid="",
            plan_status="active",
            plan_key="community",
            api_key_ready=api_key_ready,
            openai_model=str(runtime.get("model") or DEFAULT_OPENAI_MODEL),
            hub_url="",
            logo_path=LOGO_PATH,
            history_db_path=ANONYMOUS_DB_PATH,
            local_url=str(config.get("local_url") or config.get(LEGACY_LOCAL_URL_KEY) or DEFAULT_CONFIG["local_url"]),
            remote_url="",
            max_transcript_messages=int(config.get("max_transcript_messages") or DEFAULT_CONFIG["max_transcript_messages"]),
            vision_mode=str(config.get("vision_mode") or DEFAULT_CONFIG["vision_mode"]),
            vision_local_start_cmd=str(config.get("vision_local_start_cmd") or DEFAULT_CONFIG["vision_local_start_cmd"]),
            vision_local_stop_cmd=str(config.get("vision_local_stop_cmd") or DEFAULT_CONFIG["vision_local_stop_cmd"]),
            vision_cloud_parse_url=str(config.get("vision_cloud_parse_url") or DEFAULT_CONFIG["vision_cloud_parse_url"]),
            auth_ready=api_key_ready,
            plan_name="Community",
            activation_notice_plan=None,
            prompts_remaining=None,
            device_id=str(device.get("device_id") or ""),
            device_name=str(device.get("device_name") or "Aria VM"),
            machine_uid=str(device.get("machine_uid") or ""),
            voice_name=_normalize_voice_name(config.get("voice_name") or DEFAULT_CONFIG["voice_name"]),
            voice_language=_normalize_voice_language(config.get("voice_language") or DEFAULT_CONFIG["voice_language"]),
            app_version=str(release.get("version") or "dev"),
            app_build=int(release.get("build") or 0),
            update_required=False,
            update_message="",
            update_download_url="",
            latest_version="",
            min_supported_build=0,
            latest_build=0,
        )

    @staticmethod
    def launch_legacy_terminal() -> None:
        subprocess.Popen([str(LEGACY_TERMINAL)], start_new_session=True)

    @staticmethod
    def launch_shell_window(view: str) -> None:
        launcher = GTK_SHELL_LAUNCHER if GTK_SHELL_LAUNCHER.exists() else PACKAGED_GTK_SHELL_LAUNCHER
        if not launcher.exists():
            if view == "terminal":
                RuntimeStore.launch_legacy_terminal()
            return
        env = dict(os.environ)
        env["ARIA_SHELL_START_VIEW"] = view
        subprocess.Popen([str(launcher)], env=env, start_new_session=True)

    @staticmethod
    def open_account() -> None:
        RuntimeStore.open_browser()

    @staticmethod
    def open_billing() -> bool:
        return RuntimeStore.open_browser()

    @staticmethod
    def open_download(url: str | None = None) -> bool:
        target = str(url or "").strip() or "about:blank"
        return RuntimeStore._open_url(target)

    @staticmethod
    def fetch_client_update_manifest(base_url: str | None = None) -> tuple[bool, dict[str, Any], str]:
        del base_url
        return False, {}, "Hosted update checks are disabled in AriaOS Community."

    @staticmethod
    def refresh_client_update_status(base_url: str | None = None) -> tuple[bool, bool, str]:
        del base_url
        _persist_update_gate(required=False)
        return True, False, "Hosted update checks are disabled in AriaOS Community."

    @staticmethod
    def apply_client_update(
        *,
        vm_admin_password: str | None = None,
        base_url: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        del vm_admin_password, base_url, progress
        _persist_update_gate(required=False)
        return False, "Client updates are disabled in AriaOS Community."

    @staticmethod
    def open_account_signin(base_url: str | None = None) -> None:
        hub_url = _canonical_hub_url(base_url or HUB_URL)
        next_path = quote("/login?next=/connect-vm", safe="")
        RuntimeStore._open_url(f"{hub_url}/logout-vm?next={next_path}")

    @staticmethod
    def open_google_connect(base_url: str | None = None) -> None:
        hub_url = _canonical_hub_url(base_url or HUB_URL)
        next_path = quote("/connect-vm", safe="")
        RuntimeStore._open_url(f"{hub_url}/login?next={next_path}&provider=google")

    @staticmethod
    def open_browser() -> None:
        RuntimeStore._open_url("about:blank")

    @staticmethod
    def open_files() -> None:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["thunar", str(WORKSPACE_DIR)], start_new_session=True)

    @staticmethod
    def ensure_long_term_memory_file() -> str:
        return str(_ensure_long_term_memory_file())

    @staticmethod
    def open_long_term_memory() -> bool:
        path = _ensure_long_term_memory_file()
        return RuntimeStore._open_url(str(path)) or RuntimeStore._open_url(f"file://{path}")

    @staticmethod
    def read_long_term_memory() -> str:
        path = _ensure_long_term_memory_file()
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return _default_memory_document()

    @staticmethod
    def long_term_memory_snapshot(user_prompt: str = "") -> str:
        path = _ensure_long_term_memory_file()
        return _extract_memory_context(path, str(user_prompt or ""))

    @staticmethod
    def apply_long_term_memory_action(action: dict[str, Any], *, runtime: dict[str, str] | None = None) -> dict[str, Any]:
        normalized_action = str((action or {}).get("action") or "none").strip().lower() or "none"
        notice = _normalize_memory_line(str((action or {}).get("user_notice") or ""), max_len=160)
        path = _ensure_long_term_memory_file()
        try:
            sections, extras = _parse_memory_sections(path.read_text(encoding="utf-8"))
        except Exception:
            sections, extras = _parse_memory_sections(_default_memory_document())

        changed = False
        target_section = str((action or {}).get("section") or "").strip()
        target_section = target_section if target_section in sections else ""
        content = str((action or {}).get("content") or "").strip()
        match_text = str((action or {}).get("match_text") or (action or {}).get("matchText") or "").strip()

        if normalized_action == "user_preference":
            line = _normalize_memory_line(content, max_len=MEMORY_MAX_PREFERENCE_CHARS)
            current = _memory_section_lines(sections, "User Preferences")
            current, changed = _upsert_memory_line(current, line)
            sections["User Preferences"] = current
        elif normalized_action == "verified_playbook":
            line = _normalize_memory_line(content, max_len=MEMORY_MAX_PLAYBOOK_CHARS)
            current = _memory_section_lines(sections, "Verified Playbooks")
            current, changed = _upsert_memory_line(current, line)
            sections["Verified Playbooks"] = current
        elif normalized_action == "useful_task_summary":
            line = _normalize_memory_line(content, max_len=MEMORY_MAX_SUMMARY_CHARS)
            if line:
                stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
                entry = f"- {stamp}: {line}"
                current = _memory_section_lines(sections, "Recent Useful Task Summaries", drop_failed=True)
                if not current or current[-1] != entry:
                    current.append(entry)
                    sections["Recent Useful Task Summaries"] = _dedupe_memory_lines(current)
                    changed = True
        elif normalized_action == "remove_memory_item":
            candidate_sections = [target_section] if target_section else list(sections.keys())
            for section_name in candidate_sections:
                current = _memory_section_lines(
                    sections,
                    section_name,
                    drop_failed=(section_name in {"Recent Useful Task Summaries", "Archive Summary"}),
                )
                updated, removed = _remove_memory_line(current, match_text)
                if removed:
                    sections[section_name] = updated
                    changed = True
                    break
        elif normalized_action == "update_memory_item":
            candidate_sections = [target_section] if target_section else list(sections.keys())
            max_len = MEMORY_MAX_SUMMARY_CHARS
            if target_section == "User Preferences":
                max_len = MEMORY_MAX_PREFERENCE_CHARS
            elif target_section == "Verified Playbooks":
                max_len = MEMORY_MAX_PLAYBOOK_CHARS
            next_line = _normalize_memory_line(content, max_len=max_len)
            for section_name in candidate_sections:
                current = _memory_section_lines(
                    sections,
                    section_name,
                    drop_failed=(section_name in {"Recent Useful Task Summaries", "Archive Summary"}),
                )
                updated, replaced = _update_memory_line(current, match_text, next_line)
                if replaced:
                    sections[section_name] = updated
                    changed = True
                    break
        elif normalized_action == "compact_summaries":
            if runtime:
                changed = _llm_compact_recent_summaries(path, runtime)
            if not changed:
                _compact_memory_file(path)
                changed = True

        path.write_text(_serialize_memory_sections(sections, extras), encoding="utf-8")
        if runtime and normalized_action != "compact_summaries":
            _llm_compact_recent_summaries(path, runtime)
        _compact_memory_file(path)
        if runtime:
            _ensure_memory_index_synced(path, runtime)
        return {
            "action": normalized_action,
            "changed": bool(changed),
            "user_notice": notice or _memory_notice_fallback(normalized_action, changed=bool(changed)),
            "path": str(path),
        }

    @staticmethod
    def record_long_term_task_summary(goal: str, result_summary: str) -> None:
        _append_long_term_task_summary(goal, result_summary)

    @staticmethod
    def decide_long_term_memory_action(
        goal: str,
        result_summary: str,
        transcript_messages: list[Any] | None = None,
    ) -> dict[str, Any]:
        runtime = RuntimeStore.remote_openai_runtime()
        local_preference_lines = _extract_durable_user_preference_lines(goal, transcript_messages)
        if not str(runtime.get("api_key") or "").strip():
            preference_changed = _append_user_preference_lines(local_preference_lines)
            summary_changed = _append_long_term_task_summary(goal, result_summary)
            if preference_changed or summary_changed:
                notice = "Saved to long-term memory."
                action = "useful_task_summary" if summary_changed else "user_preference"
                if preference_changed and not summary_changed:
                    notice = "Saved to long-term memory as a preference."
                elif summary_changed and not preference_changed:
                    notice = "Saved to long-term memory as a useful summary."
                return {
                    "action": action,
                    "changed": True,
                    "user_notice": notice,
                    "reason": "local_memory_fallback_without_api_key",
                    "memory_context": _extract_memory_context(_ensure_long_term_memory_file(), goal),
                }
            return {
                "action": "none",
                "changed": False,
                "user_notice": "Long-term memory not updated.",
                "reason": "missing_api_key",
            }
        memory_path = _ensure_long_term_memory_file()
        memory_text = RuntimeStore.read_long_term_memory()
        transcript_excerpt = _memory_transcript_excerpt(list(transcript_messages or []))
        try:
            decision = _openai_chat_json(
                runtime=runtime,
                max_completion_tokens=280,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You decide long-term memory updates for AriaOS. "
                            "Return JSON only. "
                            "Choose exactly one action from: none, user_preference, verified_playbook, useful_task_summary, "
                            "remove_memory_item, update_memory_item, compact_summaries. "
                            "For completed tasks, default to useful_task_summary. "
                            "Use none only for empty outputs, obvious duplicates, pure smalltalk, or explicitly temporary chatter. "
                            "Use user_preference for any durable user preference, recurring instruction, preferred tool, language, style, workflow, or profile fact. "
                            "Use verified_playbook for any reusable method, successful workaround, bug bypass, reliable fix, or sequence that helped complete the task. "
                            "Use useful_task_summary for every other completed task outcome so long-term memory keeps a record of completed work. "
                            "Do not refuse to save a completed task just because it seems ordinary. "
                            "If the user clearly wants to forget, delete, or correct something in long-term memory, use remove_memory_item or update_memory_item. "
                            "Keep content very short. Hard limits: user_preference <= 120 chars, verified_playbook <= 220 chars, useful_task_summary <= 320 chars. "
                            "The user_notice must be short, natural, and in the user's language. "
                            "If action is none, user_notice must briefly say that long-term memory was not updated."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"CURRENT USER GOAL:\n{_normalize_memory_line(goal, max_len=500)}\n\n"
                            f"ASSISTANT FINAL ANSWER:\n{_normalize_memory_line(result_summary, max_len=700)}\n\n"
                            f"RECENT TRANSCRIPT:\n{transcript_excerpt or '(empty)'}\n\n"
                            f"CURRENT LONG-TERM MEMORY:\n{memory_text[:12000]}\n\n"
                            "Return JSON with keys: action, section, content, match_text, user_notice, reason."
                        ),
                    },
                ],
            )
        except Exception as exc:
            return {
                "action": "none",
                "changed": False,
                "user_notice": "Long-term memory not updated.",
                "reason": f"memory_decision_failed:{exc}",
            }
        applied = RuntimeStore.apply_long_term_memory_action(decision, runtime=runtime)
        normalized_action = str(decision.get("action") or "none").strip().lower() or "none"
        auto_record_changed = False
        auto_preference_changed = _append_user_preference_lines(local_preference_lines)
        if normalized_action in {"none", "user_preference", "verified_playbook"}:
            auto_record_changed = _append_long_term_task_summary(goal, result_summary)
            if normalized_action == "none" and auto_record_changed:
                applied["action"] = "useful_task_summary"
                applied["changed"] = True
                applied["user_notice"] = "Saved to long-term memory as a useful summary."
        applied["reason"] = str(decision.get("reason") or "").strip()
        if auto_record_changed or auto_preference_changed:
            applied["reason"] = (
                f"{applied['reason']}|auto_recorded_completed_task".strip("|")
                if applied["reason"]
                else "auto_recorded_completed_task"
            )
            applied["changed"] = True
            if auto_preference_changed and not auto_record_changed and normalized_action == "none":
                applied["action"] = "user_preference"
                applied["user_notice"] = "Saved to long-term memory as a preference."
            elif auto_preference_changed and auto_record_changed:
                applied["user_notice"] = "Saved to long-term memory."
        applied["memory_context"] = _extract_memory_context(memory_path, goal)
        return applied

    @staticmethod
    def user_secrets() -> dict[str, Any]:
        _migrate_sensitive_fields_from_json()
        return _merge_local_secrets(_load_json(SECRETS_FILE))

    @staticmethod
    def account_session() -> dict[str, Any]:
        return _load_session_json()

    @staticmethod
    def apply_account_link_payload(payload: dict[str, Any]) -> tuple[bool, str]:
        auth_token = str(
            payload.get("auth_token")
            or payload.get(LEGACY_AUTH_KEY)
            or payload.get("session_cookie")
            or ""
        ).strip()
        hub_url = str(
            payload.get("hub_url")
            or payload.get(LEGACY_HUB_URL_KEY)
            or payload.get("control_tower_url")
            or HUB_URL
        ).rstrip("/")
        account_email = str(payload.get("account_email") or "").strip()
        account_uid = str(payload.get("account_uid") or "").strip()
        plan_status = str(payload.get("plan_status") or "trialing").strip()
        plan_name = str(payload.get("plan_name") or "Free").strip()
        plan_key = str(payload.get("plan_key") or "free").strip()
        ticket_token = str(payload.get("ticket_token") or "").strip()

        if not auth_token:
            return False, "Missing session cookie."

        RuntimeStore.save_account_session(
            {
                "account_email": account_email,
                "account_uid": account_uid,
                "plan_status": plan_status,
                "plan_key": plan_key,
                "hub_url": hub_url,
                "auth_token": auth_token,
                "plan_name": plan_name,
                "activation_notice_pending": "",
                "activation_notice_seen_for": "",
                "prompts_remaining": payload.get("prompts_remaining"),
            }
        )
        ok, message = RuntimeStore.refresh_access(hub_url)
        if ticket_token and hub_url:
            try:
                _http_json(
                    f"{hub_url.rstrip('/')}/api/vm-link-ticket/{urllib.parse.quote(ticket_token, safe='')}/ack",
                    method="POST",
                    payload={
                        "status": "connected" if ok else "failed",
                        "message": message,
                    },
                )
            except Exception:
                pass
        return ok, message

    @staticmethod
    def save_account_session(payload: dict[str, Any]) -> None:
        _save_session_json(payload)

    @staticmethod
    def clear_account_session() -> None:
        _save_session_json(
            {
                "account_email": "",
                "account_uid": "",
                "plan_status": "trialing",
                "plan_key": "free",
                "hub_url": PUBLIC_HUB_URL,
                "auth_token": "",
                "plan_name": "AriaOS",
                "activation_notice_pending": "",
                "activation_notice_seen_for": "",
                "prompts_remaining": None,
                "update_required": False,
                "update_message": "",
                "update_download_url": "",
                "latest_version": "",
                "min_supported_build": 0,
                "latest_build": 0,
            },
        )

    @staticmethod
    def logout_account(open_browser: bool = True) -> tuple[bool, str]:
        session = _load_session_json()
        hub_url = _canonical_hub_url(session.get("hub_url") or HUB_URL)
        RuntimeStore.clear_account_session()
        if open_browser:
            RuntimeStore.open_account_signin(hub_url)
        return True, "Account session cleared. Sign in with another account to continue."

    @staticmethod
    def refresh_access(base_url: str | None = None) -> tuple[bool, str]:
        del base_url
        return True, "AriaOS Community runs locally. No account refresh is required."

    @staticmethod
    def login_access(base_url: str, email: str, password: str) -> tuple[bool, str]:
        del base_url, email, password
        return False, "Account sign-in is disabled in AriaOS Community."

    @staticmethod
    def create_access(base_url: str, email: str, password: str, display_name: str, company: str) -> tuple[bool, str]:
        del base_url, email, password, display_name, company
        return False, "Account creation is disabled in AriaOS Community."

    @staticmethod
    def ensure_access() -> tuple[bool, str]:
        return True, "Local runtime ready."

    @staticmethod
    def pack_input(session_id: str, user_prompt: str, transcript_messages: list[Any]) -> str:
        del session_id, transcript_messages
        return str(user_prompt or "").strip()

    @staticmethod
    def prepare_task(
        *,
        task_id: str,
        session_id: str,
        goal: str,
        requested_model: str = "",
        image: dict[str, Any] | None = None,
        max_budget_usd: float = 0.0,
    ) -> tuple[bool, dict[str, Any]]:
        return _prepare_task(
            task_id=str(task_id or "").strip(),
            session_id=str(session_id or "").strip(),
            goal=str(goal or "").strip(),
            requested_model=str(requested_model or "").strip(),
            image=image if isinstance(image, dict) else None,
            max_budget_usd=float(max_budget_usd or 0.0),
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_account_payload,
            save_json=_save_account_payload,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=RuntimeStore.clear_account_session,
            device=RuntimeStore._device_identity(),
            runtime=RuntimeStore.remote_openai_runtime(),
            account_uid=str(RuntimeStore().read().account_uid or ""),
            active_app="Terminal Aria",
            local_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @staticmethod
    def fetch_realtime_token(*, kind: str = "device") -> tuple[bool, dict[str, Any]]:
        return _fetch_realtime_token(
            kind=str(kind or "device").strip(),
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_account_payload,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=RuntimeStore.clear_account_session,
            device=RuntimeStore._device_identity(),
        )

    @staticmethod
    def sync_device(
        *,
        current_task_id: str = "",
        vm_status: str = "ready",
        remote_enabled: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        return _sync_device(
            current_task_id=str(current_task_id or "").strip(),
            vm_status=str(vm_status or "ready").strip() or "ready",
            remote_enabled=bool(remote_enabled),
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_account_payload,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=RuntimeStore.clear_account_session,
            device=RuntimeStore._device_identity(),
        )

    @staticmethod
    def sync_task(
        task_id: str,
        *,
        status: str = "",
        session_id: str = "",
        latest_summary: str = "",
        latest_result: str = "",
        error: str = "",
        spent_usd: float | None = None,
        usage: dict[str, Any] | None = None,
        event_text: str = "",
        event_kind: str = "",
        ack_message_ids: list[str] | None = None,
        latest_screenshot_data_url: str = "",
        latest_screenshot_captured_at: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        return _sync_task(
            task_id=task_id,
            status=status,
            session_id=session_id,
            latest_summary=latest_summary,
            latest_result=latest_result,
            error=error,
            spent_usd=spent_usd,
            usage=usage if isinstance(usage, dict) else None,
            event_text=event_text,
            event_kind=event_kind,
            ack_message_ids=ack_message_ids,
            latest_screenshot_data_url=latest_screenshot_data_url,
            latest_screenshot_captured_at=latest_screenshot_captured_at,
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_account_payload,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            http_upload_binary=_http_upload_binary,
            decode_data_url=_decode_data_url,
            clear_account_session=RuntimeStore.clear_account_session,
        )

    @staticmethod
    def report_usage(payload: dict[str, Any]) -> tuple[bool, str]:
        return _report_usage(
            payload=payload,
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_account_payload,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=RuntimeStore.clear_account_session,
        )

    @staticmethod
    def consume_activation_notice() -> str | None:
        session = _load_session_json()
        plan_key = str(session.get("activation_notice_pending") or "").strip()
        if not plan_key:
            return None
        session["activation_notice_pending"] = ""
        session["activation_notice_seen_for"] = plan_key
        _save_session_json(session)
        return plan_key

    @staticmethod
    def save_user_secrets(payload: dict[str, Any]) -> None:
        plain, sensitive = _split_secret_payload(payload)
        for field, value in sensitive.items():
            label = "AriaOS OpenAI API Key" if field == "openai_api_key" else "AriaOS VM Admin Password"
            if value:
                if not set_secret(field, value, label=label):
                    plain[field] = value
            else:
                clear_secret(field)
        _save_json(SECRETS_FILE, plain)

    @staticmethod
    def remote_openai_runtime() -> dict[str, str]:
        secrets = RuntimeStore.user_secrets()
        return {
            "api_key": str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip(),
            "base_url": str(
                os.environ.get("OPENAI_BASE_URL")
                or secrets.get("openai_base_url")
                or default_model_api()
            ).strip()
            or default_model_api(),
            "model": _normalize_openai_model(os.environ.get("ARIAOS_MODEL") or secrets.get("openai_model") or DEFAULT_OPENAI_MODEL),
        }

    @staticmethod
    def ensure_openai_key_ready() -> bool:
        secrets = RuntimeStore.user_secrets()
        return bool(str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip())

    @staticmethod
    def save_openai_settings(api_key: str, base_url: str, model: str) -> bool:
        payload = _load_json(SECRETS_FILE)
        payload["openai_api_key"] = api_key.strip()
        payload["openai_base_url"] = base_url.strip() or default_model_api()
        payload["openai_model"] = _normalize_openai_model(model)
        RuntimeStore.save_user_secrets(payload)
        os.environ["OPENAI_API_KEY"] = payload["openai_api_key"]
        os.environ["OPENAI_BASE_URL"] = payload["openai_base_url"]
        os.environ["ARIAOS_MODEL"] = payload["openai_model"]
        return RuntimeStore.restart_local_backend()

    @staticmethod
    def save_openai_model(model: str) -> bool:
        runtime = RuntimeStore.remote_openai_runtime()
        return RuntimeStore.save_openai_settings(
            api_key=str(runtime.get("api_key") or ""),
            base_url=str(runtime.get("base_url") or default_model_api()),
            model=model,
        )

    @staticmethod
    def save_voice_name(voice_name: str) -> str:
        config = load_runtime_config()
        normalized = _normalize_voice_name(voice_name)
        config["voice_name"] = normalized
        _save_json(CONFIG_PATH, config)
        return normalized

    @staticmethod
    def save_voice_language(language: str) -> str:
        config = load_runtime_config()
        normalized = _normalize_voice_language(language)
        config["voice_language"] = normalized
        _save_json(CONFIG_PATH, config)
        return normalized

    @staticmethod
    def vm_admin_password() -> str:
        secrets = RuntimeStore.user_secrets()
        return str(secrets.get("vm_admin_password") or "").strip()

    @staticmethod
    def save_vm_admin_password(password: str) -> None:
        payload = _load_json(SECRETS_FILE)
        payload["vm_admin_password"] = str(password or "").strip()
        RuntimeStore.save_user_secrets(payload)

    @staticmethod
    def restart_local_backend() -> bool:
        if str(load_runtime_config().get("remote_url") or "").strip():
            return True
        ariaos_dir = Path(os.environ.get("ARIAOS_DIR") or str(Path.home() / "ariaos"))
        if not ariaos_dir.exists():
            return False

        if RuntimeStore.backend_ws_reachable():
            return True

        STATE_DIR.mkdir(parents=True, exist_ok=True)

        pid = None
        try:
            if BACKEND_PID_PATH.exists():
                pid = int(BACKEND_PID_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None

        if pid:
            try:
                os.kill(pid, 15)
                time.sleep(0.5)
            except Exception:
                pass

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ariaos_dir)
        with BACKEND_LOG_PATH.open("a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                ["python3", "-u", "backend/server.py"],
                cwd=str(ariaos_dir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        BACKEND_PID_PATH.write_text(str(proc.pid), encoding="utf-8")

        deadline = time.time() + 12.0
        while time.time() < deadline:
            sock = socket.socket()
            sock.settimeout(0.25)
            try:
                sock.connect(("127.0.0.1", 8080))
                return True
            except OSError:
                time.sleep(0.25)
            finally:
                sock.close()
        return False

    @staticmethod
    def state_stamp() -> tuple[float, float]:
        session_mtime = max(
            SESSION_FILE.stat().st_mtime if SESSION_FILE.exists() else 0.0,
            LEGACY_SESSION_FILE.stat().st_mtime if LEGACY_SESSION_FILE.exists() else 0.0,
        )
        secrets_mtime = SECRETS_FILE.stat().st_mtime if SECRETS_FILE.exists() else 0.0
        stamp_mtime = LOCAL_SECRET_STAMP.stat().st_mtime if LOCAL_SECRET_STAMP.exists() else 0.0
        return max(session_mtime, stamp_mtime), max(secrets_mtime, stamp_mtime)

    @staticmethod
    def _history_db_path(session: dict[str, Any]) -> Path:
        account_email = str(session.get("account_email") or "").strip().lower()
        auth_token = str(session.get("auth_token") or session.get(LEGACY_AUTH_KEY) or "").strip()
        if not account_email or not auth_token:
            return ANONYMOUS_DB_PATH

        slug = re.sub(r"[^a-z0-9]+", "_", account_email).strip("_") or "account"
        account_db = DATA_DIR / f"aria_history_{slug}.db"
        if not account_db.exists() and LEGACY_DB_PATH.exists():
            try:
                account_db.write_bytes(LEGACY_DB_PATH.read_bytes())
            except Exception:
                pass
        return account_db

    @staticmethod
    def ensure_local_link_server() -> None:
        global _LINK_SERVER_STARTED
        if _LINK_SERVER_STARTED:
            return

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                origin = self.headers.get("Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin, Access-Control-Request-Private-Network")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):  # noqa: N802
                self._send(200, {"ok": True})

            def do_POST(self):  # noqa: N802
                if urlparse(self.path).path != "/connect":
                    self._send(404, {"ok": False, "error": "Not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
                except Exception:
                    self._send(400, {"ok": False, "error": "Invalid JSON payload."})
                    return

                ok, message = RuntimeStore.apply_account_link_payload(payload)
                self._send(200 if ok else 400, {"ok": ok, "message": message})

            def log_message(self, _format: str, *_args) -> None:
                return

        def _serve() -> None:
            try:
                server = ThreadingHTTPServer((LOCAL_LINK_HOST, LOCAL_LINK_PORT), Handler)
            except OSError:
                return
            server.serve_forever()

        threading.Thread(target=_serve, daemon=True).start()
        _LINK_SERVER_STARTED = True
