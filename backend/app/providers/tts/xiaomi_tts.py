"""小米 MiMo TTS 适配器（mimo-v2.5-tts 系列）。返回 mp3 音频字节。

小米开放平台的语音合成走 OpenAI 风格的 /v1/chat/completions（而非 /audio/speech）：
- 合成文本放在 role=assistant 消息；风格指令（语速/情绪）放在 role=user 消息
- 音频以 base64 返回在 choices[0].message.audio.data
- 音色设计（voicedesign）模型：voice 参数即音色文字描述，写在 user 消息里，
  由此可凭一句话描述扩展出无限自定义音色
"""
import asyncio
import base64

import httpx

from ..base import TTSProvider, ProviderError

# 瞬时错误重试：网络抖动 / 5xx / 429 限流。4xx 参数类错误不重试（重试也不会成功）。
_RETRY_DELAYS = (3, 15, 30)

# 情绪值 → 中文风格指令（小米 TTS 无独立 emotion 参数，用自然语言风格控制）
_EMOTION_STYLE = {
    "happy": "开心",
    "sad": "悲伤",
    "angry": "愤怒",
    "fearful": "恐惧",
    "surprised": "惊讶",
    "calm": "沉着平静",
}

# 预置音色（官方文档静态列表；平台无音色查询接口，配置小米凭据后由发现接口下发）
XIAOMI_PRESET_VOICES: list[dict] = [
    {"id": "mimo_default", "label": "MiMo 默认（随集群）"},
    {"id": "冰糖", "label": "冰糖（中文·女声）"},
    {"id": "茉莉", "label": "茉莉（中文·女声）"},
    {"id": "苏打", "label": "苏打（中文·男声）"},
    {"id": "白桦", "label": "白桦（中文·男声）"},
    {"id": "Mia", "label": "Mia（英文·女声）"},
    {"id": "Chloe", "label": "Chloe（英文·女声）"},
    {"id": "Milo", "label": "Milo（英文·男声）"},
    {"id": "Dean", "label": "Dean（英文·男声）"},
]

class XiaomiMiMoTTS(TTSProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.xiaomimimo.com"):
        base = (base_url or "https://api.xiaomimimo.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]  # 统一去掉 /v1 再自行拼接，兼容用户填 /v1 结尾的地址
        self.api_key = api_key
        self.base_url = base

    def _build_messages(self, text: str, model: str, voice: str, speed: float, emotion: str) -> tuple[list[dict], dict]:
        """组装 messages 与 audio 参数。返回 (messages, audio_setting)。"""
        is_design = model.endswith("voicedesign")
        is_clone = model.endswith("voiceclone")
        audio_setting: dict = {"format": "mp3"}
        if is_design:
            # voicedesign：voice 即音色描述（user 消息必填），不支持 audio.voice；
            # 风格只能走 assistant 文本内的音频标签
            tags = ""
            if emotion and emotion != "neutral" and emotion in _EMOTION_STYLE:
                tags += f"（{_EMOTION_STYLE[emotion]}）"
            if abs(speed - 1.0) > 0.05:
                tags += "（语速加快）" if speed > 1.0 else "（语速放慢）"
            return (
                [
                    {"role": "user", "content": voice or "中年男声，温和沉稳"},
                    {"role": "assistant", "content": f"{tags}{text}" if tags else text},
                ],
                audio_setting,
            )
        # 预置音色 / 克隆：风格指令写在 user 消息，合成文本保持逐字不变
        style_parts: list[str] = []
        if emotion and emotion != "neutral":
            style_parts.append(f"用{_EMOTION_STYLE.get(emotion, emotion)}的语气朗读")
        if abs(speed - 1.0) > 0.05:
            style_parts.append(f"整体语速约为正常语速的{speed:.1f}倍")
        if is_clone:
            if not (voice or "").startswith("data:audio"):
                raise ProviderError(
                    "音色克隆需要音频样本：voice 参数须为 data:audio/mpeg;base64,… 或 data:audio/wav;base64,… 格式"
                )
            audio_setting["voice"] = voice
        else:
            audio_setting["voice"] = voice or "mimo_default"
        user_content = "；".join(style_parts)
        messages: list[dict] = []
        if user_content:
            messages.append({"role": "user", "content": user_content})
        messages.append({"role": "assistant", "content": text})
        return messages, audio_setting

    async def synthesize(
        self,
        text: str,
        model: str,
        voice: str,
        speed: float = 1.0,
        emotion: str = "neutral",
    ) -> bytes:
        messages, audio_setting = self._build_messages(text, model, voice, speed, emotion)
        body = {"model": model, "messages": messages, "stream": False, "audio": audio_setting}
        data: dict = {}
        last_err: Exception | None = None
        for attempt, delay in enumerate((0, *_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
            except httpx.HTTPError as exc:
                last_err = ProviderError(f"TTS 请求失败（第 {attempt + 1} 次）：{exc}")
                continue  # 网络类错误一律可重试
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = ProviderError(f"TTS 返回 {resp.status_code}（第 {attempt + 1} 次）：{resp.text[:200]}")
                continue  # 限流/服务端瞬时错误可重试
            if resp.status_code >= 400:
                detail = resp.text[:300]
                try:
                    err = resp.json().get("error") or {}
                    detail = str(err.get("message") or detail)
                except Exception:  # noqa: BLE001
                    pass
                raise ProviderError(f"TTS 返回 {resp.status_code}：{detail}")
            data = resp.json()
            if data.get("error"):
                err = data["error"]
                msg = str(err.get("message") if isinstance(err, dict) else err)
                if "rate limit" in msg.lower():
                    last_err = ProviderError(f"TTS 错误（第 {attempt + 1} 次）：{msg}")
                    continue
                raise ProviderError(f"TTS 错误：{msg}")
            break
        else:
            raise last_err or ProviderError("TTS 多次重试后仍失败。")
        try:
            choices = data.get("choices") or []
            audio = ((choices[0].get("message") or {}).get("audio") or {}) if choices else {}
            audio_b64 = audio.get("data")
        except (IndexError, AttributeError, TypeError) as exc:
            raise ProviderError("TTS 响应结构异常（缺少 choices[0].message.audio.data）。") from exc
        if not audio_b64:
            raise ProviderError("TTS 未返回音频数据。")
        try:
            return base64.b64decode(audio_b64)
        except (ValueError, TypeError) as exc:
            raise ProviderError("TTS 音频数据解码失败。") from exc
