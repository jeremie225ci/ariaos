from __future__ import annotations

import ast
import base64
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

from gi.repository import GLib, Gtk, Pango

from ..services.loop import LocalLoop
from ..services.history import HistoryService, SessionMessage, SessionSummary
from ..services.core import RuntimeState, RuntimeStore, SUPPORTED_OPENAI_VOICE_NAMES, SUPPORTED_VOICE_LANGUAGES
from ..services.voice_client import VoiceClient
from .panels import show_access_dialog, show_model_dialog, show_key_dialog
from .voice_overlay import VoiceOverlay

DEBUG_PREFIXES = (
    "Aria>",
    "━━━ Step ",
    "🧠 Thought:",
    "📈 Progress:",
    "🔧 Action:",
    "📋 Output:",
    "👁️ Screen",
    "👁️ ask_vision",
    "🔍 Visual parser call",
    "🔍 Total visual parser calls",
    "🧠 Conversation saved to memory.",
    "💬 Direct response probe enabled",
    "🧾 Session state saved.",
    "[task]",
    "[vision]",
    "[stop]",
    "[session]",
    "⚠️ Could not parse action:",
    "⚠️ Error:",
    "✓ ",
    "↺ ",
    "```",
)

SESSION_PREAMBLE_PREFIXES = (
    "🎯 Goal:",
    "N:",
    "S:",
    "L:",
    "Q:",
    "🧩 Active profile:",
    "🧠 Model:",
    "🖥️ UI engine:",
    "🖱️ Input backend:",
    "🔄 Starting agentic loop",
    "[Brain] 🎯 New goal:",
)

SESSION_PREAMBLE_LINES = {
    "- Keep scope local.",
    "- Use only the log below.",
}

MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
IMAGE_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
GTK_LOG_PATH = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos" / "gtk_terminal.log"
STRUCTURED_ASSISTANT_PREFIX = "__AR1__"
VOICE_NAME_LABELS = {name: name.title() for name in SUPPORTED_OPENAI_VOICE_NAMES}
VOICE_LANGUAGE_LABELS = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
}
LEGACY_STRUCTURED_ASSISTANT_PREFIXES = (
    STRUCTURED_ASSISTANT_PREFIX,
    "__ARIA_STRUCTURED_V1__:",
    "__ARIA_STRUCTURED_V1__",
    "_ARIA_STRUCTURED_V1__:",
    "_ARIA_STRUCTURED_V1__",
)
DEBUG_MARKER_RE = re.compile(
    r"(?=("
    r"(?:🧠\s*)?Thought:"
    r"|(?:📈\s*)?Progress:"
    r"|(?:🔧\s*)?Action:"
    r"|(?:📋\s*)?Output:"
    r"|━━━\s*Step\s+\d+:"
    r"|Step\s+\d+:"
    r"))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AssistantPayload:
    answer: str
    thoughts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    progress: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    logs: tuple[str, ...] = ()

    @property
    def has_details(self) -> bool:
        return bool(self.thoughts or self.actions or self.progress or self.steps or self.logs)



def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()



def _normalize_display_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    compact: list[str] = []
    blank_pending = False
    for line in lines:
        if not line.strip():
            if compact:
                blank_pending = True
            continue
        if blank_pending:
            compact.append("")
            blank_pending = False
        compact.append(line.strip())
    return "\n".join(compact).strip()


def _sanitize_terminal_result_text(text: str) -> str:
    normalized = _normalize_display_text(text)
    if not normalized:
        return ""
    cleaned_lines: list[str] = []
    for line in normalized.split("\n"):
        value = (
            line.strip()
            .replace("\uFFFD", " ")
            .replace("□", " ")
            .replace("■", " ")
            .replace("▢", " ")
            .replace("▪", " ")
        )
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            continue
        if not re.search(r"[\w]", value, flags=re.UNICODE):
            continue
        cleaned_lines.append(value)
    return "\n".join(cleaned_lines).strip()



def _normalize_detail_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_session_preamble_line(text: str) -> bool:
    normalized = _normalize_detail_text(text)
    if not normalized:
        return False
    if normalized in SESSION_PREAMBLE_LINES:
        return True
    return any(normalized.startswith(prefix) for prefix in SESSION_PREAMBLE_PREFIXES)


def _extract_explicit_final_answer(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[LOG]"):
            line = line[len("[LOG]"):].strip()
        if "RESPOND:" in line:
            candidate = line.split("RESPOND:", 1)[-1].strip()
            if candidate:
                return _sanitize_terminal_result_text(candidate)
    return ""


def _clean_answer_lines(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[LOG]"):
            line = line[len("[LOG]"):].strip()
        if not line:
            continue
        if "RESPOND:" in line:
            candidate = line.split("RESPOND:", 1)[-1].strip()
            if candidate:
                kept.append(candidate)
            continue
        if _is_session_preamble_line(line):
            continue
        if line.startswith("U:") or line.startswith("A:"):
            continue
        if line in {"🧠", "📈", "🔧", "📋"}:
            continue
        if line.startswith("[Brain]"):
            continue
        if line.startswith("━━━ Step ") or line.lower().startswith("step "):
            continue
        if any(line.startswith(prefix) for prefix in DEBUG_PREFIXES):
            continue
        kept.append(line)
    return _sanitize_terminal_result_text("\n".join(kept))



def _dedupe_lines(lines: list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for line in lines:
        value = _normalize_detail_text(line)
        if not value:
            continue
        if not cleaned or cleaned[-1] != value:
            cleaned.append(value)
    return tuple(cleaned)



def _structured_assistant_payload(text: str) -> AssistantPayload | None:
    raw = str(text or "")
    prefix = next((value for value in LEGACY_STRUCTURED_ASSISTANT_PREFIXES if raw.startswith(value)), "")
    if not prefix:
        return None
    try:
        data = json.loads(raw[len(prefix):])
    except Exception:
        try:
            data = ast.literal_eval(raw[len(prefix):])
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    answer = _clean_answer_lines(data.get("answer", data.get("anaswer", "")))
    if not answer:
        answer = _extract_explicit_final_answer(data.get("answer", data.get("anaswer", "")))
    return AssistantPayload(
        answer=answer,
        thoughts=_dedupe_lines(list(data.get("thoughts") or [])),
        actions=_dedupe_lines(list(data.get("actions") or [])),
        progress=_dedupe_lines(list(data.get("progress") or [])),
        steps=_dedupe_lines(list(data.get("steps") or [])),
        logs=_dedupe_lines(list(data.get("logs") or [])),
    )



def _parse_assistant_payload(text: str) -> AssistantPayload:
    structured = _structured_assistant_payload(text)
    if structured is not None:
        return structured

    normalized_source = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("�", " ")
    explicit_answer = _extract_explicit_final_answer(normalized_source)
    source_parts = DEBUG_MARKER_RE.split(normalized_source)
    segments = [segment.strip() for segment in source_parts if segment and segment.strip()]

    answer_lines: list[str] = []
    thoughts: list[str] = []
    actions: list[str] = []
    progress: list[str] = []
    steps: list[str] = []
    logs: list[str] = []
    iterable = segments if segments else normalized_source.split("\n")
    for raw_line in iterable:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[LOG]"):
            line = line[len("[LOG]"):].strip()
            if not line:
                continue
        normalized = line.strip()
        lower = normalized.lower()
        if not normalized or normalized in {"🧠", "📈", "🔧", "📋"}:
            continue
        if _is_session_preamble_line(normalized):
            logs.append(normalized)
            continue
        if normalized.startswith("USER:") or normalized.startswith("ARIA:"):
            logs.append(normalized)
            continue
        if lower.startswith("[brain] 🤔 thought:"):
            thoughts.append(normalized.split(":", 1)[-1].strip())
            continue
        if lower.startswith("[🧠 thought]"):
            thoughts.append(normalized.split("]", 1)[-1].strip())
            continue
        if lower.startswith("[📈 progress]"):
            progress.append(normalized.split("]", 1)[-1].strip())
            continue
        if lower.startswith("[🔧 action]"):
            actions.append(normalized.split("]", 1)[-1].strip())
            continue
        if "respond:" in lower:
            continue
        if "thought:" in lower:
            thoughts.append(normalized.split("Thought:", 1)[-1].split("thought:", 1)[-1].strip())
            continue
        if "action:" in lower:
            actions.append(normalized.split("Action:", 1)[-1].split("action:", 1)[-1].strip())
            continue
        if "progress:" in lower:
            progress.append(normalized.split("Progress:", 1)[-1].split("progress:", 1)[-1].strip())
            continue
        if normalized.startswith("━━━ Step ") or lower.startswith("step "):
            steps.append(normalized)
            continue
        if "output:" in lower:
            steps.append(normalized)
            continue
        if any(normalized.startswith(prefix.replace("�", "")) for prefix in DEBUG_PREFIXES):
            logs.append(normalized)
            continue
        answer_lines.append(normalized)

    answer = explicit_answer or _clean_answer_lines("\n".join(answer_lines)) or _normalize_display_text("\n".join(answer_lines))
    return AssistantPayload(
        answer=answer,
        thoughts=_dedupe_lines(thoughts),
        actions=_dedupe_lines(actions),
        progress=_dedupe_lines(progress),
        steps=_dedupe_lines(steps),
        logs=_dedupe_lines(logs),
    )


def _merge_final_payload(buffer_text: str, response_text: str) -> AssistantPayload:
    detail_source = str(buffer_text or response_text or "")
    response_source = str(response_text or "")
    detail_payload = _parse_assistant_payload(detail_source)
    response_payload = _parse_assistant_payload(response_source) if response_source else AssistantPayload(answer="")
    answer = (
        _extract_explicit_final_answer(response_source)
        or _clean_answer_lines(response_source)
        or response_payload.answer
        or _extract_explicit_final_answer(detail_source)
        or _clean_answer_lines(detail_source)
        or detail_payload.answer
    )
    return AssistantPayload(
        answer=_sanitize_terminal_result_text(answer),
        thoughts=detail_payload.thoughts,
        actions=detail_payload.actions,
        progress=detail_payload.progress,
        steps=detail_payload.steps,
        logs=detail_payload.logs,
    )



def _serialize_assistant_payload(payload: AssistantPayload) -> str:
    if not payload.has_details:
        return payload.answer
    return STRUCTURED_ASSISTANT_PREFIX + json.dumps(
        {
            "answer": payload.answer,
            "thoughts": list(payload.thoughts),
            "actions": list(payload.actions),
            "progress": list(payload.progress),
            "steps": list(payload.steps),
            "logs": list(payload.logs),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _visible_message_text(role: str, text: str) -> str:
    role_name = (role or "assistant").strip().lower()
    if role_name == "assistant":
        return _parse_assistant_payload(text).answer
    cleaned = _normalize_display_text(text)
    for prefix in ("You>", "USER:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned


def _should_record_memory_summary(text: str) -> bool:
    cleaned = _normalize_display_text(text).strip().lower()
    if not cleaned:
        return False
    blocked_markers = (
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
        "connect a control tower account first",
        "update required",
    )
    return not any(marker in cleaned for marker in blocked_markers)


def _looks_like_smalltalk(text: str) -> bool:
    lowered = _normalize_display_text(text).strip().lower().replace("’", "'")
    if not lowered:
        return True
    smalltalk_markers = (
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
    return any(marker in lowered for marker in smalltalk_markers) and len(lowered) < 80


def _is_useful_memory_candidate(goal: str, text: str, *, has_remote_task: bool, has_details: bool) -> bool:
    normalized_goal = _normalize_display_text(goal).strip()
    normalized_text = _normalize_display_text(text).strip()
    if not normalized_goal or not normalized_text:
        return False
    if not has_remote_task and not has_details:
        return False
    if len(normalized_goal) < 12 or len(normalized_text) < 24:
        return False
    if _looks_like_smalltalk(normalized_goal) or _looks_like_smalltalk(normalized_text):
        return False
    packed_markers = ("M:", "\nM:", "L:", "\nL:", "Q:", "\nQ:", "N:", "\nN:", "Verified playbooks:", "Recent useful task summaries:")
    if any(marker in normalized_text for marker in packed_markers):
        return False
    return _should_record_memory_summary(normalized_text)



def _preview_text(text: str, role: str = "assistant") -> str:
    cleaned = _compact_text(_visible_message_text(role, text))
    for prefix in ("Aria>", "ARIA:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned[:84] or "No messages yet."


def _append_memory_notice(answer: str, notice: str) -> str:
    base = str(answer or "").rstrip()
    note = str(notice or "").strip()
    if not note:
        return base
    marker = f"[Memory] {note}"
    if marker in base:
        return base
    if not base:
        return marker
    return f"{base}\n\n{marker}"


def _pretty_timestamp(value: str) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%d %b %H:%M")
        except ValueError:
            continue
    return value



def _message_role(message: SessionMessage) -> str:
    role = (message.role or "assistant").strip().lower()
    return "user" if role == "user" else "assistant"

class ConsoleView(Gtk.Box):
    def __init__(self, state: RuntimeState, service: RuntimeStore, bridge: LocalLoop, go_home):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("terminal-shell")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.state = state
        self.service = service
        self.bridge = bridge
        self.go_home = go_home
        self.history = HistoryService(state.history_db_path)

        self.sessions: list[SessionSummary] = []
        self.selected_session_id: str | None = None
        self.current_running = False
        self.active_request_session_id: str | None = None
        self.current_stream_buffer = ""
        self.streaming_row = None
        self.streaming_body_label = None
        self.streaming_details_expander = None
        self.streaming_details_box = None
        self.streaming_details_scroller = None
        self.streaming_detail_sections: dict[str, tuple[Gtk.Widget, Gtk.Label]] = {}
        self.key_window = None
        self.access_window = None
        self.pending_image: dict[str, str] | None = None
        self._last_state_stamp = self.service.state_stamp()
        self._last_disk_refresh = 0.0
        self._last_backend_ensure = 0.0
        self.backend_connected = False
        self._task_started_at = 0.0
        self._last_task_event_at = 0.0
        self._awaiting_remote_accept = False
        self._activation_notice_shown_for: str | None = None
        self._active_remote_task_id: str | None = None
        self._active_remote_budget_usd = 0.0
        self._active_remote_paused = False
        self._active_goal_text = ""
        self._last_remote_progress_sync_at = 0.0
        self._last_remote_screenshot_sync_at = 0.0
        self._remote_screenshot_sync_inflight = False
        self._pending_remote_task: dict[str, object] | None = None
        self._seen_remote_message_ids: set[str] = set()
        self._update_in_progress = False
        self._update_check_inflight = False
        self._update_password_window = None
        self.voice_client: VoiceClient | None = None
        self.voice_mode_enabled = False
        self.voice_overlay: VoiceOverlay | None = None
        self.strip_voice_name_button: Gtk.Button | None = None
        self.strip_voice_language_button: Gtk.Button | None = None
        self.strip_voice_button: Gtk.ToggleButton | None = None
        self._voice_toggle_handler_id = 0
        self._voice_snapshot: dict[str, object] = {
            "status": "idle",
            "session_id": "",
            "task_id": "",
            "goal": "",
            "latest_summary": "",
            "latest_result": "",
            "latest_action": "",
            "latest_progress": "",
            "error": "",
            "paused": False,
            "supports_followups": False,
            "updated_at": time.time(),
        }

        self._build_sidebar()
        self._build_workspace()
        self.refresh_state(state)
        GLib.timeout_add(80, self._poll_backend_queue)

    def _log_debug(self, message: str) -> None:
        try:
            GTK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with GTK_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.utcnow().isoformat()}Z {message}\n")
        except Exception:
            pass

    def _update_required_message(self) -> str:
        return (
            str(self.state.update_message or "").strip()
            or "Update required. Install the latest AriaOS update to continue."
        )

    def _access_required_message(self) -> str:
        return "Connect your local OpenAI key to continue using Terminal Aria."

    def _access_block_message(self) -> str:
        if not self.state.api_key_ready:
            return self._access_required_message()
        if not self.backend_connected and not self._uses_remote_gateway():
            return "Start or reconnect the local runtime to continue."
        return ""

    def _report_usage_async(self, task_id: str | None, usage: dict | None) -> None:
        if not task_id or not isinstance(usage, dict):
            return

        payload = {
            "taskId": str(task_id),
            "sessionId": str(self.selected_session_id or ""),
            "inputTokens": int(usage.get("input_tokens") or 0),
            "outputTokens": int(usage.get("output_tokens") or 0),
            "cachedInputTokens": int(usage.get("cached_input_tokens") or 0),
            "costUsd": float(usage.get("cost_usd") or 0.0),
            "modelCalls": int(usage.get("model_calls") or 0),
            "success": bool(usage.get("success")),
            "steps": int(usage.get("steps") or 0),
            "elapsedSeconds": float(usage.get("elapsed_seconds") or 0.0),
            "models": usage.get("models") or {},
        }

        def _worker():
            ok, message = self.service.report_usage(payload)
            self._log_debug(
                f"report_usage task_id={task_id} ok={ok} cost={payload['costUsd']:.6f} message={message}"
            )
            if ok:
                GLib.idle_add(lambda: self.refresh_state(self.service.read()) or False)

        threading.Thread(target=_worker, daemon=True).start()

    def remote_presence_state(self) -> tuple[str, str]:
        if self.current_running and self._active_remote_task_id:
            return self._active_remote_task_id, "paused" if self._active_remote_paused else "running"
        if self.current_running:
            return "", "busy"
        return "", "ready"

    def stop_active_remote_task(self) -> bool:
        if not self.current_running or not self._active_remote_task_id:
            return False
        self._request_stop()
        return True

    def pause_active_remote_task(self) -> bool:
        if not self.current_running or not self._active_remote_task_id or self._active_remote_paused:
            self._log_debug(
                "pause_active_remote_task skipped "
                f"running={self.current_running} task={self._active_remote_task_id} paused={self._active_remote_paused}"
            )
            return False
        if not hasattr(self.bridge, "request_pause") or not bool(self.bridge.request_pause()):
            self._log_debug("pause_active_remote_task bridge rejected pause")
            return False
        self._log_debug(f"pause_active_remote_task task={self._active_remote_task_id}")
        self._active_remote_paused = True
        self._append_system_notice("Remote task paused.")
        self._sync_remote_task_async(
            status="paused",
            latest_summary="Remote task paused from Control Tower.",
            event_text="Remote task paused from Control Tower.",
            event_kind="control",
        )
        self._publish_voice_snapshot()
        self._sync_controls()
        return True

    def resume_active_remote_task(self) -> bool:
        if not self.current_running or not self._active_remote_task_id or not self._active_remote_paused:
            self._log_debug(
                "resume_active_remote_task skipped "
                f"running={self.current_running} task={self._active_remote_task_id} paused={self._active_remote_paused}"
            )
            return False
        if not hasattr(self.bridge, "request_resume") or not bool(self.bridge.request_resume()):
            self._log_debug("resume_active_remote_task bridge rejected resume")
            return False
        self._log_debug(f"resume_active_remote_task task={self._active_remote_task_id}")
        self._active_remote_paused = False
        self._append_system_notice("Remote task resumed.")
        self._sync_remote_task_async(
            status="running",
            latest_summary="Remote task resumed from Control Tower.",
            event_text="Remote task resumed from Control Tower.",
            event_kind="control",
        )
        self._publish_voice_snapshot()
        self._sync_controls()
        return True

    def handle_remote_task_messages(self, task_id: str, messages: list[dict[str, object]]) -> bool:
        active_task_id = str(self._active_remote_task_id or "").strip()
        if not active_task_id or active_task_id != str(task_id or "").strip() or not self.current_running:
            return False
        ack_ids: list[str] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            role = str(item.get("role") or "operator").strip() or "operator"
            if not message_id or not text or message_id in self._seen_remote_message_ids:
                continue
            if not hasattr(self.bridge, "send_task_message"):
                continue
            if not bool(self.bridge.send_task_message(text, role=role, message_id=message_id)):
                continue
            self._seen_remote_message_ids.add(message_id)
            ack_ids.append(message_id)
            preview = _sanitize_terminal_result_text(text)[:220]
            if preview:
                self._append_system_notice(f"Remote operator: {preview}")
        if ack_ids:
            summary = f"Delivered {len(ack_ids)} remote operator instruction(s) to the running task."
            self._sync_remote_task_async(
                latest_summary=summary,
                event_text=summary,
                event_kind="control",
                ack_message_ids=ack_ids,
            )
        return bool(ack_ids)

    def _capture_remote_screenshot_preview(self) -> tuple[str, str]:
        runtime = getattr(self.bridge, "runtime", None)
        if runtime is None:
            return "", ""
        capture = getattr(runtime, "capture_screenshot", None)
        if not callable(capture):
            # The shipped bundle obfuscates runtime method names.
            capture = getattr(runtime, "m1", None)
        if not callable(capture):
            return "", ""
        ok, image_base64, mime, _detail = capture()
        if not ok or not image_base64:
            return "", ""
        return self._prepare_remote_screenshot_preview(image_base64, mime)

    def _prepare_remote_screenshot_preview(self, image_base64: str, mime: str) -> tuple[str, str]:
        try:
            raw = base64.b64decode(image_base64)
        except Exception:
            return "", ""
        out_mime = mime or "image/png"
        out_bytes = raw
        if Image is not None:
            try:
                from io import BytesIO
                with Image.open(BytesIO(raw)) as src:
                    image = src.convert("RGB") if src.mode != "RGB" else src.copy()
                    image.thumbnail((1280, 800))
                    buffer = BytesIO()
                    image.save(buffer, format="JPEG", quality=84, optimize=True)
                    out_bytes = buffer.getvalue()
                    out_mime = "image/jpeg"
            except Exception:
                out_bytes = raw
        if len(out_bytes) > 900_000:
            return "", ""
        return f"data:{out_mime};base64,{base64.b64encode(out_bytes).decode('ascii')}", datetime.utcnow().isoformat() + "Z"

    def _sync_remote_screenshot_async(self, *, minimum_interval: float = 4.0) -> None:
        if not self._active_remote_task_id or not self.current_running or self._remote_screenshot_sync_inflight:
            return
        now = time.monotonic()
        if now - self._last_remote_screenshot_sync_at < minimum_interval:
            return
        self._remote_screenshot_sync_inflight = True

        def _worker() -> None:
            try:
                data_url, captured_at = self._capture_remote_screenshot_preview()
                if not data_url:
                    return
                self._last_remote_screenshot_sync_at = time.monotonic()
                ok, payload = self.service.sync_task(
                    str(self._active_remote_task_id or ""),
                    latest_screenshot_data_url=data_url,
                    latest_screenshot_captured_at=captured_at,
                )
                if not ok:
                    self._log_debug(
                        f"remote_screenshot_sync_failed task={self._active_remote_task_id or '-'} "
                        f"message={payload.get('message') if isinstance(payload, dict) else payload}"
                    )
            finally:
                self._remote_screenshot_sync_inflight = False

        threading.Thread(target=_worker, daemon=True).start()

    def _sync_remote_screenshot_data_async(self, image_base64: str, mime: str) -> None:
        if not self._active_remote_task_id or not self.current_running:
            return

        def _worker() -> None:
            data_url, captured_at = self._prepare_remote_screenshot_preview(image_base64, mime)
            if not data_url:
                return
            self._last_remote_screenshot_sync_at = time.monotonic()
            ok, payload = self.service.sync_task(
                str(self._active_remote_task_id or ""),
                latest_screenshot_data_url=data_url,
                latest_screenshot_captured_at=captured_at,
            )
            if not ok:
                self._log_debug(
                    f"remote_screenshot_sync_failed task={self._active_remote_task_id or '-'} "
                    f"message={payload.get('message') if isinstance(payload, dict) else payload}"
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _sync_remote_task_async(
        self,
        *,
        status: str = "",
        latest_summary: str = "",
        latest_result: str = "",
        error: str = "",
        spent_usd: float | None = None,
        usage: dict | None = None,
        event_text: str = "",
        event_kind: str = "",
        throttle_seconds: float = 0.0,
        ack_message_ids: list[str] | None = None,
        latest_screenshot_data_url: str = "",
        latest_screenshot_captured_at: str = "",
    ) -> None:
        task_id = str(self._active_remote_task_id or "").strip()
        if not task_id:
            return
        now = time.monotonic()
        if throttle_seconds > 0 and now - self._last_remote_progress_sync_at < throttle_seconds:
            return
        if throttle_seconds > 0:
            self._last_remote_progress_sync_at = now
        session_id = str(self.selected_session_id or self.active_request_session_id or task_id)

        def _worker() -> None:
            ok, payload = self.service.sync_task(
                task_id,
                status=status,
                session_id=session_id,
                latest_summary=latest_summary,
                latest_result=latest_result,
                error=error,
                spent_usd=spent_usd,
                usage=usage,
                event_text=event_text,
                event_kind=event_kind,
                ack_message_ids=ack_message_ids,
                latest_screenshot_data_url=latest_screenshot_data_url,
                latest_screenshot_captured_at=latest_screenshot_captured_at,
            )
            self._log_debug(
                f"remote_task_sync task_id={task_id} ok={ok} status={status or '-'} "
                f"summary_len={len(latest_summary)} event_kind={event_kind or '-'} "
                f"message={payload.get('message') if isinstance(payload, dict) else payload}"
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _sync_remote_task_blocked_async(self, task_id: str, session_id: str, message: str) -> None:
        blocked_task_id = str(task_id or "").strip()
        if not blocked_task_id:
            return

        def _worker() -> None:
            ok, payload = self.service.sync_task(
                blocked_task_id,
                status="failed",
                session_id=str(session_id or blocked_task_id).strip(),
                latest_summary=message,
                error=message,
                event_text=message,
                event_kind="error",
            )
            self._log_debug(
                f"remote_task_blocked task_id={blocked_task_id} ok={ok} "
                f"message={payload.get('message') if isinstance(payload, dict) else payload}"
            )

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _remote_progress_summary(chunk: str) -> str:
        cleaned = []
        for raw_line in str(chunk or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[LOG]"):
                line = line[len("[LOG]"):].strip()
            if not line:
                continue
            cleaned.append(line)
        summary = _sanitize_terminal_result_text("\n".join(cleaned))
        if not summary:
            return ""
        return summary[:280]

    def _submit_prompt_payload(
        self,
        raw: str,
        *,
        session_id: str | None = None,
        image: dict[str, str] | None = None,
        external_task_id: str | None = None,
        max_budget_usd: float | None = None,
        session_title: str | None = None,
        model: str | None = None,
    ) -> bool:
        self.state = self.service.read()
        remote_mode = bool(self.state.remote_url.strip())
        if self.state.update_required:
            self.refresh_state(self.state)
            self._append_system_notice(self._update_required_message())
            return False
        if self.current_running:
            return False
        chosen_session_id = str(session_id or self.selected_session_id or "").strip()
        if not chosen_session_id:
            self._new_session()
            chosen_session_id = str(self.selected_session_id or "").strip()
        if not chosen_session_id:
            return False
        self.selected_session_id = chosen_session_id
        self.history.create_session(chosen_session_id, session_title or "New Chat")
        if self.state.auth_ready:
            self.service.refresh_access()
            self.state = self.service.read()
            if self.state.update_required:
                self.refresh_state(self.state)
                self._append_system_notice(self._update_required_message())
                return False
        if self.state.plan_status not in {"active", "trialing"}:
            self.refresh_state(self.service.read())
            self._append_system_notice(self._access_required_message())
            return False
        if not remote_mode:
            allowed, message = self.service.ensure_access()
            self.state = self.service.read()
            if not allowed:
                self.refresh_state(self.state)
                self._append_system_notice(message)
                return False
            if not self.state.api_key_ready:
                self._show_api_key_dialog()
                self.state = self.service.read()
                self._sync_controls()
                if not self.state.api_key_ready:
                    return False
            if not self.backend_connected:
                self._ensure_backend_ready(force=True)
                if not self.service.backend_ws_reachable():
                    self.refresh_state(self.service.read())
                    return False

        clean_prompt = str(raw or "").strip()
        if not clean_prompt:
            return False
        self._active_goal_text = clean_prompt
        is_first_user_message = self.history.count_user_messages(chosen_session_id) == 0
        self._save_message(chosen_session_id, "user", clean_prompt)
        if is_first_user_message:
            self.history.rename_session(chosen_session_id, session_title or self._provisional_title(clean_prompt))
            self.sessions = self.history.list_sessions()
            self._render_session_list()

        payload = self._pack_request_text(chosen_session_id, clean_prompt)
        self.active_request_session_id = chosen_session_id
        self.current_stream_buffer = ""
        self.current_running = True
        self._task_started_at = time.monotonic()
        self._last_task_event_at = self._task_started_at
        self._awaiting_remote_accept = bool(remote_mode)
        self._last_remote_progress_sync_at = 0.0
        self._last_remote_screenshot_sync_at = 0.0
        self._remote_screenshot_sync_inflight = False
        self._active_remote_task_id = str(external_task_id or "").strip() or None
        self._active_remote_budget_usd = float(max_budget_usd or 0.0)
        self._active_remote_paused = False
        self._seen_remote_message_ids = set()
        self._push_voice_snapshot(
            status="dispatching" if remote_mode else "running",
            session_id=chosen_session_id,
            task_id=str(external_task_id or chosen_session_id or "").strip(),
            goal=clean_prompt,
            latest_summary="Task accepted and waiting for runtime execution." if remote_mode else "Task started.",
            latest_result="",
            latest_action="",
            latest_progress="",
            error="",
            paused=False,
            supports_followups=bool(remote_mode and hasattr(self.bridge, "send_task_message")),
        )
        self._log_debug(f"send session={chosen_session_id} prompt_len={len(payload)} remote_task={self._active_remote_task_id or '-'}")
        self.refresh_state(self.state)
        self._append_streaming_placeholder()
        self._sync_controls()
        try:
            self.bridge.submit_goal(
                chosen_session_id,
                payload,
                image=image,
                task_id=external_task_id,
                max_budget_usd=max_budget_usd,
                model=model,
            )
        except Exception as exc:
            self._log_debug(f"submit_goal exception: {exc}\n{traceback.format_exc()}")
            self._abort_current_task("Connection issue: the terminal failed to send the request to the local runtime.")
            return False
        return True

    def launch_remote_task(self, task: dict[str, object]) -> bool:
        task_id = str(task.get("id") or "").strip()
        session_id = str(task.get("sessionId") or "").strip() or task_id
        goal = str(task.get("goal") or "").strip()
        if not task_id or not goal or self.current_running:
            return False
        title = str(task.get("title") or "").strip() or self._provisional_title(goal)
        max_budget_usd = float(task.get("maxBudgetUsd") or 0.0)
        model = str(task.get("model") or "").strip() or None
        ok = self._submit_prompt_payload(
            goal,
            session_id=session_id,
            external_task_id=task_id,
            max_budget_usd=max_budget_usd,
            session_title=title,
            model=model,
        )
        if ok:
            self._active_remote_paused = False
            self._sync_remote_task_async(
                status="dispatching",
                latest_summary="Terminal Aria claimed the remote task on the device.",
                event_text="Device claimed remote task and started Terminal Aria.",
                event_kind="status",
            )
        else:
            blocked_message = self._access_block_message()
            if blocked_message:
                self._sync_remote_task_blocked_async(task_id, session_id, blocked_message)
        return ok

    def refresh_state(self, state: RuntimeState) -> None:
        previous_state = self.state
        self.state = state
        self.history = HistoryService(state.history_db_path)
        previous = self.selected_session_id
        self.sessions = self.history.list_sessions()
        if previous and any(item.session_id == previous for item in self.sessions):
            self.selected_session_id = previous
        elif self.sessions:
            self.selected_session_id = self.sessions[0].session_id
        else:
            self.selected_session_id = None
        self._render_session_list()
        self._render_messages()
        self.strip_model_button.set_label(f"Model: {state.openai_model.upper()}")
        self.strip_key_button.set_visible(not self._uses_remote_gateway())
        self._refresh_voice_name_button()
        self._refresh_voice_language_button()
        if self.voice_mode_enabled:
            allowed, _message = self._voice_runtime_allowed()
            if not allowed:
                self._stop_voice_mode()
            else:
                self._publish_voice_snapshot()
                if self.voice_overlay is not None:
                    self.voice_overlay.set_voice_name(self._voice_name())
        self._maybe_show_activation_notice(previous_state, state)

    def prepare_for_display(self) -> None:
        self.refresh_state(self.service.read())
        if not self._uses_remote_gateway():
            self._ensure_backend_ready()
        if self.selected_session_id and not self.current_running:
            self.bridge.reset_session(self.selected_session_id)

    def _ensure_backend_ready(self, force: bool = False) -> bool:
        if not self.state.api_key_ready:
            return False
        if self.service.backend_ws_reachable():
            return True
        now = time.monotonic()
        if not force and now - self._last_backend_ensure < 4.0:
            return False
        self._last_backend_ensure = now
        self._log_debug("backend unreachable -> restart_local_backend()")
        ok = self.service.restart_local_backend()
        self._log_debug(f"restart_local_backend result={ok}")
        return ok

    def _build_sidebar(self) -> None:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sidebar.add_css_class("terminal-sidebar")
        sidebar.set_size_request(185, -1)
        sidebar.set_margin_top(10)
        sidebar.set_margin_bottom(10)
        sidebar.set_margin_start(10)
        sidebar.set_margin_end(0)

        brand = Gtk.Label(label="ARIA COMMUNITY")
        brand.add_css_class("brand-label")
        brand.set_halign(Gtk.Align.START)
        sidebar.append(brand)

        self.connection_label = Gtk.Label(label="disconnected")
        self.connection_label.add_css_class("sidebar-status-off")
        self.connection_label.set_halign(Gtk.Align.START)
        sidebar.append(self.connection_label)

        self.plan_label = Gtk.Label(label="local")
        self.plan_label.add_css_class("sidebar-plan")
        self.plan_label.set_halign(Gtk.Align.START)
        sidebar.append(self.plan_label)

        self.update_button = Gtk.Button(label="Browser")
        self.update_button.add_css_class("nav-button")
        self.update_button.connect("clicked", lambda _btn: self._open_browser_once())
        sidebar.append(self.update_button)

        section = Gtk.Label(label="SESSIONS")
        section.add_css_class("sidebar-section")
        section.set_halign(Gtk.Align.START)
        section.set_margin_top(8)
        sidebar.append(section)

        self.new_chat_button = Gtk.Button(label="+  New Chat")
        self.new_chat_button.add_css_class("nav-button")
        self.new_chat_button.connect("clicked", lambda _btn: self._new_session())
        sidebar.append(self.new_chat_button)

        self.recommended_button = Gtk.Button(label="Aria Home")
        self.recommended_button.add_css_class("nav-button-accent")
        self.recommended_button.connect("clicked", lambda _btn: self.go_home())
        sidebar.append(self.recommended_button)

        self.account_button = Gtk.Button(label="OpenAI Key")
        self.account_button.add_css_class("nav-button")
        self.account_button.connect("clicked", lambda _btn: self._show_api_key_dialog())
        sidebar.append(self.account_button)

        self.logout_button = Gtk.Button(label="Files")
        self.logout_button.add_css_class("nav-button")
        self.logout_button.connect("clicked", lambda _btn: self._open_files_once())
        sidebar.append(self.logout_button)

        self.session_list = Gtk.ListBox()
        self.session_list.add_css_class("session-list")
        self.session_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.session_list.set_activate_on_single_click(True)
        self.session_list.connect("row-selected", self._on_session_selected)
        self.session_list.connect("row-activated", self._on_session_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.session_list)
        sidebar.append(scroll)

        self.append(sidebar)

    def _build_workspace(self) -> None:
        workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        workspace.add_css_class("workspace-stage")
        workspace.set_hexpand(True)
        workspace.set_vexpand(True)
        workspace.set_margin_top(10)
        workspace.set_margin_bottom(10)
        workspace.set_margin_start(0)
        workspace.set_margin_end(10)

        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        strip.add_css_class("workspace-strip")

        self.titlebar_label = Gtk.Label(label="Terminal Aria")
        self.titlebar_label.add_css_class("workspace-strip-title")
        self.titlebar_label.set_halign(Gtk.Align.CENTER)
        self.titlebar_label.set_hexpand(True)
        strip.append(self.titlebar_label)

        strip_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        strip_actions.set_halign(Gtk.Align.END)

        self.strip_access_button = Gtk.Button(label="Access")
        self.strip_access_button.add_css_class("dock-button")
        self.strip_access_button.connect("clicked", lambda _btn: self._show_access_dialog())
        self.strip_access_button.set_visible(False)
        strip_actions.append(self.strip_access_button)

        self.strip_key_button = Gtk.Button(label="OpenAI Key")
        self.strip_key_button.add_css_class("dock-button")
        self.strip_key_button.connect("clicked", lambda _btn: self._show_api_key_dialog())
        strip_actions.append(self.strip_key_button)

        self.strip_model_button = Gtk.Button(label="Model: GPT-5.4")
        self.strip_model_button.add_css_class("dock-button")
        self.strip_model_button.connect("clicked", lambda _btn: self._show_model_dialog())
        strip_actions.append(self.strip_model_button)

        self.strip_voice_name_button = Gtk.Button(label="Voice: MARIN")
        self.strip_voice_name_button.add_css_class("dock-button")
        self.strip_voice_name_button.connect("clicked", lambda _btn: self._show_voice_name_dialog())
        self.strip_voice_name_button.set_visible(False)
        strip_actions.append(self.strip_voice_name_button)

        self.strip_voice_language_button = Gtk.Button(label="Language: EN")
        self.strip_voice_language_button.add_css_class("dock-button")
        self.strip_voice_language_button.connect("clicked", lambda _btn: self._show_voice_language_dialog())
        self.strip_voice_language_button.set_visible(False)
        strip_actions.append(self.strip_voice_language_button)

        self.strip_voice_button = Gtk.ToggleButton(label="Voice Mode")
        self.strip_voice_button.add_css_class("dock-button")
        self.strip_voice_button.add_css_class("voice-toggle-button")
        self._voice_toggle_handler_id = self.strip_voice_button.connect("toggled", self._on_voice_toggled)
        self.strip_voice_button.set_visible(False)
        strip_actions.append(self.strip_voice_button)

        strip.append(strip_actions)
        workspace.append(strip)

        self.overlay = Gtk.Overlay()
        self.overlay.set_hexpand(True)
        self.overlay.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)

        self.message_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.message_list.set_margin_top(26)
        self.message_list.set_margin_bottom(26)
        self.message_list.set_margin_start(26)
        self.message_list.set_margin_end(26)

        self.message_scroll = Gtk.ScrolledWindow()
        self.message_scroll.add_css_class("workspace-scroll")
        self.message_scroll.set_hexpand(True)
        self.message_scroll.set_vexpand(True)
        self.message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        self.message_scroll.set_propagate_natural_height(False)
        self.message_scroll.set_child(self.message_list)
        content.append(self.message_scroll)

        self.attachment_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.attachment_row.add_css_class("attachment-row")
        self.attachment_row.set_margin_top(8)
        self.attachment_row.set_margin_bottom(8)
        self.attachment_row.set_margin_start(16)
        self.attachment_row.set_margin_end(16)

        self.attachment_label = Gtk.Label(label="")
        self.attachment_label.add_css_class("attachment-label")
        self.attachment_label.set_xalign(0)
        self.attachment_label.set_hexpand(True)
        self.attachment_row.append(self.attachment_label)

        self.attachment_clear_button = Gtk.Button(label="Remove")
        self.attachment_clear_button.add_css_class("dock-button")
        self.attachment_clear_button.connect("clicked", lambda _btn: self._clear_pending_image())
        self.attachment_row.append(self.attachment_clear_button)
        content.append(self.attachment_row)

        dock = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dock.add_css_class("composer-dock")
        dock.set_margin_top(0)
        dock.set_margin_bottom(0)
        dock.set_margin_start(16)
        dock.set_margin_end(16)

        self.attach_button = Gtk.Button(label="Image")
        self.attach_button.add_css_class("dock-button")
        self.attach_button.connect("clicked", lambda _btn: self._choose_image())
        dock.append(self.attach_button)

        self.composer_entry = Gtk.Entry()
        self.composer_entry.add_css_class("dock-input")
        self.composer_entry.set_placeholder_text("What can I do for you?")
        self.composer_entry.set_hexpand(True)
        self.composer_entry.connect("activate", lambda _entry: self._send_prompt())
        dock.append(self.composer_entry)

        self.send_button = Gtk.Button(label="SEND")
        self.send_button.add_css_class("dock-send")
        self.send_button.connect("clicked", lambda _btn: self._send_prompt())
        dock.append(self.send_button)

        self.stop_button = Gtk.Button(label="STOP")
        self.stop_button.add_css_class("dock-stop")
        self.stop_button.connect("clicked", lambda _btn: self._request_stop())
        dock.append(self.stop_button)
        content.append(dock)

        self.overlay.set_child(content)

        self.empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.empty_box.set_halign(Gtk.Align.CENTER)
        self.empty_box.set_valign(Gtk.Align.CENTER)
        self.empty_box.set_vexpand(True)
        self.empty_title = Gtk.Label(label="Start a conversation with Terminal Aria")
        self.empty_title.add_css_class("workspace-empty")
        self.empty_title.set_halign(Gtk.Align.CENTER)
        self.empty_hint = Gtk.Label(label="")
        self.empty_hint.add_css_class("workspace-subtle")
        self.empty_hint.set_halign(Gtk.Align.CENTER)
        self.empty_box.append(self.empty_title)
        self.empty_box.append(self.empty_hint)
        self.overlay.add_overlay(self.empty_box)

        self.state_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.state_overlay.add_css_class("overlay-card")
        self.state_overlay.set_halign(Gtk.Align.CENTER)
        self.state_overlay.set_valign(Gtk.Align.CENTER)
        self.state_overlay.set_size_request(420, -1)
        self.state_overlay.set_margin_top(28)
        self.state_overlay.set_margin_bottom(28)
        self.state_overlay.set_margin_start(28)
        self.state_overlay.set_margin_end(28)

        state_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        state_inner.set_margin_top(18)
        state_inner.set_margin_bottom(18)
        state_inner.set_margin_start(18)
        state_inner.set_margin_end(18)

        self.overlay_kicker = Gtk.Label(label="LOCAL RUNTIME")
        self.overlay_kicker.add_css_class("brand-label")
        self.overlay_kicker.set_halign(Gtk.Align.START)
        state_inner.append(self.overlay_kicker)

        self.overlay_body = Gtk.Label(label="")
        self.overlay_body.add_css_class("overlay-text")
        self.overlay_body.set_wrap(True)
        self.overlay_body.set_xalign(0)
        state_inner.append(self.overlay_body)

        self.overlay_status = Gtk.Label(label="")
        self.overlay_status.add_css_class("overlay-meta")
        self.overlay_status.set_wrap(True)
        self.overlay_status.set_xalign(0)
        state_inner.append(self.overlay_status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.overlay_primary = Gtk.Button(label="Connect Key")
        self.overlay_primary.add_css_class("dock-send")
        self.overlay_secondary = Gtk.Button(label="Browser")
        self.overlay_secondary.add_css_class("dock-button")
        actions.append(self.overlay_primary)
        actions.append(self.overlay_secondary)
        state_inner.append(actions)

        self.state_overlay.append(state_inner)
        self.overlay.add_overlay(self.state_overlay)

        self.voice_overlay = VoiceOverlay()
        self.overlay.add_overlay(self.voice_overlay)

        self.update_scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.update_scrim.add_css_class("update-scrim")
        self.update_scrim.set_hexpand(True)
        self.update_scrim.set_vexpand(True)
        self.update_scrim.set_halign(Gtk.Align.FILL)
        self.update_scrim.set_valign(Gtk.Align.FILL)
        self.update_scrim.set_visible(False)

        update_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        update_shell.add_css_class("update-scrim-card")
        update_shell.set_halign(Gtk.Align.CENTER)
        update_shell.set_valign(Gtk.Align.CENTER)
        update_shell.set_size_request(420, -1)
        update_shell.set_margin_top(24)
        update_shell.set_margin_bottom(24)
        update_shell.set_margin_start(24)
        update_shell.set_margin_end(24)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(26)
        inner.set_margin_bottom(26)
        inner.set_margin_start(26)
        inner.set_margin_end(26)

        self.update_spinner = Gtk.Spinner()
        self.update_spinner.set_size_request(44, 44)
        self.update_spinner.set_halign(Gtk.Align.CENTER)
        inner.append(self.update_spinner)

        self.update_scrim_title = Gtk.Label(label="Installing AriaOS update")
        self.update_scrim_title.add_css_class("brand-label")
        self.update_scrim_title.set_halign(Gtk.Align.CENTER)
        inner.append(self.update_scrim_title)

        self.update_scrim_body = Gtk.Label(
            label="Aria will restart automatically when the update finishes."
        )
        self.update_scrim_body.add_css_class("overlay-text")
        self.update_scrim_body.set_wrap(True)
        self.update_scrim_body.set_xalign(0.5)
        self.update_scrim_body.set_justify(Gtk.Justification.CENTER)
        inner.append(self.update_scrim_body)

        update_shell.append(inner)
        self.update_scrim.append(update_shell)
        self.overlay.add_overlay(self.update_scrim)
        workspace.append(self.overlay)
        self.append(workspace)

    def _run_on_ui_thread_sync(self, callback):
        if threading.current_thread() is threading.main_thread():
            return callback()
        result: dict[str, object] = {}
        completed = threading.Event()

        def _runner() -> bool:
            try:
                result["value"] = callback()
            except Exception as exc:
                result["error"] = exc
            finally:
                completed.set()
            return False

        GLib.idle_add(_runner)
        if not completed.wait(8.0):
            raise TimeoutError("Voice mode UI command timed out.")
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        return result.get("value")

    def _dispatch_voice_ui(self, callback, *args) -> None:
        def _runner() -> bool:
            callback(*args)
            return False

        GLib.idle_add(_runner)

    def _set_voice_button_state(self, active: bool) -> None:
        if self.strip_voice_button is None:
            return
        try:
            self.strip_voice_button.handler_block(self._voice_toggle_handler_id)
            self.strip_voice_button.set_active(bool(active))
        finally:
            self.strip_voice_button.handler_unblock(self._voice_toggle_handler_id)

    def _voice_name(self) -> str:
        return str(getattr(self.state, "voice_name", "marin") or "marin").strip().lower() or "marin"

    def _voice_name_display(self, voice_name: str | None = None) -> str:
        name = str(voice_name or self._voice_name() or "marin").strip().lower() or "marin"
        return VOICE_NAME_LABELS.get(name, name.title())

    def _voice_language(self) -> str:
        value = str(getattr(self.state, "voice_language", "en") or "en").strip().lower() or "en"
        if value in SUPPORTED_VOICE_LANGUAGES:
            return value
        return "en"

    def _voice_language_display(self, language: str | None = None) -> str:
        code = str(language or self._voice_language() or "en").strip().lower() or "en"
        return VOICE_LANGUAGE_LABELS.get(code, code.upper())

    def _refresh_voice_name_button(self) -> None:
        if self.strip_voice_name_button is None:
            return
        self.strip_voice_name_button.set_label(f"Voice: {self._voice_name_display()}")

    def _refresh_voice_language_button(self) -> None:
        if self.strip_voice_language_button is None:
            return
        self.strip_voice_language_button.set_label(f"Language: {self._voice_language_display()}")

    def _show_voice_name_dialog(self) -> None:
        win = Gtk.Window(title="Voice", modal=True, transient_for=self.get_root())
        win.set_default_size(360, 420)
        win.set_resizable(False)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        shell.add_css_class("overlay-card")
        shell.set_margin_top(18)
        shell.set_margin_bottom(18)
        shell.set_margin_start(18)
        shell.set_margin_end(18)

        title = Gtk.Label(label="VOICE")
        title.add_css_class("brand-label")
        title.set_halign(Gtk.Align.START)
        shell.append(title)

        body = Gtk.Label(
            label="Choose which OpenAI voice Jarvis should use when speaking back to you."
        )
        body.add_css_class("overlay-text")
        body.set_wrap(True)
        body.set_xalign(0)
        shell.append(body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shell.append(buttons)

        current = self._voice_name()
        for voice_name in SUPPORTED_OPENAI_VOICE_NAMES:
            button = Gtk.Button(label=self._voice_name_display(voice_name))
            button.add_css_class("dock-button")
            if voice_name == current:
                button.add_css_class("nav-button-accent")
            button.connect("clicked", lambda _btn, selected=voice_name: self._select_voice_name(win, selected))
            buttons.append(button)

        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("dock-button")
        close_button.set_halign(Gtk.Align.END)
        close_button.connect("clicked", lambda *_args: win.destroy())
        shell.append(close_button)

        win.set_child(shell)
        win.present()

    def _select_voice_name(self, window: Gtk.Window, voice_name: str) -> None:
        selected = self.service.save_voice_name(voice_name)
        self.refresh_state(self.service.read())
        if self.voice_overlay is not None:
            self.voice_overlay.set_voice_name(selected)
        if self.voice_mode_enabled:
            self._stop_voice_mode()
            self._start_voice_mode()
        self._append_system_notice(f"Voice set to {self._voice_name_display(selected)}.")
        window.destroy()

    def _show_voice_language_dialog(self) -> None:
        win = Gtk.Window(title="Language", modal=True, transient_for=self.get_root())
        win.set_default_size(360, 360)
        win.set_resizable(False)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        shell.add_css_class("overlay-card")
        shell.set_margin_top(18)
        shell.set_margin_bottom(18)
        shell.set_margin_start(18)
        shell.set_margin_end(18)

        title = Gtk.Label(label="LANGUAGE")
        title.add_css_class("brand-label")
        title.set_halign(Gtk.Align.START)
        shell.append(title)

        body = Gtk.Label(
            label="Choose the language Jarvis should expect and speak by default."
        )
        body.add_css_class("overlay-text")
        body.set_wrap(True)
        body.set_xalign(0)
        shell.append(body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shell.append(buttons)

        current = self._voice_language()
        for language in SUPPORTED_VOICE_LANGUAGES:
            button = Gtk.Button(label=self._voice_language_display(language))
            button.add_css_class("dock-button")
            if language == current:
                button.add_css_class("nav-button-accent")
            button.connect("clicked", lambda _btn, selected=language: self._select_voice_language(win, selected))
            buttons.append(button)

        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("dock-button")
        close_button.set_halign(Gtk.Align.END)
        close_button.connect("clicked", lambda *_args: win.destroy())
        shell.append(close_button)

        win.set_child(shell)
        win.present()

    def _select_voice_language(self, window: Gtk.Window, language: str) -> None:
        selected = self.service.save_voice_language(language)
        self.refresh_state(self.service.read())
        if self.voice_mode_enabled:
            self._stop_voice_mode()
            self._start_voice_mode()
        self._append_system_notice(f"Language set to {self._voice_language_display(selected)}.")
        window.destroy()

    def _voice_runtime_allowed(self) -> tuple[bool, str]:
        return False, "Voice mode is disabled in this local-first AriaOS build for now."

    def _voice_supports_followups(self) -> bool:
        return bool(
            self.current_running
            and self._active_remote_task_id
            and hasattr(self.bridge, "send_task_message")
            and callable(getattr(self.bridge, "send_task_message", None))
        )

    def _build_voice_snapshot(self) -> dict[str, object]:
        snapshot = dict(self._voice_snapshot)
        if self.current_running:
            payload = _parse_assistant_payload(self.current_stream_buffer)
            current_status = str(snapshot.get("status") or "running").strip() or "running"
            if self._active_remote_paused:
                status = "paused"
            elif current_status == "dispatching" and not self.current_stream_buffer.strip():
                status = "dispatching"
            else:
                status = "running"
            summary = str(snapshot.get("latest_summary") or "").strip()
            if payload.progress:
                summary = payload.progress[-1]
            elif payload.answer and payload.answer not in {"Working…", "Thinking…"}:
                summary = _sanitize_terminal_result_text(payload.answer)[:280]
            elif self.current_stream_buffer:
                summary = self._remote_progress_summary(self.current_stream_buffer) or summary
            latest_action = payload.actions[-1] if payload.actions else str(snapshot.get("latest_action") or "")
            latest_progress = (
                payload.progress[-1]
                if payload.progress
                else (payload.steps[-1] if payload.steps else str(snapshot.get("latest_progress") or ""))
            )
            snapshot.update(
                {
                    "status": status,
                    "session_id": str(self.active_request_session_id or self.selected_session_id or snapshot.get("session_id") or ""),
                    "task_id": str(self._active_remote_task_id or self.active_request_session_id or snapshot.get("task_id") or ""),
                    "goal": str(self._active_goal_text or snapshot.get("goal") or ""),
                    "latest_summary": summary,
                    "latest_action": latest_action,
                    "latest_progress": latest_progress,
                    "error": "",
                    "paused": bool(self._active_remote_paused),
                    "supports_followups": self._voice_supports_followups(),
                    "updated_at": time.time(),
                }
            )
        elif str(snapshot.get("status") or "").strip() not in {"completed", "failed", "stopped"}:
            snapshot.update(
                {
                    "status": "idle",
                    "session_id": str(self.selected_session_id or snapshot.get("session_id") or ""),
                    "task_id": "",
                    "goal": "",
                    "latest_summary": "",
                    "latest_result": "",
                    "latest_action": "",
                    "latest_progress": "",
                    "error": "",
                    "paused": False,
                    "supports_followups": False,
                    "updated_at": time.time(),
                }
            )
        return snapshot

    def _push_voice_snapshot(self, **updates: object) -> None:
        snapshot = dict(self._voice_snapshot)
        snapshot.update(updates)
        snapshot["updated_at"] = time.time()
        self._voice_snapshot = snapshot
        if self.voice_mode_enabled and self.voice_client is not None:
            self.voice_client.publish_snapshot(dict(snapshot))

    def _publish_voice_snapshot(self) -> None:
        snapshot = self._build_voice_snapshot()
        self._voice_snapshot = snapshot
        if self.voice_mode_enabled and self.voice_client is not None:
            self.voice_client.publish_snapshot(dict(snapshot))

    def _on_voice_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            if not self._start_voice_mode():
                self._set_voice_button_state(False)
            return
        self._stop_voice_mode()

    def _start_voice_mode(self) -> bool:
        allowed, message = self._voice_runtime_allowed()
        if not allowed:
            self._append_system_notice(message)
            return False
        if self.voice_client is None:
            self.voice_client = VoiceClient(
                service=self.service,
                snapshot_provider=self._build_voice_snapshot,
                command_handler=self._handle_voice_command,
                on_state=self._on_voice_state_event,
                on_transcript=self._on_voice_transcript_event,
                on_spoken_text=self._on_voice_spoken_text_event,
                on_error=self._on_voice_error_event,
            )
        self.voice_mode_enabled = True
        if self.voice_overlay is not None:
            self.voice_overlay.clear_transcript()
            self.voice_overlay.set_voice_name(self._voice_name())
            self.voice_overlay.set_mode_visible(True)
            self.voice_overlay.set_state("thinking")
        started = self.voice_client.start()
        if not started:
            self.voice_mode_enabled = False
            if self.voice_overlay is not None:
                self.voice_overlay.set_mode_visible(False)
            return False
        self._set_voice_button_state(True)
        self._publish_voice_snapshot()
        self.voice_client.set_voice_name(self._voice_name())
        return True

    def _stop_voice_mode(self) -> None:
        self.voice_mode_enabled = False
        if self.voice_client is not None:
            self.voice_client.stop()
            self.voice_client = None
        if self.voice_overlay is not None:
            self.voice_overlay.clear_transcript()
            self.voice_overlay.set_state("idle")
            self.voice_overlay.set_mode_visible(False)
        self._set_voice_button_state(False)

    def _handle_voice_command(self, command_id: str, command: str, text: str) -> tuple[bool, str]:
        try:
            result = self._run_on_ui_thread_sync(lambda: self._handle_voice_command_ui(command_id, command, text))
        except Exception as exc:
            self._log_debug(f"voice_command_error command={command} exc={exc}")
            return False, str(exc) or "Voice command failed."
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1])
        return False, "Voice command failed."

    def _handle_voice_command_ui(self, command_id: str, command: str, text: str) -> tuple[bool, str]:
        kind = str(command or "").strip().lower()
        body = str(text or "").strip()
        if kind == "start_task":
            if self.current_running:
                return False, "Aria is already busy on another task."
            if not body:
                return False, "No task text was provided."
            ok = self._submit_prompt_payload(body)
            if not ok:
                return False, "Aria could not start that task right now."
            self._publish_voice_snapshot()
            return True, "Task started."
        if kind == "send_followup":
            if not self.current_running:
                return False, "There is no active Aria task to guide right now."
            if not self._voice_supports_followups():
                return False, "The current task does not support live voice follow-ups yet."
            if not body:
                return False, "No follow-up text was provided."
            ok = bool(self.bridge.send_task_message(body, role="voice"))
            if not ok:
                return False, "Aria could not apply that live instruction."
            preview = _sanitize_terminal_result_text(body)[:200]
            if preview:
                self._append_system_notice(f"Voice mode: {preview}")
            self._publish_voice_snapshot()
            return True, "Live instruction sent."
        if kind == "stop_task":
            if not self.current_running:
                return False, "There is no active Aria task to stop."
            self._request_stop()
            self._publish_voice_snapshot()
            return True, "Stopping the current task."
        return False, "Unsupported voice command."

    def _on_voice_state_event(self, state: str) -> None:
        self._dispatch_voice_ui(self._apply_voice_state, state)

    def _apply_voice_state(self, state: str) -> None:
        if not self.voice_mode_enabled or self.voice_overlay is None:
            return
        self.voice_overlay.set_voice_name(self._voice_name())
        self.voice_overlay.set_state(state)

    def _on_voice_transcript_event(self, text: str) -> None:
        self._dispatch_voice_ui(self._apply_voice_transcript, text)

    def _apply_voice_transcript(self, text: str) -> None:
        if not self.voice_mode_enabled or self.voice_overlay is None:
            return
        self.voice_overlay.set_user_text(text)
        self.voice_overlay.set_assistant_text("")

    def _on_voice_spoken_text_event(self, text: str) -> None:
        self._dispatch_voice_ui(self._apply_voice_spoken_text, text)

    def _apply_voice_spoken_text(self, text: str) -> None:
        if not self.voice_mode_enabled or self.voice_overlay is None:
            return
        self.voice_overlay.set_assistant_text(text)

    def _on_voice_error_event(self, text: str) -> None:
        self._dispatch_voice_ui(self._apply_voice_error, text)

    def _apply_voice_error(self, text: str) -> None:
        if not self.voice_mode_enabled or self.voice_overlay is None:
            return
        self.voice_overlay.set_error(text)

    def _clear_messages(self) -> None:
        child = self.message_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.message_list.remove(child)
            child = next_child

    def _render_session_list(self) -> None:
        child = self.session_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.session_list.remove(child)
            child = next_child

        for summary in self.sessions:
            row = Gtk.ListBoxRow()
            row.add_css_class("session-row")
            row.set_name(summary.session_id)
            row.set_activatable(True)
            row.set_selectable(True)
            row.session_id = summary.session_id

            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.set_margin_top(12)
            card.set_margin_bottom(12)
            card.set_margin_start(10)
            card.set_margin_end(10)

            title = Gtk.Label(label=summary.title or "New Chat")
            title.add_css_class("session-title")
            title.set_xalign(0)
            title.set_halign(Gtk.Align.START)

            last_role = "assistant"
            try:
                messages = self.history.get_messages(summary.session_id, limit=1)
                if messages:
                    last_role = _message_role(messages[-1])
            except Exception:
                pass
            preview = Gtk.Label(label=_preview_text(summary.last_message, last_role))
            preview.add_css_class("session-snippet")
            preview.set_wrap(True)
            preview.set_xalign(0)

            card.append(title)
            card.append(preview)
            row.set_child(card)
            self.session_list.append(row)
            if summary.session_id == self.selected_session_id:
                self.session_list.select_row(row)

    def _selected_summary(self) -> SessionSummary | None:
        return next((item for item in self.sessions if item.session_id == self.selected_session_id), None)

    def _uses_remote_gateway(self) -> bool:
        return bool(self.state.remote_url.strip())

    def _runtime_ready(self) -> bool:
        if self._uses_remote_gateway():
            return self.backend_connected
        return self.state.api_key_ready and self.backend_connected

    def _on_session_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self.current_running:
            return
        selected = row.get_name() or getattr(row, "session_id", None)
        if not selected:
            return
        self.selected_session_id = selected
        try:
            self.bridge.reset_session(selected)
        except Exception as exc:
            self._log_debug(f"reset_session failed for {selected}: {exc}")
        self._render_messages()

    def _on_session_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._on_session_selected(listbox, row)

    def _set_connection_state(self) -> None:
        connected = self._runtime_ready()
        if connected:
            label = "local runtime connected"
        elif not self.state.api_key_ready:
            label = "openai key required"
        else:
            label = "local runtime offline"
        self.connection_label.set_label(label)
        self.connection_label.remove_css_class("sidebar-status")
        self.connection_label.remove_css_class("sidebar-status-off")
        self.connection_label.add_css_class("sidebar-status" if connected else "sidebar-status-off")
        self.plan_label.set_label(self._plan_label_text())

    def _plan_label_text(self) -> str:
        version = f"v{self.state.app_build}" if self.state.app_build else (self.state.app_version or "dev")
        return f"local · {version}"

    def _maybe_show_activation_notice(self, previous_state: RuntimeState | None, new_state: RuntimeState) -> None:
        pending_plan = new_state.activation_notice_plan
        if pending_plan:
            self.service.consume_activation_notice()
        if not pending_plan or pending_plan == self._activation_notice_shown_for:
            return
        if pending_plan not in {"monthly_pro", "annual_pro"}:
            return
        if previous_state and previous_state.plan_status == "active" and previous_state.plan_key == pending_plan:
            return
        self._activation_notice_shown_for = pending_plan
        self._show_activation_notice(pending_plan)

    def _show_activation_notice(self, plan_key: str) -> None:
        plan_label = "Monthly Pro" if plan_key == "monthly_pro" else "Annual Pro"
        win = Gtk.Window(title="Subscription activated", modal=True, transient_for=self.get_root())
        win.set_default_size(420, 230)
        win.set_resizable(False)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        shell.add_css_class("overlay-card")
        shell.set_margin_top(18)
        shell.set_margin_bottom(18)
        shell.set_margin_start(18)
        shell.set_margin_end(18)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        title = Gtk.Label(label="SUBSCRIPTION ACTIVE")
        title.add_css_class("brand-label")
        title.set_halign(Gtk.Align.START)
        inner.append(title)

        body = Gtk.Label(
            label=f"{plan_label} is now active on this account. Terminal Aria is unlocked and ready to run."
        )
        body.add_css_class("overlay-text")
        body.set_wrap(True)
        body.set_xalign(0)
        inner.append(body)

        close_btn = Gtk.Button(label="Continue")
        close_btn.add_css_class("dock-send")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda *_args: win.destroy())
        inner.append(close_btn)

        shell.append(inner)
        win.set_child(shell)
        win.present()

    def _render_messages(self) -> None:
        self._clear_messages()
        self.streaming_row = None
        self.streaming_body_label = None
        self.streaming_details_expander = None
        self.streaming_details_box = None
        self.streaming_details_scroller = None
        self.streaming_detail_sections = {}
        self.current_stream_buffer = ""
        self._set_connection_state()

        self.titlebar_label.set_label("Terminal Aria")

        messages = self.history.get_messages(self.selected_session_id) if self.selected_session_id else []
        if messages:
            for message in messages:
                self.message_list.append(self._build_message_row(message))

        self._render_empty_hint(messages)
        self._render_overlay(has_messages=bool(messages))
        self._refresh_attachment_ui()
        self._sync_controls()
        self._scroll_messages_to_bottom()

    def _render_empty_hint(self, messages: list[SessionMessage]) -> None:
        if messages:
            self.empty_box.set_visible(False)
            return
        self.empty_box.set_visible(True)
        if not self._selected_summary():
            self.empty_hint.set_label("Create a session from the left rail.")
        elif not self.state.api_key_ready and not self._uses_remote_gateway():
            self.empty_hint.set_label("Connect your OpenAI key to continue.")
        elif not self.backend_connected:
            if self._uses_remote_gateway():
                self.empty_hint.set_label("Connect the remote link to continue.")
            else:
                self.empty_hint.set_label("Start the local OpenAI runtime to continue.")
        else:
            self.empty_hint.set_label("")

    def _render_overlay(self, *, has_messages: bool = False) -> None:
        for button, callback in (
            (self.overlay_primary, self._open_key_dialog_once),
            (self.overlay_primary, self._reconnect_backend_once),
            (self.overlay_secondary, self._open_browser_once),
            (self.overlay_secondary, self._open_files_once),
            (self.overlay_secondary, self._open_key_dialog_once),
        ):
            try:
                button.disconnect_by_func(callback)
            except Exception:
                pass
        if has_messages:
            self.state_overlay.set_visible(False)
            return
        if not self.state.api_key_ready:
            self.empty_box.set_visible(False)
            self.overlay_kicker.set_label("LOCAL OPENAI KEY")
            self.overlay_body.set_label(
                "Connect your OpenAI key locally to unlock Terminal Aria in AriaOS."
            )
            self.overlay_status.set_label("The key is stored locally on this VM and the local backend restarts after save.")
            self.overlay_primary.set_label("Connect Key")
            self.overlay_secondary.set_label("Browser")
            self.overlay_primary.connect("clicked", self._open_key_dialog_once)
            self.overlay_secondary.connect("clicked", self._open_browser_once)
            self.state_overlay.set_visible(True)
            return
        if not self.backend_connected:
            self.empty_box.set_visible(False)
            self.overlay_kicker.set_label("RUNTIME OFFLINE")
            if self._uses_remote_gateway():
                self.overlay_body.set_label(
                    "Terminal Aria cannot reach the remote link. Reconnect the remote stack first."
                )
                self.overlay_status.set_label("The terminal will stay locked until the remote link is reachable.")
                self.overlay_primary.set_label("Refresh Access")
                self.overlay_secondary.set_label("Open Control Tower")
                self.overlay_primary.connect("clicked", self._refresh_access_once)
                self.overlay_secondary.connect("clicked", self._open_tower_once)
            else:
                self.overlay_body.set_label(
                    "Terminal Aria cannot reach the local backend. Start or reconnect the local runtime first."
                )
                self.overlay_status.set_label("The terminal will stay locked until the local websocket is reachable.")
                self.overlay_primary.set_label("Reconnect Runtime")
                self.overlay_secondary.set_label("OpenAI Key")
                self.overlay_primary.connect("clicked", self._reconnect_backend_once)
                self.overlay_secondary.connect("clicked", self._open_key_dialog_once)
            self.state_overlay.set_visible(True)
            return
        self.state_overlay.set_visible(False)

    def _open_browser_once(self, *_args) -> None:
        self.service.open_browser()
        self.overlay_status.set_label("Opening the browser…")

    def _open_files_once(self, *_args) -> None:
        self.service.open_files()
        self.overlay_status.set_label("Opening local files…")

    def _open_billing_once(self, *_args) -> None:
        opened = self.service.open_billing()
        if not opened:
            self.overlay_status.set_label("Billing could not be opened. Start the browser and try again.")
        else:
            self.overlay_status.set_label("Opening billing in the browser…")

    def _set_update_scrim(self, *, visible: bool, message: str = "") -> None:
        self._update_in_progress = visible
        if message:
            self.update_scrim_body.set_label(message)
        if visible:
            self.update_spinner.start()
        else:
            self.update_spinner.stop()
        self.update_scrim.set_visible(visible)
        self._sync_controls()

    def _refresh_update_status_async(self, *, open_gate_on_available: bool = False) -> None:
        if self._update_check_inflight or self._update_in_progress:
            return
        self._update_check_inflight = True
        self._sync_controls()

        def _worker() -> None:
            ok, available, message = self.service.refresh_client_update_status()

            def _finish() -> bool:
                self._update_check_inflight = False
                self.refresh_state(self.service.read())
                if open_gate_on_available and available:
                    self.overlay_status.set_label(message)
                    self._open_update_password_gate()
                elif open_gate_on_available:
                    self.overlay_status.set_label(message)
                    self._append_system_notice(message)
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _restart_after_update(self) -> None:
        self.service.launch_shell_window("terminal")

        def _close() -> bool:
            root = self.get_root()
            if isinstance(root, Gtk.Window):
                app = root.get_application()
                root.close()
                if app is not None:
                    app.quit()
            return False

        GLib.timeout_add(350, _close)

    def _run_update_with_password(self, password: str) -> None:
        if self._update_in_progress:
            return
        self.overlay_status.set_label("Installing AriaOS update…")
        self._set_update_scrim(
            visible=True,
            message="Installing the latest AriaOS client. Aria will restart automatically when the update finishes.",
        )

        def _progress(message: str) -> None:
            GLib.idle_add(self.update_scrim_body.set_label, message)

        def _worker() -> None:
            ok, message = self.service.apply_client_update(vm_admin_password=password, progress=_progress)

            def _finish() -> bool:
                if ok:
                    self.refresh_state(self.service.read())
                    self.update_scrim_body.set_label("Update installed. Restarting AriaOS…")
                    self.overlay_status.set_label(message)
                    self._restart_after_update()
                    return False
                self._set_update_scrim(visible=False)
                self.refresh_state(self.service.read())
                self.overlay_status.set_label(message)
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_update_password_gate(self) -> None:
        if self._update_password_window is not None:
            self._update_password_window.present()
            return

        root = self.get_root()
        win = Gtk.Window(title="Install AriaOS update", modal=True, transient_for=root if isinstance(root, Gtk.Window) else None)
        if isinstance(root, Gtk.Window):
            application = root.get_application()
            if application is not None:
                win.set_application(application)
        win.set_default_size(420, 220)
        win.set_resizable(False)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        shell.add_css_class("overlay-card")
        shell.set_margin_top(18)
        shell.set_margin_bottom(18)
        shell.set_margin_start(18)
        shell.set_margin_end(18)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        title = Gtk.Label(label="INSTALL UPDATE")
        title.add_css_class("brand-label")
        title.set_halign(Gtk.Align.START)
        inner.append(title)

        body = Gtk.Label(
            label="Enter the VM admin password to replace the local AriaOS client files in /opt/aria-client."
        )
        body.add_css_class("overlay-text")
        body.set_wrap(True)
        body.set_xalign(0)
        inner.append(body)

        status = Gtk.Label(label="")
        status.add_css_class("overlay-meta")
        status.set_wrap(True)
        status.set_xalign(0)
        inner.append(status)

        password_entry = Gtk.Entry()
        password_entry.add_css_class("dock-input")
        password_entry.set_placeholder_text("VM admin password")
        password_entry.set_visibility(False)
        saved_password = self.service.vm_admin_password()
        if saved_password:
            password_entry.set_text(saved_password)
        inner.append(password_entry)

        remember = Gtk.CheckButton(label="Remember on this VM")
        remember.set_active(True)
        inner.append(remember)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("dock-button")
        install = Gtk.Button(label="Install update")
        install.add_css_class("dock-send")
        actions.append(cancel)
        actions.append(install)
        inner.append(actions)

        shell.append(inner)
        win.set_child(shell)

        def _close(*_args) -> None:
            if self._update_password_window is not None:
                self._update_password_window = None
            win.destroy()

        def _submit(*_args) -> None:
            password = password_entry.get_text().strip()
            if not password:
                status.set_label("VM admin password is required.")
                return
            if remember.get_active():
                self.service.save_vm_admin_password(password)
            _close()
            self._run_update_with_password(password)

        cancel.connect("clicked", _close)
        install.connect("clicked", _submit)
        password_entry.connect("activate", _submit)
        win.connect("destroy", lambda *_args: setattr(self, "_update_password_window", None))
        self._update_password_window = win
        win.present()

    def _start_update_once(self, *_args) -> None:
        if self.current_running:
            self.overlay_status.set_label("Stop the current task before installing an AriaOS update.")
            return
        if self._update_in_progress:
            return
        self.overlay_status.set_label("Checking for updates…")
        self._refresh_update_status_async(open_gate_on_available=True)

    def _open_download_once(self, *_args) -> None:
        opened = self.service.open_download(self.state.update_download_url)
        if not opened:
            self.overlay_status.set_label("The download page could not be opened. Open the latest OVA manually.")
        else:
            self.overlay_status.set_label("Opening the latest OVA download page…")

    def _open_tower_once(self, *_args) -> None:
        self.service.open_browser()

    def _open_key_dialog_once(self, *_args) -> None:
        self._show_api_key_dialog()

    def _open_unlock_once(self, *_args) -> None:
        self._show_access_dialog()

    def _refresh_access_once(self, *_args) -> None:
        self.overlay_secondary.set_sensitive(False)
        self.overlay_status.set_label("Refreshing access…")

        def _worker() -> None:
            ok, message = self.service.refresh_access()

            def _finish() -> bool:
                self.refresh_state(self.service.read())
                self.overlay_secondary.set_sensitive(True)
                self.overlay_status.set_label(message)
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_update_status_once(self, *_args) -> None:
        self.overlay_secondary.set_sensitive(False)
        self.overlay_status.set_label("Checking for updates…")

        def _worker() -> None:
            _ok, _available, message = self.service.refresh_client_update_status()

            def _finish() -> bool:
                self.refresh_state(self.service.read())
                self.overlay_secondary.set_sensitive(True)
                self.overlay_status.set_label(message)
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_legacy_once(self, *_args) -> None:
        self.service.launch_legacy_terminal()

    def _reconnect_backend_once(self, *_args) -> None:
        self._ensure_backend_ready(force=True)
        self.refresh_state(self.service.read())

    def _build_details_expander(self, payload: AssistantPayload) -> Gtk.Widget | None:
        if not payload.has_details:
            return None
        expander = Gtk.Expander(label="View steps and logs")
        expander.add_css_class("details-expander")
        expander.set_expanded(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(10)
        inner.set_margin_bottom(4)

        for title, lines in (
            ("Thoughts", payload.thoughts),
            ("Actions", payload.actions),
            ("Progress", payload.progress),
            ("Steps", payload.steps),
            ("Logs", payload.logs),
        ):
            if not lines:
                continue
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            header = Gtk.Label(label=title.upper())
            header.add_css_class("details-title")
            header.set_xalign(0)
            section.append(header)
            body = Gtk.Label(label="\n".join(lines))
            body.add_css_class("details-body")
            body.set_wrap(True)
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            body.set_xalign(0)
            body.set_selectable(True)
            section.append(body)
            inner.append(section)

        expander.set_child(inner)
        return expander

    def _build_live_details_expander(self) -> Gtk.Expander:
        expander = Gtk.Expander(label="Live steps and logs")
        expander.add_css_class("details-expander")
        expander.add_css_class("live-details-expander")
        expander.set_expanded(True)

        scroller = Gtk.ScrolledWindow()
        scroller.add_css_class("live-details-scroller")
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(120)
        scroller.set_max_content_height(220)
        scroller.set_propagate_natural_height(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(10)
        inner.set_margin_bottom(4)
        inner.set_margin_start(2)
        inner.set_margin_end(2)

        sections: dict[str, tuple[Gtk.Widget, Gtk.Label]] = {}
        for title in ("Thoughts", "Actions", "Progress", "Steps", "Logs"):
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            header = Gtk.Label(label=title.upper())
            header.add_css_class("details-title")
            header.set_xalign(0)
            section.append(header)

            body = Gtk.Label(label="")
            body.add_css_class("details-body")
            body.set_wrap(True)
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            body.set_xalign(0)
            body.set_selectable(True)
            section.append(body)
            section.set_visible(False)
            inner.append(section)
            sections[title] = (section, body)

        scroller.set_child(inner)
        expander.set_child(scroller)
        expander.sections = sections
        expander.details_box = inner
        expander.details_scroller = scroller
        expander.set_visible(False)
        return expander

    def _append_live_details_expander(self) -> None:
        if self.streaming_row is None or self.streaming_details_expander is not None:
            return
        expander = self._build_live_details_expander()
        self.streaming_details_expander = expander
        self.streaming_details_box = expander.details_box
        self.streaming_details_scroller = expander.details_scroller
        self.streaming_detail_sections = dict(expander.sections)
        if getattr(self.streaming_row, "inner_box", None) is not None:
            self.streaming_row.inner_box.append(expander)

    def _scroll_live_details_to_bottom(self) -> None:
        if self.streaming_details_scroller is None:
            return
        vadj = self.streaming_details_scroller.get_vadjustment()
        GLib.idle_add(vadj.set_value, max(0.0, vadj.get_upper() - vadj.get_page_size()))

    def _update_live_details_expander(self, payload: AssistantPayload) -> None:
        if not payload.has_details:
            return
        self._append_live_details_expander()
        if self.streaming_details_expander is None:
            return

        visible_any = False
        for title, lines in (
            ("Thoughts", payload.thoughts),
            ("Actions", payload.actions),
            ("Progress", payload.progress),
            ("Steps", payload.steps),
            ("Logs", payload.logs),
        ):
            section, body = self.streaming_detail_sections.get(title, (None, None))
            if section is None or body is None:
                continue
            values = [line for line in lines if str(line or "").strip()]
            section.set_visible(bool(values))
            if values:
                body.set_label("\n".join(values))
                visible_any = True
        self.streaming_details_expander.set_visible(visible_any)
        if visible_any and self.streaming_details_expander.get_expanded():
            self._scroll_live_details_to_bottom()

    def _build_status_strip(self, label_text: str, body_text: str, css_name: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.add_css_class("assistant-status-box")
        box.add_css_class(css_name)

        label = Gtk.Label(label=label_text)
        label.add_css_class("assistant-status-label")
        label.set_xalign(0)
        box.append(label)

        body = Gtk.Label(label=body_text)
        body.add_css_class("assistant-status-body")
        body.set_wrap(True)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_xalign(0)
        body.set_selectable(True)
        box.append(body)
        return box

    def _build_prompt_line(self, prompt: str, body_text: str, css_name: str) -> Gtk.Widget:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        line.add_css_class("terminal-line")
        line.add_css_class(css_name)

        label = Gtk.Label(label=prompt)
        label.add_css_class("terminal-prompt")
        label.set_xalign(0)
        label.set_valign(Gtk.Align.START)
        line.append(label)

        body = Gtk.Label(label=body_text)
        body.add_css_class("terminal-line-body")
        body.set_wrap(True)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_xalign(0)
        body.set_selectable(True)
        body.set_hexpand(True)
        line.append(body)
        line.body_label = body
        return line

    def _build_message_row(self, message: SessionMessage | None = None, *, role: str | None = None, content: str | None = None, timestamp: str | None = None) -> Gtk.Widget:
        payload: AssistantPayload | None = None
        if message is not None:
            role_name = _message_role(message)
            raw_content = message.content
            stamp = _pretty_timestamp(message.timestamp)
        else:
            role_name = role or "assistant"
            raw_content = content or ""
            stamp = _pretty_timestamp(timestamp or "")
        if role_name == "assistant":
            payload = _parse_assistant_payload(raw_content)
            body_text = _sanitize_terminal_result_text(payload.answer) or ("Working…" if payload.has_details else "Thinking…")
        else:
            body_text = _visible_message_text(role_name, raw_content) or " "

        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.add_css_class("message-row")
        row.set_hexpand(True)
        row.set_halign(Gtk.Align.FILL)
        row.body_label = None

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        frame.add_css_class("message-frame")
        frame.add_css_class("assistant-frame" if role_name == "assistant" else "user-frame")
        frame.set_margin_top(0)
        frame.set_margin_bottom(0)
        frame.set_margin_start(0)
        frame.set_margin_end(0)
        frame.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        row.inner_box = inner

        if stamp:
            meta = Gtk.Label(label=stamp)
            meta.add_css_class("message-meta")
            meta.set_xalign(0)
            meta.set_halign(Gtk.Align.START)
            inner.append(meta)

        if role_name == "assistant" and payload is not None:
            for thought in payload.thoughts:
                inner.append(self._build_prompt_line("THINK>", thought, "assistant-think-line"))
            for action in payload.actions:
                inner.append(self._build_prompt_line("ACTION>", action, "assistant-action-line"))
            for progress_line in payload.progress:
                inner.append(self._build_prompt_line("PROGRESS>", progress_line, "assistant-progress-line"))
            if body_text and body_text not in {"Working…", "Thinking…"}:
                result_line = self._build_prompt_line("RESULT>", body_text, "assistant-result-line")
                inner.append(result_line)
                row.body_label = result_line.body_label
            details = self._build_details_expander(payload)
            if details is not None:
                inner.append(details)
        else:
            user_line = self._build_prompt_line("YOU>", body_text, "user-terminal-line")
            inner.append(user_line)
            row.body_label = user_line.body_label

        if row.body_label is None:
            fallback_line = self._build_prompt_line("ARIA>", body_text or "Working…", "assistant-result-line")
            inner.append(fallback_line)
            row.body_label = fallback_line.body_label

        frame.append(inner)
        row.append(frame)
        return row

    def _scroll_messages_to_bottom(self) -> None:
        vadj = self.message_scroll.get_vadjustment()
        GLib.idle_add(vadj.set_value, max(0.0, vadj.get_upper() - vadj.get_page_size()))

    def _sync_controls(self) -> None:
        locked = self.current_running or self._update_in_progress or self._update_check_inflight
        can_send = (
            not locked
            and self._runtime_ready()
        )
        self.composer_entry.set_sensitive(can_send)
        self.send_button.set_sensitive(can_send)
        self.attach_button.set_sensitive(can_send)
        self.attachment_clear_button.set_sensitive(can_send and self.pending_image is not None)
        self.stop_button.set_sensitive(self.current_running and not self._update_in_progress)
        self.new_chat_button.set_sensitive(not locked)
        self.recommended_button.set_sensitive(not locked)
        self.account_button.set_sensitive(not locked)
        self.logout_button.set_sensitive(not locked)
        self.update_button.set_sensitive(not locked)
        self.session_list.set_sensitive(not locked)
        self.strip_access_button.set_sensitive(not locked)
        self.strip_key_button.set_sensitive(not locked)
        self.strip_model_button.set_sensitive(not locked)
        can_voice = False
        if self.strip_voice_button is not None:
            self.strip_voice_button.set_sensitive(can_voice)
        if self.strip_voice_name_button is not None:
            self.strip_voice_name_button.set_sensitive(can_voice)
        if self.strip_voice_language_button is not None:
            self.strip_voice_language_button.set_sensitive(can_voice)
        self.overlay_primary.set_sensitive(not self._update_in_progress and not self._update_check_inflight)
        self.overlay_secondary.set_sensitive(not self._update_in_progress and not self._update_check_inflight)

    def _new_session(self) -> None:
        if self.current_running:
            return
        session_id = uuid.uuid4().hex[:8]
        self.history.create_session(session_id, "New Chat")
        self.selected_session_id = session_id
        self._push_voice_snapshot(
            status="idle",
            session_id=session_id,
            task_id="",
            goal="",
            latest_summary="",
            latest_result="",
            latest_action="",
            latest_progress="",
            error="",
            paused=False,
            supports_followups=False,
        )
        self.refresh_state(self.state)
        self.bridge.reset_session(session_id)

    def _save_message(self, session_id: str, role: str, text: str) -> None:
        self.history.add_message(session_id, role, text)

    @staticmethod
    def _provisional_title(text: str) -> str:
        compact = _compact_text(text)
        if len(compact) <= 42:
            return compact or "New Chat"
        return compact[:39].rstrip() + "..."

    def _pack_request_text(self, session_id: str, user_prompt: str) -> str:
        transcript = self.history.get_messages(session_id, limit=self.state.max_transcript_messages)
        return self.service.pack_input(session_id, user_prompt, transcript)

    def _send_prompt(self) -> None:
        raw = self.composer_entry.get_text().strip()
        if not raw:
            return
        self.composer_entry.set_text("")
        image = self.pending_image
        self._clear_pending_image()
        ok = self._submit_prompt_payload(raw, image=image)
        if not ok:
            self.composer_entry.set_text(raw)

    def _append_system_notice(self, text: str) -> None:
        notice = self._build_message_row(role="assistant", content=text, timestamp="")
        self.message_list.append(notice)
        self._scroll_messages_to_bottom()

    def _append_streaming_placeholder(self) -> None:
        if self.streaming_row is not None:
            return
        row = self._build_message_row(role="assistant", content="Thinking…", timestamp="")
        self.streaming_row = row
        self.streaming_body_label = row.body_label
        self.streaming_details_expander = None
        self.streaming_details_box = None
        self.streaming_details_scroller = None
        self.streaming_detail_sections = {}
        self.message_list.append(row)
        self._scroll_messages_to_bottom()

    def _request_stop(self) -> None:
        if not self.current_running:
            return
        self._last_task_event_at = time.monotonic()
        self.bridge.request_stop()

    def _abort_current_task(self, reason: str) -> None:
        self._log_debug(f"abort_current_task reason={reason}")
        self._finalize_stream(reason, outcome="failed")
        self.current_running = False
        self._sync_controls()

    def _finalize_stream(
        self,
        response_text: str = "",
        usage: dict | None = None,
        task_id: str | None = None,
        outcome: str = "",
    ) -> None:
        session_id = self.active_request_session_id
        if not session_id:
            self._log_debug("finalize skipped: no active_request_session_id")
            return
        response_raw = str(response_text or "").strip()
        buffer_raw = str(self.current_stream_buffer or "").strip()
        if response_raw == "__ARIA_STOPPED__" or buffer_raw == "__ARIA_STOPPED__":
            payload = AssistantPayload(answer="Request stopped.")
            final_text = payload.answer
        else:
            payload = _merge_final_payload(buffer_raw, response_raw)
            final_text = payload.answer.strip() or response_raw or buffer_raw
        self._log_debug(
            f"finalize session={session_id} text_len={len(final_text)} current_running={self.current_running} details={payload.has_details}"
        )
        remote_task_id = str(self._active_remote_task_id or "").strip()
        remote_status = "completed"
        remote_event_kind = "result"
        remote_error = ""
        if outcome == "stopped" or response_raw == "__ARIA_STOPPED__" or buffer_raw == "__ARIA_STOPPED__":
            remote_status = "stopped"
            remote_event_kind = "control"
        elif outcome == "failed":
            remote_status = "failed"
            remote_event_kind = "error"
            remote_error = final_text
        memory_text = final_text
        memory_notice = ""
        if remote_status == "completed" and final_text and self._active_goal_text:
            try:
                transcript = self.history.get_messages(session_id, limit=self.state.max_transcript_messages)
                memory_result = self.service.decide_long_term_memory_action(
                    self._active_goal_text,
                    final_text,
                    transcript_messages=transcript,
                )
                memory_notice = str(memory_result.get("user_notice") or "").strip()
            except Exception:
                memory_notice = "Long-term memory not updated."
        if memory_notice:
            final_text = _append_memory_notice(memory_text, memory_notice)
            payload = AssistantPayload(
                answer=final_text,
                thoughts=payload.thoughts,
                actions=payload.actions,
                progress=payload.progress,
                steps=payload.steps,
                logs=payload.logs,
            )
        if final_text or payload.has_details:
            self._save_message(session_id, "assistant", _serialize_assistant_payload(payload))
        if usage and task_id:
            self._report_usage_async(task_id, usage)
        if remote_task_id:
            self._sync_remote_task_async(
                status=remote_status,
                latest_summary=memory_text or response_raw or "Task finished.",
                latest_result=memory_text if remote_status == "completed" else "",
                error=remote_error,
                spent_usd=float(usage.get("cost_usd") or 0.0) if isinstance(usage, dict) else None,
                usage=usage if isinstance(usage, dict) else None,
                event_text=memory_text or response_raw or "Task finished.",
                event_kind=remote_event_kind,
            )
        final_snapshot_status = remote_status
        if not remote_task_id and outcome == "failed":
            final_snapshot_status = "failed"
        elif not remote_task_id and (outcome == "stopped" or response_raw == "__ARIA_STOPPED__" or buffer_raw == "__ARIA_STOPPED__"):
            final_snapshot_status = "stopped"
        elif not remote_task_id:
            final_snapshot_status = "completed"
        self._push_voice_snapshot(
            status=final_snapshot_status,
            session_id=session_id,
            task_id=remote_task_id or str(task_id or session_id or "").strip(),
            goal=self._active_goal_text,
            latest_summary=memory_text or response_raw or "Task finished.",
            latest_result=memory_text if final_snapshot_status == "completed" else "",
            latest_action=payload.actions[-1] if payload.actions else "",
            latest_progress=payload.progress[-1] if payload.progress else "",
            error=remote_error or (memory_text if final_snapshot_status == "failed" else ""),
            paused=False,
            supports_followups=False,
        )
        self.current_stream_buffer = ""
        self.active_request_session_id = None
        self.current_running = False
        self._task_started_at = 0.0
        self._last_task_event_at = 0.0
        self._awaiting_remote_accept = False
        self._active_remote_task_id = None
        self._active_remote_budget_usd = 0.0
        self._active_remote_paused = False
        self._active_goal_text = ""
        self._last_remote_screenshot_sync_at = 0.0
        self._remote_screenshot_sync_inflight = False
        self._seen_remote_message_ids = set()
        self.streaming_details_expander = None
        self.streaming_details_box = None
        self.streaming_details_scroller = None
        self.streaming_detail_sections = {}
        self.refresh_state(self.service.read())

    def _handle_stream_chunk(self, session_id: str, chunk: str, usage: dict | None = None) -> None:
        if session_id != self.active_request_session_id:
            return
        normalized_lines: list[str] = []
        for line in chunk.splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith("[LOG]"):
                prefix_ws = line[: len(line) - len(stripped)]
                stripped = stripped[len("[LOG]"):].lstrip()
                if not stripped:
                    continue
                line = prefix_ws + stripped
            normalized_lines.append(line)
        normalized_chunk = "".join(normalized_lines)
        if not normalized_chunk:
            return
        self.current_stream_buffer += normalized_chunk
        self._last_task_event_at = time.monotonic()
        if self._active_remote_task_id:
            self._sync_remote_screenshot_async()
            summary = self._remote_progress_summary(normalized_chunk)
            if summary:
                self._sync_remote_task_async(
                    status="running",
                    latest_summary=summary,
                    spent_usd=float(usage.get("cost_usd") or 0.0) if isinstance(usage, dict) else None,
                    usage=usage if isinstance(usage, dict) else None,
                    event_text=summary,
                    event_kind="progress",
                    throttle_seconds=3.0,
                )
        if self.selected_session_id == session_id:
            payload = _parse_assistant_payload(self.current_stream_buffer)
            visible_text = _sanitize_terminal_result_text(payload.answer) or ("Working…" if payload.has_details else "Thinking…")
            self._append_streaming_placeholder()
            if self.streaming_body_label is not None:
                self.streaming_body_label.set_label(visible_text)
            self._update_live_details_expander(payload)
            self._scroll_messages_to_bottom()
        self._publish_voice_snapshot()

    def _poll_backend_queue(self) -> bool:
        try:
            now = time.monotonic()
            if (
                self.current_running
                and self._awaiting_remote_accept
                and self.active_request_session_id
                and self._task_started_at > 0
                and (now - self._task_started_at) >= 15.0
            ):
                self._log_debug("remote accept timeout after 15s without task_state running=True")
                self._abort_current_task("Connection issue: the remote task was not accepted by the runtime. Retry.")
                return True
            if now - self._last_disk_refresh >= 1.0:
                self._last_disk_refresh = now
                stamp = self.service.state_stamp()
                if stamp != self._last_state_stamp:
                    self._last_state_stamp = stamp
                    self.refresh_state(self.service.read())
                if not self._uses_remote_gateway() and not self.backend_connected and self.state.api_key_ready:
                    self._ensure_backend_ready()
            while True:
                try:
                    event = self.bridge.ui_queue.get_nowait()
                except Exception:
                    break
                etype = event.get("type")
                if etype == "status":
                    text = str(event.get("text") or "")
                    if text == "connected":
                        self.backend_connected = True
                    elif text.startswith("connecting ") or "reconnect:" in text:
                        self.backend_connected = False
                        if "reconnect:" in text and self.current_running and self.active_request_session_id:
                            self._abort_current_task(
                                "Connection issue: the runtime disconnected. Retry after it reconnects."
                            )
                    self._sync_controls()
                    self._render_overlay()
                    self._log_debug(f"event {etype}: {event}")
                elif etype == "system":
                    self._log_debug(f"event {etype}: {event}")
                    text = str(event.get("text") or event.get("message") or "").strip()
                    if text == "vm_link_connected":
                        self.refresh_state(self.service.read())
                        self._append_system_notice("VM linked to your Aria account.")
                    elif text.startswith("vm_link_failed:"):
                        reason = text.split(":", 1)[1].strip() or "VM link failed."
                        self._append_system_notice(f"VM link failed: {reason}")
                elif etype == "remote_task_start":
                    task = event.get("task") if isinstance(event.get("task"), dict) else None
                    if task:
                        incoming_task_id = str(task.get("id") or "").strip()
                        if self.current_running:
                            if incoming_task_id and incoming_task_id == str(self._active_remote_task_id or "").strip():
                                continue
                            self._pending_remote_task = task
                            self._log_debug(f"queued remote_task_start task_id={incoming_task_id or '-'} while busy")
                            continue
                        self._log_debug(f"event remote_task_start task_id={incoming_task_id or '-'}")
                        self.launch_remote_task(task)
                elif etype == "remote_task_stop":
                    self._log_debug(f"event remote_task_stop task_id={str(event.get('task_id') or '-')}")
                    self.stop_active_remote_task()
                elif etype == "remote_task_pause":
                    self._log_debug(f"event remote_task_pause task_id={str(event.get('task_id') or '-')}")
                    self.pause_active_remote_task()
                elif etype == "remote_task_resume":
                    self._log_debug(f"event remote_task_resume task_id={str(event.get('task_id') or '-')}")
                    self.resume_active_remote_task()
                elif etype == "remote_task_messages":
                    self._log_debug(f"event remote_task_messages task_id={str(event.get('task_id') or '-')}")
                    self.handle_remote_task_messages(
                        str(event.get("task_id") or ""),
                        [item for item in list(event.get("messages") or []) if isinstance(item, dict)],
                    )
                elif etype == "task_state":
                    running = bool(event.get("running"))
                    self._log_debug(f"event task_state running={running} active={self.active_request_session_id}")
                    self._last_task_event_at = time.monotonic()
                    if running:
                        self._awaiting_remote_accept = False
                    self.current_running = running
                    if not running:
                        self._awaiting_remote_accept = False
                        self._active_remote_paused = False
                    if running and self._active_remote_task_id:
                        self._sync_remote_task_async(
                            status="running",
                            latest_summary="Remote task is now running on the device.",
                            event_text="Remote task entered running state on device.",
                            event_kind="status",
                        )
                        self._sync_remote_screenshot_async(minimum_interval=0.0)
                    if not running and self.active_request_session_id is not None:
                        self._finalize_stream()
                    if not running and self._pending_remote_task and not self.current_running:
                        pending_task = self._pending_remote_task
                        self._pending_remote_task = None
                        self.launch_remote_task(pending_task)
                    self._publish_voice_snapshot()
                    self._sync_controls()
                elif etype == "stream":
                    self._handle_stream_chunk(
                        str(event.get("session_id") or ""),
                        str(event.get("chunk") or ""),
                        usage=event.get("usage") if isinstance(event.get("usage"), dict) else None,
                    )
                elif etype == "computer_action":
                    if self.current_running and self._active_remote_task_id:
                        self._sync_remote_screenshot_async(minimum_interval=0.0)
                elif etype == "computer_screenshot":
                    if self.current_running and self._active_remote_task_id:
                        image_base64 = str(event.get("image_base64") or "")
                        mime = str(event.get("mime") or "image/png")
                        if image_base64:
                            self._sync_remote_screenshot_data_async(image_base64, mime)
                        else:
                            self._sync_remote_screenshot_async(minimum_interval=0.0)
                elif etype == "response":
                    self._log_debug(f"event response len={len(str(event.get('text') or ''))}")
                    self._last_task_event_at = time.monotonic()
                    self._finalize_stream(
                        str(event.get("text") or ""),
                        usage=event.get("usage"),
                        task_id=str(event.get("task_id") or ""),
                        outcome=str(event.get("outcome") or ""),
                    )
                    self._sync_controls()
                elif etype == "session_title":
                    session_id = str(event.get("session_id") or "")
                    title = str(event.get("title") or "").strip()
                    if session_id and title:
                        self.history.rename_session(session_id, title)
                        self.refresh_state(self.service.read())
            return True
        except Exception as exc:
            self.current_running = False
            self._sync_controls()
            self._log_debug(f"poll_backend_queue exception: {exc}\n{traceback.format_exc()}")
            return True

    def _show_api_key_dialog(self) -> None:
        if self.key_window is not None:
            self.key_window.present()
            return

        def on_success():
            self.key_window = None
            self.refresh_state(self.service.read())

        self.key_window = show_key_dialog(self, self.service, on_success)
        self.key_window.connect("close-request", lambda *_args: self._clear_key_window())
        self.key_window.connect("destroy", lambda *_args: self._clear_key_window())

    def _show_model_dialog(self) -> None:
        show_model_dialog(self, self.service, lambda: self.refresh_state(self.service.read()))

    def _show_access_dialog(self) -> None:
        self._show_api_key_dialog()

    def _open_account(self) -> None:
        self._show_api_key_dialog()

    def _clear_key_window(self, *_args):
        self.key_window = None
        return False

    def _clear_access_window(self, *_args):
        self._log_debug("_clear_access_window")
        self.access_window = None
        return False


    def _logout_account(self) -> None:
        if self.current_running:
            return
        self._stop_voice_mode()
        self.service.logout_account(open_browser=False)
        self.pending_image = None
        self.selected_session_id = None
        self.current_stream_buffer = ""
        self.streaming_row = None
        self.streaming_body_label = None
        self.streaming_details_expander = None
        self.streaming_details_box = None
        self.streaming_details_scroller = None
        self.streaming_detail_sections = {}
        self.active_request_session_id = None
        self.backend_connected = False
        self._push_voice_snapshot(
            status="idle",
            session_id="",
            task_id="",
            goal="",
            latest_summary="",
            latest_result="",
            latest_action="",
            latest_progress="",
            error="",
            paused=False,
            supports_followups=False,
        )
        self.refresh_state(self.service.read())

    def _refresh_attachment_ui(self) -> None:
        if self.pending_image is None:
            self.attachment_row.set_visible(False)
            self.attachment_label.set_label("")
            return
        name = str(self.pending_image.get("filename") or "attached-image")
        mime_type = str(self.pending_image.get("mime_type") or "image")
        self.attachment_label.set_label(f"Attached image: {name} · {mime_type}")
        self.attachment_row.set_visible(True)

    def _clear_pending_image(self) -> None:
        self.pending_image = None
        self._refresh_attachment_ui()
        self._sync_controls()

    def _load_image_payload(self, path: Path) -> dict[str, str]:
        suffix = path.suffix.lower()
        if suffix not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("Only image files are supported.")
        if not path.exists() or not path.is_file():
            raise ValueError("Selected image does not exist.")
        if path.stat().st_size <= 0:
            raise ValueError("Selected image is empty.")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Selected image is too large.")
        mime_type = IMAGE_MIME_MAP.get(suffix) or mimetypes.guess_type(str(path))[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "filename": path.name,
            "mime": mime_type,
            "mime_type": mime_type,
            "data": encoded,
        }

    def _choose_image(self) -> None:
        if self.current_running:
            return
        dialog = Gtk.FileChooserNative(
            title="Attach image",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Attach",
            cancel_label="Cancel",
        )
        filter_images = Gtk.FileFilter()
        filter_images.set_name("Images")
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp"):
            filter_images.add_pattern(pattern)
        dialog.add_filter(filter_images)

        def on_response(_dialog, response):
            try:
                if response != Gtk.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if not file_obj:
                    return
                path = Path(file_obj.get_path())
                self.pending_image = self._load_image_payload(path)
                self._refresh_attachment_ui()
                self._sync_controls()
            except Exception as exc:
                self._log_debug(f"image selection error: {exc}\n{traceback.format_exc()}")
            finally:
                dialog.destroy()

        dialog.connect("response", on_response)
        dialog.show()
