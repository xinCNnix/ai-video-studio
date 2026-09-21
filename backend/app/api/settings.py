"""设置相关 API：模型目录 / API Key 凭据 / 模型发现与自定义 / ComfyUI 模板 / 常规信息。"""
import json

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import key_manager, model_store, settings
from ..config import app_settings
from ..providers.base import ProviderError
from ..providers.registry import get_llm, provider_label
from ..services import file_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AddKeyRequest(BaseModel):
    provider: str
    name: str = "Default"
    api_key: str = Field(min_length=1)
    base_url: str | None = None
    group_id: str | None = None


def _merged(capability: str, builtin: list[dict]) -> list[dict]:
    """内置兜底 + 用户自定义模型合并（自定义模型补齐视频能力默认值）。"""
    out = list(builtin)
    seen = {(m["id"], m["provider"]) for m in out}
    for m in model_store.list_custom().get(capability, []):
        if (m["id"], m["provider"]) in seen:
            continue
        entry = dict(m)
        if capability == "video":
            entry.setdefault("durations", list(range(1, 16)))
            entry.setdefault("aspect_ratios", ["16:9", "9:16", "1:1"])
            entry.setdefault("resolutions", ["auto"])
            entry.setdefault("modes", ["text", "image"])
        out.append(entry)
    return out


@router.get("/catalog")
async def get_catalog():
    return {
        "providers": [
            {"id": pid, **info} for pid, info in settings.PROVIDER_CATALOG.items()
        ],
        "llm": _merged("llm", settings.LLM_MODELS),
        "tts": _merged("tts", settings.TTS_MODELS),
        "tts_voices": _merged("voice", settings.TTS_VOICES),
        "emotions": settings.TTS_EMOTIONS,
        "image": _merged("image", settings.IMAGE_MODELS),
        "video": _merged("video", settings.VIDEO_MODELS),
        "prompt_modes": settings.PROMPT_MODES,
        "refine_actions": settings.REFINE_ACTIONS,
    }


def _fetch_voices_payload(data: dict) -> list[dict]:
    """解析 get_voice 响应（system_voice / voice_cloning / voice_list）。"""
    voices: list[dict] = []
    for source, key in (("system", "system_voice"), ("clone", "voice_cloning"), ("system", "voice_list")):
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("voice_id"):
                vid = item["voice_id"]
                if any(v["id"] == vid for v in voices):
                    continue
                voices.append({"id": vid, "label": item.get("description") or vid, "source": source})
    return voices


