from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets


L0 = "u0"
L1 = "u1"
L2 = "u2"
L3 = "u3"
L4 = "u4"
L5 = "u5"
L6 = "u6"
L7 = "u7"
L8 = "u8"
L9 = "u9"

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_SESSION_MESSAGES = 16
MAX_CHUNK_CHARS = 180
SYSTEM_PROMPT = (
    "You are AriaOS, a local AI workspace running inside a Linux VM. "
    "Be practical, concise, and action-oriented. "
    "If the user asks you to do something that requires missing capabilities in this local build, "
    "say so plainly and offer the closest useful next step."
)
SECRETS_FILE = Path.home() / ".config" / "ariaos" / "local_agent_secrets.json"


def _is_local_type(value: Any, compact: str, legacy: str) -> bool:
    return str(value or "").strip() in {compact, legacy}


def _normalize_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if normalized in {"gpt-5.4", "gpt-5.4-mini"}:
        return normalized
    return DEFAULT_MODEL


def _load_runtime() -> dict[str, str]:
    try:
        payload = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "api_key": str(os.environ.get("OPENAI_API_KEY") or payload.get("openai_api_key") or "").strip(),
        "base_url": str(os.environ.get("OPENAI_BASE_URL") or payload.get("openai_base_url") or DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL,
        "model": _normalize_model(os.environ.get("ARIAOS_MODEL") or payload.get("openai_model") or DEFAULT_MODEL),
    }


def _heuristic_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'`")
    if not cleaned:
        return "New Chat"
    words = [word for word in cleaned.split(" ") if word]
    return (" ".join(words[:5])[:48].strip() or "New Chat")


