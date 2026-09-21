"""faster-whisper small 转写：第3镜成片音轨 + 用户上传台词 mp3（对照组）。"""
from faster_whisper import WhisperModel

VIDEO_WAV = r"C:\Users\Administrator\Documents\video_gener_agent\data\temp\scene3_asr.wav"
MP3_L1 = r"C:\Users\Administrator\Documents\video_gener_agent\data\uploads\audio\438dc2d4_04还要看看白斑面积有没有变化，其他地方有没有新出现的白斑。.mp3"
MP3_L2 = r"C:\Users\Administrator\Documents\video_gener_agent\data\uploads\audio\90689a0e_05明白了，不能只盯着边界，还得看一段时间里的整体变化！.mp3"
BASELINE = {
    VIDEO_WAV: "（成片音轨·待比对）",
    MP3_L1: "还要看看白斑面积有没有变化，其他地方有没有新出现的白斑。",
    MP3_L2: "明白了，不能只盯着边界，还得看一段时间里的整体变化！",
}

model = WhisperModel("Systran/faster-whisper-small", device="cpu", compute_type="int8")
for path, ref in BASELINE.items():
    segs, info = model.transcribe(path, language="zh", vad_filter=False, word_timestamps=True)
    print("=" * 20, path.rsplit("\\", 1)[-1][:44])
    print("基准:", ref)
    for s in segs:
        print(f"  [{s.start:5.2f}-{s.end:5.2f}] {s.text}")
