"""OpenAI 生图适配器：generations + gpt-image-1 的 edits（支持参考图）。"""
import base64

import httpx

from ..base import ImageProvider, ProviderError

# 比例 -> 尺寸映射（用户不需要填像素）
SIZES_GPT_IMAGE = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
}
SIZES_DALLE3 = {
    "1:1": "1024x1024",
    "3:4": "1024x1792",
    "4:3": "1792x1024",
    "9:16": "1024x1792",
    "16:9": "1792x1024",
}


class OpenAIImageProvider(ImageProvider):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def generate(
        self,
        prompt: str,
        model: str,
        size: str | None = None,
        n: int = 1,
        references: list[tuple[str, bytes]] | None = None,
    ) -> list[bytes]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=300) as client:
            try:
                if references and model == "gpt-image-1":
                    files = []
                    for i, (filename, content) in enumerate(references[:4]):
                        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
                        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
                        files.append(
                            ("image[]", (filename, content, f"image/{mime}"))
                        )
                    form = {
                        "model": (None, model),
                        "prompt": (None, prompt),
                        "n": (None, str(n)),
                    }
                    if size and size in SIZES_GPT_IMAGE.values():
                        form["size"] = (None, size)
                    resp = await client.post(
                        f"{self.base_url}/images/edits", headers=headers, data=form, files=files
                    )
                else:
                    body: dict = {"model": model, "prompt": prompt, "n": n}
                    mapped = size or SIZES_GPT_IMAGE["1:1"]
                    body["size"] = mapped
                    if model.startswith("dall-e"):
                        body["response_format"] = "b64_json"
                    resp = await client.post(
                        f"{self.base_url}/images/generations", headers=headers, json=body
                    )
            except httpx.HTTPError as exc:
                raise ProviderError(f"生图请求失败：{exc}") from exc
            if resp.status_code >= 400:
                raise ProviderError(f"生图返回 {resp.status_code}：{resp.text[:300]}")
            data = resp.json()
            results: list[bytes] = []
            for item in data.get("data", []):
                if item.get("b64_json"):
                    results.append(base64.b64decode(item["b64_json"]))
                elif item.get("url"):
                    dl = await client.get(item["url"])
                    dl.raise_for_status()
                    results.append(dl.content)
            if not results:
                raise ProviderError("生图返回为空。")
            return results
