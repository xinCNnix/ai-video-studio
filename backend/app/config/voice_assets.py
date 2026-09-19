"""音色资产管理：设计音色的固化与稳定复用。

小米 voicedesign 是"从零生成"——同一描述每次合成的音色都会漂移（无 seed、
无音色库、无可回传的 voice_id）。固化机制：把满意的一次设计输出保存为音频
样本文件，之后该音色的所有合成自动切换到 voiceclone 协议，音色锚定在样本上。

MiniMax 侧是服务端一次性克隆：保存资产时可选同步调 /v1/voice_clone 生成
永久 voice_id（记录在 minimax_voice_id），之后 speech 系列模型直接复用。

资产两类：
- kind=clone：带样本文件，稳定复用（推荐形态）
- kind=design：仅存描述，每次合成仍会漂移（过渡形态，待固化）
"""
import base64
import json
import mimetypes
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import settings

ASSETS_FILE = settings.CONFIG_DIR / "voice_assets.json"
SAMPLES_DIR = settings.DATA_DIR / "voices"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    if not ASSETS_FILE.exists():
        return []
    try:
        return json.loads(ASSETS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件损坏时当作无资产，不阻断配音
        return []


def _save(items: list[dict]) -> None:
    ASSETS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _public(a: dict) -> dict:
    sample = SAMPLES_DIR / a["sample"] if a.get("sample") else None
    return {
        "id": a["id"],
        "name": a["name"],
        "kind": a["kind"],
        "desc": a.get("desc") or "",
        "has_sample": bool(sample and sample.exists()),
        "sample_url": f"/api/voice/assets/{a['id']}/sample" if (sample and sample.exists()) else None,
        # MiniMax 服务端克隆音色 ID（可选）：有值时该资产可直接用于 speech 系列模型
        "minimax_voice_id": a.get("minimax_voice_id") or None,
        "created_at": a.get("created_at"),
    }


def list_assets() -> list[dict]:
    return [_public(a) for a in _load()]


def find_asset(value: str) -> dict | None:
    """按资产 ID 或唯一名称查找（名称重名时不按名称匹配，避免歧义）。"""
    value = (value or "").strip()
    if not value:
        return None
    items = _load()
    for a in items:
        if a["id"] == value:
            return a
    named = [a for a in items if a.get("name") == value]
    return named[0] if len(named) == 1 else None


def create_asset(name: str, kind: str, desc: str = "", sample_src: Path | None = None) -> dict:
    """创建音色资产。sample_src 为本地音频文件（mp3/wav），存在则固化为 clone。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("音色名不能为空")
    if not desc and sample_src is None:
        raise ValueError("需要音色描述（design）或音频样本（clone）至少一项")
    asset_id = f"voice_{uuid.uuid4().hex[:8]}"
    sample_name = ""
    if sample_src is not None:
        ext = sample_src.suffix.lower()
        if ext not in SAMPLE_MIME:
            raise ValueError(f"样本仅支持 mp3/wav，收到：{ext or '无扩展名'}")
        sample_name = f"{asset_id}{ext}"
        shutil.copyfile(sample_src, SAMPLES_DIR / sample_name)
        if kind != "clone":
            kind = "clone"  # 有样本即为固化形态
    item = {
        "id": asset_id,
        "name": name,
        "kind": kind if kind in ("clone", "design") else "design",
        "desc": (desc or "").strip(),
        "sample": sample_name,
        "created_at": _now(),
    }
    items = _load()
    items.append(item)
    _save(items)
    return _public(item)


def delete_asset(asset_id: str) -> bool:
    items = _load()
    target = next((a for a in items if a["id"] == asset_id), None)
    if target is None:
        return False
    _save([a for a in items if a["id"] != asset_id])
    if target.get("sample"):
        try:
            (SAMPLES_DIR / target["sample"]).unlink(missing_ok=True)
        except OSError:
            pass
    return True


def set_minimax_voice(asset_id: str, voice_id: str) -> dict | None:
    """记录资产对应的 MiniMax 服务端克隆音色 ID。"""
    items = _load()
    for a in items:
        if a["id"] == asset_id:
            a["minimax_voice_id"] = voice_id
            _save(items)
            return _public(a)
    return None


def resolve_tts_voice(provider: str, model: str, voice: str) -> tuple[str, str]:
    """TTS 音色解析：音色资产 → 各厂商的稳定复用形态。

    voice 命中资产时返回改写后的 (model, voice)；普通音色 ID / 未识别协议原样返回。
    - MiniMax（speech 系列）：资产带 minimax_voice_id → 直接用服务端克隆音色
    - 小米：带样本 → voiceclone 协议（样本 base64 锚定）；仅描述 → voicedesign
    样本文件缺失时回退描述（design），再缺失则原样返回。
    """
    if _is_minimax(provider, model):
        asset = find_asset(voice)
        if asset is not None and asset.get("minimax_voice_id"):
            return model, asset["minimax_voice_id"]
        return model, voice
    is_xiaomi = provider == "xiaomi" or (model or "").strip().lower().startswith("mimo-")
    if not is_xiaomi:
        return model, voice
    asset = find_asset(voice)
    if asset is None:
        return model, voice
    if asset.get("sample"):
        path = SAMPLES_DIR / asset["sample"]
        if path.exists():
            mime = SAMPLE_MIME.get(path.suffix.lower()) or (
                mimetypes.guess_type(str(path))[0] or "audio/mpeg"
            )
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            return "mimo-v2.5-tts-voiceclone", f"data:{mime};base64,{b64}"
    if asset.get("desc"):
        return "mimo-v2.5-tts-voicedesign", asset["desc"]
    return model, voice


def _is_minimax(provider: str, model: str) -> bool:
    """是否按 MiniMax t2a_v2 协议解析：minimax 厂商，或 speech-* 模型 ID。"""
    if provider == "minimax":
        return True
    return (model or "").strip().lower().startswith("speech-")
