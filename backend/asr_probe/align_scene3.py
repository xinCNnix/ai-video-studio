"""第3镜成片音频 vs 剧本台词强制对齐（本地 ForcedAligner，零下载）。

判定逻辑：语音若逐字念的是参考文本，每个字会拿到单调递增、时长合理的时间戳；
语音与文本不符时，对不上的字会塌缩为 0 宽/乱序时间戳。对照组=用户上传的
台词 mp3（内容已知正确）对齐同句，排除对齐器本身的误差。
"""
import os
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 对齐器已缓存，禁止联网检查更新
import torch

from qwen_asr import Qwen3ForcedAligner

VIDEO_WAV = r"C:\Users\Administrator\Documents\video_gener_agent\data\temp\scene3_asr.wav"
LINE1 = "还要看看白斑面积有没有变化，其他地方有没有新出现的白斑。"
LINE2 = "明白了，不能只盯着边界，还得看一段时间里的整体变化！"
MP3_L1 = r"C:\Users\Administrator\Documents\video_gener_agent\data\uploads\audio\438dc2d4_04还要看看白斑面积有没有变化，其他地方有没有新出现的白斑。.mp3"
MP3_L2 = r"C:\Users\Administrator\Documents\video_gener_agent\data\uploads\audio\90689a0e_05明白了，不能只盯着边界，还得看一段时间里的整体变化！.mp3"


def dump(tag, items):
    zero = sum(1 for it in items if it.end_time - it.start_time < 0.02)
    back = sum(1 for a, b in zip(items, items[1:]) if b.start_time < a.start_time - 0.01)
    print(f"\n[{tag}] 字数={len(items)} 零宽/近零宽={zero} 时戳回退={back}")
    line = []
    for it in items:
        line.append(f"{it.text}({it.start_time:.2f}-{it.end_time:.2f})")
    print(" ".join(line))


def main():
    model = Qwen3ForcedAligner.from_pretrained(
        "Qwen/Qwen3-ForcedAligner-0.6B", dtype=torch.float32
    )
    full_text = LINE1 + LINE2

    # 对照组：用户上传台词 mp3（内容已知正确）
    for path, text, tag in ((MP3_L1, LINE1, "对照·mp3台词1"), (MP3_L2, LINE2, "对照·mp3台词2")):
        res = model.align(path, text, "Chinese")[0]
        dump(tag, res.items)

    # 实验组：第3镜成片音频 vs 剧本全文台词
    res = model.align(VIDEO_WAV, full_text, "Chinese")[0]
    dump("成片音轨 vs 剧本两句话", res.items)

    # 成片音轨分段对齐（前半/后半各自对齐，防两句连排干扰）
    half1 = model.align(VIDEO_WAV, LINE1, "Chinese")[0]
    dump("成片音轨 vs 台词1单句", half1.items)
    half2 = model.align(VIDEO_WAV, LINE2, "Chinese")[0]
    dump("成片音轨 vs 台词2单句", half2.items)


if __name__ == "__main__":
    sys.exit(main())
