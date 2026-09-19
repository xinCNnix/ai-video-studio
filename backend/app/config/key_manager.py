"""API Key 管理：整体 JSON 使用 AES-256-GCM 加密保存到 data/config/providers.enc。"""
import json
import os
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import settings

SECRET_FILE = settings.CONFIG_DIR / "secret.key"
PROVIDERS_FILE = settings.CONFIG_DIR / "providers.enc"


class KeyNotFound(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_secret() -> bytes:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    key = os.urandom(32)
    SECRET_FILE.write_bytes(key)
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return key


def _encrypt(data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_get_secret()).encrypt(nonce, data, None)


def _decrypt(blob: bytes) -> bytes:
    return AESGCM(_get_secret()).decrypt(blob[:12], blob[12:], None)


def _load() -> dict:
    if not PROVIDERS_FILE.exists():
        return {"keys": []}
    try:
        return json.loads(_decrypt(PROVIDERS_FILE.read_bytes()))
    except Exception:
        return {"keys": []}


def _save(data: dict) -> None:
    PROVIDERS_FILE.write_bytes(_encrypt(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")))


def _mask(api_key: str) -> str:
    tail = api_key[-4:] if len(api_key) >= 4 else api_key
    return "••••••" + tail


def _public(key: dict) -> dict:
    return {
        "id": key["id"],
        "provider": key["provider"],
        "name": key["name"],
        "masked_key": _mask(key["api_key"]),
        "base_url": key.get("base_url") or "",
        "group_id": key.get("group_id") or "",
        "is_default": key.get("is_default", False),
        "created_at": key.get("created_at"),
    }


def list_keys() -> list[dict]:
    return [_public(k) for k in _load()["keys"]]


def add_key(provider: str, name: str, api_key: str, base_url: str | None = None, group_id: str | None = None) -> dict:
    data = _load()
    if not any(k["provider"] == provider for k in data["keys"]):
        is_default = True
    else:
        is_default = False
    key = {
        "id": f"key_{uuid.uuid4().hex[:8]}",
        "provider": provider,
        "name": name or "Default",
        "api_key": api_key,
        "base_url": base_url or "",
        "group_id": group_id or "",
        "is_default": is_default,
        "created_at": _now(),
    }
    data["keys"].append(key)
    _save(data)
    return _public(key)


def delete_key(key_id: str) -> None:
    data = _load()
    before = len(data["keys"])
    data["keys"] = [k for k in data["keys"] if k["id"] != key_id]
    if len(data["keys"]) == before:
        raise KeyNotFound(key_id)
    # 若删除的是默认 Key，将剩余同厂商第一个设为默认
    if not any(k.get("is_default") for k in data["keys"]) and data["keys"]:
        data["keys"][0]["is_default"] = True
    _save(data)


def set_default(key_id: str) -> dict:
    """把某把 Key 设为其厂商的默认：只影响同厂商，其他厂商的默认保持不变。"""
    data = _load()
    target = next((k for k in data["keys"] if k["id"] == key_id), None)
    if target is None:
        raise KeyNotFound(key_id)
    for k in data["keys"]:
        if k["provider"] == target["provider"]:
            k["is_default"] = k["id"] == key_id
    _save(data)
    return _public(target)


def update_key(key_id: str, name: str | None = None, base_url: str | None = None, group_id: str | None = None) -> dict:
    """编辑凭据（名称 / Base URL / Group ID），用于补填信息而无需删除重建。"""
    data = _load()
    target = next((k for k in data["keys"] if k["id"] == key_id), None)
    if target is None:
        raise KeyNotFound(key_id)
    if name is not None and name.strip():
        target["name"] = name.strip()
    if base_url is not None:
        target["base_url"] = base_url.strip()
    if group_id is not None:
        target["group_id"] = group_id.strip()
    _save(data)
    return _public(target)


def get_key(key_id: str) -> dict:
    for k in _load()["keys"]:
        if k["id"] == key_id:
            return k
    raise KeyNotFound(key_id)


def get_default_key(provider: str | None = None) -> dict:
    keys = _load()["keys"]
    if provider:
        keys = [k for k in keys if k["provider"] == provider]
    if not keys:
        raise KeyNotFound(provider or "any")
    return next((k for k in keys if k.get("is_default")), keys[0])
