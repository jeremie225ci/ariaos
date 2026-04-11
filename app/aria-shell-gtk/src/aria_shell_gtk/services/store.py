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

from .broker import (
    ensure_access as _ensure_access,
    login_access as _login_access,
    prepare_task as _prepare_task,
    refresh_access as _refresh_access,
    report_usage as _report_usage,
    create_access as _create_access,
    sync_device as _sync_device,
    sync_task as _sync_task,
)
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
    local_hub_url,
    public_hub_url,
)


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
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos"
BACKEND_PID_PATH = STATE_DIR / "backend.pid"
BACKEND_LOG_PATH = STATE_DIR / "backend.log"
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

DEFAULT_CONFIG = {
    "local_url": "ws://127.0.0.1:8080",
    "remote_url": os.environ.get("ARIA_REMOTE_GATEWAY_WS", DEFAULT_REMOTE_URL).strip() or DEFAULT_REMOTE_URL,
    "hub_url": HUB_URL,
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




def _load_session_json() -> dict[str, Any]:
    payload = _load_json(SESSION_FILE)
    if payload:
        min_supported_build = _to_build_number(payload.get("min_supported_build"))
        if bool(payload.get("update_required")) and min_supported_build > 0:
            local_build = _client_release_info().get("build") or 0
            if int(local_build) >= min_supported_build:
                _clear_update_gate_fields(payload)
                _save_json(SESSION_FILE, payload)
        return payload
    legacy = _load_json(LEGACY_SESSION_FILE)
    if legacy:
        min_supported_build = _to_build_number(legacy.get("min_supported_build"))
        if bool(legacy.get("update_required")) and min_supported_build > 0:
            local_build = _client_release_info().get("build") or 0
            if int(local_build) >= min_supported_build:
                _clear_update_gate_fields(legacy)
    return legacy


def _save_session_json(payload: dict[str, Any]) -> None:
    _save_json(SESSION_FILE, payload)
    if LEGACY_SESSION_FILE.exists():
        try:
            LEGACY_SESSION_FILE.unlink()
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
            _apply_update_gate_from_body(body, clear_on_success=bool(response.status < 400 and body.get("ok")))
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
    return cfg


@dataclass
class ClientState:
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
    app_version: str
    app_build: int
    update_required: bool
    update_message: str
    update_download_url: str
    latest_version: str
    min_supported_build: int
    latest_build: int


class ClientStore:
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

    def read(self) -> ClientState:
        _migrate_sensitive_fields_from_json()
        session = _load_session_json()
        secrets = self.user_secrets()
        config = load_runtime_config()
        device = self._device_identity()
        runtime = self.remote_openai_runtime()
        history_db_path = self._history_db_path(session)
        release = _client_release_info()
        return ClientState(
            account_email=str(session.get("account_email") or "Not connected"),
            account_uid=str(session.get("account_uid") or ""),
            plan_status=str(session.get("plan_status") or "trialing"),
            plan_key=str(session.get("plan_key") or "free"),
            api_key_ready=bool(
                str(config.get("remote_url") or "").strip()
                or str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip()
            ),
            openai_model=str(runtime.get("model") or DEFAULT_OPENAI_MODEL),
            hub_url=_canonical_hub_url(
                session.get("hub_url") or config.get("hub_url") or HUB_URL
            ),
            logo_path=LOGO_PATH,
            history_db_path=history_db_path,
            local_url=str(config.get("local_url") or config.get(LEGACY_LOCAL_URL_KEY) or DEFAULT_CONFIG["local_url"]),
            remote_url=str(config.get("remote_url") or config.get(LEGACY_REMOTE_URL_KEY) or DEFAULT_CONFIG["remote_url"]),
            max_transcript_messages=int(config.get("max_transcript_messages") or DEFAULT_CONFIG["max_transcript_messages"]),
            vision_mode=str(config.get("vision_mode") or DEFAULT_CONFIG["vision_mode"]),
            vision_local_start_cmd=str(config.get("vision_local_start_cmd") or DEFAULT_CONFIG["vision_local_start_cmd"]),
            vision_local_stop_cmd=str(config.get("vision_local_stop_cmd") or DEFAULT_CONFIG["vision_local_stop_cmd"]),
            vision_cloud_parse_url=str(config.get("vision_cloud_parse_url") or DEFAULT_CONFIG["vision_cloud_parse_url"]),
            auth_ready=bool(str(session.get("auth_token") or session.get(LEGACY_AUTH_KEY) or "").strip()),
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
            app_version=str(release.get("version") or "dev"),
            app_build=int(release.get("build") or 0),
            update_required=bool(session.get("update_required")),
            update_message=str(session.get("update_message") or "").strip(),
            update_download_url=str(session.get("update_download_url") or "").strip(),
            latest_version=str(session.get("latest_version") or "").strip(),
            min_supported_build=_to_build_number(session.get("min_supported_build")),
            latest_build=_to_build_number(session.get("latest_build")),
        )

    @staticmethod
    def launch_legacy_terminal() -> None:
        subprocess.Popen([str(LEGACY_TERMINAL)], start_new_session=True)

    @staticmethod
    def launch_shell_window(view: str) -> None:
        launcher = GTK_SHELL_LAUNCHER if GTK_SHELL_LAUNCHER.exists() else PACKAGED_GTK_SHELL_LAUNCHER
        if not launcher.exists():
            if view == "terminal":
                ClientStore.launch_legacy_terminal()
            return
        env = dict(os.environ)
        env["ARIA_SHELL_START_VIEW"] = view
        subprocess.Popen([str(launcher)], env=env, start_new_session=True)

    @staticmethod
    def open_account() -> None:
        ClientStore._open_url(f"{PUBLIC_HUB_URL}/dashboard")

    @staticmethod
    def open_billing() -> bool:
        return ClientStore._open_url(f"{PUBLIC_HUB_URL}/pricing")

    @staticmethod
    def open_download(url: str | None = None) -> bool:
        target = str(url or "").strip() or f"{PUBLIC_HUB_URL}/download"
        return ClientStore._open_url(target)

    @staticmethod
    def open_account_signin(base_url: str | None = None) -> None:
        hub_url = _canonical_hub_url(base_url or HUB_URL)
        next_path = quote("/login?next=/connect-vm", safe="")
        ClientStore._open_url(f"{hub_url}/logout-vm?next={next_path}")

    @staticmethod
    def open_google_connect(base_url: str | None = None) -> None:
        hub_url = _canonical_hub_url(base_url or HUB_URL)
        next_path = quote("/connect-vm", safe="")
        ClientStore._open_url(f"{hub_url}/login?next={next_path}&provider=google")

    @staticmethod
    def open_browser() -> None:
        ClientStore._open_url(PUBLIC_HUB_URL)

    @staticmethod
    def open_files() -> None:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["thunar", str(WORKSPACE_DIR)], start_new_session=True)

    @staticmethod
    def user_secrets() -> dict[str, Any]:
        _migrate_sensitive_fields_from_json()
        return _merge_local_secrets(_load_json(SECRETS_FILE))

    @staticmethod
    def account_session() -> dict[str, Any]:
        return _load_session_json()

    @staticmethod
    def save_account_session(payload: dict[str, Any]) -> None:
        _save_session_json(payload)

    @staticmethod
    def clear_account_session() -> None:
        _save_json(
            SESSION_FILE,
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
        ClientStore.clear_account_session()
        if open_browser:
            ClientStore.open_account_signin(hub_url)
        return True, "Account session cleared. Sign in with another account to continue."

    @staticmethod
    def refresh_access(base_url: str | None = None) -> tuple[bool, str]:
        return _refresh_access(
            base_url=base_url,
            session_file=SESSION_FILE,
            load_json=_load_json,
            save_json=_save_json,
            hub_url=HUB_URL,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=ClientStore.clear_account_session,
        )

    @staticmethod
    def login_access(base_url: str, email: str, password: str) -> tuple[bool, str]:
        return _login_access(
            base_url=base_url,
            email=email,
            password=password,
            session_file=SESSION_FILE,
            save_json=_save_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            refresh_session=ClientStore.refresh_access,
        )

    @staticmethod
    def create_access(base_url: str, email: str, password: str, display_name: str, company: str) -> tuple[bool, str]:
        return _create_access(
            base_url=base_url,
            email=email,
            password=password,
            display_name=display_name,
            company=company,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
        )

    @staticmethod
    def ensure_access() -> tuple[bool, str]:
        return _ensure_access(
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_json,
            save_json=_save_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=ClientStore.clear_account_session,
        )


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
            load_json=_load_json,
            save_json=_save_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=ClientStore.clear_account_session,
            device=ClientStore._device_identity(),
            runtime=ClientStore.remote_openai_runtime(),
            account_uid=str(ClientStore().read().account_uid or ""),
            active_app="Terminal Aria",
            local_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
            load_json=_load_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=ClientStore.clear_account_session,
            device=ClientStore._device_identity(),
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
            load_json=_load_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            http_upload_binary=_http_upload_binary,
            decode_data_url=_decode_data_url,
            clear_account_session=ClientStore.clear_account_session,
        )

    @staticmethod
    def report_usage(payload: dict[str, Any]) -> tuple[bool, str]:
        return _report_usage(
            payload=payload,
            session_file=SESSION_FILE,
            hub_url=HUB_URL,
            load_json=_load_json,
            canonical_hub_url=_canonical_hub_url,
            http_json=_http_json,
            clear_account_session=ClientStore.clear_account_session,
        )

    @staticmethod
    def consume_activation_notice() -> str | None:
        session = _load_session_json()
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
        secrets = ClientStore.user_secrets()
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
        secrets = ClientStore.user_secrets()
        return bool(
            str(load_runtime_config().get("remote_url") or "").strip()
            or str(os.environ.get("OPENAI_API_KEY") or secrets.get("openai_api_key") or "").strip()
        )

    @staticmethod
    def save_openai_settings(api_key: str, base_url: str, model: str) -> bool:
        payload = _load_json(SECRETS_FILE)
        payload["openai_api_key"] = api_key.strip()
        payload["openai_base_url"] = base_url.strip() or default_model_api()
        payload["openai_model"] = _normalize_openai_model(model)
        ClientStore.save_user_secrets(payload)
        os.environ["OPENAI_API_KEY"] = payload["openai_api_key"]
        os.environ["OPENAI_BASE_URL"] = payload["openai_base_url"]
        os.environ["ARIAOS_MODEL"] = payload["openai_model"]
        if str(load_runtime_config().get("remote_url") or "").strip():
            return True
        return ClientStore.restart_local_backend()

    @staticmethod
    def save_openai_model(model: str) -> bool:
        runtime = ClientStore.remote_openai_runtime()
        return ClientStore.save_openai_settings(
            api_key=str(runtime.get("api_key") or ""),
            base_url=str(runtime.get("base_url") or default_model_api()),
            model=model,
        )

    @staticmethod
    def vm_admin_password() -> str:
        secrets = ClientStore.user_secrets()
        return str(secrets.get("vm_admin_password") or "").strip()

    @staticmethod
    def save_vm_admin_password(password: str) -> None:
        payload = _load_json(SECRETS_FILE)
        payload["vm_admin_password"] = str(password or "").strip()
        ClientStore.save_user_secrets(payload)

    @staticmethod
    def restart_local_backend() -> bool:
        if str(load_runtime_config().get("remote_url") or "").strip():
            return True
        ariaos_dir = Path(os.environ.get("ARIAOS_DIR") or str(Path.home() / "ariaos"))
        if not ariaos_dir.exists():
            return False

        if ClientStore.backend_ws_reachable():
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

                auth_token = str(payload.get("auth_token") or payload.get(LEGACY_AUTH_KEY) or "").strip()
                hub_url = str(payload.get("hub_url") or HUB_URL).rstrip("/")
                account_email = str(payload.get("account_email") or "").strip()
                account_uid = str(payload.get("account_uid") or "").strip()
                plan_status = str(payload.get("plan_status") or "trialing").strip()
                plan_name = str(payload.get("plan_name") or "Free").strip()
                plan_key = str(payload.get("plan_key") or "free").strip()

                if not auth_token:
                    self._send(400, {"ok": False, "error": "Missing session cookie."})
                    return

                ClientStore.save_account_session(
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
                ok, message = ClientStore.refresh_access(hub_url)
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
