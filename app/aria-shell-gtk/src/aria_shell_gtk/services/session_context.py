from __future__ import annotations

import re
from typing import Any, Iterable


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _assistant_memory_text(raw_text: str, *, parse_assistant_payload) -> str:
    payload = parse_assistant_payload(raw_text)
    if payload.answer:
        return _normalize_memory_text(payload.answer)[:500]
    combined = " ".join(
        list(payload.actions)[:1] + list(payload.progress)[:2] + list(payload.thoughts)[:1] + list(payload.steps)[:1]
    )
    return _normalize_memory_text(combined)[:500]


def prompt_memory_line(role: str, raw_text: str, *, parse_assistant_payload) -> str:
    if role == "user":
        text = _normalize_memory_text(raw_text)
        if text.startswith("You>"):
            text = text[4:].strip()
        return f"USER: {text}" if text else ""
    text = _assistant_memory_text(raw_text, parse_assistant_payload=parse_assistant_payload)
    return f"ARIA: {text}" if text else ""


def build_isolated_prompt(
    *,
    session_id: str,
    user_prompt: str,
    transcript_messages: Iterable[Any],
    parse_assistant_payload,
) -> str:
    lines: list[str] = []
    for message in transcript_messages:
        role = str(getattr(message, "role", "") or "")
        content = str(getattr(message, "content", "") or "")
        line = prompt_memory_line(role, content, parse_assistant_payload=parse_assistant_payload)
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    context = "\n".join(lines).strip()
    return (
        "GUIDANCE:\n"
        "- Stay inside this session.\n"
        "- Use only the history below for context.\n\n"
        f"SID: {session_id}\n"
        f"HISTORY:\n{context if context else '(empty)'}\n\n"
        f"INPUT:\n{user_prompt}\n"
    )
