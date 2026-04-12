from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import websockets

from backend.brain import (
    chunk_text,
)
from backend.loopagentic import LocalTaskLoop


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


def _is_local_type(value: Any, compact: str, legacy: str) -> bool:
    return str(value or "").strip() in {compact, legacy}


@dataclass
class ActiveRun:
    websocket: Any
    stop_event: threading.Event
    task: asyncio.Task[Any]


class AriaLocalServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.loopagentic = LocalTaskLoop()
        self.active_runs: dict[str, ActiveRun] = {}
        self.active_lock = threading.Lock()

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
            # The heavy OpenAI call stays off the websocket loop so the shell can
            # continue handling stop requests and parallel client events.
            result = await asyncio.to_thread(
                self.loopagentic.complete,
                session_id=session_id,
                prompt=prompt,
                image=image,
                stop_requested=stop_event.is_set,
                started_at=started_at,
            )
            if stop_event.is_set():
                return
            for chunk in chunk_text(result.response_text):
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
                    "response": result.response_text,
                    "usage": result.usage,
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
                    self.loopagentic.reset_session(session_id)
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
                    await self._send_json(
                        websocket,
                        {
                            "type": L3,
                            "requestId": request_id,
                            "data": {
                                "sessionId": session_id,
                                "title": self.loopagentic.generate_title(str(data.get("text") or "")),
                            },
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
