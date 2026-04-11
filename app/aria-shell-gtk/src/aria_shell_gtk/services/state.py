from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
import base64
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .control import (
    claim_prompt_access as _claim_prompt_access,
    fetch_remote_entitlement as _fetch_remote_entitlement,
    login_control_tower as _login_control_tower,
    prepare_remote_task_start as _prepare_remote_task_start,
    refresh_control_tower_session as _refresh_control_tower_session,
    report_task_usage as _report_task_usage,
    signup_control_tower as _signup_control_tower,
    sync_remote_device_presence as _sync_remote_device_presence,
    sync_remote_task_state as _sync_remote_task_state,
)
from .local_secrets import (
    LOCAL_SECRET_STAMP,
    clear_secret,
    get_secret,
    sensitive_fields,
    set_secret,
)
from .runtime_seed import (
    default_openai_base_url,
    default_remote_gateway_ws,
    local_control_tower_url,
    public_control_tower_url,
)

CONFIG_DIR = Path.home() / ".config" / "ariaos"
SESSION_FILE = CONFIG_DIR / "control_tower_session.json"
DEVICE_FILE = CONFIG_DIR / "device_identity.json"
SECRETS_FILE = CONFIG_DIR / "user_secrets.json"
DATA_DIR = Path.home() / ".ariaos" / "data"
LEGACY_DB_PATH = DATA_DIR / "aria_history.db"
ANONYMOUS_DB_PATH = DATA_DIR / "aria_history_anonymous.db"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos"
BACKEND_PID_PATH = STATE_DIR / "backend.pid"
BACKEND_LOG_PATH = STATE_DIR / "backend.log"
CONTROL_TOWER_URL = local_control_tower_url()
PRODUCTION_CONTROL_TOWER_URL = public_control_tower_url()
DEFAULT_REMOTE_GATEWAY_WS = default_remote_gateway_ws()
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

DEFAULT_CONFIG = {
    "backend_ws_url": "ws://127.0.0.1:8080",
    "remote_gateway_ws_url": os.environ.get("ARIA_REMOTE_GATEWAY_WS", DEFAULT_REMOTE_GATEWAY_WS).strip() or DEFAULT_REMOTE_GATEWAY_WS,
    "control_tower_url": CONTROL_TOWER_URL,
    "vision_mode": "local",
    "vision_local_start_cmd": "bash -lc 'curl -fsS http://192.168.0.242:8000/health >/dev/null 2>&1 || true'",
    "vision_local_stop_cmd": "bash -lc 'true'",
    "vision_cloud_parse_url": "https://vision.example.com/parse",
    "max_transcript_messages": 20,
}

DEFAULT_OPENAI_MODEL = "gpt-5.4"
SUPPORTED_OPENAI_MODELS = {"gpt-5.4", "gpt-5.4-mini"}


def _normalize_openai_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if normalized in SUPPORTED_OPENAI_MODELS:
        return normalized
    return DEFAULT_OPENAI_MODEL


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


