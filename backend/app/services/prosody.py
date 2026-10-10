"""韵律评分：对合成音频做基频（F0）分析，量化「句尾收在多高 / 语调起伏」。

背景：MiniMax TTS 同参数多次合成的语调具有随机性（实测问句升调约三成命中，
用户「抽卡」数次才有一条上扬）。多版本挑优需要客观评分器：问句按收尾点
（末 5 个浊音帧 F0 中位 − 全句 F0 中位）排名——疑问句的听感核心是句尾收在
高位；陈述句按 F0 标准差（起伏幅度）排名。

实现：librosa.pyin 概率化 YIN 提 F0（16kHz，70~400Hz）。不要自研自相关/
简化 YIN——清脆少年音类音色基频弱、谐波强，自相关峰值比较会系统性锁到
半频假峰（8k 采样下曾把收尾平调误判成 +213Hz 上扬，25 文件对 pyin 真值
仅 18/25 一致）；pyin 与听感对齐，收尾点指标经用户听感校验。

ffmpeg 转 wav 供 soundfile 稳妥解码；ffmpeg/librosa 不可用或有效浊音帧
不足时返回 None（调用方回退取第 1 版）。
"""
import subprocess
import tempfile
import wave
from pathlib import Path

try:  # 依赖缺失时评分器整体降级为「不可用」，不影响合成主链路与后端启动
    import numpy as np
    import librosa
except ImportError:
    np = None
    librosa = None


def _decode(data: bytes) -> tuple[np.ndarray, int] | None:
    """mp3 字节 → (16kHz 单声道 float, sr)；失败返回 None。"""
    if librosa is None or np is None:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(data)
            mp3 = Path(tmp.name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav = Path(tmp.name)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(mp3), "-ar", "16000", "-ac", "1", str(wav)],
                capture_output=True, timeout=60,
            )
            if proc.returncode != 0 or not wav.exists():
                return None
            with wave.open(str(wav), "rb") as w:  # 仅为确保是合法 wav
                pass
            return librosa.load(str(wav), sr=16000, mono=True)
        finally:
            mp3.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError, wave.Error, ValueError):
        return None


def _voiced_f0(data: bytes) -> np.ndarray | None:
    """浊音帧 F0 序列（Hz，时序保留）；无有效浊音返回 None。"""
    loaded = _decode(data)
    if loaded is None:
        return None
    y, sr = loaded
    if len(y) < sr // 10:
        return None
    f0, _, _ = librosa.pyin(y, fmin=70.0, fmax=400.0, sr=sr,
                            frame_length=1024, hop_length=160)
    voiced = f0[~np.isnan(f0)]
    return voiced if len(voiced) >= 8 else None


def tail_rise_hz(data: bytes) -> float | None:
    """句尾收尾点：末 5 个浊音帧 F0 中位 − 全句 F0 中位（Hz）。问句挑优用。"""
    voiced = _voiced_f0(data)
    if voiced is None:
        return None
    return float(np.median(voiced[-5:]) - np.median(voiced))


def pitch_std_hz(data: bytes) -> float | None:
    """语调起伏：全句浊音帧 F0 标准差（Hz）。陈述句挑优用。"""
    voiced = _voiced_f0(data)
    if voiced is None:
        return None
    return float(np.std(voiced))


def score(data: bytes, is_question: bool) -> float | None:
    """挑优评分（越大越好）：问句看收尾点，陈述句看起伏。"""
    return tail_rise_hz(data) if is_question else pitch_std_hz(data)


def describe(scored: float | None, is_question: bool) -> str:
    """评分的人类可读描述（写进任务 note）。"""
    if scored is None:
        return "评分不可用"
    if is_question:
        return f"句尾收 {scored:+.0f}Hz"
    return f"语调起伏 ±{scored:.0f}Hz"
