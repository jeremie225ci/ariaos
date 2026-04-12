"""Readable local task loop used by the websocket backend."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from backend.brain import (
    CHAT_SYSTEM_PROMPT,
    SessionMemory,
    extract_text,
    heuristic_title,
    load_runtime,
    normalize_usage,
    openai_chat,
)


@dataclass
class LocalTaskResult:
    response_text: str
    usage: dict[str, Any]


class LocalTaskLoop:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionMemory] = {}

    def session(self, session_id: str) -> SessionMemory:
        key = str(session_id or "").strip() or "default"
        if key not in self.sessions:
            self.sessions[key] = SessionMemory()
        return self.sessions[key]

    def reset_session(self, session_id: str) -> None:
        self.session(session_id).reset()

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
        # This is the public replacement for the older hidden kernel/engine
        # naming inside the VM bundle: one local request in, one model call out.
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

        # The public local build keeps a short rolling chat memory so every
        # request stays readable and self-contained in the repository.
        session = self.session(session_id)
        user_prompt = str(prompt or "").strip()
        if image:
            user_prompt += (
                "\n\n[An image was attached to this prompt. "
                "Image reasoning is not enabled in this local backend yet.]"
            )

        messages: list[dict[str, Any]] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        messages.extend(session.messages)
        messages.append({"role": "user", "content": user_prompt})

        raw = openai_chat(runtime, messages)
        if stop_requested():
            return LocalTaskResult(
                response_text="",
                usage=normalize_usage(raw, started_at=started_at, success=False),
            )

        response_text = extract_text(raw)
        session.append_exchange(user_prompt, response_text)
        return LocalTaskResult(
            response_text=response_text,
            usage=normalize_usage(raw, started_at=started_at, success=True),
        )
