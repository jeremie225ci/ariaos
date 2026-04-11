from __future__ import annotations

import base64
import ast
import csv
import difflib
import filecmp
import glob
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any

from .desktop_io import DesktopIO
from .local_secrets import get_secret
from .bootstrap import default_model_api

def _merge_text(*parts: str) -> str:
    return "".join(parts)

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
except ImportError:
    ImageSequenceClip = None

try:
    from .exec_policy import RuntimeExecPolicy
except Exception:
    class RuntimeExecPolicy:
        def __init__(self, *, home_dir: str, client_root: str, app_dir: str, workspace_dir: str, state_dir: str) -> None:
            self.home_dir = home_dir
            self.client_root = client_root
            self.app_dir = app_dir
            self.workspace_dir = workspace_dir
            self.state_dir = state_dir
            self._legacy_app = _merge_text("/opt", "/ariaos", "/ariaos")
            self._legacy_app_skills = f"{self._legacy_app}/skills"
            self._legacy_workspace = _merge_text("/opt", "/ariaos")
            self._legacy_workspace_app = f"{self._legacy_workspace}/app"

        def command_cwd(self) -> str:
            for candidate in (self.home_dir, "/tmp", "/"):
                if candidate and os.path.isdir(candidate):
                    return candidate
            return "/"

        def normalize_path(self, path: str) -> str:
            value = str(path or "").strip()
            if not value:
                return self.command_cwd()
            if value == self._legacy_app:
                return self.app_dir
            if value.startswith(f"{self._legacy_app}/"):
                return str(Path(self.app_dir) / value[len(f"{self._legacy_app}/"):])
            if value == self._legacy_workspace or value == self._legacy_workspace_app:
                return self.workspace_dir
            if value.startswith(f"{self._legacy_workspace_app}/"):
                return str(Path(self.workspace_dir) / value[len(f"{self._legacy_workspace_app}/"):])
            if value.startswith(f"{self._legacy_workspace}/"):
                return str(Path(self.workspace_dir) / value[len(f"{self._legacy_workspace}/"):])
            if value == "/root":
                return self.home_dir
            if value.startswith("/root/"):
                return str(Path(self.home_dir) / value[len("/root/"):])
            return os.path.expanduser(value)

        def normalize_command(self, command: str) -> str:
            value = str(command or "").strip()
            if not value:
                return value
            if self.workspace_dir:
                value = re.sub(r"(?<!\S)/opt/ariaos/ariaos(?=/|\b)", self.app_dir, value)
                value = re.sub(r"(?<!\S)/opt/ariaos/app(?=/|\b)", self.workspace_dir, value)
                value = re.sub(r"(?<!\S)/opt/ariaos(?=/|\b)", self.workspace_dir, value)
            if self.home_dir:
                value = re.sub(r"(?<!\S)/root(?=/|\b)", self.home_dir, value)
            return value

        def resolve_app_command(self, app_name: str) -> list[str]:
            raw = str(app_name or "").strip()
            key = raw.lower()
            if key in {"terminal", "xfce4-terminal", "shell"}:
                return ["xfce4-terminal"]
            if key in {"files", "file manager", "thunar"}:
                return ["thunar"]
            if key in {"browser", "chrome", "google chrome"}:
                return ["google-chrome"]
            if key in {"text editor", "editor", "mousepad"}:
                return ["mousepad"]
            return [raw] if raw else []

        def path_under(self, path: str, root: str) -> bool:
            value = os.path.abspath(str(path or "").strip())
            base = os.path.abspath(str(root or "").strip())
            if not value or not base:
                return False
            return value == base or value.startswith(base + os.sep)

        def is_internal_runtime_path(self, normalized: str) -> bool:
            value = os.path.abspath(str(normalized or "").strip())
            internal_roots = (
                self.client_root,
                os.path.join(self.home_dir, ".config", _merge_text("aria", "os")),
                os.path.join(self.home_dir, _merge_text(".", "aria", "os")),
                self.state_dir,
                os.path.join(self.home_dir, "Desktop", _merge_text("Aria", "App")),
                os.path.join(self.home_dir, _merge_text("aria", "os")),
            )
            internal_paths = {
                os.path.join(self.home_dir, ".local", "bin", _merge_text("aria", "-shell", "-launch")),
                os.path.join(self.home_dir, ".local", "bin", _merge_text("aria", "-shell", "-remote", "-test")),
                os.path.join(self.home_dir, ".local", "bin", _merge_text("aria", "-import", "-session", "-env")),
                os.path.join(self.home_dir, ".local", "bin", _merge_text("aria", "-browser")),
            }
            return any(self.path_under(value, root) for root in internal_roots) or value in {
                os.path.abspath(item) for item in internal_paths
            }

        def deny_internal_runtime_access(
            self,
            normalized: str,
            *,
            action: str,
            extra: dict[str, Any] | None = None,
        ) -> tuple[bool, str, dict[str, Any]] | None:
            if not self.is_internal_runtime_path(normalized):
                return None
            payload = {"path": normalized}
            if extra:
                payload.update(extra)
            return False, f"ERROR {action} {normalized}: internal_runtime_path", payload

        def filter_internal_names(self, base_dir: str, names: list[str]) -> list[str]:
            visible: list[str] = []
            for name in names:
                if self.is_internal_runtime_path(os.path.join(base_dir, name)):
                    continue
                visible.append(name)
            return visible

        def prune_internal_walk(self, current_root: str, dirnames: list[str]) -> None:
            blocked = {".git", "__pycache__", "node_modules", ".venv"}
            dirnames[:] = [
                name
                for name in dirnames
                if name not in blocked and not self.is_internal_runtime_path(os.path.join(current_root, name))
            ]

        def is_protected_path(self, normalized: str) -> bool:
            value = os.path.abspath(str(normalized or "").strip())
            if self.is_internal_runtime_path(value):
                return True
            protected = {
                "/",
                os.path.abspath(self.home_dir),
                os.path.abspath(self.workspace_dir),
                "/opt",
                "/opt/ariaos",
                "/opt/aria-client",
                "/tmp",
            }
            return value in protected

        def normalize_keys(self, keys: Any) -> list[str]:
            if isinstance(keys, str):
                raw_keys = [part for part in keys.replace(",", "+").split("+") if part.strip()]
            elif isinstance(keys, list):
                raw_keys = keys
            else:
                raw_keys = [keys]
            aliases = {
                "control": "ctrl",
                "return": "Return",
                "enter": "Return",
                "escape": "Escape",
                "esc": "Escape",
                "space": "space",
                "arrowup": "Up",
                "arrowdown": "Down",
                "arrowleft": "Left",
                "arrowright": "Right",
                "pageup": "Page_Up",
                "pagedown": "Page_Down",
                "meta": "Super_L",
                "cmd": "Super_L",
                "command": "Super_L",
                "option": "Alt_L",
                "alt": "Alt_L",
                "shift": "Shift_L",
                "tab": "Tab",
                "backspace": "BackSpace",
                "delete": "Delete",
                "home": "Home",
                "end": "End",
            }
            normalized: list[str] = []
            for item in raw_keys:
                value = str(item or "").strip()
                if not value:
                    continue
                mapped = aliases.get(value.lower(), value)
                normalized.append(mapped)
            return normalized or ["Return"]


