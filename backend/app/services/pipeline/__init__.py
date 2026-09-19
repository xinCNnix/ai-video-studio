"""pipeline 状态机：intake → normalize → plan → bind → review(门禁) → generate → assemble。

执行模型：
- start（带新剧本/素材）重置周期从 intake 跑；advance 从上一个未完成阶段续跑
- single-flight：watchdog 运行锁保证同项目同时只有一个 pipeline/agent run
- NeedsUserInput → pipeline.state=paused + awaiting 标记，任务干净退出，等 advance
- FatalError → stage_failed + 完整产物保留，trace 可回放
"""
import asyncio

from ...harness import checkpoint, trace as trace_mod
from ...harness.errors import (
    FatalError,
    NeedsUserInputError,
    ProjectNotFoundError,
    RetryableError,
    RunBusyError,
)
from ...harness.watchdog import watchdog
from .. import export_service, project_store, reconcile
from . import stages
from .llm_client import LLMEnvelope

STAGE_ORDER = ["intake", "normalize", "plan", "bind", "review", "generate", "assemble"]


def _next_stage(project: dict) -> str:
    """从 history 里找当前周期第一个未完成的阶段。"""
    pipeline = project.get("pipeline") or {}
    done = {h.get("stage") for h in (pipeline.get("history") or []) if h.get("status") == "done"}
    for stage in STAGE_ORDER:
        if stage not in done:
            return stage
    return "done"


def _envelope(project_id: str, credential_id: str | None, model: str | None, trace) -> LLMEnvelope:
    from ...providers.registry import resolve_llm_call

    llm, resolved_model = resolve_llm_call(credential_id, provider_hint="minimax", model=model)
    return LLMEnvelope(llm, resolved_model, trace=trace)


