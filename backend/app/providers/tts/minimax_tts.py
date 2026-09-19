"""MiniMax TTS (t2a_v2) 适配器。返回 mp3 音频字节。

声音克隆走独立的两步接口（一次性服务端克隆，之后 voice_id 永久复用）：
- POST /v1/files/upload 上传样本（purpose=voice_clone）→ file.file_id
- POST /v1/voice_clone 用 file_id 换取自定义 voice_id
克隆出的 voice_id 与系统音色同构，直接放进 t2a_v2 的 voice_setting.voice_id，
且 speed/emotion 结构化字段对克隆音色同样生效。
"""
import asyncio
import mimetypes
from pathlib import Path

import httpx

from ..base import TTSProvider, ProviderError

# 瞬时错误重试：网络抖动 / 5xx / 429 限流。4xx 参数类错误不重试（重试也不会成功）。
_RETRY_DELAYS = (3, 15, 30)


class MiniMaxTTS(TTSProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.minimax.cn", group_id: str | None = None):
        base = (base_url or "https://api.minimax.cn").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]  # TTS/音色等接口自带 /v1 前缀，避免拼成 /v1/v1
        self.api_key = api_key
        self.base_url = base
        self.group_id = group_id or None

    async def clone_voice(
        self, sample_path: Path, voice_id: str, preview_text: str = "", models: list[str] | None = None
    ) -> dict:
        """把本地样本克隆为 MiniMax 服务端音色，返回克隆接口响应。

        voice_id 由调用方自定义（全局唯一）；preview_text 非空时 MiniMax 会
        同步合成一段试听音频用于验证克隆效果。
        model 为必填且按账号白名单校验：依次尝试候选（model not allowed 才换下一个，
        其余错误如实抛出），全部被拒时抛最后一个错误。
        """
        if not sample_path.exists():
            raise ProviderError(f"样本文件不存在：{sample_path}")
        mime = mimetypes.guess_type(str(sample_path))[0] or "audio/mpeg"
        params = {"GroupId": self.group_id} if self.group_id else None
        auth = {"Authorization": f"Bearer {self.api_key}"}
        candidates = models or ["speech-2.8-hd", "speech-2.5-hd-preview", "speech-02-hd"]
        async with httpx.AsyncClient(timeout=180) as client:
            with open(sample_path, "rb") as f:
                try:
                    resp = await client.post(
                        f"{self.base_url}/v1/files/upload",
                        data={"purpose": "voice_clone"},
                        files={"file": (sample_path.name, f, mime)},
                        params=params,
                        headers=auth,
                    )
                except httpx.HTTPError as exc:
                    raise ProviderError(f"样本上传失败：{str(exc)[:200]}") from exc
            if resp.status_code >= 400:
                raise ProviderError(f"样本上传失败（HTTP {resp.status_code}）：{resp.text[:200]}")
            try:
                file_id = ((resp.json() or {}).get("file") or {}).get("file_id")
            except ValueError as exc:
                raise ProviderError("样本上传响应不是合法 JSON。") from exc
            if not file_id:
                raise ProviderError("样本上传成功但未返回 file_id。")
            last_model_err: ProviderError | None = None
            for model in candidates:
                body: dict = {"file_id": file_id, "voice_id": voice_id, "model": model}
                if preview_text:
                    body["text"] = preview_text
                try:
                    resp = await client.post(
                        f"{self.base_url}/v1/voice_clone",
                        json=body,
                        params=params,
                        headers={**auth, "Content-Type": "application/json"},
                        timeout=300,  # 克隆需分析样本，耗时明显长于普通合成
                    )
                except httpx.HTTPError as exc:
                    raise ProviderError(f"克隆请求失败：{str(exc)[:200]}") from exc
                if resp.status_code >= 400:
                    err = ProviderError(f"克隆失败（HTTP {resp.status_code}）：{resp.text[:200]}")
                else:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise ProviderError("克隆响应不是合法 JSON。") from exc
                    base_resp = data.get("base_resp") or {}
                    if base_resp.get("status_code", 0) == 0:
                        return data
                    err = ProviderError(f"克隆失败：{base_resp.get('status_msg') or '未知错误'}")
                if "model not allowed" in str(err).lower():
                    last_model_err = err  # 模型不在账号白名单：换下一个候选
                    continue
                raise err
        raise last_model_err or ProviderError("克隆失败：所有候选模型均被拒绝。")

    async def synthesize(
        self,
        text: str,
        model: str,
        voice: str,
        speed: float = 1.0,
        emotion: str = "neutral",
    ) -> bytes:
        params = {"GroupId": self.group_id} if self.group_id else None
        voice_setting: dict = {"voice_id": voice, "speed": max(0.5, min(2.0, speed)), "vol": 1.0, "pitch": 0}
        if emotion and emotion != "neutral":
            voice_setting["emotion"] = emotion
        body = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        }
        last_err: Exception | None = None
        for attempt, delay in enumerate((0, *_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        f"{self.base_url}/v1/t2a_v2",
                        json=body,
                        params=params,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
            except httpx.HTTPError as exc:
                last_err = ProviderError(f"TTS 请求失败（第 {attempt + 1} 次）：{exc}")
                continue  # 网络类错误一律可重试
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = ProviderError(f"TTS 返回 {resp.status_code}（第 {attempt + 1} 次）：{resp.text[:200]}")
                continue  # 限流/服务端瞬时错误可重试
            if resp.status_code >= 400:
                raise ProviderError(f"TTS 返回 {resp.status_code}：{resp.text[:300]}")
            data = resp.json()
            base_resp = data.get("base_resp") or {}
            if base_resp.get("status_code", 0) != 0:
                msg = str(base_resp.get("status_msg", "未知错误"))
                if "rate limit" in msg.lower():
                    last_err = ProviderError(f"TTS 错误（第 {attempt + 1} 次）：{msg}")
                    continue
                raise ProviderError(f"TTS 错误：{msg}")
            break
        else:
            raise last_err or ProviderError("TTS 多次重试后仍失败。")
        audio_hex = (data.get("data") or {}).get("audio")
        if not audio_hex:
            raise ProviderError("TTS 未返回音频数据。")
        try:
            return bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise ProviderError("TTS 音频数据解码失败。") from exc
