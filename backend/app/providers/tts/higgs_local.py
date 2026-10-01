"""本地 Higgs Audio v3 TTS（ONNX int4）适配器：subprocess 调 inference.py。

模型与运行时固定部署在 <PROJECT_ROOT>/models/higgs/（已 gitignore）：
  cpu_int4/        ONNX 子模型（manifest 驱动）
  inference.py     仓库自带推理脚本（纯 onnxruntime，无需 PyTorch）
  .venv/           独立虚拟环境（onnxruntime/numpy/soundfile/transformers）

无需 API Key。本机纯 CPU 实测 RTF≈9（生成 1 秒音频约需 9 秒），只适合
后台任务形态。输出 24kHz WAV，经 ffmpeg 转 MP3 以兼容 voice_service 的
分段裸拼接（MP3 帧可直拼）与 check_audio 的格式校验。

音色：voice 命中音色资产（带样本 + ref_text 转写）→ 声音克隆；
"default"/未命中 → 零样本（内建默认音色，无音色库可选）。
emotion 映射到 Higgs 内联情感标签（<|emotion:xxx|>，21 种），
speed 映射到语速档位标签（<|prosody:speed_*|>，4 档）。
"""
import asyncio
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ...config import settings
from ...config import voice_assets
from ..base import TTSProvider, ProviderError

HIGGS_DIR = settings.PROJECT_ROOT / "models" / "higgs"
MODEL_DIR = HIGGS_DIR / "cpu_int4"
SCRIPT = HIGGS_DIR / "inference.py"

# PROMPTING.md 全量情感标签（21 种）；应用侧情感名 → 标签
EMOTION_TAGS = {
    "affection", "amusement", "anger", "arousal", "awe", "bitterness",
    "confusion", "contemplation", "contentment", "determination", "disgust",
    "elation", "enthusiasm", "fear", "helplessness", "longing", "pride",
    "relief", "sadness", "shame", "surprise",
}
EMOTION_MAP = {
    "neutral": "",
    "happy": "elation",
    "sad": "sadness",
    "angry": "anger",
    "fearful": "fear",
    "surprised": "surprise",
    "calm": "contentment",
}

AR_CHUNK = 150          # 单次自回归的文本上限（约 30s 音频，防截断/劣化）
SECS_PER_CHAR = 0.2     # 中文播报语速估计（≈5 字/秒）
RUN_LOCK = asyncio.Lock()   # 6 核 CPU 上串行跑，防止多个 4B 会话互相拖垮


def map_emotion(emotion: str) -> str:
    """应用情感名/原始标签 → Higgs 情感标签；无法映射返回空（不加标签）。"""
    e = (emotion or "").strip().lower()
    if not e or e == "neutral":
        return ""
    if e in EMOTION_TAGS:
        return e
    return EMOTION_MAP.get(e, "")


def map_speed(speed: float) -> str:
    """连续语速值 → 最近的 Higgs 语速档位标签。"""
    s = speed if speed and speed > 0 else 1.0
    if s >= 1.4:
        return "speed_very_fast"
    if s >= 1.15:
        return "speed_fast"
    if s > 0.75:
        return ""
    if s > 0.55:
        return "speed_slow"
    return "speed_very_slow"


def split_for_ar(text: str, max_len: int = AR_CHUNK) -> list[str]:
    """按句子边界切成自回归友好的小段（长段有 80s 截断与重复退化风险）。"""
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    chunks, cur = [], ""
    for s in sentences:
        if not s:
            continue
        if len(cur) + len(s) <= max_len:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            while len(s) > max_len:      # 单句超长硬切（逗号处优先）
                cut = s.rfind("，", 0, max_len)
                cut = cut if cut > max_len // 2 else max_len
                chunks.append(s[:cut])
                s = s[cut:]
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _venv_python() -> Path:
    p = HIGGS_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return p


def _prefill_tags(emotion: str, speed: float, pitch: int = 0) -> str:
    tags = ""
    emo = map_emotion(emotion)
    if emo:
        tags += f"<|emotion:{emo}|>"
    spd = map_speed(speed)
    if spd:
        tags += f"<|prosody:{spd}|>"
    # 连续音调值 → 档位标签（Higgs 只有 pitch_low/pitch_high 两档，无音量控制）
    if pitch:
        tags += "<|prosody:pitch_high|>" if pitch > 0 else "<|prosody:pitch_low|>"
    return tags