async def _execute(project_id: str, run_id: str, trace, payload: dict | None,
                   credential_id: str | None, model: str | None,
                   cancel_check=None, confirmed: bool = False) -> dict:
    """执行到下一个暂停点/终点。payload 仅 start（intake）时携带；confirmed 为用户已确认 review 门禁。"""
    if not watchdog.try_acquire(project_id, run_id, "pipeline"):
        active = watchdog.current(project_id)
        raise RunBusyError("该项目已有进行中的任务", active_run=active)
    summary: dict = {"run_id": run_id, "project_id": project_id, "stages": []}
    try:
        while True:
            if cancel_check and cancel_check():
                summary["cancelled"] = True
                break
            watchdog.heartbeat(project_id)
            project = project_store.get(project_id)
            if not project:
                raise ProjectNotFoundError(f"项目不存在：{project_id}")
            stage = _next_stage(project)
            if stage == "done":
                def fn_done(p) -> None:
                    pipeline = p.pipeline
                    pipeline.state = "done"
                    pipeline.awaiting = None
                    pipeline.error = None
                    pipeline.history.append({"stage": "cycle", "status": "done", "at": _now_iso(), "note": "全部阶段完成"})
                    pipeline.history = pipeline.history[-50:]
                project_store.mutate(project_id, fn_done)
                summary["finished"] = True
                break
            if stage == "intake" and payload is None:
                # advance 续跑但没有新投放物：intake 可能本来就没跑过（历史周期重置）——
                # 没有 payload 的 intake 只对已有素材的项目合法，否则向用户要剧本
                has_script = any(
                    (m.get("kind") in ("script", "doc")) and (m.get("text") or "").strip()
                    for m in project.get("materials", [])
                )
                if not has_script:
                    _pause(project_id, stage, "input",
                           "项目还没有剧本：请投放剧本文本或上传 .md/.txt 剧本")
                    summary["awaiting"] = "input"
                    break
                _mark_done(project_id, "intake", "跳过（无新投放，素材池已有剧本）", trace)
                continue

            _mark_running(project_id, stage, run_id, trace)
            try:
                if stage == "intake":
                    result = stages.run_intake(project_id, payload or {}, trace=trace)
                    await stages.probe_audio_durations(project_id)
                elif stage == "normalize":
                    result = await stages.run_normalize(project_id, _envelope(project_id, credential_id, model, trace), trace=trace)
                elif stage == "plan":
                    result = await stages.run_plan(project_id, _envelope(project_id, credential_id, model, trace), trace=trace)
                elif stage == "bind":
                    result = await stages.run_bind(project_id, _envelope(project_id, credential_id, model, trace), trace=trace)
                elif stage == "review":
                    result = _review_gate(project, pass_through=confirmed)
                elif stage == "generate":
                    result = await reconcile.run_generate_stage(project_id, trace=trace, cancel_check=cancel_check)
                elif stage == "assemble":
                    result = await export_service.export_project_v2(project_id)
                else:  # pragma: no cover
                    raise FatalError(f"未知阶段：{stage}")
            except NeedsUserInputError as exc:
                _pause(project_id, stage, _awaiting_for(stage), exc.question, trace=trace)
                summary["stages"].append({"stage": stage, "paused": exc.question})
                summary["awaiting"] = _awaiting_for(stage)
                trace.log("stage_paused", stage=stage, question=exc.question)
                break
            except (FatalError, RetryableError) as exc:
                # LLM/依赖连续失败（Retryable）与不可修复错误（Fatal）同样干净中止：
                # 完成产物保留、阶段标记 failed、trace 可回放——绝不允许 pipeline 卡死在 running
                checkpoint_stage_failed(project_id, stage, str(exc)[:500], trace)
                summary["stages"].append({"stage": stage, "failed": str(exc)[:300]})
                summary["failed"] = str(exc)[:300]
                trace.end("failed", at_stage=stage)
                return summary
            except Exception as exc:  # noqa: BLE001 - 顶层兜底：未知异常也必须落到 stage_failed
                checkpoint_stage_failed(project_id, stage, f"未预期异常：{exc}"[:500], trace)
                summary["stages"].append({"stage": stage, "failed": f"未预期异常：{exc}"[:300]})
                summary["failed"] = str(exc)[:300]
                trace.end("failed", at_stage=stage)
                return summary
            note = "; ".join(f"{k}={v}" for k, v in (result or {}).items() if isinstance(v, (int, str, bool)) and k != "ok")[:300]
            _mark_done(project_id, stage, note, trace)
            summary["stages"].append({"stage": stage, "ok": True, "detail": (result or {})})
        trace.end("paused" if summary.get("awaiting") else ("done" if summary.get("finished") else "stopped"))
        return summary
    finally:
        watchdog.release(project_id, run_id)


def _awaiting_for(stage: str) -> str:
    if stage == "bind":
        return "bindings"
    if stage == "review":
        return "review"
    return "input"


def _review_gate(project: dict, pass_through: bool = False) -> dict:
    """review 门禁：默认暂停等用户确认；auto_generate 或用户已确认（pass_through）时直通。"""
    pending = project.get("pending_bindings") or []
    settings = project.get("settings") or {}
    scenes = project.get("scenes") or []
    if settings.get("auto_generate") or pass_through:
        return {"ok": True, "note": "已确认，开始生成", "scenes": len(scenes), "pending": len(pending)}
    if not scenes:
        raise NeedsUserInputError("项目还没有分镜，请先投放剧本完成拆分")
    if pending:
        raise NeedsUserInputError(
            f"有 {len(pending)} 项素材归属待确认（参考图/配音与分镜的对应关系不确定），请逐项确认后再开始生成"
        )
    raise NeedsUserInputError(
        f"分镜与素材分配已就绪（共 {len(scenes)} 镜），请确认后开始生成（会消耗视频/生图/配音额度）"
    )


# ---- pipeline 状态写回（经 project_store，变更即时 SSE 推送） ----


def _set_pipeline(project_id: str, **fields) -> None:
    def fn(p) -> None:
        for k, v in fields.items():
            setattr(p.pipeline, k, v)
    project_store.mutate(project_id, fn)


def _mark_running(project_id: str, stage: str, run_id: str, trace) -> None:
    def fn(p) -> None:
        checkpoint.stage_running(p.pipeline, stage, run_id)
    project_store.mutate(project_id, fn)
    trace.log("stage_start", stage=stage, run_id=run_id)


