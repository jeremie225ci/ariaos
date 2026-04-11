from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import threading
import time
import uuid
from typing import Any

import websockets

from .state import ShellState


def _run_shell(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        capture_output=True,
        text=True,
        check=False,
    )


class VisionPowerManager:
    def __init__(self, state: ShellState, ui_queue: queue.Queue):
        self.state = state
        self.ui_queue = ui_queue
        self._active = False

    def _emit(self, message: str) -> None:
        self.ui_queue.put({"type": "system", "message": message})

    def start_for_task(self) -> None:
        if self.state.vision_mode == "cloud":
            self._emit(f"[vision] cloud mode active -> {self.state.vision_cloud_parse_url}")
            self._active = True
            return
        cmd = self.state.vision_local_start_cmd.strip()
        if not cmd:
            self._active = True
            return
        proc = _run_shell(cmd)
        if proc.returncode == 0:
            self._emit("[vision] local parser power ON")
        elif proc.stderr.strip():
            self._emit(f"[vision] local parser start warning: {proc.stderr.strip()[:160]}")
        self._active = True

    def stop_after_task(self) -> None:
        if not self._active:
            return
        if self.state.vision_mode == "cloud":
            self._active = False
            return
        cmd = self.state.vision_local_stop_cmd.strip()
        if cmd:
            proc = _run_shell(cmd)
            if proc.returncode == 0:
                self._emit("[vision] local parser power OFF")
            elif proc.stderr.strip():
                self._emit(f"[vision] local parser stop warning: {proc.stderr.strip()[:160]}")
        self._active = False


