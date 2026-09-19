"""Nano Banana（Gemini 2.5 Flash Image）适配器：generateContent 私有协议。

强项是多参考图编辑：references 会作为 inline_data 随文本一起提交。
Gemini 不接受尺寸参数，请求的 size 会被忽略（按模型默认输出）。
"""
import base64
import httpx

from ..base import ImageProvider, ProviderError

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiImageProvider(ImageProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    async def generate(self, prompt: str, model: str, size: str | None = None,
                       n: int = 1, references=None) -> list[bytes]:
        parts: list[dict] = [{"text": prompt}]
        for _, content in (references or [])[:6]:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(content).decode("ascii"),
                }
            })
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    f"{self.base_url}/models/{model}:generateContent",
                    json=body,
                    headers={"x-goog-api-key": self.api_key},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini 请求失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Gemini 返回 {resp.status_code}：{resp.text[:300]}")
        images: list[bytes] = []
        try:
            for part in resp.json()["candidates"][0]["content"]["parts"]:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    images.append(base64.b64decode(inline["data"]))
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Gemini 响应格式异常：{resp.text[:300]}") from exc
        if not images:
            raise ProviderError("Gemini 未返回图片（可能被安全策略拦截）。")
        return images[:n] if n > 1 else images
