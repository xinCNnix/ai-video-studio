"""TTS 语音生成任务执行服务。长文本自动分段合成后拼接（MP3 帧可直接拼接）。"""
import asyncio
import re
import subprocess

from ..config import voice_assets
from ..jobs import job_manager
from ..providers import registry
from . import file_service, self_check

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


async def run_voice_job(job_id: str, provider: str, model: str, credential_id: str | None,
                        text: str, voice: str, speed: float, emotion: str) -> None:
    jm = job_manager
    jm.update(job_id, status="generating")
    if not text or not text.strip():
        jm.update(job_id, status="failed", error="配音文本为空，请检查剧本行是否为空行。")
        return
    tts = registry.get_tts(credential_id, provider_hint=provider)
    # 音色资产解析：命中的固化音色自动切换到克隆协议（样本锚定，稳定复用）
    model, voice = voice_assets.resolve_tts_voice(provider, model, voice)
    chunks = split_text(text)
    audio = b""
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if total > 1:
            jm.update(job_id, note=f"分段合成 {i + 1}/{total}")
        audio += await tts.synthesize(text=chunk, model=model, voice=voice, speed=speed, emotion=emotion)
    jm.update(job_id, status="downloading")
    url = await file_service.save_output("audio", audio)
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
    est_seconds = exact_seconds if exact_seconds else round(self_check.estimate_audio_seconds(audio), 1)
    jm.update(
        job_id,
        status="completed",
        output=url,
        outputs=[url],
        duration_seconds=est_seconds,
        self_check=self_check.check_audio(audio),
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
