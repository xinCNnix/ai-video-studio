"""Studio 项目 CRUD API。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str = "未命名项目"
    aspect_ratio: str = "16:9"


class SaveProjectRequest(BaseModel):
    name: str | None = None
    aspect_ratio: str | None = None
    scenes: list[dict] = []


@router.get("")
async def list_projects():
    return {"projects": project_service.list_projects()}


@router.post("")
async def create_project(req: CreateProjectRequest):
    return project_service.create_project(req.name, req.aspect_ratio)


@router.get("/{project_id}")
async def get_project(project_id: str):
    project = project_service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.put("/{project_id}")
async def save_project(project_id: str, req: SaveProjectRequest):
    existing = project_service.get_project(project_id)
    data = existing or {}
    if req.name is not None:
        data["name"] = req.name
    if req.aspect_ratio is not None:
        data["aspect_ratio"] = req.aspect_ratio
    data["scenes"] = req.scenes
    saved = project_service.save_project(project_id, data)
    if not saved:
        raise HTTPException(status_code=404, detail="项目不存在")
    return saved


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    if not project_service.delete_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"ok": True}


@router.post("/{project_id}/export")
async def export_project(project_id: str):
    """按分镜顺序拼接成片（自动混入旁白），返回成片地址。"""
    from ..services import export_service

    try:
        return await export_service.export_project(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
