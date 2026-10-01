"""通用 OpenAI 兼容 TTS 适配器：POST {base}/audio/speech。

OpenAI 的 /audio/speech 已成为事实标准，多数 TTS 服务与自部署推理
（SiliconFlow、AllTalk、fish-speech 网关等）均兼容。音色无统一列表
接口，由用户手动输入或保存为自定义音色。
"""
import httpx

from ..base import TTSProvider, ProviderError


class OpenAITTS(TTSProvider):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def synthesize(
        self,
        text: str,
        model: str,
        voice: str,
        speed: float = 1.0,
        emotion: str = "neutral",
        pitch: int = 0,
        vol: float = 1.0,
    ) -> bytes:
        body: dict = {
            "model": model,
            "input": text,
            "voice": voice,
            "speed": max(0.25, min(4.0, speed)),
            "response_format": "mp3",
        }
        # emotion/pitch/vol 非开放标准字段：仅在用户显式设置时携带，兼容厂商
        # 读取，标准 OpenAI 端点会忽略未知字段
        if emotion and emotion != "neutral":
            body["emotion"] = emotion
        if pitch:
            body["pitch"] = max(-12, min(12, int(round(pitch))))
        if vol and abs(vol - 1.0) > 0.05:
            body["vol"] = vol
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/audio/speech",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"TTS 请求失败：{exc}") from exc
        if resp.status_code >= 400:
            # 兼容返回 JSON 错误体的厂商
            detail = resp.text[:300]
            try:
                detail = resp.json().get("error", {}).get("message") or detail
            except Exception:  # noqa: BLE001
                pass
            raise ProviderError(f"TTS 返回 {resp.status_code}：{detail}")
        if not resp.content:
            raise ProviderError("TTS 未返回音频数据。")
        return resp.content
