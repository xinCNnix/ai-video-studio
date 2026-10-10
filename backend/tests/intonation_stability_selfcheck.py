"""疑问句升调稳定性测试：裸? / 加吗 / 整句情绪，各 3 次，看句尾升幅。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intonation_ab_selfcheck import OUT_DIR, MiniMaxTTS, f0_contour, mp3_to_pcm  # noqa: E402

from app.providers import registry  # noqa: E402


def tail_rise(path: Path) -> tuple[float, float]:
    samples, sr = mp3_to_pcm(path)
    f0 = [f for f in f0_contour(samples, sr) if f is not None]
    n = len(f0)
    k = max(1, int(n * 0.2))
    tail = sorted(f0[-k:])[k // 2]
    med = sorted(f0)[n // 2]
    return med, tail - med


async def main() -> None:
    cred = registry._resolve_credential(None, "minimax")
    tts = MiniMaxTTS(cred["api_key"], cred.get("base_url") or "https://api.minimax.cn", cred.get("group_id"))
    clone = "ttv-voice-2026092108541526-1mdQm18x"
    groups = [
        ("bare_q_neutral", "今天天气不错，去公园玩？", "neutral"),
        ("ma_neutral", "今天天气不错，要去公园玩吗？", "neutral"),
        ("whole_surprised", "今天天气不错，去公园玩？", "surprised"),
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text, emo in groups:
        rises = []
        for i in range(3):
            out = OUT_DIR / f"{name}_{i + 1}.mp3"
            out.write_bytes(await tts.synthesize(text=text, model="speech-2.8-hd", voice=clone, speed=1.0, emotion=emo))
            med, rise = tail_rise(out)
            rises.append(rise)
            print(f"  {name} #{i + 1}: 中位 {med:.0f}Hz, 句尾相对 {rise:+.0f}Hz")
        print(f"{name}: 平均 {sum(rises) / len(rises):+.0f}Hz")


if __name__ == "__main__":
    asyncio.run(main())