class LocalTransportBridge(threading.Thread):
    def __init__(self, state: ShellState):
        super().__init__(daemon=True)
        self.state = state
        self.ui_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.task_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.shutdown_event = threading.Event()
        self.stop_event = threading.Event()
        self.vision = VisionPowerManager(state, self.ui_queue)
        self._last_reset_session_id: str | None = None

    def emit(self, payload: dict[str, Any]) -> None:
        self.ui_queue.put(payload)

    def submit_goal(
        self,
        session_id: str,
        prompt: str,
        image: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        task_id: str | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        self._last_reset_session_id = session_id
        self.task_queue.put({"type": "run_goal", "session_id": session_id, "prompt": prompt, "image": image})

    def reset_session(self, session_id: str) -> None:
        if self._last_reset_session_id == session_id:
            return
        self._last_reset_session_id = session_id
        self.task_queue.put({"type": "reset_session", "session_id": session_id})

    def request_title(self, session_id: str, text: str) -> None:
        self.task_queue.put({"type": "generate_title", "session_id": session_id, "text": text})

    def request_stop(self) -> None:
        self.stop_event.set()

    def shutdown(self) -> None:
        self.shutdown_event.set()
        self.task_queue.put({"type": "shutdown"})

    async def _send_json(self, ws, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _next_task(self) -> dict[str, Any]:
        while not self.shutdown_event.is_set():
            try:
                return self.task_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)
        return {"type": "shutdown"}

    async def _wait_for(self, ws, request_id: str, expected_type: str, timeout: float = 8.0) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            data = json.loads(raw)
            if data.get("requestId") == request_id and data.get("type") == expected_type:
                return data
            if data.get("type") == "proactive_action":
                self.emit({"type": "proactive", "data": data.get("data", {})})
        return None

    async def _reset_remote_session(self, ws, session_id: str, timeout: float = 8.0) -> bool:
        request_id = f"reset-{uuid.uuid4().hex[:8]}"
        await self._send_json(
            ws,
            {
                "type": "new_session",
                "requestId": request_id,
                "sessionId": session_id,
            },
        )
        reply = await self._wait_for(ws, request_id, "new_session_result", timeout=timeout)
        if not reply:
            self.emit({"type": "system", "message": "[session] reset timeout, continuing"})
            return False
        self.emit({"type": "system", "message": "[session] isolated context ready"})
        return True

    async def _request_session_title(self, ws, session_id: str, text: str, timeout: float = 8.0) -> str | None:
        request_id = f"title-{uuid.uuid4().hex[:8]}"
        await self._send_json(
            ws,
            {
                "type": "generate_title",
                "requestId": request_id,
                "sessionId": session_id,
                "text": text,
            },
        )
        reply = await self._wait_for(ws, request_id, "generate_title_result", timeout=timeout)
        if not reply:
            return None
        title = str((reply.get("data") or {}).get("title") or "").strip()
        if title:
            self.emit({"type": "session_title", "session_id": session_id, "title": title})
        return title or None

    async def _run_single_goal(self, ws, session_id: str, prompt: str, image: dict[str, Any] | None = None) -> None:
        self.stop_event.clear()
        self.vision.start_for_task()

        request_id = f"cmd-{uuid.uuid4().hex}"
        payload = {
            "type": "command",
            "requestId": request_id,
            "command": prompt,
            "sessionId": session_id,
        }
        if image:
            payload["image"] = image
        await self._send_json(ws, payload)
        self.emit({"type": "task_state", "running": True, "session_id": session_id})

        stop_sent = False
        try:
            while True:
                if self.stop_event.is_set() and not stop_sent:
                    stop_request_id = f"stop-{uuid.uuid4().hex[:8]}"
                    await self._send_json(
                        ws,
                        {
                            "type": "stop",
                            "requestId": stop_request_id,
                            "targetRequestId": request_id,
                            "sessionId": session_id,
                        },
                    )
                    stop_sent = True
                    self.emit({"type": "system", "message": "[stop] signal sent"})

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                data = json.loads(raw)
                msg_type = data.get("type")
                rid = data.get("requestId")

                if msg_type == "proactive_action":
                    self.emit({"type": "proactive", "data": data.get("data", {})})
                    continue

                if msg_type == "stream" and rid == request_id:
                    chunk = data.get("chunk", "")
                    if chunk:
                        self.emit({"type": "stream", "chunk": chunk, "session_id": session_id})
                    continue

                if msg_type == "response" and rid == request_id:
                    self.emit(
                        {
                            "type": "response",
                            "text": data.get("response", ""),
                            "session_id": session_id,
                            "usage": data.get("usage"),
                            "task_id": request_id,
                        }
                    )
                    break

                if msg_type == "stop_result":
                    stopped = bool((data.get("data") or {}).get("stopped"))
                    self.emit({"type": "system", "message": f"[stop] acknowledged={stopped}"})
                    if stopped:
                        self.emit({"type": "response", "text": "__ARIA_STOPPED__", "session_id": session_id})
                        break
        finally:
            self.stop_event.clear()
            self.vision.stop_after_task()
            self.emit({"type": "task_state", "running": False, "session_id": session_id})

    async def _loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                self.emit({"type": "status", "text": f"connecting {self.state.backend_ws_url}"})
                async with websockets.connect(
                    self.state.backend_ws_url,
                    ping_interval=20,
                    ping_timeout=30,
                    max_size=8_000_000,
                ) as ws:
                    self.emit({"type": "status", "text": "connected"})
                    while not self.shutdown_event.is_set():
                        task = await self._next_task()
                        if task.get("type") == "shutdown":
                            return
                        if task.get("type") == "reset_session":
                            session_id = task["session_id"]
                            await self._reset_remote_session(ws, session_id, timeout=4.0)
                            continue
                        if task.get("type") == "generate_title":
                            await self._request_session_title(ws, task["session_id"], str(task.get("text") or ""), timeout=6.0)
                            continue
                        if task.get("type") == "run_goal":
                            await self._run_single_goal(
                                ws,
                                task["session_id"],
                                task["prompt"],
                                image=task.get("image"),
                            )
            except Exception as exc:
                self.emit({"type": "status", "text": f"backend reconnect: {exc}"})
                await asyncio.sleep(1.5)

    def run(self) -> None:
        asyncio.run(self._loop())
