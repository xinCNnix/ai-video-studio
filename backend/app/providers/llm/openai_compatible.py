"""OpenAI 兼容 Chat Completions 适配器（OpenAI / MiniMax / 任意兼容厂商）。"""
import re

import httpx

from ..base import LLMProvider, ProviderError


def _strip_think(text: str) -> str:
    """剥离推理模型输出中的 <think> 思考过程。"""
    if "<think>" not in text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in text:  # 未闭合（被 max_tokens 截断）：只保留思考之后的文本
        text = text.rsplit("<think>", 1)[-1]
    return text.strip()


class OpenAICompatibleLLM(LLMProvider):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")

    async def generate(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        if not self.base_url:
            raise ProviderError("LLM Base URL 未配置，请在设置中填写 Base URL。")
        body: dict = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"LLM 请求失败：{exc}") from exc
        if resp.status_code >= 400:
            text = resp.text[:300]
            if "unknown model" in resp.text.lower() or "not found" in resp.text.lower():
                raise ProviderError(
                    f"模型 ID「{model}」不被当前端点识别。请核对模型名与厂商完全一致；"
                    "也可在「设置 → 自动发现」里点击模型名测试可用性。厂商原始信息：" + text
                )
            raise ProviderError(f"LLM 返回 {resp.status_code}：{text}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"LLM 响应格式异常：{str(data)[:300]}") from exc
        return _strip_think(content or "")
