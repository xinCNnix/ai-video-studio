"""JobManager：进程内异步任务 + data/jobs/{job_id}.json 状态持久化。不引入 Celery/Redis。"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from ..config import settings

ACTIVE_STATUSES = {"queued", "submitted", "generating", "downloading"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._load_existing()

    # ---------- 基础 ----------
    def _path(self, job_id: str):
        return settings.JOBS_DIR / f"{job_id}.json"

    def _persist(self, job: dict) -> None:
        # 原子替换：崩溃不留半截 JSON（旧行为直接 write_text，断电即损坏且被 _load_existing 静默丢弃）
        path = self._path(job["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _load_existing(self) -> None:
        for f in settings.JOBS_DIR.glob("*.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
                # 服务重启时的未完成任务：视频任务带云端任务号（provider_task_id），
                # 云端还在正常生成——保留原状态，由启动钩子 job_recovery 续上轮询，
                # 不能标失败把钱已花的成片扔掉。其余类型（图/TTS 无任务号可续）标失败。
                if job.get("status") in ACTIVE_STATUSES:
                    if job.get("type") == "video" and job.get("provider_task_id"):
                        job["status"] = "generating"
                    else:
                        job["status"] = "failed"
                        job["error"] = "服务重启，任务中断。"
                    self._persist(job)
                self.jobs[job["id"]] = job
            except Exception:
                continue

    def create(self, jtype: str, provider: str, model: str, params: dict) -> dict:
        job_id = f"{jtype}_{uuid.uuid4().hex[:8]}"
        job = {
            "id": job_id,
            "type": jtype,
            "provider": provider,
            "model": model,
            "status": "queued",
            "provider_task_id": None,
            "params": params,
            "output": None,
            "outputs": [],
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.jobs[job_id] = job
        self._persist(job)
        return dict(job)

    def update(self, job_id: str, **fields) -> dict | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        job.update(fields)
        job["updated_at"] = _now()
        self._persist(job)
        return dict(job)

    # ---------- 执行 ----------
    def attach(self, job_id: str, coro) -> None:
        task = asyncio.create_task(self._run(job_id, coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job_id: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败统一落盘
            self.update(job_id, status="failed", error=str(exc)[:500])

    # ---------- 查询 ----------
    def get(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def list(self, jtype: str | None = None, status: str | None = None, limit: int = 30) -> list[dict]:
        jobs = list(self.jobs.values())
        if jtype:
            jobs = [j for j in jobs if j["type"] == jtype]
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return [dict(j) for j in jobs[: max(1, limit)]]

    def delete(self, job_id: str) -> bool:
        if job_id not in self.jobs:
            return False
        self.jobs.pop(job_id, None)
        try:
            self._path(job_id).unlink(missing_ok=True)
        except OSError:
            pass
        return True


job_manager = JobManager()
