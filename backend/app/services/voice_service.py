"""TTS 语音生成任务执行服务。

整段（无标记）：长文本自动分段合成后 MP3 帧直拼。
带工作台标记（片段情绪/音调/音量/语速/音色/停顿）：见 voice_script——
逐段独立合成 + 静音插入 + ffmpeg 重编码拼接。
"""
import asyncio
import re
import subprocess

from ..config import voice_assets
from ..jobs import job_manager
from ..providers import registry
from . import file_service, self_check, voice_script

MAX_CHUNK = 800


def split_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    """按句子边界切分长文本，每段不超过 max_len。"""
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s:
            continue
        if len(cur) + len(s) <= max_len:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            # 单句超长时硬切
            while len(s) > max_len:
                chunks.append(s[:max_len])
                s = s[max_len:]
            cur = s
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _adapt_tags_for(text: str, provider: str, model: str) -> str:
    """按厂商/模型保留或剥离 MiniMax 专属内联标签（语气词/注音）。"""
    m = (model or "").lower()
    is_minimax = provider == "minimax" or m.startswith("speech")
    return voice_script.adapt_inline_tags(
        text,
        keep_paratags=is_minimax and "2.8" in m,   # 语气词标签仅 speech-2.8 系
        keep_pinyin=is_minimax,
    )


async def run_voice_job(job_id: str, provider: str, model: str, credential_id: str | None,
                        text: str, voice: str, speed: float, emotion: str,
                        pitch: int = 0, vol: float = 1.0) -> None:
    jm = job_manager
    jm.update(job_id, status="generating")
    if not text or not text.strip():
        jm.update(job_id, status="failed", error="配音文本为空，请检查剧本行是否为空行。")
        return
    tts = registry.get_tts(credential_id, provider_hint=provider)
    # 音色资产解析：命中的固化音色自动切换到克隆协议（样本锚定，稳定复用）
    model, voice = voice_assets.resolve_tts_voice(provider, model, voice)
    defaults = {"voice": voice, "speed": speed, "emotion": emotion, "pitch": int(pitch), "vol": vol}
    parts = voice_script.parse(text)
    if voice_script.has_markers(text):
        # 分段路径：片段参数独立合成，段间停顿插静音后 ffmpeg 拼接
        speech_parts = [p for p in parts if isinstance(p, voice_script.SpeechPart)]
        total = len(speech_parts)
        segments: list[tuple[bytes, float]] = []
        idx = 0
        for p in parts:
            if isinstance(p, voice_script.PausePart):
                if segments:
                    audio, _ = segments[-1]
                    segments[-1] = (audio, p.seconds)
                continue
            if not p.text.strip():
                continue
            if total > 1:
                jm.update(job_id, note=f"分段合成 {idx + 1}/{total}")
            merged_model, seg_voice = model, p.params(defaults)["voice"]
            if seg_voice != voice:  # 片段音色可能是另一个资产：单独解析克隆协议
                merged_model, seg_voice = voice_assets.resolve_tts_voice(provider, model, seg_voice)
            params = p.params(defaults)
            audio = await tts.synthesize(
                text=_adapt_tags_for(p.text, provider, merged_model),
                model=merged_model, voice=seg_voice,
                speed=float(params["speed"]), emotion=str(params["emotion"]),
                pitch=int(params["pitch"]), vol=float(params["vol"]),
            )
            segments.append((audio, 0.0))
            idx += 1
        jm.update(job_id, note="拼接分段音频")
        audio_out = await voice_script.concat_audio(segments, reencode=True)
    else:
        # 整段路径（兼容原有行为）：长文按句分段合成后 MP3 帧直拼
        chunks = split_text(_adapt_tags_for(text, provider, model))
        audio_out = b""
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            if total > 1:
                jm.update(job_id, note=f"分段合成 {i + 1}/{total}")
            audio_out += await tts.synthesize(
                text=chunk, model=model, voice=voice, speed=speed,
                emotion=emotion, pitch=int(pitch), vol=vol,
            )
    jm.update(job_id, status="downloading")
    # 台词写入文件名：下游（素材清单/分镜 bind/编号互证）按文件名提台词对号。
    # 标记与厂商内联标签 ((laughs)/(he2)) 都不进文件名，只留可朗读正文
    plain = voice_script.adapt_inline_tags(
        voice_script.strip_markers(text).strip(), keep_paratags=False, keep_pinyin=False
    ) or text.strip()
    url = await file_service.save_output("audio", audio_out, name_hint=plain)
    local = file_service.resolve_local(url)
    exact_seconds = None
    if local and local.exists():
        try:
            # ffprobe 是同步子进程（最长 10s），放线程池执行，避免卡死事件循环
            proc = await asyncio.to_thread(
                subprocess.run,
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(local)],
                capture_output=True, text=True, timeout=10,
            )
            out = proc.stdout.strip()
            exact_seconds = round(float(out), 2)
        except Exception:  # noqa: BLE001 - ffprobe 不可用时回退估算
            exact_seconds = None
    est_seconds = exact_seconds if exact_seconds else round(self_check.estimate_audio_seconds(audio_out), 1)
    jm.update(
        job_id,
        status="completed",
        output=url,
        outputs=[url],
        duration_seconds=est_seconds,
        self_check=self_check.check_audio(audio_out),
    )


async def probe_audio_seconds(reference: str) -> float | None:
    """探测引用音频的真实时长（秒），失败返回 None。

    仅用于信息展示（如提示上传配音与设定时长是否匹配），不作为改写分镜时长的依据。
    """
    try:
        local = await file_service.materialize(reference)
    except Exception:  # noqa: BLE001 - 素材缺失/下载失败时无可探测
        return None
    if not local.exists():
        return None
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(local)],
            capture_output=True, text=True, timeout=10,
        )
        return round(float(proc.stdout.strip()), 2)
    except Exception:  # noqa: BLE001 - ffprobe 不可用/格式不支持
        return None
