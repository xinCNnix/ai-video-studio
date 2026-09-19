"""API 公共依赖。"""
from fastapi import HTTPException

from ..config import key_manager
from ..providers.base import ProviderError


def credential_or_default(credential_id: str | None, provider_hint: str | None) -> dict:
    """按 credential_id 取凭据；未传时回退到该厂商默认 Key。路由层只需捕获 ProviderError。"""
    if credential_id:
        try:
            return key_manager.get_key(credential_id)
        except key_manager.KeyNotFound as exc:
            raise ProviderError(f"凭据 {credential_id} 不存在，请重新选择。") from exc
    try:
        return key_manager.get_default_key(provider_hint)
    except key_manager.KeyNotFound as exc:
        raise ProviderError(
            f"未找到可用的 API Key（{provider_hint or "默认"}），请先在「设置 → API Keys」中添加。"
        ) from exc


def provider_error_handler(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return wrapper
