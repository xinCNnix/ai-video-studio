"""根据凭据构造 Provider 实例的注册中心。

TTS 方言按凭据自动选择：
- 小米凭据，或 base_url 指向 xiaomimimo 的凭据 → MiMo chat/completions 协议
- minimax 凭据，或 base_url 指向 MiniMax 的凭据 → MiniMax t2a_v2 协议
- 其余（OpenAI / OpenAI Compatible）→ 通用 /audio/speech 协议
新增厂商 = 在 providers/ 下加一个适配器文件并在此注册。
"""
import httpx

from ..config import settings
from ..config.key_manager import get_key, get_default_key, KeyNotFound
from .base import LLMProvider, ImageProvider, TTSProvider, VideoProvider, ProviderError
from .llm.openai_compatible import OpenAICompatibleLLM
from .image.openai_image import OpenAIImageProvider
from .image.bfl_image import BFLImageProvider
from .image.gemini_image import GeminiImageProvider
from .image.minimax_image import MiniMaxImageProvider
from .image.dashscope_image import DashScopeImageProvider
from .comfy_local import ComfyUILocal
from .tts.minimax_tts import MiniMaxTTS
from .tts.openai_tts import OpenAITTS
from .tts.xiaomi_tts import XiaomiMiMoTTS
from .video.minimax_h3 import MiniMaxH3Video
from .video.generic_video import GenericVideoProvider
from .video.seedance import SeedanceVideo
from .video.wan import WanVideo
from .video.autodl_comfyui import AutoDLComfyUI

MINIMAX_LLM_BASE = "https://api.minimax.cn/v1"
OPENAI_BASE = "https://api.openai.com/v1"

# 厂商 → 域名特征：兜底挑选凭据时优先匹配，避免把请求发给错误的网关
PROVIDER_DOMAIN = {
    "minimax": "minimax",
    "xiaomi": "xiaomimimo",
    "seedance": "volces",
    "wan": "dashscope",
    "bfl": "bfl",
    "google": "googleapis",
    "autodl": "autodl.art",
    "ollama": "localhost",
}


def get_llm(credential_id: str | None, provider_hint: str | None = None) -> LLMProvider:
    cred = _resolve_credential(credential_id, provider_hint or "minimax")
    base = _llm_base_for(cred)
    return OpenAICompatibleLLM(cred["api_key"], base)


def _llm_base_for(cred: dict) -> str:
    base = cred.get("base_url") or ""
    if not base:
        if cred["provider"] == "minimax":
            base = MINIMAX_LLM_BASE
        else:
            # Ollama 等类型使用目录里的默认地址（如 http://localhost:11434/v1）
            base = (
                settings.PROVIDER_CATALOG.get(cred["provider"], {}).get("default_base_url")
                or OPENAI_BASE
            )
    return base


_model_cache: dict[str, tuple[float, list[str]]] = {}


