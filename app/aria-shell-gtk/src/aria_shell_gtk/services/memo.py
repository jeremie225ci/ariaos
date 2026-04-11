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


def memory_line(role: str, raw_text: str, *, parse_assistant_payload) -> str:
    if role == "user":
        text = _normalize_memory_text(raw_text)
        if text.startswith("You>"):
            text = text[4:].strip()
        return f"U: {text}" if text else ""
    text = _assistant_memory_text(raw_text, parse_assistant_payload=parse_assistant_payload)
    return f"A: {text}" if text else ""


def compose_prompt(
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
        line = memory_line(role, content, parse_assistant_payload=parse_assistant_payload)
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    context = "\n".join(lines).strip()
    return (
        "N:\n"
        "- Keep scope local.\n"
        "- Use the log below as context, not as a hard limit.\n"
        "- If the user is asking you to continue working, retry, search, install, open, create, fix, send, or perform any new action, continue the task with tools as needed.\n"
        "- Only answer from the log alone when the user is explicitly asking for a recap, explanation, summary, result, or status of what already happened.\n\n"
        f"S: {session_id}\n"
        f"L:\n{context if context else '(empty)'}\n\n"
        f"Q:\n{user_prompt}\n"
    )
