from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from backend.brain import load_runtime, synthesize_speech, transcribe_pcm


VOICE_HOST = "127.0.0.1"
VOICE_PORT = 8081
VOICE_AUDIO_CHUNK_BYTES = 4_800
VOICE_MIN_UTTERANCE_BYTES = VOICE_AUDIO_CHUNK_BYTES * 5
VOICE_MAX_UTTERANCE_BYTES = VOICE_AUDIO_CHUNK_BYTES * 120
VOICE_SILENCE_CHUNKS = 8
VOICE_SPEECH_RMS_THRESHOLD = 280
VOICE_AUDIO_DELTA_BYTES = 8_192
VOICE_TTS_PREVIEW_CHARS = 520

STOP_MARKERS = (
    "stop",
    "cancel",
    "halt",
    "arrete",
    "arrête",
    "annule",
    "annuler",
)


def _pcm_rms(chunk: bytes) -> int:
    # `audioop` disappeared from newer Python builds, but the voice server only
    # needs a simple RMS estimate over 16-bit mono PCM to detect speech bursts.
    if not chunk or len(chunk) < 2:
        return 0
    size = len(chunk) // 2
    if size <= 0:
        return 0
    try:
        samples = struct.unpack("<" + ("h" * size), chunk[: size * 2])
    except struct.error:
        return 0
    energy = sum(sample * sample for sample in samples)
    if energy <= 0:
        return 0
    return int(math.sqrt(energy / size))


def _clean_text(text: str, *, limit: int = 0) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if limit > 0:
        return cleaned[:limit].strip()
    return cleaned


def _task_is_running(snapshot: dict[str, Any]) -> bool:
    status = str((snapshot or {}).get("status") or "").strip().lower()
    return status in {"running", "dispatching", "paused"}


def _command_from_transcript(transcript: str, snapshot: dict[str, Any]) -> tuple[str, str]:
    lowered = _clean_text(transcript).lower()
    if not lowered:
        return "", ""
    if any(marker in lowered for marker in STOP_MARKERS):
        return "stop_task", ""
    # The public local build keeps voice commands intentionally narrow: start a
    # new task when idle, or tell the user to stop the current one first.
    if _task_is_running(snapshot):
        return "", "Aria is already running a task. Say stop to cancel it before starting a new one."
    return "start_task", transcript


def _result_signature(snapshot: dict[str, Any]) -> str:
    status = str((snapshot or {}).get("status") or "").strip().lower()
    if status not in {"completed", "failed", "stopped"}:
        return ""
    task_id = str((snapshot or {}).get("task_id") or "").strip()
    text = _clean_text(
        str((snapshot or {}).get("latest_result") or (snapshot or {}).get("error") or (snapshot or {}).get("latest_summary") or ""),
        limit=180,
    )
    if not text:
        return ""
    return f"{task_id}:{status}:{text}"


@dataclass
class VoiceSessionState:
    voice_name: str = "marin"
    language: str = "en"
    latest_snapshot: dict[str, Any] = field(default_factory=dict)
    listening_enabled: bool = True
    in_speech: bool = False
    processing_audio: bool = False
    audio_buffer: bytearray = field(default_factory=bytearray)
    pre_roll: deque[bytes] = field(default_factory=lambda: deque(maxlen=3))
    silence_chunks: int = 0
    pending_commands: dict[str, str] = field(default_factory=dict)
    last_result_signature: str = ""
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    speak_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_capture(self) -> None:
        self.in_speech = False
        self.audio_buffer = bytearray()
        self.silence_chunks = 0
        self.pre_roll.clear()


