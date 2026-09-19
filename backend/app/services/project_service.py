"""Studio 项目服务：data/projects/{project_id}.json。"""
import json
import os
import uuid
from datetime import datetime, timezone

from ..config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_projects() -> list[dict]:
    items = []
    for f in settings.PROJECTS_DIR.glob("project_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # v2 项目由 project_store 管理，旧服务不可见（桥接期隔离）
            if data.get("schema_version") == 2:
                continue
            items.append(
                {
                    "id": data["id"],
                    "name": data.get("name", "未命名项目"),
                    "aspect_ratio": data.get("aspect_ratio", "16:9"),
                    "scene_count": len(data.get("scenes", [])),
                    "updated_at": data.get("updated_at"),
                }
            )
        except Exception:
            continue
    items.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return items


def get_project(project_id: str) -> dict | None:
    if ".." in project_id or "/" in project_id or "\\" in project_id:
        return None
    path = settings.PROJECTS_DIR / f"{project_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("schema_version") == 2:
        return None
    return data


def create_project(name: str, aspect_ratio: str = "16:9") -> dict:
    project = {
        "id": f"project_{uuid.uuid4().hex[:8]}",
        "name": name or "未命名项目",
        "aspect_ratio": aspect_ratio,
        "scenes": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _persist(project)
    return project


def save_project(project_id: str, data: dict) -> dict | None:
    path = settings.PROJECTS_DIR / f"{project_id}.json"
    # v2 项目严禁被旧接口整包覆盖（upsert 会把 v2 文件改写成 v1 形状）
    if path.exists():
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("schema_version") == 2:
                return None
        except Exception:
            pass
    existing = get_project(project_id)
    data["id"] = project_id
    data["updated_at"] = _now()
    data.setdefault("created_at", (existing or {}).get("created_at", _now()))
    data.setdefault("name", "未命名项目")
    data.setdefault("aspect_ratio", "16:9")
    data.setdefault("scenes", [])
    _persist(data)
    return data


def delete_project(project_id: str) -> bool:
    path = settings.PROJECTS_DIR / f"{project_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _persist(project: dict) -> None:
    if ".." in project["id"] or "/" in project["id"]:
        raise ValueError("invalid project id")
    path = settings.PROJECTS_DIR / f"{project['id']}.json"
    # 先写临时文件再原子替换：前端保存与 Agent 写回并发时不会留下半截 JSON
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
