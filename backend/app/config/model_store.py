"""自定义模型管理：data/config/custom_models.json（无敏感信息，明文存储）。"""
import json

from . import settings

FILE = settings.CONFIG_DIR / "custom_models.json"
CAPABILITIES = ("llm", "tts", "image", "video", "voice")


def _empty() -> dict:
    return {c: [] for c in CAPABILITIES}


def _load() -> dict:
    if not FILE.exists():
        return _empty()
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    return {c: data.get(c, []) for c in CAPABILITIES}


def _save(data: dict) -> None:
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_custom() -> dict:
    return _load()


def add_model(capability: str, provider: str, model_id: str) -> dict:
    if capability not in CAPABILITIES:
        raise ValueError(f"capability 必须是 {CAPABILITIES}")
    model_id = model_id.strip()
    if not model_id:
        raise ValueError("model_id 不能为空")
    data = _load()
    entry = {"id": model_id, "label": model_id, "provider": provider}
    if not any(m["id"] == model_id and m["provider"] == provider for m in data[capability]):
        data[capability].append(entry)
        _save(data)
    return entry


def remove_model(capability: str, provider: str, model_id: str) -> bool:
    data = _load()
    before = len(data[capability])
    data[capability] = [
        m for m in data[capability] if not (m["id"] == model_id and m["provider"] == provider)
    ]
    _save(data)
    return len(data[capability]) < before
