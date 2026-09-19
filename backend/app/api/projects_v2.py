"""项目 v2 命令式 API（前缀 /api/p2，Phase 5 前端切换后取代旧 /api/projects）。

设计要点：
- 一切修改走 project_store.mutate + base_version 乐观锁（409 = 有并发写，刷新重放）
- 字段级 PATCH，废除全量 scenes PUT
- GET /{id}/events：SSE 推送项目快照（按 version 去重），替代前端轮询
"""
import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..harness.bus import bus
from ..harness.errors import InvariantViolation, ProjectNotFoundError, VersionConflictError
from ..harness.state import ImageBinding, Scene, clamp_duration, new_scene_id
from ..services import project_store

router = APIRouter(prefix="/api/p2/projects", tags=["projects-v2"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, VersionConflictError):
        return HTTPException(status_code=409, detail=str(exc), headers={"x-current-version": str(exc.current_version)})
    if isinstance(exc, InvariantViolation):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


class CreateRequest(BaseModel):
    name: str = "未命名项目"
    aspect_ratio: str = "16:9"


class PatchProjectRequest(BaseModel):
    base_version: int | None = None
    name: str | None = None
    aspect_ratio: str | None = None
    settings: dict | None = None


class PatchSceneRequest(BaseModel):
    base_version: int | None = None
    title: str | None = None
    description: str | None = None
    prompt: str | None = None
    keyframe_prompt: str | None = None
    narration: str | None = None
    duration: int | None = None
    gen_mode: Literal["i2v", "t2v"] | None = None


class AddSceneRequest(BaseModel):
    base_version: int | None = None
    index: int | None = None
    title: str | None = None


class ReorderRequest(BaseModel):
    base_version: int
    order: list[str]


class BindRequest(BaseModel):
    base_version: int | None = None
    scene_id: str
    kind: Literal["image", "audio", "video"]
    material_id: str
    role: Literal["reference", "first_frame"] | None = None


@router.get("")
async def list_projects():
    return {"projects": project_store.list_projects()}


@router.post("")
async def create_project(req: CreateRequest):
    try:
        return project_store.create(req.name.strip()[:80] or "未命名项目", req.aspect_ratio)
    except (ProjectNotFoundError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.get("/{project_id}")
async def get_project(project_id: str):
    project = project_store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.patch("/{project_id}")
async def patch_project(project_id: str, req: PatchProjectRequest):
    def fn(p) -> None:
        if req.name is not None:
            p.name = req.name.strip()[:80] or p.name
        if req.aspect_ratio is not None:
            p.aspect_ratio = req.aspect_ratio
        if req.settings is not None:
            merged = p.settings.model_dump()
            merged.update(req.settings)
            p.settings = type(p.settings).model_validate(merged)

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    if not project_store.delete(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"ok": True}


# ---------------- 分镜命令 ----------------


def _find_scene(p, scene_id: str) -> tuple[int, Scene]:
    for i, s in enumerate(p.scenes):
        if s.id == scene_id:
            return i, s
    raise HTTPException(status_code=404, detail=f"分镜不存在：{scene_id}")


@router.patch("/{project_id}/scenes/{scene_id}")
async def patch_scene(project_id: str, scene_id: str, req: PatchSceneRequest):
    def fn(p) -> None:
        _, s = _find_scene(p, scene_id)
        if req.title is not None:
            s.title = req.title.strip()[:50] or s.title
        if req.description is not None:
            s.description = req.description[:500]
        if req.prompt is not None:
            s.prompt = req.prompt[:8000]
        if req.keyframe_prompt is not None:
            s.keyframe_prompt = req.keyframe_prompt[:2000]
        if req.narration is not None:
            s.narration = req.narration[:5000]
        if req.gen_mode is not None:
            s.gen_mode = req.gen_mode
        if req.duration is not None:
            # 唯一钳制点：按项目所选视频模型档位收敛
            s.duration = clamp_duration(req.duration, p.settings.video_model)
        s.revision += 1

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/scenes")
async def add_scene(project_id: str, req: AddSceneRequest):
    def fn(p) -> None:
        scene = Scene(id=new_scene_id(), title=(req.title or "").strip()[:50] or f"分镜 {len(p.scenes) + 1}")
        scene.duration = clamp_duration(scene.duration, p.settings.video_model)
        at = req.index if req.index is not None else len(p.scenes)
        at = max(0, min(at, len(p.scenes)))
        p.scenes.insert(at, scene)

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}/scenes/{scene_id}")
async def delete_scene(project_id: str, scene_id: str, base_version: int | None = None):
    def fn(p) -> None:
        i, _ = _find_scene(p, scene_id)
        p.scenes.pop(i)

    try:
        return project_store.mutate(project_id, fn, base_version=base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/scenes/reorder")
async def reorder_scenes(project_id: str, req: ReorderRequest):
    def fn(p) -> None:
        by_id = {s.id: s for s in p.scenes}
        if len(req.order) != len(p.scenes) or set(req.order) != set(by_id):
            raise HTTPException(status_code=400, detail="order 必须是全部分镜 id 的一个排列")
        p.scenes = [by_id[sid] for sid in req.order]

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


# ---------------- 素材绑定命令 ----------------


@router.post("/{project_id}/bindings")
async def bind_material(project_id: str, req: BindRequest):
    def fn(p) -> None:
        _, s = _find_scene(p, req.scene_id)
        if not any(m.id == req.material_id for m in p.materials):
            raise HTTPException(status_code=404, detail=f"素材不存在：{req.material_id}")
        b = s.binding
        if req.kind == "image":
            if any(x.material_id == req.material_id for x in b.images):
                return
            b.images.append(ImageBinding(material_id=req.material_id, role=req.role or "reference"))
        elif req.kind == "audio":
            if req.material_id not in b.audios:
                b.audios.append(req.material_id)
        else:
            if req.material_id not in b.videos:
                b.videos.append(req.material_id)
        s.revision += 1
        # 用户手动换绑 = 对应关系的明确决定，清掉该素材的待确认标记
        p.pending_bindings = [x for x in p.pending_bindings if x.material_id != req.material_id]

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}/bindings")
async def unbind_material(project_id: str, req: BindRequest):
    def fn(p) -> None:
        _, s = _find_scene(p, req.scene_id)
        b = s.binding
        if req.kind == "image":
            b.images = [x for x in b.images if x.material_id != req.material_id]
        elif req.kind == "audio":
            b.audios = [x for x in b.audios if x != req.material_id]
        else:
            b.videos = [x for x in b.videos if x != req.material_id]
        s.revision += 1

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


# ---------------- SSE 事件流 ----------------


@router.get("/{project_id}/events")
async def project_events(project_id: str):
    """项目变更事件流：先订阅再取快照，事件按 version 去重，15s 心跳保活。"""
    if project_store.get(project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    queue = bus.subscribe(project_id)
    snapshot = project_store.get(project_id)

    async def stream():
        try:
            base_version = snapshot["version"] if snapshot else 0
            if snapshot:
                yield f"data: {json.dumps({'type': 'snapshot', 'version': base_version, 'project': snapshot}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                # 快照之后的旧版本事件直接跳过（订阅窗口内发生的并发写）
                if isinstance(event.get("version"), int) and event["version"] <= base_version:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") == "project_deleted":
                    break
        finally:
            bus.unsubscribe(project_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"})