def _extract_session_cookie(headers) -> str | None:
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
    session_cookie: str | None = None,
) -> tuple[int, dict[str, Any], str | None]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if session_cookie:
        headers["Cookie"] = f"aria_session={session_cookie}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            body = json.loads(raw or "{}")
            return response.status, body, _extract_session_cookie(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {"error": raw or f"HTTP {exc.code}"}
        return exc.code, body, _extract_session_cookie(exc.headers)


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


def _canonical_control_tower_url(url: str | None) -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return PRODUCTION_CONTROL_TOWER_URL
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip().lower()
    if host in {"127.0.0.1", "localhost", "10.0.2.2"}:
        return PRODUCTION_CONTROL_TOWER_URL
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
    return cfg


@dataclass
class ShellState:
    account_email: str
    account_uid: str
    plan_status: str
    plan_key: str
    api_key_ready: bool
    openai_model: str
    control_tower_url: str
    logo_path: Path
    history_db_path: Path
    backend_ws_url: str
    remote_gateway_ws_url: str
    max_transcript_messages: int
    vision_mode: str
    vision_local_start_cmd: str
    vision_local_stop_cmd: str
    vision_cloud_parse_url: str
    session_cookie_ready: bool
    plan_name: str
    activation_notice_plan: str | None
    prompts_remaining: int | None
    device_id: str
    device_name: str
    machine_uid: str


class StateService:
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

    def read(self) -> ShellState:
        _migrate_sensitive_fields_from_json()
        session = _load_json(SESSION_FILE)
        secrets = self.user_secrets()
        config = load_runtime_config()
        device = self._device_identity()
        runtime = self.remote_openai_runtime()
        history_db_path = self._history_db_path(session)
        return ShellState(
            account_email=str(session.get("account_email") or "Not connected"),
            account_uid=str(session.get("account_uid") or ""),
            plan_status=str(session.get("plan_status") or "trialing"),
            plan_key=str(session.get("plan_key") or "free"),
            api_key_ready=bool(
                str(config.get("remote_gateway_ws_url") or "").strip()
                or str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip()
            ),
            openai_model=str(runtime.get("model") or DEFAULT_OPENAI_MODEL),
            control_tower_url=_canonical_control_tower_url(
                session.get("control_tower_url") or config.get("control_tower_url") or CONTROL_TOWER_URL
            ),
            logo_path=LOGO_PATH,
            history_db_path=history_db_path,
            backend_ws_url=str(config.get("backend_ws_url") or DEFAULT_CONFIG["backend_ws_url"]),
            remote_gateway_ws_url=str(config.get("remote_gateway_ws_url") or DEFAULT_CONFIG["remote_gateway_ws_url"]),
            max_transcript_messages=int(config.get("max_transcript_messages") or DEFAULT_CONFIG["max_transcript_messages"]),
            vision_mode=str(config.get("vision_mode") or DEFAULT_CONFIG["vision_mode"]),
            vision_local_start_cmd=str(config.get("vision_local_start_cmd") or DEFAULT_CONFIG["vision_local_start_cmd"]),
            vision_local_stop_cmd=str(config.get("vision_local_stop_cmd") or DEFAULT_CONFIG["vision_local_stop_cmd"]),
            vision_cloud_parse_url=str(config.get("vision_cloud_parse_url") or DEFAULT_CONFIG["vision_cloud_parse_url"]),
            session_cookie_ready=bool(str(session.get("session_cookie") or "").strip()),
            plan_name=str(session.get("plan_name") or "Free"),
            activation_notice_plan=(
                str(session.get("activation_notice_pending") or "").strip() or None
            ),
            prompts_remaining=(
                int(session["prompts_remaining"])
                if str(session.get("prompts_remaining", "")).strip() not in {"", "None", "null"}
                else None
            ),
            device_id=str(device.get("device_id") or ""),
            device_name=str(device.get("device_name") or "Aria VM"),
            machine_uid=str(device.get("machine_uid") or ""),
        )

    @staticmethod
    def launch_legacy_terminal() -> None:
        subprocess.Popen([str(LEGACY_TERMINAL)], start_new_session=True)

    @staticmethod
    def launch_shell_window(view: str) -> None:
        launcher = GTK_SHELL_LAUNCHER if GTK_SHELL_LAUNCHER.exists() else PACKAGED_GTK_SHELL_LAUNCHER
        if not launcher.exists():
            if view == "terminal":
                StateService.launch_legacy_terminal()
            return
        env = dict(os.environ)
        env["ARIA_SHELL_START_VIEW"] = view
        subprocess.Popen([str(launcher)], env=env, start_new_session=True)

    @staticmethod
    def open_control_tower() -> None:
        StateService._open_url(f"{PRODUCTION_CONTROL_TOWER_URL}/dashboard")

    @staticmethod
    def open_billing() -> bool:
        return StateService._open_url(f"{PRODUCTION_CONTROL_TOWER_URL}/pricing")

    @staticmethod
    def open_control_tower_signin(base_url: str | None = None) -> None:
        control_tower_url = _canonical_control_tower_url(base_url or CONTROL_TOWER_URL)
        next_path = quote("/login?next=/connect-vm", safe="")
        StateService._open_url(f"{control_tower_url}/logout-vm?next={next_path}")

    @staticmethod
    def open_google_connect(base_url: str | None = None) -> None:
        control_tower_url = _canonical_control_tower_url(base_url or CONTROL_TOWER_URL)
        next_path = quote("/connect-vm", safe="")
        StateService._open_url(f"{control_tower_url}/login?next={next_path}&provider=google")

    @staticmethod
    def open_browser() -> None:
        StateService._open_url(PRODUCTION_CONTROL_TOWER_URL)

    @staticmethod
    def open_files() -> None:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["thunar", str(WORKSPACE_DIR)], start_new_session=True)

    @staticmethod
    def user_secrets() -> dict[str, Any]:
        _migrate_sensitive_fields_from_json()
        return _merge_local_secrets(_load_json(SECRETS_FILE))

    @staticmethod
    def control_tower_session() -> dict[str, Any]:
        return _load_json(SESSION_FILE)

    @staticmethod
    def save_control_tower_session(payload: dict[str, Any]) -> None:
        _save_json(SESSION_FILE, payload)

    @staticmethod
    def clear_control_tower_session() -> None:
        _save_json(
            SESSION_FILE,
            {
                "account_email": "",
                "account_uid": "",
                "plan_status": "trialing",
                "plan_key": "free",
                "control_tower_url": PRODUCTION_CONTROL_TOWER_URL,
                "session_cookie": "",
                "plan_name": "AriaOS",
                "activation_notice_pending": "",
                "activation_notice_seen_for": "",
                "prompts_remaining": None,
            },
        )

    @staticmethod
    def logout_control_tower(open_browser: bool = True) -> tuple[bool, str]:
        session = _load_json(SESSION_FILE)
        control_tower_url = _canonical_control_tower_url(session.get("control_tower_url") or CONTROL_TOWER_URL)
        StateService.clear_control_tower_session()
        if open_browser:
            StateService.open_control_tower_signin(control_tower_url)
        return True, "Control Tower session cleared. Sign in with another account to continue."

    @staticmethod
    def refresh_control_tower_session(base_url: str | None = None) -> tuple[bool, str]:
        return _refresh_control_tower_session(
            base_url=base_url,
            session_file=SESSION_FILE,
            load_json=_load_json,
            save_json=_save_json,
            control_tower_url=CONTROL_TOWER_URL,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
        )

    @staticmethod
    def login_control_tower(base_url: str, email: str, password: str) -> tuple[bool, str]:
        return _login_control_tower(
            base_url=base_url,
            email=email,
            password=password,
            session_file=SESSION_FILE,
            save_json=_save_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            refresh_session=StateService.refresh_control_tower_session,
        )

    @staticmethod
    def signup_control_tower(base_url: str, email: str, password: str, display_name: str, company: str) -> tuple[bool, str]:
        return _signup_control_tower(
            base_url=base_url,
            email=email,
            password=password,
            display_name=display_name,
            company=company,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
        )

    @staticmethod
    def claim_prompt_access() -> tuple[bool, str]:
        return _claim_prompt_access(
            session_file=SESSION_FILE,
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            save_json=_save_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
        )

    @staticmethod
    def fetch_remote_entitlement(*, task_id: str, session_id: str, goal: str) -> tuple[bool, dict[str, Any]]:
        return _fetch_remote_entitlement(
            task_id=task_id,
            session_id=session_id,
            goal=goal,
            session_file=SESSION_FILE,
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            save_json=_save_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
            device=StateService._device_identity(),
        )

    @staticmethod
    def prepare_remote_task_start(
        *,
        task_id: str,
        session_id: str,
        goal: str,
        requested_model: str = "",
        image: dict[str, Any] | None = None,
        max_budget_usd: float = 0.0,
    ) -> tuple[bool, dict[str, Any]]:
        return _prepare_remote_task_start(
            task_id=str(task_id or "").strip(),
            session_id=str(session_id or "").strip(),
            goal=str(goal or "").strip(),
            requested_model=str(requested_model or "").strip(),
            image=image if isinstance(image, dict) else None,
            max_budget_usd=float(max_budget_usd or 0.0),
            session_file=SESSION_FILE,
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            save_json=_save_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
            device=StateService._device_identity(),
            runtime=StateService.remote_openai_runtime(),
            account_uid=str(StateService().read().account_uid or ""),
            active_app="Terminal Aria",
            local_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @staticmethod
    def sync_remote_device_presence(
        *,
        current_task_id: str = "",
        vm_status: str = "ready",
        remote_enabled: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        return _sync_remote_device_presence(
            current_task_id=str(current_task_id or "").strip(),
            vm_status=str(vm_status or "ready").strip() or "ready",
            remote_enabled=bool(remote_enabled),
            session_file=SESSION_FILE,
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
            device=StateService._device_identity(),
        )

    @staticmethod
    def sync_remote_task_state(
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
        return _sync_remote_task_state(
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
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            http_upload_binary=_http_upload_binary,
            decode_data_url=_decode_data_url,
            clear_control_tower_session=StateService.clear_control_tower_session,
        )

    @staticmethod
    def report_task_usage(payload: dict[str, Any]) -> tuple[bool, str]:
        return _report_task_usage(
            payload=payload,
            session_file=SESSION_FILE,
            control_tower_url=CONTROL_TOWER_URL,
            load_json=_load_json,
            canonical_control_tower_url=_canonical_control_tower_url,
            http_json=_http_json,
            clear_control_tower_session=StateService.clear_control_tower_session,
        )

    @staticmethod
    def consume_activation_notice() -> str | None:
        session = _load_json(SESSION_FILE)
        plan_key = str(session.get("activation_notice_pending") or "").strip()
        if not plan_key:
            return None
        session["activation_notice_pending"] = ""
        session["activation_notice_seen_for"] = plan_key
        _save_json(SESSION_FILE, session)
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
        secrets = StateService.user_secrets()
        return {
            "api_key": str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip(),
            "base_url": str(
                os.environ.get("OPENAI_BASE_URL")
                or secrets.get("openai_base_url")
                or default_openai_base_url()
            ).strip()
            or default_openai_base_url(),
            "model": _normalize_openai_model(os.environ.get("ARIAOS_MODEL") or secrets.get("openai_model") or DEFAULT_OPENAI_MODEL),
        }

    @staticmethod
    def ensure_openai_key_ready() -> bool:
        secrets = StateService.user_secrets()
        return bool(
            str(load_runtime_config().get("remote_gateway_ws_url") or "").strip()
            or str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip()
        )

    @staticmethod
    def save_openai_settings(api_key: str, base_url: str, model: str) -> bool:
        payload = _load_json(SECRETS_FILE)
        payload["openai_api_key"] = api_key.strip()
        payload["openai_base_url"] = base_url.strip() or default_openai_base_url()
        payload["openai_model"] = _normalize_openai_model(model)
        StateService.save_user_secrets(payload)
        os.environ["OPENAI_API_KEY"] = payload["openai_api_key"]
        os.environ["OPENAI_BASE_URL"] = payload["openai_base_url"]
        os.environ["ARIAOS_MODEL"] = payload["openai_model"]
        if str(load_runtime_config().get("remote_gateway_ws_url") or "").strip():
            return True
        return StateService.restart_local_backend()

    @staticmethod
    def save_openai_model(model: str) -> bool:
        runtime = StateService.remote_openai_runtime()
        return StateService.save_openai_settings(
            api_key=str(runtime.get("api_key") or ""),
            base_url=str(runtime.get("base_url") or default_openai_base_url()),
            model=model,
        )

    @staticmethod
    def vm_admin_password() -> str:
        secrets = StateService.user_secrets()
        return str(secrets.get("vm_admin_password") or "").strip()

    @staticmethod
    def save_vm_admin_password(password: str) -> None:
        payload = _load_json(SECRETS_FILE)
        payload["vm_admin_password"] = str(password or "").strip()
        StateService.save_user_secrets(payload)

    @staticmethod
    def restart_local_backend() -> bool:
        if str(load_runtime_config().get("remote_gateway_ws_url") or "").strip():
            return True
        ariaos_dir = Path(os.environ.get("ARIAOS_DIR") or str(Path.home() / "ariaos"))
        if not ariaos_dir.exists():
            return False

        if StateService.backend_ws_reachable():
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
        session_mtime = SESSION_FILE.stat().st_mtime if SESSION_FILE.exists() else 0.0
        secrets_mtime = SECRETS_FILE.stat().st_mtime if SECRETS_FILE.exists() else 0.0
        stamp_mtime = LOCAL_SECRET_STAMP.stat().st_mtime if LOCAL_SECRET_STAMP.exists() else 0.0
        return max(session_mtime, stamp_mtime), max(secrets_mtime, stamp_mtime)

    @staticmethod
    def _history_db_path(session: dict[str, Any]) -> Path:
        account_email = str(session.get("account_email") or "").strip().lower()
        session_cookie = str(session.get("session_cookie") or "").strip()
        if not account_email or not session_cookie:
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

                session_cookie = str(payload.get("session_cookie") or "").strip()
                control_tower_url = str(payload.get("control_tower_url") or CONTROL_TOWER_URL).rstrip("/")
                account_email = str(payload.get("account_email") or "").strip()
                account_uid = str(payload.get("account_uid") or "").strip()
                plan_status = str(payload.get("plan_status") or "trialing").strip()
                plan_name = str(payload.get("plan_name") or "Free").strip()
                plan_key = str(payload.get("plan_key") or "free").strip()

                if not session_cookie:
                    self._send(400, {"ok": False, "error": "Missing session cookie."})
                    return

                StateService.save_control_tower_session(
                    {
                        "account_email": account_email,
                        "account_uid": account_uid,
                        "plan_status": plan_status,
                        "plan_key": plan_key,
                        "control_tower_url": control_tower_url,
                        "session_cookie": session_cookie,
                        "plan_name": plan_name,
                        "activation_notice_pending": "",
                        "activation_notice_seen_for": "",
                        "prompts_remaining": payload.get("prompts_remaining"),
                    }
                )
                ok, message = StateService.refresh_control_tower_session(control_tower_url)
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
