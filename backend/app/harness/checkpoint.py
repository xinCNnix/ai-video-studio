"""阶段检查点：pipeline 进度持久化在项目本身（project.pipeline），
trace 账本负责留痕，本模块提供两边的读写助手与断点恢复判定。

崩溃恢复语义：进程重启后 pipeline.state == "running" 的项目由
reap_stale_pipelines()（watchdog）转为 paused，用户或 agent 从
pipeline.stage 指示的阶段重新推进；各阶段实现必须幂等或可安全重入。
"""
from datetime import datetime, timezone


PIPELINE_STAGES = ("intake", "normalize", "plan", "bind", "review", "generate", "assemble")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage_done(project: dict, stage: str, note: str = "", trace=None) -> None:
    """在项目 pipeline 上登记一个已完成阶段（供 mutate 回调内直接操作 dict）。"""
    pipeline = project.setdefault("pipeline", {})
    history = pipeline.setdefault("history", [])
    history.append({"stage": stage, "status": "done", "at": _now(), "note": note[:300]})
    pipeline["history"] = history[-50:]
    pipeline["stage"] = stage
    pipeline["state"] = "idle"
    if trace:
        trace.log("stage_done", stage=stage, note=note[:300])


def stage_failed(project: dict, stage: str, error: str, trace=None) -> None:
    pipeline = project.setdefault("pipeline", {})
    history = pipeline.setdefault("history", [])
    history.append({"stage": stage, "status": "failed", "at": _now(), "note": error[:300]})
    pipeline["history"] = history[-50:]
    pipeline["stage"] = stage
    pipeline["state"] = "failed"
    pipeline["error"] = error[:500]
    if trace:
        trace.log("stage_failed", stage=stage, error=error[:500])


def stage_running(project: dict, stage: str, run_id: str | None = None, trace=None) -> None:
    pipeline = project.setdefault("pipeline", {})
    pipeline["stage"] = stage
    pipeline["state"] = "running"
    pipeline["run_id"] = run_id
    pipeline.setdefault("history", []).append(
        {"stage": stage, "status": "running", "at": _now(), "run_id": run_id}
    )
    pipeline["history"] = pipeline["history"][-50:]
    if trace:
        trace.log("stage_start", stage=stage, run_id=run_id)


def resume_point(project: dict) -> dict:
    """断点恢复信息：下一步该推进哪个阶段、卡在什么输入上。"""
    pipeline = project.get("pipeline") or {}
    return {
        "stage": pipeline.get("stage"),
        "state": pipeline.get("state"),
        "awaiting": pipeline.get("awaiting"),
        "run_id": pipeline.get("run_id"),
        "error": pipeline.get("error"),
    }
