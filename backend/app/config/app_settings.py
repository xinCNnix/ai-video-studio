"""应用级设置：data/config/app_settings.json（非敏感配置，明文存储）。"""
import json

from . import settings

FILE = settings.CONFIG_DIR / "app_settings.json"


def _load() -> dict:
    if not FILE.exists():
        return {}
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_default_llm() -> dict | None:
    """返回 {"model": "...", "credential_id": "..."} 或 None。"""
    d = _load().get("default_llm")
    if isinstance(d, dict) and d.get("model"):
        return d
    return None


def set_default_llm(model: str, credential_id: str | None = None) -> dict:
    data = _load()
    data["default_llm"] = {"model": model.strip(), "credential_id": credential_id or None}
    _save(data)
    return data["default_llm"]
