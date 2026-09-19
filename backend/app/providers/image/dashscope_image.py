"""DashScope 原生图片适配器：Qwen-Image / WanX 文生图（异步任务）。"""
import httpx

from ..base import ImageProvider, ProviderError

DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"


class DashScopeImageProvider(ImageProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    async def generate(self, prompt: str, model: str, size: str | None = None,
                       n: int = 1, references=None) -> list[bytes]:
        parameters: dict = {"n": max(1, min(4, n))}
        if size and "x" in size:
            parameters["size"] = size.replace("x", "*", 1)
        body = {"model": model, "input": {"prompt": prompt}, "parameters": parameters}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/services/aigc/text2image/image-synthesis",
                    json=body,
                    headers=headers,
                )
                if resp.status_code >= 400:
                    raise ProviderError(f"DashScope 提交返回 {resp.status_code}：{resp.text[:300]}")
                task_id = (resp.json().get("output") or {}).get("task_id")
                if not task_id:
                    raise ProviderError(f"DashScope 未返回任务 ID：{resp.text[:300]}")
                # 轮询（文生图一般 5-20 秒）
                for _ in range(45):
                    poll = await client.get(f"{self.base_url}/tasks/{task_id}", headers={"Authorization": f"Bearer {self.api_key}"})
                    if poll.status_code >= 400:
                        raise ProviderError(f"DashScope 轮询返回 {poll.status_code}：{poll.text[:200]}")
                    output = poll.json().get("output") or {}
                    status = output.get("task_status")
                    if status == "SUCCEEDED":
                        results = output.get("results") or []
                        urls = [r.get("url") for r in results if r.get("url")]
                        if not urls:
                            raise ProviderError("DashScope 完成但未返回图片地址")
                        images = []
                        for u in urls:
                            dl = await client.get(u, timeout=120)
                            dl.raise_for_status()
                            images.append(dl.content)
                        return images
                    if status == "FAILED":
                        raise ProviderError(f"DashScope 生成失败：{output.get('message', '未知错误')}")
                    import asyncio as _aio
                    await _aio.sleep(2)
                raise ProviderError("DashScope 生成超时")
        except httpx.HTTPError as exc:
            raise ProviderError(f"DashScope 请求失败：{exc}") from exc
