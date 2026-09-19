"""v2 项目存取：唯一写入口 = mutate（乐观锁 + 不变量 + 原子写 + 事件推送）。

与旧 project_service（v1）并存：v2 文件以 schema_version==2 标记，旧服务对其
不可见；本服务遇到 v1 遗留文件按既定决策自动归档到 projects_archive/。
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

from pydantic import ValidationError

from ..config import settings
from ..harness.bus import bus
from ..harness.errors import InvariantViolation, ProjectNotFoundError, VersionConflictError
from ..harness.state import Project, check_invariants

ARCHIVE_DIR = settings.DATA_DIR / "projects_archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_lock(project_id: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _locks[project_id] = lock
        return lock


def _check_id(project_id: str) -> None:
    if ".." in project_id or "/" in project_id or "\\" in project_id or not project_id:
        raise ProjectNotFoundError(f"非法项目 id：{project_id!r}")


def _path(project_id: str):
    _check_id(project_id)
    return settings.PROJECTS_DIR / f"{project_id}.json"


def _archive_v1(project_id: str) -> None:
    try:
        src = settings.PROJECTS_DIR / f"{project_id}.json"
        if src.exists():
            os.replace(src, ARCHIVE_DIR / f"{project_id}.json")
    except OSError:
        pass


def _load(project_id: str) -> Project:
    path = _path(project_id)
    if not path.exists():
        raise ProjectNotFoundError(f"项目不存在：{project_id}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectNotFoundError(f"项目文件损坏：{project_id}（{exc}）") from exc
    if raw.get("schema_version") != 2:
        _archive_v1(project_id)
        raise ProjectNotFoundError(f"v1 项目已归档：{project_id}")
    try:
        return Project.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0].get("msg", "") if exc.errors() else ""
        raise ProjectNotFoundError(f"项目数据不符合 v2 模型：{project_id}（{first}）") from exc


def _persist(p: Project) -> None:
    path = _path(p.id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(p.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get(project_id: str) -> dict | None:
    try:
        return _load(project_id).model_dump()
    except ProjectNotFoundError:
        return None


def list_projects() -> list[dict]:
    items = []
    for f in settings.PROJECTS_DIR.glob("project_*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 2:
                continue
            pipeline = raw.get("pipeline") or {}
            items.append(
                {
                    "id": raw["id"],
                    "name": raw.get("name", "未命名项目"),
                    "aspect_ratio": raw.get("aspect_ratio", "16:9"),
                    "scene_count": len(raw.get("scenes", [])),
                    "pipeline_stage": pipeline.get("stage"),
                    "pipeline_state": pipeline.get("state"),
                    "version": raw.get("version", 0),
                    "updated_at": raw.get("updated_at"),
                }
            )
        except Exception:  # noqa: BLE001 - 单文件损坏不影响列表
            continue
    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return items


def create(name: str, aspect_ratio: str = "16:9") -> dict:
    now = _now()
    p = Project(
        id=f"project_{uuid.uuid4().hex[:8]}",
        name=(name or "").strip()[:80] or "未命名项目",
        aspect_ratio=aspect_ratio,
        version=1,
        created_at=now,
        updated_at=now,
    )
    violations = check_invariants(p)
    if violations:  # 理论上空项目不会触发，防御性保留
        raise InvariantViolation("；".join(violations))
    _persist(p)
    snapshot = p.model_dump()
    bus.publish(snapshot["id"], {"type": "project_created", "project_id": snapshot["id"], "project": snapshot})
    return snapshot


def delete(project_id: str) -> bool:
    _check_id(project_id)
    with _project_lock(project_id):
        path = settings.PROJECTS_DIR / f"{project_id}.json"
        if not path.exists():
            return False
        path.unlink()
    bus.publish(project_id, {"type": "project_deleted", "project_id": project_id})
    return True


def mutate(
    project_id: str,
    fn: Callable[[Project], None],
    base_version: int | None = None,
) -> dict:
    """唯一写入口。

    - 锁内完成 读→（版本校验）→fn→不变量→version+1→原子落盘
    - fn 抛异常则不落盘，磁盘状态保持写入前快照
    - base_version 由外部客户端携带（乐观锁）；内部可信写（pipeline/写回）传 None
    - 成功后向事件总线推送全量快照（SSE 消费者按 version 去重）
    """
    with _project_lock(project_id):
        p = _load(project_id)
        if base_version is not None and p.version != base_version:
            raise VersionConflictError(
                f"版本冲突：客户端基线 {base_version}，服务端已到 {p.version}（已有并发修改，请刷新后重放）",
                current_version=p.version,
            )
        fn(p)
        violations = check_invariants(p)
        if violations:
            raise InvariantViolation("；".join(violations))
        p.version += 1
        p.updated_at = _now()
        _persist(p)
        snapshot = p.model_dump()
    bus.publish(
        project_id,
        {"type": "project_updated", "project_id": project_id, "version": snapshot["version"], "project": snapshot},
    )
    return snapshot