def list_models_for_credential(cred: dict) -> list[str]:
    """查询凭据端点的 /models 列表（10 分钟缓存）。"""
    import time

    cache_key = f"{cred['id']}:{cred.get('base_url') or ''}"
    now = time.time()
    hit = _model_cache.get(cache_key)
    if hit and now - hit[0] < 600:
        return hit[1]
    ids: list[str] = []
    try:
        resp = httpx.get(
            f"{_llm_base_for(cred)}/models",
            headers={"Authorization": f"Bearer {cred['api_key']}"},
            timeout=10,
        )
        if resp.status_code < 400:
            ids = [
                m.get("id")
                for m in resp.json().get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
    except Exception:  # noqa: BLE001
        ids = []
    _model_cache[cache_key] = (now, ids)
    return ids


def pick_default_model_for_credential(cred: dict) -> str | None:
    """为凭据自动挑一个可用的文本模型（优先厂商旗舰命名）。"""
    ids = list_models_for_credential(cred)
    if not ids:
        return None
    base_l = (cred.get("base_url") or "").lower()
    exclude = ("tts", "asr", "voiceclone", "voicedesign", "music", "image", "embedding", "rerank")
    llm_ids = [i for i in ids if not any(k in i.lower() for k in exclude)]
    if not llm_ids:
        return None
    if "minimax" in base_l or cred["provider"] == "minimax":
        for cand in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2"):
            if cand in llm_ids:
                return cand
    return llm_ids[0]


def resolve_llm_call(
    credential_id: str | None, provider_hint: str | None = None, model: str | None = None
) -> tuple[LLMProvider, str]:
    """解析 LLM 调用的 (适配器, 模型)。优先级：显式指定 > 应用默认 > 凭据端点自动挑选。"""
    from ..config import app_settings

    cred = _resolve_credential(credential_id, provider_hint or "minimax")
    if not model:
        d = app_settings.get_default_llm()
        if d:
            model = d["model"]
            if d.get("credential_id"):
                try:
                    cred = get_key(d["credential_id"])
                except KeyNotFound:
                    pass
    if not model:
        picked = pick_default_model_for_credential(cred)
        if not picked:
            raise ProviderError(
                "无法确定默认文本模型：请在「设置 → Models → 默认文本模型」中选择，或显式指定模型。"
            )
        model = picked
    return OpenAICompatibleLLM(cred["api_key"], _llm_base_for(cred)), model


def get_image(credential_id: str | None, provider_hint: str | None = None) -> ImageProvider:
    cred = _resolve_credential(credential_id, provider_hint or "openai")
    base = cred.get("base_url") or ""
    hint = provider_hint or cred["provider"]

    # ComfyUI 本地（模板驱动）：模板名 = 模型 ID
    if hint == "comfyui" or cred["provider"] == "comfyui":
        return ComfyUILocal(cred["api_key"], base or "http://127.0.0.1:8188", kind="image")

    # 方舟（Seedance/Seedream）：图片接口与 OpenAI 同形，仅默认域名不同
    if hint == "seedance":
        _require_endpoint(cred, "volces", "Seedream 需要 Seedance (火山方舟) 类型的 API Key，请在设置中添加。")
        return OpenAIImageProvider(cred["api_key"], base or "https://ark.cn-beijing.volces.com/api/v3")
    # Gemini（Nano Banana）：私有协议，支持多参考图
    if hint == "google":
        _require_endpoint(cred, "googleapis", "Nano Banana 需要 Google Gemini 类型的 API Key，请在设置中添加。")
        return GeminiImageProvider(cred["api_key"], base or "https://generativelanguage.googleapis.com/v1beta")
    # FLUX：BFL 官方异步协议
    if hint == "bfl":
        _require_endpoint(cred, "bfl", "FLUX 需要 FLUX (BFL) 类型的 API Key，请在设置中添加。")
        return BFLImageProvider(cred["api_key"], base or "https://api.bfl.ai/v1")
    # DashScope 原生（Qwen-Image / WanX）：指向 dashscope 时走原生异步协议，其他网关走 OpenAI 兼容
    if hint == "wan":
        _require_endpoint(cred, "dashscope", "Qwen-Image/WanX 需要 Wan (阿里云百炼) 类型的 API Key，请在设置中添加。")
        return DashScopeImageProvider(cred["api_key"], base or "https://dashscope.aliyuncs.com/api/v1")
    # MiniMax（image-01 系列）：私有同步协议
    if hint == "minimax":
        _require_endpoint(cred, "minimax", "MiniMax 生图需要 MiniMax 类型的 API Key，请在设置中添加。")
        return MiniMaxImageProvider(cred["api_key"], base or "https://api.minimax.cn")
    # openai 形态生图：凭据不能指向纯 LLM/语音网关（否则 gpt-image-1 请求 404），
    # 防止"任意 Key 兜底"误路由；未知网关放行，由厂商错误如实透出
    base_l = base.lower()
    non_image_hosts = ("minimax", "xiaomimimo", "localhost", "127.0.0.1", "ollama")
    if any(h in base_l for h in non_image_hosts):
        raise ProviderError(
            "当前没有可用的生图 API Key（现有的 Key 均指向 LLM/语音网关）。"
            "请到「设置 → API Keys」添加生图类凭据：OpenAI / Google Gemini (Nano Banana) / FLUX (BFL) / Seedance (火山方舟) / Wan (阿里云百炼) 任选其一。"
        )
    return OpenAIImageProvider(cred["api_key"], base or OPENAI_BASE)


def _require_endpoint(cred: dict, domain: str, message: str) -> None:
    """目标厂商有独立域名时，兜底解析到的凭据必须真的指向该厂商，否则提前报错。"""
    if cred["provider"] in ("seedance", "wan", "minimax"):
        return
    if domain not in (cred.get("base_url") or "").lower():
        raise ProviderError(message)


def get_tts(credential_id: str | None, provider_hint: str | None = None) -> TTSProvider:
    cred = _resolve_credential(credential_id, provider_hint or "minimax")
    base = cred.get("base_url") or ""
    # ComfyUI 本地 TTS：模板名 = 自定义语音模型 ID
    if cred["provider"] == "comfyui" or provider_hint == "comfyui":
        return ComfyUILocal(cred["api_key"], base or "http://127.0.0.1:8188", kind="tts")
    # 小米 MiMo：私有 chat/completions 形态（mimo-v2.5-tts 系列）
    is_xiaomi = cred["provider"] == "xiaomi" or "xiaomimimo" in base.lower()
    if is_xiaomi:
        return XiaomiMiMoTTS(cred["api_key"], base or "https://api.xiaomimimo.com")
    if provider_hint == "xiaomi":
        raise ProviderError(
            "小米 TTS 需要 Xiaomi MiMo 类型的 API Key（或在 OpenAI Compatible 凭据里填 "
            "https://api.xiaomimimo.com/v1），请到「设置 → API Keys」添加。"
        )
    is_minimax = cred["provider"] == "minimax" or "minimax" in base.lower()
    if is_minimax:
        return MiniMaxTTS(cred["api_key"], base or "https://api.minimax.cn", cred.get("group_id"))
    return OpenAITTS(cred["api_key"], base or OPENAI_BASE)


def _is_minimax_cred(cred: dict) -> bool:
    return cred["provider"] == "minimax" or "minimax" in (cred.get("base_url") or "").lower()


def get_minimax_tts(credential_id: str | None = None) -> MiniMaxTTS:
    """克隆专用：必须解析到真正指向 MiniMax 的凭据，不做“任意 Key”兜底。

    顺序：显式指定 > MiniMax 厂商默认 > 任一 MiniMax 类凭据。
    """
    if credential_id:
        try:
            cred = get_key(credential_id)
        except KeyNotFound as exc:
            raise ProviderError(f"凭据 {credential_id} 不存在。") from exc
        if not _is_minimax_cred(cred):
            raise ProviderError("指定凭据不是 MiniMax Key，无法克隆到 MiniMax。")
    else:
        try:
            cred = get_default_key("minimax")
        except KeyNotFound:
            cred = None
        if cred is None or not _is_minimax_cred(cred):
            cred = next(
                (get_key(pub["id"]) for pub in key_manager_list() if _is_minimax_cred(pub)),
                None,
            )
        if cred is None:
            raise ProviderError(
                "未找到 MiniMax API Key：请到「设置 → API Keys」添加 MiniMax 凭据后再克隆。"
            )
    base = cred.get("base_url") or "https://api.minimax.cn"
    return MiniMaxTTS(cred["api_key"], base, cred.get("group_id"))


def get_video(provider: str, credential_id: str | None) -> VideoProvider:
    cred = _resolve_credential(credential_id, provider)
    if provider == "seedance":
        _require_endpoint(cred, "volces", "Seedance 需要 Seedance (火山方舟) 类型的 API Key，请在设置中添加。")
        return SeedanceVideo(cred["api_key"], cred.get("base_url"))
    if provider == "wan":
        _require_endpoint(cred, "dashscope", "Wan 需要 Wan (阿里云百炼) 类型的 API Key，请在设置中添加。")
        return WanVideo(cred["api_key"], cred.get("base_url"))
    if provider == "autodl":
        return AutoDLComfyUI(cred["api_key"], cred.get("base_url"))
    if provider == "comfyui":
        return ComfyUILocal(cred["api_key"], cred.get("base_url") or "http://127.0.0.1:8188", kind="video")
    if provider == "minimax":
        return MiniMaxH3Video(cred["api_key"], cred.get("base_url") or "https://api.minimax.cn", cred.get("group_id"))
    if provider == "generic_video":
        return GenericVideoProvider(cred["api_key"], cred.get("base_url") or "")
    raise ProviderError(f"不支持的视频厂商：{provider}")


def _resolve_credential(credential_id: str | None, provider_hint: str | None) -> dict:
    """解析凭据：显式指定 > 厂商默认 > 域名匹配 > 任意可用。"""
    if credential_id:
        try:
            return get_key(credential_id)
        except KeyNotFound as exc:
            raise ProviderError(f"凭据 {credential_id} 不存在。") from exc
    try:
        return get_default_key(provider_hint)
    except KeyNotFound:
        pass
    # 域名匹配：全局默认凭据可能指向其他厂商（如小米），不能盲目使用
    domain = PROVIDER_DOMAIN.get(provider_hint or "")
    if domain:
        for pub in key_manager_list():
            if domain in (pub.get("base_url") or "").lower() or pub["provider"] == provider_hint:
                return get_key(pub["id"])
    # 任意可用 Key（单凭据用户最常见）
    try:
        return get_default_key(None)
    except KeyNotFound as exc:
        raise ProviderError(
            f"未找到可用的 API Key（{provider_hint or "默认"}），请先在「设置 → API Keys」中添加。"
        ) from exc


def key_manager_list() -> list[dict]:
    from ..config import key_manager

    return key_manager.list_keys()


def resolve_credential_label(credential_id: str | None, provider_hint: str | None = None) -> str | None:
    """解析本次调用将实际使用的凭据，返回展示名（写入任务记录，前端据此显示用的哪把 Key）。
    与 get_tts/get_image/get_video 的解析路径完全一致：显式指定 > 厂商默认 > 域名匹配 > 任意。
    """
    try:
        cred = _resolve_credential(credential_id, provider_hint)
    except ProviderError:
        return None
    return f"{cred.get('name') or cred['provider']} · {cred['api_key'][-4:] if len(cred.get('api_key', '')) >= 4 else ''}".rstrip(" ·")


def provider_label(provider: str) -> str:
    return settings.PROVIDER_CATALOG.get(provider, {}).get("label", provider)
