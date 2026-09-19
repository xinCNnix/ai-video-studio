"""AI 图片生成 API。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..jobs.manager import job_manager
from ..providers import registry
from ..providers.base import ProviderError
from ..services import image_service

router = APIRouter(prefix="/api/image", tags=["image"])


class ImageRequest(BaseModel):
    provider: str = "openai"
    model: str = "gpt-image-1"
    credential_id: str | None = None
    prompt: str = Field(min_length=1)
    aspect_ratio: str = "1:1"
    size: str | None = None
    n: int = 1
    references: list[str] = []


@router.post("/generate")
async def generate(req: ImageRequest):
    params = {
        "prompt": req.prompt[:200],
        "aspect_ratio": req.aspect_ratio,
        "model": req.model,
        "references": len(req.references),
    }
    # 记录实际将使用的凭据（与 run_image_job 内 get_image 的解析一致），前端历史据此显示用的哪把 Key
    cred_label = registry.resolve_credential_label(req.credential_id, req.provider)
    if cred_label:
        params["credential"] = cred_label
    job = job_manager.create("image", req.provider, req.model, params)
    job_manager.attach(
        job["id"],
        image_service.run_image_job(
            job_id=job["id"],
            provider=req.provider,
            model=req.model,
            credential_id=req.credential_id,
            prompt=req.prompt,
            size=req.size,
            n=max(1, min(4, req.n)),
            references=req.references,
        ),
    )
    return {"job_id": job["id"], "job": job}


@router.get("/models")
async def models():
    from ..config import settings

    return {"models": settings.IMAGE_MODELS}
