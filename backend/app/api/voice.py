"""AI 语音（TTS）生成 API + 音色资产管理（设计音色固化/复用）。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import settings, voice_assets
from ..jobs.manager import job_manager
from ..providers import registry
from ..providers.base import ProviderError
from ..services import file_service, voice_service

router = APIRouter(prefix="/api/voice", tags=["voice"])


class VoiceRequest(BaseModel):
    provider: str = "minimax"
    model: str = Field(min_length=1)
    credential_id: str | None = None
    text: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    speed: float = 1.0
    emotion: str = "neutral"


@router.post("/generate")
async def generate(req: VoiceRequest):
    if len(req.text) > 20000:
        raise HTTPException(status_code=400, detail="文本过长（上限 20000 字，超长部分自动分段合成）")
    params = {"text": req.text[:120], "voice": req.voice, "speed": req.speed, "emotion": req.emotion, "model": req.model}
    # 记录实际将使用的凭据（与 run_voice_job 内 get_tts 的解析一致），前端历史据此显示用的哪把 Key
    cred_label = registry.resolve_credential_label(req.credential_id, req.provider)
    if cred_label:
        params["credential"] = cred_label
    job = job_manager.create("tts", req.provider, req.model, params)
    job_manager.attach(
        job["id"],
        voice_service.run_voice_job(
            job_id=job["id"],
            provider=req.provider,
            model=req.model,
            credential_id=req.credential_id,
            text=req.text,
            voice=req.voice,
            speed=req.speed,
            emotion=req.emotion,
        ),
    )
    return {"job_id": job["id"], "job": job}


@router.get("/voices")
async def voices():
    return {"voices": settings.TTS_VOICES, "emotions": settings.TTS_EMOTIONS}


# ---------------- 音色资产：设计音色的固化与复用 ----------------
class SaveAssetRequest(BaseModel):
    name: str = Field(min_length=1)
    kind: str = "clone"  # clone=带样本固化（推荐） / design=仅描述
    desc: str = ""
    sample_from: str | None = None  # 任务输出/上传音频的 URL（mp3/wav）
    sync_minimax: bool = False  # 同时克隆到 MiniMax 服务端（生成永久 voice_id）
    credential_id: str | None = None  # MiniMax 凭据（缺省自动解析默认 Key）


@router.get("/assets")
async def list_assets():
    return {"assets": voice_assets.list_assets()}


def _minimax_clone_models() -> list[str]:
    """克隆用模型候选：优先用户配置的 MiniMax TTS 模型，再兜底官方主流型号。

    /v1/voice_clone 的 model 为必填且按账号白名单校验（缺省报
    "invalid params, model not allowed"），适配器会在候选间自动回退。
    """
    from ..config import model_store

    ids = [m["id"] for m in model_store.list_custom().get("tts", []) if m.get("provider") == "minimax"]
    ids += ["speech-2.8-hd", "speech-2.5-hd-preview", "speech-02-hd"]
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


async def _clone_asset_to_minimax(asset_id: str, credential_id: str | None) -> str:
    """把带样本的资产克隆到 MiniMax，成功时把 voice_id 写回资产。返回错误（空=成功）。"""
    raw = voice_assets.find_asset(asset_id) or {}
    sample_path = voice_assets.SAMPLES_DIR / raw["sample"] if raw.get("sample") else None
    if not (sample_path and sample_path.exists()):
        return "资产没有可克隆的本地样本（可能已被清理）。"
    # voice_ab12cd34 → vga_ab12cd34：与资产一一对应，便于排查
    mm_voice_id = f"vga_{asset_id.removeprefix('voice_')}"
    try:
        mm = registry.get_minimax_tts(credential_id)
        await mm.clone_voice(
            sample_path, mm_voice_id,
            preview_text="你好，这是我的克隆音色。",
            models=_minimax_clone_models(),
        )
    except ProviderError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - 克隆失败不拖垮资产保存
        return f"MiniMax 克隆异常：{str(exc)[:200]}"
    voice_assets.set_minimax_voice(asset_id, mm_voice_id)
    return ""


def _public_with_error(asset_id: str, error: str) -> dict:
    fresh = next((a for a in voice_assets.list_assets() if a["id"] == asset_id), None)
    if fresh is None:
        return {"id": asset_id, "minimax_error": error}
    out = dict(fresh)
    out["minimax_error"] = error
    return out


@router.post("/assets")
async def create_asset(req: SaveAssetRequest):
    sample_src = None
    if req.sample_from:
        sample_src = file_service.resolve_local(req.sample_from)
        if sample_src is None:
            raise HTTPException(status_code=400, detail="sample_from 必须是本服务的音频文件 URL（任务输出或上传）")
    try:
        asset = voice_assets.create_asset(req.name.strip(), req.kind, req.desc, sample_src)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 同步克隆到 MiniMax：失败不阻断资产保存（小米样本克隆仍可用），错误随资产带回前端提示
    if req.sync_minimax and asset.get("has_sample"):
        return _public_with_error(asset["id"], await _clone_asset_to_minimax(asset["id"], req.credential_id))
    return asset


class CloneMinimaxRequest(BaseModel):
    credential_id: str | None = None


@router.post("/assets/{asset_id}/clone-minimax")
async def clone_asset_to_minimax(asset_id: str, req: CloneMinimaxRequest | None = None):
    """对已保存的资产补做/重试 MiniMax 服务端克隆（样本无需重新上传）。"""
    if voice_assets.find_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail="音色资产不存在")
    body = req or CloneMinimaxRequest()
    return _public_with_error(asset_id, await _clone_asset_to_minimax(asset_id, body.credential_id))


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str):
    if not voice_assets.delete_asset(asset_id):
        raise HTTPException(status_code=404, detail="音色资产不存在")
    return {"ok": True}


@router.get("/assets/{asset_id}/sample")
async def get_asset_sample(asset_id: str):
    asset = voice_assets.find_asset(asset_id)
    if not asset or not asset.get("sample"):
        raise HTTPException(status_code=404, detail="该音色没有样本文件")
    path = voice_assets.SAMPLES_DIR / asset["sample"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="样本文件缺失")
    return FileResponse(path, media_type=file_service.guess_mime(path), filename=f"{asset['name']}{path.suffix}")