class AriaVoiceServer:
    # This server stays local to the VM. It accepts microphone chunks from the
    # GTK client, detects utterance boundaries, sends the transcript back to the
    # client, and turns completed task results into local TTS playback.
    def __init__(self, host: str = VOICE_HOST, port: int = VOICE_PORT) -> None:
        self.host = host
        self.port = port

    async def _send_json(self, websocket, state: VoiceSessionState, payload: dict[str, Any]) -> None:
        async with state.send_lock:
            try:
                await websocket.send(json.dumps(payload, ensure_ascii=False))
            except ConnectionClosed:
                # Closing the client while Aria is still speaking is a normal
                # shutdown path during local tests and UI restarts.
                return

    async def _send_status(self, websocket, state: VoiceSessionState, status: str) -> None:
        await self._send_json(websocket, state, {"type": "status", "state": str(status or "idle").strip() or "idle"})

    async def _send_audio(self, websocket, state: VoiceSessionState, audio_bytes: bytes) -> None:
        if not audio_bytes:
            return
        for index in range(0, len(audio_bytes), VOICE_AUDIO_DELTA_BYTES):
            chunk = audio_bytes[index : index + VOICE_AUDIO_DELTA_BYTES]
            await self._send_json(
                websocket,
                state,
                {"type": "audio_delta", "audio_b64": base64.b64encode(chunk).decode("ascii")},
            )
        await self._send_json(websocket, state, {"type": "audio_done"})

    async def _announce_text(
        self,
        websocket,
        state: VoiceSessionState,
        text: str,
        *,
        next_state: str = "listening",
    ) -> None:
        preview = _clean_text(text, limit=VOICE_TTS_PREVIEW_CHARS)
        if not preview:
            await self._send_status(websocket, state, next_state)
            return

        async with state.speak_lock:
            await self._send_json(websocket, state, {"type": "assistant_text", "text": preview})
            await self._send_status(websocket, state, "speaking")
            try:
                audio_bytes = await asyncio.to_thread(
                    synthesize_speech,
                    load_runtime(),
                    preview,
                    voice_name=state.voice_name,
                )
            except Exception as exc:
                await self._send_json(websocket, state, {"type": "error", "message": f"Voice synthesis failed: {exc}"})
                await self._send_status(websocket, state, next_state)
                return
            await self._send_audio(websocket, state, audio_bytes)
            await self._send_status(websocket, state, next_state)

    def _ingest_audio_chunk(self, state: VoiceSessionState, chunk: bytes) -> bytes:
        if not chunk:
            return b""

        state.pre_roll.append(chunk)
        rms = _pcm_rms(chunk)
        is_speech = rms >= VOICE_SPEECH_RMS_THRESHOLD

        if not state.in_speech:
            if not is_speech:
                return b""
            state.in_speech = True
            state.audio_buffer = bytearray()
            for buffered in state.pre_roll:
                state.audio_buffer.extend(buffered)
            state.silence_chunks = 0
            return b""

        state.audio_buffer.extend(chunk)
        if is_speech:
            state.silence_chunks = 0
        else:
            state.silence_chunks += 1

        # A simple RMS gate is enough for the public local build: it gives the
        # client a bounded utterance without dragging a full remote VAD stack
        # back into the repository.
        if len(state.audio_buffer) >= VOICE_MAX_UTTERANCE_BYTES:
            pcm_bytes = bytes(state.audio_buffer)
            state.reset_capture()
            return pcm_bytes
        if state.silence_chunks >= VOICE_SILENCE_CHUNKS and len(state.audio_buffer) >= VOICE_MIN_UTTERANCE_BYTES:
            pcm_bytes = bytes(state.audio_buffer)
            state.reset_capture()
            return pcm_bytes
        return b""

    async def _process_transcript(self, websocket, state: VoiceSessionState, transcript: str) -> None:
        cleaned = _clean_text(transcript)
        state.processing_audio = False
        if not cleaned:
            await self._send_status(websocket, state, "listening")
            return

        await self._send_json(websocket, state, {"type": "transcript", "text": cleaned})
        command_name, payload = _command_from_transcript(cleaned, state.latest_snapshot)
        if not command_name:
            await self._announce_text(
                websocket,
                state,
                payload or "I heard you, but I cannot apply that while a task is already running.",
                next_state="thinking" if _task_is_running(state.latest_snapshot) else "listening",
            )
            return

        command_id = f"voice-{uuid.uuid4().hex[:10]}"
        state.pending_commands[command_id] = command_name
        await self._send_json(
            websocket,
            state,
            {
                "type": "command",
                "command_id": command_id,
                "command": command_name,
                "text": payload,
            },
        )

    async def _process_audio(self, websocket, state: VoiceSessionState, pcm_bytes: bytes) -> None:
        try:
            await self._send_status(websocket, state, "thinking")
            transcript = await asyncio.to_thread(
                transcribe_pcm,
                load_runtime(),
                pcm_bytes,
                language=state.language,
            )
        except Exception as exc:
            state.processing_audio = False
            await self._send_json(websocket, state, {"type": "error", "message": f"Voice transcription failed: {exc}"})
            await self._send_status(websocket, state, "listening")
            return
        await self._process_transcript(websocket, state, transcript)

    async def _handle_command_result(self, websocket, state: VoiceSessionState, data: dict[str, Any]) -> None:
        command_id = str(data.get("command_id") or "").strip()
        command_name = state.pending_commands.pop(command_id, "")
        ok = bool(data.get("ok"))
        message = _clean_text(str(data.get("message") or ""))

        if not ok:
            await self._announce_text(websocket, state, message or "The voice command failed.", next_state="listening")
            return

        if command_name == "start_task":
            await self._announce_text(websocket, state, message or "Task started.", next_state="thinking")
            return
        if command_name == "stop_task":
            await self._announce_text(websocket, state, message or "Stopping the current task.", next_state="thinking")
            return
        await self._send_status(websocket, state, "listening")

    async def _maybe_announce_snapshot(self, websocket, state: VoiceSessionState) -> None:
        signature = _result_signature(state.latest_snapshot)
        if not signature or signature == state.last_result_signature:
            return
        state.last_result_signature = signature
        # Completed task summaries are spoken once when the terminal snapshot
        # changes, which keeps the voice layer stateless across reconnects.
        text = str(
            state.latest_snapshot.get("latest_result")
            or state.latest_snapshot.get("error")
            or state.latest_snapshot.get("latest_summary")
            or ""
        ).strip()
        if not text:
            return
        await self._announce_text(websocket, state, text, next_state="listening")

    async def handle_client(self, websocket) -> None:
        state = VoiceSessionState()
        await self._send_status(websocket, state, "idle")
        async for raw in websocket:
            data = json.loads(raw)
            msg_type = str(data.get("type") or "").strip()

            if msg_type == "hello":
                state.voice_name = _clean_text(str(data.get("voice_name") or "marin")).lower() or "marin"
                state.language = _clean_text(str(data.get("response_language") or data.get("transcription_language") or "en")).lower() or "en"
                await self._send_json(websocket, state, {"type": "ready", "local": True})
                await self._send_status(websocket, state, "listening")
                continue

            if msg_type == "heartbeat":
                continue

            if msg_type == "client_state":
                if "voice_name" in data:
                    state.voice_name = _clean_text(str(data.get("voice_name") or state.voice_name)).lower() or state.voice_name
                if "listening" in data:
                    state.listening_enabled = bool(data.get("listening"))
                continue

            if msg_type == "task_snapshot":
                state.latest_snapshot = dict(data.get("snapshot") or {})
                if _task_is_running(state.latest_snapshot):
                    await self._send_status(websocket, state, "thinking")
                else:
                    await self._send_status(websocket, state, "listening")
                await self._maybe_announce_snapshot(websocket, state)
                continue

            if msg_type == "text":
                transcript = _clean_text(str(data.get("text") or ""))
                if not transcript:
                    continue
                state.processing_audio = True
                await self._process_transcript(websocket, state, transcript)
                continue

            if msg_type == "audio_chunk":
                if not state.listening_enabled or state.processing_audio:
                    continue
                try:
                    chunk = base64.b64decode(str(data.get("audio_b64") or ""), validate=False)
                except Exception:
                    chunk = b""
                pcm_bytes = self._ingest_audio_chunk(state, chunk)
                if not pcm_bytes:
                    continue
                state.processing_audio = True
                asyncio.create_task(self._process_audio(websocket, state, pcm_bytes))
                continue

            if msg_type == "command_result":
                await self._handle_command_result(websocket, state, data)
                continue

    async def start(self) -> None:
        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=30,
        ):
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(AriaVoiceServer().start())
