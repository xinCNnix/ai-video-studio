"""Prompt Agent API。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..providers.base import ProviderError
from ..services import prompt_service

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


class GenerateRequest(BaseModel):
    description: str
    mode: str = "text_to_video"
    target_model: str = "minimax_h3"
    credential_id: str | None = None
    model: str | None = None
    duration: int | None = None
    provider: str | None = None


class RefineRequest(BaseModel):
    action: str
    prompt: str
    description: str = ""
    target_model: str = "minimax_h3"
    credential_id: str | None = None
    model: str | None = None
    provider: str | None = None


class ImproveRequest(BaseModel):
    prompt: str
    target_model: str = "minimax_h3"
    credential_id: str | None = None
    model: str | None = None
    provider: str | None = None


class ImproveImageRequest(BaseModel):
    prompt: str
    image_model: str = ""
    credential_id: str | None = None
    model: str | None = None
    provider: str | None = None


class SavePresetRequest(BaseModel):
    title: str
    content: str


@router.post("/generate")
async def generate(req: GenerateRequest):
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="请输入视频描述")
    try:
        return await prompt_service.generate_prompt(
            description=req.description.strip(),
            mode=req.mode,
            target_model=req.target_model,
            credential_id=req.credential_id,
            model=req.model,
            duration=req.duration,
            provider=req.provider,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/refine")
async def refine(req: RefineRequest):
    try:
        return await prompt_service.refine_prompt(
            action=req.action,
            current_prompt=req.prompt,
            description=req.description,
            target_model=req.target_model,
            credential_id=req.credential_id,
            model=req.model,
            provider=req.provider,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/improve")
async def improve(req: ImproveRequest):
    try:
        prompt = await prompt_service.improve_prompt(
            prompt=req.prompt,
            target_model=req.target_model,
            credential_id=req.credential_id,
            model=req.model,
            provider=req.provider,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prompt": prompt}


@router.post("/image")
async def improve_image(req: ImproveImageRequest):
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="请输入图片描述")
    try:
        prompt = await prompt_service.improve_image_prompt(
            prompt=req.prompt.strip(),
            image_model=req.image_model,
            credential_id=req.credential_id,
            model=req.model,
            provider=req.provider,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prompt": prompt}


@router.get("/presets")
async def presets():
    return {"presets": prompt_service.list_presets()}


@router.get("/presets/{preset_id}")
async def preset_detail(preset_id: str):
    data = prompt_service.get_preset(preset_id)
    if not data:
        raise HTTPException(status_code=404, detail="预设不存在")
    return data


@router.post("/presets")
async def save_preset(req: SavePresetRequest):
    return prompt_service.save_preset(req.title, req.content)
