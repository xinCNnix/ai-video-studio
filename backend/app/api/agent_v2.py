"""Agent v2 API（前缀 /api/p2/agent）。

与旧 /api/agent 并存（旧前端继续用旧端点），Phase 5 前端切换后删除旧端点。
SSE 事件：route → step* → done；非流式 /run 返回最终结果。
"""
import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services import agent_v2

router = APIRouter(prefix="/api/p2/agent", tags=["agent-v2"])


class RunRequest(BaseModel):
    goal: str
    project_id: str | None = None
    scene_id: str | None = None
    model: str | None = None
    credential_id: str | None = None
    history: list[dict] | None = None


@router.post("/run")
async def run(req: RunRequest):
    context = {"project_id": req.project_id, "scene_id": req.scene_id}
    return await agent_v2.run_agent_v2(
        req.goal, context, credential_id=req.credential_id, model=req.model, history=req.history
    )


@router.post("/run/stream")
async def run_stream(req: RunRequest):
    context = {"project_id": req.project_id, "scene_id": req.scene_id}
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    def on_step(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def worker() -> dict:
        try:
            return await agent_v2.run_agent_v2(
                req.goal, context, credential_id=req.credential_id, model=req.model,
                history=req.history, on_step=on_step,
            )
        finally:
            try:
                queue.put_nowait({"type": "__end__"})
            except asyncio.QueueFull:
                pass

    task = asyncio.create_task(worker())

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=120.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event.get("type") == "__end__":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            result = await task
            yield f"data: {json.dumps({'type': 'done', 'result': result}, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


class CancelRequest(BaseModel):
    run_id: str


@router.post("/cancel")
async def cancel(req: CancelRequest):
    agent_v2.cancel_run(req.run_id.strip())
    return {"ok": True}
