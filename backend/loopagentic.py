"""Bridge the websocket backend to the full local AriaOS agentic runtime."""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agentic_runtime import AgenticLoop
from backend.brain import heuristic_title, load_runtime, normalize_usage
from backend.brain_config import SESSION_LOG_DIR


@dataclass
class LocalTaskResult:
    response_text: str
    usage: dict[str, Any]


class LocalTaskLoop:
    def __init__(self) -> None:
        self.display = str(os.environ.get("DISPLAY") or ":0").strip() or ":0"

    def _session_log_path(self, session_id: str) -> Path:
        safe_session_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "").strip())[:120]
        return Path(SESSION_LOG_DIR) / f"{safe_session_id}.json"

    def _new_brain(self, session_id: str, runtime: dict[str, str]) -> AgenticLoop:
        # The public repo now uses the same local brain family as the VM clone.
        # We instantiate per task so concurrent websocket runs do not trample
        # each other's state, while session context still persists on disk.
        brain = AgenticLoop(display=self.display)
        brain.attach_session(session_id, force_reload=True)
        brain.configure_openai_runtime(
            api_key=str(runtime.get("api_key") or "").strip(),
            base_url=str(runtime.get("base_url") or "").strip(),
            model=str(runtime.get("model") or "").strip(),
        )
        brain.configure_scheduler_context([])
        brain.configure_task_budget(0.0)
        return brain

    def reset_session(self, session_id: str) -> None:
        session_path = self._session_log_path(session_id)
        try:
            session_path.unlink()
        except FileNotFoundError:
            return

    def generate_title(self, text: str) -> str:
        return heuristic_title(text)

    def complete(
        self,
        *,
        session_id: str,
        prompt: str,
        image: dict[str, Any] | None,
        stop_requested: Callable[[], bool],
        started_at: float,
    ) -> LocalTaskResult:
        runtime = load_runtime()
        if not runtime["api_key"]:
            usage = normalize_usage({}, started_at=started_at, success=False)
            usage["elapsed_seconds"] = max(0.0, time.monotonic() - started_at)
            return LocalTaskResult(
                response_text=(
                    "OpenAI API key is missing. Open Aria Home and connect your key "
                    "to start the local backend."
                ),
                usage=usage,
            )

        brain = self._new_brain(session_id, runtime)
        if isinstance(image, dict) and image:
            # The VM brain consumes the pending image from this attribute before
            # starting the direct-response probe or the agentic loop.
            brain._pending_user_image = dict(image)

        stop_event = threading.Event()

        def _watch_stop_signal() -> None:
            while not stop_event.is_set():
                if stop_requested():
                    stop_event.set()
                    return
                time.sleep(0.10)

        watcher = threading.Thread(target=_watch_stop_signal, daemon=True)
        watcher.start()

        goal = str(prompt or "").strip()
        result: dict[str, Any] = {}
        generator = brain.execute_goal(goal, stop_event=stop_event)

        try:
            while True:
                try:
                    next(generator)
                except StopIteration as exc:
                    result = dict(exc.value or {})
                    break
        finally:
            stop_event.set()
            watcher.join(timeout=0.25)

        usage = dict(brain.last_task_usage_summary or {})
        if not usage:
            usage = normalize_usage({}, started_at=started_at, success=bool(result.get("success")))
        usage["elapsed_seconds"] = max(0.0, time.monotonic() - started_at)
        usage["success"] = bool(result.get("success"))
        usage["steps"] = int(result.get("steps") or usage.get("steps") or 0)

        response_text = str(
            result.get("response_text")
            or result.get("summary")
            or result.get("reason")
            or ""
        ).strip()

        if result.get("stopped") and stop_requested():
            response_text = ""

        return LocalTaskResult(
            response_text=response_text,
            usage=usage,
        )
