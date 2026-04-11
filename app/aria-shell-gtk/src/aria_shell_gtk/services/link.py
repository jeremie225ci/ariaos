from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from .core import RuntimeState, RuntimeStore
from .remote_runtime import RemoteComputerRuntime
from .codec import M0, M1, M2, M3, M4, M5, M6, M7, M8, M9, MA, MB, MC, MD, ME, MF, MG


WEBSOCKET_MAX_SIZE = int(os.environ.get("ARIA_REMOTE_WS_MAX_SIZE", str(16 * 1024 * 1024)) or str(16 * 1024 * 1024))


def _fallback_title(text: str) -> str:
    words = [part for part in str(text or "").strip().split() if part]
    if not words:
        return "New Chat"
    return " ".join(words[:5])[:48].strip() or "New Chat"


def _raw_chunk_to_stream(raw_chunk: str) -> str:
    value = str(raw_chunk or "")
    if not value:
        return ""
    if value.endswith("\n"):
        return value
    return value + "\n"


class LiveLoop(threading.Thread):
    def __init__(self, state: RuntimeState):
        super().__init__(daemon=True)
        self.state = state
        self.service = RuntimeStore()
        self.ui_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.task_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.shutdown_event = threading.Event()
        self._active_task_id: str | None = None
        self._active_session_id: str | None = None
        self.runtime = RemoteComputerRuntime()

    def emit(self, payload: dict[str, Any]) -> None:
        self.ui_queue.put(payload)

    def submit_goal(
        self,
        session_id: str,
        prompt: str,
        image: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        max_budget_usd: float | None = None,
        model: str | None = None,
    ) -> None:
        self.task_queue.put(
            {
                "type": "run_goal",
                "session_id": session_id,
                "prompt": prompt,
                "image": image,
                "task_id": str(task_id or "").strip(),
                "max_budget_usd": float(max_budget_usd or 0.0),
                "model": str(model or "").strip(),
            }
        )

    def reset_session(self, session_id: str) -> None:
        self._active_session_id = session_id

    def request_title(self, session_id: str, text: str) -> None:
        self.emit({"type": "session_title", "session_id": session_id, "title": _fallback_title(text)})

    def request_stop(self) -> None:
        self.task_queue.put({"type": "stop_task"})

    def send_task_message(self, text: str, *, role: str = "operator", message_id: str = "") -> bool:
        if not self._active_task_id or not self._active_session_id:
            return False
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        self.task_queue.put(
            {
                "type": "task_message",
                "text": cleaned,
                "role": str(role or "operator").strip() or "operator",
                "message_id": str(message_id or "").strip(),
            }
        )
        return True

    def request_pause(self) -> bool:
        if not self._active_task_id or not self._active_session_id:
            self.emit({"type": "system", "message": "[remote-control] pause ignored: no active task"})
            return False
        self.emit({"type": "system", "message": f"[remote-control] queue pause task={self._active_task_id}"})
        self.task_queue.put({"type": "pause_task"})
        return True

    def request_resume(self) -> bool:
        if not self._active_task_id or not self._active_session_id:
            self.emit({"type": "system", "message": "[remote-control] resume ignored: no active task"})
            return False
        self.emit({"type": "system", "message": f"[remote-control] queue resume task={self._active_task_id}"})
        self.task_queue.put({"type": "resume_task"})
        return True

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

    async def _sender(self, ws) -> None:
        while not self.shutdown_event.is_set():
            task = await self._next_task()
            kind = task.get("type")
            if kind == "shutdown":
                return
            if kind == "stop_task":
                if self._active_task_id and self._active_session_id:
                    await self._send_json(
                        ws,
                        {
                            "type": M7,
                            "taskId": self._active_task_id,
                            "sessionId": self._active_session_id,
                        },
                    )
                continue
            if kind == "pause_task":
                if self._active_task_id and self._active_session_id:
                    self.emit({"type": "system", "message": f"[remote-control] send pause task={self._active_task_id}"})
                    await self._send_json(
                        ws,
                        {
                            "type": M8,
                            "taskId": self._active_task_id,
                            "sessionId": self._active_session_id,
                        },
                    )
                continue
            if kind == "resume_task":
                if self._active_task_id and self._active_session_id:
                    self.emit({"type": "system", "message": f"[remote-control] send resume task={self._active_task_id}"})
                    await self._send_json(
                        ws,
                        {
                            "type": M9,
                            "taskId": self._active_task_id,
                            "sessionId": self._active_session_id,
                        },
                    )
                continue
            if kind == "task_message":
                if self._active_task_id and self._active_session_id:
                    await self._send_json(
                        ws,
                        {
                            "type": MA,
                            "taskId": self._active_task_id,
                            "sessionId": self._active_session_id,
                            "text": str(task.get("text") or ""),
                            "role": str(task.get("role") or "operator"),
                            "messageId": str(task.get("message_id") or ""),
                        },
                    )
                continue
            if kind != "run_goal":
                continue
            task_id = str(task.get("task_id") or "").strip() or f"task_{uuid.uuid4().hex}"
            self._active_task_id = task_id
            self._active_session_id = str(task.get("session_id") or "")
            goal = str(task.get("prompt") or "").strip()
            ok, prepared = await asyncio.to_thread(
                self.service.prepare_task,
                task_id=task_id,
                session_id=self._active_session_id,
                goal=goal,
                requested_model=str(task.get("model") or "").strip(),
                image=task.get("image") if isinstance(task.get("image"), dict) else None,
                max_budget_usd=float(task.get("max_budget_usd") or 0.0),
            )
            self.state = self.service.read()
            if not ok:
                self.emit(
                    {
                        "type": "response",
                        "text": str(prepared.get("message") or "Remote task access denied."),
                        "session_id": self._active_session_id,
                        "task_id": task_id,
                    }
                )
                self.emit({"type": "task_state", "running": False, "session_id": self._active_session_id})
                self._active_task_id = None
                continue
            prepared["type"] = M5
            await self._send_json(ws, prepared)

    async def _receiver(self, ws) -> None:
        while not self.shutdown_event.is_set():
            raw = await ws.recv()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == MC:
                self.emit({"type": "task_state", "running": True, "session_id": self._active_session_id})
                continue

            if msg_type == M6:
                raw_chunk = str(data.get("rawChunk") or "")
                if not raw_chunk:
                    parts = []
                    think = str(data.get("think") or "").strip()
                    action = str(data.get("action") or "").strip()
                    progress = str(data.get("progress") or "").strip()
                    if think:
                        parts.append(f"🧠 Thought: {think}")
                    if action:
                        parts.append(f"🔧 Action: {action}")
                    if progress:
                        parts.append(f"📈 Progress: {progress}")
                    raw_chunk = "\n".join(parts)
                if raw_chunk:
                    self.emit(
                        {
                            "type": "stream",
                            "chunk": _raw_chunk_to_stream(raw_chunk),
                            "session_id": self._active_session_id,
                            "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None,
                        }
                    )
                continue

            if msg_type == M1:
                request_id = str(data.get("requestId") or "")
                ok, detail = await asyncio.to_thread(
                    self.runtime.execute_action,
                    data.get("action") if isinstance(data.get("action"), dict) else {},
                )
                self.emit(
                    {
                        "type": "computer_action",
                        "task_id": str(data.get("taskId") or self._active_task_id or ""),
                        "detail": str(detail or ""),
                        "ok": bool(ok),
                    }
                )
                await self._send_json(
                    ws,
                    {
                        "type": M2,
                        "taskId": str(data.get("taskId") or self._active_task_id or ""),
                        "requestId": request_id,
                        "ok": bool(ok),
                        "detail": str(detail or ""),
                    },
                )
                continue

            if msg_type == M3:
                request_id = str(data.get("requestId") or "")
                ok, image_base64, mime, detail = await asyncio.to_thread(self.runtime.capture_screenshot)
                self.emit(
                    {
                        "type": "computer_screenshot",
                        "task_id": str(data.get("taskId") or self._active_task_id or ""),
                        "ok": bool(ok),
                        "image_base64": str(image_base64 or ""),
                        "mime": str(mime or "image/png"),
                        "detail": str(detail or ""),
                    }
                )
                await self._send_json(
                    ws,
                    {
                        "type": M4,
                        "taskId": str(data.get("taskId") or self._active_task_id or ""),
                        "requestId": request_id,
                        "ok": bool(ok),
                        "imageBase64": str(image_base64 or ""),
                        "mime": str(mime or "image/png"),
                        "detail": str(detail or ""),
                    },
                )
                continue

            if msg_type == MF:
                request_id = str(data.get("requestId") or "")
                operation = str(data.get("operation") or "").strip().lower()
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                ok = False
                detail = f"unsupported_vm_tool:{operation}"
                result_payload: dict[str, Any] = {}

                if operation == "execute":
                    ok, detail = await asyncio.to_thread(
                        self.runtime.execute_command,
                        str(payload.get("command") or ""),
                    )
                elif operation == "write_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.write_file,
                        str(payload.get("path") or ""),
                        str(payload.get("content") or ""),
                    )
                elif operation == "read_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_file,
                        str(payload.get("path") or ""),
                        int(payload.get("start") or 1),
                        int(payload.get("end") or (int(payload.get("start") or 1) + 50)),
                    )
                elif operation == "list_dir":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.list_dir,
                        str(payload.get("path") or "."),
                    )
                elif operation == "open_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.open_file,
                        str(payload.get("path") or ""),
                    )
                elif operation == "open_url":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.open_url,
                        str(payload.get("url") or ""),
                    )
                elif operation == "reveal_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.reveal_path,
                        str(payload.get("path") or ""),
                    )
                elif operation == "copy_to_clipboard":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.copy_to_clipboard,
                        str(payload.get("text") or ""),
                    )
                elif operation == "paste":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.paste,
                    )
                elif operation == "read_clipboard":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_clipboard,
                    )
                elif operation == "focus_window":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.focus_window,
                        str(payload.get("app_or_title") or ""),
                    )
                elif operation == "open_app":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.open_app,
                        str(payload.get("app_name") or ""),
                    )
                elif operation == "search_files":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.search_files,
                        str(payload.get("pattern") or ""),
                        str(payload.get("root") or ""),
                    )
                elif operation == "select_file_for_active_dialog":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.select_file_for_active_dialog,
                        str(payload.get("path") or ""),
                    )
                elif operation == "upload_file_to_active_app":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.upload_file_to_active_app,
                        str(payload.get("path") or ""),
                    )
                elif operation == "drag_file_to_target":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.drag_file_to_target,
                        str(payload.get("path") or ""),
                        int(payload.get("x") or 0),
                        int(payload.get("y") or 0),
                    )
                elif operation == "list_windows":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.list_windows,
                    )
                elif operation == "make_dir":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.make_dir,
                        str(payload.get("path") or ""),
                    )
                elif operation == "move_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.move_path,
                        str(payload.get("src") or ""),
                        str(payload.get("dst") or ""),
                    )
                elif operation == "copy_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.copy_path,
                        str(payload.get("src") or ""),
                        str(payload.get("dst") or ""),
                    )
                elif operation == "delete_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.delete_path,
                        str(payload.get("path") or ""),
                    )
                elif operation == "append_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.append_file,
                        str(payload.get("path") or ""),
                        str(payload.get("content") or ""),
                    )
                elif operation == "replace_in_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.replace_in_file,
                        str(payload.get("path") or ""),
                        str(payload.get("search") or ""),
                        str(payload.get("replace") or ""),
                    )
                elif operation == "tail_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.tail_file,
                        str(payload.get("path") or ""),
                        int(payload.get("lines") or 20),
                    )
                elif operation == "read_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_path,
                        str(payload.get("path") or ""),
                    )
                elif operation == "open_with":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.open_with,
                        str(payload.get("path") or ""),
                        str(payload.get("app_name") or ""),
                    )
                elif operation == "read_clipboard_image":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_clipboard_image,
                    )
                elif operation == "replace_regex_in_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.replace_regex_in_file,
                        str(payload.get("path") or ""),
                        str(payload.get("pattern") or ""),
                        str(payload.get("replace") or ""),
                    )
                elif operation == "grep_in_files":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.grep_in_files,
                        str(payload.get("pattern") or ""),
                        str(payload.get("root") or ""),
                    )
                elif operation == "stat_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.stat_path,
                        str(payload.get("path") or ""),
                    )
                elif operation == "diff_paths":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.diff_paths,
                        str(payload.get("a") or ""),
                        str(payload.get("b") or ""),
                    )
                elif operation == "list_processes":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.list_processes,
                    )
                elif operation == "kill_process":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.kill_process,
                        str(payload.get("pid_or_name") or ""),
                    )
                elif operation == "chmod_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.chmod_path,
                        str(payload.get("path") or ""),
                        str(payload.get("mode") or ""),
                    )
                elif operation == "symlink_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.symlink_path,
                        str(payload.get("src") or ""),
                        str(payload.get("dst") or ""),
                    )
                elif operation == "unzip_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.unzip_path,
                        str(payload.get("path") or ""),
                        str(payload.get("dst") or ""),
                    )
                elif operation == "archive_path":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.archive_path,
                        str(payload.get("path") or ""),
                        str(payload.get("dst") or ""),
                    )
                elif operation == "read_json":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_json,
                        str(payload.get("path") or ""),
                    )
                elif operation == "write_json":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.write_json,
                        str(payload.get("path") or ""),
                        payload.get("data"),
                    )
                elif operation == "read_csv_preview":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_csv_preview,
                        str(payload.get("path") or ""),
                        int(payload.get("rows") or 10),
                    )
                elif operation == "git_status":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.git_status,
                        str(payload.get("repo") or ""),
                    )
                elif operation == "git_diff":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.git_diff,
                        str(payload.get("repo") or ""),
                        str(payload.get("path") or ""),
                    )
                elif operation == "run_tests":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.run_tests,
                        str(payload.get("target") or ""),
                    )
                elif operation == "download_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.download_file,
                        str(payload.get("url") or ""),
                        str(payload.get("path") or ""),
                    )
                elif operation == "extract_text_from_pdf":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.extract_text_from_pdf,
                        str(payload.get("path") or ""),
                    )
                elif operation == "replace_block_in_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.replace_block_in_file,
                        str(payload.get("path") or ""),
                        str(payload.get("start_marker") or ""),
                        str(payload.get("end_marker") or ""),
                        str(payload.get("content") or ""),
                    )
                elif operation == "grep_ast":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.grep_ast,
                        str(payload.get("symbol") or ""),
                        str(payload.get("root") or ""),
                    )
                elif operation == "read_yaml":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_yaml,
                        str(payload.get("path") or ""),
                    )
                elif operation == "write_yaml":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.write_yaml,
                        str(payload.get("path") or ""),
                        payload.get("data"),
                    )
                elif operation == "list_ports":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.list_ports,
                    )
                elif operation == "process_tree":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.process_tree,
                        str(payload.get("pid_or_name") or ""),
                    )
                elif operation == "disk_usage":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.disk_usage,
                        str(payload.get("path") or ""),
                    )
                elif operation == "find_large_files":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.find_large_files,
                        str(payload.get("root") or ""),
                        float(payload.get("limit_mb") or 1),
                    )
                elif operation == "env_get":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.env_get,
                        str(payload.get("name") or ""),
                    )
                elif operation == "env_set_local":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.env_set_local,
                        str(payload.get("name") or ""),
                        str(payload.get("value") or ""),
                    )
                elif operation == "http_request":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.http_request,
                        str(payload.get("method") or ""),
                        str(payload.get("url") or ""),
                        payload.get("headers"),
                        payload.get("body"),
                    )
                elif operation == "sqlite_query":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.sqlite_query,
                        str(payload.get("path") or ""),
                        str(payload.get("sql") or ""),
                    )
                elif operation == "read_json_schema":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_json_schema,
                        str(payload.get("path") or ""),
                    )
                elif operation == "read_yaml_schema":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.read_yaml_schema,
                        str(payload.get("path") or ""),
                    )
                elif operation == "docker_ps":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.docker_ps,
                    )
                elif operation == "docker_logs":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.docker_logs,
                        str(payload.get("container") or ""),
                    )
                elif operation == "systemctl_status":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.systemctl_status,
                        str(payload.get("service") or ""),
                    )
                elif operation == "systemctl_restart":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.systemctl_restart,
                        str(payload.get("service") or ""),
                    )
                elif operation == "ffmpeg_run":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.ffmpeg_run,
                        payload.get("args"),
                    )
                elif operation == "blender_render":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.blender_render,
                        str(payload.get("script") or ""),
                        str(payload.get("output_path") or ""),
                        payload.get("size"),
                    )
                elif operation == "compose_video_from_images":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.compose_video_from_images,
                        payload.get("inputs"),
                        str(payload.get("output_path") or ""),
                        float(payload.get("fps") or 24.0),
                    )
                elif operation == "generate_image_ai":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.generate_image_ai,
                        str(payload.get("prompt") or ""),
                        str(payload.get("output_path") or ""),
                        str(payload.get("size") or "1024x1024"),
                        str(payload.get("quality") or "low"),
                    )
                elif operation == "upscale_image_ai":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.upscale_image_ai,
                        str(payload.get("input_path") or ""),
                        str(payload.get("output_path") or ""),
                        str(payload.get("size") or "1536x1024"),
                        str(payload.get("quality") or "low"),
                    )
                elif operation == "remove_background_ai":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.remove_background_ai,
                        str(payload.get("input_path") or ""),
                        str(payload.get("output_path") or ""),
                        str(payload.get("quality") or "low"),
                    )
                elif operation == "edit_image_ai":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.edit_image_ai,
                        str(payload.get("prompt") or ""),
                        payload.get("input_paths"),
                        str(payload.get("output_path") or ""),
                        str(payload.get("mask_path") or ""),
                        str(payload.get("size") or "1024x1024"),
                        str(payload.get("quality") or "low"),
                    )
                elif operation == "extract_video_frames":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.extract_video_frames,
                        str(payload.get("path") or ""),
                        str(payload.get("output_dir") or ""),
                        float(payload.get("fps") or 1.0),
                        int(payload.get("max_frames") or 0),
                    )
                elif operation == "storyboard_to_video":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.storyboard_to_video,
                        payload.get("storyboard"),
                        str(payload.get("output_path") or ""),
                        str(payload.get("size") or "1024x1024"),
                        str(payload.get("quality") or "low"),
                        float(payload.get("fps") or 12.0),
                        float(payload.get("seconds_per_scene") or 2.0),
                    )
                elif operation == "generate_image":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.generate_image,
                        str(payload.get("prompt") or ""),
                        payload.get("size"),
                        str(payload.get("output_path") or ""),
                    )
                elif operation == "ocr_region":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.ocr_region,
                        int(payload.get("x") or 0),
                        int(payload.get("y") or 0),
                        int(payload.get("w") or 0),
                        int(payload.get("h") or 0),
                    )
                elif operation == "list_downloads":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.list_downloads,
                    )
                elif operation == "watch_file":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.watch_file,
                        str(payload.get("path") or ""),
                        float(payload.get("seconds") or 5),
                    )
                elif operation == "watch_dir":
                    ok, detail, result_payload = await asyncio.to_thread(
                        self.runtime.watch_dir,
                        str(payload.get("path") or ""),
                        float(payload.get("seconds") or 5),
                    )

                await self._send_json(
                    ws,
                    {
                        "type": MG,
                        "taskId": str(data.get("taskId") or self._active_task_id or ""),
                        "requestId": request_id,
                        "operation": operation,
                        "ok": bool(ok),
                        "detail": str(detail or ""),
                        "payload": dict(result_payload or {}),
                    },
                )
                continue

            if msg_type == M0:
                success = bool(data.get("success", True))
                self.emit(
                    {
                        "type": "response",
                        "text": str(data.get("result") or ""),
                        "session_id": self._active_session_id,
                        "usage": data.get("usage"),
                        "task_id": self._active_task_id,
                        "outcome": "completed" if success else "failed",
                    }
                )
                self.emit({"type": "task_state", "running": False, "session_id": self._active_session_id})
                self._active_task_id = None
                continue

            if msg_type == ME:
                self.emit(
                    {
                        "type": "response",
                        "text": "__ARIA_STOPPED__",
                        "session_id": self._active_session_id,
                        "task_id": self._active_task_id,
                        "outcome": "stopped",
                    }
                )
                self.emit({"type": "task_state", "running": False, "session_id": self._active_session_id})
                self._active_task_id = None
                continue

            if msg_type == MD:
                self.emit(
                    {
                        "type": "response",
                        "text": str(data.get("reason") or "Task failed."),
                        "session_id": self._active_session_id,
                        "task_id": self._active_task_id,
                        "outcome": "failed",
                    }
                )
                self.emit({"type": "task_state", "running": False, "session_id": self._active_session_id})
                self._active_task_id = None
                continue

            if msg_type == MB:
                self.emit({"type": "system", "message": str(data.get("message") or "")})
                continue

    async def _loop(self) -> None:
        gateway_url = self.state.remote_url.strip()
        if websockets is None:
            self.emit({"type": "status", "text": "remote link unavailable: install websockets"})
            return
        while not self.shutdown_event.is_set():
            try:
                self.emit({"type": "status", "text": "connecting remote link"})
                async with websockets.connect(
                    gateway_url,
                    ping_interval=20,
                    ping_timeout=30,
                    max_size=WEBSOCKET_MAX_SIZE,
                ) as ws:
                    self.emit({"type": "status", "text": "connected"})
                    sender = asyncio.create_task(self._sender(ws))
                    receiver = asyncio.create_task(self._receiver(ws))
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        exc = task.exception()
                        if exc:
                            raise exc
            except Exception as exc:
                self.emit({"type": "status", "text": f"remote link reconnect: {exc}"})
                await asyncio.sleep(1.5)

    def run(self) -> None:
        asyncio.run(self._loop())
