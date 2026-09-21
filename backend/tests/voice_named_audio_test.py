"""回归测试：voice 页配音按台词命名，分镜对号全链路可用。

事故：TTS 产物 uuid 命名（a3f8b2c1d4e5.mp3），audio_semantic 提出 hex 垃圾——
素材清单显示"台词摘要：a3f8b2c1d4e5"误导 LLM，bind 台词分发全部不中进 pending。
修复：save_output 台词入文件名 / audio_semantic 识别 hex 为无语义 /
add_materials 用台词合成语义文件名（兜底旧任务）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import file_service, material_match

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{f'  {detail}' if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


# 1) 新格式：save_output 台词入文件名，audio_semantic 提出台词
async def main() -> None:
    url = await file_service.save_output("audio", b"", name_hint="哎呀！原来是这样，太神奇了~")
    name = url.rsplit("/", 1)[-1]
    check("save_output 台词入文件名", name.endswith(".mp3") and "_" in name and "哎呀原来是这样太神奇了" in name, name)
    check("新格式 audio_semantic 提出台词", material_match.audio_semantic(url) == "哎呀原来是这样太神奇了")

    # 2) hex/uuid 名识别为无语义（旧产物与任意生成文件）
    for bad in ("a3f8b2c1d4e5.mp3", "/api/files/outputs/audio/0f1e2d3c4b5a.mp3", "ABCDEF123456.wav"):
        check(f"hex 名无语义 {bad}", material_match.audio_semantic(bad) == "")

    # 3) 正常用户上传名不受影响
    check("上传名台词保留", material_match.audio_semantic("xxxx_tts_085821_哎呀！原来….mp3") == "哎呀原来")
    check("序号前缀剥离保留", material_match.audio_semantic("02台词正文.mp3") == "台词正文")

    # 4) 旧任务兜底：uuid URL + 台词 → 合成语义文件名，bind 分发可命中
    old_url = "/api/files/outputs/audio/a3f8b2c1d4e5.mp3"
    synth = material_match.synthesize_audio_filename(old_url, "医生说：要多喝水")
    check("合成文件名带台词", material_match.audio_semantic(synth) == "医生说要多喝水", synth)
    check("合成文件名保持唯一前缀", synth.startswith("a3f8b2c1d4e5_"))
    check("新格式不再重合成", material_match.synthesize_audio_filename(url, "别的台词") == name)
    check("无台词原样返回", material_match.synthesize_audio_filename(old_url, "") == "a3f8b2c1d4e5.mp3")

    # 5) 端到端：合成名素材在 bind 台词分发里命中分镜（distribute_audios 按 filename 键）
    scene_texts = [("医院走廊，医生递水", "医生说：要多喝水，每天八杯。")]
    claims, unmatched, overflow = material_match.distribute_audios([synth], scene_texts)
    check("bind 分发命中分镜", not unmatched and claims.get(0) == [synth], f"claims={claims} unmatched={unmatched}")

    # 6) hex 名（无台词兜底）不乱命中，走 unmatched 待用户定夺
    claims2, unmatched2, _ = material_match.distribute_audios(["a3f8b2c1d4e5.mp3"], scene_texts)
    check("hex 名不乱命中", unmatched2 == ["a3f8b2c1d4e5.mp3"] and not claims2)

    print(f"\n{'=' * 40}\n{'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
