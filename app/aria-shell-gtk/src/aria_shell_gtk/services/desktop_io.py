from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class DesktopIO:
    def __init__(
        self,
        *,
        run_ok: Callable[[list[str], float], tuple[bool, str]],
        extract_point: Callable[[dict[str, Any] | None], tuple[bool, int, int]],
        normalize_keys: Callable[[Any], list[str]],
    ) -> None:
        self._run_ok = run_ok
        self._extract_point = extract_point
        self._normalize_keys = normalize_keys

    def capture_screenshot(self) -> tuple[bool, str, str, str]:
        with tempfile.NamedTemporaryFile(prefix="aria_remote_screen_", suffix=".png", delete=False) as handle:
            screenshot_path = Path(handle.name)
        try:
            commands = (
                ["scrot", str(screenshot_path), "-o"],
                ["import", "-window", "root", str(screenshot_path)],
                ["gnome-screenshot", "-f", str(screenshot_path)],
            )
            for cmd in commands:
                ok, _detail = self._run_ok(cmd, timeout=8.0)
                if ok and screenshot_path.exists() and screenshot_path.stat().st_size > 0:
                    image_base64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
                    return True, image_base64, "image/png", "screenshot_captured"
            return False, "", "image/png", "screenshot_capture_failed"
        finally:
            try:
                screenshot_path.unlink(missing_ok=True)
            except Exception:
                pass

    def execute_action(self, action: dict[str, Any]) -> tuple[bool, str]:
        action_type = str((action or {}).get("type") or "").strip().lower()
        if not action_type:
            return False, "computer_action_missing_type"
        try:
            if action_type == "click":
                has_point, x, y = self._extract_point(action)
                if not has_point:
                    return False, "click_failed:missing_coordinates"
                button = str(action.get("button") or "left").strip().lower()
                btn = "3" if button == "right" else "1"
                ok, detail = self._run_ok(["xdotool", "mousemove", str(x), str(y), "click", "--clearmodifiers", btn], timeout=5.0)
                return ok, f"click({x},{y},{button}) {detail}"

            if action_type == "double_click":
                has_point, x, y = self._extract_point(action)
                if not has_point:
                    return False, "double_click_failed:missing_coordinates"
                ok, detail = self._run_ok(
                    ["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2", "--delay", "90", "--clearmodifiers", "1"],
                    timeout=6.0,
                )
                return ok, f"double_click({x},{y}) {detail}"

            if action_type == "scroll":
                raw_value = action.get("scroll_y", action.get("delta_y", action.get("y", 0)))
                scroll_y = int(raw_value or 0)
                direction = "down" if scroll_y >= 0 else "up"
                button = "5" if direction == "down" else "4"
                amount = max(1, min(12, max(1, abs(scroll_y) // 240 or 3)))
                for _ in range(amount):
                    ok, detail = self._run_ok(["xdotool", "click", "--clearmodifiers", button], timeout=4.0)
                    if not ok:
                        return False, f"scroll({direction},{amount}) {detail}"
                    time.sleep(0.02)
                return True, f"scroll({direction},{amount}) ok"

            if action_type == "type":
                text = str(action.get("text") or "")
                ok, detail = self._run_ok(["xdotool", "type", "--delay", "24", text], timeout=max(8.0, min(40.0, len(text) / 3.0 + 4.0)))
                return ok, f"type({len(text)} chars) {detail}"

            if action_type in {"keypress", "key_press"}:
                keys = action.get("keys")
                if not keys:
                    keys = [action.get("key", "")]
                combo = "+".join(self._normalize_keys(keys))
                ok, detail = self._run_ok(["xdotool", "key", "--clearmodifiers", combo], timeout=6.0)
                return ok, f"keypress({combo}) {detail}"

            if action_type == "wait":
                seconds = max(0.05, min(10.0, float(action.get("seconds", 1) or 1)))
                time.sleep(seconds)
                return True, f"wait({seconds:.2f}s)"

            if action_type == "move":
                has_point, x, y = self._extract_point(action)
                if not has_point:
                    return False, "move_failed:missing_coordinates"
                ok, detail = self._run_ok(["xdotool", "mousemove", str(x), str(y)], timeout=4.0)
                return ok, f"move({x},{y}) {detail}"

            if action_type == "drag":
                path = action.get("path") or []
                if not isinstance(path, list) or not path:
                    return False, "drag_failed:empty_path"
                last_point = path[-1]
                has_point, x, y = self._extract_point(last_point if isinstance(last_point, dict) else {})
                if not has_point:
                    return False, "drag_failed:missing_coordinates"
                ok, detail = self._run_ok(
                    ["xdotool", "mousedown", "1", "mousemove", "--sync", str(x), str(y), "mouseup", "1"],
                    timeout=8.0,
                )
                return ok, f"drag({x},{y}) {detail}"

            if action_type == "screenshot":
                return True, "screenshot_requested"

            return False, f"unsupported_computer_action:{action_type}"
        except Exception as exc:
            return False, f"{action_type}_failed:{exc}"
