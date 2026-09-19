"""FLUX（Black Forest Labs 官方 API）图片适配器：提交拿 polling_url，轮询取图。"""
import httpx

from ..base import ImageProvider, ProviderError

DEFAULT_BASE = "https://api.bfl.ai/v1"


class BFLImageProvider(ImageProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    async def generate(self, prompt: str, model: str, size: str | None = None,
                       n: int = 1, references=None) -> list[bytes]:
        body: dict = {"prompt": prompt, "output_format": "jpeg"}
        if size and "x" in size:
            w, h = size.split("x", 1)
            if w.isdigit() and h.isdigit():
                body["width"], body["height"] = int(w), int(h)
        # FLUX 官方 API 不支持参考图（图生图走 kontext 系列，可在模型 ID 里自定义）
        headers = {"x-key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{self.base_url}/{model}", json=body, headers=headers)
                if resp.status_code >= 400:
                    raise ProviderError(f"FLUX 提交返回 {resp.status_code}：{resp.text[:300]}")
                task = resp.json()
                polling_url = task.get("polling_url") or f"{self.base_url}/get_result?id={task.get('id')}"
                # 轮询（生成一般 3-10 秒）
                for _ in range(60):
                    poll = await client.get(polling_url, headers=headers)
                    if poll.status_code >= 400:
                        raise ProviderError(f"FLUX 轮询返回 {poll.status_code}：{poll.text[:200]}")
                    data = poll.json()
                    status = data.get("status")
                    if status == "Ready":
                        sample = (data.get("result") or {}).get("sample")
                        if not sample:
                            raise ProviderError("FLUX 完成但未返回图片地址")
                        dl = await client.get(sample, timeout=120)
                        dl.raise_for_status()
                        return [dl.content]
                    if status in ("Error", "Content Moderated"):
                        raise ProviderError(f"FLUX 生成失败：{status}")
                    import asyncio as _aio
                    await _aio.sleep(2)
                raise ProviderError("FLUX 生成超时")
        except httpx.HTTPError as exc:
            raise ProviderError(f"FLUX 请求失败：{exc}") from exc
