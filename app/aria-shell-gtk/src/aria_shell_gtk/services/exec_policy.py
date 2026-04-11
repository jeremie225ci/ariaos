from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


class RuntimeExecPolicy:
    def __init__(self, *, home_dir: str, client_root: str, app_dir: str, workspace_dir: str, state_dir: str) -> None:
        self.home_dir = home_dir
        self.client_root = client_root
        self.app_dir = app_dir
        self.workspace_dir = workspace_dir
        self.state_dir = state_dir
        self.skills_dir = str(Path(self.client_root) / "app" / "aria_shell_gtk" / "tools")

    def command_cwd(self) -> str:
        for candidate in (self.home_dir, "/tmp", "/"):
            if candidate and os.path.isdir(candidate):
                return candidate
        return "/"

    def normalize_path(self, path: str) -> str:
        value = str(path or "").strip()
        if not value:
            return self.command_cwd()
        if value == "/opt/ariaos/ariaos":
            return self.app_dir
        if value == "/opt/ariaos/ariaos/skills":
            return self.skills_dir
        if value.startswith("/opt/ariaos/ariaos/skills/"):
            return str(Path(self.skills_dir) / value[len("/opt/ariaos/ariaos/skills/"):])
        if value.startswith("/opt/ariaos/ariaos/"):
            return str(Path(self.app_dir) / value[len("/opt/ariaos/ariaos/"):])
        if value == "/opt/ariaos" or value == "/opt/ariaos/app":
            return self.workspace_dir
        if value.startswith("/opt/ariaos/app/"):
            return str(Path(self.workspace_dir) / value[len("/opt/ariaos/app/"):])
        if value.startswith("/opt/ariaos/"):
            return str(Path(self.workspace_dir) / value[len("/opt/ariaos/"):])
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
            value = re.sub(r"(?<!\S)/opt/ariaos/ariaos/skills(?=/|\b)", self.skills_dir, value)
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
            os.path.join(self.home_dir, ".config", "ariaos"),
            os.path.join(self.home_dir, ".ariaos"),
            self.state_dir,
            os.path.join(self.home_dir, "Desktop", "AriaApp"),
            os.path.join(self.home_dir, "ariaos"),
        )
        internal_paths = {
            os.path.join(self.home_dir, ".local", "bin", "aria-shell-launch"),
            os.path.join(self.home_dir, ".local", "bin", "aria-shell-remote-test"),
            os.path.join(self.home_dir, ".local", "bin", "aria-import-session-env"),
            os.path.join(self.home_dir, ".local", "bin", "aria-browser"),
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