def _resolve_voice(voice: str) -> tuple[str | None, str | None]:
    """voice → (ref_audio_path, ref_text)。命中带转写的样本资产 → 克隆；否则零样本。"""
    v = (voice or "").strip()
    if not v or v.lower() == "default":
        return None, None
    asset = voice_assets.find_asset(v)
    if asset is None or not asset.get("sample"):
        return None, None          # 未命中/纯描述资产：退化为零样本
    path = voice_assets.SAMPLES_DIR / asset["sample"]
    if not path.exists():
        return None, None
    ref_text = (asset.get("ref_text") or "").strip()
    if not ref_text:
        raise ProviderError(
            f"音色「{asset.get('name') or v}」缺少参考音频的转写文本（ref_text），"
            "Higgs 克隆必须提供样本逐字稿：保存音色资产时请一并填写。"
        )
    return str(path), ref_text


class HiggsLocalTTS(TTSProvider):
    def __init__(self) -> None:
        self.python = _venv_python()
        self.ffmpeg = "ffmpeg"

    def _check_env(self) -> None:
        missing = []
        if not self.python.exists():
            missing.append(f"{self.python}（在 models/higgs 下建 venv 并装 onnxruntime 等依赖）")
        if not (MODEL_DIR / "manifest.json").exists():
            missing.append(f"{MODEL_DIR}（cpu_int4 模型目录）")
        if not SCRIPT.exists():
            missing.append(f"{SCRIPT}")
        if missing:
            raise ProviderError("Higgs 本地模型未就绪，缺少：" + "；".join(missing))

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
        self._check_env()
        ref_audio, ref_text = _resolve_voice(voice)
        chunks = split_for_ar(text)
        if not chunks:
            raise ProviderError("配音文本为空。")
        tags = _prefill_tags(emotion, speed, pitch)

        async with RUN_LOCK:          # 串行：CPU 上一个会话已占满核
            with tempfile.TemporaryDirectory(prefix="higgs_tts_") as td:
                # 样本统一转 24kHz 单声道 WAV：inference.py 的非 24kHz 分支依赖
                # librosa（未安装，依赖链过重），且 mp3 样本直接读不可靠——ffmpeg 预处理
                ref_path = str(await self._prepare_ref(ref_audio, Path(td))) if ref_audio else None
                wavs: list[Path] = []
                for i, chunk in enumerate(chunks):
                    est_secs = max(4.0, len(chunk) * SECS_PER_CHAR)
                    out = Path(td) / f"part{i:03d}.wav"
                    await self._run_inference(tags + chunk, ref_path, ref_text, est_secs, out)
                    if not out.exists() or out.stat().st_size < 100:
                        raise ProviderError(f"Higgs 第 {i + 1} 段未产出音频文件。")
                    wavs.append(out)
                return await self._to_mp3(wavs)

    async def _prepare_ref(self, src: str, td: Path) -> Path:
        """克隆样本（mp3/wav，任意采样率）→ 24kHz 单声道 f32 WAV。"""
        out = td / "ref_24k.wav"
        proc = await asyncio.to_thread(
            subprocess.run,
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_f32le", str(out)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not out.exists():
            raise ProviderError(
                f"克隆样本解码失败：{(proc.stderr or b'')[-300:].decode('utf-8', 'ignore')}"
            )
        return out

    async def _run_inference(
        self, text: str, ref_audio: str | None, ref_text: str | None,
        est_secs: float, out: Path,
    ) -> None:
        max_frames = int(math.ceil(est_secs * 25 * 1.5)) + 40
        # 实测 RTF≈9（0.33s/帧）+ 90s 模型加载，留 50% 余量
        timeout = 120 + int(est_secs * 15)
        cmd = [
            str(self.python), str(SCRIPT),
            "--model-path", str(MODEL_DIR),
            "--text", text,
            "--out", str(out),
            "--max-frames", str(max_frames),
            "--temperature", "0.8", "--top-k", "50", "--top-p", "0.95",
        ]
        if ref_audio and ref_text:
            cmd += ["--ref-audio", ref_audio, "--ref-text", ref_text]
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
                cwd=str(HIGGS_DIR),
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"Higgs 合成超时（>{timeout}s，est {est_secs:.0f}s 音频）。") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-500:]
            raise ProviderError(f"Higgs 推理失败（exit={proc.returncode}）：{tail}")

    async def _to_mp3(self, wavs: list[Path]) -> bytes:
        """WAV(24k mono) → MP3；多段用 concat filter 一次拼接转码。"""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for w in wavs:
            cmd += ["-i", str(w)]
        if len(wavs) == 1:
            cmd += ["-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"]
        else:
            streams = "".join(f"[{i}:a]" for i in range(len(wavs)))
            cmd += [
                "-filter_complex", f"{streams}concat=n={len(wavs)}:v=0:a=1[out]",
                "-map", "[out]", "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1",
            ]
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or not proc.stdout:
            raise ProviderError(f"音频转码失败：{(proc.stderr or b'')[-300:].decode('utf-8', 'ignore')}")
        return proc.stdout
