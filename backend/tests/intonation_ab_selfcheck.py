"""疑问句升调 A/B 自检：克隆音色 vs 系统音色，情绪开/关。

同一句问句四种组合合成，存 data/temp/intonation_ab/，并对每条提取
基频（F0）轮廓（自相关法），对比句尾是否上扬——用数据回答
「疑问句没有升调是模型问题还是克隆音色问题」。

用法：python backend/tests/intonation_ab_selfcheck.py
"""
import asyncio
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.settings import _discover_voices  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.tts.minimax_tts import MiniMaxTTS  # noqa: E402

TEXT = "今天天气不错，去公园玩？"
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "temp" / "intonation_ab"


def mp3_to_pcm(mp3_path: Path) -> tuple[list[float], int]:
    """mp3 → 8k 单声道 float（借 ffmpeg），返回 (samples, sr)。8k 对 60~450Hz F0 足够。"""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(mp3_path), "-ar", "8000", "-ac", "1", str(wav_path)],
        capture_output=True, timeout=60, check=True,
    )
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    wav_path.unlink(missing_ok=True)
    samples = [b / 32768.0 for b in wave.struct.unpack(f"<{len(raw)//2}h", raw)]
    return samples, sr


def f0_contour(samples: list[float], sr: int, win_s: float = 0.05, hop_s: float = 0.02,
               fmin: float = 60.0, fmax: float = 450.0) -> list[float]:
    """逐帧自相关 F0（纯 Python）；无声/浊度不足帧记 None。"""
    win = int(win_s * sr)
    hop = int(hop_s * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    out: list[float] = []
    for start in range(0, max(1, len(samples) - win), hop):
        x = samples[start:start + win]
        mean = sum(x) / len(x)
        x = [v - mean for v in x]
        energy = sum(v * v for v in x)
        if energy < 1e-6:
            out.append(None)
            continue
        best_lag, best_r = 0, 0.0
        for lag in range(lo, min(hi, len(x) - 1)):
            dot = sum(x[i] * x[i + lag] for i in range(len(x) - lag))
            r = dot / (energy + 1e-12)
            if r > best_r:
                best_r, best_lag = r, lag
        out.append(sr / best_lag if best_lag and best_r > 0.35 else None)
    return out


def analyze(mp3_path: Path) -> None:
    samples, sr = mp3_to_pcm(mp3_path)
    f0 = f0_contour(samples, sr)
    voiced = [f for f in f0 if f is not None]
    if len(voiced) < 5:
        print("    F0: 有效浊音帧不足，跳过")
        return
    n = len(f0)
    head = voiced[: max(1, int(n * 0.15))]  # 句头 15%
    tail = voiced[-max(1, int(n * 0.20)):]  # 句尾 20%
    med = sorted(voiced)[len(voiced) // 2]
    head_m = sorted(head)[len(head) // 2]
    tail_m = sorted(tail)[len(tail) // 2]
    rise = tail_m - med
    print(f"    F0 中位 {med:.0f}Hz | 句头 {head_m:.0f}Hz | 句尾 {tail_m:.0f}Hz | "
          f"句尾相对中位 {rise:+.0f}Hz | 范围 {min(voiced):.0f}~{max(voiced):.0f}Hz")


async def main() -> None:
    cred = registry._resolve_credential(None, "minimax")
    print(f"凭据: {cred.get('label') or cred.get('id')}")
    tts = MiniMaxTTS(cred["api_key"], cred.get("base_url") or "https://api.minimax.cn", cred.get("group_id"))
    voices, err = _discover_voices(cred)
    system = [v for v in voices if v["source"] == "system"]
    if err:
        print(f"音色发现失败（{err}），回退 male-qn-qingse")
    def label_of(v: dict) -> str:
        lab = v.get("label")
        return v["id"] + (json.dumps(lab, ensure_ascii=False) if not isinstance(lab, str) else lab)

    prefer = [v for v in system if any(k in label_of(v).lower() for k in ("男", "male", "qingse", "jingying"))]
    sys_voice = (prefer or system or [{"id": "male-qn-qingse", "label": "回退"}])[0]
    print(f"对照系统音色: {sys_voice['id']} ({sys_voice['label']})")

    clone_voice = "ttv-voice-2026092108541526-1mdQm18x"  # 孙悟空（voice_da8bb415）
    cases = [
        ("A_克隆_平叙问句", clone_voice, "neutral"),
        ("B_克隆_surprised", clone_voice, "surprised"),
        ("C_系统_平叙问句", sys_voice["id"], "neutral"),
        ("D_系统_surprised", sys_voice["id"], "surprised"),
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, voice, emotion in cases:
        out = OUT_DIR / f"{name}.mp3"
        audio = await tts.synthesize(text=TEXT, model="speech-2.8-hd", voice=voice,
                                     speed=1.0, emotion=emotion)
        out.write_bytes(audio)
        print(f"{name}: {out.name}（{len(audio)} 字节）")
        analyze(out)


if __name__ == "__main__":
    asyncio.run(main())