def _discover_voices(cred: dict) -> tuple[list[dict], str | None]:
    """调 MiniMax get_voice（POST）获取系统音色 + 克隆音色。返回 (voices, error)。"""
    base = (cred.get("base_url") or "https://api.minimax.cn").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    params = {"GroupId": cred["group_id"]} if cred.get("group_id") else None
    try:
        resp = httpx.post(
            f"{base}/v1/get_voice",
            params=params,
            json={"voice_type": "all"},
            headers={"Authorization": f"Bearer {cred['api_key']}"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return [], f"请求失败：{str(exc)[:120]}"
    if resp.status_code >= 400:
        return [], f"接口返回 {resp.status_code}（该地址可能不是 MiniMax）"
    data = resp.json()
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code", 0) != 0:
        msg = base_resp.get("status_msg") or "未知错误"
        if base_resp.get("status_code") in (2013, 1004):
            msg = f"{msg} —— 音色发现需要 Group ID：请在 MiniMax 控制台复制 Group ID 并补填到该 Key"
        return [], msg
    return _fetch_voices_payload(data), None


# ---------------- 模型发现：从已配置凭据的 /models 接口拉取 ----------------
@router.get("/discovered-models")
async def discovered_models():
    results: dict[str, list[str]] = {}
    results_by_cred: dict[str, list[str]] = {}  # 凭据id → 该端点实际可用的模型
    discovered_tts: dict[str, list[str]] = {}
    voices: dict[str, list[dict]] = {}
    voice_errors: list[dict] = []

    def classify(ids: list[str]) -> tuple[list[str], list[str]]:
        """把厂商 /models 混合列表按能力分类：TTS 系归语音模型，ASR/多模态辅助排除。"""
        exclude_llm = ("asr", "voiceclone", "voicedesign", "music", "image", "embedding", "rerank", "whisper")
        llm, tts = [], []
        for mid in ids:
            low = mid.lower()
            if "tts" in low:
                tts.append(mid)
            elif any(k in low for k in exclude_llm):
                continue
            else:
                llm.append(mid)
        return llm, tts

    for pub in key_manager.list_keys():
        try:
            cred = key_manager.get_key(pub["id"])
        except key_manager.KeyNotFound:
            continue
        info = settings.PROVIDER_CATALOG.get(cred["provider"])
        if not info:
            continue
        caps = info.get("capabilities", [])
        # 音色发现：minimax 凭据，或指向 MiniMax 的 openai_compatible 凭据
        base_lower = (cred.get("base_url") or "").lower()
        is_minimax_like = cred["provider"] == "minimax" or (
            cred["provider"] == "openai_compatible" and "minimax" in base_lower
        )
        if is_minimax_like:
            provider_voices, error = _discover_voices(cred)
            if error:
                voice_errors.append(
                    {"provider": cred["provider"], "name": cred["name"], "message": error}
                )
            elif provider_voices:
                # 统一归到 minimax 组：minimax 凭据与指向 MiniMax 的 openai_compatible
                # 凭据会发现同一份音色库，分组按凭据 provider 会整组重复
                existing = voices.setdefault("minimax", [])
                have = {v["id"] for v in existing}
                existing.extend(v for v in provider_voices if v["id"] not in have)
        # 小米 MiMo：无音色查询接口，官方预置音色为静态列表，配了 Key 即全量下发；
        # 想要更多音色 → 用 mimo-v2.5-tts-voicedesign 模型，Voice 填音色文字描述
        is_xiaomi_like = cred["provider"] == "xiaomi" or (
            cred["provider"] == "openai_compatible" and "xiaomimimo" in base_lower
        )
        if is_xiaomi_like:
            from ..providers.tts.xiaomi_tts import XIAOMI_PRESET_VOICES

            existing = voices.setdefault("xiaomi", [])
            have = {v["id"] for v in existing}
            existing.extend(v for v in XIAOMI_PRESET_VOICES if v["id"] not in have)
        if not ("llm" in caps or "image" in caps):
            continue
        base = cred.get("base_url") or info.get("default_base_url") or ""
        if cred["provider"] == "minimax" and not cred.get("base_url"):
            base = "https://api.minimax.cn/v1"
        base = base.rstrip("/")
        if not base:
            continue
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{base}/models", headers={"Authorization": f"Bearer {cred['api_key']}"}
                )
            if resp.status_code < 400:
                ids = [
                    m.get("id")
                    for m in resp.json().get("data", [])
                    if isinstance(m, dict) and m.get("id")
                ]
                llm_ids, tts_ids = classify(ids)
                current = results.setdefault(cred["provider"], [])
                cred_models = results_by_cred.setdefault(pub["id"], [])
                for mid in llm_ids:
                    if mid not in current:
                        current.append(mid)
                    if mid not in cred_models:
                        cred_models.append(mid)
                if tts_ids:
                    # mimo-* TTS 走小米私有协议：归属 xiaomi 厂商，避免与内置条目
                    # 在选择器里重复，并保证语音页选中后按小米协议路由
                    tts_provider = (
                        "xiaomi" if all(i.lower().startswith("mimo-") for i in tts_ids)
                        else cred["provider"]
                    )
                    tts_existing = discovered_tts.setdefault(tts_provider, [])
                    for mid in tts_ids:
                        if mid not in tts_existing:
                            tts_existing.append(mid)
        except Exception:  # noqa: BLE001 - 单个凭据失败不影响整体
            continue
    # 已固化的音色资产：设计音色保存样本后即可在选择器里按名复用（稳定，不漂移）
    from ..config import voice_assets

    existing = voices.setdefault("xiaomi", [])
    have = {v["id"] for v in existing}
    for a in voice_assets.list_assets():
        if a["id"] in have:
            continue
        tag = "固化音色·稳定复用" if a["kind"] == "clone" else "设计音色·未固化会漂移"
        existing.append({"id": a["id"], "label": f"{a['name']}（{tag}）", "source": "asset"})
    return {
        "discovered": results,
        "discovered_by_credential": results_by_cred,
        "discovered_tts": discovered_tts,
        "voices": voices,
        "voice_errors": voice_errors,
    }


class TestModelRequest(BaseModel):
    provider: str
    credential_id: str | None = None
    model: str


class DefaultLlmRequest(BaseModel):
    model: str
    credential_id: str | None = None


@router.get("/default-llm")
async def get_default_llm():
    return {"default_llm": app_settings.get_default_llm()}


@router.put("/default-llm")
async def put_default_llm(req: DefaultLlmRequest):
    return {"default_llm": app_settings.set_default_llm(req.model, req.credential_id)}


@router.post("/test-model")
async def test_model(req: TestModelRequest):
    """对指定模型做一次最小连通性测试（厂商 /models 列表与实际可用性可能不一致）。"""
    import time

    start = time.time()
    try:
        llm = get_llm(req.credential_id, provider_hint=req.provider)
        out = await llm.generate(
            [{"role": "user", "content": "ping"}], req.model, temperature=0, max_tokens=1024
        )
        return {"ok": True, "message": f"可用（{int((time.time() - start) * 1000)}ms）", "sample": (out or "")[:60]}
    except ProviderError as exc:
        return {"ok": False, "message": str(exc)[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)[:400]}


# ---------------- 自定义模型 CRUD ----------------
class CustomModelRequest(BaseModel):
    capability: str
    provider: str
    model_id: str


@router.get("/custom-models")
async def list_custom_models():
    return {"custom": model_store.list_custom()}


@router.post("/custom-models")
async def add_custom_model(req: CustomModelRequest):
    if req.provider not in settings.PROVIDER_CATALOG:
        raise HTTPException(status_code=400, detail=f"未知厂商：{req.provider}")
    try:
        return model_store.add_model(req.capability, req.provider, req.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/custom-models")
async def remove_custom_model(capability: str, provider: str, model_id: str):
    removed = model_store.remove_model(capability, provider, model_id)
    if not removed:
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"ok": True}


@router.get("/credentials")
async def list_credentials():
    return {"credentials": key_manager.list_keys()}


@router.post("/credentials")
async def add_credential(req: AddKeyRequest):
    if req.provider not in settings.PROVIDER_CATALOG:
        raise HTTPException(status_code=400, detail=f"未知厂商：{req.provider}")
    key = key_manager.add_key(req.provider, req.name, req.api_key, req.base_url, req.group_id)
    return key


@router.delete("/credentials/{key_id}")
async def delete_credential(key_id: str):
    try:
        key_manager.delete_key(key_id)
    except key_manager.KeyNotFound:
        raise HTTPException(status_code=404, detail="Key 不存在")
    return {"ok": True}


class UpdateKeyRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    group_id: str | None = None


@router.patch("/credentials/{key_id}")
async def update_credential(key_id: str, req: UpdateKeyRequest):
    try:
        return key_manager.update_key(key_id, req.name, req.base_url, req.group_id)
    except key_manager.KeyNotFound:
        raise HTTPException(status_code=404, detail="Key 不存在")


@router.post("/credentials/{key_id}/default")
async def set_default_credential(key_id: str):
    try:
        return key_manager.set_default(key_id)
    except key_manager.KeyNotFound:
        raise HTTPException(status_code=404, detail="Key 不存在")


@router.post("/credentials/{key_id}/test")
async def test_credential(key_id: str):
    try:
        cred = key_manager.get_key(key_id)
    except key_manager.KeyNotFound:
        raise HTTPException(status_code=404, detail="Key 不存在")

    import time

    start = time.time()
    try:
        if cred["provider"] in ("openai", "openai_compatible", "generic_video", "ollama", "xiaomi"):
            base = (cred.get("base_url") or (settings.PROVIDER_CATALOG.get(cred["provider"], {}).get("default_base_url") or "https://api.openai.com/v1")).rstrip("/")
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {cred['api_key']}"})
            ok = resp.status_code < 400
            message = "连接成功" if ok else f"返回 {resp.status_code}"
        else:  # minimax：用一次 1 token 的对话 ping
            llm = get_llm(cred["id"])
            await llm.generate([{"role": "user", "content": "ping"}], "MiniMax-M2", max_tokens=4)
            ok, message = True, "连接成功"
    except ProviderError as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # noqa: BLE001
        ok, message = False, str(exc)[:200]
    return {"ok": ok, "message": message, "latency_ms": int((time.time() - start) * 1000)}


@router.get("/general")
async def general_info():
    return {
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "data_dir": str(settings.DATA_DIR),
        "output_counts": file_service.output_counts(),
    }


class CleanupRequest(BaseModel):
    days: int = 7


@router.post("/cleanup-uploads")
async def cleanup_uploads(req: CleanupRequest):
    days = min(max(int(req.days or 7), 1), 365)
    # 仍被任何项目引用的上传素材不参与清理（删了分镜引用就 404）：
    # 收集所有项目 scenes 里的 image/voice/video 与 ref_* 引用
    keep: set = set()
    for f in settings.PROJECTS_DIR.glob("project_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 单个项目损坏不影响其余项目的保护
            continue
        for s in data.get("scenes") or []:
            if not isinstance(s, dict):
                continue
            urls = [s.get("image"), s.get("voice"), s.get("video")]
            urls += [u for u in (s.get("ref_images") or []) if isinstance(u, str)]
            urls += [u for u in (s.get("ref_videos") or []) if isinstance(u, str)]
            urls += [u for u in (s.get("ref_audios") or []) if isinstance(u, str)]
            for o in data.get("orphan_refs") or []:
                if isinstance(o, dict):
                    urls += [u for k in ("ref_images", "ref_videos", "ref_audios")
                             for u in (o.get(k) or []) if isinstance(u, str)]
            for url in urls:
                local = file_service.resolve_local(url) if isinstance(url, str) else None
                if local:
                    keep.add(local)
    deleted = file_service.cleanup_uploads(days, keep_paths=keep)
    return {"ok": True, "deleted": deleted, "days": days, "kept_referenced": len(keep)}


# ---------------- ComfyUI 本地工作流模板管理 ----------------
import re as _re

_TEMPLATE_NAME_RE = _re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _comfy_dir():
    from ..providers.comfy_local import _comfy_dir

    return _comfy_dir()


@router.get("/comfyui-templates")
async def list_comfy_templates():
    d = _comfy_dir()
    templates = []
    for f in sorted(d.glob("*.json")):
        try:
            templates.append({"name": f.stem, "size": f.stat().st_size})
        except OSError:
            continue
    return {"templates": templates}


@router.get("/comfyui-templates/{name}")
async def get_comfy_template(name: str):
    if not _TEMPLATE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法模板名")
    path = _comfy_dir() / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="模板不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"模板解析失败：{exc}") from exc


@router.put("/comfyui-templates/{name}")
async def save_comfy_template(name: str, body: dict):
    if not _TEMPLATE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="模板名只允许字母/数字/下划线/中划线")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="工作流必须是 JSON 对象（ComfyUI「Save (API Format)」导出格式）")
    path = _comfy_dir() / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": name}


@router.delete("/comfyui-templates/{name}")
async def delete_comfy_template(name: str):
    if not _TEMPLATE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法模板名")
    path = _comfy_dir() / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="模板不存在")
    path.unlink()
    return {"ok": True}
