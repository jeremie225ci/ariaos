"""Shared OpenAI helpers for the readable local AriaOS backend."""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.prompts import CHAT_SYSTEM_PROMPT

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
FALLBACK_TTS_MODEL = "tts-1"
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
FALLBACK_TRANSCRIBE_MODEL = "whisper-1"
MAX_SESSION_MESSAGES = 16
MAX_CHUNK_CHARS = 180
VOICE_SAMPLE_RATE = 24_000
PRIMARY_SECRETS_FILE = Path.home() / ".config" / "ariaos" / "user_secrets.json"
LEGACY_SECRETS_FILE = Path.home() / ".config" / "ariaos" / "local_agent_secrets.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _post_json(
    endpoint: str,
    *,
    payload: dict[str, Any],
    runtime: dict[str, str],
    timeout: float = 90.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {str(runtime.get('api_key') or '').strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"OpenAI HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        return json.loads(body or "{}")
    except Exception as exc:
        raise RuntimeError(f"Invalid OpenAI response: {exc}") from exc


def _multipart_body(
    *,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"ariaos-{uuid.uuid4().hex}"
    buffer = io.BytesIO()
    for name, value in fields.items():
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        buffer.write(str(value).encode("utf-8"))
        buffer.write(b"\r\n")
    buffer.write(f"--{boundary}\r\n".encode("utf-8"))
    buffer.write(
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    buffer.write(file_bytes)
    buffer.write(b"\r\n")
    buffer.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), boundary


def _post_multipart(
    endpoint: str,
    *,
    fields: dict[str, str],
    file_name: str,
    content_type: str,
    file_bytes: bytes,
    runtime: dict[str, str],
    timeout: float = 90.0,
) -> dict[str, Any]:
    body, boundary = _multipart_body(
        fields=fields,
        file_field="file",
        file_name=file_name,
        content_type=content_type,
        file_bytes=file_bytes,
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {str(runtime.get('api_key') or '').strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"OpenAI HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        return json.loads(payload or "{}")
    except Exception as exc:
        raise RuntimeError(f"Invalid OpenAI response: {exc}") from exc


def _post_binary(
    endpoint: str,
    *,
    payload: dict[str, Any],
    runtime: dict[str, str],
    timeout: float = 90.0,
) -> bytes:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/octet-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {str(runtime.get('api_key') or '').strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"OpenAI HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def _response_text_candidates(raw: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    choices = raw.get("choices")
    if isinstance(choices, list):
        for item in choices:
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    candidates.append(content.strip())
                elif isinstance(content, list):
                    for chunk in content:
                        if isinstance(chunk, dict):
                            text = chunk.get("text")
                            if isinstance(text, str) and text.strip():
                                candidates.append(text.strip())
    for key in ("text", "output_text", "response"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    return candidates


def normalize_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if normalized in {"gpt-5.4", "gpt-5.4-mini"}:
        return normalized
    return DEFAULT_MODEL


def load_runtime() -> dict[str, str]:
    payload: dict[str, Any] = {}
    for path in (PRIMARY_SECRETS_FILE, LEGACY_SECRETS_FILE):
        payload.update(_read_json(path))
    # `user_secrets.json` is the public local-first source of truth now, but we
    # still read the legacy file so older VM builds continue to boot cleanly.
    return {
        "api_key": str(os.environ.get("OPENAI_API_KEY") or payload.get("openai_api_key") or "").strip(),
        "base_url": str(os.environ.get("OPENAI_BASE_URL") or payload.get("openai_base_url") or DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL,
        "model": normalize_model(os.environ.get("ARIAOS_MODEL") or payload.get("openai_model") or DEFAULT_MODEL),
    }


def ensure_runtime_key(runtime: dict[str, str]) -> None:
    if str(runtime.get("api_key") or "").strip():
        return
    raise RuntimeError("OpenAI API key is missing.")


def extract_text(raw: dict[str, Any]) -> str:
    for candidate in _response_text_candidates(raw):
        if candidate:
            return candidate
    return ""


def normalize_usage(raw: dict[str, Any], *, started_at: float, success: bool) -> dict[str, Any]:
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


def chunk_text(text: str) -> list[str]:
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
    return parts or [cleaned]


def heuristic_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'`")
    if not cleaned:
        return "New Chat"
    words = [word for word in cleaned.split(" ") if word]
    return (" ".join(words[:5])[:48].strip() or "New Chat")


def openai_chat(runtime: dict[str, str], messages: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_runtime_key(runtime)
    endpoint = f"{str(runtime.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}/chat/completions"
    payload = {
        "model": normalize_model(runtime.get("model")),
        "messages": messages,
        "temperature": 0.2,
    }
    raw = _post_json(endpoint, payload=payload, runtime=runtime)
    text = extract_text(raw)
    if not text:
        raise RuntimeError("OpenAI returned an empty answer.")
    return raw


def pcm_to_wav_bytes(pcm_bytes: bytes, *, sample_rate: int = VOICE_SAMPLE_RATE) -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()


def transcribe_pcm(runtime: dict[str, str], pcm_bytes: bytes, *, language: str = "") -> str:
    ensure_runtime_key(runtime)
    wav_bytes = pcm_to_wav_bytes(pcm_bytes)
    endpoint = f"{str(runtime.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}/audio/transcriptions"
    fields = {"language": str(language or "").strip()} if str(language or "").strip() else {}

    # The modern transcription model is preferred, but we keep a whisper
    # fallback so the local voice mode still works on older OpenAI accounts.
    for model_name in (DEFAULT_TRANSCRIBE_MODEL, FALLBACK_TRANSCRIBE_MODEL):
        try:
            raw = _post_multipart(
                endpoint,
                fields={**fields, "model": model_name},
                file_name="aria-voice.wav",
                content_type="audio/wav",
                file_bytes=wav_bytes,
                runtime=runtime,
            )
        except RuntimeError:
            if model_name != FALLBACK_TRANSCRIBE_MODEL:
                continue
            raise
        text = str(raw.get("text") or "").strip()
        if text:
            return text
    raise RuntimeError("OpenAI returned an empty transcription.")


def synthesize_speech(runtime: dict[str, str], text: str, *, voice_name: str) -> bytes:
    ensure_runtime_key(runtime)
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return b""
    endpoint = f"{str(runtime.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}/audio/speech"

    # The local voice server asks for raw PCM so the GTK client can stream the
    # bytes directly into ffplay without an additional decode step.
    for model_name in (DEFAULT_TTS_MODEL, FALLBACK_TTS_MODEL):
        try:
            audio_bytes = _post_binary(
                endpoint,
                payload={
                    "model": model_name,
                    "voice": str(voice_name or "marin").strip().lower() or "marin",
                    "input": cleaned[:1200],
                    "response_format": "pcm",
                },
                runtime=runtime,
            )
        except RuntimeError:
            if model_name != FALLBACK_TTS_MODEL:
                continue
            raise
        if audio_bytes:
            return audio_bytes
    raise RuntimeError("OpenAI returned empty speech audio.")


@dataclass
class SessionMemory:
    messages: list[dict[str, str]] = field(default_factory=list)

    def reset(self) -> None:
        self.messages = []

    # The shell only needs a short rolling memory window for the local build,
    # so we keep the last exchanges instead of persisting a larger graph here.
    def append_exchange(self, prompt: str, response: str) -> None:
        self.messages.append({"role": "user", "content": str(prompt or "").strip()})
        self.messages.append({"role": "assistant", "content": str(response or "").strip()})
        self.messages = self.messages[-MAX_SESSION_MESSAGES:]
