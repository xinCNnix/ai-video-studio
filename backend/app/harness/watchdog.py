"""看门狗：项目级 single-flight 运行锁 + 心跳。

- 同一项目同时只允许一个活跃 run（agent run / pipeline），杜绝并发写项目
- 心跳超时（STALE_SECONDS）判死：持锁 run 崩溃不释放也不会永久卡住项目
- reap_stale_pipelines()：启动/巡检时把 state=="running" 但无活跃 run 的项目
  转为 paused（服务重启等造成的悬空 running），供用户从检查点续跑
"""
import threading
import time


STALE_SECONDS = 300


class Watchdog:
    def __init__(self, stale_seconds: float = STALE_SECONDS):
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stale_seconds = stale_seconds

    def try_acquire(self, project_id: str, run_id: str, kind: str) -> bool:
        """尝试持有项目运行锁；已有未过期且不同 run_id 的活跃 run 时拒绝。"""
        now = time.time()
        with self._lock:
            cur = self._runs.get(project_id)
            if (
                cur
                and cur["run_id"] != run_id
                and now - cur["heartbeat"] < self._stale_seconds
            ):
                return False
            self._runs[project_id] = {
                "run_id": run_id,
                "kind": kind,
                "started_at": now,
                "heartbeat": now,
            }
            return True

    def heartbeat(self, project_id: str) -> None:
        with self._lock:
            cur = self._runs.get(project_id)
            if cur:
                cur["heartbeat"] = time.time()

    def release(self, project_id: str, run_id: str) -> None:
        with self._lock:
            cur = self._runs.get(project_id)
            if cur and cur["run_id"] == run_id:
                self._runs.pop(project_id, None)

    def current(self, project_id: str) -> dict | None:
        """当前持锁 run（附 stale 标记）；过期视为无主。"""
        with self._lock:
            cur = self._runs.get(project_id)
            if not cur:
                return None
            out = dict(cur)
            out["stale"] = time.time() - cur["heartbeat"] >= self._stale_seconds
            return out

    def reap_stale(self) -> list[dict]:
        """回收过期运行锁，返回被回收的 run 列表（调用方可据此标记项目失败）。"""
        now = time.time()
        reaped: list[dict] = []
        with self._lock:
            for pid in list(self._runs.keys()):
                cur = self._runs[pid]
                if now - cur["heartbeat"] >= self._stale_seconds:
                    reaped.append({"project_id": pid, **cur})
                    self._runs.pop(pid, None)
        return reaped


watchdog = Watchdog()


def reap_stale_pipelines() -> list[str]:
    """启动钩子：把悬空 running 的项目 pipeline 转为 paused。

    返回被修正的 project_id 列表。服务重启后 async 任务全部消失，
    但项目 JSON 里 state 还是 "running"——不修正的话项目会被 single-flight
    永久锁死或让 UI 永远显示"生成中"。
    """
    from ..services import project_store

    fixed: list[str] = []
    for summary in project_store.list_projects():
        pid = summary["id"]
        try:
            project = project_store.get(pid)
        except Exception:  # noqa: BLE001 - 单个项目损坏不阻断巡检
            continue
        if not project:
            continue
        pipeline = project.get("pipeline") or {}
        if pipeline.get("state") == "running":
            run = watchdog.current(pid)
            if run and not run.get("stale"):
                continue
            project_store.mutate(
                pid,
                lambda p: _pause_dangling(p),
            )
            fixed.append(pid)
    return fixed


def _pause_dangling(p) -> None:
    pipeline = p.pipeline
    pipeline.state = "paused"
    pipeline.awaiting = "resume"
    pipeline.error = "服务重启或运行中断，可从检查点续跑"
    p.updated_at = _utc_now()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