class RemoteComputerRuntime:
    def __init__(self) -> None:
        self.env = dict(os.environ)
        self.home_dir = str(Path.home())
        self.client_root = str(Path(os.environ.get("ARIA_CLIENT_DIR") or "/opt/aria-client"))
        self.app_dir = str(
            Path(
                os.environ.get("ARIA_SHELL_APP_DIR")
                or str(Path(self.client_root) / "share" / "AriaApp")
            )
        )
        self.workspace_dir = str(
            Path(
                os.environ.get("ARIA_CLIENT_WORKSPACE_DIR")
                or str(Path.home() / _merge_text("Aria", "Workspace"))
            )
        )
        self.local_env_file = str(
            Path(self.home_dir)
            / _merge_text(".", "aria", "os")
            / _merge_text("local", "_env", ".json")
        )
        self.state_dir = str(
            Path(
                os.environ.get("XDG_STATE_HOME")
                or str(Path(self.home_dir) / ".local" / "state")
            )
            / "ariaos"
        )
        self.policy = RuntimeExecPolicy(
            home_dir=self.home_dir,
            client_root=self.client_root,
            app_dir=self.app_dir,
            workspace_dir=self.workspace_dir,
            state_dir=self.state_dir,
        )
        self.desktop = DesktopIO(
            run_ok=self._run_ok,
            extract_point=self._extract_point,
            normalize_keys=self.policy.normalize_keys,
        )
        self._load_session_graphics_env()
        self._load_local_env()

    def _command_cwd(self) -> str:
        return self.policy.command_cwd()

    def _load_local_env(self) -> None:
        try:
            data = json.loads(Path(self.local_env_file).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if key:
                        self.env[str(key)] = str(value)
        except Exception:
            return

    def _load_session_graphics_env(self) -> None:
        try:
            user_selector = str(self.env.get("USER") or os.getuid())
            probe = subprocess.run(
                ["pgrep", "-u", user_selector, "-n", "xfce4-session"],
                capture_output=True,
                text=True,
                timeout=2.0,
                env=self.env,
                check=False,
            )
            session_pid = (probe.stdout or "").strip()
            if not session_pid:
                return
            environ_path = Path("/proc") / session_pid / "environ"
            raw = environ_path.read_bytes().decode("utf-8", errors="ignore")
            session_env: dict[str, str] = {}
            for entry in raw.split("\0"):
                if "=" not in entry:
                    continue
                key, value = entry.split("=", 1)
                if key in {"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER"} and value:
                    session_env[key] = value
            for key, value in session_env.items():
                self.env[key] = value
        except Exception:
            return

    def _save_local_env(self) -> None:
        payload = {}
        for key, value in self.env.items():
            if key.startswith("ARIA_") or key.startswith("OPENAI_") or key in {"HOME", "USER", "PATH", "DISPLAY", "XAUTHORITY"}:
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                payload[key] = str(value)
        path = Path(self.local_env_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _normalize_path(self, path: str) -> str:
        return self.policy.normalize_path(path)

    def _normalize_command(self, command: str) -> str:
        return self.policy.normalize_command(command)

    def _extract_point(self, payload: dict[str, Any] | None) -> tuple[bool, int, int]:
        data = payload or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            for key in ("position", "point", "coordinates", "target"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    if x is None:
                        x = nested.get("x")
                    if y is None:
                        y = nested.get("y")
                    if x is not None and y is not None:
                        break
        if x is None or y is None:
            return False, 0, 0
        return True, int(x), int(y)

    def _run(self, cmd: list[str], *, timeout: float = 8.0) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )

    def _run_ok(self, cmd: list[str], *, timeout: float = 8.0) -> tuple[bool, str]:
        try:
            result = self._run(cmd, timeout=timeout)
        except Exception as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, "ok"
        stderr = (result.stderr or result.stdout or "").strip()
        return False, stderr or f"exit_{result.returncode}"

    def _spawn_detached(self, cmd: list[str]) -> tuple[bool, str]:
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            time.sleep(0.35)
            status = proc.poll()
            if status is None:
                return True, "started"
            stdout, stderr = proc.communicate(timeout=1.0)
            detail = (stderr or stdout or "").strip() or f"exit_{status}"
            return status == 0, detail
        except Exception as exc:
            return False, str(exc)

    def _resolve_app_command(self, app_name: str) -> list[str]:
        return self.policy.resolve_app_command(app_name)

    def _path_under(self, path: str, root: str) -> bool:
        return self.policy.path_under(path, root)

    def _is_internal_runtime_path(self, normalized: str) -> bool:
        return self.policy.is_internal_runtime_path(normalized)

    def _deny_internal_runtime_access(
        self,
        normalized: str,
        *,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[bool, str, dict[str, Any]] | None:
        return self.policy.deny_internal_runtime_access(normalized, action=action, extra=extra)

    def _filter_internal_names(self, base_dir: str, names: list[str]) -> list[str]:
        return self.policy.filter_internal_names(base_dir, names)

    def _prune_internal_walk(self, current_root: str, dirnames: list[str]) -> None:
        self.policy.prune_internal_walk(current_root, dirnames)

    def _is_protected_path(self, normalized: str) -> bool:
        return self.policy.is_protected_path(normalized)

    def _truncate_text(self, text: str, limit: int = 200000) -> tuple[str, bool]:
        value = str(text or "")
        if len(value) <= limit:
            return value, False
        return value[:limit], True

    def _find_marker_upward(self, start: str, marker: str) -> str | None:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        while True:
            candidate = current / marker
            if candidate.exists():
                return str(current)
            if current.parent == current:
                return None
            current = current.parent

    def _extract_pdf_text_fallback(self, raw: bytes) -> str:
        patterns = []

        def _decode_pdf_literal(match: bytes) -> str:
            try:
                text = match.decode("latin-1", errors="ignore")
            except Exception:
                return ""
            text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
            text = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)
            return text

        for found in re.findall(rb"\((.*?)\)\s*Tj", raw, flags=re.DOTALL):
            patterns.append(_decode_pdf_literal(found))
        for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, flags=re.DOTALL):
            candidates = [stream]
            try:
                candidates.append(zlib.decompress(stream))
            except Exception:
                pass
            for candidate in candidates:
                for found in re.findall(rb"\((.*?)\)\s*Tj", candidate, flags=re.DOTALL):
                    patterns.append(_decode_pdf_literal(found))
                for block in re.findall(rb"\[(.*?)\]\s*TJ", candidate, flags=re.DOTALL):
                    for found in re.findall(rb"\((.*?)\)", block, flags=re.DOTALL):
                        patterns.append(_decode_pdf_literal(found))
        cleaned = [item.strip() for item in patterns if item and item.strip()]
        return "\n".join(cleaned)

    def _read_yaml_basic(self, normalized: str) -> tuple[bool, Any, str]:
        text = Path(normalized).read_text(encoding="utf-8", errors="replace")
        try:
            return True, json.loads(text), "json"
        except Exception:
            pass
        data: dict[str, Any] = {}
        current_list_key: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if line.startswith("  - ") and current_list_key:
                data.setdefault(current_list_key, []).append(line[4:].strip())
                continue
            if ":" in line and not line.startswith("  "):
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if not value:
                    current_list_key = key
                    data[key] = []
                else:
                    current_list_key = None
                    data[key] = value
        return True, data, "basic"

    def _write_yaml_basic(self, data: Any) -> str:
        if yaml is not None:
            return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _schema_for_value(self, value: Any, depth: int = 0) -> dict[str, Any]:
        if depth >= 4:
            return {"type": self._schema_type_name(value)}
        if isinstance(value, dict):
            props: dict[str, Any] = {}
            required: list[str] = []
            for index, (key, item) in enumerate(value.items()):
                if index >= 50:
                    break
                key_str = str(key)
                props[key_str] = self._schema_for_value(item, depth + 1)
                required.append(key_str)
            return {
                "type": "object",
                "property_count": len(value),
                "required": required,
                "properties": props,
            }
        if isinstance(value, list):
            item_schemas = [self._schema_for_value(item, depth + 1) for item in value[:5]]
            item_types = sorted({schema.get("type", "unknown") for schema in item_schemas}) if item_schemas else []
            return {
                "type": "array",
                "length": len(value),
                "item_types": item_types,
                "items": item_schemas[0] if item_schemas else {"type": "unknown"},
            }
        schema_type = self._schema_type_name(value)
        node: dict[str, Any] = {"type": schema_type}
        if value is not None and schema_type in {"string", "number", "integer", "boolean"}:
            sample = str(value)
            node["sample"] = sample[:120]
        return node

    def _schema_type_name(self, value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int) and not isinstance(value, bool):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        return "string"

    def _parse_image_size(self, size: Any) -> tuple[int, int]:
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            try:
                return max(128, min(int(size[0]), 2048)), max(128, min(int(size[1]), 2048))
            except Exception:
                pass
        raw = str(size or "").strip().lower()
        aliases = {
            "square": (1024, 1024),
            "portrait": (768, 1024),
            "landscape": (1024, 768),
        }
        if raw in aliases:
            return aliases[raw]
        match = re.match(r"^(\d{2,4})x(\d{2,4})$", raw)
        if match:
            return (
                max(128, min(int(match.group(1)), 2048)),
                max(128, min(int(match.group(2)), 2048)),
            )
        return 1024, 1024

    def _image_format_for_path(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            return "JPEG"
        if ext == ".bmp":
            return "BMP"
        if ext == ".webp":
            return "WEBP"
        return "PNG"

    def _openai_api_key(self) -> str:
        return str(
            self.env.get(_merge_text("OPENAI", "_API", "_KEY"))
            or get_secret(_merge_text("openai", "_api", "_key"))
            or ""
        ).strip()

    def _openai_base_url(self) -> str:
        return str(
            self.env.get(_merge_text("OPENAI", "_IMAGE", "_BASE", "_URL"))
            or self.env.get(_merge_text("OPENAI", "_BASE", "_URL"))
            or default_model_api()
        ).rstrip("/")

    def _openai_image_request(self, endpoint: str, payload: dict[str, Any], *, timeout: float = 180.0) -> tuple[bool, str, dict[str, Any]]:
        api_key = self._openai_api_key()
        if not api_key:
            return False, "ERROR image_ai: missing_openai_api_key", {}
        url = f"{self._openai_base_url()}{endpoint}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            return False, f"ERROR image_ai_request: {exc}", {"endpoint": endpoint}
        try:
            data = json.loads(raw)
        except Exception:
            return False, "ERROR image_ai_request: invalid_json_response", {"endpoint": endpoint, "raw": raw[:4000]}
        return True, "image_ai_request_ok", data if isinstance(data, dict) else {"response": data}

    def _image_data_url(self, path: str) -> tuple[bool, str, str]:
        normalized = self._normalize_path(path)
        if not os.path.isfile(normalized):
            return False, "", f"missing_file:{normalized}"
        mime, _encoding = mimetypes.guess_type(normalized)
        mime = mime or "image/png"
        try:
            raw = Path(normalized).read_bytes()
        except Exception as exc:
            return False, "", f"read_failed:{exc}"
        return True, f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", normalized

    def _resolve_image_sequence_inputs(self, inputs: Any) -> list[str]:
        candidates: list[str] = []
        if isinstance(inputs, str):
            value = str(inputs).strip()
            if any(token in value for token in ["*", "?", "["]):
                candidates.extend(sorted(glob.glob(self._normalize_path(value))))
            else:
                normalized = self._normalize_path(value)
                if os.path.isdir(normalized):
                    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"):
                        candidates.extend(sorted(glob.glob(str(Path(normalized) / ext))))
                else:
                    candidates.append(normalized)
        elif isinstance(inputs, list):
            for item in inputs:
                value = str(item or "").strip()
                if value:
                    candidates.append(self._normalize_path(value))
        filtered: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if not item or item in seen:
                continue
            if os.path.isfile(item):
                filtered.append(item)
                seen.add(item)
        return filtered

    def _parse_storyboard_scenes(self, storyboard: Any) -> list[str]:
        if isinstance(storyboard, list):
            scenes = [str(item or "").strip() for item in storyboard if str(item or "").strip()]
            return scenes
        raw = str(storyboard or "").strip()
        if not raw:
            return []
        lines = [line.strip(" -\t") for line in raw.splitlines()]
        scenes = [line for line in lines if line]
        if len(scenes) > 1:
            return scenes
        return [raw]

    def _capture_region_image(self, x: int, y: int, w: int, h: int) -> tuple[bool, str, str]:
        ok, image_b64, _mime, detail = self.capture_screenshot()
        if not ok or not image_b64:
            return False, "", detail
        if Image is None:
            return False, "", "ocr_region requires Pillow"
        try:
            raw = base64.b64decode(image_b64)
            with Image.open(io.BytesIO(raw)) as img:
                left = max(0, int(x))
                top = max(0, int(y))
                right = min(img.width, left + max(1, int(w)))
                bottom = min(img.height, top + max(1, int(h)))
                if right <= left or bottom <= top:
                    return False, "", "invalid_region"
                crop = img.crop((left, top, right, bottom))
                with tempfile.NamedTemporaryFile(prefix="aria_ocr_region_", suffix=".png", delete=False) as handle:
                    crop_path = handle.name
                crop.save(crop_path, format="PNG")
                return True, crop_path, "region_captured"
        except Exception as exc:
            return False, "", f"region_capture_failed:{exc}"

    def _snapshot_file_state(self, normalized: str) -> dict[str, Any]:
        if not os.path.exists(normalized):
            return {"exists": False}
        stat = os.stat(normalized)
        payload: dict[str, Any] = {
            "exists": True,
            "size": stat.st_size,
            "mtime": round(stat.st_mtime, 6),
        }
        if os.path.isfile(normalized):
            try:
                raw = Path(normalized).read_bytes()
                payload["crc32"] = f"{zlib.crc32(raw) & 0xffffffff:08x}"
            except Exception:
                pass
        return payload

    def _snapshot_dir_state(self, normalized: str) -> dict[str, Any]:
        if not os.path.exists(normalized):
            return {"exists": False}
        entries: list[dict[str, Any]] = []
        latest_mtime = 0.0
        for child in sorted(Path(normalized).iterdir(), key=lambda item: item.name.lower())[:200]:
            try:
                stat = child.stat()
            except Exception:
                continue
            latest_mtime = max(latest_mtime, float(stat.st_mtime))
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size": int(stat.st_size),
                    "mtime": round(float(stat.st_mtime), 6),
                }
            )
        return {
            "exists": True,
            "entry_count": len(entries),
            "latest_mtime": round(latest_mtime, 6),
            "entries": entries,
        }

    def _detect_test_command(self, target: str) -> tuple[list[str] | None, str]:
        normalized = self._normalize_path(target or self._command_cwd())
        start = normalized if os.path.exists(normalized) else self._command_cwd()

        pytest_root = self._find_marker_upward(start, "pytest.ini") or self._find_marker_upward(start, "pyproject.toml")
        if pytest_root:
            if os.path.exists(normalized):
                return ["pytest", "-q", normalized], pytest_root
            return ["pytest", "-q"], pytest_root

        package_root = self._find_marker_upward(start, "package.json")
        if package_root:
            return ["npm", "test"], package_root

        cargo_root = self._find_marker_upward(start, "Cargo.toml")
        if cargo_root:
            return ["cargo", "test"], cargo_root

        go_root = self._find_marker_upward(start, "go.mod")
        if go_root:
            return ["go", "test", "./..."], go_root

        return None, self._command_cwd()

    def capture_screenshot(self) -> tuple[bool, str, str, str]:
        return self.desktop.capture_screenshot()

    def execute_action(self, action: dict[str, Any]) -> tuple[bool, str]:
        return self.desktop.execute_action(action)

    def execute_command(self, command: str) -> tuple[bool, str]:
        normalized = self._normalize_command(command)
        if not normalized:
            return False, "execute_failed:empty_command"
        try:
            if "sudo" in normalized and "-S" not in normalized:
                sudo_password = get_secret("vm_admin_password")
                if sudo_password:
                    replacement = f"echo {shlex.quote(sudo_password)} | sudo -S -p '' "
                    normalized = normalized.replace("sudo -n ", replacement)
                    normalized = normalized.replace("sudo ", replacement)
                elif "sudo -n" not in normalized:
                    return False, "[ERROR] VM admin password is not configured locally."

            stripped = normalized.strip()
            env = dict(self.env)
            env.setdefault("HOME", self.home_dir)
            cwd = self._command_cwd()

            if stripped.endswith("&"):
                background_cmd = stripped[:-1].strip()
                detached_cmd = (
                    "nohup bash -lc "
                    + shlex.quote(background_cmd)
                    + " >/tmp/ariaos_background.log 2>&1 < /dev/null &"
                )
                subprocess.run(
                    detached_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=cwd,
                    env=env,
                    check=False,
                )
                return True, f"[started background] {background_cmd}"

            result = subprocess.run(
                normalized,
                shell=True,
                capture_output=True,
                text=True,
                timeout=45,
                cwd=cwd,
                env=env,
                check=False,
            )
            output = ""
            if result.stdout.strip():
                output += result.stdout.strip()
            if result.stderr.strip():
                if output:
                    output += "\n"
                output += f"[stderr] {result.stderr.strip()}"
            if result.returncode != 0:
                if not output:
                    output = f"[ERROR exit {result.returncode}]"
                return False, output
            return True, output or "[no output]"
        except subprocess.TimeoutExpired:
            return False, "[ERREUR TIMEOUT] La commande a bloqué le terminal plus de 45s. Si tu lances une interface graphique (GUI) ou un serveur, tu DOIS détacher le processus en utilisant 'nohup <commande> >/dev/null 2>&1 &'."  # noqa: E501
        except Exception as exc:
            return False, f"[ERROR: {exc}]"

    def write_file(self, path: str, content: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        try:
            parent = os.path.dirname(os.path.abspath(normalized))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(normalized, "w") as handle:
                handle.write(content)
            return True, f"Successfully wrote {len(content)} chars to {normalized}", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR writing file {normalized}: {exc}", {"path": normalized}

    def read_file(self, path: str, start_line: int, end_line: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading file")
        if denied is not None:
            return denied
        try:
            with open(normalized, "r") as handle:
                lines = handle.readlines()
            total = len(lines)
            sl = max(0, int(start_line) - 1)
            el = min(total, int(end_line))
            snippet = "".join(lines[sl:el])
            return True, f"Read {el - sl} lines from {normalized}", {
                "path": normalized,
                "total": total,
                "start": sl + 1,
                "end": el,
                "snippet": snippet,
            }
        except Exception as exc:
            return False, f"ERROR reading file {normalized}: {exc}", {"path": normalized}

    def list_dir(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="listing directory")
        if denied is not None:
            return denied
        try:
            items = self._filter_internal_names(normalized, os.listdir(normalized))
            details: list[dict[str, Any]] = []
            for item in items:
                full_path = os.path.join(normalized, item)
                if os.path.isdir(full_path):
                    details.append({"type": "dir", "name": item})
                else:
                    details.append({"type": "file", "name": item, "size": os.path.getsize(full_path)})
            return True, f"Listed {len(items)} items in {normalized}", {"path": normalized, "items": details}
        except Exception as exc:
            return False, f"ERROR listing directory {normalized}: {exc}", {"path": normalized}

    def open_file(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="opening file")
        if denied is not None:
            return denied
        if not os.path.exists(normalized):
            return False, f"ERROR opening file {normalized}: file_not_found", {"path": normalized}
        ok, detail = self._spawn_detached(["xdg-open", normalized])
        return ok, (f"Opened file: {normalized}" if ok else f"ERROR opening file {normalized}: {detail}"), {"path": normalized}

    def open_url(self, url: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = str(url or "").strip()
        if not re.match(r"^https?://", normalized, re.IGNORECASE):
            return False, f"ERROR opening URL {normalized}: invalid_url", {"url": normalized}
        ok, detail = self._spawn_detached(["xdg-open", normalized])
        return ok, (f"Opened URL: {normalized}" if ok else f"ERROR opening URL {normalized}: {detail}"), {"url": normalized}

    def reveal_path(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="revealing path")
        if denied is not None:
            return denied
        target = normalized if os.path.isdir(normalized) else os.path.dirname(normalized)
        if not target:
            target = self._command_cwd()
        if not os.path.exists(normalized):
            return False, f"ERROR revealing path {normalized}: path_not_found", {"path": normalized, "directory": target}
        ok, detail = self._spawn_detached(["xdg-open", target])
        return ok, (
            f"Opened parent directory for: {normalized}"
            if ok
            else f"ERROR revealing path {normalized}: {detail}"
        ), {"path": normalized, "directory": target}

    def copy_to_clipboard(self, text: str) -> tuple[bool, str, dict[str, Any]]:
        value = str(text or "")
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        commands = (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        )
        last_error = "clipboard_tool_unavailable"
        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd,
                    input=value,
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                    env=env,
                    check=False,
                )
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                try:
                    verify = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-o"],
                        capture_output=True,
                        text=True,
                        timeout=4.0,
                        env=env,
                        check=False,
                    )
                    if verify.returncode == 0 and str(verify.stdout or "") == value:
                        return True, f"Copied text to clipboard ({len(value)} chars)", {"length": len(value)}
                except Exception:
                    pass
                last_error = "clipboard_timeout"
                continue
            except Exception as exc:
                last_error = str(exc)
                continue
            if result.returncode == 0:
                return True, f"Copied text to clipboard ({len(value)} chars)", {"length": len(value)}
            last_error = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
        return False, f"ERROR copying to clipboard: {last_error}", {"length": len(value)}

    def paste(self) -> tuple[bool, str, dict[str, Any]]:
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        clipboard_text = ""
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=4.0,
                    env=env,
                    check=False,
                )
            except FileNotFoundError:
                continue
            except Exception as exc:
                return False, f"ERROR pasting clipboard: {exc}", {}
            if result.returncode == 0:
                clipboard_text = str(result.stdout or "")
                break
        if not clipboard_text:
            return False, "ERROR pasting clipboard: clipboard_empty", {}
        ok, detail = self._run_ok(
            ["xdotool", "type", "--delay", "12", "--clearmodifiers", clipboard_text],
            timeout=max(8.0, min(40.0, len(clipboard_text) / 3.0 + 4.0)),
        )
        return ok, (
            f"Pasted clipboard into active field"
            if ok else f"ERROR pasting clipboard: {detail}"
        ), {"length": len(clipboard_text)}

    def read_clipboard(self) -> tuple[bool, str, dict[str, Any]]:
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        last_error = "clipboard_unavailable"
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=4.0,
                    env=env,
                    check=False,
                )
            except FileNotFoundError:
                continue
            except Exception as exc:
                return False, f"ERROR reading clipboard: {exc}", {}
            if result.returncode == 0:
                text = str(result.stdout or "")
                return True, f"Read clipboard ({len(text)} chars)", {"text": text, "length": len(text)}
            last_error = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
        return False, f"ERROR reading clipboard: {last_error}", {}

    def focus_window(self, app_or_title: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(app_or_title or "").strip().lower()
        if not query:
            return False, "ERROR focusing window: empty_query", {"query": query}
        try:
            result = self._run(["wmctrl", "-lx"], timeout=5.0)
        except Exception as exc:
            return False, f"ERROR focusing window: {exc}", {"query": query}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR focusing window: {detail}", {"query": query}
        for line in result.stdout.splitlines():
            low = line.lower()
            if query in low:
                win_id = line.split()[0]
                ok, detail = self._run_ok(["wmctrl", "-ia", win_id], timeout=5.0)
                title = line.split(None, 4)[-1] if len(line.split(None, 4)) >= 5 else line
                return ok, (
                    f"Focused window: {title}" if ok else f"ERROR focusing window {title}: {detail}"
                ), {"query": query, "window": title}
        return False, f"ERROR focusing window: no_match_for_{query}", {"query": query}

    def open_app(self, app_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(app_name or "").strip()
        command = self._resolve_app_command(raw)
        commands: list[list[str]] = [command] if command else []
        if not commands:
            return False, "ERROR opening app: empty_app_name", {"app": raw}
        last_error = "app_launch_failed"
        for cmd in commands:
            ok, detail = self._spawn_detached(cmd)
            if ok:
                return True, f"Opened app: {raw}", {"app": raw, "command": cmd}
            last_error = detail
        return False, f"ERROR opening app {raw}: {last_error}", {"app": raw}

    def search_files(self, pattern: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(pattern or "").strip()
        normalized_root = self._normalize_path(root or self.home_dir)
        if not query:
            return False, "ERROR searching files: empty_pattern", {"pattern": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR searching files: invalid_root {normalized_root}", {"pattern": query, "root": normalized_root}
        denied = self._deny_internal_runtime_access(normalized_root, action="searching files", extra={"pattern": query, "root": normalized_root})
        if denied is not None:
            return denied
        query_low = query.lower()
        matches: list[str] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                self._prune_internal_walk(current_root, dirnames)
                for name in filenames:
                    full_path = os.path.join(current_root, name)
                    if self._is_internal_runtime_path(full_path):
                        continue
                    rel_path = os.path.relpath(full_path, normalized_root)
                    haystack = f"{name} {rel_path} {full_path}".lower()
                    if query_low in haystack:
                        matches.append(full_path)
                        if len(matches) >= 100:
                            break
                if len(matches) >= 100:
                    break
        except Exception as exc:
            return False, f"ERROR searching files in {normalized_root}: {exc}", {"pattern": query, "root": normalized_root}
        return True, f"Found {len(matches)} file(s) matching '{query}' in {normalized_root}", {
            "pattern": query,
            "root": normalized_root,
            "matches": matches,
        }

    def select_file_for_active_dialog(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        if not os.path.exists(normalized):
            return False, f"ERROR selecting file {normalized}: file_not_found", {"path": normalized}
        ok, detail, _payload = self.copy_to_clipboard(normalized)
        if not ok:
            return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        ok, detail = self._run_ok(["xdotool", "key", "--clearmodifiers", "ctrl+l"], timeout=6.0)
        if not ok:
            return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        time.sleep(0.18)
        ok, detail, _payload = self.paste()
        if not ok:
            return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        time.sleep(0.18)
        ok, detail = self._run_ok(["xdotool", "key", "--clearmodifiers", "Return"], timeout=6.0)
        if not ok:
            return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        return True, f"Selected file in active dialog: {normalized}", {"path": normalized}

    def upload_file_to_active_app(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        if not os.path.isfile(normalized):
            return False, f"ERROR uploading file {normalized}: file_not_found", {"path": normalized}
        ok, detail, payload = self.select_file_for_active_dialog(normalized)
        if not ok:
            return False, f"ERROR uploading file {normalized}: {detail}", {"path": normalized}
        return True, f"Selected file for active app upload: {normalized}", dict(payload)

    def drag_file_to_target(self, path: str, x: int, y: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        if not os.path.exists(normalized):
            return False, f"ERROR dragging file {normalized}: file_not_found", {"path": normalized, "x": int(x), "y": int(y)}
        thunar_cmd = shutil.which("thunar")
        if thunar_cmd:
            ok, detail = self._spawn_detached([thunar_cmd, "--select", normalized])
        else:
            ok, detail, _payload = self.reveal_path(normalized)
        if not ok:
            return False, f"ERROR dragging file {normalized}: {detail}", {"path": normalized, "x": int(x), "y": int(y)}
        time.sleep(0.8)
        self.focus_window("thunar")
        geometry = {"x": 180, "y": 180, "width": 900, "height": 700}
        try:
            result = self._run(["bash", "-lc", "xdotool getactivewindow getwindowgeometry --shell"], timeout=6.0)
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip().lower()
                    if key in geometry:
                        geometry[key] = int(value.strip())
        except Exception:
            pass
        src_x = int(geometry["x"]) + max(80, min(220, int(geometry["width"]) // 3))
        src_y = int(geometry["y"]) + max(120, min(220, int(geometry["height"]) // 4))
        ok, detail = self._run_ok(
            [
                "xdotool",
                "mousemove",
                str(src_x),
                str(src_y),
                "mousedown",
                "1",
                "mousemove",
                "--sync",
                str(int(x)),
                str(int(y)),
                "mouseup",
                "1",
            ],
            timeout=10.0,
        )
        if not ok:
            return False, f"ERROR dragging file {normalized}: {detail}", {
                "path": normalized,
                "x": int(x),
                "y": int(y),
                "source_x": src_x,
                "source_y": src_y,
            }
        return True, f"Dragged file toward target: {normalized}", {
            "path": normalized,
            "x": int(x),
            "y": int(y),
            "source_x": src_x,
            "source_y": src_y,
        }

    def list_windows(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = self._run(["wmctrl", "-lx"], timeout=5.0)
        except Exception as exc:
            return False, f"ERROR listing windows: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing windows: {detail}", {}
        windows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            windows.append(
                {
                    "id": parts[0],
                    "desktop": parts[1],
                    "wm_class": parts[2],
                    "host": parts[3],
                    "title": parts[4],
                }
            )
        return True, f"Listed {len(windows)} window(s)", {"windows": windows}

    def make_dir(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        try:
            os.makedirs(normalized, exist_ok=True)
        except Exception as exc:
            return False, f"ERROR creating directory {normalized}: {exc}", {"path": normalized}
        return True, f"Created directory: {normalized}", {"path": normalized}

    def move_path(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = self._normalize_path(src)
        target = self._normalize_path(dst)
        if not os.path.exists(source):
            return False, f"ERROR moving path {source}: path_not_found", {"src": source, "dst": target}
        if self._is_protected_path(source):
            return False, f"ERROR moving path {source}: protected_path", {"src": source, "dst": target}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(source, target)
        except Exception as exc:
            return False, f"ERROR moving path {source} -> {target}: {exc}", {"src": source, "dst": target}
        return True, f"Moved path: {source} -> {target}", {"src": source, "dst": target}

    def copy_path(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = self._normalize_path(src)
        target = self._normalize_path(dst)
        if not os.path.exists(source):
            return False, f"ERROR copying path {source}: path_not_found", {"src": source, "dst": target}
        if self._is_protected_path(source) or self._is_internal_runtime_path(target):
            return False, f"ERROR copying path {source}: protected_path", {"src": source, "dst": target}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(source):
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        except Exception as exc:
            return False, f"ERROR copying path {source} -> {target}: {exc}", {"src": source, "dst": target}
        return True, f"Copied path: {source} -> {target}", {"src": source, "dst": target}

    def delete_path(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        if not os.path.exists(normalized):
            return False, f"ERROR deleting path {normalized}: path_not_found", {"path": normalized}
        if self._is_protected_path(normalized):
            return False, f"ERROR deleting path {normalized}: protected_path", {"path": normalized}
        try:
            if os.path.isdir(normalized) and not os.path.islink(normalized):
                shutil.rmtree(normalized)
            else:
                os.remove(normalized)
        except Exception as exc:
            return False, f"ERROR deleting path {normalized}: {exc}", {"path": normalized}
        return True, f"Deleted path: {normalized}", {"path": normalized}

    def append_file(self, path: str, content: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        value = str(content or "")
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(normalized, "a", encoding="utf-8") as handle:
                handle.write(value)
        except Exception as exc:
            return False, f"ERROR appending file {normalized}: {exc}", {"path": normalized}
        return True, f"Appended {len(value)} chars to {normalized}", {"path": normalized, "length": len(value)}

    def replace_in_file(self, path: str, search: str, replace: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        needle = str(search or "")
        replacement = str(replace or "")
        if not needle:
            return False, f"ERROR replacing in file {normalized}: empty_search", {"path": normalized}
        try:
            with open(normalized, "r", encoding="utf-8", errors="replace") as handle:
                original = handle.read()
        except Exception as exc:
            return False, f"ERROR replacing in file {normalized}: {exc}", {"path": normalized}
        count = original.count(needle)
        if count == 0:
            return False, f"ERROR replacing in file {normalized}: no_match", {"path": normalized, "search": needle}
        updated = original.replace(needle, replacement)
        try:
            with open(normalized, "w", encoding="utf-8") as handle:
                handle.write(updated)
        except Exception as exc:
            return False, f"ERROR replacing in file {normalized}: {exc}", {"path": normalized}
        return True, f"Replaced {count} occurrence(s) in {normalized}", {
            "path": normalized,
            "count": count,
            "search": needle,
        }

    def tail_file(self, path: str, lines: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="tailing file")
        if denied is not None:
            return denied
        limit = max(1, min(int(lines or 20), 500))
        try:
            with open(normalized, "r", encoding="utf-8", errors="replace") as handle:
                content_lines = handle.readlines()
        except Exception as exc:
            return False, f"ERROR tailing file {normalized}: {exc}", {"path": normalized}
        tail = "".join(content_lines[-limit:])
        return True, f"Read last {min(limit, len(content_lines))} line(s) from {normalized}", {
            "path": normalized,
            "lines": tail,
            "line_count": min(limit, len(content_lines)),
        }

    def read_path(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading path")
        if denied is not None:
            return denied
        if not os.path.exists(normalized):
            return False, f"ERROR reading path {normalized}: path_not_found", {"path": normalized}
        if os.path.isdir(normalized):
            try:
                items = sorted(self._filter_internal_names(normalized, os.listdir(normalized)))
            except Exception as exc:
                return False, f"ERROR reading path {normalized}: {exc}", {"path": normalized}
            return True, f"Read directory: {normalized}", {"path": normalized, "kind": "directory", "items": items[:200]}
        try:
            raw = Path(normalized).read_bytes()
        except Exception as exc:
            return False, f"ERROR reading path {normalized}: {exc}", {"path": normalized}
        if b"\x00" in raw[:4096]:
            return False, f"ERROR reading path {normalized}: binary_file_not_supported", {"path": normalized}
        text = raw.decode("utf-8", errors="replace")
        capped = text[:200000]
        return True, f"Read file: {normalized}", {
            "path": normalized,
            "kind": "file",
            "content": capped,
            "truncated": len(capped) < len(text),
        }

    def open_with(self, path: str, app_name: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        command = self._resolve_app_command(app_name)
        denied = self._deny_internal_runtime_access(normalized, action="opening file")
        if denied is not None:
            return denied
        if not os.path.exists(normalized):
            return False, f"ERROR opening file {normalized} with {app_name}: path_not_found", {"path": normalized, "app": app_name}
        if not command:
            return False, f"ERROR opening file {normalized}: empty_app_name", {"path": normalized, "app": app_name}
        ok, detail = self._spawn_detached(command + [normalized])
        return ok, (
            f"Opened {normalized} with {app_name}" if ok else f"ERROR opening file {normalized} with {app_name}: {detail}"
        ), {"path": normalized, "app": app_name, "command": command}

    def read_clipboard_image(self) -> tuple[bool, str, dict[str, Any]]:
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        candidates = [
            ("image/png", ".png"),
            ("image/jpeg", ".jpg"),
            ("image/bmp", ".bmp"),
        ]
        last_error = "clipboard_image_unavailable"
        for mime, suffix in candidates:
            try:
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                    capture_output=True,
                    timeout=6.0,
                    env=env,
                    check=False,
                )
            except FileNotFoundError:
                last_error = "xclip_not_installed"
                break
            except Exception as exc:
                last_error = str(exc)
                continue
            if result.returncode != 0 or not result.stdout:
                last_error = (result.stderr or b"").decode("utf-8", errors="replace").strip() or f"exit_{result.returncode}"
                continue
            with tempfile.NamedTemporaryFile(prefix="aria_clipboard_", suffix=suffix, delete=False) as handle:
                handle.write(result.stdout)
                saved_path = handle.name
            payload: dict[str, Any] = {
                "path": saved_path,
                "mime": mime,
                "size_bytes": len(result.stdout),
            }
            if Image is not None:
                try:
                    with Image.open(saved_path) as img:
                        payload["width"] = int(img.width)
                        payload["height"] = int(img.height)
                except Exception:
                    pass
            return True, f"Saved clipboard image to {saved_path}", payload
        return False, f"ERROR reading clipboard image: {last_error}", {}

    def replace_regex_in_file(self, path: str, pattern: str, replace: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        regex = str(pattern or "")
        replacement = str(replace or "")
        if not regex:
            return False, f"ERROR regex replacing in file {normalized}: empty_pattern", {"path": normalized}
        try:
            compiled = re.compile(regex, flags=re.MULTILINE)
            original = Path(normalized).read_text(encoding="utf-8", errors="replace")
            updated, count = compiled.subn(replacement, original)
            if count == 0:
                return False, f"ERROR regex replacing in file {normalized}: no_match", {"path": normalized, "pattern": regex}
            Path(normalized).write_text(updated, encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR regex replacing in file {normalized}: {exc}", {"path": normalized}
        return True, f"Regex replaced {count} occurrence(s) in {normalized}", {"path": normalized, "count": count, "pattern": regex}

    def grep_in_files(self, pattern: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(pattern or "").strip()
        normalized_root = self._normalize_path(root or self.home_dir)
        if not query:
            return False, "ERROR grepping files: empty_pattern", {"pattern": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR grepping files: invalid_root {normalized_root}", {"pattern": query, "root": normalized_root}
        denied = self._deny_internal_runtime_access(normalized_root, action="grepping files", extra={"pattern": query, "root": normalized_root})
        if denied is not None:
            return denied
        try:
            regex = re.compile(query, flags=re.IGNORECASE)
            use_regex = True
        except re.error:
            regex = None
            use_regex = False
            query_low = query.lower()
        matches: list[dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                self._prune_internal_walk(current_root, dirnames)
                for name in filenames:
                    full_path = os.path.join(current_root, name)
                    if self._is_internal_runtime_path(full_path):
                        continue
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as handle:
                            for line_number, line in enumerate(handle, start=1):
                                hay = line if use_regex else line.lower()
                                matched = bool(regex.search(line)) if use_regex else (query_low in hay)
                                if matched:
                                    matches.append(
                                        {
                                            "path": full_path,
                                            "line": line_number,
                                            "text": line.rstrip("\n")[:400],
                                        }
                                    )
                                    if len(matches) >= 200:
                                        break
                    except Exception:
                        continue
                    if len(matches) >= 200:
                        break
                if len(matches) >= 200:
                    break
        except Exception as exc:
            return False, f"ERROR grepping files in {normalized_root}: {exc}", {"pattern": query, "root": normalized_root}
        return True, f"Found {len(matches)} grep match(es) in {normalized_root}", {
            "pattern": query,
            "root": normalized_root,
            "matches": matches,
        }

    def stat_path(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="stating path")
        if denied is not None:
            return denied
        try:
            info = os.lstat(normalized)
        except Exception as exc:
            return False, f"ERROR stat path {normalized}: {exc}", {"path": normalized}
        kind = "symlink" if os.path.islink(normalized) else "directory" if os.path.isdir(normalized) else "file"
        return True, f"Stat path: {normalized}", {
            "path": normalized,
            "kind": kind,
            "size": int(info.st_size),
            "mode": oct(info.st_mode & 0o777),
            "mtime": float(info.st_mtime),
            "ctime": float(info.st_ctime),
        }

    def diff_paths(self, a: str, b: str) -> tuple[bool, str, dict[str, Any]]:
        left = self._normalize_path(a)
        right = self._normalize_path(b)
        denied = self._deny_internal_runtime_access(left, action="diffing path", extra={"a": left, "b": right})
        if denied is not None:
            return denied
        denied = self._deny_internal_runtime_access(right, action="diffing path", extra={"a": left, "b": right})
        if denied is not None:
            return denied
        if not os.path.exists(left) or not os.path.exists(right):
            return False, f"ERROR diffing paths {left} vs {right}: path_not_found", {"a": left, "b": right}
        if os.path.isdir(left) and os.path.isdir(right):
            cmp = filecmp.dircmp(left, right)
            return True, f"Diffed directories: {left} vs {right}", {
                "a": left,
                "b": right,
                "kind": "directory",
                "left_only": cmp.left_only,
                "right_only": cmp.right_only,
                "diff_files": cmp.diff_files,
            }
        try:
            left_text = Path(left).read_text(encoding="utf-8", errors="replace").splitlines()
            right_text = Path(right).read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return False, f"ERROR diffing paths {left} vs {right}: {exc}", {"a": left, "b": right}
        diff = "\n".join(
            difflib.unified_diff(left_text, right_text, fromfile=left, tofile=right, lineterm="")
        )
        compact, truncated = self._truncate_text(diff, 200000)
        return True, f"Diffed files: {left} vs {right}", {"a": left, "b": right, "kind": "file", "diff": compact, "truncated": truncated}

    def list_processes(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = self._run(["ps", "-eo", "pid=,comm=,args="], timeout=8.0)
        except Exception as exc:
            return False, f"ERROR listing processes: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing processes: {detail}", {}
        processes: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            processes.append(
                {
                    "pid": int(parts[0]),
                    "comm": parts[1],
                    "args": parts[2] if len(parts) > 2 else "",
                }
            )
            if len(processes) >= 200:
                break
        return True, f"Listed {len(processes)} process(es)", {"processes": processes}

    def kill_process(self, pid_or_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(pid_or_name or "").strip()
        if not raw:
            return False, "ERROR killing process: empty_target", {"target": raw}
        protected = {1, os.getpid(), os.getppid()}
        killed: list[int] = []
        try:
            if raw.isdigit():
                pid = int(raw)
                if pid in protected:
                    return False, f"ERROR killing process {pid}: protected_process", {"target": raw}
                os.kill(pid, 15)
                killed.append(pid)
            else:
                ok, detail, payload = self.list_processes()
                if not ok:
                    return False, detail, payload
                for proc in payload.get("processes") or []:
                    pid = int(proc.get("pid") or 0)
                    if pid in protected:
                        continue
                    haystack = f"{proc.get('comm','')} {proc.get('args','')}".lower()
                    if raw.lower() in haystack:
                        try:
                            os.kill(pid, 15)
                            killed.append(pid)
                        except Exception:
                            continue
            if not killed:
                return False, f"ERROR killing process {raw}: no_match", {"target": raw}
        except Exception as exc:
            return False, f"ERROR killing process {raw}: {exc}", {"target": raw}
        return True, f"Killed {len(killed)} process(es)", {"target": raw, "pids": killed}

    def chmod_path(self, path: str, mode: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        try:
            parsed = int(str(mode or "").strip(), 8)
            os.chmod(normalized, parsed)
        except Exception as exc:
            return False, f"ERROR chmod path {normalized}: {exc}", {"path": normalized, "mode": str(mode or "")}
        return True, f"Changed mode for {normalized} to {oct(parsed)}", {"path": normalized, "mode": oct(parsed)}

    def symlink_path(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = self._normalize_path(src)
        target = self._normalize_path(dst)
        if not os.path.exists(source):
            return False, f"ERROR symlinking path {source}: path_not_found", {"src": source, "dst": target}
        if os.path.lexists(target):
            return False, f"ERROR symlinking path {source}: destination_exists", {"src": source, "dst": target}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.symlink(source, target)
        except Exception as exc:
            return False, f"ERROR symlinking path {source} -> {target}: {exc}", {"src": source, "dst": target}
        return True, f"Created symlink: {target} -> {source}", {"src": source, "dst": target}

    def unzip_path(self, path: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = self._normalize_path(path)
        target = self._normalize_path(dst)
        if not os.path.exists(source):
            return False, f"ERROR extracting archive {source}: path_not_found", {"path": source, "dst": target}
        try:
            os.makedirs(target, exist_ok=True)
            members = 0
            lower = source.lower()
            if lower.endswith(".zip"):
                with zipfile.ZipFile(source, "r") as archive:
                    archive.extractall(target)
                    members = len(archive.namelist())
            else:
                with tarfile.open(source, "r:*") as archive:
                    archive.extractall(target)
                    members = len(archive.getmembers())
        except Exception as exc:
            return False, f"ERROR extracting archive {source}: {exc}", {"path": source, "dst": target}
        return True, f"Extracted archive {source} into {target}", {"path": source, "dst": target, "members": members}

    def archive_path(self, path: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = self._normalize_path(path)
        target = self._normalize_path(dst)
        if not os.path.exists(source):
            return False, f"ERROR archiving path {source}: path_not_found", {"path": source, "dst": target}
        if self._is_protected_path(source) or self._is_internal_runtime_path(target):
            return False, f"ERROR archiving path {source}: protected_path", {"path": source, "dst": target}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            lower = target.lower()
            if lower.endswith(".zip"):
                with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    if os.path.isdir(source):
                        for root, _dirnames, filenames in os.walk(source):
                            for name in filenames:
                                full = os.path.join(root, name)
                                archive.write(full, arcname=os.path.relpath(full, os.path.dirname(source)))
                    else:
                        archive.write(source, arcname=os.path.basename(source))
            else:
                mode = "w:gz" if lower.endswith((".tar.gz", ".tgz")) else "w"
                with tarfile.open(target, mode) as archive:
                    archive.add(source, arcname=os.path.basename(source))
        except Exception as exc:
            return False, f"ERROR archiving path {source}: {exc}", {"path": source, "dst": target}
        return True, f"Archived {source} into {target}", {"path": source, "dst": target}

    def read_json(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading JSON")
        if denied is not None:
            return denied
        try:
            data = json.loads(Path(normalized).read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return False, f"ERROR reading JSON {normalized}: {exc}", {"path": normalized}
        preview, truncated = self._truncate_text(json.dumps(data, indent=2, ensure_ascii=False), 200000)
        return True, f"Read JSON: {normalized}", {"path": normalized, "preview": preview, "truncated": truncated}

    def write_json(self, path: str, data: Any) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            Path(normalized).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR writing JSON {normalized}: {exc}", {"path": normalized}
        return True, f"Wrote JSON: {normalized}", {"path": normalized}

    def read_csv_preview(self, path: str, rows: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading CSV")
        if denied is not None:
            return denied
        limit = max(1, min(int(rows or 10), 200))
        try:
            with open(normalized, "r", encoding="utf-8", errors="replace", newline="") as handle:
                reader = csv.reader(handle)
                preview = []
                for index, row in enumerate(reader):
                    preview.append(row)
                    if index + 1 >= limit:
                        break
        except Exception as exc:
            return False, f"ERROR reading CSV {normalized}: {exc}", {"path": normalized}
        return True, f"Read CSV preview: {normalized}", {"path": normalized, "rows": preview, "row_count": len(preview)}

    def git_status(self, repo: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(repo or self._command_cwd())
        ok, detail = self._run_ok(["git", "-C", normalized, "status", "--short", "--branch"], timeout=15.0)
        if not ok:
            return False, f"ERROR git status {normalized}: {detail}", {"repo": normalized}
        result = self._run(["git", "-C", normalized, "status", "--short", "--branch"], timeout=15.0)
        output, truncated = self._truncate_text(result.stdout or "", 100000)
        return True, f"Git status: {normalized}", {"repo": normalized, "output": output, "truncated": truncated}

    def git_diff(self, repo: str, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized_repo = self._normalize_path(repo or self._command_cwd())
        cmd = ["git", "-C", normalized_repo, "diff"]
        target = str(path or "").strip()
        if target:
            cmd.extend(["--", target])
        result = self._run(cmd, timeout=20.0)
        if result.returncode not in (0, 1):
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR git diff {normalized_repo}: {detail}", {"repo": normalized_repo, "path": target}
        output, truncated = self._truncate_text(result.stdout or "", 150000)
        return True, f"Git diff: {normalized_repo}", {"repo": normalized_repo, "path": target, "output": output, "truncated": truncated}

    def run_tests(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        command, cwd = self._detect_test_command(target)
        if not command:
            return False, f"ERROR running tests for {target}: no_test_runner_detected", {"target": target}
        env = dict(self.env)
        env.setdefault("HOME", self.home_dir)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180.0,
                env=env,
                cwd=cwd,
                check=False,
            )
        except Exception as exc:
            return False, f"ERROR running tests in {cwd}: {exc}", {"target": target, "cwd": cwd, "command": command}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        compact, truncated = self._truncate_text(output, 150000)
        ok = result.returncode == 0
        detail = f"Ran tests in {cwd} (exit {result.returncode})"
        return ok, detail, {"target": target, "cwd": cwd, "command": command, "output": compact, "truncated": truncated}

    def download_file(self, url: str, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized_url = str(url or "").strip()
        normalized_path = self._normalize_path(path)
        if not re.match(r"^https?://", normalized_url, re.IGNORECASE):
            return False, f"ERROR downloading file {normalized_url}: invalid_url", {"url": normalized_url, "path": normalized_path}
        try:
            parent = os.path.dirname(normalized_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with urllib.request.urlopen(normalized_url, timeout=30.0) as response:
                data = response.read()
            Path(normalized_path).write_bytes(data)
        except Exception as exc:
            return False, f"ERROR downloading file {normalized_url}: {exc}", {"url": normalized_url, "path": normalized_path}
        return True, f"Downloaded file: {normalized_url} -> {normalized_path}", {"url": normalized_url, "path": normalized_path, "size_bytes": len(data)}

    def extract_text_from_pdf(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="extracting PDF text")
        if denied is not None:
            return denied
        if not os.path.exists(normalized):
            return False, f"ERROR extracting PDF text {normalized}: path_not_found", {"path": normalized}
        try:
            result = self._run(["pdftotext", normalized, "-"], timeout=20.0)
            if result.returncode == 0 and (result.stdout or "").strip():
                text, truncated = self._truncate_text(result.stdout, 200000)
                return True, f"Extracted text from PDF: {normalized}", {"path": normalized, "text": text, "truncated": truncated}
        except Exception:
            pass
        try:
            raw = Path(normalized).read_bytes()
            text = self._extract_pdf_text_fallback(raw)
            if text.strip():
                compact, truncated = self._truncate_text(text, 200000)
                return True, f"Extracted text from PDF: {normalized}", {"path": normalized, "text": compact, "truncated": truncated}
        except Exception as exc:
            return False, f"ERROR extracting PDF text {normalized}: {exc}", {"path": normalized}
        return False, f"ERROR extracting PDF text {normalized}: no_text_backend_or_text_found", {"path": normalized}

    def replace_block_in_file(self, path: str, start_marker: str, end_marker: str, content: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        start = str(start_marker or "")
        end = str(end_marker or "")
        value = str(content or "")
        if not start or not end:
            return False, f"ERROR replacing block in {normalized}: missing_marker", {"path": normalized}
        try:
            original = Path(normalized).read_text(encoding="utf-8", errors="replace")
            start_index = original.find(start)
            end_index = original.find(end, start_index + len(start))
            if start_index < 0 or end_index < 0:
                return False, f"ERROR replacing block in {normalized}: marker_not_found", {"path": normalized}
            insert_start = start_index + len(start)
            updated = original[:insert_start] + "\n" + value.rstrip("\n") + "\n" + original[end_index:]
            Path(normalized).write_text(updated, encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR replacing block in {normalized}: {exc}", {"path": normalized}
        return True, f"Replaced block in {normalized}", {"path": normalized, "start_marker": start, "end_marker": end}

    def grep_ast(self, symbol: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(symbol or "").strip()
        normalized_root = self._normalize_path(root or self.home_dir)
        if not query:
            return False, "ERROR grepping AST: empty_symbol", {"symbol": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR grepping AST: invalid_root {normalized_root}", {"symbol": query, "root": normalized_root}
        denied = self._deny_internal_runtime_access(normalized_root, action="grepping AST", extra={"symbol": query, "root": normalized_root})
        if denied is not None:
            return denied
        matches: list[dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                self._prune_internal_walk(current_root, dirnames)
                for name in filenames:
                    full_path = os.path.join(current_root, name)
                    if self._is_internal_runtime_path(full_path):
                        continue
                    if name.endswith(".py"):
                        try:
                            source = Path(full_path).read_text(encoding="utf-8", errors="replace")
                            tree = ast.parse(source, filename=full_path)
                            for node in ast.walk(tree):
                                node_name = getattr(node, "name", None)
                                if node_name == query:
                                    matches.append({"path": full_path, "line": getattr(node, "lineno", 0), "kind": type(node).__name__})
                                elif isinstance(node, ast.Name) and node.id == query:
                                    matches.append({"path": full_path, "line": getattr(node, "lineno", 0), "kind": "Name"})
                                if len(matches) >= 200:
                                    break
                        except Exception:
                            continue
                    elif name.endswith((".js", ".ts", ".tsx", ".jsx")):
                        try:
                            for line_number, line in enumerate(Path(full_path).read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                                if re.search(rf"\\b{re.escape(query)}\\b", line):
                                    matches.append({"path": full_path, "line": line_number, "kind": "text_fallback"})
                                    if len(matches) >= 200:
                                        break
                        except Exception:
                            continue
                    if len(matches) >= 200:
                        break
                if len(matches) >= 200:
                    break
        except Exception as exc:
            return False, f"ERROR grepping AST in {normalized_root}: {exc}", {"symbol": query, "root": normalized_root}
        return True, f"Found {len(matches)} AST/code match(es) for '{query}'", {"symbol": query, "root": normalized_root, "matches": matches}

    def read_yaml(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading YAML")
        if denied is not None:
            return denied
        try:
            if yaml is not None:
                data = yaml.safe_load(Path(normalized).read_text(encoding="utf-8", errors="replace"))
                mode = "yaml"
            else:
                ok, data, mode = self._read_yaml_basic(normalized)
                if not ok:
                    return False, f"ERROR reading YAML {normalized}: parse_failed", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR reading YAML {normalized}: {exc}", {"path": normalized}
        preview, truncated = self._truncate_text(json.dumps(data, indent=2, ensure_ascii=False), 200000)
        return True, f"Read YAML: {normalized}", {"path": normalized, "data": data, "preview": preview, "truncated": truncated, "parser": mode}

    def write_yaml(self, path: str, data: Any) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            Path(normalized).write_text(self._write_yaml_basic(data), encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR writing YAML {normalized}: {exc}", {"path": normalized}
        return True, f"Wrote YAML: {normalized}", {"path": normalized, "parser": "yaml" if yaml is not None else "json_fallback"}

    def read_json_schema(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading JSON schema")
        if denied is not None:
            return denied
        try:
            data = json.loads(Path(normalized).read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return False, f"ERROR reading JSON schema {normalized}: {exc}", {"path": normalized}
        schema = self._schema_for_value(data)
        preview, truncated = self._truncate_text(json.dumps(schema, indent=2, ensure_ascii=False), 200000)
        return True, f"Read JSON schema: {normalized}", {"path": normalized, "schema": schema, "preview": preview, "truncated": truncated}

    def read_yaml_schema(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="reading YAML schema")
        if denied is not None:
            return denied
        try:
            if yaml is not None:
                data = yaml.safe_load(Path(normalized).read_text(encoding="utf-8", errors="replace"))
            else:
                ok, data, _mode = self._read_yaml_basic(normalized)
                if not ok:
                    return False, f"ERROR reading YAML schema {normalized}: parse_failed", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR reading YAML schema {normalized}: {exc}", {"path": normalized}
        schema = self._schema_for_value(data)
        preview, truncated = self._truncate_text(json.dumps(schema, indent=2, ensure_ascii=False), 200000)
        return True, f"Read YAML schema: {normalized}", {"path": normalized, "schema": schema, "preview": preview, "truncated": truncated}

    def list_ports(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = self._run(["ss", "-ltnupH"], timeout=8.0)
        except Exception as exc:
            return False, f"ERROR listing ports: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing ports: {detail}", {}
        ports: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[4]
            host = local
            port: int | None = None
            match = re.search(r":(\d+)$", local)
            if match:
                port = int(match.group(1))
                host = local[: match.start()] or local
            ports.append({"proto": parts[0], "state": parts[1], "local": local, "host": host, "port": port, "process": parts[-1] if parts else ""})
            if len(ports) >= 200:
                break
        return True, f"Listed {len(ports)} listening port(s)", {"ports": ports}

    def process_tree(self, pid_or_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(pid_or_name or "").strip()
        try:
            result = self._run(["ps", "-eo", "pid=,ppid=,comm=,args="], timeout=8.0)
        except Exception as exc:
            return False, f"ERROR building process tree: {exc}", {"target": raw}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR building process tree: {detail}", {"target": raw}
        processes: dict[int, dict[str, Any]] = {}
        children: dict[int, list[int]] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            pid = int(parts[0]); ppid = int(parts[1]); comm = parts[2]; args = parts[3] if len(parts) > 3 else ""
            processes[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "args": args}
            children.setdefault(ppid, []).append(pid)
        root_pids: list[int] = []
        if raw.isdigit():
            pid = int(raw)
            if pid in processes:
                root_pids = [pid]
        else:
            needle = raw.lower()
            root_pids = [pid for pid, proc in processes.items() if needle and needle in f"{proc['comm']} {proc['args']}".lower()]
        if not root_pids:
            return False, f"ERROR building process tree for {raw}: no_match", {"target": raw}
        seen: set[int] = set()
        tree: list[dict[str, Any]] = []
        stack = [(pid, 0) for pid in root_pids[:20]]
        while stack:
            pid, depth = stack.pop(0)
            if pid in seen or pid not in processes:
                continue
            seen.add(pid)
            proc = dict(processes[pid])
            proc["depth"] = depth
            tree.append(proc)
            for child in children.get(pid, [])[:100]:
                stack.append((child, depth + 1))
            if len(tree) >= 300:
                break
        return True, f"Built process tree for {raw}", {"target": raw, "tree": tree, "processes": tree}

    def disk_usage(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        denied = self._deny_internal_runtime_access(normalized, action="computing disk usage")
        if denied is not None:
            return denied
        if not os.path.exists(normalized):
            return False, f"ERROR disk usage {normalized}: path_not_found", {"path": normalized}
        total = 0
        file_count = 0
        dir_count = 0
        try:
            if os.path.isfile(normalized):
                total = os.path.getsize(normalized)
                file_count = 1
            else:
                for current_root, dirnames, filenames in os.walk(normalized):
                    self._prune_internal_walk(current_root, dirnames)
                    dir_count += len(dirnames)
                    for name in filenames:
                        full_path = os.path.join(current_root, name)
                        if self._is_internal_runtime_path(full_path):
                            continue
                        try:
                            total += os.path.getsize(full_path)
                            file_count += 1
                        except Exception:
                            continue
        except Exception as exc:
            return False, f"ERROR disk usage {normalized}: {exc}", {"path": normalized}
        return True, f"Computed disk usage for {normalized}", {"path": normalized, "size_bytes": total, "bytes": total, "size_mb": round(total / (1024 * 1024), 3), "files": file_count, "dirs": dir_count}

    def find_large_files(self, root: str, limit_mb: float) -> tuple[bool, str, dict[str, Any]]:
        normalized_root = self._normalize_path(root or self.home_dir)
        threshold = max(float(limit_mb or 1), 0.001) * 1024 * 1024
        if not os.path.isdir(normalized_root):
            return False, f"ERROR finding large files: invalid_root {normalized_root}", {"root": normalized_root}
        denied = self._deny_internal_runtime_access(normalized_root, action="finding large files", extra={"root": normalized_root})
        if denied is not None:
            return denied
        matches: list[dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                self._prune_internal_walk(current_root, dirnames)
                for name in filenames:
                    full_path = os.path.join(current_root, name)
                    if self._is_internal_runtime_path(full_path):
                        continue
                    try:
                        size = os.path.getsize(full_path)
                    except Exception:
                        continue
                    if size >= threshold:
                        matches.append({"path": full_path, "size_bytes": size, "size_mb": round(size / (1024 * 1024), 3)})
                        if len(matches) >= 200:
                            break
                if len(matches) >= 200:
                    break
        except Exception as exc:
            return False, f"ERROR finding large files in {normalized_root}: {exc}", {"root": normalized_root}
        matches.sort(key=lambda item: item.get("size_bytes", 0), reverse=True)
        return True, f"Found {len(matches)} file(s) >= {round(threshold / (1024 * 1024), 3)} MB", {"root": normalized_root, "matches": matches, "files": matches}

    def env_get(self, name: str) -> tuple[bool, str, dict[str, Any]]:
        key = str(name or "").strip()
        if not key:
            return False, "ERROR env_get: empty_name", {"name": key}
        value = self.env.get(key, os.environ.get(key))
        if value is None:
            return False, f"ERROR env_get {key}: not_set", {"name": key}
        return True, f"Read environment variable: {key}", {"name": key, "value": str(value)}

    def env_set_local(self, name: str, value: str) -> tuple[bool, str, dict[str, Any]]:
        key = str(name or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            return False, f"ERROR env_set_local {key}: invalid_name", {"name": key}
        self.env[key] = str(value or "")
        os.environ[key] = str(value or "")
        try:
            self._save_local_env()
        except Exception as exc:
            return False, f"ERROR env_set_local {key}: {exc}", {"name": key}
        return True, f"Set local environment variable: {key}", {"name": key, "value": str(value or "")}

    def http_request(self, method: str, url: str, headers: dict[str, Any] | None, body: Any) -> tuple[bool, str, dict[str, Any]]:
        http_method = str(method or "GET").upper()
        normalized_url = str(url or "").strip()
        if not re.match(r"^https?://", normalized_url, re.IGNORECASE):
            return False, f"ERROR http_request {normalized_url}: invalid_url", {"url": normalized_url}
        request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        payload = None
        if body not in (None, ""):
            if isinstance(body, (dict, list)):
                payload = json.dumps(body).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            else:
                payload = str(body).encode("utf-8")
        try:
            req = urllib.request.Request(normalized_url, data=payload, headers=request_headers, method=http_method)
            with urllib.request.urlopen(req, timeout=30.0) as response:
                response_body = response.read()
                status = getattr(response, "status", 200)
                resp_headers = dict(response.getheaders())
        except Exception as exc:
            return False, f"ERROR http_request {normalized_url}: {exc}", {"url": normalized_url, "method": http_method}
        text = response_body.decode("utf-8", errors="replace")
        preview, truncated = self._truncate_text(text, 200000)
        return True, f"HTTP {http_method} {normalized_url} -> {status}", {"url": normalized_url, "method": http_method, "status": status, "headers": resp_headers, "body": preview, "text": preview, "truncated": truncated}

    def sqlite_query(self, path: str, sql: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        query = str(sql or "").strip()
        if not query:
            return False, f"ERROR sqlite_query {normalized}: empty_sql", {"path": normalized}
        conn = None
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(normalized)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query)
            lower = query.lower()
            if lower.startswith(("select", "pragma", "with")):
                rows = [dict(row) for row in cur.fetchmany(200)]
                conn.commit()
                return True, f"SQLite query returned {len(rows)} row(s)", {"path": normalized, "rows": rows}
            affected = cur.rowcount
            conn.commit()
            return True, f"SQLite query applied to {normalized}", {"path": normalized, "rows_affected": affected}
        except Exception as exc:
            return False, f"ERROR sqlite_query {normalized}: {exc}", {"path": normalized}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def docker_ps(self) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("docker", path=self.env.get("PATH")) is None:
            return False, "ERROR docker_ps: docker_not_installed", {}
        try:
            result = self._run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=20.0)
        except Exception as exc:
            return False, f"ERROR docker_ps: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR docker_ps: {detail}", {}
        containers: list[dict[str, Any]] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                containers.append(json.loads(line))
            except Exception:
                containers.append({"raw": line})
        return True, f"Listed {len(containers)} container(s)", {"containers": containers}

    def docker_logs(self, container: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(container or "").strip()
        if not target:
            return False, "ERROR docker_logs: empty_container", {"container": target}
        if shutil.which("docker", path=self.env.get("PATH")) is None:
            return False, "ERROR docker_logs: docker_not_installed", {"container": target}
        try:
            result = self._run(["docker", "logs", "--tail", "200", target], timeout=30.0)
        except Exception as exc:
            return False, f"ERROR docker_logs {target}: {exc}", {"container": target}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR docker_logs {target}: {detail}", {"container": target}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text(output, 150000)
        return True, f"Docker logs: {target}", {"container": target, "output": preview, "truncated": truncated}

    def systemctl_status(self, service: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(service or "").strip()
        if not target:
            return False, "ERROR systemctl_status: empty_service", {"service": target}
        attempts = [
            ("system", ["systemctl", "status", target, "--no-pager", "--full"]),
            ("user", ["systemctl", "--user", "status", target, "--no-pager", "--full"]),
        ]
        for mode, cmd in attempts:
            try:
                result = self._run(cmd, timeout=20.0)
            except Exception:
                continue
            if result.returncode == 0:
                output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
                preview, truncated = self._truncate_text(output, 150000)
                active = ""
                for line in output.splitlines():
                    if "Active:" in line:
                        active = line.strip()
                        break
                return True, f"Systemctl status ({mode}): {target}", {"service": target, "mode": mode, "output": preview, "truncated": truncated, "active": active}
        return False, f"ERROR systemctl_status {target}: status_failed", {"service": target}

    def systemctl_restart(self, service: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(service or "").strip()
        if not target:
            return False, "ERROR systemctl_restart: empty_service", {"service": target}
        attempts = [
            ("system", ["systemctl", "restart", target]),
            ("user", ["systemctl", "--user", "restart", target]),
            ("sudo", ["sudo", "-n", "systemctl", "restart", target]),
        ]
        errors: list[str] = []
        for mode, cmd in attempts:
            try:
                result = self._run(cmd, timeout=25.0)
            except Exception as exc:
                errors.append(f"{mode}:{exc}")
                continue
            if result.returncode == 0:
                status_ok, status_detail, payload = self.systemctl_status(target)
                response = {"service": target, "mode": mode}
                if status_ok:
                    response.update(payload)
                return True, f"Restarted service ({mode}): {target}", response
            errors.append(f"{mode}:{(result.stderr or result.stdout or '').strip() or f'exit_{result.returncode}'}")
        return False, f"ERROR systemctl_restart {target}: {' | '.join(errors[:3])}", {"service": target}

    def ffmpeg_run(self, args: Any) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("ffmpeg", path=self.env.get("PATH")) is None:
            return False, "ERROR ffmpeg_run: ffmpeg_not_installed", {}
        if isinstance(args, list):
            argv = [str(item) for item in args if str(item).strip()]
        else:
            argv = shlex.split(str(args or "").strip())
        if argv and argv[0] == "ffmpeg":
            argv = argv[1:]
        if not argv:
            return False, "ERROR ffmpeg_run: empty_args", {}
        try:
            result = self._run(["ffmpeg", *argv], timeout=240.0)
        except Exception as exc:
            return False, f"ERROR ffmpeg_run: {exc}", {"args": argv}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text(output, 200000)
        ok = result.returncode == 0
        return ok, f"ffmpeg run exit {result.returncode}", {"args": argv, "output": preview, "truncated": truncated}

    def blender_render(self, script: str, output_path: str, size: Any) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("blender", path=self.env.get("PATH")) is None:
            return False, "ERROR blender_render: blender_not_installed", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_blender_render.png")
        width, height = self._parse_image_size(size)
        image_format = self._image_format_for_path(normalized)
        user_script = str(script or "").strip()
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        wrapper = f"""
import bpy
import os

bpy.ops.wm.read_factory_settings(use_empty=False)
scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE_NEXT'
scene.render.resolution_x = {width}
scene.render.resolution_y = {height}
scene.render.image_settings.file_format = {image_format!r}
scene.render.filepath = {normalized!r}

{user_script}

scene = bpy.context.scene
scene.render.resolution_x = {width}
scene.render.resolution_y = {height}
scene.render.image_settings.file_format = {image_format!r}
scene.render.filepath = {normalized!r}
bpy.ops.render.render(write_still=True)
"""
        script_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="aria_blender_render_", suffix=".py", delete=False) as handle:
                script_path = handle.name
                handle.write(wrapper.encode("utf-8"))
            result = self._run(["blender", "--background", "--python", script_path], timeout=420.0)
        except Exception as exc:
            return False, f"ERROR blender_render: {exc}", {"path": normalized}
        finally:
            try:
                os.unlink(script_path)
            except Exception:
                pass
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text(output, 200000)
        ok = result.returncode == 0 and os.path.exists(normalized)
        payload = {
            "path": normalized,
            "width": width,
            "height": height,
            "output": preview,
            "truncated": truncated,
        }
        return ok, f"blender render exit {result.returncode}", payload

    def compose_video_from_images(self, inputs: Any, output_path: str, fps: float = 24.0) -> tuple[bool, str, dict[str, Any]]:
        if ImageSequenceClip is None:
            return False, "ERROR compose_video_from_images: moviepy_not_available", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_slideshow.mp4")
        frames = self._resolve_image_sequence_inputs(inputs)
        if not frames:
            return False, "ERROR compose_video_from_images: no_input_images", {"path": normalized}
        frame_rate = max(1.0, min(float(fps or 24.0), 60.0))
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        try:
            clip = ImageSequenceClip(frames, fps=frame_rate)
            clip.write_videofile(
                normalized,
                codec="libx264",
                audio=False,
                fps=frame_rate,
                logger=None,
            )
            clip.close()
        except Exception as exc:
            return False, f"ERROR compose_video_from_images: {exc}", {"path": normalized, "inputs": frames[:20]}
        return True, f"Composed video from {len(frames)} image(s)", {"path": normalized, "inputs": frames, "fps": frame_rate}

    def generate_image_ai(self, prompt: str, output_path: str, size: str = "1024x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR generate_image_ai: empty_prompt", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_generated_ai.png")
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        ok, detail, payload = self._openai_image_request(
            "/images/generations",
            {
                "model": "gpt-image-1",
                "prompt": value,
                "size": str(size or "1024x1024"),
                "quality": str(quality or "low"),
            },
            timeout=180.0,
        )
        if not ok:
            return False, detail, payload
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        if not data or not isinstance(data[0], dict) or not data[0].get("b64_json"):
            return False, "ERROR generate_image_ai: missing_b64_json", {"response": payload}
        try:
            image_bytes = base64.b64decode(str(data[0].get("b64_json") or ""))
            Path(normalized).write_bytes(image_bytes)
        except Exception as exc:
            return False, f"ERROR generate_image_ai: save_failed:{exc}", {"path": normalized}
        return True, f"Generated AI image: {normalized}", {"path": normalized, "size": str(size or '1024x1024'), "quality": str(quality or 'low')}

    def upscale_image_ai(self, input_path: str, output_path: str, size: str = "1536x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        ok_image, data_url, detail = self._image_data_url(input_path)
        if not ok_image:
            return False, f"ERROR upscale_image_ai: {detail}", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_upscaled_ai.png")
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        prompt = (
            "Create a faithful higher-resolution upscale of this image. "
            "Preserve the composition, colors, text, layout, and subject as closely as possible. "
            "Do not add or remove objects. Improve sharpness and clarity only."
        )
        ok, detail_req, payload = self._openai_image_request(
            "/images/edits",
            {
                "model": "gpt-image-1",
                "prompt": prompt,
                "images": [{"image_url": data_url}],
                "size": str(size or "1536x1024"),
                "quality": str(quality or "low"),
                "input_fidelity": "high",
            },
            timeout=240.0,
        )
        if not ok:
            return False, detail_req, payload
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        if not data or not isinstance(data[0], dict) or not data[0].get("b64_json"):
            return False, "ERROR upscale_image_ai: missing_b64_json", {"response": payload}
        try:
            Path(normalized).write_bytes(base64.b64decode(str(data[0].get("b64_json") or "")))
        except Exception as exc:
            return False, f"ERROR upscale_image_ai: save_failed:{exc}", {"path": normalized}
        return True, f"Upscaled AI image: {normalized}", {"path": normalized, "input": detail, "size": str(size or '1536x1024'), "quality": str(quality or 'low')}

    def remove_background_ai(self, input_path: str, output_path: str, quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        ok_image, data_url, detail = self._image_data_url(input_path)
        if not ok_image:
            return False, f"ERROR remove_background_ai: {detail}", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_no_background.png")
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        prompt = (
            "Remove the background completely and keep only the main foreground subject. "
            "Preserve the subject faithfully and return a transparent PNG."
        )
        ok, detail_req, payload = self._openai_image_request(
            "/images/edits",
            {
                "model": "gpt-image-1",
                "prompt": prompt,
                "images": [{"image_url": data_url}],
                "quality": str(quality or "low"),
                "background": "transparent",
                "output_format": "png",
                "input_fidelity": "high",
            },
            timeout=240.0,
        )
        if not ok:
            return False, detail_req, payload
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        if not data or not isinstance(data[0], dict) or not data[0].get("b64_json"):
            return False, "ERROR remove_background_ai: missing_b64_json", {"response": payload}
        try:
            Path(normalized).write_bytes(base64.b64decode(str(data[0].get("b64_json") or "")))
        except Exception as exc:
            return False, f"ERROR remove_background_ai: save_failed:{exc}", {"path": normalized}
        return True, f"Removed background: {normalized}", {"path": normalized, "input": detail, "quality": str(quality or 'low')}

    def edit_image_ai(self, prompt: str, input_paths: Any, output_path: str, mask_path: str = "", size: str = "1024x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR edit_image_ai: empty_prompt", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_edited_ai.png")
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        paths = input_paths if isinstance(input_paths, list) else [input_paths]
        images: list[dict[str, str]] = []
        normalized_inputs: list[str] = []
        for item in paths:
            ok_data, data_url, detail = self._image_data_url(str(item or ""))
            if not ok_data:
                return False, f"ERROR edit_image_ai: {detail}", {}
            images.append({"image_url": data_url})
            normalized_inputs.append(detail)
        request_payload: dict[str, Any] = {
            "model": "gpt-image-1",
            "prompt": value,
            "images": images,
            "size": str(size or "1024x1024"),
            "quality": str(quality or "low"),
        }
        if str(mask_path or "").strip():
            ok_mask, mask_url, detail = self._image_data_url(mask_path)
            if not ok_mask:
                return False, f"ERROR edit_image_ai: {detail}", {}
            request_payload["mask"] = {"image_url": mask_url}
        ok, detail, payload = self._openai_image_request("/images/edits", request_payload, timeout=240.0)
        if not ok:
            return False, detail, payload
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        if not data or not isinstance(data[0], dict) or not data[0].get("b64_json"):
            return False, "ERROR edit_image_ai: missing_b64_json", {"response": payload}
        try:
            image_bytes = base64.b64decode(str(data[0].get("b64_json") or ""))
            Path(normalized).write_bytes(image_bytes)
        except Exception as exc:
            return False, f"ERROR edit_image_ai: save_failed:{exc}", {"path": normalized}
        return True, f"Edited AI image: {normalized}", {"path": normalized, "inputs": normalized_inputs, "size": str(size or '1024x1024'), "quality": str(quality or 'low')}

    def extract_video_frames(self, path: str, output_dir: str, fps: float = 1.0, max_frames: int = 0) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("ffmpeg", path=self.env.get("PATH")) is None:
            return False, "ERROR extract_video_frames: ffmpeg_not_installed", {}
        source = self._normalize_path(path)
        if not os.path.isfile(source):
            return False, f"ERROR extract_video_frames: missing_video:{source}", {}
        target_dir = self._normalize_path(output_dir or "/tmp/aria_video_frames")
        os.makedirs(target_dir, exist_ok=True)
        pattern = str(Path(target_dir) / "frame_%05d.png")
        argv = ["ffmpeg", "-y", "-i", source, "-vf", f"fps={max(0.1, min(float(fps or 1.0), 60.0))}"]
        if int(max_frames or 0) > 0:
            argv.extend(["-frames:v", str(int(max_frames))])
        argv.append(pattern)
        try:
            result = self._run(argv, timeout=240.0)
        except Exception as exc:
            return False, f"ERROR extract_video_frames: {exc}", {"path": source, "output_dir": target_dir}
        frames = sorted(glob.glob(str(Path(target_dir) / "frame_*.png")))
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text(output, 120000)
        ok = result.returncode == 0 and bool(frames)
        return ok, f"Extracted {len(frames)} frame(s)", {"path": source, "output_dir": target_dir, "frames": frames, "output": preview, "truncated": truncated}

    def storyboard_to_video(
        self,
        storyboard: Any,
        output_path: str,
        size: str = "1024x1024",
        quality: str = "low",
        fps: float = 12.0,
        seconds_per_scene: float = 2.0,
    ) -> tuple[bool, str, dict[str, Any]]:
        scenes = self._parse_storyboard_scenes(storyboard)
        if not scenes:
            return False, "ERROR storyboard_to_video: empty_storyboard", {}
        normalized = self._normalize_path(output_path or "/tmp/aria_storyboard.mp4")
        Path(normalized).parent.mkdir(parents=True, exist_ok=True)
        frame_rate = max(1.0, min(float(fps or 12.0), 30.0))
        hold_seconds = max(0.25, min(float(seconds_per_scene or 2.0), 10.0))
        repeat_count = max(1, int(round(frame_rate * hold_seconds)))
        with tempfile.TemporaryDirectory(prefix="aria_storyboard_") as tempdir:
            repeated_inputs: list[str] = []
            for index, scene in enumerate(scenes, start=1):
                frame_path = str(Path(tempdir) / f"scene_{index:03d}.png")
                ok_img, detail, _payload = self.generate_image_ai(scene, frame_path, size, quality)
                if not ok_img:
                    return False, f"ERROR storyboard_to_video scene_{index}: {detail}", {"scene": scene}
                repeated_inputs.extend([frame_path] * repeat_count)
            ok_vid, detail, payload = self.compose_video_from_images(repeated_inputs, normalized, frame_rate)
            if not ok_vid:
                return False, f"ERROR storyboard_to_video: {detail}", payload
        result_payload = dict(payload or {})
        result_payload.update(
            {
                "path": normalized,
                "scene_count": len(scenes),
                "fps": frame_rate,
                "seconds_per_scene": hold_seconds,
            }
        )
        return True, f"Storyboard video created from {len(scenes)} scene(s)", result_payload

    def generate_image(self, prompt: str, size: Any, output_path: str) -> tuple[bool, str, dict[str, Any]]:
        if Image is None or ImageDraw is None:
            return False, "ERROR generate_image: pillow_not_available", {"path": output_path}
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR generate_image: empty_prompt", {"path": output_path}
        normalized = self._normalize_path(output_path or "/tmp/aria_generated_image.png")
        width, height = self._parse_image_size(size)
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            seed = zlib.adler32(value.encode("utf-8")) & 0xFFFFFFFF
            base = ((seed >> 16) & 255, (seed >> 8) & 255, seed & 255)
            accent = ((seed >> 8) & 255, seed & 255, (seed >> 24) & 255)
            img = Image.new("RGB", (width, height), base)
            draw = ImageDraw.Draw(img)
            for row in range(height):
                blend = row / max(1, height - 1)
                color = (
                    int(base[0] * (1 - blend) + accent[0] * blend),
                    int(base[1] * (1 - blend) + accent[1] * blend),
                    int(base[2] * (1 - blend) + accent[2] * blend),
                )
                draw.line((0, row, width, row), fill=color)
            for index in range(8):
                radius = max(24, ((seed >> (index % 16)) & 127) + 24)
                x = ((seed >> (index * 3 % 24)) % max(1, width))
                y = ((seed >> (index * 5 % 24)) % max(1, height))
                outline = tuple(min(255, channel + 40) for channel in accent)
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=outline, width=3)
            wrapped: list[str] = []
            words = value.split()
            current = ""
            for word in words:
                tentative = f"{current} {word}".strip()
                if len(tentative) > 28 and current:
                    wrapped.append(current)
                    current = word
                else:
                    current = tentative
            if current:
                wrapped.append(current)
            y_cursor = 40
            for line in wrapped[:8]:
                draw.text((40, y_cursor), line, fill="white")
                y_cursor += 36
            img.save(normalized)
        except Exception as exc:
            return False, f"ERROR generate_image {normalized}: {exc}", {"path": normalized}
        return True, f"Generated image: {normalized}", {"path": normalized, "width": width, "height": height}

    def ocr_region(self, x: int, y: int, w: int, h: int) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("tesseract", path=self.env.get("PATH")) is None:
            return False, "ERROR ocr_region: tesseract_not_installed", {"region": [x, y, w, h]}
        ok, crop_path, detail = self._capture_region_image(x, y, w, h)
        if not ok:
            return False, f"ERROR ocr_region: {detail}", {"region": [x, y, w, h]}
        try:
            result = self._run(["tesseract", crop_path, "stdout", "--psm", "6"], timeout=20.0)
        except Exception as exc:
            try:
                os.unlink(crop_path)
            except Exception:
                pass
            return False, f"ERROR ocr_region: {exc}", {"region": [x, y, w, h]}
        try:
            os.unlink(crop_path)
        except Exception:
            pass
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR ocr_region: {detail}", {"region": [x, y, w, h]}
        text = (result.stdout or "").strip()
        preview, truncated = self._truncate_text(text, 50000)
        return True, "OCR region captured", {"region": [x, y, w, h], "text": preview, "truncated": truncated}

    def list_downloads(self) -> tuple[bool, str, dict[str, Any]]:
        downloads = Path(self.home_dir) / "Downloads"
        if not downloads.exists():
            return True, "Downloads directory missing", {"path": str(downloads), "entries": []}
        entries: list[dict[str, Any]] = []
        for child in sorted(downloads.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)[:200]:
            try:
                stat = child.stat()
            except Exception:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size_bytes": int(stat.st_size),
                    "mtime": round(float(stat.st_mtime), 6),
                }
            )
        return True, f"Listed {len(entries)} download item(s)", {"path": str(downloads), "entries": entries}

    def watch_file(self, path: str, seconds: float = 5.0) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        timeout_s = max(0.5, min(float(seconds or 5.0), 30.0))
        initial = self._snapshot_file_state(normalized)
        changed = False
        deadline = time.time() + timeout_s
        final = initial
        while time.time() < deadline:
            time.sleep(0.5)
            final = self._snapshot_file_state(normalized)
            if final != initial:
                changed = True
                break
        return True, f"Watched file for {round(timeout_s, 2)}s", {"path": normalized, "changed": changed, "initial": initial, "final": final, "seconds": timeout_s}

    def watch_dir(self, path: str, seconds: float = 5.0) -> tuple[bool, str, dict[str, Any]]:
        normalized = self._normalize_path(path)
        timeout_s = max(0.5, min(float(seconds or 5.0), 30.0))
        initial = self._snapshot_dir_state(normalized)
        changed = False
        deadline = time.time() + timeout_s
        final = initial
        while time.time() < deadline:
            time.sleep(0.5)
            final = self._snapshot_dir_state(normalized)
            if final != initial:
                changed = True
                break
        return True, f"Watched directory for {round(timeout_s, 2)}s", {"path": normalized, "changed": changed, "initial": initial, "final": final, "seconds": timeout_s}

    def _normalize_keys(self, keys: Any) -> list[str]:
        return self.policy.normalize_keys(keys)


NodeRuntime = RemoteComputerRuntime
