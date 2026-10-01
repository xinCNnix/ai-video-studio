"""润色开关 + storyboard 锚点/保真修复离线回归（2026-09-22 第4集三连事故）：

1. settings.prompt_polish=False：plan 跳过导演润色遍（0 次润色调用，草稿直落盘）
2. storyboard_polish_issues：发明越肩机位检出（镜3/镜8 事故句式）、原文自带越肩
   放行、幻影角色检出（镜4：未定义角色 Lu Manchun 的肩臂入镜）
3. rewrite_aligned_prompt 锚点行角色分流：三视图=身份锚+turnaround 防复制、
   首帧=构图锚、其余=内容锚；不再统一"锚定本镜构图"（双吕满春事故根因之一）
4. 构造性注入：三视图绑定 → visual_constraints 追加防复制/肢体数约束（悟空多手
   缓解）；单人说话双人镜 → 非说话人闭嘴约束（镜11 台词张冠李戴缓解）；均幂等
5. 旧结构（非 H3）声明行格式不变

运行：.venv/Scripts/python.exe tests/polish_toggle_and_anchor_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="polish_toggle_")

from app.services import project_store  # noqa: E402
from app.services.pipeline import stages  # noqa: E402
from app.services.pipeline.stages import (  # noqa: E402
    rewrite_aligned_prompt, storyboard_polish_issues)

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


SRC_S3 = (
    "【分镜03｜换一种说法】\nDuration：7秒。配音1条，参考图3张。\n"
    "Reference 图片上传绑定：Picture 1=Image 01；Picture 2=Image 02；Picture 3=Image 07。\n"
    "首帧：Image 07。只出现吕满春与小墨，悟空位于画外左侧。\n"
    "0—1秒：吕满春掌心向下轻压一次，示意小墨暂时停下；小墨将身体转向他。\n"
    "1—5秒：摄影机平缓推进至吕满春胸像，他放下手，用自然眼神和一小次向外的手势简化解释。"
)
SRC_S5 = "【分镜05】\n0—4秒：越肩镜头，从吕满春肩后看向小墨，两人对话。\n"
SRC_S8 = (
    "【分镜08｜让开一条路】\n首帧：Image 12。四人同场。\n"
    "0—4秒：摄影机从四人关系中景轻轻向唐僧靠近。\n4—8秒：摄影机缓缓后退，把四人重新收入。\n"
)
TABLE_EP4 = {
    1: "吕满春(2).png｜真人身份、西装三视图。",
    2: "小墨(9).png｜小墨身份、帽标识、衣装背包三视图。",
    7: "吕满春右侧抬掌、小墨左侧看他。",
    3: "孙悟空2.png｜悟空身份、服装、冠饰和武器参考。",
}


def h3_prompt(defline: str, body: str) -> str:
    return (
        f"subject_definitions: {defline}\n\nsummary: [reference generation] Two-shot.\n\n"
        "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
        f"detailed_description:\n{body}\n\ncinematography: smooth.\n\n"
        "overall_soundscape:\nQuiet.\n\nnon_diegetic_music: N/A")


def main() -> None:
    # ---- 1. 润色保真闸门 ----
    cand_s3 = h3_prompt(
        "<Subject 1> is Lu Manchun in <Picture 1>. <Subject 2> is Xiao Mo in <Picture 2>.",
        "[Shot 1] 0.0-1.0s | Over-shoulder medium shot from a slightly elevated angle: "
        "the camera starts positioned behind <Subject 1>'s right shoulder, looking past him "
        "toward <Subject 2>, who turns toward him.")
    issues = storyboard_polish_issues(cand_s3, SRC_S3)
    check("镜3 事故句式：发明越肩机位检出",
          any("越肩" in i for i in issues), f"{issues}")

    check("原文自带越肩（镜5）：放行",
          storyboard_polish_issues(
              h3_prompt("<Subject 1> is Lu Manchun in <Picture 1>.",
                        "[Shot 1] Over-shoulder shot: the camera looks from behind Lu Manchun "
                        "at <Subject 2> while they talk."),
              SRC_S5) == [], "应无问题")

    cand_s8 = h3_prompt(
        "<Subject 1> is Lu Manchun in <Picture 1>. <Subject 2> is Xiao Mo in <Picture 2>. "
        "<Subject 3> is Sun Wukong in <Picture 3>. <Subject 4> is Tang Seng in <Picture 4>.",
        "[Shot 1] 0.0-4.0s | Over-shoulder push-in toward <Subject 4> (Tang Seng): "
        "the camera starts from a medium shot of all four and pushes in; <Subject 3> shifts "
        "aside holding his staff (Ruyi Jingu Bang) firmly at his side.")
    issues8 = storyboard_polish_issues(cand_s8, SRC_S8)
    check("镜8 事故句式：四人镜凭空 over-shoulder 检出、已定义角色不误报幻影",
          any("越肩" in i for i in issues8)
          and not any("幻影" in i for i in issues8), f"{issues8}")

    cand_s4 = h3_prompt(
        "<Subject 1> is the 3D character Sun Wukong in <Picture 1>.",
        "[Shot 1] 0.0-2.0s | Static medium close-up on <Subject 1>. To the right of frame, "
        "the shoulder and arm of an off-screen figure (Lu Manchun) are just visible, "
        "providing the point of his gaze.")
    issues4 = storyboard_polish_issues(cand_s4, "【分镜04】首帧：Image 08。只出现悟空。\n0—2秒：悟空眼神收紧。")
    check("镜4 事故句式：幻影角色（未定义 Lu Manchun 肩臂入镜）检出",
          any("幻影" in i and "Lu Manchun" in i for i in issues4), f"{issues4}")

    # ---- 1b. 台词时段对齐（镜11 事故：Audio 0-4s，正文 2.0-4.0s 段才开口）----
    def speech_cand(speak_seg: str) -> str:
        segs = [
            "0.0-2.0s | The hand closest to <Subject 2> lifts a few centimeters from his knee, "
            "palm pressing down once to signal silence; both stay silent.",
            "2.0-4.0s | <Subject 1> (S1) delivers the spoken line of <Audio 1> with lip "
            "synchronization, his voice low; <Subject 2> lowers the scanning panel.",
            "4.0-5.0s | No dialogue. <Subject 2> gives a single nod.",
            "5.0-6.0s | Both hold the upward gaze.",
        ]
        body = "\n".join(segs)
        if speak_seg == "first":
            body = ("0.0-2.0s | <Subject 1> (S1) delivers the spoken line of <Audio 1> with lip "
                    "synchronization, his voice low, while pressing his palm down once.\n"
                    "2.0-4.0s | <Subject 2> lowers the scanning panel; the line continues.\n"
                    "4.0-6.0s | No dialogue. <Subject 2> gives a single nod.")
        return (h3_prompt("<Subject 1> is the real person Lu Manchun in <Picture 1>. "
                          "<Subject 2> is the 3D character Xiao Mo in <Picture 2>.", body)
                + "\nAudio Timeline:\n0.0-4.0s | <Audio 1> | <Subject 1> (S1): \"先别出声。记下位置。\"\n"
                  "4.0-6.0s | No dialogue.")

    issues_t = storyboard_polish_issues(speech_cand("2.0-4.0s"), SRC_S3)
    check("镜11 事故结构：台词窗 0.0s 起、正文 2.0s 段才开口 → 检出错位",
          any("台词时段错位" in i and "2.0" in i for i in issues_t), f"{issues_t}")
    check("首段即开口（对齐）无错位问题",
          not any("台词时段错位" in i for i in storyboard_polish_issues(speech_cand("first"), SRC_S3)))

    # ---- 1c. 台词时段对齐=基础操作：确定性修复（不依赖润色/闸门）----
    from app.services.pipeline.stages import align_speech_segments
    bad = speech_cand("2.0-4.0s")
    fixed_prompt, changed = align_speech_segments(bad)
    check("错位 prompt 被确定性修复（changed=True）", changed, fixed_prompt[:200])
    seg04 = (fixed_prompt.split("0.0-4.0s |", 1)[1].split("\n4.0-5.0s")[0]
             if "0.0-4.0s |" in fixed_prompt else "")
    check("合并段从台词窗起点起标：0.0-4.0s 且手势+说话同段",
          "palm pressing down once" in seg04 and "delivers the spoken line" in seg04,
          fixed_prompt[:300])
    check("后续时段保持原样（4.0-5.0s 点头段仍在）", "4.0-5.0s | No dialogue." in fixed_prompt, "")
    check("Audio Timeline 未被改动",
          '0.0-4.0s | <Audio 1> | <Subject 1> (S1): "先别出声。记下位置。"' in fixed_prompt, "")
    check("修复后闸门通过、重跑幂等",
          not any("台词时段错位" in i for i in storyboard_polish_issues(fixed_prompt, SRC_S3))
          and align_speech_segments(fixed_prompt) == (fixed_prompt, False), "")
    same, ch2 = align_speech_segments(speech_cand("first"))
    check("已对齐 prompt 原样返回", not ch2 and same == speech_cand("first"), "")

    # ---- 2. 锚点行角色分流 + 构造性注入 ----
    prompt23 = h3_prompt(
        "<Subject 1> is Lu Manchun in <Picture 1>. <Subject 2> is Xiao Mo in <Picture 2>.",
        "[Shot 1] The camera holds the anchored two-shot; both characters act naturally.")
    out = rewrite_aligned_prompt(
        prompt23, image_ns=[1, 2, 7], table=TABLE_EP4,
        bound_mids=["g1", "g2", "g7"], global_mids=["g1", "g2", "g7"], keyframe_ns={7})
    check("三视图身份行=身份锚（不再自称构图锚；含道具防复制）",
          "<Picture 1> is the reference for: 吕满春(2).png｜真人身份、西装三视图。 "
          "(original storyboard Image 1); it anchors the character's identity, costume and "
          "proportions; it is a turnaround sheet — its multi-view layout must never be "
          "reproduced as multiple characters or duplicated props." in out, out[:400])
    check("首帧身份行=构图/起始姿态/光线锚",
          "it anchors this shot's composition, starting poses and lighting as the designated "
          "first frame." in out, out[:400])
    check("统一误标措辞绝迹", "anchors this shot's composition and visual states" not in out, out[:200])
    check("防复制/肢体数/道具唯一约束注入（no extra limbs + 每道具一次）",
          "no extra limbs" in out and "appears exactly once in the shot" in out, out[-450:])
    out_again = rewrite_aligned_prompt(
        out, image_ns=[1, 2, 7], table=TABLE_EP4,
        bound_mids=["g1", "g2", "g7"], global_mids=["g1", "g2", "g7"], keyframe_ns={7})
    check("注入与锚点行重写幂等（重跑 bind 不加料）", out_again == out, f"{len(out_again)} vs {len(out)}")

    neutral = rewrite_aligned_prompt(
        h3_prompt("<Subject 1> is Xiao Mo in <Picture 1>.", "[Shot 1] Body."),
        image_ns=[5], table={5: "关键帧1｜两块区域形成强烈反差"}, bound_mids=["g5"], global_mids=["g5"])
    check("非三视图非首帧=内容锚中性措辞、不注入防复制",
          "it anchors the referenced content's appearance and visual states in this shot." in neutral
          and "no extra limbs" not in neutral, neutral[:300])

    legacy = rewrite_aligned_prompt(
        "小墨 anchored by <Picture 4>，构图参考 <Picture 2>。",
        image_ns=[4, 5, 6], table={},
        wants_mids=["g4", "g6", "g7"], bound_mids=["g4", "g5", "g6"],
        global_mids=["g1", "g2", "g3", "g4", "g5", "g6", "g7"])
    check("旧结构声明行格式不变（中文尾注）",
          "<Picture 1>：本镜参考图（原稿 Image 4）" in legacy, legacy)

    # ---- 3. 非说话人闭嘴约束（镜11 缓解）----
    lips_src = h3_prompt(
        "<Subject 1> is the real person Lu Manchun in <Picture 1>. "
        "<Subject 2> is the 3D character Xiao Mo in <Picture 2>.",
        "[Shot 1] <Subject 1> (S1) delivers the spoken line of <Audio 1> with lips "
        "synchronized, his voice low; <Subject 2> nods once.\nAudio Timeline:\n"
        "0.0-4.0s | <Audio 1> | <Subject 1> (S1): \"先别出声。记下位置。\"")
    lips_out = rewrite_aligned_prompt(
        lips_src, image_ns=[1, 2], table=TABLE_EP4,
        bound_mids=["g1", "g2"], global_mids=["g1", "g2"])
    check("单人说话双人镜：非说话人闭嘴约束注入",
          "only the tagged speaker's lips move; <Subject 2> keeps the mouth closed" in lips_out,
          lips_out[-300:])
    lips_again = rewrite_aligned_prompt(
        lips_out, image_ns=[1, 2], table=TABLE_EP4,
        bound_mids=["g1", "g2"], global_mids=["g1", "g2"])
    check("闭嘴约束注入幂等", lips_again == lips_out, "")

    # ---- 4. 润色开关 ----
    good_polished = h3_prompt(
        "<Subject 1> is X in <Picture 1>.",
        "[Shot 1] " + "The camera observes the patch carefully and moves forward. " * 30
        + "The lens racks focus onto the sparse pigment points beneath the glass.")
    draft = h3_prompt("<Subject 1> is X in <Picture 1>.", "[Shot 1] thin body.")

    class Env:
        def __init__(self):
            self.polish_calls = 0

        async def structured_call(self, system, user, schema, **kw):
            from app.services.pipeline.schemas import PlanBatch, PolishedPrompt
            if schema is PolishedPrompt:
                self.polish_calls += 1
                return PolishedPrompt.model_validate({"prompt": good_polished})
            return PlanBatch.model_validate({
                "shots": [{"title": "镜1", "description": "画面", "prompt": draft,
                           "keyframe_prompt": "k", "narration": "", "duration": 5,
                           "gen_mode": "i2v", "wants_images": [], "wants_audios": [],
                           "wants_videos": []}],
                "total": 1, "done": True})

    def put_beat(p):
        p.screenplay = {"script_type": "storyboard",
                        "beats": [{"title": "拍1", "description": "画面1", "narration": "",
                                   "source_text": SRC_S3, "source_fidelity": "verbatim"}],
                        "characters": []}

    pid = project_store.create("润色关", "9:16")["id"]
    project_store.mutate(pid, put_beat)
    project_store.mutate(pid, lambda p: setattr(p.settings, "prompt_polish", False))
    env = Env()
    asyncio.run(stages.run_plan(pid, env))
    sc = project_store.get(pid)["scenes"][0]
    check("开关关闭：润色遍 0 次调用、草稿直落盘",
          env.polish_calls == 0 and "thin body" in sc["prompt"],
          f"calls={env.polish_calls} prompt={sc['prompt'][:80]}")

    pid2 = project_store.create("润色开", "9:16")["id"]
    project_store.mutate(pid2, put_beat)
    env2 = Env()
    asyncio.run(stages.run_plan(pid2, env2))
    sc2 = project_store.get(pid2)["scenes"][0]
    check("开关默认开：润色遍执行并采纳（既有行为不变）",
          env2.polish_calls == 1 and "racks focus onto the sparse pigment points" in sc2["prompt"],
          f"calls={env2.polish_calls}")

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