def _extract_text(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = (message or {}).get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
            return "\n".join(chunks).strip()
    return ""


def _normalize_usage(raw: dict[str, Any], *, started_at: float, success: bool) -> dict[str, Any]:
    usage = raw.get("usage") if isinstance(raw, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached_input_tokens = int(usage.get("cached_prompt_tokens") or usage.get("cached_input_tokens") or 0)
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cost_usd": 0.0,
        "model_calls": 1 if success else 0,
        "success": success,
        "steps": 1 if success else 0,
        "elapsed_seconds": max(0.0, time.monotonic() - started_at),
        "models": {},
    }


def _openai_chat(runtime: dict[str, str], messages: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = str(runtime.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("OpenAI API key is missing.")
    endpoint = f"{str(runtime.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}/chat/completions"
    payload = {
        "model": _normalize_model(runtime.get("model")),
        "messages": messages,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"OpenAI HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        raw = json.loads(body or "{}")
    except Exception as exc:
        raise RuntimeError(f"Invalid OpenAI response: {exc}") from exc
    text = _extract_text(raw)
    if not text:
        raise RuntimeError("OpenAI returned an empty answer.")
    return raw


def _chunk_text(text: str) -> list[str]:
    cleaned = str(text or "")
    if not cleaned:
        return []
    parts: list[str] = []
    buffer = ""
    for paragraph in cleaned.splitlines(keepends=True):
        if not paragraph:
            continue
        if len(buffer) + len(paragraph) > MAX_CHUNK_CHARS and buffer:
            parts.append(buffer)
            buffer = paragraph
        else:
            buffer += paragraph
    if buffer:
        parts.append(buffer)
    if not parts:
        return [cleaned]
    return parts


@dataclass
class SessionState:
    messages: list[dict[str, str]] = field(default_factory=list)

    def reset(self) -> None:
        self.messages = []

    def append_exchange(self, prompt: str, response: str) -> None:
        self.messages.append({"role": "user", "content": str(prompt or "").strip()})
        self.messages.append({"role": "assistant", "content": str(response or "").strip()})
        self.messages = self.messages[-MAX_SESSION_MESSAGES:]


@dataclass
class ActiveRun:
    websocket: Any
    stop_event: threading.Event
    task: asyncio.Task[Any]


class AriaLocalServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.sessions: dict[str, SessionState] = {}
        self.active_runs: dict[str, ActiveRun] = {}
        self.active_lock = threading.Lock()

    def _session(self, session_id: str) -> SessionState:
        key = str(session_id or "").strip() or "default"
        if key not in self.sessions:
            self.sessions[key] = SessionState()
        return self.sessions[key]

    def _complete_goal(
        self,
        *,
        session_id: str,
        prompt: str,
        image: dict[str, Any] | None,
        stop_event: threading.Event,
        started_at: float,
    ) -> tuple[str, dict[str, Any]]:
        runtime = _load_runtime()
        if not runtime["api_key"]:
            usage = _normalize_usage({}, started_at=started_at, success=False)
            usage["elapsed_seconds"] = max(0.0, time.monotonic() - started_at)
            return (
                "OpenAI API key is missing. Open Aria Home and connect your key to start the local backend.",
                usage,
            )

        session = self._session(session_id)
        user_prompt = str(prompt or "").strip()
        if image:
            user_prompt += "\n\n[An image was attached to this prompt. Image reasoning is not enabled in this local backend yet.]"
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(session.messages[-MAX_SESSION_MESSAGES:])
        messages.append({"role": "user", "content": user_prompt})

        raw = _openai_chat(runtime, messages)
        if stop_event.is_set():
            return "", _normalize_usage(raw, started_at=started_at, success=False)

        text = _extract_text(raw)
        session.append_exchange(user_prompt, text)
        return text, _normalize_usage(raw, started_at=started_at, success=True)

    async def _send_json(self, websocket, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _run_goal(
        self,
        websocket,
        request_id: str,
        session_id: str,
        prompt: str,
        image: dict[str, Any] | None,
        stop_event: threading.Event,
    ) -> None:
        started_at = time.monotonic()
        try:
            response_text, usage = await asyncio.to_thread(
                self._complete_goal,
                session_id=session_id,
                prompt=prompt,
                image=image,
                stop_event=stop_event,
                started_at=started_at,
            )
            if stop_event.is_set():
                return
            for chunk in _chunk_text(response_text):
                if stop_event.is_set():
                    return
                await self._send_json(
                    websocket,
                    {
                        "type": L5,
                        "requestId": request_id,
                        "chunk": chunk,
                    },
                )
            if stop_event.is_set():
                return
            await self._send_json(
                websocket,
                {
                    "type": L6,
                    "requestId": request_id,
                    "response": response_text,
                    "usage": usage,
                },
            )
        except Exception as exc:
            await self._send_json(
                websocket,
                {
                    "type": L6,
                    "requestId": request_id,
                    "response": f"Error: {exc}",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_input_tokens": 0,
                        "cost_usd": 0.0,
                        "model_calls": 0,
                        "success": False,
                        "steps": 0,
                        "elapsed_seconds": max(0.0, time.monotonic() - started_at),
                        "models": {},
                    },
                },
            )
        finally:
            with self.active_lock:
                self.active_runs.pop(request_id, None)

    def _mark_stop(self, request_id: str | None, websocket) -> bool:
        found = False
        with self.active_lock:
            for active_request_id, run in list(self.active_runs.items()):
                if request_id and active_request_id != request_id:
                    continue
                if run.websocket is not websocket:
                    continue
                run.stop_event.set()
                found = True
        return found

    async def handle_client(self, websocket) -> None:
        try:
            async for raw in websocket:
                data = json.loads(raw)
                msg_type = data.get("type")
                request_id = str(data.get("requestId") or "").strip()
                session_id = str(data.get("sessionId") or "").strip() or "default"

                if _is_local_type(msg_type, L0, "new_session"):
                    self._mark_stop(None, websocket)
                    self._session(session_id).reset()
                    await self._send_json(
                        websocket,
                        {
                            "type": L1,
                            "requestId": request_id,
                            "data": {"ok": True, "isolated": True},
                        },
                    )
                    continue

                if _is_local_type(msg_type, L2, "generate_title"):
                    title = _heuristic_title(str(data.get("text") or ""))
                    await self._send_json(
                        websocket,
                        {
                            "type": L3,
                            "requestId": request_id,
                            "data": {"sessionId": session_id, "title": title},
                        },
                    )
                    continue

                if _is_local_type(msg_type, L7, "stop"):
                    target_request = str(data.get("targetRequestId") or "").strip()
                    stopped = self._mark_stop(target_request, websocket)
                    await self._send_json(
                        websocket,
                        {
                            "type": L8,
                            "requestId": request_id,
                            "data": {
                                "stopped": stopped,
                                "targetRequestId": target_request,
                                "memoryCleared": False,
                            },
                        },
                    )
                    continue

                if _is_local_type(msg_type, L4, "command"):
                    stop_event = threading.Event()
                    task = asyncio.create_task(
                        self._run_goal(
                            websocket,
                            request_id=request_id,
                            session_id=session_id,
                            prompt=str(data.get("command") or ""),
                            image=data.get("image") if isinstance(data.get("image"), dict) else None,
                            stop_event=stop_event,
                        )
                    )
                    with self.active_lock:
                        self.active_runs[request_id] = ActiveRun(
                            websocket=websocket,
                            stop_event=stop_event,
                            task=task,
                        )
                    continue
        finally:
            self._mark_stop(None, websocket)

    async def start(self) -> None:
        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            max_size=8_000_000,
            ping_interval=20,
            ping_timeout=30,
        ):
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(AriaLocalServer().start())
