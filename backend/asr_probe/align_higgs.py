"""Higgs TTS 本地合成音频的可懂度校验：强制对齐 zh_test.wav vs 原文。

判定逻辑（同 align_scene3.py）：逐字念对 → 时间戳单调递增、宽度合理；
念错/含糊 → 对不上的字塌缩为 0 宽/乱序时间戳。对照组 = 音频统计指标。
"""
import torch

from qwen_asr import Qwen3ForcedAligner

WAV = r"C:\Users\Administrator\Documents\video_gener_agent\models\higgs\zh_test.wav"
TEXT = "大家好，欢迎来到我的频道，今天我们聊一聊人工智能。"


def main():
    model = Qwen3ForcedAligner.from_pretrained(
        "Qwen/Qwen3-ForcedAligner-0.6B", dtype=torch.float32
    )
    res = model.align(WAV, TEXT, "Chinese")[0]
    items = res.items
    zero = sum(1 for it in items if it.end_time - it.start_time < 0.02)
    back = sum(1 for a, b in zip(items, items[1:]) if b.start_time < a.start_time - 0.01)
    total = items[-1].end_time - items[0].start_time if items else 0
    print(f"字数={len(items)} 零宽/近零宽={zero} 时戳回退={back} 覆盖时长={total:.2f}s")
    print(" ".join(f"{it.text}({it.start_time:.2f}-{it.end_time:.2f})" for it in items))


if __name__ == "__main__":
    main()
