"""AI 视频生成 API —— 统一视频接口 POST /api/video/generate。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..jobs.manager import job_manager
from ..providers import registry
from ..services import video_service

router = APIRouter(prefix="/api/video", tags=["video"])

MODE_ALIASES = {
    "text": "text", "text_to_video": "text",
    "image": "image", "image_to_video": "image",
    "reference": "reference", "reference_video": "reference",
    "first_last": "first_last", "first_last_frame": "first_last", "flf": "first_last",
}


class VideoRequest(BaseModel):
    provider: str = "minimax"
    model: str = "MiniMax-H3"
    credential_id: str | None = None

    mode: str = "text"

    prompt: str = ""

    duration: int | None = 5

    aspect_ratio: str = "16:9"

    resolution: str | None = None

    images: list[str] = []

    last_images: list[str] = []

    videos: list[str] = []

    audios: list[str] = []

    options: dict | None = None


@router.post("/generate")
async def generate(req: VideoRequest):
    mode = MODE_ALIASES.get(req.mode, "text")
    if mode == "text" and not req.prompt.strip():
        raise HTTPException(status_code=400, detail="请输入视频 Prompt")
    if mode == "first_last":
        if not req.images:
            raise HTTPException(status_code=400, detail="首尾帧模式需要上传首帧图片")
        if not req.last_images:
            raise HTTPException(status_code=400, detail="首尾帧模式需要上传尾帧图片")
    elif mode != "text" and not (req.images or req.videos or req.audios):
        raise HTTPException(status_code=400, detail="该模式需要提供参考素材（图片 / 视频 / 音频）")
    # AutoDL 工作流按契约校验（缺参考图 / 模式不支持 / Prompt 必填提前 400，而不是等云端报错）
    contract_error = video_service.autodl_contract_error(
        req.model, mode, req.images, req.videos, req.audios, prompt=req.prompt
    )
    if contract_error:
        raise HTTPException(status_code=400, detail=contract_error)
    # H3 参考标签悬空拦截（编号必须与本次传入素材一一对应，否则模型自行编造内容）
    tag_error = video_service.reference_tag_error(
        req.model, req.prompt,
        req.images + (req.last_images if mode == "first_last" else []),
        req.videos, req.audios,
    )
    if tag_error:
        raise HTTPException(status_code=400, detail=tag_error)
    # 时长按模型档位收敛（目录中查不到该模型时退回全局 1-15 兜底）
    model_entry = next((m for m in settings.VIDEO_MODELS if m["id"] == req.model), None)
    if model_entry and model_entry.get("durations"):
        dmin, dmax = min(model_entry["durations"]), max(model_entry["durations"])
    else:
        dmin, dmax = 1, 15
    duration = min(max(int(req.duration or 5), dmin), dmax)
    params = {
        "mode": mode,
        "prompt": req.prompt[:300],
        "duration": duration,
        "aspect_ratio": req.aspect_ratio,
        "model": req.model,
        "refs": len(req.images) + len(req.videos) + len(req.audios),
    }
    # 记录实际将使用的凭据（与 run_video_job 内 get_video 的解析一致），前端历史据此显示用的哪把 Key
    cred_label = registry.resolve_credential_label(req.credential_id, req.provider)
    if cred_label:
        params["credential"] = cred_label
    job = job_manager.create("video", req.provider, req.model, params)
    job_manager.attach(
        job["id"],
        video_service.run_video_job(
            job_id=job["id"],
            provider=req.provider,
            model=req.model,
            credential_id=req.credential_id,
            mode=mode,
            prompt=req.prompt,
            duration=duration,
            aspect_ratio=req.aspect_ratio,
            resolution=req.resolution,
            images=req.images,
            last_images=req.last_images,
            videos=req.videos,
            audios=req.audios,
            options=req.options or {},
        ),
    )
    return {"job_id": job["id"], "job": job}
