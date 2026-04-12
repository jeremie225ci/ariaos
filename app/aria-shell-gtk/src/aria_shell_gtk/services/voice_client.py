from __future__ import annotations

import asyncio
import base64
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from .core import (
    DEFAULT_VOICE_NAME,
    DEFAULT_VOICE_LANGUAGE,
    RuntimeStore,
    SUPPORTED_OPENAI_VOICE_NAMES,
    SUPPORTED_VOICE_LANGUAGES,
)


JsonDict = dict[str, Any]
SnapshotProvider = Callable[[], JsonDict]
CommandHandler = Callable[[str, str, str], tuple[bool, str]]
TextCallback = Callable[[str], None]


VOICE_WS_ENV = "ARIA_VOICE_WS_URL"
VOICE_LOCAL_WS_URL = "ws://127.0.0.1:8081/ws/voice"
VOICE_HEARTBEAT_SECONDS = 12.0
VOICE_CONNECT_TIMEOUT = 12.0
VOICE_RETRY_SECONDS = 2.0
VOICE_AUDIO_SAMPLE_RATE = 24_000
VOICE_AUDIO_CHUNK_BYTES = 4_800
VOICE_WS_MAX_SIZE = 16 * 1024 * 1024
VOICE_NAME_OPTIONS = frozenset(SUPPORTED_OPENAI_VOICE_NAMES)
VOICE_LANGUAGE_OPTIONS = frozenset(SUPPORTED_VOICE_LANGUAGES)
VOICE_DEBUG_LOG = Path("/tmp/aria-voice-client.log")


def _normalize_voice_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VOICE_NAME_OPTIONS:
        return normalized
    return DEFAULT_VOICE_NAME


