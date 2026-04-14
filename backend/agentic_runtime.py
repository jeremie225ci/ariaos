"""
AriaOS v3 — Agentic Loop with GPT-5.4 Vision + Computer Use
============================================================
THE BRAIN. This is the core loop that:
1. Loads conversation memory from disk
2. Sends the user's goal + context to GPT-5.4
3. Executes actions in a loop (Thought → Action → Observation)
4. Uses screenshot vision + native computer use for UI tasks
5. Saves the conversation to memory after completion
"""

import json
import os
import sys
import subprocess
import time
import re
import shlex
import ast
import base64
import csv
import difflib
import filecmp
import glob
import hashlib
import io
import mimetypes
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import traceback
import urllib.request
import urllib.error
import uuid
import zipfile
import zlib
from typing import Dict, Any, List, Optional, Generator, Union
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from PIL import Image, ImageOps, ImageDraw
except ImportError:
    Image = None
    ImageOps = None
    ImageDraw = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
except ImportError:
    ImageSequenceClip = None


class UserStopRequested(RuntimeError):
    """Raised when the active stop_event interrupts an in-flight model call."""


class TaskBudgetExceeded(RuntimeError):
    """Raised when cumulative model spend reaches the configured per-task budget."""

# Import brain config
try:
    from backend.brain_config import (
        OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_TEMPERATURE,
        SYSTEM_PROMPT, DIRECT_RESPONSE_SYSTEM_PROMPT, MAX_AGENTIC_STEPS, COMMAND_TIMEOUT,
        STEP_DELAY, CHAT_HISTORY_FILE, MEMORY_DIR,
        MAX_HISTORY_MESSAGES, CONTEXT_WINDOW_SIZE,
        PERCEPTION_LAYERS, SCREEN_RESOLUTION,
        PERCEPTION_MAX_ELEMENTS, PERCEPTION_MAX_CHARS,
        ACTION_RETRY_LIMIT, ACTION_SETTLE_DELAY, TYPE_TEXT_DELAY,
        TRACE_ENABLED, TRACE_FILE,
        VISUAL_PARSER_URL, VISUAL_PARSER_TIMEOUT, VISUAL_PARSER_RETRY_AFTER,
        PREFER_KERNEL_INPUT,
        SNIPER_API_KEY, SNIPER_BASE_URL, SNIPER_MODEL,
        SLIDING_WINDOW_RAW_MESSAGES, USER_PROFILE_FILE, TASK_SUMMARIES_FILE,
        SESSION_LOG_DIR
    )
except ImportError:
    from config import OPENAI_API_KEY, OPENAI_MODEL
    OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_TEMPERATURE = 0.1
    SYSTEM_PROMPT = "You are AriaOS."
    DIRECT_RESPONSE_SYSTEM_PROMPT = (
        "You are AriaOS in direct-answer mode. "
        "If the goal needs only a textual answer, answer directly in plain text. "
        "If the goal requires operating the computer, answer exactly NEEDS_AGENTIC_ACTION."
    )
    MAX_AGENTIC_STEPS = 30
    COMMAND_TIMEOUT = 15
    STEP_DELAY = 0.3
    CHAT_HISTORY_FILE = os.path.expanduser("~/.ariaos/memory/chat_history.json")
    MEMORY_DIR = os.path.expanduser("~/.ariaos/memory")
    MAX_HISTORY_MESSAGES = 50
    CONTEXT_WINDOW_SIZE = 10
    PERCEPTION_LAYERS = ["vision"]
    SCREEN_RESOLUTION = (1280, 800)
    PERCEPTION_MAX_ELEMENTS = 200
    PERCEPTION_MAX_CHARS = 12000
    ACTION_RETRY_LIMIT = 2
    ACTION_SETTLE_DELAY = 0.25
    TYPE_TEXT_DELAY = 0.03
    TRACE_ENABLED = True
    TRACE_FILE = "/tmp/ariaos_agent_trace.jsonl"
    VISUAL_PARSER_URL = "http://192.168.0.242:8000/parse"
    VISUAL_PARSER_TIMEOUT = 12
    VISUAL_PARSER_RETRY_AFTER = 30
    PREFER_KERNEL_INPUT = False
    SNIPER_API_KEY = os.environ.get("SNIPER_API_KEY", "")
    SNIPER_BASE_URL = os.environ.get("SNIPER_BASE_URL", "https://openrouter.ai/api/v1")
    SNIPER_MODEL = os.environ.get("SNIPER_MODEL", "bytedance/ui-tars-1.5-7b")
    SLIDING_WINDOW_RAW_MESSAGES = 8
    USER_PROFILE_FILE = os.path.expanduser("~/.ariaos/memory/user_profile.json")
    TASK_SUMMARIES_FILE = os.path.expanduser("~/.ariaos/memory/task_summaries.json")
    SESSION_LOG_DIR = os.path.expanduser("~/.ariaos/memory/sessions")

# Bound what is sent back to the planner each step to reduce cost/drift.
MODEL_CONTEXT_MAX_MESSAGES = int(os.environ.get("ARIA_MODEL_CONTEXT_MAX_MESSAGES", "50"))
MODEL_OBSERVATION_MAX_CHARS = int(os.environ.get("ARIA_MODEL_OBS_MAX_CHARS", "1400"))
MODEL_OBSERVATION_MAX_ELEMENTS = int(os.environ.get("ARIA_MODEL_OBS_MAX_ELEMENTS", "200"))
FRAME_CACHE_LIMIT = int(os.environ.get("ARIA_FRAME_CACHE_LIMIT", "16"))
ALLOW_AUTONOMOUS_HELPERS = (
    os.environ.get("ARIA_ALLOW_AUTONOMOUS_HELPERS", "false").lower()
    in ("1", "true", "yes", "on")
)
DEFAULT_OPENAI_MODEL = "gpt-5.4"
SUPPORTED_OPENAI_MODELS = {
    "gpt-5.4",
    "gpt-5.4-mini",
}
LEGACY_UI_ACTIONS = {
    "ask_vision",
    "chain_type",
    "click",
    "double_click",
    "key_combo",
    "precision_click",
    "read_screen",
    "right_click",
    "scroll",
    "type_text",
}
# These action names still exist in older prompts and traces, but in the
# public local-first runtime they must never drive UI work directly. Visible
# interaction goes through the planner's "computer" action instead.
CONFIDENCE_FALLBACK = 0.96
PARSER_MIN_CONFIDENCE = 0.10
SNIPER_AUTO_TRIGGER = 0.85
USER_IMAGE_MIN_SIDE = int(os.environ.get("ARIA_USER_IMAGE_MIN_SIDE", "256"))
USER_IMAGE_MAX_SIDE = int(os.environ.get("ARIA_USER_IMAGE_MAX_SIDE", "1536"))
USER_IMAGE_DETAIL = os.environ.get("ARIA_USER_IMAGE_DETAIL", "high").strip().lower() or "high"
SESSION_HISTORY_MAX_TASKS = int(os.environ.get("ARIA_SESSION_HISTORY_MAX_TASKS", "10"))
SESSION_HISTORY_FACTS_PER_TASK = int(os.environ.get("ARIA_SESSION_HISTORY_FACTS_PER_TASK", "4"))
CURRENT_TASK_CHECKPOINT_FACTS = int(os.environ.get("ARIA_CURRENT_TASK_CHECKPOINT_FACTS", "8"))
CURRENT_TASK_RECENT_STEPS = int(os.environ.get("ARIA_CURRENT_TASK_RECENT_STEPS", "12"))
STRICT_CHAT_ISOLATION = (
    os.environ.get("ARIA_STRICT_CHAT_ISOLATION", "true").lower()
    in ("1", "true", "yes", "on")
)
MODEL_PRICING_USD_PER_MILLION = {
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
    "gpt-5.2": {
        "input": 1.75,
        "cached_input": 0.175,
        "output": 14.00,
    },
}
# SYSTEM_PROMPT is imported from brain_config.py — single source of truth


# =================================================================
# MEMORY SYSTEM — Persistent chat history
# =================================================================

class Memory:
    """Persistent JSON memory for the AI brain."""

    def __init__(self, history_file: str = CHAT_HISTORY_FILE, max_items: int = MAX_HISTORY_MESSAGES):
        self.history_file = history_file
        self.max_items = max_items
        self._load()

    def _load(self):
        """Load conversation history from disk."""
        if STRICT_CHAT_ISOLATION:
            self.history = []
            print("[Memory] Strict chat isolation enabled; legacy global memory disabled")
            return
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, "r") as f:
                    self.history = json.load(f)
                    # Keep only last N items
                    self.history = self.history[-self.max_items:]
                print(f"[Memory] Loaded {len(self.history)} exchanges from disk")
            else:
                print("[Memory] No history file found, starting fresh")
                self.history = []
        except Exception as e:
            print(f"[Memory] Error loading history: {e}")
            self.history = []

    def save(self):
        """Save conversation history to disk."""
        if STRICT_CHAT_ISOLATION:
            return
        try:
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            with open(self.history_file, "w") as f:
                json.dump(self.history[-self.max_items:], f, indent=2, ensure_ascii=False)
            print(f"[Memory] Saved {len(self.history)} exchanges to disk")
        except Exception as e:
            print(f"[Memory] Error saving history: {e}")

    def add_exchange(self, user_goal: str, result_summary: str, steps: int, success: bool):
        """Record a completed task exchange."""
        if STRICT_CHAT_ISOLATION:
            return
        self.history.append({
            "timestamp": datetime.now().isoformat(),
            "user": user_goal,
            "assistant": result_summary,
            "steps": steps,
            "success": success
        })
        self.save()

    def get_context(self, n: int = CONTEXT_WINDOW_SIZE) -> str:
        """Get the last N exchanges formatted for the system prompt."""
        if STRICT_CHAT_ISOLATION:
            return "Global memory disabled."
        if not self.history:
            return "No previous conversations."

        recent = self.history[-n:]
        lines = ["Previous conversations (most recent last):"]
        for ex in recent:
            ts = ex.get("timestamp", "?")[:16]
            user_msg = ex.get("user", "?")[:100]
            ai_msg = ex.get("assistant", "?")[:200]
            success = "✅" if ex.get("success") else "❌"
            lines.append(f"  [{ts}] User: {user_msg}")
            lines.append(f"  [{ts}] AI: {success} {ai_msg}")
        return "\n".join(lines)

    def search(self, query: str) -> List[Dict]:
        """Search history for relevant past exchanges."""
        if STRICT_CHAT_ISOLATION:
            return []
        query_lower = query.lower()
        return [
            ex for ex in self.history
            if query_lower in ex.get("user", "").lower()
            or query_lower in ex.get("assistant", "").lower()
        ]


# =================================================================
# PERCEPTION ROUTER — Screenshot-only placeholder
# =================================================================

