"""项目命令式 API（/api/projects，v2 唯一实现）。

设计要点：
- 一切修改走 project_store.mutate + base_version 乐观锁（409 = 有并发写，刷新重放）
- 字段级 PATCH，废除全量 scenes PUT（v1 已随重构移除）
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
from ..services import project_store, prompt_timing

router = APIRouter(prefix="/api/projects", tags=["projects"])


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
    gen_mode: Literal["i2v", "t2v", "first_last"] | None = None


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
    role: Literal["reference", "first_frame", "last_frame"] | None = None


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
            new_duration = clamp_duration(req.duration, p.settings.video_model)
            if new_duration != s.duration and s.prompt:
                # 时长修正必须同步 Prompt 时间轴：Action Timeline/总时长引用停在旧时长
                # 上，生成提交的就是自相矛盾的指令。用户显式提交了 prompt 时以提交值
                # 为基准缩放（基准时长 = 修改前的分镜时长）
                s.prompt, _ = prompt_timing.rescale_prompt_timing(s.prompt, s.duration, new_duration)
            s.duration = new_duration
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


# ---------------- 素材登记（不触发流水线） ----------------


class AddMaterialsRequest(BaseModel):
    materials: list[dict]  # [{url, kind?, filename?, text?}]


@router.post("/{project_id}/materials")
async def add_materials(project_id: str, req: AddMaterialsRequest):
    """登记素材入池（幂等：同 URL 去重）。音频时长探测由服务端补齐。"""
    from ..harness.state import Material, new_material_id
    from ..services.pipeline.stages import _infer_kind

    def fn(p) -> None:
        from ..services import material_match

        existing = {m.url for m in p.materials if m.url}
        for item in req.materials[:50]:
            url = str(item.get("url") or "")
            if not url or url in existing:
                continue
            kind = str(item.get("kind") or _infer_kind(url))
            text = (item.get("text") or None)
            filename = str(item.get("filename") or url.rsplit("/", 1)[-1])[:200]
            # 旧格式配音（uuid 命名提不出台词）+ voice 页带来的台词 → 合成语义文件名，
            # 分镜 bind/素材清单按文件名提台词才能对号
            if kind == "audio" and text:
                filename = material_match.synthesize_audio_filename(url, str(text))[:200]
            p.materials.append(Material(
                id=new_material_id(),
                kind=kind,
                url=url,
                filename=filename,
                text=text,
                duration=(float(item["duration"]) if item.get("duration") else None),
            ))
            existing.add(url)

    try:
        snap = project_store.mutate(project_id, fn)
    except (ProjectNotFoundError, InvariantViolation) as exc:
        raise _http(exc) from exc
    # 异步补齐音频时长
    import asyncio

    from ..services.pipeline import stages as pipeline_stages

    asyncio.create_task(pipeline_stages.probe_audio_durations(project_id))
    return {"ok": True, "materials": snap.get("materials")}


@router.post("/{project_id}/materials/reorder")
async def reorder_materials(project_id: str, req: ReorderRequest):
    """素材池重排（交换上传顺序）。storyboard 的 <Image N>/<Audio N> 全局编号按
    上传顺序对齐——传错顺序时交换池内素材即可修正编号；已落到分镜上的绑定是
    material_id 引用，不受影响（需要换绑时在分镜上手动调整）。"""
    def fn(p) -> None:
        by_id = {m.id: m for m in p.materials}
        if len(req.order) != len(p.materials) or set(req.order) != set(by_id):
            raise HTTPException(status_code=400, detail="order 必须是全部素材 id 的一个排列")
        p.materials = [by_id[mid] for mid in req.order]

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}/materials/{material_id}")
async def delete_material(project_id: str, material_id: str, base_version: int | None = None):
    """删除单个素材：出池 + 解除全部分镜对它的绑定与待确认标记（不变量禁止悬空引用）。
    底层文件不删——可能是任务产物，其他项目/历史仍在用。"""
    def fn(p) -> None:
        if not any(m.id == material_id for m in p.materials):
            raise HTTPException(status_code=404, detail=f"素材不存在：{material_id}")
        p.materials = [m for m in p.materials if m.id != material_id]
        p.pending_bindings = [x for x in p.pending_bindings if x.material_id != material_id]
        for s in p.scenes:
            b = s.binding
            before = (len(b.images), len(b.audios), len(b.videos))
            b.images = [x for x in b.images if x.material_id != material_id]
            b.audios = [x for x in b.audios if x != material_id]
            b.videos = [x for x in b.videos if x != material_id]
            if (len(b.images), len(b.audios), len(b.videos)) != before:
                s.revision += 1

    try:
        return project_store.mutate(project_id, fn, base_version=base_version)
    except HTTPException:
        raise
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


# ---------------- 清空重投（保留项目壳，内容全清） ----------------


class ResetRequest(BaseModel):
    base_version: int | None = None


@router.post("/{project_id}/reset")
async def reset_project_content(project_id: str, req: ResetRequest):
    """清空重投：素材池/分镜/剧本/角色卡/待确认/成片引用全部清空，流水线回 empty。
    保留：项目名、画幅、模型与门禁设置。任务进行中拒绝（防写竞争）。"""
    from ..harness.watchdog import watchdog

    active = watchdog.current(project_id)
    if active and not active.get("stale"):
        raise HTTPException(status_code=409, detail="项目有任务在进行中，请等它结束或先取消，再清空重投")

    def fn(p) -> None:
        p.materials = []
        p.characters = []
        p.scenes = []
        p.screenplay = None
        p.pending_bindings = []
        p.final_video = None
        p.pipeline.stage = "empty"
        p.pipeline.state = "idle"
        p.pipeline.awaiting = None
        p.pipeline.error = None
        p.pipeline.run_id = None
        p.pipeline.history = []

    try:
        return project_store.mutate(project_id, fn, base_version=req.base_version)
    except (ProjectNotFoundError, VersionConflictError, InvariantViolation) as exc:
        raise _http(exc) from exc


# ---------------- 手动生成（与 agent/pipeline 共用 generation_service） ----------------


class GenerateRequest(BaseModel):
    kind: Literal["keyframe", "voice", "video"]
    prompt: str | None = None  # keyframe 描述覆盖
    text: str | None = None    # voice 文本覆盖
    force: bool = False
    use_uploaded: bool = False
    reference_scene_id: str | None = None


@router.post("/{project_id}/scenes/{scene_id}/generate")
async def generate_for_scene(project_id: str, scene_id: str, req: GenerateRequest):
    """单镜手动生成：关键帧/配音后台执行（经 SSE 推送状态），视频提交即返回。"""
    import asyncio

    from ..services import generation_service

    project = project_store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    scene = next((s for s in project["scenes"] if s["id"] == scene_id), None)
    if scene is None:
        raise HTTPException(status_code=404, detail=f"分镜不存在：{scene_id}")

    if req.kind == "video":
        result = await generation_service.submit_scene_video(project_id, scene_id, force=req.force, manual=True)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "提交失败"))
        return result

    async def _run() -> None:
        try:
            if req.kind == "keyframe":
                await generation_service.generate_keyframe(
                    project_id, scene_id,
                    prompt_override=req.prompt, reference_scene_id=req.reference_scene_id,
                )
            else:
                await generation_service.generate_voice(
                    project_id, scene_id,
                    text_override=req.text, force=req.force, use_uploaded=req.use_uploaded,
                )
        except Exception as exc:  # noqa: BLE001 - 后台失败写进分镜状态，由 SSE 推给前端
            def _fail(p) -> None:
                s = next((x for x in p.scenes if x.id == scene_id), None)
                if s:
                    s.status = "failed"
                    s.error = f"{req.kind} 生成异常：{str(exc)[:200]}"
            try:
                project_store.mutate(project_id, _fail)
            except Exception:  # noqa: BLE001
                pass

    asyncio.create_task(_run())
    return {"ok": True, "queued": req.kind, "scene_id": scene_id}


@router.post("/{project_id}/export")
async def export_project_v2(project_id: str):
    from ..services import export_service

    try:
        return await export_service.export_project_v2(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------- 流水线 ----------------


class PipelineStartRequest(BaseModel):
    script_text: str | None = None
    materials: list[dict] | None = None  # [{url, kind?, filename?, text?}]
    settings: dict | None = None  # 顺带更新 video_model/voice_model 等


class PipelineAdvanceRequest(BaseModel):
    confirmed: bool = False  # review/bindings 门禁确认


@router.post("/{project_id}/pipeline/start")
async def pipeline_start(project_id: str, req: PipelineStartRequest):
    from ..harness.errors import ProjectNotFoundError, RunBusyError
    from ..services import pipeline as pipeline_svc

    try:
        if req.settings:
            def fn(p) -> None:
                merged = p.settings.model_dump()
                merged.update(req.settings or {})
                p.settings = type(p.settings).model_validate(merged)
            project_store.mutate(project_id, fn)
        return pipeline_svc.start_pipeline(
            project_id,
            payload={"script_text": req.script_text, "materials": req.materials or []},
        )
    except ProjectNotFoundError as exc:
        raise _http(exc) from exc
    except RunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvariantViolation as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/pipeline/advance")
async def pipeline_advance(project_id: str, req: PipelineAdvanceRequest):
    from ..harness.errors import ProjectNotFoundError, RunBusyError
    from ..services import pipeline as pipeline_svc

    try:
        return pipeline_svc.advance_pipeline(project_id, confirmed=req.confirmed)
    except ProjectNotFoundError as exc:
        raise _http(exc) from exc
    except RunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{project_id}/bindings/confirm")
async def bindings_confirm(project_id: str, req: PipelineAdvanceRequest):
    """确认全部待定素材绑定（保留现状），随后自动续跑流水线。"""
    from ..harness.errors import ProjectNotFoundError, RunBusyError
    from ..services import pipeline as pipeline_svc

    try:
        project_store.mutate(project_id, lambda p: setattr(p, "pending_bindings", []))
        return pipeline_svc.advance_pipeline(project_id, confirmed=req.confirmed)
    except ProjectNotFoundError as exc:
        raise _http(exc) from exc
    except RunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{project_id}/runs")
async def list_project_runs(project_id: str):
    from ..harness import trace as trace_mod

    return {"runs": trace_mod.list_runs(project_id=project_id, limit=10)}


@router.get("/{project_id}/runs/{run_id}/trace")
async def get_run_trace(project_id: str, run_id: str):
    from ..harness import trace as trace_mod

    events = trace_mod.read_trace(run_id, limit=300)
    if not events or events[0].get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"run_id": run_id, "events": events}


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