def _mark_done(project_id: str, stage: str, note: str, trace) -> None:
    def fn(p) -> None:
        checkpoint.stage_done(p.pipeline, stage, note)
    project_store.mutate(project_id, fn)


def checkpoint_stage_failed(project_id: str, stage: str, error: str, trace) -> None:
    def fn(p) -> None:
        checkpoint.stage_failed(p.pipeline, stage, error)
    project_store.mutate(project_id, fn)


def _pause(project_id: str, stage: str, awaiting: str, question: str, trace=None) -> None:
    def fn(p) -> None:
        pipeline = p.pipeline
        pipeline.state = "paused"
        pipeline.awaiting = awaiting
        pipeline.error = None
        pipeline.history.append({"stage": stage, "status": "paused", "at": _now_iso(), "note": question[:300]})
        pipeline.history = pipeline.history[-50:]
    project_store.mutate(project_id, fn)
    if trace:
        trace.log("stage_paused", stage=stage, awaiting=awaiting)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---- 对外入口（后台任务执行，立即返回 run_id） ----


def start_pipeline(project_id: str, payload: dict | None = None,
                   credential_id: str | None = None, model: str | None = None) -> dict:
    """投放剧本/素材并启动流水线（新周期：重置阶段历史，从 intake 跑）。"""
    project = project_store.get(project_id)
    if not project:
        raise ProjectNotFoundError(f"项目不存在：{project_id}")
    run_id = trace_mod.new_run_id("pipe")
    trace = trace_mod.RunTrace(run_id, project_id, "pipeline",
                               meta={"goal": ((payload or {}).get("script_text") or "")[:200]})

    # 运行锁在改状态前获取：冲突时立即报错，且不会给项目留下悬空的 running 状态
    if not watchdog.try_acquire(project_id, run_id, "pipeline"):
        active = watchdog.current(project_id)
        raise RunBusyError("该项目已有进行中的任务", active_run=active)

    def fn(p) -> None:
        p.pipeline.history = []
        p.pipeline.stage = "intake"
        p.pipeline.state = "running"
        p.pipeline.awaiting = None
        p.pipeline.error = None
        p.pipeline.run_id = run_id

    project_store.mutate(project_id, fn)
    task = asyncio.create_task(_execute(project_id, run_id, trace, payload, credential_id, model))
    task.add_done_callback(_log_task_error)
    return {"ok": True, "run_id": run_id}


def advance_pipeline(project_id: str, credential_id: str | None = None,
                     model: str | None = None, confirmed: bool = False) -> dict:
    """从暂停点/检查点续跑（review 确认、bindings 确认后、崩溃恢复）。"""
    project = project_store.get(project_id)
    if not project:
        raise ProjectNotFoundError(f"项目不存在：{project_id}")
    pipeline = project.get("pipeline") or {}
    # single-flight 无条件互斥：agent run 或其他 pipeline 占锁时拒绝续跑
    active = watchdog.current(project_id)
    if active and not active.get("stale"):
        raise RunBusyError("该项目已有进行中的任务，请等待完成或先取消", active_run=active)
    # review 确认 = 用户已点头：清空 pending（保留绑定现状），继续 generate
    if confirmed and pipeline.get("awaiting") in ("review", "bindings"):
        def fn(p) -> None:
            p.pending_bindings = []
        project_store.mutate(project_id, fn)
    run_id = trace_mod.new_run_id("pipe")
    trace = trace_mod.RunTrace(run_id, project_id, "pipeline",
                               meta={"note": f"resume from {pipeline.get('stage')}"})
    if not watchdog.try_acquire(project_id, run_id, "pipeline"):
        active = watchdog.current(project_id)
        raise RunBusyError("该项目已有进行中的任务", active_run=active)
    task = asyncio.create_task(
        _execute(project_id, run_id, trace, None, credential_id, model, confirmed=confirmed)
    )
    task.add_done_callback(_log_task_error)
    return {"ok": True, "run_id": run_id}


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[pipeline] 后台任务异常逃逸：{str(exc)[:200]}")