def _normalize_voice_language(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VOICE_LANGUAGE_OPTIONS:
        return normalized
    return DEFAULT_VOICE_LANGUAGE


def _derive_voice_ws_url(service: RuntimeStore) -> str:
    # The public build talks to a local voice server inside the VM. An explicit
    # override is still allowed for development, but the default path is local.
    override = str(os.environ.get(VOICE_WS_ENV) or "").strip()
    if override:
        return override
    _ = service
    return VOICE_LOCAL_WS_URL


def _pick_capture_command() -> list[str] | None:
    candidates = [
        [
            "parec",
            "--device=@DEFAULT_SOURCE@",
            f"--rate={VOICE_AUDIO_SAMPLE_RATE}",
            "--channels=1",
            "--format=s16le",
            "--raw",
        ],
        ["arecord", "-q", "-c", "1", "-f", "S16_LE", "-r", str(VOICE_AUDIO_SAMPLE_RATE), "-t", "raw"],
        ["pw-record", "--rate", str(VOICE_AUDIO_SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"],
    ]
    for command in candidates:
        if shutil.which(command[0]):
            return command
    return None


def voice_environment_error() -> str:
    if websockets is None:
        return "Voice mode is unavailable because the websockets dependency is missing."
    if _pick_capture_command() is None:
        return "Voice mode is unavailable because no microphone capture command is installed."
    if shutil.which("ffplay") is None:
        return "Voice mode is unavailable because ffplay is not installed."
    return ""


def _prepare_audio_devices() -> None:
    if not shutil.which("pactl"):
        return
    commands = [
        ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"],
        ["pactl", "set-source-volume", "@DEFAULT_SOURCE@", "120%"],
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "160%"],
    ]
    for command in commands:
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except Exception:
            continue


def _debug_log(message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        VOICE_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with VOICE_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


class VoiceClient:
    def __init__(
        self,
        *,
        service: RuntimeStore,
        snapshot_provider: SnapshotProvider,
        command_handler: CommandHandler,
        on_state: TextCallback | None = None,
        on_transcript: TextCallback | None = None,
        on_spoken_text: TextCallback | None = None,
        on_error: TextCallback | None = None,
    ) -> None:
        self.service = service
        self.snapshot_provider = snapshot_provider
        self.command_handler = command_handler
        self.on_state = on_state
        self.on_transcript = on_transcript
        self.on_spoken_text = on_spoken_text
        self.on_error = on_error

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_queue: asyncio.Queue[JsonDict] | None = None
        self._stop_event = threading.Event()
        self._latest_snapshot: JsonDict = {}
        self._connected = False

        self._playback_proc: subprocess.Popen[bytes] | None = None
        self._playback_queue: queue.Queue[bytes | None] | None = None
        self._playback_thread: threading.Thread | None = None
        self._playback_lock = threading.Lock()
        self._capture_paused_until = 0.0
        self._assistant_speaking = False
        self._listening_enabled = True

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.active:
            _debug_log("start skipped: already active")
            return True
        environment_error = voice_environment_error()
        if environment_error:
            _debug_log(f"start failed: {environment_error}")
            self._emit_error(environment_error)
            return False
        self._stop_event.clear()
        _debug_log("start requested")
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="aria-voice-client")
        self._thread.start()
        return True

    def stop(self) -> None:
        _debug_log("stop requested")
        self._stop_event.set()
        self._stop_playback()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._close_async_resources)

    def publish_snapshot(self, snapshot: JsonDict) -> None:
        self._latest_snapshot = dict(snapshot or {})
        self._queue_payload({"type": "task_snapshot", "snapshot": self._latest_snapshot})

    def set_listening_enabled(self, listening: bool) -> None:
        self._set_listening_enabled_internal(bool(listening), force=True)

    def set_voice_name(self, voice_name: str) -> None:
        self._queue_payload({"type": "client_state", "voice_name": _normalize_voice_name(voice_name)})

    def send_text(self, text: str) -> None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        self._queue_payload({"type": "text", "text": cleaned})

    def _thread_main(self) -> None:
        _debug_log("thread main enter")
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._send_queue = asyncio.Queue()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_forever())
        finally:
            _debug_log("thread main exit")
            try:
                pending = asyncio.all_tasks(loop)
            except Exception:
                pending = set()
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
            loop.close()
            self._loop = None
            self._send_queue = None
            self._connected = False
            self._emit_state("idle")

    def _queue_payload(self, payload: JsonDict) -> None:
        queue_ref = self._send_queue
        loop = self._loop
        if queue_ref is None or loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(queue_ref.put_nowait, dict(payload))
        except Exception:
            pass

    def _set_listening_enabled_internal(self, listening: bool, *, force: bool = False) -> None:
        normalized = bool(listening)
        if not force and normalized == self._listening_enabled:
            return
        self._listening_enabled = normalized
        _debug_log(f"client listening -> {self._listening_enabled}")
        self._queue_payload({"type": "client_state", "listening": self._listening_enabled})

    def _close_async_resources(self) -> None:
        task = asyncio.create_task(self._shutdown_async())
        task.add_done_callback(lambda _task: None)

    async def _shutdown_async(self) -> None:
        queue_ref = self._send_queue
        if queue_ref is not None:
            try:
                queue_ref.put_nowait({"type": "__close__"})
            except Exception:
                pass

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            ws_url = _derive_voice_ws_url(self.service)
            if not ws_url:
                _debug_log("run_forever abort: ws_url missing")
                self._emit_error("Voice mode is disabled in this local-only AriaOS build.")
                return
            try:
                _debug_log(f"connecting to {ws_url}")
                await self._connect_once(ws_url)
            except asyncio.CancelledError:
                _debug_log("run_forever cancelled")
                raise
            except Exception as exc:
                self._connected = False
                _debug_log(f"run_forever error: {type(exc).__name__}: {exc}")
                self._emit_error(f"Voice mode reconnecting: {exc}")
                self._emit_state("error")
            if self._stop_event.is_set():
                return
            await asyncio.sleep(VOICE_RETRY_SECONDS)

    async def _connect_once(self, ws_url: str) -> None:
        state = self.service.read()
        self._emit_state("thinking")
        _debug_log("connect_once opening websocket")
        websocket = await asyncio.wait_for(
            websockets.connect(
                ws_url,
                max_size=VOICE_WS_MAX_SIZE,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ),
            timeout=VOICE_CONNECT_TIMEOUT,
        )
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "device_id": state.device_id,
                        "device_name": state.device_name,
                        "machine_uid": state.machine_uid,
                        "voice_name": _normalize_voice_name(getattr(state, "voice_name", DEFAULT_VOICE_NAME)),
                        "transcription_language": _normalize_voice_language(getattr(state, "voice_language", DEFAULT_VOICE_LANGUAGE)),
                        "response_language": _normalize_voice_language(getattr(state, "voice_language", DEFAULT_VOICE_LANGUAGE)),
                        "client_build": int(getattr(state, "app_build", 0) or 0),
                        "client_version": str(getattr(state, "app_version", "") or "").strip(),
                    },
                    ensure_ascii=False,
                )
            )
            _debug_log("hello sent")
            raw_ready = await asyncio.wait_for(websocket.recv(), timeout=VOICE_CONNECT_TIMEOUT)
            ready = json.loads(raw_ready)
            if str(ready.get("type") or "") == "error":
                raise RuntimeError(str(ready.get("message") or "voice handshake failed"))
            if str(ready.get("type") or "") != "ready":
                raise RuntimeError("voice handshake failed")

            self._connected = True
            _debug_log("handshake ready")
            self._emit_state("listening")
            if self._latest_snapshot:
                await websocket.send(json.dumps({"type": "task_snapshot", "snapshot": self._latest_snapshot}, ensure_ascii=False))
            else:
                try:
                    snapshot = dict(self.snapshot_provider() or {})
                except Exception:
                    snapshot = {}
                if snapshot:
                    self._latest_snapshot = snapshot
                    await websocket.send(json.dumps({"type": "task_snapshot", "snapshot": snapshot}, ensure_ascii=False))

            # Sender, receiver, heartbeat and microphone capture run as peers so
            # the local voice session behaves like a single duplex channel.
            sender = asyncio.create_task(self._sender_loop(websocket))
            receiver = asyncio.create_task(self._receiver_loop(websocket))
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            capture = asyncio.create_task(self._capture_audio_loop())
            done, pending = await asyncio.wait(
                {sender, receiver, heartbeat, capture},
                return_when=asyncio.FIRST_COMPLETED,
            )
            _debug_log(
                "task group completed: "
                + ", ".join(
                    f"{name}={task.done()}"
                    for name, task in (
                        ("sender", sender),
                        ("receiver", receiver),
                        ("heartbeat", heartbeat),
                        ("capture", capture),
                    )
                )
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None:
                    _debug_log(f"task exception: {type(error).__name__}: {error}")
                    raise error
        finally:
            self._connected = False
            _debug_log("connect_once cleanup")
            self._stop_playback()
            try:
                await websocket.close()
            except Exception:
                pass

    async def _sender_loop(self, websocket) -> None:
        assert self._send_queue is not None
        while not self._stop_event.is_set():
            payload = await self._send_queue.get()
            if str(payload.get("type") or "") == "__close__":
                _debug_log("sender received close sentinel")
                await websocket.close()
                return
            await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _receiver_loop(self, websocket) -> None:
        async for raw in websocket:
            data = json.loads(raw)
            msg_type = str(data.get("type") or "").strip()
            if msg_type in {"status", "error"}:
                _debug_log(f"receiver event: {msg_type} {data}")
            if msg_type == "status":
                self._emit_state(str(data.get("state") or "listening"))
                continue
            if msg_type == "transcript":
                self._emit_transcript(str(data.get("text") or ""))
                continue
            if msg_type == "assistant_text":
                self._emit_spoken_text(str(data.get("text") or ""))
                continue
            if msg_type == "audio_delta":
                # The server streams raw PCM deltas so playback can start before
                # the full spoken answer has been generated.
                audio_b64 = str(data.get("audio_b64") or "")
                if audio_b64:
                    try:
                        audio_bytes = base64.b64decode(audio_b64, validate=False)
                    except Exception:
                        audio_bytes = b""
                    if audio_bytes:
                        self._queue_audio_delta(audio_bytes)
                continue
            if msg_type == "audio_done":
                self._finish_playback()
                continue
            if msg_type == "interrupt":
                self._stop_playback()
                continue
            if msg_type == "command":
                ok, message = await asyncio.to_thread(
                    self.command_handler,
                    str(data.get("command_id") or "").strip(),
                    str(data.get("command") or "").strip(),
                    str(data.get("text") or "").strip(),
                )
                self._queue_payload(
                    {
                        "type": "command_result",
                        "command_id": str(data.get("command_id") or "").strip(),
                        "ok": bool(ok),
                        "message": str(message or "").strip(),
                    }
                )
                continue
            if msg_type == "error":
                self._emit_error(str(data.get("message") or "Voice mode error."))
                continue
        _debug_log("receiver loop ended")

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(VOICE_HEARTBEAT_SECONDS)
            self._queue_payload({"type": "heartbeat", "ts": int(time.time())})
        _debug_log("heartbeat loop ended")

    async def _capture_audio_loop(self) -> None:
        command = _pick_capture_command()
        if not command:
            _debug_log("capture loop abort: no capture command")
            self._emit_error("Voice mode is unavailable because no microphone capture command is installed.")
            return
        _debug_log(f"capture command: {' '.join(command)}")
        await asyncio.to_thread(_prepare_audio_devices)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            while not self._stop_event.is_set():
                chunk = await proc.stdout.read(VOICE_AUDIO_CHUNK_BYTES)
                if not chunk:
                    _debug_log("capture loop EOF")
                    break
                # While Aria is speaking, microphone capture is paused locally to
                # avoid feeding the synthesized answer straight back into STT.
                if self._assistant_speaking:
                    continue
                if time.monotonic() < self._capture_paused_until:
                    continue
                self._queue_payload(
                    {
                        "type": "audio_chunk",
                        "audio_b64": base64.b64encode(chunk).decode("ascii"),
                    }
                )
        finally:
            _debug_log(f"capture cleanup returncode={proc.returncode}")
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except Exception:
                    proc.kill()
                    await proc.wait()

    def _queue_audio_delta(self, audio_bytes: bytes) -> None:
        if not audio_bytes:
            return
        # Briefly pause capture before and during playback so the local voice
        # loop does not self-trigger on its own synthesized audio.
        self._capture_paused_until = max(self._capture_paused_until, time.monotonic() + 0.85)
        should_disable_listening = False
        _debug_log(f"audio delta bytes={len(audio_bytes)}")
        with self._playback_lock:
            if not self._assistant_speaking:
                self._assistant_speaking = True
                should_disable_listening = True
            self._ensure_playback_stream_locked()
            queue_ref = self._playback_queue
        if should_disable_listening:
            self._set_listening_enabled_internal(False)
        if queue_ref is None:
            _debug_log("audio delta dropped: no playback queue")
            return
        try:
            queue_ref.put_nowait(audio_bytes)
        except queue.Full:
            _debug_log("audio delta dropped: playback queue full")
            pass

    def _ensure_playback_stream_locked(self) -> None:
        proc = self._playback_proc
        if proc is not None and proc.poll() is None and self._playback_queue is not None:
            return
        player = shutil.which("ffplay")
        if not player:
            _debug_log("playback unavailable: ffplay missing")
            return
        try:
            proc = subprocess.Popen(
                [
                    player,
                    "-autoexit",
                    "-nodisp",
                    "-loglevel",
                    "quiet",
                    "-f",
                    "s16le",
                    "-ar",
                    str(VOICE_AUDIO_SAMPLE_RATE),
                    "-ch_layout",
                    "mono",
                    "-af",
                    "volume=12dB",
                    "-i",
                    "pipe:0",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _debug_log(f"playback process started pid={proc.pid}")
        except Exception:
            _debug_log("playback process failed to start")
            return
        queue_ref: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        thread = threading.Thread(
            target=self._playback_writer,
            args=(proc, queue_ref),
            daemon=True,
            name="aria-voice-playback",
        )
        self._playback_proc = proc
        self._playback_queue = queue_ref
        self._playback_thread = thread
        thread.start()

    def _playback_writer(self, proc: subprocess.Popen[bytes], queue_ref: queue.Queue[bytes | None]) -> None:
        try:
            while True:
                chunk = queue_ref.get()
                if chunk is None:
                    _debug_log("playback writer received close sentinel")
                    break
                if proc.stdin is None:
                    _debug_log("playback writer abort: stdin missing")
                    break
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except Exception:
                    _debug_log("playback writer abort: stdin write failed")
                    break
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            _debug_log(f"playback process exited returncode={proc.returncode}")
            should_reenable_listening = False
            with self._playback_lock:
                if self._playback_proc is proc:
                    should_reenable_listening = self._assistant_speaking
                    self._assistant_speaking = False
                    self._playback_proc = None
                    self._playback_queue = None
                    self._playback_thread = None
            self._capture_paused_until = max(self._capture_paused_until, time.monotonic() + 0.25)
            if should_reenable_listening:
                self._set_listening_enabled_internal(True)

    def _finish_playback(self) -> None:
        self._capture_paused_until = max(self._capture_paused_until, time.monotonic() + 0.45)
        with self._playback_lock:
            queue_ref = self._playback_queue
        if queue_ref is None:
            return
        try:
            queue_ref.put_nowait(None)
        except queue.Full:
            pass

    def _stop_playback(self) -> None:
        self._capture_paused_until = max(self._capture_paused_until, time.monotonic() + 0.25)
        with self._playback_lock:
            proc = self._playback_proc
            queue_ref = self._playback_queue
            self._assistant_speaking = False
            self._playback_proc = None
            self._playback_queue = None
            self._playback_thread = None
        self._set_listening_enabled_internal(True)
        if queue_ref is not None:
            try:
                queue_ref.put_nowait(None)
            except Exception:
                pass
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _emit_state(self, state: str) -> None:
        if callable(self.on_state):
            try:
                self.on_state(str(state or "idle"))
            except Exception:
                pass

    def _emit_transcript(self, text: str) -> None:
        if callable(self.on_transcript):
            try:
                self.on_transcript(str(text or ""))
            except Exception:
                pass

    def _emit_spoken_text(self, text: str) -> None:
        if callable(self.on_spoken_text):
            try:
                self.on_spoken_text(str(text or ""))
            except Exception:
                pass

    def _emit_error(self, text: str) -> None:
        if callable(self.on_error):
            try:
                self.on_error(str(text or "Voice mode error."))
            except Exception:
                pass
