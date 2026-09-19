"""Run 级 trace：事件溯源账本，data/runs/{run_id}.jsonl。

每个 agent run / pipeline run 一个账本文件，逐事件追加写（无缓冲，崩溃不丢）。
任何事故可回放：每次 LLM 请求/响应、校验失败与修复、工具调用、阶段转换全在案。
字符串截断到 _MAX_STR，防止一次失控输出把磁盘写爆。
"""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

RUNS_DIR = settings.DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

_MAX_STR = 4000
_MAX_LIST = 50


def new_run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trunc(value):
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, list):
        return [_trunc(v) for v in value[:_MAX_LIST]]
    if isinstance(value, dict):
        return {str(k)[:80]: _trunc(v) for k, v in list(value.items())[:_MAX_LIST]}
    return value


class RunTrace:
    """单 run 账本。线程安全：pipeline 协程与 threadpool 工具回调都可能写。"""

    def __init__(self, run_id: str, project_id: str, kind: str, meta: dict | None = None):
        self.run_id = run_id
        self.project_id = project_id
        self.kind = kind
        self._path = RUNS_DIR / f"{run_id}.jsonl"
        self._seq = 0
        self._lock = threading.Lock()
        self.counts = {"llm_calls": 0, "repairs": 0, "validation_errors": 0, "retries": 0}
        self.log("run_meta", project_id=project_id, kind=kind, **(meta or {}))

    def log(self, type: str, **data) -> None:  # noqa: A002 - 事件类型名
        event = {"seq": 0, "t": _now(), "type": type}
        event.update(_trunc(data))
        with self._lock:
            self._seq += 1
            event["seq"] = self._seq
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def count(self, key: str) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def end(self, status: str, **data) -> None:
        self.log("run_end", status=status, counts=dict(self.counts), **data)

    def tail(self, limit: int = 50) -> list[dict]:
        return read_trace(self.run_id, limit=limit)


def read_trace(run_id: str, limit: int = 500) -> list[dict]:
    """读取账本尾部事件（回放/排障用）。非法 run_id 直接拒绝，防路径穿越。"""
    if ".." in run_id or "/" in run_id or "\\" in run_id:
        return []
    path = RUNS_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events[-limit:]


def list_runs(project_id: str | None = None, limit: int = 20) -> list[dict]:
    """按开始时间倒序列出 run 摘要（首行 meta + 尾行 end，不全量读）。"""
    items: list[dict] = []
    for path in RUNS_DIR.glob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
                if not first:
                    continue
                meta = json.loads(first)
                if project_id and meta.get("project_id") != project_id:
                    continue
                # 尾行：从文件尾倒退找最后一个可解析行
                f.seek(0, 2)
                tail_bytes = min(path.stat().st_size, 65536)
                f.seek(max(0, path.stat().st_size - tail_bytes))
                last = {}
                for line in f.read().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                items.append(
                    {
                        "run_id": meta.get("run_id") or path.stem,
                        "project_id": meta.get("project_id"),
                        "kind": meta.get("kind"),
                        "goal": (meta.get("goal") or "")[:120],
                        "started_at": meta.get("t"),
                        "status": last.get("status") if last.get("type") == "run_end" else "running",
                        "counts": last.get("counts") or {},
                    }
                )
        except (OSError, json.JSONDecodeError):
            continue
    items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return items[:limit]
