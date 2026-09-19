"""MiniMax 图片适配器：/v1/image_generation（image-01 系列）。

文档：https://platform.minimaxi.com — 提交即同步返回图片，无需轮询。
图生图走 subject_reference（仅支持一张人像参考图，type=character）。
"""
import base64

import httpx

from ..base import ImageProvider, ProviderError

DEFAULT_BASE = "https://api.minimax.cn"


class MiniMaxImageProvider(ImageProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        # base_url 可能带 /v1 后缀（LLM 风格），统一剥掉再拼 /v1/image_generation
        base = (base_url or DEFAULT_BASE).rstrip("/")
        self.base_url = base[:-3] if base.endswith("/v1") else base

    async def generate(self, prompt: str, model: str, size: str | None = None,
                       n: int = 1, references=None) -> list[bytes]:
        body: dict = {"model": model, "prompt": prompt, "count": max(1, min(n, 4))}
        # 图生图：subject_reference 仅支持一张参考图，多余的被忽略
        if references:
            name, blob = references[0]
            mime = "image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            body["subject_reference"] = [{
                "type": "character",
                "image_file": f"data:{mime};base64,{base64.b64encode(blob).decode()}",
            }]
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/image_generation", json=body, headers=headers
                )
                if resp.status_code >= 400:
                    raise ProviderError(f"MiniMax 生图返回 {resp.status_code}：{resp.text[:300]}")
                data = resp.json()
                base_resp = data.get("base_resp") or {}
                if base_resp.get("status_code", 0) != 0:
                    raise ProviderError(
                        f"MiniMax 生图失败：{base_resp.get('status_msg') or base_resp}"
                    )
                # 返回格式随站点/版本不同：data.image_urls（国内站）、
                # data 为列表且元素是 URL 字符串 / {url} 对象 / base64 字符串
                raw = data.get("data")
                items: list = raw.get("image_urls") if isinstance(raw, dict) else (raw or [])
                urls: list[str] = []
                b64s: list[str] = []
                for item in items:
                    u = item if isinstance(item, str) else (item.get("url") or item.get("image_url"))
                    if not u:
                        continue
                    (urls if u.startswith("http") else b64s).append(u)
                if b64s:
                    import base64 as _b64
                    return [_b64.b64decode(s) for s in b64s]
                if not urls:
                    raise ProviderError("MiniMax 生图完成但未返回图片地址。")
                images: list[bytes] = []
                for u in urls:
                    dl = await client.get(u, timeout=120)
                    dl.raise_for_status()
                    images.append(dl.content)
                return images
        except httpx.HTTPError as exc:
            raise ProviderError(f"MiniMax 生图请求失败：{exc}") from exc