class PerceptionRouter:
    """Lightweight screenshot capture placeholder.

    Screen understanding is handled in AgenticLoop via GPT-5.4 vision.
    """

    def __init__(self, display: str = ":0"):
        self.display = display
        self.env = {**os.environ, "DISPLAY": display}
        self.layers: Dict[str, Any] = {"vision": "gpt-5.4_screenshot"}
        self.current_elements: Dict[str, Dict[str, Any]] = {}
        self.last_snapshot: Dict[str, Any] = {}
        self.max_elements = PERCEPTION_MAX_ELEMENTS
        self.max_snapshot_chars = PERCEPTION_MAX_CHARS
        self.viewport_width, self.viewport_height = SCREEN_RESOLUTION

        self._init_layers()

    def _init_layers(self):
        print("[Perception] ✅ Screenshot capture layer loaded")
        print("[Perception] ✅ Screen understanding delegated to GPT-5.4 vision")

    def _vision_snapshot(self) -> str:
        screenshot_path = "/tmp/ariaos_screenshot.png"
        self.current_elements = {}

        if not self._capture_screenshot(screenshot_path):
            return "ERROR: Vision mode failed to capture screenshot."
        return self._build_snapshot_text(
            layer_name="vision",
            raw_text="Screenshot captured. Use GPT-5.4 vision or computer actions for real UI understanding.",
            raw_elements=[],
            metadata={"vision_source": "gpt-5.4_screenshot"},
        )

    def _capture_screenshot(self, screenshot_path: str) -> bool:
        for cmd in (["scrot", screenshot_path, "-o"], ["import", "-window", "root", screenshot_path], ["gnome-screenshot", "-f", screenshot_path]):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                    env=self.env,
                )
                if result.returncode == 0 and os.path.exists(screenshot_path):
                    return True
            except Exception:
                continue
        return False

    def _extract_elements_remote(self, screenshot_path: str) -> tuple:
        metadata: Dict[str, str] = {}
        if time.time() < self.remote_backoff_until:
            metadata["vision_source"] = "omniparser_api:backoff"
            return {}, metadata

        try:
            with open(screenshot_path, "rb") as img_file:
                image_b64 = base64.b64encode(img_file.read()).decode("ascii")
        except Exception as e:
            metadata["vision_source"] = "omniparser_api:image_read_error"
            metadata["parser_error"] = str(e)[:120]
            return {}, metadata

        last_error = ""
        for endpoint in self.visual_parser_urls:
            started = time.time()
            try:
                payload = {
                    "image_base64": image_b64,
                    "include_som": True,
                    "include_elements": True,
                }
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.visual_parser_timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))

                normalized = self._normalize_remote_payload(body)
                latency_ms = int((time.time() - started) * 1000)
                metadata = {
                    "vision_source": "omniparser_api",
                    "parser_latency_ms": str(latency_ms),
                    "parser_endpoint": endpoint,
                }
                frame_id = str(body.get("frame_id", "")).strip() if isinstance(body, dict) else ""
                if frame_id:
                    metadata["frame_id"] = frame_id[:64]
                if normalized:
                    return normalized, metadata
                last_error = "empty_elements"
            except Exception as e:
                last_error = str(e)
                continue

        self.remote_backoff_until = time.time() + self.visual_parser_retry_after
        metadata = {
            "vision_source": "omniparser_api:error",
            "parser_error": last_error[:120],
        }
        return {}, metadata

    def _normalize_remote_payload(self, payload: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}

        # Preferred schema: {"elements": [{id, label, x, y, ...}, ...]}
        elements = payload.get("elements")
        if isinstance(elements, list):
            return self._normalize_elements(elements)
        if isinstance(elements, dict):
            return self._normalize_elements_map(elements)

        # Alternate schema: direct id->coords map
        if all(isinstance(v, dict) for v in payload.values()):
            return self._normalize_elements_map(payload)

        return {}

    def _extract_elements_local(self, screenshot_path: str) -> Dict[str, Dict[str, Any]]:
        elements_path = "/tmp/ariaos_screenshot_elements.json"
        parser_candidates = [
            "/home/jeremie/ariaos/aria/ui_parser.py",
            "/home/jeremie/Ariaos/vm_patches/ui_parser.py",
        ]
        parser_path = next((p for p in parser_candidates if os.path.exists(p)), None)
        if not parser_path:
            return {}

        try:
            subprocess.run(
                ["python3", parser_path, screenshot_path],
                capture_output=True,
                timeout=15,
                env=self.env,
            )
            if not os.path.exists(elements_path):
                return {}

            with open(elements_path, "r") as f:
                raw_map = json.load(f)
            return self._normalize_elements_map(raw_map)
        except Exception as e:
            print(f"[Perception] Local visual fallback failed: {e}")
            return {}

    def _normalize_elements(self, elements: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for idx, item in enumerate(elements):
            try:
                x = int(item.get("x"))
                y = int(item.get("y"))
            except (TypeError, ValueError):
                continue
            if not self._is_point_on_screen(x, y):
                continue

            elem_id = str(item.get("id", idx)).replace("[", "").replace("]", "").strip() or str(idx)
            label = str(item.get("label") or item.get("text") or "").strip()[:140]
            role = str(item.get("role") or item.get("type") or "ui_target").strip().lower() or "ui_target"
            confidence = item.get("confidence", PARSER_MIN_CONFIDENCE)
            try:
                conf = float(confidence)
            except (TypeError, ValueError):
                conf = PARSER_MIN_CONFIDENCE
            w = item.get("w", item.get("width", 18))
            h = item.get("h", item.get("height", 18))
            try:
                width = max(8, int(w))
                height = max(8, int(h))
            except (TypeError, ValueError):
                width = 18
                height = 18

            normalized[elem_id] = {
                "x": x,
                "y": y,
                "text": label,
                "role": role,
                "confidence": max(0.0, min(1.0, conf)),
                "w": width,
                "h": height,
                "focused": bool(item.get("focused", False)),
            }
        return normalized

    def _normalize_elements_map(self, raw_map: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for idx, (elem_id, data) in enumerate(raw_map.items()):
            if not isinstance(data, dict):
                continue
            try:
                x = int(data.get("x"))
                y = int(data.get("y"))
            except (TypeError, ValueError):
                continue
            if not self._is_point_on_screen(x, y):
                continue
            clean_id = str(elem_id).replace("[", "").replace("]", "").strip() or str(idx)
            label = str(data.get("text") or data.get("label") or "").strip()[:140]
            role = str(data.get("role") or data.get("type") or "ui_target").strip().lower() or "ui_target"
            confidence = data.get("confidence", PARSER_MIN_CONFIDENCE)
            try:
                conf = float(confidence)
            except (TypeError, ValueError):
                conf = PARSER_MIN_CONFIDENCE
            w = data.get("w", data.get("width", 18))
            h = data.get("h", data.get("height", 18))
            try:
                width = max(8, int(w))
                height = max(8, int(h))
            except (TypeError, ValueError):
                width = 18
                height = 18
            normalized[clean_id] = {
                "x": x,
                "y": y,
                "text": label,
                "role": role,
                "confidence": max(0.0, min(1.0, conf)),
                "w": width,
                "h": height,
                "focused": bool(data.get("focused", False)),
            }
        return normalized

    def _is_point_on_screen(self, x: int, y: int) -> bool:
        return 0 <= x < self.viewport_width and 0 <= y < self.viewport_height

    def _is_rect_on_screen(self, x: int, y: int, w: int, h: int) -> bool:
        if w <= 0 or h <= 0:
            return False
        if x >= self.viewport_width or y >= self.viewport_height:
            return False
        if x + w <= 0 or y + h <= 0:
            return False
        return True

    def _looks_like_noise_label(self, label: str) -> bool:
        text = re.sub(r"\s+", " ", (label or "").strip().lower())
        if not text:
            return False

        keep_terms = (
            "gmail", "mail", "email", "discord", "chat", "dm",
            "compose", "send", "envoy", "enviar", "inbox", "sent",
            "draft", "subject", "message", "to", "recipient", "reply",
            "search", "channel", "server",
        )
        if any(term in text for term in keep_terms):
            return False

        noise_terms = (
            "console", "sources", "breakpoints", "listeners", "computed",
            "styles", "network", "performance", "memory", "application",
            "lighthouse", "recorder", "coverage", "issues", "devtools",
            "<script", "</script", "<div", "</div", "style=", "class=",
            "display:", "overflow:", "unicode-bidi", "webkit", "html.",
            "body.", "div.", "jsaction", "jscontroller",
        )
        if any(term in text for term in noise_terms):
            return True

        if text.startswith("<") or text.startswith("</"):
            return True

        alnum = sum(ch.isalnum() for ch in text)
        punct = sum(1 for ch in text if (not ch.isalnum() and not ch.isspace()))
        if alnum <= 2 and punct >= 1:
            return True
        if ":" in text and " " not in text and len(text) < 32:
            return True
        if "=" in text and len(text) < 42:
            return True
        return False

    def _filter_elements(self, raw_elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        interactive_roles = {
            "push", "button", "toggle", "check", "radio", "menu", "link",
            "text", "entry", "textbox", "input", "textarea", "tab", "list",
            "combo", "combobox", "document", "cell", "row", "tree", "ui_target",
        }
        editable_roles = {"entry", "textbox", "input", "textarea", "combo", "combobox", "text"}
        seen = set()
        filtered: List[Dict[str, Any]] = []

        for elem in raw_elements:
            try:
                x = int(elem.get("x", 0))
                y = int(elem.get("y", 0))
                w = max(1, int(elem.get("w", 1)))
                h = max(1, int(elem.get("h", 1)))
            except (TypeError, ValueError):
                continue

            if not self._is_rect_on_screen(x, y, w, h):
                continue

            role = str(elem.get("role", "ui_target")).strip().lower() or "ui_target"
            label = re.sub(r"\s+", " ", str(elem.get("label", "")).strip())[:140]
            focused = bool(elem.get("focused"))
            confidence = float(elem.get("confidence", PARSER_MIN_CONFIDENCE))
            if confidence < PARSER_MIN_CONFIDENCE:
                continue

            # Drop obvious OCR noise from DevTools/CSS/DOM dumps.
            if label and self._looks_like_noise_label(label):
                continue
            # Top panel glyphs/icons create heavy noise for planners.
            if y < 60 and len(label) <= 3 and role == "ui_target":
                continue

            if not label and role in {"panel", "filler", "section", "window", "frame"}:
                continue
            if (
                not label
                and not focused
                and role in {"button", "push", "link", "menu", "tab"}
                and w <= 44
                and h <= 44
            ):
                continue

            dedupe_key = (
                role,
                label.lower(),
                int(round(x / 8.0)),
                int(round(y / 8.0)),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            center_x = x + (w // 2)
            center_y = y + (h // 2)
            if not self._is_point_on_screen(center_x, center_y):
                continue

            priority = 0.0
            if focused:
                priority += 3.0
            if any(token in role for token in interactive_roles):
                priority += 2.0
            if role in editable_roles:
                priority += 1.5
            if label:
                priority += min(len(label), 30) / 30.0
                low = label.lower()
                if any(k in low for k in ("send", "envoy", "compose", "subject", "to", "recipient", "message")):
                    priority += 1.3
            priority += max(0.0, min(confidence, 1.0))

            filtered.append({
                "role": role,
                "label": label,
                "x": center_x,
                "y": center_y,
                "w": w,
                "h": h,
                "focused": focused,
                "confidence": confidence,
                "priority": priority,
            })

        filtered.sort(key=lambda e: (-e["priority"], e["y"], e["x"]))
        return filtered[: self.max_elements]

    def _build_snapshot_text(
        self,
        layer_name: str,
        raw_text: str,
        raw_elements: List[Dict[str, Any]],
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        metadata = metadata or {}
        visible_elements = self._filter_elements(raw_elements)

        self.current_elements = {}
        element_lines: List[str] = []
        for idx, elem in enumerate(visible_elements):
            elem_id = str(idx)
            label = elem["label"] if elem["label"] else "(no label)"
            marker = " *" if elem.get("focused") else ""
            element_lines.append(
                f"[{elem_id}] {elem['role']} | {label} | ({elem['x']},{elem['y']}){marker}"
            )
            self.current_elements[elem_id] = {
                "x": int(elem["x"]),
                "y": int(elem["y"]),
                "text": elem["label"],
                "role": elem["role"],
                "focused": bool(elem.get("focused")),
                "confidence": float(elem.get("confidence", CONFIDENCE_FALLBACK)),
            }

        if not visible_elements:
            confidence = 0.18
        elif len(visible_elements) < 3:
            confidence = 0.55
        else:
            confidence = 0.82

        lines = [
            f"PERCEPTION SNAPSHOT [{layer_name}]",
            f"confidence: {confidence:.2f}",
            f"elements_visible: {len(visible_elements)}/{len(raw_elements)}",
            f"viewport: {self.viewport_width}x{self.viewport_height}",
        ]

        if metadata.get("vision_source"):
            lines.append(f"vision_source: {metadata['vision_source']}")
        if metadata.get("frame_id"):
            lines.append(f"frame_id: {metadata['frame_id']}")
        if metadata.get("parser_latency_ms"):
            lines.append(f"parser_latency_ms: {metadata['parser_latency_ms']}")
        if metadata.get("parser_error"):
            lines.append(f"parser_error: {metadata['parser_error']}")

        lines.append("ELEMENTS:")
        if element_lines:
            lines.extend(element_lines)
        else:
            lines.append("(none)")

        if raw_text:
            lines.append("RAW_EXCERPT:")
            lines.append(raw_text[:240])

        snapshot = "\n".join(lines)
        if len(snapshot) > self.max_snapshot_chars:
            snapshot = snapshot[: self.max_snapshot_chars] + "\n[truncated]"

        self.last_snapshot = {
            "layer": layer_name,
            "confidence": confidence,
            "raw_elements": len(raw_elements),
            "visible_elements": len(visible_elements),
            "metadata": metadata,
        }
        return snapshot

# =================================================================
# ACTION EXECUTOR — Unified low-level input with capability probe
# =================================================================

class ActionExecutor:
    """Unified input executor with backend probing and bounded retries."""

    def __init__(
        self,
        display: str = ":0",
        screen_resolution: tuple = SCREEN_RESOLUTION,
        prefer_kernel: bool = PREFER_KERNEL_INPUT,
        retry_limit: int = ACTION_RETRY_LIMIT,
        settle_delay: float = ACTION_SETTLE_DELAY,
        type_delay: float = TYPE_TEXT_DELAY,
    ):
        self.display = display
        self.env = {**os.environ, "DISPLAY": display}
        self.screen_width, self.screen_height = screen_resolution
        self.prefer_kernel = prefer_kernel
        self.retry_limit = max(1, int(retry_limit))
        self.settle_delay = max(0.05, float(settle_delay))
        self.type_delay = max(0.005, float(type_delay))
        self.click_jitter_pixels = int(os.environ.get("ARIA_CLICK_JITTER_PX", "10"))
        self.backend = None
        self.backend_name = "none"
        self.capabilities: Dict[str, bool] = {}
        self._create_input = None

        try:
            from aria.kernel_input import create_input
            self._create_input = create_input
        except Exception as e:
            print(f"[Executor] Input module unavailable: {e}")

        self._select_initial_backend()

    @property
    def available(self) -> bool:
        return self.backend is not None

    def _select_initial_backend(self):
        order = [True, False] if self.prefer_kernel else [False, True]
        for prefer_kernel in order:
            backend = self._build_backend(prefer_kernel=prefer_kernel)
            if not backend:
                continue
            healthy, caps, reason = self._probe_backend(backend)
            name = type(backend).__name__
            if healthy:
                self.backend = backend
                self.backend_name = name
                self.capabilities = caps
                print(f"[Executor] ✅ Selected backend: {name} (caps={caps})")
                return
            print(f"[Executor] ⚠️ Rejecting backend '{name}': {reason}")
            self._safe_close(backend)

        # Final fallback: try xdotool backend without strict probe.
        fallback = self._build_backend(prefer_kernel=False)
        if fallback:
            self.backend = fallback
            self.backend_name = type(fallback).__name__
            self.capabilities = self._capability_map(fallback, optimistic=True)
            print(f"[Executor] ⚠️ Using fallback backend: {self.backend_name}")
        else:
            print("[Executor] ❌ No usable input backend found")

    def _build_backend(self, prefer_kernel: bool):
        if not self._create_input:
            return None
        try:
            if prefer_kernel:
                backend = self._create_input(
                    prefer_kernel=True,
                    screen_width=self.screen_width,
                    screen_height=self.screen_height,
                )
                if type(backend).__name__ != "KernelInput":
                    # create_input can silently fallback to xdotool; skip duplicate here.
                    if type(backend).__name__ == "XdotoolInput":
                        self._safe_close(backend)
                    return None
                return backend
            return self._create_input(prefer_kernel=False, display=self.display)
        except Exception as e:
            print(f"[Executor] Backend build failed (prefer_kernel={prefer_kernel}): {e}")
            return None

    def _capability_map(self, backend, optimistic: bool = False) -> Dict[str, bool]:
        caps = {
            "move_to": hasattr(backend, "move_to"),
            "click": hasattr(backend, "click"),
            "right_click": hasattr(backend, "right_click") or type(backend).__name__ == "XdotoolInput",
            "double_click": hasattr(backend, "double_click"),
            "type_text": hasattr(backend, "type_text"),
            "key_combo": hasattr(backend, "key_combo"),
            "scroll": hasattr(backend, "scroll"),
        }
        if optimistic:
            if caps["click"]:
                caps["click_effective"] = True
            if caps["move_to"]:
                caps["move_effective"] = True
        return caps

    def _probe_backend(self, backend) -> tuple:
        name = type(backend).__name__
        caps = self._capability_map(backend)
        if not caps["click"] or not caps["type_text"] or not caps["key_combo"]:
            return False, caps, "missing required click/type/key capabilities"

        start = self._get_mouse_position()
        if start is None:
            # Cannot verify pointer movement (headless or missing xdotool).
            return True, caps, "mouse probe unavailable"

        if not caps["move_to"]:
            return False, caps, "missing move_to for pointer probe"

        target_x = min(max(start[0] + 12, 4), self.screen_width - 4)
        target_y = min(max(start[1] + 12, 4), self.screen_height - 4)
        if (target_x, target_y) == start:
            target_x = max(4, start[0] - 12)

        try:
            backend.move_to(target_x, target_y)
            time.sleep(0.08)
            moved = self._position_matches(target_x, target_y, tolerance=6)
            backend.move_to(start[0], start[1])
            caps["move_effective"] = moved
            caps["click_effective"] = moved if name == "KernelInput" else True
            if not moved and name == "KernelInput":
                return False, caps, "pointer did not move during kernel probe"
            return True, caps, "ok"
        except Exception as e:
            return False, caps, f"probe exception: {e}"

    def _switch_backend(self, reason: str) -> bool:
        current_name = self.backend_name
        print(f"[Executor] ⚠️ Switching backend (reason: {reason}) from {current_name}")
        self._safe_close(self.backend)
        self.backend = None
        self.backend_name = "none"
        self.capabilities = {}

        for prefer_kernel in (False, True):
            candidate = self._build_backend(prefer_kernel=prefer_kernel)
            if not candidate:
                continue
            name = type(candidate).__name__
            if name == current_name:
                self._safe_close(candidate)
                continue
            healthy, caps, detail = self._probe_backend(candidate)
            if healthy:
                self.backend = candidate
                self.backend_name = name
                self.capabilities = caps
                print(f"[Executor] ✅ Switched to backend: {name} (caps={caps})")
                return True
            print(f"[Executor] ⚠️ Candidate backend '{name}' failed probe: {detail}")
            self._safe_close(candidate)

        # Recover original backend as last attempt.
        recovered = self._build_backend(prefer_kernel=(current_name == "KernelInput"))
        if recovered:
            self.backend = recovered
            self.backend_name = type(recovered).__name__
            self.capabilities = self._capability_map(recovered, optimistic=True)
            print(f"[Executor] ⚠️ Recovered backend: {self.backend_name}")
            return True
        return False

    def _run_with_retry(self, op_name: str, callback, verify=None) -> tuple:
        errors: List[str] = []
        for attempt in range(1, self.retry_limit + 1):
            if not self.backend:
                return False, "No input backend available"
            try:
                callback()
                time.sleep(self.settle_delay)
                if verify and not verify():
                    raise RuntimeError(f"{op_name} verification failed")
                return True, f"{op_name} ok via {self.backend_name}"
            except Exception as e:
                err = f"attempt {attempt}/{self.retry_limit} on {self.backend_name}: {e}"
                errors.append(err)
                print(f"[Executor] {op_name} failed: {err}")
                if attempt < self.retry_limit:
                    switched = self._switch_backend(reason=f"{op_name}_failure")
                    if not switched:
                        time.sleep(self.settle_delay)
        return False, "; ".join(errors)

    def click(self, x: int, y: int) -> tuple:
        x = int(x)
        y = int(y)

        def _callback():
            if self.backend_name == "XdotoolInput":
                self._run_xdotool(["mousemove", str(x), str(y)], timeout=4)
                self._run_xdotool(["click", "--clearmodifiers", "1"], timeout=4)
                return
            self.backend.click(x, y)

        return self._run_with_retry(
            op_name="click",
            callback=_callback,
            verify=lambda: self._position_matches(x, y, tolerance=10),
        )

    def double_click(self, x: int, y: int) -> tuple:
        x = int(x)
        y = int(y)

        def _callback():
            if self.backend_name == "XdotoolInput":
                self._run_xdotool(["mousemove", str(x), str(y)], timeout=4)
                self._run_xdotool(
                    ["click", "--repeat", "2", "--delay", "90", "--clearmodifiers", "1"],
                    timeout=5,
                )
                return
            if hasattr(self.backend, "double_click"):
                self.backend.double_click(x, y)
            else:
                self.backend.click(x, y)
                time.sleep(0.08)
                self.backend.click(x, y)

        return self._run_with_retry(
            op_name="double_click",
            callback=_callback,
            verify=lambda: self._position_matches(x, y, tolerance=10),
        )

    def right_click(self, x: int, y: int) -> tuple:
        x = int(x)
        y = int(y)

        def _callback():
            if self.backend_name == "XdotoolInput":
                self._run_xdotool(["mousemove", str(x), str(y)], timeout=4)
                self._run_xdotool(["click", "--clearmodifiers", "3"], timeout=4)
                return
            if hasattr(self.backend, "right_click"):
                self.backend.right_click(x, y)
                return
            if hasattr(self.backend, "click"):
                try:
                    self.backend.click(x, y, 3)
                    return
                except TypeError:
                    pass
            raise RuntimeError("right_click not supported by active backend")

        return self._run_with_retry(
            op_name="right_click",
            callback=_callback,
            verify=lambda: self._position_matches(x, y, tolerance=10),
        )

    def type_text(self, text: str) -> tuple:
        text = str(text)
        def _callback():
            if self.backend_name == "XdotoolInput":
                delay_ms = max(1, int(self.type_delay * 1000))
                timeout_sec = min(30, max(5, len(text) // 6 + 5))
                self._run_xdotool(["type", "--delay", str(delay_ms), text], timeout=timeout_sec)
                return
            self.backend.type_text(text, delay=self.type_delay)
        return self._run_with_retry(
            op_name="type_text",
            callback=_callback,
        )

    def key_combo(self, keys: List[str]) -> tuple:
        normalized = self._normalize_key_combo(keys)
        def _callback():
            if self.backend_name == "XdotoolInput":
                combo = "+".join(normalized)
                self._run_xdotool(["key", "--clearmodifiers", combo], timeout=6)
                return
            self.backend.key_combo(normalized)
        return self._run_with_retry(
            op_name="key_combo",
            callback=_callback,
        )

    def scroll(self, direction: str = "down", amount: int = 3) -> tuple:
        direction = str(direction).lower()
        amount = max(1, min(20, int(amount)))
        wheel_value = -1 if direction == "down" else 1
        xdotool_btn = "5" if direction == "down" else "4"

        def _callback():
            if hasattr(self.backend, "scroll"):
                if self.backend_name == "KernelInput":
                    for _ in range(amount):
                        self.backend.scroll(wheel_value)
                        time.sleep(0.03)
                    return
                try:
                    self.backend.scroll(direction, amount)
                    return
                except TypeError:
                    pass
            for _ in range(amount):
                self._run_xdotool(["click", "--clearmodifiers", xdotool_btn], timeout=4)
                time.sleep(0.02)

        return self._run_with_retry(op_name="scroll", callback=_callback)

    def _normalize_key_combo(self, keys: List[str]) -> List[str]:
        if not isinstance(keys, list):
            return []
        if self.backend_name != "XdotoolInput":
            return [str(k).strip() for k in keys if str(k).strip()]

        key_map = {
            "enter": "Return",
            "return": "Return",
            "tab": "Tab",
            "escape": "Escape",
            "esc": "Escape",
            "pageup": "Page_Up",
            "pagedown": "Page_Down",
            "super": "Super_L",
            "meta": "Super_L",
            "alt": "Alt_L",
            "shift": "Shift_L",
            "ctrl": "ctrl",
        }
        normalized = []
        for key in keys:
            key_str = str(key).strip()
            if not key_str:
                continue
            normalized.append(key_map.get(key_str.lower(), key_str))
        return normalized

    def _position_matches(self, x: int, y: int, tolerance: int = 8) -> bool:
        pos = self._get_mouse_position()
        if pos is None:
            return True
        return abs(pos[0] - x) <= tolerance and abs(pos[1] - y) <= tolerance

    def _get_mouse_position(self) -> Optional[tuple]:
        try:
            result = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                capture_output=True,
                text=True,
                timeout=3,
                env=self.env,
            )
            if result.returncode != 0:
                return None
            values = {}
            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
            return (int(values["X"]), int(values["Y"]))
        except Exception:
            return None

    def _safe_close(self, backend):
        if not backend:
            return
        try:
            if hasattr(backend, "close"):
                backend.close()
        except Exception:
            pass

    def _run_xdotool(self, args: List[str], timeout: int = 5):
        last_detail = ""
        for attempt in range(2):
            result = subprocess.run(
                ["xdotool", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.env,
            )
            if result.returncode == 0:
                return
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            last_detail = stderr or stdout or f"exit={result.returncode}"
            if attempt == 0:
                time.sleep(0.08)
        raise RuntimeError(f"xdotool {' '.join(args)} failed: {last_detail}")

    def close(self):
        self._safe_close(self.backend)
        self.backend = None
        self.backend_name = "none"
        self.capabilities = {}


# =================================================================
# VERIFIER — Post-condition validation and DONE gating
# =================================================================

class VerificationEngine:
    """Tracks objective evidence and blocks DONE without proof."""

    SEND_CONFIRM_PATTERNS = (
        r"\bmessage\s+sent\b",
        r"\bmensaje\s+enviado\b",
        r"\bmessage\s+envoy",
        r"\bcorreo\s+enviado\b",
        r"\bmail\s+sent\b",
        r"\bsent\b.*\bundo\b",
    )

    def __init__(self, max_events: int = 80):
        self.max_events = max_events
        self.reset()

    def reset(self):
        self.events: List[Dict[str, Any]] = []
        self.last_screen_text = ""
        self.last_screen_fingerprint = ""
        self.last_layer = ""
        self.last_url = ""
        self.command_successes = 0
        self.command_failures = 0
        self.send_confirm_patterns = list(self.SEND_CONFIRM_PATTERNS)

    def observe_screen(self, layer: str, text: str) -> Dict[str, Any]:
        text = text or ""
        new_fp = self._fingerprint_screen(text)
        changed = bool(self.last_screen_fingerprint and new_fp and new_fp != self.last_screen_fingerprint)

        url = self._extract_url(text) or self.last_url
        url_changed = bool(self.last_url and url and url != self.last_url)

        send_confirmation = self.has_send_confirmation_text(text)
        visible_count = self._extract_visible_count(text)

        if visible_count > 0:
            self._record("screen_read", True, f"layer={layer}, visible={visible_count}")
        if send_confirmation:
            self._record("confirmation", True, "explicit_send_confirmation")

        self.last_screen_text = text
        self.last_screen_fingerprint = new_fp
        self.last_layer = layer
        if url:
            self.last_url = url

        return {
            "changed": changed,
            "url_changed": url_changed,
            "send_confirmation": send_confirmation,
            "visible_count": visible_count,
            "layer": layer,
        }

    def record_command(self, output: str):
        ok = not self._is_command_failure(output or "")
        if ok:
            self.command_successes += 1
        else:
            self.command_failures += 1
        self._record("execute", ok, "command_success" if ok else "command_failure")

    def can_mark_done(self, goal_lower: str) -> tuple:
        goal_lower = goal_lower or ""
        channel_tokens = ("mail", "gmail", "email", "discord", "message", "dm")
        delivery_tokens = ("reply", "respond", "envoie", "envoy", "send", "post")
        is_delivery_goal = (
            any(tok in goal_lower for tok in channel_tokens)
            and any(tok in goal_lower for tok in delivery_tokens)
        )

        if is_delivery_goal and not self.has_explicit_confirmation():
            return False, "No explicit delivery confirmation detected on screen."

        verified_events = sum(1 for evt in self.events if evt.get("ok"))
        if verified_events == 0 and self.command_successes == 0:
            return False, "No verified post-condition recorded."

        return True, "verified"

    def has_explicit_confirmation(self) -> bool:
        return any(
            evt.get("type") == "confirmation" and evt.get("ok")
            for evt in self.events
        )

    def has_send_confirmation_text(self, text: str) -> bool:
        low = (text or "").lower()
        return any(re.search(pat, low) for pat in self.send_confirm_patterns)

    def set_send_confirmation_patterns(self, patterns: List[str]):
        cleaned = [str(p).strip() for p in (patterns or []) if str(p).strip()]
        if cleaned:
            self.send_confirm_patterns = cleaned
        else:
            self.send_confirm_patterns = list(self.SEND_CONFIRM_PATTERNS)

    def _extract_url(self, text: str) -> str:
        url_line = re.search(r"^url:\s*(https?://\S+)", text or "", re.IGNORECASE | re.MULTILINE)
        if url_line:
            return url_line.group(1).strip()
        inline = re.search(r"https?://[^\s\]\">]+", text or "")
        return inline.group(0).strip() if inline else ""

    def _extract_visible_count(self, text: str) -> int:
        match = re.search(r"elements_visible:\s*(\d+)/(\d+)", text or "")
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _fingerprint_screen(self, text: str) -> str:
        lines = []
        for line in (text or "").splitlines():
            stripped = line.strip().lower()
            if not stripped:
                continue
            if stripped.startswith("confidence:"):
                continue
            if stripped.startswith("elements_visible:"):
                continue
            lines.append(stripped)
            if len(lines) >= 120:
                break
        normalized = "\n".join(lines)
        return str(hash(normalized))

    def _record(self, event_type: str, ok: bool, details: str):
        self.events.append({
            "type": event_type,
            "ok": bool(ok),
            "details": details,
            "ts": time.time(),
        })
        self.events = self.events[-self.max_events:]

    def _is_command_failure(self, output: str) -> bool:
        return (
            "[exit code:" in output
            or output.startswith("[TIMEOUT")
            or output.startswith("[ERROR")
            or output.startswith("ERROR:")
        )


# =================================================================
# PROFILE MANAGER — External app profiles (JSON)
# =================================================================

class ProfileManager:
    """Loads app behavior profiles so core loop stays app-agnostic."""

    def __init__(self):
        env_dir = os.environ.get("ARIA_PROFILE_DIR", "").strip()
        self.profile_dirs = [p for p in [
            env_dir,
            str(Path(__file__).resolve().parent / "profiles"),
        ] if p]
        self.profiles: List[Dict[str, Any]] = self._load_profiles()

    def _load_profiles(self) -> List[Dict[str, Any]]:
        profiles: List[Dict[str, Any]] = []
        for directory in self.profile_dirs:
            if not os.path.isdir(directory):
                continue
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(directory, filename)
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data.get("id"):
                        profiles.append(data)
                        print(f"[Profiles] ✅ Loaded profile: {data.get('id')} ({path})")
                except Exception as e:
                    print(f"[Profiles] ⚠️ Failed to load {path}: {e}")
        return profiles

    def select_profile(self, goal_lower: str) -> Optional[Dict[str, Any]]:
        goal_lower = goal_lower or ""
        for profile in self.profiles:
            keywords = [str(k).lower() for k in profile.get("goal_keywords", [])]
            if keywords and any(k in goal_lower for k in keywords):
                return profile
        return None

    def intent_match(self, profile: Optional[Dict[str, Any]], intent: str, goal_lower: str) -> bool:
        if not profile:
            return False
        intents = profile.get("intents", {})
        keywords = [str(k).lower() for k in intents.get(intent, [])]
        return bool(keywords and any(k in (goal_lower or "") for k in keywords))

    def get_ui(self, profile: Optional[Dict[str, Any]], key: str, default=None):
        if not profile:
            return default
        ui = profile.get("ui", {})
        return ui.get(key, default)


# =================================================================
# THE AGENTIC LOOP — The Brain
# =================================================================

class AgenticLoop:
    """The AriaOS Brain — ReAct loop with memory and perception routing."""

    def __init__(self, display: str = ":0", max_steps: int = MAX_AGENTIC_STEPS):
        self.max_steps = max_steps
        self.display = display
        self.env = {**os.environ, "DISPLAY": display}

        # Memory
        self.memory = Memory()

        # Perception router
        self.perception = PerceptionRouter(display=display)
        self.verifier = VerificationEngine()
        self.last_screen_signals: Dict[str, Any] = {}
        self.profile_manager = ProfileManager()
        self.active_profile: Optional[Dict[str, Any]] = None
        self.trace_enabled = TRACE_ENABLED
        self.trace_file = TRACE_FILE
        self.allow_autonomous_helpers = ALLOW_AUTONOMOUS_HELPERS
        self.decision_model = DEFAULT_OPENAI_MODEL
        self.vision_model = DEFAULT_OPENAI_MODEL
        self.target_focus_keywords: List[str] = []
        self._active_stop_event: Optional[threading.Event] = None

        # Unified action executor (capability probe + backend fallback)
        self.executor = ActionExecutor(
            display=display,
            screen_resolution=SCREEN_RESOLUTION,
            prefer_kernel=PREFER_KERNEL_INPUT,
            retry_limit=ACTION_RETRY_LIMIT,
            settle_delay=ACTION_SETTLE_DELAY,
            type_delay=TYPE_TEXT_DELAY,
        )
        self.input_backend = self.executor.backend_name
        self.input_device = self.executor.backend  # compatibility alias
        if self.executor.available:
            print(f"[Brain] ✅ Input backend initialized: {self.input_backend}")
        else:
            print("[Brain] ⚠️ No input backend available")

        self.openai_api_key = ""
        self.openai_base_url = OPENAI_BASE_URL
        self.openai_model = OPENAI_MODEL
        self.client = None
        self.remote_computer = None
        self.remote_workspace = None
        self.remote_message_source = None
        self.pause_waiter = None
        self.configure_openai_runtime(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            model=OPENAI_MODEL,
        )

        # Current task state
        self.messages: List[Dict[str, Any]] = []
        self.step_count = 0
        self.vision_calls = 0
        self.current_elements = {}
        self.goal_text = ""
        self.goal_lower = ""
        self.last_action_signature = ""
        self.last_action_streak = 0
        self.command_failures: Dict[str, int] = {}
        self.max_same_action_streak = 3
        self.max_failed_command_retries = 2
        self.current_frame_id = ""
        self.frame_element_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.frame_cache_order: List[str] = []

        # Async progress log — summarized by the current decision model
        self.progress_log: List[str] = []
        self._summary_lock = threading.Lock()
        self._summary_threads: List[threading.Thread] = []

        # Session-level execution memory
        self.session_log: List[str] = []
        self.session_step_offset = 0
        self._last_goal_success: Optional[bool] = None
        self._current_session_id = ""
        self._current_task_id = ""
        self.scheduled_tasks_context: List[Dict[str, Any]] = []
        self._openai_request_counter = 0
        self._pending_user_image: Optional[Dict[str, str]] = None
        self._direct_response_mode = False
        self._usage_lock = threading.Lock()
        self._task_started_at = 0.0
        self._current_task_usage: Dict[str, Any] = {}
        self.last_task_usage_summary: Dict[str, Any] = {}
        self.max_budget_usd = 0.0
        self._budget_exceeded_reason = ""

    def configure_openai_runtime(self, api_key: str, base_url: str | None = None, model: str | None = None) -> None:
        self.openai_api_key = str(api_key or "").strip()
        self.openai_base_url = (
            str(base_url or OPENAI_BASE_URL or "https://api.openai.com/v1").strip()
            or "https://api.openai.com/v1"
        )
        requested_model = str(model or OPENAI_MODEL or DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
        self.openai_model = self._normalize_supported_model(requested_model)
        self.decision_model = self.openai_model
        self.vision_model = self.openai_model

        if OpenAI and self.openai_api_key:
            self.client = OpenAI(api_key=self.openai_api_key, base_url=self.openai_base_url)
            print("[Brain] ✅ OpenAI client initialized")
            if self.openai_model != requested_model:
                print(
                    f"[Brain] ⚠️ Unsupported model '{requested_model}' requested; "
                    f"falling back to {self.openai_model}",
                    flush=True,
                )
        else:
            self.client = None
            print("[Brain] ⚠️ OpenAI client not available")

    def _normalize_supported_model(self, model_name: str) -> str:
        normalized = str(model_name or "").strip().lower()
        if normalized in SUPPORTED_OPENAI_MODELS:
            return normalized
        return DEFAULT_OPENAI_MODEL

    def configure_remote_computer(self, remote_computer: Any | None) -> None:
        self.remote_computer = remote_computer

    def configure_remote_workspace(self, remote_workspace: Any | None) -> None:
        self.remote_workspace = remote_workspace

    def configure_remote_message_source(self, remote_message_source: Any | None) -> None:
        self.remote_message_source = remote_message_source if callable(remote_message_source) else None

    def configure_pause_waiter(self, pause_waiter: Any | None) -> None:
        self.pause_waiter = pause_waiter if callable(pause_waiter) else None

    def configure_openai_request_context(self, task_id: str | None = None) -> None:
        self._current_task_id = self._sanitize_session_id(task_id or "")
        self._openai_request_counter = 0

    def configure_scheduler_context(self, scheduled_tasks: Any | None) -> None:
        if not isinstance(scheduled_tasks, list):
            self.scheduled_tasks_context = []
            return
        normalized: List[Dict[str, Any]] = []
        for item in scheduled_tasks[:12]:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            run_at = str(item.get("runAt") or item.get("run_at") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if not task_id or not prompt or not run_at or not status:
                continue
            normalized.append(
                {
                    "id": task_id,
                    "prompt": prompt,
                    "runAt": run_at,
                    "status": status,
                    "timezone": str(item.get("timezone") or "UTC").strip() or "UTC",
                    "deviceId": str(item.get("deviceId") or "").strip(),
                    "recurrence": item.get("recurrence") if isinstance(item.get("recurrence"), dict) else None,
                }
            )
        self.scheduled_tasks_context = normalized

    def _inject_remote_messages(self) -> List[str]:
        source = self.remote_message_source
        if not callable(source):
            return []
        try:
            incoming = source()
        except Exception as exc:
            print(f"[Brain] Remote message source failed: {exc}", flush=True)
            return []
        if not incoming:
            return []
        if not isinstance(incoming, list):
            incoming = [incoming]
        previews: List[str] = []
        for item in incoming:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                role = str(item.get("role") or "operator").strip() or "operator"
            else:
                text = str(item or "").strip()
                role = "operator"
            if not text:
                continue
            previews.append(text[:240])
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"REMOTE {role.upper()} MESSAGE\n{text}\n\n"
                        "Treat this as a new instruction for the same running task. "
                        "Do not restart from scratch unless the new instruction explicitly requires it."
                    ),
                }
            )
        return previews

    def _wait_if_paused(self) -> None:
        waiter = self.pause_waiter
        if callable(waiter):
            waiter()

    def configure_task_budget(self, max_budget_usd: float | None) -> None:
        try:
            value = float(max_budget_usd or 0.0)
        except Exception:
            value = 0.0
        self.max_budget_usd = max(0.0, value)
        self._budget_exceeded_reason = ""

    def _budget_limit_reached(self) -> bool:
        if self.max_budget_usd <= 0:
            return False
        with self._usage_lock:
            current_cost = float((self._current_task_usage or {}).get("cost_usd") or 0.0)
        return current_cost >= self.max_budget_usd

    def _budget_failure_reason(self) -> str:
        if self._budget_exceeded_reason:
            return self._budget_exceeded_reason
        return f"Task budget reached (${self.max_budget_usd:.2f} max)."

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @staticmethod
    def _usage_get(value: Any, key: str, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    def _model_pricing(self, model_name: str) -> Optional[Dict[str, float]]:
        normalized = str(model_name or "").strip().lower()
        for prefix, pricing in MODEL_PRICING_USD_PER_MILLION.items():
            if normalized.startswith(prefix):
                return pricing
        return None

    def _reset_task_usage(self) -> None:
        with self._usage_lock:
            self._task_started_at = time.monotonic()
            self._current_task_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cost_usd": 0.0,
                "model_calls": 0,
                "tool_calls": 0,
                "models": {},
                "tools": {},
            }
            self.last_task_usage_summary = {}
            self._budget_exceeded_reason = ""

    def _record_tool_usage(self, action_type: Any) -> None:
        normalized = str(action_type or "").strip().lower() or "unknown"
        with self._usage_lock:
            usage_state = self._current_task_usage
            usage_state["tool_calls"] = int(usage_state.get("tool_calls") or 0) + 1
            tools = usage_state.setdefault("tools", {})
            if isinstance(tools, dict):
                tools[normalized] = int(tools.get(normalized) or 0) + 1

    def _record_model_usage(self, response: Any, requested_model: Optional[str], source: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        model_name = str(getattr(response, "model", None) or requested_model or self.decision_model or "").strip()
        input_tokens = self._safe_int(
            self._usage_get(usage, "input_tokens")
            or self._usage_get(usage, "prompt_tokens")
        )
        output_tokens = self._safe_int(
            self._usage_get(usage, "output_tokens")
            or self._usage_get(usage, "completion_tokens")
        )

        input_details = self._usage_get(usage, "input_tokens_details")
        prompt_details = self._usage_get(usage, "prompt_tokens_details")
        cached_input_tokens = self._safe_int(
            self._usage_get(input_details, "cached_tokens")
            or self._usage_get(prompt_details, "cached_tokens")
            or self._usage_get(usage, "cached_input_tokens")
        )
        cached_input_tokens = max(0, min(cached_input_tokens, input_tokens))

        pricing = self._model_pricing(model_name)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        cost_usd = 0.0
        if pricing:
            cost_usd = (
                (uncached_input_tokens / 1_000_000.0) * pricing["input"]
                + (cached_input_tokens / 1_000_000.0) * pricing["cached_input"]
                + (output_tokens / 1_000_000.0) * pricing["output"]
            )

        with self._usage_lock:
            usage_state = self._current_task_usage
            usage_state["input_tokens"] += input_tokens
            usage_state["output_tokens"] += output_tokens
            usage_state["cached_input_tokens"] += cached_input_tokens
            usage_state["cost_usd"] += cost_usd
            usage_state["model_calls"] += 1
            total_cost_usd = float(usage_state["cost_usd"] or 0.0)

            model_bucket = usage_state["models"].setdefault(
                model_name or "unknown",
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "cost_usd": 0.0,
                    "calls": 0,
                    "sources": [],
                },
            )
            model_bucket["input_tokens"] += input_tokens
            model_bucket["output_tokens"] += output_tokens
            model_bucket["cached_input_tokens"] += cached_input_tokens
            model_bucket["cost_usd"] += cost_usd
            model_bucket["calls"] += 1
            if source not in model_bucket["sources"]:
                model_bucket["sources"].append(source)
            if self.max_budget_usd > 0 and total_cost_usd >= self.max_budget_usd:
                self._budget_exceeded_reason = (
                    f"Task budget reached (${self.max_budget_usd:.2f} max, ${total_cost_usd:.2f} used)."
                )

    def _finalize_task_usage(self, result: Dict[str, Any]) -> None:
        with self._usage_lock:
            raw = dict(self._current_task_usage or {})
        elapsed_seconds = max(0.0, time.monotonic() - (self._task_started_at or time.monotonic()))
        self.last_task_usage_summary = {
            "input_tokens": int(raw.get("input_tokens") or 0),
            "output_tokens": int(raw.get("output_tokens") or 0),
            "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
            "cost_usd": round(float(raw.get("cost_usd") or 0.0), 6),
            "model_calls": int(raw.get("model_calls") or 0),
            "tool_calls": int(raw.get("tool_calls") or 0),
            "models": raw.get("models") or {},
            "tools": raw.get("tools") or {},
            "success": bool(result.get("success")),
            "steps": int(result.get("steps") or 0),
            "elapsed_seconds": round(elapsed_seconds, 3),
            "goal": self.goal_text,
        }

    def current_task_usage_summary(self) -> Dict[str, Any]:
        with self._usage_lock:
            raw = dict(self._current_task_usage or {})
        return {
            "input_tokens": int(raw.get("input_tokens") or 0),
            "output_tokens": int(raw.get("output_tokens") or 0),
            "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
            "cost_usd": round(float(raw.get("cost_usd") or 0.0), 6),
            "model_calls": int(raw.get("model_calls") or 0),
            "tool_calls": int(raw.get("tool_calls") or 0),
            "models": raw.get("models") or {},
            "tools": raw.get("tools") or {},
            "max_budget_usd": round(float(self.max_budget_usd or 0.0), 6),
        }

    def _aria_surface(self) -> str:
        return "remote" if self.remote_computer is not None else "local"

    def _aria_version(self) -> str:
        value = (
            os.environ.get("ARIA_VERSION")
            or os.environ.get("VERCEL_GIT_COMMIT_SHA")
            or os.environ.get("RENDER_GIT_COMMIT")
            or "dev"
        )
        return str(value).strip()[:64] or "dev"

    def _aria_actor_seed(self) -> str:
        seed = (
            str(self.openai_api_key or "").strip()
            or self._current_session_id
            or self._current_task_id
            or "anonymous"
        )
        return seed[:512]

    def _aria_safety_identifier(self) -> str:
        digest = hashlib.sha256(
            f"ariaos::{self.openai_base_url}::{self._aria_actor_seed()}".encode("utf-8")
        ).hexdigest()[:24]
        return f"ariaos_{digest}"

    def _build_openai_tracking_context(
        self,
        *,
        source: str,
        model: Optional[str],
    ) -> Dict[str, Any]:
        self._openai_request_counter += 1
        aria_request_id = f"ariaos-{source[:18]}-{self._openai_request_counter}-{uuid.uuid4().hex[:8]}"
        metadata = {
            "app": "ariaos",
            "mode": "byok",
            "surface": self._aria_surface(),
            "source": str(source or "unknown")[:40],
            "version": self._aria_version(),
            "aria_request_id": aria_request_id,
        }
        if self._current_session_id:
            metadata["session_id"] = self._current_session_id[:64]
        if self._current_task_id:
            metadata["task_id"] = self._current_task_id[:64]
        if model:
            metadata["requested_model"] = str(model).strip()[:64]

        safety_identifier = self._aria_safety_identifier()
        return {
            "aria_request_id": aria_request_id,
            "metadata": metadata,
            "safety_identifier": safety_identifier,
            "user": safety_identifier,
            "extra_headers": {
                "X-Client-Request-Id": aria_request_id,
            },
        }

    def _apply_openai_tracking(
        self,
        *,
        kwargs: Dict[str, Any],
        source: str,
        model: Optional[str],
    ) -> tuple[Dict[str, Any], str]:
        tracking = self._build_openai_tracking_context(source=source, model=model)
        merged = dict(kwargs)

        if bool(merged.get("store")):
            existing_metadata = merged.get("metadata")
            merged_metadata: Dict[str, Any] = {}
            if isinstance(existing_metadata, dict):
                for key, value in existing_metadata.items():
                    merged_metadata[str(key)[:64]] = str(value)[:256]
            merged_metadata.update(tracking["metadata"])
            merged["metadata"] = merged_metadata
        else:
            merged.pop("metadata", None)

        existing_headers = merged.get("extra_headers")
        merged_headers: Dict[str, str] = {}
        if isinstance(existing_headers, dict):
            for key, value in existing_headers.items():
                merged_headers[str(key)] = str(value)
        merged_headers.update(tracking["extra_headers"])
        merged["extra_headers"] = merged_headers
        merged["safety_identifier"] = str(merged.get("safety_identifier") or tracking["safety_identifier"])
        merged["user"] = str(merged.get("user") or tracking["user"])
        return merged, str(tracking["aria_request_id"])

    def _trace_openai_request(
        self,
        *,
        source: str,
        aria_request_id: str,
        model: Optional[str],
        response: Any = None,
        error: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "source": source,
            "aria_request_id": aria_request_id,
            "surface": self._aria_surface(),
            "session_id": self._current_session_id or None,
            "task_id": self._current_task_id or None,
            "requested_model": str(model or self.decision_model or "").strip() or None,
            "response_id": str(getattr(response, "id", "") or "").strip() or None,
        }
        if error:
            payload["error"] = str(error)[:240]
        self._trace("openai_request", payload)

    def _chat_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_completion_tokens: int,
        model: Optional[str] = None,
        source: str = "chat.completions",
    ):
        if not self.client:
            raise RuntimeError("OpenAI client not available")

        model_to_use = model or self.decision_model
        request_kwargs, aria_request_id = self._apply_openai_tracking(
            kwargs={
                "model": model_to_use,
                "messages": messages,
                "temperature": temperature,
                "max_completion_tokens": max_completion_tokens,
            },
            source=source,
            model=model_to_use,
        )
        try:
            response = self._run_interruptible_model_call(
                lambda: self.client.chat.completions.create(**request_kwargs)
            )
        except Exception as exc:
            self._trace_openai_request(
                source=source,
                aria_request_id=aria_request_id,
                model=model_to_use,
                error=str(exc),
            )
            raise
        try:
            self._trace_openai_request(
                source=source,
                aria_request_id=aria_request_id,
                model=model_to_use,
                response=response,
            )
        except Exception:
            pass
        self._record_model_usage(response, model_to_use, "chat.completions")
        if self._budget_limit_reached():
            raise TaskBudgetExceeded(self._budget_failure_reason())
        return response

    def _run_interruptible_model_call(self, fn):
        if not self._active_stop_event:
            return fn()

        state: Dict[str, Any] = {}
        finished = threading.Event()

        def _worker():
            try:
                state["result"] = fn()
            except Exception as exc:
                state["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        while not finished.wait(0.20):
            if self._stop_requested():
                raise UserStopRequested("Stopped by user during model call")

        if "error" in state:
            raise state["error"]
        return state.get("result")

    def _responses_create(self, *, source: str = "responses", **kwargs):
        if not self.client:
            raise RuntimeError("OpenAI client not available")
        request_kwargs, aria_request_id = self._apply_openai_tracking(
            kwargs=kwargs,
            source=source,
            model=kwargs.get("model"),
        )
        try:
            response = self._run_interruptible_model_call(
                lambda: self.client.responses.create(**request_kwargs)
            )
        except Exception as exc:
            self._trace_openai_request(
                source=source,
                aria_request_id=aria_request_id,
                model=request_kwargs.get("model"),
                error=str(exc),
            )
            raise
        try:
            self._trace_openai_request(
                source=source,
                aria_request_id=aria_request_id,
                model=request_kwargs.get("model"),
                response=response,
            )
        except Exception:
            pass
        self._record_model_usage(response, request_kwargs.get("model"), "responses")
        if self._budget_limit_reached():
            raise TaskBudgetExceeded(self._budget_failure_reason())
        return response

    def _log_chunk(self, text: str) -> str:
        lines = []
        for line in (text or "").splitlines(keepends=True):
            if line.strip():
                lines.append(f"[LOG] {line}")
            else:
                lines.append(line)
        return "".join(lines)

    def _prepare_pending_user_image(self, pending_image: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if not isinstance(pending_image, dict):
            return None
        mime = str(pending_image.get("mime") or pending_image.get("mime_type") or "").strip().lower()
        data_b64 = str(pending_image.get("data") or "").strip()
        if not mime.startswith("image/") or not data_b64:
            return None

        prepared = {
            "mime": mime,
            "data": data_b64,
            "filename": str(pending_image.get("filename") or "").strip(),
            "detail": USER_IMAGE_DETAIL,
        }
        if Image is None:
            return prepared

        try:
            raw = base64.b64decode(data_b64, validate=True)
            with Image.open(io.BytesIO(raw)) as src:
                img = ImageOps.exif_transpose(src) if ImageOps is not None else src.copy()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "A" in getattr(img, "getbands", lambda: ())() else "RGB")

                width, height = img.size
                if width < 1 or height < 1:
                    return prepared

                min_side = min(width, height)
                max_side = max(width, height)
                scale = 1.0
                if min_side < USER_IMAGE_MIN_SIDE:
                    scale = max(scale, USER_IMAGE_MIN_SIDE / float(min_side))
                if max_side * scale > USER_IMAGE_MAX_SIDE:
                    scale = USER_IMAGE_MAX_SIDE / float(max_side)

                if abs(scale - 1.0) > 0.01:
                    resampling = getattr(Image, "Resampling", Image)
                    resample = resampling.NEAREST if scale > 1.0 else resampling.LANCZOS
                    img = img.resize(
                        (
                            max(1, int(round(width * scale))),
                            max(1, int(round(height * scale))),
                        ),
                        resample=resample,
                    )

                out = io.BytesIO()
                has_alpha = "A" in getattr(img, "getbands", lambda: ())()
                if has_alpha:
                    out_mime = "image/png"
                    img.save(out, format="PNG", optimize=True)
                else:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    out_mime = "image/jpeg"
                    img.save(out, format="JPEG", quality=90, optimize=True)

                prepared["mime"] = out_mime
                prepared["data"] = base64.b64encode(out.getvalue()).decode("ascii")
                print(
                    f"[Brain] Prepared user image "
                    f"{prepared['filename'] or '<inline>'}: {width}x{height} -> {img.width}x{img.height}, "
                    f"mime={out_mime}, detail={prepared['detail']}",
                    flush=True,
                )
        except Exception as e:
            print(f"[Brain] User image preprocessing failed: {e}", flush=True)

        return prepared

    def _response_get(self, obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _computer_tool_spec(self) -> Dict[str, Any]:
        # The planner action surface is mostly JSON dispatched locally by this
        # runtime. "computer" is the one branch that hands control to the
        # native OpenAI computer-use tool for low-level desktop interaction.
        return {
            "type": "computer",
        }

    def _capture_screen_data_url(self) -> tuple:
        image_b64, screenshot_path = self._capture_raw_screen_base64()
        if not image_b64:
            return "", screenshot_path
        return f"data:image/png;base64,{image_b64}", screenshot_path

    def _extract_response_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return str(output_text).strip()

        parts: List[str] = []
        for item in getattr(response, "output", []) or []:
            item_type = str(self._response_get(item, "type", "") or "").strip().lower()
            if item_type == "message":
                for chunk in self._response_get(item, "content", []) or []:
                    chunk_type = str(self._response_get(chunk, "type", "") or "").strip().lower()
                    if chunk_type in {"output_text", "text"}:
                        text = str(self._response_get(chunk, "text", "") or "").strip()
                        if text:
                            parts.append(text)
        return "\n".join(parts).strip()

    def _extract_computer_calls(self, response: Any) -> List[Any]:
        calls: List[Any] = []
        for item in getattr(response, "output", []) or []:
            if str(self._response_get(item, "type", "") or "").strip().lower() == "computer_call":
                calls.append(item)
        return calls

    def _extract_computer_actions(self, computer_call: Any) -> List[Any]:
        actions = self._response_get(computer_call, "actions")
        if isinstance(actions, list):
            flattened: List[Any] = []
            for item in actions:
                if isinstance(item, list):
                    flattened.extend(item)
                else:
                    flattened.append(item)
            return flattened
        action = self._response_get(computer_call, "action")
        if action is not None:
            return [action]
        return []

    def _stop_requested(self) -> bool:
        return bool(self._active_stop_event and self._active_stop_event.is_set())

    def _computer_stop_result(self, logs: Optional[List[str]] = None) -> Dict[str, Any]:
        history = "\n".join((logs or [])[-12:]) if logs else "(no actions executed)"
        return {
            "success": False,
            "stopped": True,
            "summary": "Stopped by user.",
            "observation": (
                "STOPPED: computer use interrupted by user.\n\n"
                "COMPUTER ACTION LOG:\n"
                f"{history}"
            ),
        }

    def _sync_perception_state(self, layer: str, snapshot_text: str) -> None:
        text = str(snapshot_text or "").strip()
        confidence = 0.82 if text and not text.startswith("ERROR:") else 0.10
        self.current_elements = {}
        self.current_frame_id = ""
        self.frame_element_cache = {}
        self.frame_cache_order = []
        self.perception.current_elements = {}
        self.perception.last_snapshot = {
            "layer": layer,
            "confidence": confidence,
            "raw_elements": 0,
            "visible_elements": 0,
            "metadata": {"vision_source": f"gpt-5.4_{layer}"},
        }

    def _record_computer_visual_confirmation(self, screen_after: str, summary: str) -> None:
        signals = self.verifier.observe_screen("vision", screen_after)
        self.last_screen_signals = signals
        if summary and not summary.startswith("ERROR:"):
            self.verifier._record("computer", True, summary[:240])

    def _extract_computer_call_id(self, computer_call: Any) -> str:
        for key in ("call_id", "id"):
            value = str(self._response_get(computer_call, key, "") or "").strip()
            if value:
                return value
        return ""

    def _normalize_computer_keys(self, keys: Any) -> List[str]:
        if isinstance(keys, str):
            raw = [part for part in re.split(r"[+,]", keys) if part.strip()]
        elif isinstance(keys, list):
            raw = [str(part).strip() for part in keys if str(part).strip()]
        else:
            raw = []
        normalized = []
        for key in raw:
            low = key.strip().lower()
            if low == "control":
                low = "ctrl"
            elif low == "return":
                low = "enter"
            normalized.append(low)
        return normalized

    def _run_pointer_drag(self, x: int, y: int) -> tuple:
        try:
            subprocess.run(
                [
                    "xdotool",
                    "mousedown",
                    "1",
                    "mousemove",
                    "--sync",
                    str(int(x)),
                    str(int(y)),
                    "mouseup",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                env=self.env,
            )
            return True, f"dragged_to:{int(x)},{int(y)}"
        except Exception as e:
            return False, f"drag_failed:{e}"

    def _execute_computer_action(self, action: Any) -> tuple:
        if self.remote_computer is not None:
            return self.remote_computer.execute_action(action)
        action_type = str(self._response_get(action, "type", "") or "").strip().lower()
        if not action_type:
            return False, "computer_action_missing_type"

        try:
            if action_type == "click":
                x = int(self._response_get(action, "x"))
                y = int(self._response_get(action, "y"))
                button = str(self._response_get(action, "button", "left") or "left").strip().lower()
                if button == "right":
                    ok, detail = self.executor.right_click(x, y)
                else:
                    ok, detail = self.executor.click(x, y)
                return ok, f"click({x},{y},{button}) {detail}"

            if action_type == "double_click":
                x = int(self._response_get(action, "x"))
                y = int(self._response_get(action, "y"))
                ok, detail = self.executor.double_click(x, y)
                return ok, f"double_click({x},{y}) {detail}"

            if action_type == "scroll":
                scroll_y = self._response_get(action, "scroll_y")
                if scroll_y is None:
                    scroll_y = self._response_get(action, "delta_y", self._response_get(action, "y", 0))
                try:
                    scroll_y = int(scroll_y or 0)
                except (TypeError, ValueError):
                    scroll_y = 0
                direction = "down" if scroll_y >= 0 else "up"
                amount = max(1, min(12, max(1, abs(scroll_y) // 240 or 3)))
                ok, detail = self.executor.scroll(direction=direction, amount=amount)
                return ok, f"scroll({direction},{amount}) {detail}"

            if action_type == "type":
                text = str(self._response_get(action, "text", "") or "")
                ok, detail = self.executor.type_text(text)
                return ok, f"type({len(text)} chars) {detail}"

            if action_type in {"keypress", "key_press"}:
                keys = self._response_get(action, "keys")
                if not keys:
                    keys = [self._response_get(action, "key", "")]
                normalized = self._normalize_computer_keys(keys)
                ok, detail = self.executor.key_combo(normalized)
                return ok, f"keypress({'+'.join(normalized)}) {detail}"

            if action_type == "wait":
                seconds = float(self._response_get(action, "seconds", 1) or 1)
                time.sleep(max(0.05, min(seconds, 10.0)))
                return True, f"wait({seconds:.2f}s)"

            if action_type == "move":
                x = int(self._response_get(action, "x"))
                y = int(self._response_get(action, "y"))
                subprocess.run(
                    ["xdotool", "mousemove", str(x), str(y)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=self.env,
                )
                return True, f"move({x},{y})"

            if action_type == "drag":
                path = self._response_get(action, "path") or []
                if not path:
                    return False, "drag_failed:empty_path"
                last_point = path[-1]
                x = int(self._response_get(last_point, "x"))
                y = int(self._response_get(last_point, "y"))
                return self._run_pointer_drag(x, y)

            if action_type == "screenshot":
                return True, "screenshot_requested"

            return False, f"unsupported_computer_action:{action_type}"
        except Exception as e:
            return False, f"{action_type}_failed:{e}"

    def _screen_snapshot_via_gpt(self, query: Optional[str] = None) -> str:
        data_url, _ = self._capture_screen_data_url()
        if not data_url:
            self.current_elements = {}
            self.current_frame_id = ""
            self.last_screen_signals = {}
            return (
                "PERCEPTION SNAPSHOT [vision]\n"
                "confidence: 0.00\n"
                "elements_visible: 0/0\n"
                f"viewport: {SCREEN_RESOLUTION[0]}x{SCREEN_RESOLUTION[1]}\n"
                "vision_source: gpt-5.4_screenshot\n"
                "ELEMENTS:\n"
                "(none)\n"
                "RAW_EXCERPT:\n"
                "ERROR: could not capture screenshot."
            )

        question = (query or "").strip() or (
            "Summarize the current Linux desktop state for an autonomous operator. "
            "Include the active app/window, important visible labels, buttons, dialogs, errors, confirmations, "
            "and any URL if visible. Keep it concise and factual. Do not invent coordinates."
        )
        try:
            response = self._chat_completion(
                model=self.vision_model,
                temperature=0,
                max_completion_tokens=500,
                source="screen_summary",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a screen observer for desktop automation. "
                            "Return only a concise factual screen summary. "
                            "Do not emit JSON or actions. Do not invent coordinates."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"CURRENT GOAL:\n{self.goal_text}\n\nREQUEST:\n{question}"},
                            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                        ],
                    },
                ],
            )
            content = response.choices[0].message.content or ""
            if isinstance(content, list):
                parts: List[str] = []
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        parts.append(str(chunk.get("text", "")))
                    else:
                        parts.append(str(chunk))
                content = "\n".join(parts)
            summary = re.sub(r"[ \t]+\n", "\n", str(content).strip()) or "No screen summary returned."
        except Exception as e:
            summary = f"ERROR: screen vision failed: {e}"

        self.current_elements = {}
        self.current_frame_id = ""
        self.frame_element_cache = {}
        self.frame_cache_order = []
        self.perception.current_elements = {}
        self.perception.last_snapshot = {
            "layer": "vision",
            "confidence": 0.82 if not summary.startswith("ERROR:") else 0.10,
            "raw_elements": 0,
            "visible_elements": 0,
            "metadata": {"vision_source": "gpt-5.4_screenshot"},
        }
        return "\n".join([
            "PERCEPTION SNAPSHOT [vision]",
            f"confidence: {0.82 if not summary.startswith('ERROR:') else 0.10:.2f}",
            "elements_visible: 1/1" if not summary.startswith("ERROR:") else "elements_visible: 0/0",
            f"viewport: {SCREEN_RESOLUTION[0]}x{SCREEN_RESOLUTION[1]}",
            "vision_source: gpt-5.4_screenshot",
            "ELEMENTS:",
            "(none)",
            "RAW_EXCERPT:",
            summary[:2200],
        ])

    def _run_computer_use(self, instruction: str, max_turns: int = 0) -> Dict[str, Any]:
        # This is the second control layer of AriaOS:
        # 1. the main planner chooses action="computer"
        # 2. this helper runs a dedicated OpenAI Responses loop with the native
        #    computer tool until the UI subtask is completed.
        if self._stop_requested():
            return self._computer_stop_result()
        if not self.client:
            return {
                "success": False,
                "summary": "OpenAI client unavailable for computer use.",
                "observation": "ERROR: OpenAI client unavailable for computer use.",
            }

        instruction = str(instruction or "").strip() or self.goal_text
        prompt = (
            f"CURRENT GOAL:\n{self.goal_text}\n\n"
            f"SUBTASK:\n{instruction}\n\n"
            "Operate the Linux desktop directly. Use the computer tool to interact with the UI. "
            "When the subtask is complete, stop issuing computer actions and return a short factual result."
        )
        tool_spec = self._computer_tool_spec()
        logs: List[str] = []

        try:
            if self._stop_requested():
                return self._computer_stop_result(logs)
            response = self._responses_create(
                model=self.decision_model,
                tools=[tool_spec],
                input=prompt,
                reasoning={"effort": "medium"},
                truncation="auto",
                source="computer_use_start",
            )
        except UserStopRequested:
            return self._computer_stop_result(logs)
        except Exception as e:
            return {
                "success": False,
                "summary": f"Computer use start failed: {e}",
                "observation": f"ERROR: computer use start failed: {e}",
            }

        turn = 0
        while True:
            turn += 1
            if max_turns > 0 and turn > max_turns:
                break
            if self._stop_requested():
                return self._computer_stop_result(logs)
            computer_calls = self._extract_computer_calls(response)
            if not computer_calls:
                final_text = self._extract_response_text(response) or "Computer subtask completed."
                screen_after = self._screen_snapshot_via_gpt("Summarize the current screen after the subtask.")
                self._sync_perception_state("vision", screen_after)
                self._record_computer_visual_confirmation(screen_after, final_text)
                observation = (
                    "COMPUTER USE RESULT:\n"
                    f"{final_text}\n\n"
                    "COMPUTER ACTION LOG:\n"
                    + ("\n".join(logs[-12:]) if logs else "(no actions executed)")
                    + "\n\nSCREEN AFTER COMPUTER USE:\n"
                    + screen_after
                )
                return {
                    "success": True,
                    "summary": final_text,
                    "observation": observation,
                    "turns": turn,
                }

            computer_call = computer_calls[0]
            actions = self._extract_computer_actions(computer_call)
            call_id = self._extract_computer_call_id(computer_call)
            if not call_id:
                return {
                    "success": False,
                    "summary": "Computer use call missing call_id.",
                    "observation": "ERROR: computer use call missing call_id.",
                }

            if len(computer_calls) > 1:
                logs.append(f"computer_calls_truncated:{len(computer_calls)}->1")

            if len(actions) > 1:
                logs.append(f"computer_actions_truncated:{len(actions)}->1")
                actions = actions[:1]

            if not actions:
                logs.append("computer_call: no actions")
            else:
                for action in actions:
                    if self._stop_requested():
                        return self._computer_stop_result(logs)
                    ok, detail = self._execute_computer_action(action)
                    logs.append(detail)
                    self.input_backend = self.executor.backend_name
                    self.input_device = self.executor.backend
                    time.sleep(0.18)
                    if self._stop_requested():
                        return self._computer_stop_result(logs)
                    if not ok:
                        screen_after = self._screen_snapshot_via_gpt("Summarize the current screen after the failed subtask.")
                        self._sync_perception_state("vision", screen_after)
                        self.last_screen_signals = self.verifier.observe_screen("vision", screen_after)
                        observation = (
                            f"ERROR: computer action failed: {detail}\n\n"
                            "COMPUTER ACTION LOG:\n"
                            + "\n".join(logs[-12:])
                            + "\n\nCURRENT SCREEN:\n"
                            + screen_after
                        )
                        return {
                            "success": False,
                            "summary": f"Computer action failed: {detail}",
                            "observation": observation,
                        }

            time.sleep(0.35)
            if self._stop_requested():
                return self._computer_stop_result(logs)
            next_data_url, _ = self._capture_screen_data_url()
            if not next_data_url:
                return {
                    "success": False,
                    "summary": "Computer use could not capture follow-up screenshot.",
                    "observation": "ERROR: computer use could not capture follow-up screenshot.",
                }

            try:
                if self._stop_requested():
                    return self._computer_stop_result(logs)
                response = self._responses_create(
                    model=self.decision_model,
                    previous_response_id=response.id,
                    tools=[tool_spec],
                    input=[{
                        "type": "computer_call_output",
                        "call_id": call_id,
                        "output": {
                            "type": "computer_screenshot",
                            "image_url": next_data_url,
                            "detail": "original",
                        },
                    }],
                    truncation="auto",
                    source="computer_use_continue",
                )
            except UserStopRequested:
                return self._computer_stop_result(logs)
            except Exception as e:
                return {
                    "success": False,
                    "summary": f"Computer use continue failed: {e}",
                    "observation": f"ERROR: computer use continue failed: {e}",
                }

        screen_after = self._screen_snapshot_via_gpt("Summarize the current screen after the unfinished subtask.")
        self._sync_perception_state("vision", screen_after)
        observation = (
            f"ERROR: computer use reached the turn limit ({max_turns}).\n\n"
            "COMPUTER ACTION LOG:\n"
            + ("\n".join(logs[-12:]) if logs else "(no actions executed)")
            + "\n\nCURRENT SCREEN:\n"
            + screen_after
        )
        return {
            "success": False,
            "summary": f"Computer use reached the turn limit ({max_turns}).",
            "observation": observation,
        }

    def _normalize_session_entry(self, entry: str) -> str:
        text = re.sub(r"\s+", " ", str(entry or "").strip())
        text = re.sub(r"^Step\s+\d+\s*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Fact:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Result:\s*", "", text, flags=re.IGNORECASE)
        return text.strip(" -")[:280]

    def _session_entry_priority(self, entry: str) -> int:
        low = entry.lower()
        score = 0
        if "/" in entry or any(token in low for token in (".py", ".json", ".md", ".png", ".jpg", ".jpeg", ".html", ".css", ".js")):
            score += 3
        if re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", entry):
            score += 3
        if re.search(r"\b[A-Z0-9]+(?:[-_][A-Z0-9]+)+\b", entry):
            score += 2
        if re.search(r"\b(subject|recipient|recipient email|message body|body|folder|directory|path|owner|status|codename|deadline|due date|backup|browser|account|url|session id|task id)\b", low):
            score += 2
        if re.search(r"^[A-Za-z][A-Za-z0-9 _-]{0,40}:\s+\S", entry):
            score += 1
        if any(
            token in low
            for token in (
                "created", "saved", "wrote", "modified", "updated", "installed", "downloaded",
                "uploaded", "opened", "launched", "started", "posted", "sent", "replied",
                "logged in", "navigated", "verified", "attached", "loaded", "selected", "connected",
            )
        ):
            score += 3
        if any(token in low for token in ("http://", "https://", "port ", "ws://", "gmail", "linkedin", "tiktok")):
            score += 2
        if any(token in low for token in ("failed", "error", "not visible", "not found", "missing", "denied", "timeout", "blocked")):
            score += 2
        if any(token in low for token in ("looking for", "searching", "trying", "attempting", "checking", "need to", "preparing")):
            score -= 2
        if any(token in low for token in ("interaction", "workflow", "flow", "subtask")) and score <= 0:
            score -= 1
        return score

    def _select_durable_session_facts(self, entries: List[str], limit: int = SESSION_HISTORY_FACTS_PER_TASK) -> List[str]:
        candidates: List[tuple] = []
        seen = set()
        for idx, entry in enumerate(entries):
            text = self._normalize_session_entry(entry)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((idx, self._session_entry_priority(text), text))

        if not candidates:
            return []

        prioritized = [item for item in candidates if item[1] > 0]
        if not prioritized:
            prioritized = candidates[-limit:]

        chosen = sorted(prioritized, key=lambda item: (-item[1], item[0], len(item[2])))[:limit]
        chosen.sort(key=lambda item: item[0])
        return [item[2] for item in chosen]

    def _compact_session_chunk(self, entries: List[str]) -> List[str]:
        if not entries:
            return []

        header = str(entries[0] or "").strip()
        facts_source: List[str] = []
        result_text = ""
        for entry in entries[1:]:
            raw = str(entry or "").strip()
            if not raw:
                continue
            if raw.lower().startswith("result:"):
                result_text = self._normalize_session_entry(raw)
                continue
            facts_source.append(raw)

        facts = self._select_durable_session_facts(facts_source)
        compacted = [header] if header else []
        compacted.extend(f"Fact: {fact}" for fact in facts)
        if result_text and result_text.lower() not in {fact.lower() for fact in facts}:
            compacted.append(f"Result: {result_text}")
        return compacted

    def _sanitize_session_id(self, session_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "").strip())[:120]

    def _flush_summary_threads(self, timeout: float = 1.2) -> None:
        deadline = time.time() + max(0.0, timeout)
        with self._summary_lock:
            threads = list(self._summary_threads)
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(timeout=min(remaining, 0.25))
        with self._summary_lock:
            self._summary_threads = [t for t in self._summary_threads if t.is_alive()]

    def _snapshot_progress_log(self, flush: bool = False) -> List[str]:
        if flush:
            self._flush_summary_threads()
        with self._summary_lock:
            return list(self.progress_log)

    def _build_current_task_progress_sections(self, entries: List[str]) -> List[str]:
        normalized_entries = [self._normalize_session_entry(entry) for entry in entries or []]
        normalized_entries = [entry for entry in normalized_entries if entry]
        if not normalized_entries:
            return []

        sections: List[str] = []
        checkpoint = self._select_durable_session_facts(
            normalized_entries,
            limit=max(1, CURRENT_TASK_CHECKPOINT_FACTS),
        )
        if checkpoint:
            sections.append("CURRENT TASK CHECKPOINT:\n" + "\n".join(f"- {fact}" for fact in checkpoint))

        recent_limit = max(1, CURRENT_TASK_RECENT_STEPS)
        recent_entries = normalized_entries[-recent_limit:]
        if recent_entries:
            sections.append("CURRENT TASK RECENT STEPS:\n" + "\n".join(recent_entries))

        return sections

    def _append_progress_snapshot_to_session(
        self,
        goal: str,
        progress_snapshot: List[str],
        success: Optional[bool],
        step_count: int,
        summary_text: str = "",
        clear_current: bool = True,
    ) -> None:
        goal_text = re.sub(r"\s+", " ", str(goal or "").strip())
        status = "COMPLETED" if success is True else "FAILED" if success is False else "ENDED"
        if goal_text:
            self.session_log.append(f'--- {status}: "{goal_text[:240]}" ---')
        else:
            self.session_log.append(f"--- {status} ---")
        compacted_facts = self._select_durable_session_facts(progress_snapshot)
        for fact in compacted_facts:
            self.session_log.append(f"Fact: {fact}")
        result_text = self._normalize_session_entry(summary_text)
        if result_text and result_text.lower() not in {fact.lower() for fact in compacted_facts}:
            self.session_log.append(f"Result: {result_text[:240]}")
        self.session_step_offset += max(0, int(step_count or 0))
        if clear_current:
            with self._summary_lock:
                self.progress_log = []
            self.step_count = 0

    def _compress_session_log(self, log: List[str]) -> List[str]:
        if not log:
            return []

        chunks: List[List[str]] = []
        current_chunk: List[str] = []
        for entry in log:
            text = str(entry or "").strip()
            if not text:
                continue
            if text.startswith("--- "):
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = [text]
            else:
                if not current_chunk:
                    current_chunk = ["--- SESSION CONTEXT ---"]
                current_chunk.append(text)
        if current_chunk:
            chunks.append(current_chunk)

        compacted_chunks = [self._compact_session_chunk(chunk) for chunk in chunks]
        compacted_chunks = [chunk for chunk in compacted_chunks if chunk]

        if len(compacted_chunks) > SESSION_HISTORY_MAX_TASKS:
            keep_first = compacted_chunks[:1]
            keep_recent = compacted_chunks[-max(1, SESSION_HISTORY_MAX_TASKS - len(keep_first)):]
            selected_chunks = keep_first[:]
            for chunk in keep_recent:
                if chunk not in selected_chunks:
                    selected_chunks.append(chunk)
            compacted_chunks = selected_chunks

        flattened: List[str] = []
        for chunk in compacted_chunks:
            flattened.extend(chunk)
        return flattened[-60:]

    def reset_runtime_session_state(self) -> None:
        with self._summary_lock:
            self.progress_log = []
        self.session_log = []
        self.session_step_offset = 0
        self.step_count = 0
        self.goal_text = ""
        self.goal_lower = ""
        self.current_elements = {}
        self.current_frame_id = ""
        self.frame_element_cache = {}
        self.frame_cache_order = []
        self.last_action_signature = ""
        self.last_action_streak = 0
        self.command_failures = {}
        self._last_goal_success = None
        self._current_task_id = ""
        self._openai_request_counter = 0
        self._pending_user_image = None
        self._direct_response_mode = False

    def save_session_log(self, session_id: str) -> None:
        safe_session_id = self._sanitize_session_id(session_id)
        if not safe_session_id:
            return
        try:
            os.makedirs(SESSION_LOG_DIR, exist_ok=True)
            filepath = os.path.join(SESSION_LOG_DIR, f"{safe_session_id}.json")
            with self._summary_lock:
                data = {
                    "session_log": list(self.session_log),
                    "session_step_offset": int(self.session_step_offset),
                    "last_goal_success": self._last_goal_success,
                }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[SessionLog] Saved {safe_session_id} ({len(data['session_log'])} entries)", flush=True)
        except Exception as e:
            print(f"[SessionLog] Error saving {safe_session_id}: {e}", flush=True)

    def load_session_log(self, session_id: str) -> None:
        safe_session_id = self._sanitize_session_id(session_id)
        if not safe_session_id:
            return
        filepath = os.path.join(SESSION_LOG_DIR, f"{safe_session_id}.json")
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.session_log = list(data.get("session_log", []))
            self.session_step_offset = int(data.get("session_step_offset", 0) or 0)
            self._last_goal_success = data.get("last_goal_success")
            print(
                f"[SessionLog] Loaded {safe_session_id} "
                f"({len(self.session_log)} entries, offset={self.session_step_offset})",
                flush=True,
            )
        except Exception as e:
            print(f"[SessionLog] Error loading {safe_session_id}: {e}", flush=True)

    def attach_session(self, session_id: str, force_reload: bool = False) -> None:
        safe_session_id = self._sanitize_session_id(session_id)
        if not safe_session_id:
            return
        if not force_reload and safe_session_id == self._current_session_id:
            return

        previous_session = self._current_session_id
        if previous_session:
            snapshot = self._snapshot_progress_log(flush=True)
            if snapshot:
                self._append_progress_snapshot_to_session(
                    self.goal_text or "Interrupted task",
                    snapshot,
                    self._last_goal_success,
                    self.step_count,
                    summary_text="Session switched before a clean task teardown.",
                )
            self.save_session_log(previous_session)

        self._current_session_id = safe_session_id
        self.reset_runtime_session_state()
        self.load_session_log(safe_session_id)

    def execute_goal(self, goal: str, stop_event: Optional[threading.Event] = None) -> Generator[str, None, Dict[str, Any]]:
        """Run the top-level planner loop for one user goal.

        The planner receives the system prompt plus session context, returns one
        JSON action per step, and the large action dispatch block below executes
        that action using local tools, the computer-use subloop, or file/system
        helpers.
        """
        self._active_stop_event = stop_event

        if not self.client:
            yield "❌ OpenAI client not available\n"
            self._active_stop_event = None
            return {"success": False, "reason": "No OpenAI client"}

        # Rebuild the planner prompt from scratch for every task. The planner is
        # stateless between tasks except for explicit session/task history that
        # we choose to inject here.
        system_prompt = SYSTEM_PROMPT.replace("{memory_context}", "")
        system_prompt += "\n\n" + self._temporal_context_text()
        system_prompt += "\n\n" + self._scheduled_tasks_context_text()

        if not STRICT_CHAT_ISOLATION:
            # Inject last 2 successful task summaries for inspiration (read-only reference)
            try:
                if os.path.exists(TASK_SUMMARIES_FILE):
                    with open(TASK_SUMMARIES_FILE, "r") as f:
                        past_tasks = json.load(f)
                    successful = [t for t in past_tasks if t.get("success")][-2:]
                    if successful:
                        ref_lines = []
                        for t in successful:
                            plog = t.get("progress_log", [])
                            plog_text = "; ".join(plog[-5:]) if plog else t.get("summary", "")
                            ref_lines.append(f"- Goal: \"{t.get('goal', '')[:80]}\" → {plog_text[:200]}")
                        system_prompt += (
                            "\n\nPrevious successful tasks (for reference only, do NOT reuse their files):\n"
                            + "\n".join(ref_lines)
                        )
            except Exception:
                pass  # Non-critical, skip silently

        previous_progress = self._snapshot_progress_log(flush=True)
        if previous_progress:
            self._append_progress_snapshot_to_session(
                self.goal_text or "Previous task",
                previous_progress,
                self._last_goal_success,
                self.step_count,
                summary_text="Previous task context archived before starting a new goal.",
            )

        pending_image = self._prepare_pending_user_image(self._pending_user_image)
        if pending_image:
            goal_content = (
                f"GOAL: {goal}\n\n"
                "A user-supplied image is attached to this message. Inspect that attachment directly if the request refers to it."
            )
        else:
            goal_content = f"GOAL: {goal}"
        image_data = str((pending_image or {}).get("data") or "").strip()
        image_mime = str((pending_image or {}).get("mime") or "").strip().lower()
        image_detail = str((pending_image or {}).get("detail") or USER_IMAGE_DETAIL).strip().lower()
        if image_data and image_mime.startswith("image/"):
            image_url = {"url": f"data:{image_mime};base64,{image_data}"}
            if image_detail:
                image_url["detail"] = image_detail
            user_content: Any = [
                {"type": "text", "text": goal_content},
                {"type": "image_url", "image_url": image_url},
            ]
        else:
            user_content = goal_content

        # Initialize conversation
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        self._reset_task_usage()
        self._pending_user_image = None
        self.vision_calls = 0
        self.current_elements = {}
        self.progress_log = []  # Reset progress log for new task
        self.goal_text = goal
        self.goal_lower = goal.lower()
        # Decide whether this task can be answered immediately without entering
        # the expensive multi-step planner loop. If this probe succeeds we stop
        # early and never enter the agentic loop below.
        self._direct_response_mode = self._should_try_direct_response(bool(pending_image))
        self._last_goal_success = None
        self.target_focus_keywords = self._determine_target_focus_keywords()
        self.decision_model = self._normalize_supported_model(self.openai_model)
        self.vision_model = self.decision_model
        self.last_action_signature = ""
        self.last_action_streak = 0
        self.command_failures = {}
        self.current_frame_id = ""
        self.frame_element_cache = {}
        self.frame_cache_order = []
        self.verifier.reset()
        self.last_screen_signals = {}
        self.input_backend = self.executor.backend_name
        self.input_device = self.executor.backend
        self.active_profile = self.profile_manager.select_profile(self.goal_lower)
        profile_id = self.active_profile.get("id", "none") if self.active_profile else "none"
        profile_patterns = self.profile_manager.get_ui(
            self.active_profile,
            "send_confirmation_patterns",
            [],
        )
        self.verifier.set_send_confirmation_patterns(profile_patterns)

        print(f"\n{'='*60}")
        print(f"[Brain] 🎯 New goal: {goal}")
        print(
            f"[Brain] Session context: {len(self.session_log)} persisted entries loaded "
            f"(strict_isolation={STRICT_CHAT_ISOLATION})"
        )
        print(f"[Brain] Profile: {profile_id}")
        print(f"{'='*60}")

        yield self._log_chunk(f"🎯 Goal: {goal}\n")
        yield self._log_chunk(f"🧩 Active profile: {profile_id}\n")
        yield self._log_chunk(
            f"🧠 Decision model: {self.decision_model} "
            f"(vision: {self.vision_model})\n"
        )
        yield self._log_chunk(f"🖥️ UI engine: {self.vision_model} native computer use\n")
        yield self._log_chunk(
            f"🖱️ Input backend: {self.input_backend} (caps: {self.executor.capabilities})\n"
        )
        if self._direct_response_mode:
            yield self._log_chunk("💬 Direct response probe enabled for this goal.\n")
        else:
            if self.max_steps > 0:
                yield self._log_chunk(f"🔄 Starting agentic loop (max {self.max_steps} steps)...\n\n")
            else:
                yield self._log_chunk("🔄 Starting agentic loop (no hard step limit)...\n\n")
        self._trace("goal_start", {
            "goal": goal,
            "profile_id": profile_id,
            "input_backend": self.input_backend,
            "executor_caps": self.executor.capabilities,
            "decision_model": self.decision_model,
            "autonomous_helpers": self.allow_autonomous_helpers,
        })

        result = {"success": False, "reason": "Unknown", "steps": 0}

        # Fast path: some goals can be answered directly from the current
        # prompt/context without planning or computer use.
        try:
            direct_result = self._try_direct_response_once()
        except TaskBudgetExceeded as exc:
            direct_result = {
                "success": False,
                "reason": str(exc),
                "summary": str(exc),
                "steps": 0,
            }
        if direct_result is not None:
            summary = direct_result.get("summary", direct_result.get("reason", "Task completed"))
            if direct_result.get("success", False):
                print(f"[Brain] 💬 DIRECT ANSWER: {summary}", flush=True)
                yield f"{summary}\n"
                self._trace("goal_done", {"summary": summary, "step": 0, "path": "direct_probe"})
            else:
                print(f"[Brain] ❌ DIRECT ANSWER FAILED: {summary}", flush=True)
                yield f"❌ {summary}\n"
                self._trace("goal_failed", {"reason": summary, "step": 0, "path": "direct_probe"})
            result = direct_result
            self._last_goal_success = result.get("success", False)
            progress_snapshot = self._snapshot_progress_log(flush=True)
            self.memory.add_exchange(
                user_goal=goal,
                result_summary=summary,
                steps=result.get("steps", 0),
                success=result.get("success", False)
            )
            self._save_task_summary_async(goal, summary, result, progress_snapshot)
            self._append_progress_snapshot_to_session(
                goal,
                progress_snapshot,
                self._last_goal_success,
                result.get("steps", 0),
                summary_text=summary,
            )
            self.save_session_log(self._current_session_id)
            self._finalize_task_usage(result)
            self._trace("goal_end", {"result": result, "vision_calls": self.vision_calls})
            yield self._log_chunk("\n🧾 Session state saved.\n")
            self._active_stop_event = None
            return result

        self._direct_response_mode = False

        # === THE AGENTIC LOOP ===
        step = 0
        while True:
            step += 1
            if self.max_steps > 0 and step > self.max_steps:
                print(f"[Brain] ⏱️ Max steps ({self.max_steps}) reached", flush=True)
                yield f"⏱️ Max steps ({self.max_steps}) reached\n"
                self._trace("goal_max_steps", {"max_steps": self.max_steps})
                result = {"success": False, "reason": "Max steps reached", "steps": self.max_steps}
                break
            if self._budget_limit_reached():
                yield f"💸 {self._budget_failure_reason()}\n"
                result = {"success": False, "reason": self._budget_failure_reason(), "steps": max(step - 1, 0)}
                break
            if self._stop_requested():
                yield "⛔ STOP: execution interrupted by user.\n"
                result = {"success": False, "reason": "Stopped by user", "steps": max(step - 1, 0), "stopped": True}
                break
            self._wait_if_paused()
            self.step_count = step
            global_step = self.session_step_offset + step
            step_label = f"task step {step}/{self.max_steps}" if self.max_steps > 0 else f"task step {step}"
            print(
                f"\n━━━ BRAIN Step {global_step} ({step_label}) ━━━",
                flush=True,
            )
            yield self._log_chunk(f"━━━ Step {global_step} ({step_label}) ━━━\n")
            self._trace("step_start", {"step": step, "max_steps": self.max_steps if self.max_steps > 0 else None})
            injected_messages = self._inject_remote_messages()
            for injected_text in injected_messages:
                print(f"[Brain] 📩 Remote operator message: {injected_text}", flush=True)
                yield self._log_chunk(f"[task] Remote operator message: {injected_text}\n")

            try:
                # 1. THINK — Ask GPT what to do next
                model_messages = self._messages_for_model()
                response = self._chat_completion(
                    model=self.decision_model,
                    messages=model_messages,
                    temperature=OPENAI_TEMPERATURE,
                    max_completion_tokens=100000,
                    source="planner",
                )

                raw_content = response.choices[0].message.content
                finish_reason = response.choices[0].finish_reason
                if self._stop_requested():
                    yield "⛔ STOP: execution interrupted by user.\n"
                    result = {"success": False, "reason": "Stopped by user", "steps": max(step - 1, 0), "stopped": True}
                    break
                if not raw_content:
                    refusal = getattr(response.choices[0].message, "refusal", None)
                    print(f"[Brain] ⚠️ EMPTY LLM response! finish_reason={finish_reason}, refusal={refusal}", flush=True)
                    yield self._log_chunk(
                        f"⚠️ LLM returned empty response (finish_reason={finish_reason})\n"
                    )
                    self.messages.append({"role": "assistant", "content": "..."})
                    continue
                assistant_text = raw_content.strip()
                if finish_reason and finish_reason != "stop":
                    print(f"[Brain] ⚠️ finish_reason={finish_reason} (not 'stop')", flush=True)
                # 2. PARSE — turn the planner JSON into one executable action.
                action = self._parse_action(assistant_text)
                if self._stop_requested():
                    yield "⛔ STOP: execution interrupted by user.\n"
                    result = {"success": False, "reason": "Stopped by user", "steps": max(step - 1, 0), "stopped": True}
                    break

                if action:
                    thought = action.get("thought", "")
                    progress = action.get("progress_log", "")
                    if thought:
                        print(f"\n[🧠 THOUGHT] {thought}", flush=True)
                        yield f"🧠 Thought: {thought}\n"
                    if progress:
                        print(f"[📈 PROGRESS] {progress}", flush=True)
                        yield f"📈 Progress: {progress}\n"
                if action is None:
                    self.messages.append({"role": "assistant", "content": assistant_text[:500]})
                    print(f"\n\n[👁️ RAW LLM OUTPUT STEP]\n{assistant_text}\n[END RAW]\n\n", flush=True)
                    yield f"⚠️ Could not parse action: {assistant_text[:200]}\n"
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "ERROR: Invalid response. Respond with ONLY one JSON object using mandatory thought+action format. "
                            "Example: {\"thought\": \"I will list files\", \"action\": \"execute\", \"command\": \"ls\"}"
                        )
                    })
                    continue
                self.messages.append({
                    "role": "assistant",
                    "content": json.dumps(action, ensure_ascii=False),
                })

                action_type = action.get("action")
                action_signature = self._action_signature(action)
                thought_text = str(action.get("thought", "")).strip()
                if thought_text:
                    print(f"[Brain] 🤔 Thought: {thought_text}", flush=True)
                self._trace("action_plan", {"action": action})

                if self._is_action_repetition_blocked(action_signature):
                    observation = (
                        f"ERROR: Repeated action blocked after {self.max_same_action_streak} tries: {action_signature}. "
                        "Use a different strategy (focus correct window, switch perception layer, or choose another action)."
                    )
                    yield f"⚠️ Repetition blocked: {action_signature}\n"
                    self.messages.append({
                        "role": "user",
                        "content": f"OBSERVATION (step {step}):\n{observation}\n\nContinue toward the goal with a different action."
                    })
                    continue

                blind_restart_reason = self._should_block_blind_restart(action)
                if blind_restart_reason:
                    observation = f"ERROR: {blind_restart_reason}"
                    yield f"⚠️ Restart blocked: {blind_restart_reason}\n"
                    self.messages.append({
                        "role": "user",
                        "content": (
                            f"OBSERVATION (step {step}):\n{observation}\n\n"
                            "Continue from the current screen/state. Local corrections are allowed, but blind full restarts are not."
                        ),
                    })
                    continue

                # 3. CHECK COMPLETION — completion/scheduling/respond actions
                # exit the loop here; only operational actions continue into the
                # dispatch block below.
                if action_type == "done":
                    screen_truth_reason = self._screen_truth_block_reason(
                        str(self.verifier.last_screen_text or ""),
                        str(action.get("summary") or ""),
                        require_claim=False,
                    )
                    if screen_truth_reason:
                        observation = (
                            "ERROR: Cannot mark DONE yet. The visible screen still contradicts completion. "
                            f"Reason: {screen_truth_reason}"
                        )
                        yield f"⚠️ DONE blocked: {screen_truth_reason}\n"
                        self._trace("done_blocked", {"reason": screen_truth_reason, "source": "screen_truth"})
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "Trust the visible screen over the computer-use summary and continue from the current state."
                            ),
                        })
                        continue
                    can_done, done_reason = self.verifier.can_mark_done(self.goal_lower)
                    if not can_done:
                        observation = (
                            "ERROR: Cannot mark DONE yet. Verification engine has insufficient proof. "
                            f"Reason: {done_reason}"
                        )
                        yield f"⚠️ DONE blocked: {done_reason}\n"
                        self._trace("done_blocked", {"reason": done_reason})
                        self.messages.append({
                            "role": "user",
                            "content": f"OBSERVATION (step {step}):\n{observation}\n\nContinue toward the goal with objective verification."
                        })
                        continue
                    summary = action.get("summary", "Task completed")
                    print(f"[Brain] ✅ DONE: {summary}", flush=True)
                    yield f"✅ DONE: {summary}\n"
                    self._trace("goal_done", {"summary": summary, "step": step})
                    result = {"success": True, "summary": summary, "steps": step}
                    break

                if action_type == "failed":
                    reason = action.get("reason", "Unknown")
                    print(f"[Brain] ❌ FAILED: {reason}", flush=True)
                    yield f"❌ FAILED: {reason}\n"
                    self._trace("goal_failed", {"reason": reason, "step": step})
                    result = {"success": False, "reason": reason, "steps": step}
                    break

                if action_type in {"respond", "reply"}:
                    response_text = str(
                        action.get("text")
                        or action.get("response")
                        or action.get("message")
                        or action.get("summary")
                        or ""
                    ).strip()
                    if not response_text:
                        observation = "ERROR: respond action requires non-empty text."
                        yield "❌ respond action missing text\n"
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "Return one JSON object with a non-empty text field."
                            ),
                        })
                        continue
                    print(f"[Brain] 💬 RESPOND: {response_text}", flush=True)
                    yield f"{response_text}\n"
                    self._trace("goal_done", {"summary": response_text, "step": step, "path": "respond"})
                    result = {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": step,
                    }
                    break

                if action_type == "schedule_task":
                    schedule_payload, schedule_error = self._normalize_schedule_action(action)
                    if not schedule_payload:
                        observation = f"ERROR: {schedule_error}"
                        yield f"❌ {schedule_error}\n"
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "If this is a future task, either ask a clarification question in the user's language "
                                "or return schedule_task with a valid ISO-8601 run_at in the future."
                            ),
                        })
                        continue
                    response_text = str(
                        action.get("message")
                        or action.get("text")
                        or action.get("response")
                        or action.get("summary")
                        or ""
                    ).strip()
                    if not response_text:
                        response_text = (
                            f"Scheduled for {schedule_payload['runAt']} ({schedule_payload['timezone']})."
                        )
                    print(f"[Brain] 🗓️ SCHEDULE_TASK: {schedule_payload['runAt']}", flush=True)
                    yield f"{response_text}\n"
                    self._trace("goal_done", {"summary": response_text, "step": step, "path": "schedule_task"})
                    result = {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": step,
                        "deferredTask": schedule_payload,
                    }
                    break

                if action_type in {"cancel_scheduled_task", "delete_scheduled_task"}:
                    mutation_payload, mutation_error = self._normalize_scheduled_task_mutation(action, op="cancel")
                    if not mutation_payload:
                        observation = f"ERROR: {mutation_error}"
                        yield f"❌ {mutation_error}\n"
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "If the target future task is ambiguous, ask a clarification question in the user's language. "
                                "Otherwise return cancel_scheduled_task with the exact task_id."
                            ),
                        })
                        continue
                    response_text = str(
                        action.get("message")
                        or action.get("text")
                        or action.get("response")
                        or action.get("summary")
                        or "Okay — I cancelled that future task."
                    ).strip()
                    print(f"[Brain] 🗓️ CANCEL_SCHEDULED_TASK: {mutation_payload['taskId']}", flush=True)
                    yield f"{response_text}\n"
                    self._trace("goal_done", {"summary": response_text, "step": step, "path": "cancel_scheduled_task"})
                    result = {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": step,
                        "scheduledTaskMutation": mutation_payload,
                    }
                    break

                if action_type in {"update_scheduled_task", "edit_scheduled_task", "reschedule_scheduled_task"}:
                    mutation_payload, mutation_error = self._normalize_scheduled_task_mutation(action, op="update")
                    if not mutation_payload:
                        observation = f"ERROR: {mutation_error}"
                        yield f"❌ {mutation_error}\n"
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "If the target future task is ambiguous, ask a clarification question in the user's language. "
                                "Otherwise return update_scheduled_task with the exact task_id and the updated fields."
                            ),
                        })
                        continue
                    response_text = str(
                        action.get("message")
                        or action.get("text")
                        or action.get("response")
                        or action.get("summary")
                        or "Okay — I updated that future task."
                    ).strip()
                    print(f"[Brain] 🗓️ UPDATE_SCHEDULED_TASK: {mutation_payload['taskId']}", flush=True)
                    yield f"{response_text}\n"
                    self._trace("goal_done", {"summary": response_text, "step": step, "path": "update_scheduled_task"})
                    result = {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": step,
                        "scheduledTaskMutation": mutation_payload,
                    }
                    break

                if action_type in {"replace_scheduled_task", "swap_scheduled_task"}:
                    mutation_payload, mutation_error = self._normalize_replace_scheduled_task_action(action)
                    if not mutation_payload:
                        observation = f"ERROR: {mutation_error}"
                        yield f"❌ {mutation_error}\n"
                        self.messages.append({
                            "role": "user",
                            "content": (
                                f"OBSERVATION (step {step}):\n{observation}\n\n"
                                "If the target future task is ambiguous, ask a clarification question in the user's language. "
                                "Otherwise return replace_scheduled_task with the exact task_id, the new goal, and a valid ISO-8601 run_at in the future."
                            ),
                        })
                        continue
                    response_text = str(
                        action.get("message")
                        or action.get("text")
                        or action.get("response")
                        or action.get("summary")
                        or "Okay — I replaced that future task."
                    ).strip()
                    print(f"[Brain] 🗓️ REPLACE_SCHEDULED_TASK: {mutation_payload['taskId']}", flush=True)
                    yield f"{response_text}\n"
                    self._trace("goal_done", {"summary": response_text, "step": step, "path": "replace_scheduled_task"})
                    result = {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": step,
                        "scheduledTaskMutation": mutation_payload,
                    }
                    break

                # 4. EXECUTE ACTION
                print(f"[Brain] 🔧 Action: {action_type}", flush=True)
                yield f"🔧 Action: {action_type}"
                self._record_tool_usage(action_type)
                observation = ""

                # The branches below are the planner-visible action surface.
                # They define what the JSON planner can actually do in the
                # public runtime: non-visual shell work, computer-use UI work,
                # file helpers, desktop helpers, and dev/system utilities.

                # ---- EXECUTE COMMAND ----
                if action_type == "execute":
                    command = action.get("command", "")
                    print(f"[Brain]   → {command}", flush=True)
                    yield f" → `{command}`\n"
                    blocked_reason = self._should_block_execute(command)
                    if blocked_reason:
                        observation = f"ERROR: {blocked_reason}"
                        yield self._log_chunk(f"⚠️ Execute blocked: {blocked_reason}\n")
                    else:
                        if self.remote_workspace is not None:
                            observation = self.remote_workspace.execute_command(command)
                        else:
                            observation = self._execute_command(command)
                        self.verifier.record_command(observation)
                        self._trace("execute_unrestricted", {"command": command})
                        yield self._log_chunk(f"📋 Output:\n```\n{observation[:2000]}\n```\n\n")
                        direct_response = self._extract_direct_response_from_execute(command, observation)
                        if direct_response:
                            print(f"[Brain] 💬 RESPOND via execute: {direct_response}", flush=True)
                            yield f"{direct_response}\n"
                            self._trace(
                                "goal_done",
                                {"summary": direct_response, "step": step, "path": "execute_direct_response"},
                            )
                            result = {
                                "success": True,
                                "summary": direct_response,
                                "response_text": direct_response,
                                "steps": step,
                            }
                            break

                # ---- LEGACY UI ACTIONS DISABLED ----
                elif action_type in LEGACY_UI_ACTIONS:
                    observation = (
                        f"ERROR: Legacy UI action '{action_type}' is disabled in GPT-5.4 "
                        "computer-use mode. Use the computer action instead."
                    )
                    print(f"[Brain]   → blocked legacy UI action: {action_type}", flush=True)
                    yield self._log_chunk(
                        f"⚠️ Legacy UI action blocked: {action_type}. Use computer.\n"
                    )

                # ---- NATIVE COMPUTER USE ----
                elif action_type == "computer":
                    instruction = str(
                        action.get("instruction")
                        or action.get("goal")
                        or action.get("query")
                        or self.goal_text
                    ).strip()
                    print(f"[Brain]   → computer: {instruction[:160]}", flush=True)
                    yield self._log_chunk(f" → computer: {instruction[:160]}\n")
                    blocked_reason = self._should_use_specialized_tool_before_computer(instruction)
                    if blocked_reason:
                        observation = f"ERROR: {blocked_reason}"
                        yield self._log_chunk(f"⚠️ Computer blocked: {blocked_reason}\n")
                    else:
                        computer_result = self._run_computer_use(instruction)
                        observation = str(computer_result.get("observation") or "").strip()
                        summary = str(computer_result.get("summary") or "").strip()
                        screen_truth_reason = self._screen_truth_block_reason(
                            observation,
                            summary,
                            require_claim=True,
                        )
                        if screen_truth_reason:
                            if summary:
                                yield self._log_chunk(f"⚠️ Screen truth override: {screen_truth_reason}\n")
                            summary = ""
                            observation = (
                                f"ERROR: Screen truth override: {screen_truth_reason}\n\n"
                                + observation
                            )
                        if summary:
                            yield self._log_chunk(f"🖥️ Computer use summary: {summary[:400]}\n")
                        if observation:
                            yield self._log_chunk(
                                f"🖥️ Computer use observation:\n```\n{observation[:2000]}\n```\n\n"
                            )
                        if computer_result.get("stopped"):
                            yield "⛔ STOP: execution interrupted by user.\n"
                            result = {
                                "success": False,
                                "reason": "Stopped by user",
                                "steps": step,
                                "stopped": True,
                            }
                            break

                # Specialized local helpers start here. These are intended for
                # file/system/navigation work that does not require visual UI
                # interaction inside a live window.

                # ---- WAIT ----
                elif action_type == "wait":
                    seconds = min(action.get("seconds", 2), 10)
                    print(f"[Brain]   → Wait {seconds}s", flush=True)
                    yield self._log_chunk(f" → {seconds}s\n")
                    time.sleep(seconds)
                    observation = f"Waited {seconds} seconds"
                    yield self._log_chunk("✓ Waited\n\n")

                # ---- WRITE FILE ----
                elif action_type == "write_file":
                    path = action.get("path", "")
                    content = action.get("content", "")
                    print(f"[Brain]   → Write file: {path}", flush=True)
                    yield self._log_chunk(f" → write_file: {path}\n")
                    if self.remote_workspace is not None:
                        ok, detail, resolved_path = self.remote_workspace.write_file(path, content)
                        actual_path = str(resolved_path or path)
                        observation = detail
                        if ok:
                            yield self._log_chunk(f"✓ File written: {actual_path}\n\n")
                        else:
                            yield f"❌ Write failed: {detail}\n\n"
                    else:
                        try:
                            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                            with open(path, "w") as f:
                                f.write(content)
                            observation = f"Successfully wrote {len(content)} chars to {path}"
                            yield self._log_chunk(f"✓ File written: {path}\n\n")
                        except Exception as e:
                            observation = f"ERROR writing file {path}: {e}"
                            yield f"❌ Write failed: {e}\n\n"

                # ---- READ FILE ----
                elif action_type == "read_file":
                    path = action.get("path", "")
                    start_line = action.get("start", 1)
                    end_line = action.get("end", start_line + 50)
                    print(f"[Brain]   → Read file: {path} (lines {start_line}-{end_line})", flush=True)
                    yield self._log_chunk(f" → read_file: {path}\n")
                    if self.remote_workspace is not None:
                        ok, detail, payload = self.remote_workspace.read_file(path, start_line, end_line)
                        if ok:
                            actual_path = str(payload.get("path") or path)
                            total = int(payload.get("total") or 0)
                            actual_start = int(payload.get("start") or start_line)
                            actual_end = int(payload.get("end") or start_line)
                            snippet = str(payload.get("snippet") or "")
                            observation = f"FILE {actual_path} (Lines {actual_start} to {actual_end} of {total}):\n```\n{snippet}\n```"
                            yield self._log_chunk(f"✓ Read {max(0, actual_end - actual_start + 1)} lines from {actual_path}\n\n")
                        else:
                            observation = detail
                            yield f"❌ Read failed: {detail}\n\n"
                    else:
                        try:
                            with open(path, "r") as f:
                                lines = f.readlines()
                            total = len(lines)
                            sl = max(0, start_line - 1)
                            el = min(total, end_line)
                            snippet = "".join(lines[sl:el])
                            observation = f"FILE {path} (Lines {sl+1} to {el} of {total}):\n```\n{snippet}\n```"
                            yield self._log_chunk(f"✓ Read {el-sl} lines from {path}\n\n")
                        except Exception as e:
                            observation = f"ERROR reading file {path}: {e}"
                            yield f"❌ Read failed: {e}\n\n"

                # ---- LIST DIR ----
                elif action_type == "list_dir":
                    path = action.get("path", ".")
                    print(f"[Brain]   → List dir: {path}", flush=True)
                    yield self._log_chunk(f" → list_dir: {path}\n")
                    if self.remote_workspace is not None:
                        ok, detail, payload = self.remote_workspace.list_dir(path)
                        if ok:
                            actual_path = str(payload.get("path") or path)
                            details = []
                            for item in payload.get("items") or []:
                                if str(item.get("type") or "") == "dir":
                                    details.append(f"[DIR]  {item.get('name')}")
                                else:
                                    details.append(f"[FILE] {item.get('name')} ({int(item.get('size') or 0)} bytes)")
                            observation = f"DIRECTORY {actual_path}:\n" + "\n".join(details)
                            yield self._log_chunk(f"✓ Listed {len(payload.get('items') or [])} items in {actual_path}\n\n")
                        else:
                            observation = detail
                            yield f"❌ List dir failed: {detail}\n\n"
                    else:
                        try:
                            items = os.listdir(path)
                            details = []
                            for item in items:
                                fp = os.path.join(path, item)
                                if os.path.isdir(fp):
                                    details.append(f"[DIR]  {item}")
                                else:
                                    size = os.path.getsize(fp)
                                    details.append(f"[FILE] {item} ({size} bytes)")
                            observation = f"DIRECTORY {path}:\n" + "\n".join(details)
                            yield self._log_chunk(f"✓ Listed {len(items)} items in {path}\n\n")
                        except Exception as e:
                            observation = f"ERROR listing directory {path}: {e}"
                            yield f"❌ List dir failed: {e}\n\n"

                # ---- OPEN FILE ----
                elif action_type == "open_file":
                    path = action.get("path", "")
                    print(f"[Brain]   → Open file: {path}", flush=True)
                    yield self._log_chunk(f" → open_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "open_file"):
                        ok, detail, payload = self.remote_workspace.open_file(path)
                    else:
                        ok, detail, payload = self._open_file_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ open_file failed: {observation}\n\n"

                # ---- OPEN URL ----
                elif action_type == "open_url":
                    url = action.get("url", "")
                    print(f"[Brain]   → Open URL: {url}", flush=True)
                    yield self._log_chunk(f" → open_url: {url}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "open_url"):
                        ok, detail, payload = self.remote_workspace.open_url(url)
                    else:
                        ok, detail, payload = self._open_url_local(url)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ open_url failed: {observation}\n\n"

                # ---- REVEAL PATH ----
                elif action_type == "reveal_path":
                    path = action.get("path", "")
                    print(f"[Brain]   → Reveal path: {path}", flush=True)
                    yield self._log_chunk(f" → reveal_path: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "reveal_path"):
                        ok, detail, payload = self.remote_workspace.reveal_path(path)
                    else:
                        ok, detail, payload = self._reveal_path_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ reveal_path failed: {observation}\n\n"

                # ---- CLIPBOARD ----
                elif action_type == "copy_to_clipboard":
                    text = action.get("text", "")
                    print(f"[Brain]   → Copy to clipboard ({len(str(text or ''))} chars)", flush=True)
                    yield self._log_chunk(" → copy_to_clipboard\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "copy_to_clipboard"):
                        ok, detail, payload = self.remote_workspace.copy_to_clipboard(text)
                    else:
                        ok, detail, payload = self._copy_to_clipboard_local(text)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ copy_to_clipboard failed: {observation}\n\n"

                elif action_type == "paste":
                    print("[Brain]   → Paste clipboard", flush=True)
                    yield self._log_chunk(" → paste\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "paste"):
                        ok, detail, payload = self.remote_workspace.paste()
                    else:
                        ok, detail, payload = self._paste_clipboard_local()
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ paste failed: {observation}\n\n"

                elif action_type == "read_clipboard":
                    print("[Brain]   → Read clipboard", flush=True)
                    yield self._log_chunk(" → read_clipboard\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_clipboard"):
                        ok, detail, payload = self.remote_workspace.read_clipboard()
                    else:
                        ok, detail, payload = self._read_clipboard_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        clipboard_text = str(payload.get("text") or "")
                        observation = f"{observation}\nCLIPBOARD:\n```\n{clipboard_text[:4000]}\n```"
                        yield self._log_chunk(f"✓ Read clipboard ({len(clipboard_text)} chars)\n\n")
                    else:
                        yield f"❌ read_clipboard failed: {observation}\n\n"

                elif action_type == "focus_window":
                    query = action.get("app_or_title") or action.get("title") or action.get("app") or ""
                    print(f"[Brain]   → Focus window: {query}", flush=True)
                    yield self._log_chunk(f" → focus_window: {query}\n")
                    blocked_reason = self._should_block_specialized_tool(action_type, action)
                    if blocked_reason:
                        ok, detail, payload = False, blocked_reason, {}
                    elif self.remote_workspace is not None and hasattr(self.remote_workspace, "focus_window"):
                        ok, detail, payload = self.remote_workspace.focus_window(query)
                    else:
                        ok, detail, payload = self._focus_window_local(query)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ focus_window failed: {observation}\n\n"

                elif action_type == "open_app":
                    app_name = action.get("app_name") or action.get("app") or ""
                    print(f"[Brain]   → Open app: {app_name}", flush=True)
                    yield self._log_chunk(f" → open_app: {app_name}\n")
                    blocked_reason = self._should_block_specialized_tool(action_type, action)
                    if blocked_reason:
                        ok, detail, payload = False, blocked_reason, {}
                    elif self.remote_workspace is not None and hasattr(self.remote_workspace, "open_app"):
                        ok, detail, payload = self.remote_workspace.open_app(app_name)
                    else:
                        ok, detail, payload = self._open_app_local(app_name)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ open_app failed: {observation}\n\n"

                elif action_type == "search_files":
                    pattern = action.get("pattern", "")
                    root = action.get("root", "~")
                    print(f"[Brain]   → Search files: pattern='{pattern}' root='{root}'", flush=True)
                    yield self._log_chunk(f" → search_files: {pattern} @ {root}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "search_files"):
                        ok, detail, payload = self.remote_workspace.search_files(pattern, root)
                    else:
                        ok, detail, payload = self._search_files_local(pattern, root)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        matches = payload.get("matches") or []
                        if isinstance(matches, list):
                            observation += "\nMATCHES:\n" + "\n".join(str(item) for item in matches[:100])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ search_files failed: {observation}\n\n"

                elif action_type == "select_file_for_active_dialog":
                    path = action.get("path", "")
                    print(f"[Brain]   → Select file for active dialog: {path}", flush=True)
                    yield self._log_chunk(f" → select_file_for_active_dialog: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "select_file_for_active_dialog"):
                        ok, detail, payload = self.remote_workspace.select_file_for_active_dialog(path)
                    else:
                        ok, detail, payload = self._select_file_for_active_dialog_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ select_file_for_active_dialog failed: {observation}\n\n"

                elif action_type == "upload_file_to_active_app":
                    path = action.get("path", "")
                    print(f"[Brain]   → Upload file to active app: {path}", flush=True)
                    yield self._log_chunk(f" → upload_file_to_active_app: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "upload_file_to_active_app"):
                        ok, detail, payload = self.remote_workspace.upload_file_to_active_app(path)
                    else:
                        ok, detail, payload = self._upload_file_to_active_app_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ upload_file_to_active_app failed: {observation}\n\n"

                elif action_type == "drag_file_to_target":
                    path = action.get("path", "")
                    x = int(action.get("x") or 0)
                    y = int(action.get("y") or 0)
                    print(f"[Brain]   → Drag file to target: {path} -> ({x},{y})", flush=True)
                    yield self._log_chunk(f" → drag_file_to_target: {path} -> ({x},{y})\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "drag_file_to_target"):
                        ok, detail, payload = self.remote_workspace.drag_file_to_target(path, x, y)
                    else:
                        ok, detail, payload = self._drag_file_to_target_local(path, x, y)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ drag_file_to_target failed: {observation}\n\n"

                elif action_type == "list_windows":
                    print("[Brain]   → List windows", flush=True)
                    yield self._log_chunk(" → list_windows\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "list_windows"):
                        ok, detail, payload = self.remote_workspace.list_windows()
                    else:
                        ok, detail, payload = self._list_windows_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        windows = payload.get("windows") or []
                        if isinstance(windows, list):
                            lines = []
                            for item in windows[:100]:
                                if isinstance(item, dict):
                                    title = str(item.get("title") or "").strip()
                                    wm_class = str(item.get("wm_class") or "").strip()
                                    lines.append(f"{title} [{wm_class}]")
                            if lines:
                                observation += "\nWINDOWS:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ list_windows failed: {observation}\n\n"

                elif action_type == "make_dir":
                    path = action.get("path", "")
                    print(f"[Brain]   → Make directory: {path}", flush=True)
                    yield self._log_chunk(f" → make_dir: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "make_dir"):
                        ok, detail, payload = self.remote_workspace.make_dir(path)
                    else:
                        ok, detail, payload = self._make_dir_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ make_dir failed: {observation}\n\n"

                elif action_type == "move_path":
                    src = action.get("src", "")
                    dst = action.get("dst", "")
                    print(f"[Brain]   → Move path: {src} -> {dst}", flush=True)
                    yield self._log_chunk(f" → move_path: {src} -> {dst}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "move_path"):
                        ok, detail, payload = self.remote_workspace.move_path(src, dst)
                    else:
                        ok, detail, payload = self._move_path_local(src, dst)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ move_path failed: {observation}\n\n"

                elif action_type == "copy_path":
                    src = action.get("src", "")
                    dst = action.get("dst", "")
                    print(f"[Brain]   → Copy path: {src} -> {dst}", flush=True)
                    yield self._log_chunk(f" → copy_path: {src} -> {dst}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "copy_path"):
                        ok, detail, payload = self.remote_workspace.copy_path(src, dst)
                    else:
                        ok, detail, payload = self._copy_path_local(src, dst)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ copy_path failed: {observation}\n\n"

                elif action_type == "delete_path":
                    path = action.get("path", "")
                    print(f"[Brain]   → Delete path: {path}", flush=True)
                    yield self._log_chunk(f" → delete_path: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "delete_path"):
                        ok, detail, payload = self.remote_workspace.delete_path(path)
                    else:
                        ok, detail, payload = self._delete_path_local(path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ delete_path failed: {observation}\n\n"

                elif action_type == "append_file":
                    path = action.get("path", "")
                    content = action.get("content", "")
                    print(f"[Brain]   → Append file: {path}", flush=True)
                    yield self._log_chunk(f" → append_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "append_file"):
                        ok, detail, payload = self.remote_workspace.append_file(path, content)
                    else:
                        ok, detail, payload = self._append_file_local(path, content)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ append_file failed: {observation}\n\n"

                elif action_type == "replace_in_file":
                    path = action.get("path", "")
                    search = action.get("search", "")
                    replace = action.get("replace", "")
                    print(f"[Brain]   → Replace in file: {path}", flush=True)
                    yield self._log_chunk(f" → replace_in_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "replace_in_file"):
                        ok, detail, payload = self.remote_workspace.replace_in_file(path, search, replace)
                    else:
                        ok, detail, payload = self._replace_in_file_local(path, search, replace)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ replace_in_file failed: {observation}\n\n"

                elif action_type == "tail_file":
                    path = action.get("path", "")
                    lines = action.get("lines", 20)
                    print(f"[Brain]   → Tail file: {path}", flush=True)
                    yield self._log_chunk(f" → tail_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "tail_file"):
                        ok, detail, payload = self.remote_workspace.tail_file(path, int(lines or 20))
                    else:
                        ok, detail, payload = self._tail_file_local(path, int(lines or 20))
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        tail_content = str(payload.get("lines") or "")
                        observation += f"\nTAIL:\n```\n{tail_content[:4000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ tail_file failed: {observation}\n\n"

                elif action_type == "read_path":
                    path = action.get("path", "")
                    print(f"[Brain]   → Read path: {path}", flush=True)
                    yield self._log_chunk(f" → read_path: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_path"):
                        ok, detail, payload = self.remote_workspace.read_path(path)
                    else:
                        ok, detail, payload = self._read_path_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        if payload.get("kind") == "file":
                            content = str(payload.get("content") or "")
                            observation += f"\nCONTENT:\n```\n{content[:6000]}\n```"
                        elif payload.get("kind") == "directory":
                            items = payload.get("items") or []
                            if isinstance(items, list):
                                observation += "\nITEMS:\n" + "\n".join(str(item) for item in items[:200])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_path failed: {observation}\n\n"

                elif action_type == "open_with":
                    path = action.get("path", "")
                    app_name = action.get("app_name") or action.get("app") or ""
                    print(f"[Brain]   → Open with: {path} via {app_name}", flush=True)
                    yield self._log_chunk(f" → open_with: {path} via {app_name}\n")
                    blocked_reason = self._should_block_specialized_tool(action_type, action)
                    if blocked_reason:
                        ok, detail, payload = False, blocked_reason, {}
                    elif self.remote_workspace is not None and hasattr(self.remote_workspace, "open_with"):
                        ok, detail, payload = self.remote_workspace.open_with(path, app_name)
                    else:
                        ok, detail, payload = self._open_with_local(path, app_name)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ open_with failed: {observation}\n\n"

                elif action_type == "read_clipboard_image":
                    print("[Brain]   → Read clipboard image", flush=True)
                    yield self._log_chunk(" → read_clipboard_image\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_clipboard_image"):
                        ok, detail, payload = self.remote_workspace.read_clipboard_image()
                    else:
                        ok, detail, payload = self._read_clipboard_image_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        path = str(payload.get("path") or "")
                        mime = str(payload.get("mime") or "")
                        width = payload.get("width")
                        height = payload.get("height")
                        observation += f"\nIMAGE_PATH: {path}\nMIME: {mime}"
                        if width and height:
                            observation += f"\nSIZE: {width}x{height}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_clipboard_image failed: {observation}\n\n"

                elif action_type == "replace_regex_in_file":
                    path = action.get("path", "")
                    pattern = action.get("pattern", "")
                    replace = action.get("replace", "")
                    print(f"[Brain]   → Replace regex in file: {path}", flush=True)
                    yield self._log_chunk(f" → replace_regex_in_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "replace_regex_in_file"):
                        ok, detail, payload = self.remote_workspace.replace_regex_in_file(path, pattern, replace)
                    else:
                        ok, detail, payload = self._replace_regex_in_file_local(path, pattern, replace)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {observation}\n\n")
                    else:
                        yield f"❌ replace_regex_in_file failed: {observation}\n\n"

                elif action_type == "grep_in_files":
                    pattern = action.get("pattern", "")
                    root = action.get("root", "~")
                    print(f"[Brain]   → Grep in files: pattern='{pattern}' root='{root}'", flush=True)
                    yield self._log_chunk(f" → grep_in_files: {pattern} @ {root}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "grep_in_files"):
                        ok, detail, payload = self.remote_workspace.grep_in_files(pattern, root)
                    else:
                        ok, detail, payload = self._grep_in_files_local(pattern, root)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        matches = payload.get("matches") or []
                        if isinstance(matches, list):
                            lines = []
                            for item in matches[:100]:
                                if isinstance(item, dict):
                                    lines.append(f"{item.get('path')}:{item.get('line')}:{item.get('text')}")
                            if lines:
                                observation += "\nMATCHES:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ grep_in_files failed: {observation}\n\n"

                elif action_type == "stat_path":
                    path = action.get("path", "")
                    print(f"[Brain]   → Stat path: {path}", flush=True)
                    yield self._log_chunk(f" → stat_path: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "stat_path"):
                        ok, detail, payload = self.remote_workspace.stat_path(path)
                    else:
                        ok, detail, payload = self._stat_path_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nKIND: {payload.get('kind')}\nSIZE: {payload.get('size')}\nMODE: {payload.get('mode')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ stat_path failed: {observation}\n\n"

                elif action_type == "diff_paths":
                    left = action.get("a") or action.get("left") or ""
                    right = action.get("b") or action.get("right") or ""
                    print(f"[Brain]   → Diff paths: {left} vs {right}", flush=True)
                    yield self._log_chunk(f" → diff_paths: {left} vs {right}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "diff_paths"):
                        ok, detail, payload = self.remote_workspace.diff_paths(left, right)
                    else:
                        ok, detail, payload = self._diff_paths_local(left, right)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        if payload.get("kind") == "file":
                            observation += f"\nDIFF:\n```\n{str(payload.get('diff') or '')[:8000]}\n```"
                        else:
                            observation += f"\nLEFT_ONLY: {payload.get('left_only')}\nRIGHT_ONLY: {payload.get('right_only')}\nDIFF_FILES: {payload.get('diff_files')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ diff_paths failed: {observation}\n\n"

                elif action_type == "list_processes":
                    print("[Brain]   → List processes", flush=True)
                    yield self._log_chunk(" → list_processes\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "list_processes"):
                        ok, detail, payload = self.remote_workspace.list_processes()
                    else:
                        ok, detail, payload = self._list_processes_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        processes = payload.get("processes") or []
                        if isinstance(processes, list):
                            lines = [f"{p.get('pid')} {p.get('comm')} {str(p.get('args') or '')[:120]}" for p in processes[:100] if isinstance(p, dict)]
                            if lines:
                                observation += "\nPROCESSES:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ list_processes failed: {observation}\n\n"

                elif action_type == "kill_process":
                    target = action.get("pid_or_name") or action.get("target") or ""
                    print(f"[Brain]   → Kill process: {target}", flush=True)
                    yield self._log_chunk(f" → kill_process: {target}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "kill_process"):
                        ok, detail, payload = self.remote_workspace.kill_process(target)
                    else:
                        ok, detail, payload = self._kill_process_local(target)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ kill_process failed: {observation}\n\n"

                elif action_type == "chmod_path":
                    path = action.get("path", "")
                    mode = action.get("mode", "")
                    print(f"[Brain]   → Chmod path: {path} {mode}", flush=True)
                    yield self._log_chunk(f" → chmod_path: {path} {mode}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "chmod_path"):
                        ok, detail, payload = self.remote_workspace.chmod_path(path, mode)
                    else:
                        ok, detail, payload = self._chmod_path_local(path, mode)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ chmod_path failed: {observation}\n\n"

                elif action_type == "symlink_path":
                    src = action.get("src", "")
                    dst = action.get("dst", "")
                    print(f"[Brain]   → Symlink path: {src} -> {dst}", flush=True)
                    yield self._log_chunk(f" → symlink_path: {src} -> {dst}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "symlink_path"):
                        ok, detail, payload = self.remote_workspace.symlink_path(src, dst)
                    else:
                        ok, detail, payload = self._symlink_path_local(src, dst)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ symlink_path failed: {observation}\n\n"

                elif action_type == "unzip_path":
                    path = action.get("path", "")
                    dst = action.get("dst", "")
                    print(f"[Brain]   → Unzip path: {path} -> {dst}", flush=True)
                    yield self._log_chunk(f" → unzip_path: {path} -> {dst}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "unzip_path"):
                        ok, detail, payload = self.remote_workspace.unzip_path(path, dst)
                    else:
                        ok, detail, payload = self._unzip_path_local(path, dst)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ unzip_path failed: {observation}\n\n"

                elif action_type == "archive_path":
                    path = action.get("path", "")
                    dst = action.get("dst", "")
                    print(f"[Brain]   → Archive path: {path} -> {dst}", flush=True)
                    yield self._log_chunk(f" → archive_path: {path} -> {dst}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "archive_path"):
                        ok, detail, payload = self.remote_workspace.archive_path(path, dst)
                    else:
                        ok, detail, payload = self._archive_path_local(path, dst)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ archive_path failed: {observation}\n\n"

                elif action_type == "read_json":
                    path = action.get("path", "")
                    print(f"[Brain]   → Read JSON: {path}", flush=True)
                    yield self._log_chunk(f" → read_json: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_json"):
                        ok, detail, payload = self.remote_workspace.read_json(path)
                    else:
                        ok, detail, payload = self._read_json_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nJSON:\n```json\n{str(payload.get('preview') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_json failed: {observation}\n\n"

                elif action_type == "write_json":
                    path = action.get("path", "")
                    data = action.get("data")
                    print(f"[Brain]   → Write JSON: {path}", flush=True)
                    yield self._log_chunk(f" → write_json: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "write_json"):
                        ok, detail, payload = self.remote_workspace.write_json(path, data)
                    else:
                        ok, detail, payload = self._write_json_local(path, data)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ write_json failed: {observation}\n\n"

                elif action_type == "read_csv_preview":
                    path = action.get("path", "")
                    rows = action.get("rows", 10)
                    print(f"[Brain]   → Read CSV preview: {path}", flush=True)
                    yield self._log_chunk(f" → read_csv_preview: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_csv_preview"):
                        ok, detail, payload = self.remote_workspace.read_csv_preview(path, int(rows or 10))
                    else:
                        ok, detail, payload = self._read_csv_preview_local(path, int(rows or 10))
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        preview = payload.get("rows") or []
                        observation += "\nCSV_ROWS:\n" + "\n".join(str(row) for row in preview[:50])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_csv_preview failed: {observation}\n\n"

                elif action_type == "git_status":
                    repo = action.get("repo", "")
                    print(f"[Brain]   → Git status: {repo}", flush=True)
                    yield self._log_chunk(f" → git_status: {repo}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "git_status"):
                        ok, detail, payload = self.remote_workspace.git_status(repo)
                    else:
                        ok, detail, payload = self._git_status_local(repo)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nSTATUS:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ git_status failed: {observation}\n\n"

                elif action_type == "git_diff":
                    repo = action.get("repo", "")
                    path = action.get("path", "")
                    print(f"[Brain]   → Git diff: {repo} {path}", flush=True)
                    yield self._log_chunk(f" → git_diff: {repo} {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "git_diff"):
                        ok, detail, payload = self.remote_workspace.git_diff(repo, path)
                    else:
                        ok, detail, payload = self._git_diff_local(repo, path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nDIFF:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ git_diff failed: {observation}\n\n"

                elif action_type == "run_tests":
                    target = action.get("target", "")
                    print(f"[Brain]   → Run tests: {target}", flush=True)
                    yield self._log_chunk(f" → run_tests: {target}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "run_tests"):
                        ok, detail, payload = self.remote_workspace.run_tests(target)
                    else:
                        ok, detail, payload = self._run_tests_local(target)
                    observation = str(detail or "")
                    if isinstance(payload, dict):
                        observation += f"\nTEST_OUTPUT:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ run_tests failed: {observation}\n\n"

                elif action_type == "download_file":
                    url = action.get("url", "")
                    path = action.get("path", "")
                    print(f"[Brain]   → Download file: {url} -> {path}", flush=True)
                    yield self._log_chunk(f" → download_file: {url} -> {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "download_file"):
                        ok, detail, payload = self.remote_workspace.download_file(url, path)
                    else:
                        ok, detail, payload = self._download_file_local(url, path)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ download_file failed: {observation}\n\n"

                elif action_type == "extract_text_from_pdf":
                    path = action.get("path", "")
                    print(f"[Brain]   → Extract PDF text: {path}", flush=True)
                    yield self._log_chunk(f" → extract_text_from_pdf: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "extract_text_from_pdf"):
                        ok, detail, payload = self.remote_workspace.extract_text_from_pdf(path)
                    else:
                        ok, detail, payload = self._extract_text_from_pdf_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nPDF_TEXT:\n```\n{str(payload.get('text') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ extract_text_from_pdf failed: {observation}\n\n"

                elif action_type == "replace_block_in_file":
                    path = action.get("path", "")
                    start_marker = action.get("start_marker", "")
                    end_marker = action.get("end_marker", "")
                    content = action.get("content", "")
                    print(f"[Brain]   → Replace block in file: {path}", flush=True)
                    yield self._log_chunk(f" → replace_block_in_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "replace_block_in_file"):
                        ok, detail, payload = self.remote_workspace.replace_block_in_file(path, start_marker, end_marker, content)
                    else:
                        ok, detail, payload = self._replace_block_in_file_local(path, start_marker, end_marker, content)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ replace_block_in_file failed: {observation}\n\n"

                elif action_type == "grep_ast":
                    symbol = action.get("symbol", "")
                    root = action.get("root", "~")
                    print(f"[Brain]   → Grep AST: {symbol} @ {root}", flush=True)
                    yield self._log_chunk(f" → grep_ast: {symbol} @ {root}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "grep_ast"):
                        ok, detail, payload = self.remote_workspace.grep_ast(symbol, root)
                    else:
                        ok, detail, payload = self._grep_ast_local(symbol, root)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        matches = payload.get("matches") or []
                        if isinstance(matches, list):
                            lines = [f"{m.get('path')}:{m.get('line')}:{m.get('kind')}" for m in matches[:100] if isinstance(m, dict)]
                            if lines:
                                observation += "\nAST_MATCHES:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ grep_ast failed: {observation}\n\n"

                elif action_type == "read_yaml":
                    path = action.get("path", "")
                    print(f"[Brain]   → Read YAML: {path}", flush=True)
                    yield self._log_chunk(f" → read_yaml: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_yaml"):
                        ok, detail, payload = self.remote_workspace.read_yaml(path)
                    else:
                        ok, detail, payload = self._read_yaml_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nYAML:\n```yaml\n{str(payload.get('preview') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_yaml failed: {observation}\n\n"

                elif action_type == "write_yaml":
                    path = action.get("path", "")
                    data = action.get("data")
                    print(f"[Brain]   → Write YAML: {path}", flush=True)
                    yield self._log_chunk(f" → write_yaml: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "write_yaml"):
                        ok, detail, payload = self.remote_workspace.write_yaml(path, data)
                    else:
                        ok, detail, payload = self._write_yaml_local(path, data)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ write_yaml failed: {observation}\n\n"

                elif action_type == "list_ports":
                    print("[Brain]   → List ports", flush=True)
                    yield self._log_chunk(" → list_ports\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "list_ports"):
                        ok, detail, payload = self.remote_workspace.list_ports()
                    else:
                        ok, detail, payload = self._list_ports_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        ports = payload.get("ports") or []
                        if isinstance(ports, list):
                            lines = [f"{p.get('proto')} {p.get('local')} {p.get('process')}" for p in ports[:100] if isinstance(p, dict)]
                            if lines:
                                observation += "\nPORTS:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ list_ports failed: {observation}\n\n"

                elif action_type == "process_tree":
                    target = action.get("pid_or_name") or action.get("target") or ""
                    print(f"[Brain]   → Process tree: {target}", flush=True)
                    yield self._log_chunk(f" → process_tree: {target}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "process_tree"):
                        ok, detail, payload = self.remote_workspace.process_tree(target)
                    else:
                        ok, detail, payload = self._process_tree_local(target)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        tree = payload.get("tree") or []
                        if isinstance(tree, list):
                            lines = [f"{'  ' * int(item.get('depth', 0))}{item.get('pid')} {item.get('comm')}" for item in tree[:120] if isinstance(item, dict)]
                            if lines:
                                observation += "\nPROCESS_TREE:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ process_tree failed: {observation}\n\n"

                elif action_type == "disk_usage":
                    path = action.get("path", "")
                    print(f"[Brain]   → Disk usage: {path}", flush=True)
                    yield self._log_chunk(f" → disk_usage: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "disk_usage"):
                        ok, detail, payload = self.remote_workspace.disk_usage(path)
                    else:
                        ok, detail, payload = self._disk_usage_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nSIZE_MB: {payload.get('size_mb')}\nFILES: {payload.get('files')}\nDIRS: {payload.get('dirs')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ disk_usage failed: {observation}\n\n"

                elif action_type == "find_large_files":
                    root = action.get("root", "~")
                    limit_mb = action.get("limit_mb", 1)
                    print(f"[Brain]   → Find large files: {root} >= {limit_mb}MB", flush=True)
                    yield self._log_chunk(f" → find_large_files: {root} >= {limit_mb}MB\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "find_large_files"):
                        ok, detail, payload = self.remote_workspace.find_large_files(root, float(limit_mb or 1))
                    else:
                        ok, detail, payload = self._find_large_files_local(root, float(limit_mb or 1))
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        matches = payload.get("matches") or []
                        if isinstance(matches, list):
                            lines = [f"{m.get('size_mb')}MB {m.get('path')}" for m in matches[:100] if isinstance(m, dict)]
                            if lines:
                                observation += "\nLARGE_FILES:\n" + "\n".join(lines)
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ find_large_files failed: {observation}\n\n"

                elif action_type == "env_get":
                    name = action.get("name", "")
                    print(f"[Brain]   → Env get: {name}", flush=True)
                    yield self._log_chunk(f" → env_get: {name}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "env_get"):
                        ok, detail, payload = self.remote_workspace.env_get(name)
                    else:
                        ok, detail, payload = self._env_get_local(name)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nVALUE:\n```\n{str(payload.get('value') or '')[:4000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ env_get failed: {observation}\n\n"

                elif action_type == "env_set_local":
                    name = action.get("name", "")
                    value = action.get("value", "")
                    print(f"[Brain]   → Env set local: {name}", flush=True)
                    yield self._log_chunk(f" → env_set_local: {name}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "env_set_local"):
                        ok, detail, payload = self.remote_workspace.env_set_local(name, value)
                    else:
                        ok, detail, payload = self._env_set_local(name, value)
                    observation = str(detail or "")
                    if ok:
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ env_set_local failed: {observation}\n\n"

                elif action_type == "http_request":
                    method = action.get("method", "GET")
                    url = action.get("url", "")
                    headers = action.get("headers", {})
                    body = action.get("body", "")
                    print(f"[Brain]   → HTTP request: {method} {url}", flush=True)
                    yield self._log_chunk(f" → http_request: {method} {url}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "http_request"):
                        ok, detail, payload = self.remote_workspace.http_request(method, url, headers, body)
                    else:
                        ok, detail, payload = self._http_request_local(method, url, headers, body)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nHTTP_BODY:\n```\n{str(payload.get('body') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ http_request failed: {observation}\n\n"

                elif action_type == "sqlite_query":
                    path = action.get("path", "")
                    sql = action.get("sql", "")
                    print(f"[Brain]   → SQLite query: {path}", flush=True)
                    yield self._log_chunk(f" → sqlite_query: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "sqlite_query"):
                        ok, detail, payload = self.remote_workspace.sqlite_query(path, sql)
                    else:
                        ok, detail, payload = self._sqlite_query_local(path, sql)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        if "rows" in payload:
                            observation += "\nSQL_ROWS:\n" + "\n".join(str(row) for row in (payload.get("rows") or [])[:100])
                        else:
                            observation += f"\nROWS_AFFECTED: {payload.get('rows_affected')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ sqlite_query failed: {observation}\n\n"

                elif action_type == "read_json_schema":
                    path = action.get("path", "")
                    print(f"[Brain]   → Read JSON schema: {path}", flush=True)
                    yield self._log_chunk(f" → read_json_schema: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_json_schema"):
                        ok, detail, payload = self.remote_workspace.read_json_schema(path)
                    else:
                        ok, detail, payload = self._read_json_schema_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nSCHEMA:\n```\n{str(payload.get('preview') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_json_schema failed: {observation}\n\n"

                elif action_type == "read_yaml_schema":
                    path = action.get("path", "")
                    print(f"[Brain]   → Read YAML schema: {path}", flush=True)
                    yield self._log_chunk(f" → read_yaml_schema: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "read_yaml_schema"):
                        ok, detail, payload = self.remote_workspace.read_yaml_schema(path)
                    else:
                        ok, detail, payload = self._read_yaml_schema_local(path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nSCHEMA:\n```\n{str(payload.get('preview') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ read_yaml_schema failed: {observation}\n\n"

                elif action_type == "docker_ps":
                    print("[Brain]   → Docker ps", flush=True)
                    yield self._log_chunk(" → docker_ps\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "docker_ps"):
                        ok, detail, payload = self.remote_workspace.docker_ps()
                    else:
                        ok, detail, payload = self._docker_ps_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += "\nCONTAINERS:\n" + "\n".join(str(row) for row in (payload.get("containers") or [])[:50])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ docker_ps failed: {observation}\n\n"

                elif action_type == "docker_logs":
                    container = action.get("container", "")
                    print(f"[Brain]   → Docker logs: {container}", flush=True)
                    yield self._log_chunk(f" → docker_logs: {container}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "docker_logs"):
                        ok, detail, payload = self.remote_workspace.docker_logs(container)
                    else:
                        ok, detail, payload = self._docker_logs_local(container)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nDOCKER_LOGS:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ docker_logs failed: {observation}\n\n"

                elif action_type == "systemctl_status":
                    service = action.get("service", "")
                    print(f"[Brain]   → Systemctl status: {service}", flush=True)
                    yield self._log_chunk(f" → systemctl_status: {service}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "systemctl_status"):
                        ok, detail, payload = self.remote_workspace.systemctl_status(service)
                    else:
                        ok, detail, payload = self._systemctl_status_local(service)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nSYSTEMCTL:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ systemctl_status failed: {observation}\n\n"

                elif action_type == "systemctl_restart":
                    service = action.get("service", "")
                    print(f"[Brain]   → Systemctl restart: {service}", flush=True)
                    yield self._log_chunk(f" → systemctl_restart: {service}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "systemctl_restart"):
                        ok, detail, payload = self.remote_workspace.systemctl_restart(service)
                    else:
                        ok, detail, payload = self._systemctl_restart_local(service)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nACTIVE: {payload.get('active') or ''}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ systemctl_restart failed: {observation}\n\n"

                elif action_type == "ffmpeg_run":
                    args = action.get("args", [])
                    print(f"[Brain]   → ffmpeg_run", flush=True)
                    yield self._log_chunk(" → ffmpeg_run\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "ffmpeg_run"):
                        ok, detail, payload = self.remote_workspace.ffmpeg_run(args)
                    else:
                        ok, detail, payload = self._ffmpeg_run_local(args)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nFFMPEG:\n```\n{str(payload.get('output') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ ffmpeg_run failed: {observation}\n\n"

                elif action_type == "blender_render":
                    script = str(action.get("script") or "")
                    size = action.get("size", "1024x1024")
                    output_path = action.get("output_path", "")
                    print(f"[Brain]   → blender_render: {output_path}", flush=True)
                    yield self._log_chunk(f" → blender_render: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "blender_render"):
                        ok, detail, payload = self.remote_workspace.blender_render(script, output_path, size)
                    else:
                        ok, detail, payload = self._blender_render_local(script, output_path, size)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ blender_render failed: {observation}\n\n"

                elif action_type == "compose_video_from_images":
                    inputs = action.get("inputs")
                    output_path = action.get("output_path", "")
                    fps = float(action.get("fps") or 24.0)
                    print(f"[Brain]   → compose_video_from_images: {output_path}", flush=True)
                    yield self._log_chunk(f" → compose_video_from_images: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "compose_video_from_images"):
                        ok, detail, payload = self.remote_workspace.compose_video_from_images(inputs, output_path, fps)
                    else:
                        ok, detail, payload = self._compose_video_from_images_local(inputs, output_path, fps)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nVIDEO_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ compose_video_from_images failed: {observation}\n\n"

                elif action_type == "generate_image_ai":
                    prompt = action.get("prompt", "")
                    output_path = action.get("output_path", "")
                    size = str(action.get("size") or "1024x1024")
                    quality = str(action.get("quality") or "low")
                    print(f"[Brain]   → generate_image_ai: {output_path}", flush=True)
                    yield self._log_chunk(f" → generate_image_ai: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "generate_image_ai"):
                        ok, detail, payload = self.remote_workspace.generate_image_ai(prompt, output_path, size, quality)
                    else:
                        ok, detail, payload = self._generate_image_ai_local(prompt, output_path, size, quality)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ generate_image_ai failed: {observation}\n\n"

                elif action_type == "upscale_image_ai":
                    input_path = action.get("input_path", "")
                    output_path = action.get("output_path", "")
                    size = str(action.get("size") or "1536x1024")
                    quality = str(action.get("quality") or "low")
                    print(f"[Brain]   → upscale_image_ai: {output_path}", flush=True)
                    yield self._log_chunk(f" → upscale_image_ai: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "upscale_image_ai"):
                        ok, detail, payload = self.remote_workspace.upscale_image_ai(input_path, output_path, size, quality)
                    else:
                        ok, detail, payload = self._upscale_image_ai_local(input_path, output_path, size, quality)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ upscale_image_ai failed: {observation}\n\n"

                elif action_type == "remove_background_ai":
                    input_path = action.get("input_path", "")
                    output_path = action.get("output_path", "")
                    quality = str(action.get("quality") or "low")
                    print(f"[Brain]   → remove_background_ai: {output_path}", flush=True)
                    yield self._log_chunk(f" → remove_background_ai: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "remove_background_ai"):
                        ok, detail, payload = self.remote_workspace.remove_background_ai(input_path, output_path, quality)
                    else:
                        ok, detail, payload = self._remove_background_ai_local(input_path, output_path, quality)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ remove_background_ai failed: {observation}\n\n"

                elif action_type == "edit_image_ai":
                    prompt = action.get("prompt", "")
                    input_paths = action.get("input_paths")
                    output_path = action.get("output_path", "")
                    mask_path = str(action.get("mask_path") or "")
                    size = str(action.get("size") or "1024x1024")
                    quality = str(action.get("quality") or "low")
                    print(f"[Brain]   → edit_image_ai: {output_path}", flush=True)
                    yield self._log_chunk(f" → edit_image_ai: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "edit_image_ai"):
                        ok, detail, payload = self.remote_workspace.edit_image_ai(prompt, input_paths, output_path, mask_path, size, quality)
                    else:
                        ok, detail, payload = self._edit_image_ai_local(prompt, input_paths, output_path, mask_path, size, quality)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ edit_image_ai failed: {observation}\n\n"

                elif action_type == "extract_video_frames":
                    path = action.get("path", "")
                    output_dir = action.get("output_dir", "")
                    fps = float(action.get("fps") or 1.0)
                    max_frames = int(action.get("max_frames") or 0)
                    print(f"[Brain]   → extract_video_frames: {path}", flush=True)
                    yield self._log_chunk(f" → extract_video_frames: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "extract_video_frames"):
                        ok, detail, payload = self.remote_workspace.extract_video_frames(path, output_dir, fps, max_frames)
                    else:
                        ok, detail, payload = self._extract_video_frames_local(path, output_dir, fps, max_frames)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        frames = payload.get("frames") or []
                        observation += "\nFRAMES:\n" + "\n".join(str(item) for item in frames[:20])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ extract_video_frames failed: {observation}\n\n"

                elif action_type == "storyboard_to_video":
                    storyboard = action.get("storyboard")
                    output_path = action.get("output_path", "")
                    size = str(action.get("size") or "1024x1024")
                    quality = str(action.get("quality") or "low")
                    fps = float(action.get("fps") or 12.0)
                    seconds_per_scene = float(action.get("seconds_per_scene") or 2.0)
                    print(f"[Brain]   → storyboard_to_video: {output_path}", flush=True)
                    yield self._log_chunk(f" → storyboard_to_video: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "storyboard_to_video"):
                        ok, detail, payload = self.remote_workspace.storyboard_to_video(storyboard, output_path, size, quality, fps, seconds_per_scene)
                    else:
                        ok, detail, payload = self._storyboard_to_video_local(storyboard, output_path, size, quality, fps, seconds_per_scene)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nVIDEO_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ storyboard_to_video failed: {observation}\n\n"

                elif action_type == "generate_image":
                    prompt = action.get("prompt", "")
                    size = action.get("size", "1024x1024")
                    output_path = action.get("output_path", "")
                    print(f"[Brain]   → generate_image: {output_path}", flush=True)
                    yield self._log_chunk(f" → generate_image: {output_path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "generate_image"):
                        ok, detail, payload = self.remote_workspace.generate_image(prompt, size, output_path)
                    else:
                        ok, detail, payload = self._generate_image_local(prompt, size, output_path)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nIMAGE_PATH: {payload.get('path')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ generate_image failed: {observation}\n\n"

                elif action_type == "ocr_region":
                    x = int(action.get("x") or 0)
                    y = int(action.get("y") or 0)
                    w = int(action.get("w") or 0)
                    h = int(action.get("h") or 0)
                    print(f"[Brain]   → ocr_region: {x},{y},{w},{h}", flush=True)
                    yield self._log_chunk(f" → ocr_region: {x},{y},{w},{h}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "ocr_region"):
                        ok, detail, payload = self.remote_workspace.ocr_region(x, y, w, h)
                    else:
                        ok, detail, payload = self._ocr_region_local(x, y, w, h)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nOCR_TEXT:\n```\n{str(payload.get('text') or '')[:8000]}\n```"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ ocr_region failed: {observation}\n\n"

                elif action_type == "list_downloads":
                    print("[Brain]   → list_downloads", flush=True)
                    yield self._log_chunk(" → list_downloads\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "list_downloads"):
                        ok, detail, payload = self.remote_workspace.list_downloads()
                    else:
                        ok, detail, payload = self._list_downloads_local()
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += "\nDOWNLOADS:\n" + "\n".join(str(item) for item in (payload.get("entries") or [])[:50])
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ list_downloads failed: {observation}\n\n"

                elif action_type == "watch_file":
                    path = action.get("path", "")
                    seconds = float(action.get("seconds") or 5)
                    print(f"[Brain]   → watch_file: {path}", flush=True)
                    yield self._log_chunk(f" → watch_file: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "watch_file"):
                        ok, detail, payload = self.remote_workspace.watch_file(path, seconds)
                    else:
                        ok, detail, payload = self._watch_file_local(path, seconds)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nWATCH_CHANGED: {payload.get('changed')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ watch_file failed: {observation}\n\n"

                elif action_type == "watch_dir":
                    path = action.get("path", "")
                    seconds = float(action.get("seconds") or 5)
                    print(f"[Brain]   → watch_dir: {path}", flush=True)
                    yield self._log_chunk(f" → watch_dir: {path}\n")
                    if self.remote_workspace is not None and hasattr(self.remote_workspace, "watch_dir"):
                        ok, detail, payload = self.remote_workspace.watch_dir(path, seconds)
                    else:
                        ok, detail, payload = self._watch_dir_local(path, seconds)
                    observation = str(detail or "")
                    if ok and isinstance(payload, dict):
                        observation += f"\nWATCH_CHANGED: {payload.get('changed')}"
                        yield self._log_chunk(f"✓ {detail}\n\n")
                    else:
                        yield f"❌ watch_dir failed: {observation}\n\n"

                # ---- PYTHON EXEC ----
                elif action_type == "python_exec":
                    code = action.get("code", "")
                    print(f"[Brain]   → Python exec:\n{code[:50]}...", flush=True)
                    yield self._log_chunk(" → python_exec\n")
                    try:
                        # Write to temp file and run
                        tmpfile = "/tmp/ariaos_agent_exec.py"
                        with open(tmpfile, "w") as f:
                            f.write(code)
                        res = subprocess.run(
                            [sys.executable, tmpfile],
                            capture_output=True, text=True, timeout=30
                        )
                        out = res.stdout
                        err = res.stderr
                        exit_code = res.returncode
                        observation = f"PYTHON EXECUTION RESULT (Exit {exit_code}):\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                        yield self._log_chunk(f"✓ Python snippet executed (Exit {exit_code})\n\n")
                    except Exception as e:
                        observation = f"ERROR executing python: {e}"
                        yield f"❌ Python exec failed: {e}\n\n"

                # ---- UNKNOWN ----
                else:
                    observation = f"Unknown action type: {action_type}"
                    yield self._log_chunk(f" → Unknown: {action_type}\n")

                # 5. OBSERVE — compress the local result into a new user-side
                # observation so the planner can choose the next action with the
                # current state instead of replaying the whole task from zero.
                self._trace("action_result", {
                    "action_type": action_type,
                    "observation": observation[:1200],
                    "screen_signals": self.last_screen_signals,
                    "verifier_events": self.verifier.events[-5:],
                })
                compact_observation = self._compact_observation_for_model(observation)
                self.messages.append({
                    "role": "user",
                    "content": (
                        f"CURRENT GOAL: {self.goal_text}\n\n"
                        f"OBSERVATION (step {step}):\n{compact_observation}\n\n"
                        "Continue toward the goal. Return only one JSON object with mandatory thought+action."
                    ),
                })

                # Async: summarize this step in background (0 latency added)
                if action:
                    self._summarize_step_async(global_step, action, observation)

                # Small delay between steps
                time.sleep(STEP_DELAY)

            except UserStopRequested:
                yield "⛔ STOP: execution interrupted by user.\n"
                result = {"success": False, "reason": "Stopped by user", "steps": max(step - 1, 0), "stopped": True}
                break
            except TaskBudgetExceeded as exc:
                reason = str(exc) or self._budget_failure_reason()
                print(f"[Brain] 💸 Budget exceeded: {reason}", flush=True)
                yield f"💸 {reason}\n"
                result = {"success": False, "reason": reason, "steps": step}
                break
            except Exception as e:
                print(f"[Brain] ⚠️ Step {step} error: {e}", flush=True)
                traceback.print_exc()
                yield f"⚠️ Error: {e}\n"
                self._trace("step_error", {"step": step, "error": str(e)})
                self.messages.append({
                    "role": "user",
                    "content": f"ERROR in step {step}: {str(e)}. Try a different approach."
                })

        # === SAVE TO MEMORY ===
        summary = result.get("summary", result.get("reason", "Task ended"))
        self._last_goal_success = result.get("success", False)
        progress_snapshot = self._snapshot_progress_log(flush=True)
        self.memory.add_exchange(
            user_goal=goal,
            result_summary=summary,
            steps=result.get("steps", 0),
            success=result.get("success", False)
        )

        # === SAVE TASK SUMMARY + UPDATE USER PROFILE (async) ===
        self._save_task_summary_async(goal, summary, result, progress_snapshot)
        self._append_progress_snapshot_to_session(
            goal,
            progress_snapshot,
            self._last_goal_success,
            result.get("steps", 0),
            summary_text=summary,
        )
        self.save_session_log(self._current_session_id)

        if self.vision_calls > 0:
            yield self._log_chunk(f"\n🔍 Total vision reads: {self.vision_calls}\n")

        self._finalize_task_usage(result)
        self._trace("goal_end", {"result": result, "vision_calls": self.vision_calls})
        yield self._log_chunk("\n🧾 Session state saved.\n")
        self._active_stop_event = None
        return result

    def _trace(self, event_type: str, payload: Dict[str, Any]):
        if not self.trace_enabled:
            return
        try:
            record = {
                "ts": datetime.now().isoformat(),
                "event": event_type,
                "step": self.step_count,
                "goal": self.goal_lower[:240],
                "backend": self.input_backend,
                "profile": (self.active_profile or {}).get("id") if self.active_profile else None,
                "payload": payload,
            }
            os.makedirs(os.path.dirname(self.trace_file), exist_ok=True)
            with open(self.trace_file, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _messages_for_model(self) -> List[Dict[str, Any]]:
        if not self.messages:
            return []
        system_msg = self.messages[0]
        goal_msg = self.messages[1] if len(self.messages) > 1 else None

        # Inject progress log into system prompt for compact context
        progress_text = self._get_progress_log_text()
        if progress_text:
            enriched_system = {
                "role": "system",
                "content": system_msg["content"] + "\n\n" + progress_text,
            }
        else:
            enriched_system = system_msg

        if len(self.messages) <= 2:
            base = [enriched_system]
            if goal_msg is not None:
                base.append(goal_msg)
            return base

        # Only send the last N raw messages (sliding window)
        tail = self.messages[2:][-(SLIDING_WINDOW_RAW_MESSAGES * 2):]
        return [enriched_system, goal_msg, *tail]

    def _compact_observation_for_model(self, observation: str) -> str:
        text = (observation or "").strip()
        if not text:
            return "(empty observation)"

        lines = text.splitlines()
        out: List[str] = []
        kept_elements = 0
        in_elements = False
        started_snapshot = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("PERCEPTION SNAPSHOT"):
                started_snapshot = True
                in_elements = False
                out.append(stripped)
                continue

            if started_snapshot:
                if stripped in {"ELEMENTS:", "RAW_EXCERPT:"}:
                    in_elements = stripped == "ELEMENTS:"
                    out.append(stripped)
                    continue

                if in_elements and re.match(r"^\[\d+\]", stripped):
                    if kept_elements < MODEL_OBSERVATION_MAX_ELEMENTS:
                        out.append(stripped)
                    kept_elements += 1
                    continue

                if stripped.startswith((
                    "confidence:", "elements_visible:", "viewport:",
                    "vision_source:", "frame_id:", "parser_latency_ms:", "parser_error:",
                    "Verification:",
                )):
                    out.append(stripped)
                    continue
            else:
                # Keep short execution/result lines before snapshot.
                if len(out) < 14:
                    out.append(stripped)

        if not out:
            out = [text[:MODEL_OBSERVATION_MAX_CHARS]]
        compact = "\n".join(out)
        if len(compact) > MODEL_OBSERVATION_MAX_CHARS:
            compact = compact[:MODEL_OBSERVATION_MAX_CHARS] + "\n[truncated_for_model]"
        if kept_elements > MODEL_OBSERVATION_MAX_ELEMENTS:
            compact += (
                f"\n[elements_truncated_for_model: kept {MODEL_OBSERVATION_MAX_ELEMENTS} "
                f"of {kept_elements}]"
            )
        return compact

    def _action_signature(self, action: Dict[str, Any]) -> str:
        action_type = action.get("action", "")
        if action_type == "execute":
            return f"execute:{str(action.get('command', '')).strip()}"
        if action_type == "computer":
            return f"computer:{str(action.get('instruction') or action.get('goal') or action.get('query') or '').strip().lower()[:160]}"
        if action_type == "open_file":
            return f"open_file:{str(action.get('path', '')).strip()}"
        if action_type == "open_url":
            return f"open_url:{str(action.get('url', '')).strip()}"
        if action_type == "reveal_path":
            return f"reveal_path:{str(action.get('path', '')).strip()}"
        if action_type == "copy_to_clipboard":
            return f"copy_to_clipboard:{str(action.get('text', ''))[:120]}"
        if action_type == "paste":
            return "paste"
        if action_type == "read_clipboard":
            return "read_clipboard"
        if action_type == "focus_window":
            return f"focus_window:{str(action.get('app_or_title') or action.get('title') or action.get('app') or '').strip()[:120]}"
        if action_type == "open_app":
            return f"open_app:{str(action.get('app_name') or action.get('app') or '').strip()[:120]}"
        if action_type == "search_files":
            return f"search_files:{str(action.get('pattern') or '')[:80]}@{str(action.get('root') or '')[:60]}"
        if action_type == "select_file_for_active_dialog":
            return f"select_file_for_active_dialog:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "upload_file_to_active_app":
            return f"upload_file_to_active_app:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "drag_file_to_target":
            return f"drag_file_to_target:{str(action.get('path') or '')[:80]}:{int(action.get('x') or 0)},{int(action.get('y') or 0)}"
        if action_type == "list_windows":
            return "list_windows"
        if action_type == "make_dir":
            return f"make_dir:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "move_path":
            return f"move_path:{str(action.get('src') or '')[:80]}->{str(action.get('dst') or '')[:80]}"
        if action_type == "copy_path":
            return f"copy_path:{str(action.get('src') or '')[:80]}->{str(action.get('dst') or '')[:80]}"
        if action_type == "delete_path":
            return f"delete_path:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "append_file":
            return f"append_file:{str(action.get('path') or '').strip()[:80]}:{str(action.get('content') or '')[:60]}"
        if action_type == "replace_in_file":
            return f"replace_in_file:{str(action.get('path') or '').strip()[:80]}:{str(action.get('search') or '')[:40]}"
        if action_type == "tail_file":
            return f"tail_file:{str(action.get('path') or '').strip()[:80]}:{int(action.get('lines') or 20)}"
        if action_type == "read_path":
            return f"read_path:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "open_with":
            return f"open_with:{str(action.get('path') or '')[:80]}:{str(action.get('app_name') or action.get('app') or '')[:40]}"
        if action_type == "read_clipboard_image":
            return "read_clipboard_image"
        if action_type == "replace_regex_in_file":
            return f"replace_regex_in_file:{str(action.get('path') or '')[:80]}:{str(action.get('pattern') or '')[:40]}"
        if action_type == "grep_in_files":
            return f"grep_in_files:{str(action.get('pattern') or '')[:80]}@{str(action.get('root') or '')[:60]}"
        if action_type == "stat_path":
            return f"stat_path:{str(action.get('path') or '').strip()[:120]}"
        if action_type == "diff_paths":
            return f"diff_paths:{str(action.get('a') or action.get('left') or '')[:60]}::{str(action.get('b') or action.get('right') or '')[:60]}"
        if action_type == "list_processes":
            return "list_processes"
        if action_type == "kill_process":
            return f"kill_process:{str(action.get('pid_or_name') or action.get('target') or '')[:120]}"
        if action_type == "chmod_path":
            return f"chmod_path:{str(action.get('path') or '')[:80]}:{str(action.get('mode') or '')[:20]}"
        if action_type == "symlink_path":
            return f"symlink_path:{str(action.get('src') or '')[:80]}->{str(action.get('dst') or '')[:80]}"
        if action_type == "unzip_path":
            return f"unzip_path:{str(action.get('path') or '')[:80]}->{str(action.get('dst') or '')[:80]}"
        if action_type == "archive_path":
            return f"archive_path:{str(action.get('path') or '')[:80]}->{str(action.get('dst') or '')[:80]}"
        if action_type == "read_json":
            return f"read_json:{str(action.get('path') or '')[:120]}"
        if action_type == "write_json":
            return f"write_json:{str(action.get('path') or '')[:120]}"
        if action_type == "read_csv_preview":
            return f"read_csv_preview:{str(action.get('path') or '')[:80]}:{int(action.get('rows') or 10)}"
        if action_type == "git_status":
            return f"git_status:{str(action.get('repo') or '')[:120]}"
        if action_type == "git_diff":
            return f"git_diff:{str(action.get('repo') or '')[:80]}:{str(action.get('path') or '')[:60]}"
        if action_type == "run_tests":
            return f"run_tests:{str(action.get('target') or '')[:120]}"
        if action_type == "download_file":
            return f"download_file:{str(action.get('url') or '')[:120]}->{str(action.get('path') or '')[:80]}"
        if action_type == "extract_text_from_pdf":
            return f"extract_text_from_pdf:{str(action.get('path') or '')[:120]}"
        if action_type == "replace_block_in_file":
            return f"replace_block_in_file:{str(action.get('path') or '')[:80]}:{str(action.get('start_marker') or '')[:30]}"
        if action_type == "grep_ast":
            return f"grep_ast:{str(action.get('symbol') or '')[:80]}@{str(action.get('root') or '')[:60]}"
        if action_type == "read_yaml":
            return f"read_yaml:{str(action.get('path') or '')[:120]}"
        if action_type == "write_yaml":
            return f"write_yaml:{str(action.get('path') or '')[:120]}"
        if action_type == "list_ports":
            return "list_ports"
        if action_type == "process_tree":
            return f"process_tree:{str(action.get('pid_or_name') or action.get('target') or '')[:120]}"
        if action_type == "disk_usage":
            return f"disk_usage:{str(action.get('path') or '')[:120]}"
        if action_type == "find_large_files":
            return f"find_large_files:{str(action.get('root') or '')[:80]}:{str(action.get('limit_mb') or '')[:20]}"
        if action_type == "env_get":
            return f"env_get:{str(action.get('name') or '')[:80]}"
        if action_type == "env_set_local":
            return f"env_set_local:{str(action.get('name') or '')[:80]}"
        if action_type == "http_request":
            return f"http_request:{str(action.get('method') or '')[:12]}:{str(action.get('url') or '')[:100]}"
        if action_type == "sqlite_query":
            return f"sqlite_query:{str(action.get('path') or '')[:80]}:{str(action.get('sql') or '')[:40]}"
        if action_type == "read_json_schema":
            return f"read_json_schema:{str(action.get('path') or '')[:120]}"
        if action_type == "read_yaml_schema":
            return f"read_yaml_schema:{str(action.get('path') or '')[:120]}"
        if action_type == "docker_ps":
            return "docker_ps"
        if action_type == "docker_logs":
            return f"docker_logs:{str(action.get('container') or '')[:120]}"
        if action_type == "systemctl_status":
            return f"systemctl_status:{str(action.get('service') or '')[:120]}"
        if action_type == "systemctl_restart":
            return f"systemctl_restart:{str(action.get('service') or '')[:120]}"
        if action_type == "ffmpeg_run":
            return f"ffmpeg_run:{str(action.get('args') or '')[:120]}"
        if action_type == "blender_render":
            return f"blender_render:{str(action.get('output_path') or '')[:80]}:{str(action.get('size') or '')[:20]}"
        if action_type == "compose_video_from_images":
            return f"compose_video_from_images:{str(action.get('output_path') or '')[:80]}:{str(action.get('fps') or '')[:12]}"
        if action_type == "generate_image_ai":
            return f"generate_image_ai:{str(action.get('output_path') or '')[:80]}:{str(action.get('prompt') or '')[:40]}"
        if action_type == "upscale_image_ai":
            return f"upscale_image_ai:{str(action.get('input_path') or '')[:60]}->{str(action.get('output_path') or '')[:60]}"
        if action_type == "remove_background_ai":
            return f"remove_background_ai:{str(action.get('input_path') or '')[:60]}->{str(action.get('output_path') or '')[:60]}"
        if action_type == "edit_image_ai":
            return f"edit_image_ai:{str(action.get('output_path') or '')[:80]}:{str(action.get('prompt') or '')[:40]}"
        if action_type == "extract_video_frames":
            return f"extract_video_frames:{str(action.get('path') or '')[:80]}:{str(action.get('fps') or '')[:12]}"
        if action_type == "storyboard_to_video":
            return f"storyboard_to_video:{str(action.get('output_path') or '')[:80]}:{str(action.get('fps') or '')[:12]}"
        if action_type == "generate_image":
            return f"generate_image:{str(action.get('output_path') or '')[:80]}:{str(action.get('prompt') or '')[:40]}"
        if action_type == "ocr_region":
            return f"ocr_region:{int(action.get('x') or 0)},{int(action.get('y') or 0)},{int(action.get('w') or 0)},{int(action.get('h') or 0)}"
        if action_type == "list_downloads":
            return "list_downloads"
        if action_type == "watch_file":
            return f"watch_file:{str(action.get('path') or '')[:120]}:{str(action.get('seconds') or '')[:12]}"
        if action_type == "watch_dir":
            return f"watch_dir:{str(action.get('path') or '')[:120]}:{str(action.get('seconds') or '')[:12]}"
        if action_type == "wait":
            return f"wait:{action.get('seconds', 0)}"
        return json.dumps(action, sort_keys=True)[:160]

    def _is_action_repetition_blocked(self, signature: str) -> bool:
        if signature == self.last_action_signature:
            self.last_action_streak += 1
        else:
            self.last_action_signature = signature
            self.last_action_streak = 1
        return self.last_action_streak > self.max_same_action_streak

    def _has_current_task_state(self) -> bool:
        if self.progress_log:
            return True
        if str(self.verifier.last_url or "").strip():
            return True
        if str(self.verifier.last_screen_text or "").strip():
            return True
        return bool(self.last_screen_signals.get("visible_count"))

    def _current_task_state_text(self) -> str:
        parts: List[str] = []
        if self.progress_log:
            parts.extend(self.progress_log[-10:])
        last_url = str(self.verifier.last_url or "").strip()
        if last_url:
            parts.append(f"url:{last_url}")
        last_screen = str(self.verifier.last_screen_text or "").strip()
        if last_screen:
            parts.append(last_screen[:1500])
        return "\n".join(parts)

    @staticmethod
    def _extract_raw_screen_excerpt(screen_text: str) -> str:
        text = str(screen_text or "").strip()
        if not text:
            return ""
        marker = "RAW_EXCERPT:"
        if marker in text:
            return text.split(marker, 1)[1].strip()
        return text

    @staticmethod
    def _summary_claims_completion(summary_text: str) -> bool:
        low = str(summary_text or "").strip().lower()
        if not low:
            return False
        completion_tokens = (
            "sent", "send successfully", "emailed", "uploaded", "attached", "submitted",
            "logged in", "successfully", "objective achieved", "done", "completed",
            "enviado", "enviada", "envié", "enviar", "adjunto", "adjuntado",
            "subido", "completado", "hecho", "envoye", "envoyé",
        )
        return any(token in low for token in completion_tokens)

    def _screen_truth_block_reason(
        self,
        screen_text: str,
        claimed_summary: str = "",
        *,
        require_claim: bool = False,
    ) -> Optional[str]:
        raw = self._extract_raw_screen_excerpt(screen_text).lower()
        if not raw:
            return None
        if require_claim and not self._summary_claims_completion(claimed_summary):
            return None

        email_context = any(token in self.goal_lower for token in ("gmail", "email", "mail", "correo", "mensaje", "message"))
        upload_context = any(token in self.goal_lower for token in ("upload", "attach", "attachment", "join", "pièce jointe", "adjunto"))

        if any(token in raw for token in ("address not found", "wasn't delivered", "was not delivered", "undelivered", "delivery failed", "delivery incomplete", "bounce")):
            return "a delivery error is visible on screen"
        if any(token in raw for token in ("sign-in page", "google sign-in", "login page", "sign in to continue", "screen de inicio de sesión")):
            return "a login screen is still visible"
        if email_context and (
            "compose window open" in raw
            or "draft is open" in raw
            or "draft window open" in raw
        ) and any(
            token in raw for token in (
                "subject and message body are still empty",
                "message body are still empty",
                "message body is still empty",
                "subject is still empty",
                "subject and body are still empty",
                "body is empty",
                "subject is empty",
                "body are empty",
            )
        ):
            return "the email draft is still empty on screen"
        if upload_context and any(token in raw for token in ("missing attachment", "no attachment", "attachment is missing")):
            return "the attachment is still missing on screen"
        return None

    def _action_context_text(self, action: Dict[str, Any]) -> str:
        values = [
            action.get("thought", ""),
            action.get("progress_log", ""),
            action.get("instruction", ""),
            action.get("command", ""),
            action.get("url", ""),
            action.get("path", ""),
            action.get("pattern", ""),
            action.get("summary", ""),
            action.get("reason", ""),
        ]
        return " ".join(str(value or "") for value in values if value).strip()

    def _is_local_correction_context(self, text: str) -> bool:
        low = str(text or "").lower()
        if not low:
            return False
        local_tokens = (
            "back", "go back", "previous", "one step", "correct", "correction", "fix",
            "edit", "update", "change", "modify", "adjust", "retry current",
            "current step", "continue from", "continue", "resume", "return to",
            "reopen draft", "draft", "field", "value", "recipient", "subject",
            "body", "attachment", "replace attachment", "remove attachment",
            "picker", "dialog", "chooser", "undo",
        )
        return any(token in low for token in local_tokens)

    def _extract_url_host(self, url: str) -> str:
        match = re.match(r"^https?://([^/\s?#]+)", str(url or "").strip(), flags=re.IGNORECASE)
        return str(match.group(1)).strip().lower() if match else ""

    def _should_block_blind_restart(self, action: Dict[str, Any]) -> Optional[str]:
        if not self._has_current_task_state():
            return None
        action_type = str(action.get("action") or "").strip().lower()
        context_text = self._action_context_text(action)
        if self._is_local_correction_context(context_text):
            return None

        current_state = self._current_task_state_text().lower()
        lowered = context_text.lower()
        current_url = str(self.verifier.last_url or "").strip().lower()

        if action_type == "write_file":
            path = os.path.expanduser(str(action.get("path") or "").strip())
            if path and path.lower() in current_state:
                return (
                    "Blind restart detected: this file already exists and was already validated in the current task. "
                    "Continue from the current state or edit the existing file instead."
                )

        if action_type == "search_files":
            pattern = str(action.get("pattern") or "").strip().lower()
            if pattern and pattern in current_state:
                return (
                    "Blind restart detected: the same file search is being repeated from zero. "
                    "Continue from the current state unless you are correcting a specific local issue."
                )

        if action_type == "open_url":
            target_url = str(action.get("url") or "").strip().lower()
            if current_url and target_url:
                target_host = self._extract_url_host(target_url)
                current_host = self._extract_url_host(current_url)
                is_same_homepage = (
                    target_host
                    and current_host
                    and target_host == current_host
                    and re.match(r"^https?://[^/]+/?$", target_url)
                )
                if target_url == current_url or is_same_homepage:
                    return (
                        "Blind restart detected: this reopens the current site from the beginning. "
                        "Continue from the current page or perform a local correction instead."
                    )

        if action_type in {"computer", "execute"}:
            restart_tokens = (
                "start over", "restart", "from scratch", "begin again", "reopen the homepage",
                "open gmail", "open chrome", "open browser", "go to google", "search for",
                "look for apartments", "find apartments", "repeat the search",
            )
            if any(token in lowered for token in restart_tokens) and (
                current_url or any(marker in current_state for marker in ("created", "found", "verified", "draft", "attachment"))
            ):
                return (
                    "Blind restart detected: useful current state already exists. "
                    "Continue from the current screen/state or make a local correction instead of restarting the whole task."
                )

        return None

    def _is_command_failure(self, output: str) -> bool:
        return (
            "[exit code:" in output
            or output.startswith("[TIMEOUT")
            or output.startswith("[ERROR")
            or output.startswith("ERROR:")
        )

    def _command_failure_key(self, command: str) -> str:
        cmd = command.strip()
        lowered = cmd.lower()
        if (
            "websocket.create_connection" in lowered
            or "runtime.evaluate" in lowered
            or "/json" in lowered
            or "devtools/page/" in lowered
        ):
            return "websocket_script"
        if lowered.startswith("google-chrome"):
            return "launch_chrome"
        return cmd

    def _goal_is_navigation_task(self) -> bool:
        keywords = (
            "gmail", "mail", "email", "discord", "chrome", "browser",
            "web", "search", "recherche", "navig", "app", "message",
        )
        return any(k in self.goal_lower for k in keywords)

    def _extract_explicit_url(self, text: str) -> str:
        value = str(text or "")
        match = re.search(r"https?://[^\s'\"<>]+", value, flags=re.IGNORECASE)
        return str(match.group(0)).strip() if match else ""

    def _extract_explicit_file_reference(self, text: str) -> str:
        value = str(text or "")
        matches = re.findall(
            r"(?:~?/|/)?[A-Za-z0-9_.\-/]+?\.(?:txt|csv|pdf|png|jpg|jpeg|gif|bmp|webp|zip|tar|gz|json|yaml|yml|py|md|html|htm|js|ts|tsx|jsx|docx?|xlsx?|pptx?)",
            value,
            flags=re.IGNORECASE,
        )
        if not matches:
            return ""
        return str(matches[0]).strip()

    def _goal_is_browser_form_upload_task(self) -> bool:
        keywords = (
            "gmail", "mail", "email", "chrome", "browser", "web", "search",
            "recherche", "navig", "upload", "attach", "attachment", "jointe",
            "joindre", "form", "formulaire", "submit", "picker", "chooser",
        )
        return any(k in self.goal_lower for k in keywords)

    def _is_terminal_like_name(self, value: Any) -> bool:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return False
        terminal_tokens = (
            "terminal", "xfce4-terminal", "gnome-terminal", "xterm",
            "konsole", "alacritty", "kitty", "urxvt",
        )
        return any(token in normalized for token in terminal_tokens)

    def _should_block_specialized_tool(self, action_type: str, action: Dict[str, Any]) -> Optional[str]:
        if not self._goal_is_browser_form_upload_task():
            return None
        if action_type == "open_app":
            app_name = action.get("app_name") or action.get("app") or ""
            if self._is_terminal_like_name(app_name):
                return "Browser/form/upload tasks must not open Terminal. Use specialized tools and computer."
        if action_type == "focus_window":
            query = action.get("app_or_title") or action.get("title") or action.get("app") or ""
            if self._is_terminal_like_name(query):
                return "Browser/form/upload tasks must not focus Terminal. Use specialized tools and computer."
        if action_type == "open_with":
            app_name = action.get("app_name") or action.get("app") or ""
            if self._is_terminal_like_name(app_name):
                return "Browser/form/upload tasks must not open files in Terminal. Use specialized tools and computer."
        return None

    def _should_use_specialized_tool_before_computer(self, instruction: str) -> Optional[str]:
        # When the planner already mentions a concrete URL or file, route it to
        # the precise helper first so computer-use turns are spent on visible UI
        # interaction rather than on path/url resolution.
        text = str(instruction or "").strip()
        lowered = text.lower()
        if not text:
            return None
        explicit_url = self._extract_explicit_url(text)
        if explicit_url and any(token in lowered for token in ("open", "visit", "go to", "goto", "navigate", "ouvre", "abrir")):
            return f"Explicit URL detected ({explicit_url}). Use open_url before computer. Use computer only after the page is open."
        explicit_file = self._extract_explicit_file_reference(text)
        if not explicit_file:
            return None
        if any(token in lowered for token in ("attach", "attachment", "upload", "jointe", "joindre", "picker", "dialog", "chooser")):
            if "/" in explicit_file or explicit_file.startswith("~"):
                return (
                    f"Explicit file reference detected ({explicit_file}). "
                    "Use upload_file_to_active_app or select_file_for_active_dialog before computer."
                )
            return (
                f"Explicit file reference detected ({explicit_file}). "
                "Use search_files to resolve the path, then use upload_file_to_active_app or select_file_for_active_dialog before computer."
            )
        if any(token in lowered for token in ("open", "show", "reveal", "ouvre", "abrir", "visible")):
            if "/" in explicit_file or explicit_file.startswith("~"):
                return f"Explicit file reference detected ({explicit_file}). Use open_file or reveal_path before computer."
            return f"Explicit file reference detected ({explicit_file}). Use search_files to resolve the path, then use open_file or reveal_path before computer."
        return None

    def _should_block_execute(self, command: str) -> Optional[str]:
        # Shell execution is for non-visual helper work. Browser/Gmail/upload
        # flows must stay in specialized helpers plus computer-use so the agent
        # does not fall back to Terminal for tasks meant to happen on screen.
        lowered = str(command or "").strip().lower()
        if not lowered:
            return None
        if self._goal_is_navigation_task():
            browser_patterns = (
                "browser_navigator.py",
                "xdotool",
                "google-chrome",
                "chromium",
                "xdg-open",
                "mail.google.com",
                "gmail",
            )
            if any(pattern in lowered for pattern in browser_patterns):
                return "Browser/navigation tasks must use the computer action only."
        if self._goal_is_browser_form_upload_task():
            terminal_patterns = (
                "xfce4-terminal",
                "gnome-terminal",
                "xterm",
                "konsole",
                "alacritty",
                "kitty",
            )
            if any(pattern in lowered for pattern in terminal_patterns):
                return "Browser/form/upload tasks must not open Terminal. Use specialized tools and computer."
        return None

    def _open_file_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not normalized or not os.path.exists(normalized):
            return False, f"ERROR opening file {normalized}: file_not_found", {"path": normalized}
        try:
            proc = subprocess.Popen(
                ["xdg-open", normalized],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                start_new_session=True,
            )
            time.sleep(0.35)
            status = proc.poll()
        except Exception as exc:
            return False, f"ERROR opening file {normalized}: {exc}", {"path": normalized}
        if status is None:
            return True, f"Opened file: {normalized}", {"path": normalized}
        stdout, stderr = proc.communicate(timeout=1.0)
        if status != 0:
            detail = (stderr or stdout or "").strip() or f"exit_{status}"
            return False, f"ERROR opening file {normalized}: {detail}", {"path": normalized}
        return True, f"Opened file: {normalized}", {"path": normalized}

    def _open_url_local(self, url: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = str(url or "").strip()
        if not re.match(r"^https?://", normalized, re.IGNORECASE):
            return False, f"ERROR opening URL {normalized}: invalid_url", {"url": normalized}
        try:
            proc = subprocess.Popen(
                ["xdg-open", normalized],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                start_new_session=True,
            )
            time.sleep(0.35)
            status = proc.poll()
        except Exception as exc:
            return False, f"ERROR opening URL {normalized}: {exc}", {"url": normalized}
        if status is None:
            return True, f"Opened URL: {normalized}", {"url": normalized}
        stdout, stderr = proc.communicate(timeout=1.0)
        if status != 0:
            detail = (stderr or stdout or "").strip() or f"exit_{status}"
            return False, f"ERROR opening URL {normalized}: {detail}", {"url": normalized}
        return True, f"Opened URL: {normalized}", {"url": normalized}

    def _reveal_path_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        target = normalized if os.path.isdir(normalized) else os.path.dirname(normalized)
        if not target:
            target = os.path.expanduser("~")
        if not normalized or not os.path.exists(normalized):
            return False, f"ERROR revealing path {normalized}: path_not_found", {"path": normalized, "directory": target}
        try:
            proc = subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                start_new_session=True,
            )
            time.sleep(0.35)
            status = proc.poll()
        except Exception as exc:
            return False, f"ERROR revealing path {normalized}: {exc}", {"path": normalized, "directory": target}
        if status is None:
            return True, f"Opened parent directory for: {normalized}", {"path": normalized, "directory": target}
        stdout, stderr = proc.communicate(timeout=1.0)
        if status != 0:
            detail = (stderr or stdout or "").strip() or f"exit_{status}"
            return False, f"ERROR revealing path {normalized}: {detail}", {"path": normalized, "directory": target}
        return True, f"Opened parent directory for: {normalized}", {"path": normalized, "directory": target}

    def _copy_to_clipboard_local(self, text: str) -> tuple[bool, str, dict[str, Any]]:
        value = str(text or "")
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
                    timeout=8,
                    env=self.env,
                )
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                try:
                    verify = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-o"],
                        capture_output=True,
                        text=True,
                        timeout=4,
                        env=self.env,
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

    def _paste_clipboard_local(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                timeout=4,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR pasting clipboard: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR pasting clipboard: {detail}", {}
        clipboard_text = str(result.stdout or "")
        if not clipboard_text:
            return False, "ERROR pasting clipboard: clipboard_empty", {}
        try:
            typed = subprocess.run(
                ["xdotool", "type", "--delay", "12", "--clearmodifiers", clipboard_text],
                capture_output=True,
                text=True,
                timeout=max(8, min(40, int(len(clipboard_text) / 3.0 + 4.0))),
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR pasting clipboard: {exc}", {}
        if typed.returncode != 0:
            detail = (typed.stderr or typed.stdout or "").strip() or f"exit_{typed.returncode}"
            return False, f"ERROR pasting clipboard: {detail}", {}
        return True, "Pasted clipboard into active field", {"length": len(clipboard_text)}

    def _read_clipboard_local(self) -> tuple[bool, str, dict[str, Any]]:
        last_error = "clipboard_unavailable"
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=4,
                    env=self.env,
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

    def _focus_window_local(self, app_or_title: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(app_or_title or "").strip().lower()
        if not query:
            return False, "ERROR focusing window: empty_query", {"query": query}
        try:
            result = subprocess.run(
                ["wmctrl", "-lx"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR focusing window: {exc}", {"query": query}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR focusing window: {detail}", {"query": query}
        for line in result.stdout.splitlines():
            low = line.lower()
            if query in low:
                win_id = line.split()[0]
                try:
                    subprocess.run(["wmctrl", "-ia", win_id], timeout=5, env=self.env)
                except Exception as exc:
                    return False, f"ERROR focusing window: {exc}", {"query": query}
                title = line.split(None, 4)[-1] if len(line.split(None, 4)) >= 5 else line
                return True, f"Focused window: {title}", {"query": query, "window": title}
        return False, f"ERROR focusing window: no_match_for_{query}", {"query": query}

    def _open_app_local(self, app_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(app_name or "").strip()
        command = self._resolve_app_command_local(raw)
        commands: List[List[str]] = [command] if command else []
        if not commands:
            return False, "ERROR opening app: empty_app_name", {"app": raw}
        last_error = "app_launch_failed"
        for cmd in commands:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self.env,
                    start_new_session=True,
                )
                time.sleep(0.35)
                status = proc.poll()
                if status is None:
                    return True, f"Opened app: {raw}", {"app": raw, "command": cmd}
                stdout, stderr = proc.communicate(timeout=1.0)
                if status == 0:
                    return True, f"Opened app: {raw}", {"app": raw, "command": cmd}
                last_error = (stderr or stdout or "").strip() or f"exit_{status}"
            except Exception as exc:
                last_error = str(exc)
        return False, f"ERROR opening app {raw}: {last_error}", {"app": raw}

    def _search_files_local(self, pattern: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(pattern or "").strip()
        normalized_root = os.path.expanduser(str(root or "~"))
        if not query:
            return False, "ERROR searching files: empty_pattern", {"pattern": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR searching files: invalid_root {normalized_root}", {"pattern": query, "root": normalized_root}
        query_low = query.lower()
        matches: List[str] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
                for name in filenames:
                    full_path = os.path.join(current_root, name)
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

    def _select_file_for_active_dialog_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not normalized or not os.path.exists(normalized):
            return False, f"ERROR selecting file {normalized}: file_not_found", {"path": normalized}
        ok, detail, _payload = self._copy_to_clipboard_local(normalized)
        if not ok:
            return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        try:
            first = subprocess.run(
                ["xdotool", "key", "--clearmodifiers", "ctrl+l"],
                capture_output=True,
                text=True,
                timeout=6.0,
                env=self.env,
                check=False,
            )
            if first.returncode != 0:
                detail = (first.stderr or first.stdout or "").strip() or f"exit_{first.returncode}"
                return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
            time.sleep(0.18)
            ok, detail, _payload = self._paste_clipboard_local()
            if not ok:
                return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
            time.sleep(0.18)
            confirm = subprocess.run(
                ["xdotool", "key", "--clearmodifiers", "Return"],
                capture_output=True,
                text=True,
                timeout=6.0,
                env=self.env,
                check=False,
            )
            if confirm.returncode != 0:
                detail = (confirm.stderr or confirm.stdout or "").strip() or f"exit_{confirm.returncode}"
                return False, f"ERROR selecting file {normalized}: {detail}", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR selecting file {normalized}: {exc}", {"path": normalized}
        return True, f"Selected file in active dialog: {normalized}", {"path": normalized}

    def _upload_file_to_active_app_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not os.path.isfile(normalized):
            return False, f"ERROR uploading file {normalized}: file_not_found", {"path": normalized}
        ok, detail, payload = self._select_file_for_active_dialog_local(normalized)
        if not ok:
            return False, f"ERROR uploading file {normalized}: {detail}", {"path": normalized}
        return True, f"Selected file for active app upload: {normalized}", dict(payload)

    def _drag_file_to_target_local(self, path: str, x: int, y: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not normalized or not os.path.exists(normalized):
            return False, f"ERROR dragging file {normalized}: file_not_found", {"path": normalized, "x": int(x), "y": int(y)}
        command = shutil.which("thunar")
        try:
            if command:
                proc = subprocess.Popen(
                    [command, "--select", normalized],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self.env,
                    start_new_session=True,
                )
                time.sleep(0.35)
                status = proc.poll()
                if status not in {None, 0}:
                    stdout, stderr = proc.communicate(timeout=1.0)
                    detail = (stderr or stdout or "").strip() or f"exit_{status}"
                    return False, f"ERROR dragging file {normalized}: {detail}", {"path": normalized, "x": int(x), "y": int(y)}
            else:
                ok, detail, _payload = self._reveal_path_local(normalized)
                if not ok:
                    return False, f"ERROR dragging file {normalized}: {detail}", {"path": normalized, "x": int(x), "y": int(y)}
            time.sleep(0.8)
            self._focus_window_local("thunar")
            geometry = {"x": 180, "y": 180, "width": 900, "height": 700}
            result = subprocess.run(
                ["bash", "-lc", "xdotool getactivewindow getwindowgeometry --shell"],
                capture_output=True,
                text=True,
                timeout=6.0,
                env=self.env,
                check=False,
            )
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip().lower()
                    if key in geometry:
                        geometry[key] = int(value.strip())
            src_x = int(geometry["x"]) + max(80, min(220, int(geometry["width"]) // 3))
            src_y = int(geometry["y"]) + max(120, min(220, int(geometry["height"]) // 4))
            drag = subprocess.run(
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
                capture_output=True,
                text=True,
                timeout=10.0,
                env=self.env,
                check=False,
            )
            if drag.returncode != 0:
                detail = (drag.stderr or drag.stdout or "").strip() or f"exit_{drag.returncode}"
                return False, f"ERROR dragging file {normalized}: {detail}", {
                    "path": normalized,
                    "x": int(x),
                    "y": int(y),
                    "source_x": src_x,
                    "source_y": src_y,
                }
        except Exception as exc:
            return False, f"ERROR dragging file {normalized}: {exc}", {"path": normalized, "x": int(x), "y": int(y)}
        return True, f"Dragged file toward target: {normalized}", {
            "path": normalized,
            "x": int(x),
            "y": int(y),
            "source_x": src_x,
            "source_y": src_y,
        }

    def _resolve_app_command_local(self, app_name: str) -> List[str]:
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

    def _is_protected_path_local(self, normalized: str) -> bool:
        value = os.path.abspath(str(normalized or "").strip())
        protected = {
            "/",
            os.path.abspath(os.path.expanduser("~")),
            os.path.abspath(self.workspace_dir) if getattr(self, "workspace_dir", None) else "",
            "/opt",
            "/opt/ariaos",
            "/opt/aria-client",
            "/tmp",
        }
        return value in {item for item in protected if item}

    def _list_windows_local(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = subprocess.run(
                ["wmctrl", "-lx"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR listing windows: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing windows: {detail}", {}
        windows: List[Dict[str, Any]] = []
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

    def _make_dir_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            os.makedirs(normalized, exist_ok=True)
        except Exception as exc:
            return False, f"ERROR creating directory {normalized}: {exc}", {"path": normalized}
        return True, f"Created directory: {normalized}", {"path": normalized}

    def _move_path_local(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = os.path.expanduser(str(src or "").strip())
        target = os.path.expanduser(str(dst or "").strip())
        if not os.path.exists(source):
            return False, f"ERROR moving path {source}: path_not_found", {"src": source, "dst": target}
        if self._is_protected_path_local(source):
            return False, f"ERROR moving path {source}: protected_path", {"src": source, "dst": target}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(source, target)
        except Exception as exc:
            return False, f"ERROR moving path {source} -> {target}: {exc}", {"src": source, "dst": target}
        return True, f"Moved path: {source} -> {target}", {"src": source, "dst": target}

    def _copy_path_local(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = os.path.expanduser(str(src or "").strip())
        target = os.path.expanduser(str(dst or "").strip())
        if not os.path.exists(source):
            return False, f"ERROR copying path {source}: path_not_found", {"src": source, "dst": target}
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

    def _delete_path_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not os.path.exists(normalized):
            return False, f"ERROR deleting path {normalized}: path_not_found", {"path": normalized}
        if self._is_protected_path_local(normalized):
            return False, f"ERROR deleting path {normalized}: protected_path", {"path": normalized}
        try:
            if os.path.isdir(normalized) and not os.path.islink(normalized):
                shutil.rmtree(normalized)
            else:
                os.remove(normalized)
        except Exception as exc:
            return False, f"ERROR deleting path {normalized}: {exc}", {"path": normalized}
        return True, f"Deleted path: {normalized}", {"path": normalized}

    def _append_file_local(self, path: str, content: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _replace_in_file_local(self, path: str, search: str, replace: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _tail_file_local(self, path: str, lines: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _read_path_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not os.path.exists(normalized):
            return False, f"ERROR reading path {normalized}: path_not_found", {"path": normalized}
        if os.path.isdir(normalized):
            try:
                items = sorted(os.listdir(normalized))
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

    def _open_with_local(self, path: str, app_name: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        command = self._resolve_app_command_local(app_name)
        if not os.path.exists(normalized):
            return False, f"ERROR opening file {normalized} with {app_name}: path_not_found", {"path": normalized, "app": app_name}
        if not command:
            return False, f"ERROR opening file {normalized}: empty_app_name", {"path": normalized, "app": app_name}
        try:
            proc = subprocess.Popen(
                command + [normalized],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                start_new_session=True,
            )
            time.sleep(0.35)
            status = proc.poll()
            if status is None:
                return True, f"Opened {normalized} with {app_name}", {"path": normalized, "app": app_name, "command": command}
            stdout, stderr = proc.communicate(timeout=1.0)
            if status == 0:
                return True, f"Opened {normalized} with {app_name}", {"path": normalized, "app": app_name, "command": command}
            detail = (stderr or stdout or "").strip() or f"exit_{status}"
            return False, f"ERROR opening file {normalized} with {app_name}: {detail}", {"path": normalized, "app": app_name}
        except Exception as exc:
            return False, f"ERROR opening file {normalized} with {app_name}: {exc}", {"path": normalized, "app": app_name}

    def _read_clipboard_image_local(self) -> tuple[bool, str, dict[str, Any]]:
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
                    timeout=6,
                    env=self.env,
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
            tmp = tempfile.NamedTemporaryFile(prefix="aria_clipboard_", suffix=suffix, delete=False)
            try:
                tmp.write(result.stdout)
                tmp.close()
            except Exception:
                tmp.close()
                raise
            payload: Dict[str, Any] = {"path": tmp.name, "mime": mime, "size_bytes": len(result.stdout)}
            if Image is not None:
                try:
                    with Image.open(tmp.name) as img:
                        payload["width"] = int(img.width)
                        payload["height"] = int(img.height)
                except Exception:
                    pass
            return True, f"Saved clipboard image to {tmp.name}", payload
        return False, f"ERROR reading clipboard image: {last_error}", {}

    def _truncate_text_local(self, text: str, limit: int = 200000) -> tuple[str, bool]:
        value = str(text or "")
        if len(value) <= limit:
            return value, False
        return value[:limit], True

    def _find_marker_upward_local(self, start: str, marker: str) -> Optional[str]:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        while True:
            if (current / marker).exists():
                return str(current)
            if current.parent == current:
                return None
            current = current.parent

    def _extract_pdf_text_fallback_local(self, raw: bytes) -> str:
        snippets: List[str] = []

        def _decode_pdf_literal(data: bytes) -> str:
            text = data.decode("latin-1", errors="ignore")
            text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
            text = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)
            return text

        for found in re.findall(rb"\((.*?)\)\s*Tj", raw, flags=re.DOTALL):
            snippets.append(_decode_pdf_literal(found))
        for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, flags=re.DOTALL):
            candidates = [stream]
            try:
                candidates.append(zlib.decompress(stream))
            except Exception:
                pass
            for candidate in candidates:
                for found in re.findall(rb"\((.*?)\)\s*Tj", candidate, flags=re.DOTALL):
                    snippets.append(_decode_pdf_literal(found))
                for block in re.findall(rb"\[(.*?)\]\s*TJ", candidate, flags=re.DOTALL):
                    for found in re.findall(rb"\((.*?)\)", block, flags=re.DOTALL):
                        snippets.append(_decode_pdf_literal(found))
        cleaned = [item.strip() for item in snippets if item and item.strip()]
        return "\n".join(cleaned)

    def _detect_test_command_local(self, target: str) -> tuple[Optional[List[str]], str]:
        normalized = os.path.expanduser(str(target or "~"))
        start = normalized if os.path.exists(normalized) else os.path.expanduser("~")

        pytest_root = self._find_marker_upward_local(start, "pytest.ini") or self._find_marker_upward_local(start, "pyproject.toml")
        if pytest_root:
            if os.path.exists(normalized):
                return ["pytest", "-q", normalized], pytest_root
            return ["pytest", "-q"], pytest_root

        package_root = self._find_marker_upward_local(start, "package.json")
        if package_root:
            return ["npm", "test"], package_root

        cargo_root = self._find_marker_upward_local(start, "Cargo.toml")
        if cargo_root:
            return ["cargo", "test"], cargo_root

        go_root = self._find_marker_upward_local(start, "go.mod")
        if go_root:
            return ["go", "test", "./..."], go_root

        return None, os.path.expanduser("~")

    def _replace_regex_in_file_local(self, path: str, pattern: str, replace: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _grep_in_files_local(self, pattern: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(pattern or "").strip()
        normalized_root = os.path.expanduser(str(root or "~"))
        if not query:
            return False, "ERROR grepping files: empty_pattern", {"pattern": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR grepping files: invalid_root {normalized_root}", {"pattern": query, "root": normalized_root}
        try:
            regex = re.compile(query, flags=re.IGNORECASE)
            use_regex = True
        except re.error:
            regex = None
            use_regex = False
            query_low = query.lower()
        matches: List[Dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
                for name in filenames:
                    full_path = os.path.join(current_root, name)
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as handle:
                            for line_number, line in enumerate(handle, start=1):
                                matched = bool(regex.search(line)) if use_regex else (query_low in line.lower())
                                if matched:
                                    matches.append({"path": full_path, "line": line_number, "text": line.rstrip("\n")[:400]})
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
        return True, f"Found {len(matches)} grep match(es) in {normalized_root}", {"pattern": query, "root": normalized_root, "matches": matches}

    def _stat_path_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _diff_paths_local(self, a: str, b: str) -> tuple[bool, str, dict[str, Any]]:
        left = os.path.expanduser(str(a or "").strip())
        right = os.path.expanduser(str(b or "").strip())
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
        diff = "\n".join(difflib.unified_diff(left_text, right_text, fromfile=left, tofile=right, lineterm=""))
        compact, truncated = self._truncate_text_local(diff, 200000)
        return True, f"Diffed files: {left} vs {right}", {"a": left, "b": right, "kind": "file", "diff": compact, "truncated": truncated}

    def _list_processes_local(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="],
                capture_output=True,
                text=True,
                timeout=8,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR listing processes: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing processes: {detail}", {}
        processes: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            processes.append({"pid": int(parts[0]), "comm": parts[1], "args": parts[2] if len(parts) > 2 else ""})
            if len(processes) >= 200:
                break
        return True, f"Listed {len(processes)} process(es)", {"processes": processes}

    def _kill_process_local(self, pid_or_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(pid_or_name or "").strip()
        if not raw:
            return False, "ERROR killing process: empty_target", {"target": raw}
        protected = {1, os.getpid(), os.getppid()}
        killed: List[int] = []
        try:
            if raw.isdigit():
                pid = int(raw)
                if pid in protected:
                    return False, f"ERROR killing process {pid}: protected_process", {"target": raw}
                os.kill(pid, 15)
                killed.append(pid)
            else:
                ok, detail, payload = self._list_processes_local()
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

    def _chmod_path_local(self, path: str, mode: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            parsed = int(str(mode or "").strip(), 8)
            os.chmod(normalized, parsed)
        except Exception as exc:
            return False, f"ERROR chmod path {normalized}: {exc}", {"path": normalized, "mode": str(mode or "")}
        return True, f"Changed mode for {normalized} to {oct(parsed)}", {"path": normalized, "mode": oct(parsed)}

    def _symlink_path_local(self, src: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = os.path.expanduser(str(src or "").strip())
        target = os.path.expanduser(str(dst or "").strip())
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

    def _unzip_path_local(self, path: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = os.path.expanduser(str(path or "").strip())
        target = os.path.expanduser(str(dst or "").strip())
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

    def _archive_path_local(self, path: str, dst: str) -> tuple[bool, str, dict[str, Any]]:
        source = os.path.expanduser(str(path or "").strip())
        target = os.path.expanduser(str(dst or "").strip())
        if not os.path.exists(source):
            return False, f"ERROR archiving path {source}: path_not_found", {"path": source, "dst": target}
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

    def _read_json_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            data = json.loads(Path(normalized).read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return False, f"ERROR reading JSON {normalized}: {exc}", {"path": normalized}
        preview, truncated = self._truncate_text_local(json.dumps(data, indent=2, ensure_ascii=False), 200000)
        return True, f"Read JSON: {normalized}", {"path": normalized, "preview": preview, "truncated": truncated}

    def _write_json_local(self, path: str, data: Any) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            Path(normalized).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR writing JSON {normalized}: {exc}", {"path": normalized}
        return True, f"Wrote JSON: {normalized}", {"path": normalized}

    def _read_csv_preview_local(self, path: str, rows: int) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _git_status_local(self, repo: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(repo or "~").strip())
        try:
            result = subprocess.run(
                ["git", "-C", normalized, "status", "--short", "--branch"],
                capture_output=True,
                text=True,
                timeout=15,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR git status {normalized}: {exc}", {"repo": normalized}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR git status {normalized}: {detail}", {"repo": normalized}
        output, truncated = self._truncate_text_local(result.stdout or "", 100000)
        return True, f"Git status: {normalized}", {"repo": normalized, "output": output, "truncated": truncated}

    def _git_diff_local(self, repo: str, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized_repo = os.path.expanduser(str(repo or "~").strip())
        cmd = ["git", "-C", normalized_repo, "diff"]
        target = str(path or "").strip()
        if target:
            cmd.extend(["--", target])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR git diff {normalized_repo}: {exc}", {"repo": normalized_repo, "path": target}
        if result.returncode not in (0, 1):
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR git diff {normalized_repo}: {detail}", {"repo": normalized_repo, "path": target}
        output, truncated = self._truncate_text_local(result.stdout or "", 150000)
        return True, f"Git diff: {normalized_repo}", {"repo": normalized_repo, "path": target, "output": output, "truncated": truncated}

    def _run_tests_local(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        command, cwd = self._detect_test_command_local(target)
        if not command:
            return False, f"ERROR running tests for {target}: no_test_runner_detected", {"target": target}
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                env=self.env,
                cwd=cwd,
            )
        except Exception as exc:
            return False, f"ERROR running tests in {cwd}: {exc}", {"target": target, "cwd": cwd, "command": command}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        compact, truncated = self._truncate_text_local(output, 150000)
        ok = result.returncode == 0
        detail = f"Ran tests in {cwd} (exit {result.returncode})"
        return ok, detail, {"target": target, "cwd": cwd, "command": command, "output": compact, "truncated": truncated}

    def _download_file_local(self, url: str, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized_url = str(url or "").strip()
        normalized_path = os.path.expanduser(str(path or "").strip())
        if not re.match(r"^https?://", normalized_url, re.IGNORECASE):
            return False, f"ERROR downloading file {normalized_url}: invalid_url", {"url": normalized_url, "path": normalized_path}
        try:
            parent = os.path.dirname(normalized_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with urllib.request.urlopen(normalized_url, timeout=30) as response:
                data = response.read()
            Path(normalized_path).write_bytes(data)
        except Exception as exc:
            return False, f"ERROR downloading file {normalized_url}: {exc}", {"url": normalized_url, "path": normalized_path}
        return True, f"Downloaded file: {normalized_url} -> {normalized_path}", {"url": normalized_url, "path": normalized_path, "size_bytes": len(data)}

    def _extract_text_from_pdf_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not os.path.exists(normalized):
            return False, f"ERROR extracting PDF text {normalized}: path_not_found", {"path": normalized}
        try:
            result = subprocess.run(
                ["pdftotext", normalized, "-"],
                capture_output=True,
                text=True,
                timeout=20,
                env=self.env,
            )
            if result.returncode == 0 and (result.stdout or "").strip():
                text, truncated = self._truncate_text_local(result.stdout, 200000)
                return True, f"Extracted text from PDF: {normalized}", {"path": normalized, "text": text, "truncated": truncated}
        except Exception:
            pass
        try:
            raw = Path(normalized).read_bytes()
            text = self._extract_pdf_text_fallback_local(raw)
            if text.strip():
                compact, truncated = self._truncate_text_local(text, 200000)
                return True, f"Extracted text from PDF: {normalized}", {"path": normalized, "text": compact, "truncated": truncated}
        except Exception as exc:
            return False, f"ERROR extracting PDF text {normalized}: {exc}", {"path": normalized}
        return False, f"ERROR extracting PDF text {normalized}: no_text_backend_or_text_found", {"path": normalized}

    def _replace_block_in_file_local(self, path: str, start_marker: str, end_marker: str, content: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _grep_ast_local(self, symbol: str, root: str) -> tuple[bool, str, dict[str, Any]]:
        query = str(symbol or "").strip()
        normalized_root = os.path.expanduser(str(root or "~"))
        if not query:
            return False, "ERROR grepping AST: empty_symbol", {"symbol": query, "root": normalized_root}
        if not os.path.isdir(normalized_root):
            return False, f"ERROR grepping AST: invalid_root {normalized_root}", {"symbol": query, "root": normalized_root}
        matches: List[Dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
                for name in filenames:
                    full_path = os.path.join(current_root, name)
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
                                if re.search(rf"\b{re.escape(query)}\b", line):
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

    def _truncate_text_local2(self, text: str, limit: int = 200000) -> tuple[str, bool]:
        value = str(text or "")
        if len(value) <= limit:
            return value, False
        return value[:limit], True

    def _read_yaml_basic_local(self, normalized: str) -> tuple[bool, Any, str]:
        text = Path(normalized).read_text(encoding="utf-8", errors="replace")
        try:
            return True, json.loads(text), "json"
        except Exception:
            pass
        data: Dict[str, Any] = {}
        current_list_key: Optional[str] = None
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

    def _write_yaml_basic_local(self, data: Any) -> str:
        if yaml is not None:
            return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _read_yaml_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            if yaml is not None:
                data = yaml.safe_load(Path(normalized).read_text(encoding="utf-8", errors="replace"))
                mode = "yaml"
            else:
                ok, data, mode = self._read_yaml_basic_local(normalized)
                if not ok:
                    return False, f"ERROR reading YAML {normalized}: parse_failed", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR reading YAML {normalized}: {exc}", {"path": normalized}
        preview, truncated = self._truncate_text_local2(json.dumps(data, indent=2, ensure_ascii=False), 200000)
        return True, f"Read YAML: {normalized}", {"path": normalized, "data": data, "preview": preview, "truncated": truncated, "parser": mode}

    def _write_yaml_local(self, path: str, data: Any) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            Path(normalized).write_text(self._write_yaml_basic_local(data), encoding="utf-8")
        except Exception as exc:
            return False, f"ERROR writing YAML {normalized}: {exc}", {"path": normalized}
        return True, f"Wrote YAML: {normalized}", {"path": normalized, "parser": "yaml" if yaml is not None else "json_fallback"}

    def _list_ports_local(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            result = subprocess.run(["ss", "-ltnupH"], capture_output=True, text=True, timeout=8, env=self.env)
        except Exception as exc:
            return False, f"ERROR listing ports: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR listing ports: {detail}", {}
        ports: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[4]
            host = local
            port: Optional[int] = None
            match = re.search(r":(\d+)$", local)
            if match:
                port = int(match.group(1))
                host = local[: match.start()] or local
            ports.append({"proto": parts[0], "state": parts[1], "local": local, "host": host, "port": port, "process": parts[-1] if parts else ""})
            if len(ports) >= 200:
                break
        return True, f"Listed {len(ports)} listening port(s)", {"ports": ports}

    def _process_tree_local(self, pid_or_name: str) -> tuple[bool, str, dict[str, Any]]:
        raw = str(pid_or_name or "").strip()
        try:
            result = subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="], capture_output=True, text=True, timeout=8, env=self.env)
        except Exception as exc:
            return False, f"ERROR building process tree: {exc}", {"target": raw}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR building process tree: {detail}", {"target": raw}
        processes: Dict[int, Dict[str, Any]] = {}
        children: Dict[int, List[int]] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            pid = int(parts[0]); ppid = int(parts[1]); comm = parts[2]; args = parts[3] if len(parts) > 3 else ""
            processes[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "args": args}
            children.setdefault(ppid, []).append(pid)
        root_pids: List[int] = []
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
        tree: List[Dict[str, Any]] = []
        stack = [(pid, 0) for pid in root_pids[:20]]
        while stack:
            pid, depth = stack.pop(0)
            if pid in seen or pid not in processes:
                continue
            seen.add(pid)
            proc = dict(processes[pid]); proc["depth"] = depth
            tree.append(proc)
            for child in children.get(pid, [])[:100]:
                stack.append((child, depth + 1))
            if len(tree) >= 300:
                break
        return True, f"Built process tree for {raw}", {"target": raw, "tree": tree, "processes": tree}

    def _disk_usage_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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
                    dir_count += len(dirnames)
                    for name in filenames:
                        full_path = os.path.join(current_root, name)
                        try:
                            total += os.path.getsize(full_path); file_count += 1
                        except Exception:
                            continue
        except Exception as exc:
            return False, f"ERROR disk usage {normalized}: {exc}", {"path": normalized}
        return True, f"Computed disk usage for {normalized}", {"path": normalized, "size_bytes": total, "bytes": total, "size_mb": round(total / (1024 * 1024), 3), "files": file_count, "dirs": dir_count}

    def _find_large_files_local(self, root: str, limit_mb: float) -> tuple[bool, str, dict[str, Any]]:
        normalized_root = os.path.expanduser(str(root or "~"))
        threshold = max(float(limit_mb or 1), 0.001) * 1024 * 1024
        if not os.path.isdir(normalized_root):
            return False, f"ERROR finding large files: invalid_root {normalized_root}", {"root": normalized_root}
        matches: List[Dict[str, Any]] = []
        try:
            for current_root, dirnames, filenames in os.walk(normalized_root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
                for name in filenames:
                    full_path = os.path.join(current_root, name)
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

    def _local_env_file_path(self) -> str:
        return os.path.expanduser("~/.ariaos/local_env.json")

    def _load_local_env_map(self) -> Dict[str, str]:
        try:
            data = json.loads(Path(self._local_env_file_path()).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_local_env_map(self, data: Dict[str, str]) -> None:
        path = Path(self._local_env_file_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _env_get_local(self, name: str) -> tuple[bool, str, dict[str, Any]]:
        key = str(name or "").strip()
        if not key:
            return False, "ERROR env_get: empty_name", {"name": key}
        value = self.env.get(key, os.environ.get(key))
        if value is None:
            local = self._load_local_env_map()
            value = local.get(key)
        if value is None:
            return False, f"ERROR env_get {key}: not_set", {"name": key}
        return True, f"Read environment variable: {key}", {"name": key, "value": str(value)}

    def _env_set_local(self, name: str, value: str) -> tuple[bool, str, dict[str, Any]]:
        key = str(name or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            return False, f"ERROR env_set_local {key}: invalid_name", {"name": key}
        str_value = str(value or "")
        self.env[key] = str_value
        os.environ[key] = str_value
        local = self._load_local_env_map()
        local[key] = str_value
        try:
            self._save_local_env_map(local)
        except Exception as exc:
            return False, f"ERROR env_set_local {key}: {exc}", {"name": key}
        return True, f"Set local environment variable: {key}", {"name": key, "value": str_value}

    def _http_request_local(self, method: str, url: str, headers: Any, body: Any) -> tuple[bool, str, dict[str, Any]]:
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
            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read()
                status = getattr(response, "status", 200)
                resp_headers = dict(response.getheaders())
        except Exception as exc:
            return False, f"ERROR http_request {normalized_url}: {exc}", {"url": normalized_url, "method": http_method}
        text = response_body.decode("utf-8", errors="replace")
        preview, truncated = self._truncate_text_local2(text, 200000)
        return True, f"HTTP {http_method} {normalized_url} -> {status}", {"url": normalized_url, "method": http_method, "status": status, "headers": resp_headers, "body": preview, "text": preview, "truncated": truncated}

    def _sqlite_query_local(self, path: str, sql: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
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

    def _read_json_schema_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            data = json.loads(Path(normalized).read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return False, f"ERROR reading JSON schema {normalized}: {exc}", {"path": normalized}
        schema = self._schema_for_value_local(data)
        preview, truncated = self._truncate_text_local2(json.dumps(schema, indent=2, ensure_ascii=False), 200000)
        return True, f"Read JSON schema: {normalized}", {"path": normalized, "schema": schema, "preview": preview, "truncated": truncated}

    def _read_yaml_schema_local(self, path: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        try:
            if yaml is not None:
                data = yaml.safe_load(Path(normalized).read_text(encoding="utf-8", errors="replace"))
            else:
                ok, data, _mode = self._read_yaml_basic_local(normalized)
                if not ok:
                    return False, f"ERROR reading YAML schema {normalized}: parse_failed", {"path": normalized}
        except Exception as exc:
            return False, f"ERROR reading YAML schema {normalized}: {exc}", {"path": normalized}
        schema = self._schema_for_value_local(data)
        preview, truncated = self._truncate_text_local2(json.dumps(schema, indent=2, ensure_ascii=False), 200000)
        return True, f"Read YAML schema: {normalized}", {"path": normalized, "schema": schema, "preview": preview, "truncated": truncated}

    def _docker_ps_local(self) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("docker", path=self.env.get("PATH")) is None:
            return False, "ERROR docker_ps: docker_not_installed", {}
        try:
            result = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"], capture_output=True, text=True, timeout=20, env=self.env)
        except Exception as exc:
            return False, f"ERROR docker_ps: {exc}", {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR docker_ps: {detail}", {}
        containers: List[Dict[str, Any]] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                containers.append(json.loads(line))
            except Exception:
                containers.append({"raw": line})
        return True, f"Listed {len(containers)} container(s)", {"containers": containers}

    def _docker_logs_local(self, container: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(container or "").strip()
        if not target:
            return False, "ERROR docker_logs: empty_container", {"container": target}
        if shutil.which("docker", path=self.env.get("PATH")) is None:
            return False, "ERROR docker_logs: docker_not_installed", {"container": target}
        try:
            result = subprocess.run(["docker", "logs", "--tail", "200", target], capture_output=True, text=True, timeout=30, env=self.env)
        except Exception as exc:
            return False, f"ERROR docker_logs {target}: {exc}", {"container": target}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_{result.returncode}"
            return False, f"ERROR docker_logs {target}: {detail}", {"container": target}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text_local2(output, 150000)
        return True, f"Docker logs: {target}", {"container": target, "output": preview, "truncated": truncated}

    def _systemctl_status_local(self, service: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(service or "").strip()
        if not target:
            return False, "ERROR systemctl_status: empty_service", {"service": target}
        attempts = [
            ("system", ["systemctl", "status", target, "--no-pager", "--full"]),
            ("user", ["systemctl", "--user", "status", target, "--no-pager", "--full"]),
        ]
        for mode, cmd in attempts:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=self.env)
            except Exception:
                continue
            if result.returncode == 0:
                output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
                preview, truncated = self._truncate_text_local2(output, 150000)
                active = ""
                for line in output.splitlines():
                    if "Active:" in line:
                        active = line.strip()
                        break
                return True, f"Systemctl status ({mode}): {target}", {"service": target, "mode": mode, "output": preview, "truncated": truncated, "active": active}
        return False, f"ERROR systemctl_status {target}: status_failed", {"service": target}

    def _systemctl_restart_local(self, service: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(service or "").strip()
        if not target:
            return False, "ERROR systemctl_restart: empty_service", {"service": target}
        attempts = [
            ("system", ["systemctl", "restart", target]),
            ("user", ["systemctl", "--user", "restart", target]),
            ("sudo", ["sudo", "-n", "systemctl", "restart", target]),
        ]
        errors: List[str] = []
        for mode, cmd in attempts:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=25, env=self.env)
            except Exception as exc:
                errors.append(f"{mode}:{exc}")
                continue
            if result.returncode == 0:
                status_ok, _status_detail, payload = self._systemctl_status_local(target)
                response: Dict[str, Any] = {"service": target, "mode": mode}
                if status_ok:
                    response.update(payload)
                return True, f"Restarted service ({mode}): {target}", response
            errors.append(f"{mode}:{(result.stderr or result.stdout or '').strip() or f'exit_{result.returncode}'}")
        return False, f"ERROR systemctl_restart {target}: {' | '.join(errors[:3])}", {"service": target}

    def _ffmpeg_run_local(self, args: Any) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("ffmpeg", path=self.env.get("PATH")) is None:
            return False, "ERROR ffmpeg_run: ffmpeg_not_installed", {}
        argv = [str(item) for item in args if str(item).strip()] if isinstance(args, list) else shlex.split(str(args or "").strip())
        if argv and argv[0] == "ffmpeg":
            argv = argv[1:]
        if not argv:
            return False, "ERROR ffmpeg_run: empty_args", {}
        try:
            result = subprocess.run(["ffmpeg", *argv], capture_output=True, text=True, timeout=240, env=self.env)
        except Exception as exc:
            return False, f"ERROR ffmpeg_run: {exc}", {"args": argv}
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text_local2(output, 200000)
        ok = result.returncode == 0
        return ok, f"ffmpeg run exit {result.returncode}", {"args": argv, "output": preview, "truncated": truncated}

    def _blender_render_local(self, script: str, output_path: str, size: Any) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("blender", path=self.env.get("PATH")) is None:
            return False, "ERROR blender_render: blender_not_installed", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_blender_render.png").strip())
        width, height = self._parse_image_size_local(size)
        image_format = self._image_format_for_path_local(normalized)
        user_script = str(script or "").strip()
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
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
            result = subprocess.run(
                ["blender", "--background", "--python", script_path],
                capture_output=True,
                text=True,
                timeout=420,
                env=self.env,
            )
        except Exception as exc:
            return False, f"ERROR blender_render: {exc}", {"path": normalized}
        finally:
            try:
                if script_path:
                    os.unlink(script_path)
            except Exception:
                pass
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text_local2(output, 200000)
        ok = result.returncode == 0 and os.path.exists(normalized)
        return ok, f"blender render exit {result.returncode}", {"path": normalized, "width": width, "height": height, "output": preview, "truncated": truncated}

    def _compose_video_from_images_local(self, inputs: Any, output_path: str, fps: float = 24.0) -> tuple[bool, str, dict[str, Any]]:
        if ImageSequenceClip is None:
            return False, "ERROR compose_video_from_images: moviepy_not_available", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_slideshow.mp4").strip())
        frames = self._resolve_image_sequence_inputs_local(inputs)
        if not frames:
            return False, "ERROR compose_video_from_images: no_input_images", {"path": normalized}
        frame_rate = max(1.0, min(float(fps or 24.0), 60.0))
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        try:
            clip = ImageSequenceClip(frames, fps=frame_rate)
            clip.write_videofile(normalized, codec="libx264", audio=False, fps=frame_rate, logger=None)
            clip.close()
        except Exception as exc:
            return False, f"ERROR compose_video_from_images: {exc}", {"path": normalized, "inputs": frames[:20]}
        return True, f"Composed video from {len(frames)} image(s)", {"path": normalized, "inputs": frames, "fps": frame_rate}

    def _generate_image_ai_local(self, prompt: str, output_path: str, size: str = "1024x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR generate_image_ai: empty_prompt", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_generated_ai.png").strip())
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        ok, detail, payload = self._openai_image_request_local(
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

    def _upscale_image_ai_local(self, input_path: str, output_path: str, size: str = "1536x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        ok_image, data_url, detail = self._image_data_url_local(input_path)
        if not ok_image:
            return False, f"ERROR upscale_image_ai: {detail}", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_upscaled_ai.png").strip())
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        prompt = (
            "Create a faithful higher-resolution upscale of this image. "
            "Preserve the composition, colors, text, layout, and subject as closely as possible. "
            "Do not add or remove objects. Improve sharpness and clarity only."
        )
        ok, detail_req, payload = self._openai_image_request_local(
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

    def _remove_background_ai_local(self, input_path: str, output_path: str, quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        ok_image, data_url, detail = self._image_data_url_local(input_path)
        if not ok_image:
            return False, f"ERROR remove_background_ai: {detail}", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_no_background.png").strip())
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        prompt = (
            "Remove the background completely and keep only the main foreground subject. "
            "Preserve the subject faithfully and return a transparent PNG."
        )
        ok, detail_req, payload = self._openai_image_request_local(
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

    def _edit_image_ai_local(self, prompt: str, input_paths: Any, output_path: str, mask_path: str = "", size: str = "1024x1024", quality: str = "low") -> tuple[bool, str, dict[str, Any]]:
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR edit_image_ai: empty_prompt", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_edited_ai.png").strip())
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        paths = input_paths if isinstance(input_paths, list) else [input_paths]
        images: List[Dict[str, str]] = []
        normalized_inputs: List[str] = []
        for item in paths:
            ok_data, data_url, detail = self._image_data_url_local(str(item or ""))
            if not ok_data:
                return False, f"ERROR edit_image_ai: {detail}", {}
            images.append({"image_url": data_url})
            normalized_inputs.append(detail)
        request_payload: Dict[str, Any] = {
            "model": "gpt-image-1",
            "prompt": value,
            "images": images,
            "size": str(size or "1024x1024"),
            "quality": str(quality or "low"),
        }
        if str(mask_path or "").strip():
            ok_mask, mask_url, detail = self._image_data_url_local(mask_path)
            if not ok_mask:
                return False, f"ERROR edit_image_ai: {detail}", {}
            request_payload["mask"] = {"image_url": mask_url}
        ok, detail, payload = self._openai_image_request_local("/images/edits", request_payload, timeout=240.0)
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

    def _extract_video_frames_local(self, path: str, output_dir: str, fps: float = 1.0, max_frames: int = 0) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("ffmpeg", path=self.env.get("PATH")) is None:
            return False, "ERROR extract_video_frames: ffmpeg_not_installed", {}
        source = os.path.expanduser(str(path or "").strip())
        if not os.path.isfile(source):
            return False, f"ERROR extract_video_frames: missing_video:{source}", {}
        target_dir = os.path.expanduser(str(output_dir or "/tmp/aria_video_frames").strip())
        os.makedirs(target_dir, exist_ok=True)
        pattern = str(Path(target_dir) / "frame_%05d.png")
        argv = ["ffmpeg", "-y", "-i", source, "-vf", f"fps={max(0.1, min(float(fps or 1.0), 60.0))}"]
        if int(max_frames or 0) > 0:
            argv.extend(["-frames:v", str(int(max_frames))])
        argv.append(pattern)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=240, env=self.env)
        except Exception as exc:
            return False, f"ERROR extract_video_frames: {exc}", {"path": source, "output_dir": target_dir}
        frames = sorted(glob.glob(str(Path(target_dir) / "frame_*.png")))
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        preview, truncated = self._truncate_text_local2(output, 120000)
        ok = result.returncode == 0 and bool(frames)
        return ok, f"Extracted {len(frames)} frame(s)", {"path": source, "output_dir": target_dir, "frames": frames, "output": preview, "truncated": truncated}

    def _storyboard_to_video_local(
        self,
        storyboard: Any,
        output_path: str,
        size: str = "1024x1024",
        quality: str = "low",
        fps: float = 12.0,
        seconds_per_scene: float = 2.0,
    ) -> tuple[bool, str, dict[str, Any]]:
        scenes = self._parse_storyboard_scenes_local(storyboard)
        if not scenes:
            return False, "ERROR storyboard_to_video: empty_storyboard", {}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_storyboard.mp4").strip())
        os.makedirs(os.path.dirname(normalized) or "/tmp", exist_ok=True)
        frame_rate = max(1.0, min(float(fps or 12.0), 30.0))
        hold_seconds = max(0.25, min(float(seconds_per_scene or 2.0), 10.0))
        repeat_count = max(1, int(round(frame_rate * hold_seconds)))
        with tempfile.TemporaryDirectory(prefix="aria_storyboard_") as tempdir:
            repeated_inputs: List[str] = []
            for index, scene in enumerate(scenes, start=1):
                frame_path = str(Path(tempdir) / f"scene_{index:03d}.png")
                ok_img, detail, _payload = self._generate_image_ai_local(scene, frame_path, size, quality)
                if not ok_img:
                    return False, f"ERROR storyboard_to_video scene_{index}: {detail}", {"scene": scene}
                repeated_inputs.extend([frame_path] * repeat_count)
            ok_vid, detail, payload = self._compose_video_from_images_local(repeated_inputs, normalized, frame_rate)
            if not ok_vid:
                return False, f"ERROR storyboard_to_video: {detail}", payload
        result_payload = dict(payload or {})
        result_payload.update({"path": normalized, "scene_count": len(scenes), "fps": frame_rate, "seconds_per_scene": hold_seconds})
        return True, f"Storyboard video created from {len(scenes)} scene(s)", result_payload

    def _generate_image_local(self, prompt: str, size: Any, output_path: str) -> tuple[bool, str, dict[str, Any]]:
        if Image is None or ImageDraw is None:
            return False, "ERROR generate_image: pillow_not_available", {"path": output_path}
        value = str(prompt or "").strip()
        if not value:
            return False, "ERROR generate_image: empty_prompt", {"path": output_path}
        normalized = os.path.expanduser(str(output_path or "/tmp/aria_generated_image.png").strip())
        width, height = self._parse_image_size_local(size)
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
            words = value.split()
            wrapped: List[str] = []
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

    def _ocr_region_local(self, x: int, y: int, w: int, h: int) -> tuple[bool, str, dict[str, Any]]:
        if shutil.which("tesseract", path=self.env.get("PATH")) is None:
            return False, "ERROR ocr_region: tesseract_not_installed", {"region": [x, y, w, h]}
        ok, crop_path, detail = self._capture_region_image_local(x, y, w, h)
        if not ok:
            return False, f"ERROR ocr_region: {detail}", {"region": [x, y, w, h]}
        try:
            result = subprocess.run(["tesseract", crop_path, "stdout", "--psm", "6"], capture_output=True, text=True, timeout=20, env=self.env)
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
        preview, truncated = self._truncate_text_local2(text, 50000)
        return True, "OCR region captured", {"region": [x, y, w, h], "text": preview, "truncated": truncated}

    def _list_downloads_local(self) -> tuple[bool, str, dict[str, Any]]:
        downloads = Path.home() / "Downloads"
        if not downloads.exists():
            return True, "Downloads directory missing", {"path": str(downloads), "entries": []}
        entries: List[Dict[str, Any]] = []
        for child in sorted(downloads.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)[:200]:
            try:
                stat = child.stat()
            except Exception:
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir(), "size_bytes": int(stat.st_size), "mtime": round(float(stat.st_mtime), 6)})
        return True, f"Listed {len(entries)} download item(s)", {"path": str(downloads), "entries": entries}

    def _watch_file_local(self, path: str, seconds: float = 5.0) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        timeout_s = max(0.5, min(float(seconds or 5.0), 30.0))
        initial = self._snapshot_file_state_local(normalized)
        changed = False
        deadline = time.time() + timeout_s
        final = initial
        while time.time() < deadline:
            time.sleep(0.5)
            final = self._snapshot_file_state_local(normalized)
            if final != initial:
                changed = True
                break
        return True, f"Watched file for {round(timeout_s, 2)}s", {"path": normalized, "changed": changed, "initial": initial, "final": final, "seconds": timeout_s}

    def _watch_dir_local(self, path: str, seconds: float = 5.0) -> tuple[bool, str, dict[str, Any]]:
        normalized = os.path.expanduser(str(path or "").strip())
        timeout_s = max(0.5, min(float(seconds or 5.0), 30.0))
        initial = self._snapshot_dir_state_local(normalized)
        changed = False
        deadline = time.time() + timeout_s
        final = initial
        while time.time() < deadline:
            time.sleep(0.5)
            final = self._snapshot_dir_state_local(normalized)
            if final != initial:
                changed = True
                break
        return True, f"Watched directory for {round(timeout_s, 2)}s", {"path": normalized, "changed": changed, "initial": initial, "final": final, "seconds": timeout_s}

    def _schema_type_name_local(self, value: Any) -> str:
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

    def _schema_for_value_local(self, value: Any, depth: int = 0) -> Dict[str, Any]:
        if depth >= 4:
            return {"type": self._schema_type_name_local(value)}
        if isinstance(value, dict):
            props: Dict[str, Any] = {}
            required: List[str] = []
            for index, (key, item) in enumerate(value.items()):
                if index >= 50:
                    break
                key_str = str(key)
                props[key_str] = self._schema_for_value_local(item, depth + 1)
                required.append(key_str)
            return {"type": "object", "property_count": len(value), "required": required, "properties": props}
        if isinstance(value, list):
            item_schemas = [self._schema_for_value_local(item, depth + 1) for item in value[:5]]
            item_types = sorted({schema.get("type", "unknown") for schema in item_schemas}) if item_schemas else []
            return {"type": "array", "length": len(value), "item_types": item_types, "items": item_schemas[0] if item_schemas else {"type": "unknown"}}
        node: Dict[str, Any] = {"type": self._schema_type_name_local(value)}
        if value is not None and node["type"] in {"string", "number", "integer", "boolean"}:
            node["sample"] = str(value)[:120]
        return node

    def _parse_image_size_local(self, size: Any) -> tuple[int, int]:
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            try:
                return max(128, min(int(size[0]), 2048)), max(128, min(int(size[1]), 2048))
            except Exception:
                pass
        raw = str(size or "").strip().lower()
        aliases = {"square": (1024, 1024), "portrait": (768, 1024), "landscape": (1024, 768)}
        if raw in aliases:
            return aliases[raw]
        match = re.match(r"^(\d{2,4})x(\d{2,4})$", raw)
        if match:
            return max(128, min(int(match.group(1)), 2048)), max(128, min(int(match.group(2)), 2048))
        return 1024, 1024

    def _image_format_for_path_local(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            return "JPEG"
        if ext == ".bmp":
            return "BMP"
        if ext == ".webp":
            return "WEBP"
        return "PNG"

    def _openai_image_base_url_local(self) -> str:
        return str(os.environ.get("OPENAI_IMAGE_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    def _openai_image_request_local(self, endpoint: str, payload: Dict[str, Any], timeout: float = 180.0) -> tuple[bool, str, Dict[str, Any]]:
        api_key = str(self.openai_api_key or OPENAI_API_KEY or "").strip()
        if not api_key:
            return False, "ERROR image_ai: missing_openai_api_key", {}
        url = f"{self._openai_image_base_url_local()}{endpoint}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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

    def _image_data_url_local(self, path: str) -> tuple[bool, str, str]:
        normalized = os.path.expanduser(str(path or "").strip())
        if not os.path.isfile(normalized):
            return False, "", f"missing_file:{normalized}"
        mime, _encoding = mimetypes.guess_type(normalized)
        mime = mime or "image/png"
        try:
            raw = Path(normalized).read_bytes()
        except Exception as exc:
            return False, "", f"read_failed:{exc}"
        return True, f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", normalized

    def _parse_storyboard_scenes_local(self, storyboard: Any) -> List[str]:
        if isinstance(storyboard, list):
            return [str(item or "").strip() for item in storyboard if str(item or "").strip()]
        raw = str(storyboard or "").strip()
        if not raw:
            return []
        lines = [line.strip(" -\t") for line in raw.splitlines()]
        scenes = [line for line in lines if line]
        if len(scenes) > 1:
            return scenes
        return [raw]

    def _resolve_image_sequence_inputs_local(self, inputs: Any) -> List[str]:
        candidates: List[str] = []
        if isinstance(inputs, str):
            value = str(inputs).strip()
            if any(token in value for token in ["*", "?", "["]):
                candidates.extend(sorted(glob.glob(os.path.expanduser(value))))
            else:
                normalized = os.path.expanduser(value)
                if os.path.isdir(normalized):
                    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"):
                        candidates.extend(sorted(glob.glob(str(Path(normalized) / ext))))
                else:
                    candidates.append(normalized)
        elif isinstance(inputs, list):
            for item in inputs:
                value = str(item or "").strip()
                if value:
                    candidates.append(os.path.expanduser(value))
        filtered: List[str] = []
        seen: set[str] = set()
        for item in candidates:
            if not item or item in seen:
                continue
            if os.path.isfile(item):
                filtered.append(item)
                seen.add(item)
        return filtered

    def _capture_region_image_local(self, x: int, y: int, w: int, h: int) -> tuple[bool, str, str]:
        if Image is None:
            return False, "", "ocr_region requires Pillow"
        screenshot_path = "/tmp/aria_region_capture.png"
        commands = (
            ["scrot", screenshot_path, "-o"],
            ["import", "-window", "root", screenshot_path],
            ["gnome-screenshot", "-f", screenshot_path],
        )
        captured = False
        for cmd in commands:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=8, env=self.env)
                if result.returncode == 0 and os.path.exists(screenshot_path):
                    captured = True
                    break
            except Exception:
                continue
        if not captured:
            return False, "", "screenshot_capture_failed"
        try:
            with Image.open(screenshot_path) as img:
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
        finally:
            try:
                os.unlink(screenshot_path)
            except Exception:
                pass

    def _snapshot_file_state_local(self, normalized: str) -> Dict[str, Any]:
        if not os.path.exists(normalized):
            return {"exists": False}
        stat = os.stat(normalized)
        payload: Dict[str, Any] = {"exists": True, "size": stat.st_size, "mtime": round(stat.st_mtime, 6)}
        if os.path.isfile(normalized):
            try:
                payload["crc32"] = f"{zlib.crc32(Path(normalized).read_bytes()) & 0xffffffff:08x}"
            except Exception:
                pass
        return payload

    def _snapshot_dir_state_local(self, normalized: str) -> Dict[str, Any]:
        if not os.path.exists(normalized):
            return {"exists": False}
        entries: List[Dict[str, Any]] = []
        latest_mtime = 0.0
        for child in sorted(Path(normalized).iterdir(), key=lambda item: item.name.lower())[:200]:
            try:
                stat = child.stat()
            except Exception:
                continue
            latest_mtime = max(latest_mtime, float(stat.st_mtime))
            entries.append({"name": child.name, "is_dir": child.is_dir(), "size": int(stat.st_size), "mtime": round(float(stat.st_mtime), 6)})
        return {"exists": True, "entry_count": len(entries), "latest_mtime": round(latest_mtime, 6), "entries": entries}

    def _determine_target_focus_keywords(self) -> List[str]:
        if self.active_profile:
            configured = self.profile_manager.get_ui(
                self.active_profile,
                "focus_window_keywords",
                [],
            )
            if isinstance(configured, list):
                keywords = [str(k).strip().lower() for k in configured if str(k).strip()]
                if keywords:
                    return keywords

        low = self.goal_lower
        focus_map = [
            (("discord",), ["discord"]),
            (
                ("gmail", "mail.google", "linkedin", "tiktok", "chrome", "browser", "web", "http://", "https://", "www."),
                ["google-chrome", "chromium-browser", "chromium", "chrome"],
            ),
            (("terminal", "bash", "shell", "console"), ["xfce4-terminal", "terminal", "xterm", "gnome-terminal"]),
        ]
        for tokens, keywords in focus_map:
            if any(tok in low for tok in tokens):
                return list(keywords)
        return []

    def _active_window_name(self) -> str:
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=4,
                env=self.env,
            )
            if result.returncode != 0:
                return ""
            return str(result.stdout or "").strip()
        except Exception:
            return ""

    def _window_name_matches_keywords(self, window_name: str, keywords: List[str]) -> bool:
        low = (window_name or "").lower()
        return bool(low and keywords and any(k in low for k in keywords))

    def _focus_window_by_keywords(self, keywords: List[str]) -> bool:
        if not keywords:
            return False
        try:
            result = subprocess.run(
                ["wmctrl", "-lx"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self.env,
            )
            if result.returncode != 0:
                return False

            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            candidates: List[str] = []
            for line in lines:
                low = line.lower()
                if any(k in low for k in keywords):
                    candidates.append(line)
            if not candidates:
                return False

            # Avoid focusing Aria Terminal when target is another app.
            non_aria = [ln for ln in candidates if "aria terminal" not in ln.lower()]
            selected = (non_aria or candidates)[0]
            win_id = selected.split()[0]
            subprocess.run(["wmctrl", "-ia", win_id], timeout=5, env=self.env)
            return True
        except Exception as e:
            print(f"[Brain] Focus by keywords failed: {e}", flush=True)
            return False

    def _ensure_target_focus(self, force: bool = False) -> bool:
        keywords = self.target_focus_keywords or self._determine_target_focus_keywords()
        self.target_focus_keywords = keywords
        if not keywords:
            return True

        active_name = self._active_window_name()
        if not force and self._window_name_matches_keywords(active_name, keywords):
            return True

        focused = self._focus_window_by_keywords(keywords)
        if not focused:
            return False

        time.sleep(0.18)
        active_name = self._active_window_name()
        return self._window_name_matches_keywords(active_name, keywords)

    def _goal_needs_chrome_focus(self) -> bool:
        keywords = self.target_focus_keywords or self._determine_target_focus_keywords()
        if any("chrome" in k or "chromium" in k for k in keywords):
            return True
        if self.active_profile:
            keywords = self.profile_manager.get_ui(
                self.active_profile,
                "focus_keywords",
                ["gmail", "mail.google", "chrome", "email", "mail"],
            )
            return any(str(k).lower() in self.goal_lower for k in keywords)
        keywords = ("gmail", "mail.google", "chrome", "email", "mail")
        return any(k in self.goal_lower for k in keywords)

    def _goal_prefers_direct_response(self) -> bool:
        if self._direct_response_mode:
            return True
        low = self.goal_lower or ""
        direct_markers = (
            "reply exactly",
            "respond exactly",
            "answer exactly",
            "output exactly",
            "return exactly",
            "say exactly",
            "do not use tools",
            "don't use tools",
            "without tools",
            "no tools",
            "remember the phrase",
            "what phrase did i ask you to remember",
            "what did i ask you to remember",
            "reply with only",
            "answer with only",
            "respond with only",
            "one word only",
        )
        return any(marker in low for marker in direct_markers)

    def _goal_has_question_shape(self) -> bool:
        low = (self.goal_lower or "").strip()
        if not low:
            return False
        if "?" in low:
            return True
        starters = (
            "what ", "which ", "who ", "where ", "when ", "why ", "how ",
            "tell me ", "describe ", "analyze ", "analyse ", "read ", "summarize ",
            "do you remember ", "what did ", "what is ", "what color ",
            "quel ", "quelle ", "quels ", "quelles ", "que ", "quoi ",
            "ou ", "où ", "comment ", "pourquoi ", "decris ", "décris ", "lis ",
        )
        return any(low.startswith(prefix) for prefix in starters)

    def _goal_requests_computer_action(self) -> bool:
        low = (self.goal_lower or "").strip()
        if not low:
            return False
        action_markers = (
            "open ", "ouvre", "launch ", "run ", "execute ", "click ", "clique",
            "double click", "right click", "scroll ", "type ", "tape ",
            "create ", "crée", "build ", "make ", "install ", "download ",
            "upload ", "post ", "send ", "envoie", "navigate ", "go to ",
            "va sur ", "browse ", "search ", "cherche ", "find and ",
            "log in", "login ", "sign in", "fill ", "remplis ",
            "select ", "choose ", "save ", "edit ", "modify ", "delete ",
            "start ", "restart ", "deploy ", "share ", "reply to ",
            "open chrome", "open gmail", "open linkedin", "open tiktok",
            "move ", "déplace", "deplace", "bouge", "drag ", "drop ",
            "mouse", "souris", "observe", "inspect ", "check ", "verify ",
            "vérifie", "verifie", "visible", "visuellement", "visuel",
            "screen", "écran", "ecran", "window", "fenêtre", "fenetre",
            "browser", "navigateur", "gmail", "chrome", "tab ", "onglet",
        )
        return any(marker in low for marker in action_markers)

    def _should_try_direct_response(self, has_image: bool = False) -> bool:
        if self._goal_prefers_direct_response():
            return True
        if self._goal_requests_computer_action():
            return False
        if not has_image:
            visual_markers = (
                "screen", "screenshot", "capture", "ui", "window",
                "page", "visible", "look at", "image", "photo", "picture",
            )
            if any(marker in (self.goal_lower or "") for marker in visual_markers):
                return False
        if has_image and self._goal_has_question_shape():
            return True
        if self._goal_has_question_shape() and len(self.goal_text or "") <= 260:
            return True
        return False

    def _temporal_context_text(self) -> str:
        now = datetime.now().astimezone()
        tz_name = str(now.tzname() or "local")
        offset = now.strftime("%z")
        if offset and len(offset) == 5:
            offset = f"{offset[:3]}:{offset[3:]}"
        return (
            "TEMPORAL CONTEXT:\n"
            f"- Current local datetime: {now.isoformat(timespec='seconds')}\n"
            f"- Current local date: {now.date().isoformat()}\n"
            f"- Current local timezone: {tz_name}"
            + (f" ({offset})" if offset else "")
            + "\n"
            "- For any future task, convert the requested execution time into an exact ISO-8601 datetime with timezone.\n"
            "- Relative times such as 'in 5 minutes' or 'in 2 hours' are exact enough; convert them instead of asking for clarification.\n"
            "- If the current goal is a clear present-tense command with no future-time language, execute it now rather than asking for time again.\n"
            "- Never guess a missing execution time."
        )

    def _scheduled_tasks_context_text(self) -> str:
        tasks = list(self.scheduled_tasks_context or [])
        if not tasks:
            return "SCHEDULED TASKS CONTEXT:\n- No active future tasks are currently known for this user/device."
        lines = [
            "SCHEDULED TASKS CONTEXT:",
            "- These are the currently known active future tasks for this user/device.",
            "- Use task IDs exactly for cancel_scheduled_task, update_scheduled_task, and replace_scheduled_task actions.",
        ]
        for task in tasks[:12]:
            recurrence = ""
            rec = task.get("recurrence")
            if isinstance(rec, dict):
                recurrence = f" recurrence={json.dumps(rec, ensure_ascii=False, sort_keys=True)}"
            lines.append(
                f"- id={task.get('id')} | status={task.get('status')} | run_at={task.get('runAt')} | "
                f"timezone={task.get('timezone')} | prompt={task.get('prompt')}{recurrence}"
            )
        return "\n".join(lines)

    def _normalize_direct_answer_text(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = re.sub(r"^\s*(final answer|answer|response)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*step\s*\d+\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*thought\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _build_direct_response_messages(self) -> List[Dict[str, Any]]:
        progress_text = self._get_progress_log_text()
        system_text = (
            DIRECT_RESPONSE_SYSTEM_PROMPT
            + "\n\n"
            + self._temporal_context_text()
            + "\n\n"
            + self._scheduled_tasks_context_text()
        )
        if progress_text:
            system_text += "\n\n" + progress_text
        goal_msg = self.messages[1] if len(self.messages) > 1 else None
        base: List[Dict[str, Any]] = [{"role": "system", "content": system_text}]
        if goal_msg is not None:
            base.append(goal_msg)
        return base

    def _normalize_schedule_recurrence(self, value: Any) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        if isinstance(value, str):
            raw = value.strip().lower()
            if raw in {"daily", "every_day", "everyday"}:
                return {"kind": "daily"}
            return None
        if not isinstance(value, dict):
            return None

        raw_kind = str(value.get("kind") or value.get("type") or "").strip().lower()
        if raw_kind == "daily":
            return {"kind": "daily"}
        if raw_kind == "weekly":
            weekday_raw = value.get("weekday")
            weekday_map = {
                "sunday": 0,
                "monday": 1,
                "tuesday": 2,
                "wednesday": 3,
                "thursday": 4,
                "friday": 5,
                "saturday": 6,
            }
            weekday: Optional[int] = None
            if isinstance(weekday_raw, str):
                weekday = weekday_map.get(weekday_raw.strip().lower())
            else:
                try:
                    weekday = int(weekday_raw)
                except Exception:
                    weekday = None
            if weekday is None or weekday < 0 or weekday > 6:
                return None
            return {"kind": "weekly", "weekday": weekday}
        if raw_kind == "monthly":
            day_raw = value.get("dayOfMonth", value.get("day_of_month", value.get("day")))
            try:
                day = int(day_raw)
            except Exception:
                return None
            if day < 1 or day > 31:
                return None
            return {"kind": "monthly", "dayOfMonth": day}
        return None

    def _normalize_schedule_action(self, action: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], str]:
        goal = str(
            action.get("goal")
            or action.get("prompt")
            or action.get("task")
            or self.goal_text
            or ""
        ).strip()
        if not goal:
            return None, "schedule_task requires a non-empty goal."

        run_at_raw = str(
            action.get("run_at")
            or action.get("runAt")
            or action.get("when")
            or action.get("datetime")
            or ""
        ).strip()
        if not run_at_raw:
            return None, "schedule_task requires run_at as an exact ISO-8601 datetime with timezone."

        try:
            scheduled_dt = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
        except Exception:
            return None, "schedule_task run_at must be a valid ISO-8601 datetime."

        local_now = datetime.now().astimezone()
        if scheduled_dt.tzinfo is None:
            scheduled_dt = scheduled_dt.replace(tzinfo=local_now.tzinfo)
        scheduled_dt = scheduled_dt.astimezone()
        if scheduled_dt.timestamp() <= time.time():
            return None, "schedule_task run_at must be in the future."

        timezone_name = str(
            action.get("timezone")
            or action.get("tz")
            or scheduled_dt.tzname()
            or local_now.tzname()
            or "UTC"
        ).strip() or "UTC"

        recurrence = self._normalize_schedule_recurrence(action.get("recurrence"))
        message = str(
            action.get("message")
            or action.get("text")
            or action.get("response")
            or action.get("summary")
            or ""
        ).strip()

        payload: Dict[str, Any] = {
            "goal": goal,
            "runAt": scheduled_dt.isoformat(timespec="seconds"),
            "timezone": timezone_name,
        }
        if recurrence is not None:
            payload["recurrence"] = recurrence
        if message:
            payload["message"] = message
        return payload, ""

    def _normalize_scheduled_task_mutation(self, action: Dict[str, Any], *, op: str) -> tuple[Optional[Dict[str, Any]], str]:
        task_id = str(
            action.get("task_id")
            or action.get("taskId")
            or action.get("id")
            or ""
        ).strip()
        if not task_id:
            return None, f"{op} requires task_id."

        message = str(
            action.get("message")
            or action.get("text")
            or action.get("response")
            or action.get("summary")
            or ""
        ).strip()

        if op == "cancel":
            payload: Dict[str, Any] = {"op": "cancel", "taskId": task_id}
            if message:
                payload["message"] = message
            return payload, ""

        if op != "update":
            return None, f"Unsupported scheduled task mutation '{op}'."

        payload = {"op": "update", "taskId": task_id}
        if "goal" in action or "prompt" in action or "task" in action:
            next_goal = str(action.get("goal") or action.get("prompt") or action.get("task") or "").strip()
            if next_goal:
                payload["prompt"] = next_goal

        run_at_raw = str(
            action.get("run_at")
            or action.get("runAt")
            or action.get("when")
            or action.get("datetime")
            or ""
        ).strip()
        if run_at_raw:
            try:
                scheduled_dt = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
            except Exception:
                return None, "update_scheduled_task run_at must be a valid ISO-8601 datetime."
            local_now = datetime.now().astimezone()
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=local_now.tzinfo)
            scheduled_dt = scheduled_dt.astimezone()
            if scheduled_dt.timestamp() <= time.time():
                return None, "update_scheduled_task run_at must be in the future."
            payload["runAt"] = scheduled_dt.isoformat(timespec="seconds")
            payload["timezone"] = str(
                action.get("timezone")
                or action.get("tz")
                or scheduled_dt.tzname()
                or local_now.tzname()
                or "UTC"
            ).strip() or "UTC"
        elif "timezone" in action or "tz" in action:
            payload["timezone"] = str(action.get("timezone") or action.get("tz") or "UTC").strip() or "UTC"

        recurrence = self._normalize_schedule_recurrence(action.get("recurrence"))
        if recurrence is not None:
            payload["recurrence"] = recurrence

        if len(payload) <= 2:
            return None, "update_scheduled_task requires at least one change such as run_at, goal, or recurrence."
        if message:
            payload["message"] = message
        return payload, ""

    def _normalize_replace_scheduled_task_action(self, action: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], str]:
        task_id = str(
            action.get("task_id")
            or action.get("taskId")
            or action.get("id")
            or ""
        ).strip()
        if not task_id:
            return None, "replace_scheduled_task requires task_id."

        replacement_goal = str(
            action.get("goal")
            or action.get("prompt")
            or action.get("task")
            or ""
        ).strip()
        if not replacement_goal:
            return None, "replace_scheduled_task requires a non-empty goal."

        schedule_like_action = dict(action)
        schedule_like_action["goal"] = replacement_goal
        schedule_payload, schedule_error = self._normalize_schedule_action(schedule_like_action)
        if not schedule_payload:
            return None, schedule_error

        payload: Dict[str, Any] = {
            "op": "update",
            "taskId": task_id,
            "prompt": schedule_payload["goal"],
            "runAt": schedule_payload["runAt"],
            "timezone": schedule_payload["timezone"],
        }
        if "recurrence" in schedule_payload:
            payload["recurrence"] = schedule_payload["recurrence"]
        message = str(
            action.get("message")
            or action.get("text")
            or action.get("response")
            or action.get("summary")
            or ""
        ).strip()
        if message:
            payload["message"] = message
        return payload, ""

    def _try_direct_response_once(self) -> Optional[Dict[str, Any]]:
        if not self._direct_response_mode:
            return None
        try:
            response = self._chat_completion(
                model=self.decision_model,
                messages=self._build_direct_response_messages(),
                temperature=max(0.0, min(OPENAI_TEMPERATURE, 0.2)),
                max_completion_tokens=2000,
                source="direct_response_probe",
            )
        except TaskBudgetExceeded:
            raise
        except Exception as e:
            print(f"[Brain] Direct response probe failed: {e}", flush=True)
            return None

        raw_content = response.choices[0].message.content
        if not raw_content:
            return None
        assistant_text = str(raw_content).strip()
        if not assistant_text or assistant_text == "NEEDS_AGENTIC_ACTION":
            return None

        action = self._parse_action(assistant_text)
        if action:
            action_type = str(action.get("action") or "").strip().lower()
            if action_type in {"respond", "reply"}:
                response_text = str(
                    action.get("text")
                    or action.get("response")
                    or action.get("message")
                    or action.get("summary")
                    or ""
                ).strip()
                response_text = self._normalize_direct_answer_text(response_text)
                if response_text and response_text != "NEEDS_AGENTIC_ACTION":
                    return {
                        "success": True,
                        "summary": response_text,
                        "response_text": response_text,
                        "steps": 0,
                    }
            if action_type == "done":
                summary = self._normalize_direct_answer_text(str(action.get("summary") or "Task completed"))
                return {"success": True, "summary": summary, "response_text": summary, "steps": 0}
            if action_type == "failed":
                reason = self._normalize_direct_answer_text(str(action.get("reason") or "Task failed"))
                return {"success": False, "reason": reason, "summary": reason, "steps": 0}
            return None

        direct_text = self._normalize_direct_answer_text(assistant_text)
        if not direct_text or direct_text == "NEEDS_AGENTIC_ACTION":
            return None
        return {
            "success": True,
            "summary": direct_text,
            "response_text": direct_text,
            "steps": 0,
        }

    def _coerce_direct_response_action(self, text: str) -> Optional[Dict[str, Any]]:
        if not self._goal_prefers_direct_response():
            return None

        cleaned = self._normalize_direct_answer_text(text)
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if not lines or len(lines) > 14:
            return None

        cleaned = "\n".join(lines).strip()
        if not cleaned or len(cleaned) > 1600:
            return None

        blocked_tokens = ("{", "}", "[LOG]", "OBSERVATION", "CURRENT GOAL:")
        if any(token in cleaned for token in blocked_tokens):
            return None

        return {
            "thought": "The planner produced a short direct answer. I should return it to the user and stop.",
            "progress_log": "Prepared direct text response",
            "action": "respond",
            "text": cleaned,
        }

    def _extract_direct_response_from_execute(self, command: str, output: str) -> Optional[str]:
        if not self._goal_prefers_direct_response():
            return None

        cmd = str(command or "").strip()
        if not cmd:
            return None

        risky_markers = ("&&", "||", ";", "|", "\n", "$(", "`")
        if any(marker in cmd for marker in risky_markers):
            return None

        safe_prefixes = (
            "echo ",
            "printf ",
            "python3 -c ",
            "python -c ",
            f"{sys.executable} -c ",
            "node -e ",
            "perl -e ",
        )
        if not any(cmd.startswith(prefix) for prefix in safe_prefixes):
            return None

        text = (output or "").strip()
        if (
            not text
            or len(text) > 200
            or len([line for line in text.splitlines() if line.strip()]) > 3
            or text.startswith("[")
            or "ERROR" in text
            or "[stderr]" in text
            or "[exit code:" in text
        ):
            return None
        return text

    def _capture_raw_screen_base64(self) -> tuple:
        if self.remote_computer is not None:
            try:
                return self.remote_computer.capture_raw_screen_base64()
            except Exception:
                return "", "remote://capture.png"
        screenshot_path = "/tmp/ariaos_computer_capture.png"
        for cmd in (["scrot", screenshot_path, "-o"], ["import", "-window", "root", screenshot_path], ["gnome-screenshot", "-f", screenshot_path]):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                    env=self.env,
                )
                if result.returncode == 0 and os.path.exists(screenshot_path):
                    with open(screenshot_path, "rb") as img_file:
                        img_bytes = img_file.read()
                    return base64.b64encode(img_bytes).decode("ascii"), screenshot_path
            except Exception:
                continue
        return "", screenshot_path

    # =============================================================
    # ASYNC PROGRESS LOG — summarized by the active decision model
    # =============================================================
    def _summarize_step_async(self, step: int, action: Dict[str, Any], observation: str) -> None:
        """Launch a background thread to summarize this step."""
        def _do_summarize():
            thought = action.get("thought", "")
            action_type = action.get("action", "")
            # Include more detail for file operations
            action_detail = ""
            if action_type == "write_file":
                action_detail = f" path='{action.get('path', '')}'"
            elif action_type == "execute":
                action_detail = f" cmd='{(action.get('command', ''))[:100]}'"
            obs_short = (observation or "")[:400]
            prompt = (
                f"Summarize this AI agent step in ≤30 words. "
                f"MUST include: file paths created/modified, concrete action taken, and success/failure.\n"
                f"Step {step}: Action='{action_type}'{action_detail} Thought='{thought}' Observation='{obs_short}'\n"
                f"Format: 'Step {step}: <summary>'\n"
                f"Example: 'Step 3: Created /home/jeremie/app/app.py (Flask+SQLite). Server started on port 5055.'"
            )
            try:
                if not self.client:
                    raise RuntimeError("OpenAI client unavailable")
                response = self._chat_completion(
                    model=self.decision_model,
                    temperature=1,
                    max_completion_tokens=80,
                    messages=[{"role": "user", "content": prompt}],
                    source="progress_summary",
                )
                summary = (response.choices[0].message.content or "").strip()
                # Clean up: ensure it starts with "Step N:"
                if summary and not summary.startswith(f"Step {step}"):
                    summary = f"Step {step}: {summary}"
                with self._summary_lock:
                    self.progress_log.append(summary)
                print(f"[ProgressLog] {summary}", flush=True)
            except Exception as e:
                fallback = f"Step {step}: {action_type} — {thought[:50]}"
                with self._summary_lock:
                    self.progress_log.append(fallback)
                print(f"[ProgressLog] Fallback: {fallback} (err: {e})", flush=True)

        thread = threading.Thread(target=_do_summarize, daemon=True)
        with self._summary_lock:
            self._summary_threads.append(thread)
        thread.start()

    def _get_progress_log_text(self) -> str:
        """Get the current progress log formatted for the system prompt."""
        with self._summary_lock:
            parts = []
            if self.session_log:
                compressed = self._compress_session_log(self.session_log)
                parts.append("SESSION HISTORY (completed work in this chat):\n" + "\n".join(compressed))
            if self.progress_log:
                parts.extend(self._build_current_task_progress_sections(self.progress_log))
            if not parts:
                return ""
            return "\n\n".join(parts)

    def _save_task_summary_async(
        self,
        goal: str,
        summary: str,
        result: Dict[str, Any],
        progress_snapshot: List[str],
    ) -> None:
        """After each completed task, save a task summary and update user profile."""
        if STRICT_CHAT_ISOLATION:
            return

        def _do_save():
            # 1. Build task summary from progress log
            progress = (
                "CURRENT TASK PROGRESS:\n" + "\n".join(progress_snapshot)
                if progress_snapshot
                else ""
            )
            task_entry = {
                "timestamp": datetime.now().isoformat(),
                "goal": goal,
                "summary": summary,
                "steps": result.get("steps", 0),
                "success": result.get("success", False),
                "progress_log": list(progress_snapshot),
            }

            # Save to task_summaries.json
            try:
                os.makedirs(os.path.dirname(TASK_SUMMARIES_FILE), exist_ok=True)
                existing = []
                if os.path.exists(TASK_SUMMARIES_FILE):
                    with open(TASK_SUMMARIES_FILE, "r") as f:
                        existing = json.load(f)
                existing.append(task_entry)
                existing = existing[-5:]  # Keep last 5 successful tasks only
                with open(TASK_SUMMARIES_FILE, "w") as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                print(f"[Memory] Task summary saved ({len(existing)} total)", flush=True)
            except Exception as e:
                print(f"[Memory] Error saving task summary: {e}", flush=True)

            # 2. Update user profile
            self._update_user_profile(goal, summary, progress)

        thread = threading.Thread(target=_do_save, daemon=True)
        thread.start()

    def _update_user_profile(self, goal: str, summary: str, progress: str) -> None:
        """Use the active decision model to analyze the task and update user profile."""
        try:
            # Load existing profile
            profile = {
                "last_updated": "",
                "interaction_count": 0,
                "interests": [],
                "preferred_apps": [],
                "common_tasks": [],
                "communication_style": "",
                "pain_points": [],
            }
            if os.path.exists(USER_PROFILE_FILE):
                with open(USER_PROFILE_FILE, "r") as f:
                    profile = json.load(f)

            profile["interaction_count"] = profile.get("interaction_count", 0) + 1
            profile["last_updated"] = datetime.now().isoformat()

            if not self.client:
                print("[Memory] OpenAI client unavailable for profile update.", flush=True)
                return

            # Ask GPT-5.4 to extract insights from this task
            prompt = (
                f"Analyze this AI assistant task and extract user insights.\n"
                f"Goal: {goal}\n"
                f"Result: {summary}\n"
                f"Progress: {progress[:500]}\n\n"
                f"Current profile: {json.dumps(profile, ensure_ascii=False)[:800]}\n\n"
                f"Return ONLY a valid JSON object with these keys (keep existing values, add new ones):\n"
                f'{{"interests": ["list of user interests"], '
                f'"preferred_apps": ["apps the user uses"], '
                f'"common_tasks": ["types of tasks requested"], '
                f'"communication_style": "brief description", '
                f'"pain_points": ["difficulties observed"]}}\n'
                f"Merge with existing data. Do not remove existing entries. JSON only, no markdown."
            )
            response = self._chat_completion(
                model=self.decision_model,
                temperature=1,
                max_completion_tokens=300,
                messages=[{"role": "user", "content": prompt}],
                source="user_profile",
            )
            raw = (response.choices[0].message.content or "").strip()
            # Extract JSON from response
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                updates = json.loads(match.group(0))
                # Merge lists (deduplicate)
                for key in ("interests", "preferred_apps", "common_tasks", "pain_points"):
                    existing_list = profile.get(key, [])
                    new_items = updates.get(key, [])
                    merged = list(dict.fromkeys(existing_list + new_items))[:20]
                    profile[key] = merged
                if updates.get("communication_style"):
                    profile["communication_style"] = updates["communication_style"]

            # Save updated profile
            os.makedirs(os.path.dirname(USER_PROFILE_FILE), exist_ok=True)
            with open(USER_PROFILE_FILE, "w") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            print(f"[Memory] User profile updated (interests: {len(profile.get('interests', []))})", flush=True)
        except Exception as e:
            print(f"[Memory] Error updating user profile: {e}", flush=True)

    def _execute_command(self, command: str) -> str:
        """Execute a shell command and return the output."""
        try:
            stripped = command.strip()

            # Detach background commands to avoid blocking on inherited pipes.
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
                    cwd="/home/jeremie",
                    env=self.env
                )
                return f"[started background] {background_cmd}"

            # Auto-add sudo password for privileged commands
            if any(cmd in command for cmd in ["apt", "dpkg", "systemctl", "chmod", "chown", "usermod", "adduser"]):
                if "sudo" in command and "-S" not in command:
                    command = command.replace("sudo ", "echo 'Cisser,oussq2..' | sudo -S ")

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                cwd="/home/jeremie",
                env=self.env
            )

            output = ""
            if result.stdout.strip():
                output += result.stdout.strip()
            if result.stderr.strip():
                if output:
                    output += "\n"
                output += f"[stderr] {result.stderr.strip()}"

            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"

            return output if output else "(no output)"

        except subprocess.TimeoutExpired:
            return (
                f"[ERREUR TIMEOUT] La commande a bloqué le terminal plus de {COMMAND_TIMEOUT}s. "
                "Si tu lances une interface graphique (GUI) ou un serveur, tu DOIS détacher le processus "
                "en utilisant 'nohup <commande> >/dev/null 2>&1 &'."
            )
        except Exception as e:
            return f"[ERROR: {str(e)}]"

    def _parse_action(self, text: str) -> Optional[Dict]:
        """Parse an action JSON from GPT's response."""
        text = text.strip()

        def _validate_action(candidate: Any) -> Optional[Dict]:
            if not isinstance(candidate, dict):
                return None
            if "action" not in candidate:
                return None
            action_name = str(candidate.get("action") or "").strip().lower()
            if not action_name:
                return None
            candidate["action"] = action_name
            if "thought" not in candidate:
                candidate["thought"] = f"Planner omitted thought. Preserve action '{action_name}'."
            if not isinstance(candidate.get("thought"), str):
                candidate["thought"] = str(candidate.get("thought", ""))
            if action_name in {"respond", "reply"} and not candidate.get("text"):
                for key in ("response", "message", "summary", "content"):
                    value = str(candidate.get(key) or "").strip()
                    if value:
                        candidate["text"] = value
                        break
            return candidate

        # Try direct JSON
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                validated = _validate_action(parsed)
                if validated is not None:
                    return validated
            except json.JSONDecodeError:
                pass

        # Try to find JSON in markdown code block
        match = re.search(r'```(?:json)?\s*(\{[^`]+\})\s*```', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                validated = _validate_action(parsed)
                if validated is not None:
                    return validated
            except json.JSONDecodeError:
                pass

        # Try to find any JSON object with "action"
        match = re.search(r'\{[^{}]*"action"[^{}]*\}', text)
        if match:
            try:
                parsed = json.loads(match.group())
                validated = _validate_action(parsed)
                if validated is not None:
                    return validated
            except json.JSONDecodeError:
                pass

        return self._coerce_direct_response_action(text)

    def close(self):
        """Clean up."""
        if getattr(self, "executor", None):
            self.executor.close()


# Singleton
_loop_instance = None

def get_agentic_loop(**kwargs) -> AgenticLoop:
    global _loop_instance
    if _loop_instance is None:
        _loop_instance = AgenticLoop(**kwargs)
    return _loop_instance


# =========================================================================
# CLI test
# =========================================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 agentic_loop.py 'your goal here'")
        print("Example: python3 agentic_loop.py 'install htop and show system info'")
        sys.exit(1)

    goal = " ".join(sys.argv[1:])
    loop = AgenticLoop()

    for message in loop.execute_goal(goal):
        print(message, end="", flush=True)
