"""成稿直录回归（2026-10-01 事故：关闭润色后剧本仍被改动）。

用户直投单镜 H3 成稿（围栏四段正文 + 投搞头部 Reference 声明 + 中文 Audio
Timeline + 提示），prompt_polish 已关，agent 输出却：水影时段 0-2s 并成
[Shot 1] 0.0-4.0s、白痕起点丢失、配音被推迟到 "before speaking"、单连续镜拆成
[Shot 1]-[Shot 5]、凭空加 "3D animated short" 风格与 "One continuous static
shot" 摄影限制。根因两条：
1. normalize：detect_h3_prompt_boundaries 要求 ≥2 处 subject_definitions 才认
   成稿，单镜成稿（1 处）走了 LLM 慢路径，拆拍权交给模型；
2. plan：prompt_polish=False 只关 director 遍，plan 主遍照跑 LLM 重写，
   PlanBatchH3 六段齐全闸门逼模型发明原文没有的工艺块。

验证：
1. normalize：单镜 H3 成稿确定性识别（storyboard/1 拍/verbatim，拍原文含投搞
   头部 Reference 声明区）
2. plan（润色关）：零 LLM 调用逐字直录——时段 0-2/2-4/4-6/6-8/8-10 原样、
   无 [Shot N] 子拍、无发明风格/摄影/工艺块、无 "before speaking"、duration=10、
   wants 按声明对号（Image 21/22/04/05 + 全局第 10 段音频）
3. bind：加粗 **Reference** 声明行 + 「Audio 1=全局 10」写法可解析——4 图按声明
   顺序绑定、音频绑到全局第 10 段（不是正文 <Audio 1> 兜底误绑的第 1 段）、
   首帧声明落在 <Picture 1> 锚点行、组装闸门放行
4. 润色开：既有行为不变（plan 主遍 LLM 照跑）
5. 中文分镜稿（非 H3 成稿）润色关：直录不适用，仍走 LLM 路径（翻译必需）

运行：.venv/Scripts/python.exe tests/verbatim_h3_adoption_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="verbatim_h3_")

from app.services import project_store  # noqa: E402
from app.services.pipeline import stages  # noqa: E402
from app.services.pipeline.stages import (  # noqa: E402
    extract_h3_body, materialize_audio_timeline, parse_audio_map, parse_picture_map)

PASS = 0

# 用户原稿逐字重放（2026-10-01 单镜成稿直投现场）
SOURCE = """**Duration：10 秒。配音 1 条。首帧：Image 21；末帧：Image 22。**
**Reference 图片：** <Picture 1> ← Image 21；<Picture 2> ← Image 22；<Picture 3> ← Image 04（小墨(20260926-051939).png）；<Picture 4> ← Image 05（吕满春(9).png）。
**Reference 音频：** Audio 1=全局 10。

```text
subject_definitions:
<Subject 1> is 小墨, identity and costume strictly defined by <Picture 3>; one physical instance only.
<Subject 2> is 吕满春, identity and costume strictly defined by <Picture 4>; one physical instance only.
The storyboard pictures control shot composition and visual states; they do not add further physical character instances.
summary: Xiao Mo recalls earlier evidence as visual memories, then returns to the current path. The pale reflection has no matching body in the real world.
retention_analysis: Picture 1 is a subjective memory cue, not a new present-day puddle anomaly; Picture 2 returns to the road. One Xiao Mo, one distant Lü at most. The white footprint is a memory image, never a new trail of prints.
detailed_description: 0-2s stay on Xiao Mo hovering above the present road as an optical memory of an abstracted water reflection briefly overlays the background, a pale human-like reflection only within the water, no physical person above. 2-4s keep Xiao Mo in the foreground and replace only the background memory with fire-blackened stone with one clean white residual shape, not a fresh fire at Gao village. 4-6s keep Xiao Mo visible and replace the memory background with one isolated white footprint on a prior road, one imprint only. 6-8s dissolve the memory background into the present Gao path; Xiao Mo remains at the same hover height and his lips synchronize throughout the supplied voice; no literal holographic UI, no duplicate Xiao Mo. 8-10s let the memory imagery vanish, Lü in navy suit at a normal scale in the background. Use restrained desaturated flashes and a dry muted texture sweep, not fantasy beams.
```

**Audio Timeline：** 0–8s Audio 1 小墨：“水里的白影、火后的白痕、路上的白脚印……现在，连人都会说出自己不知道的话了。”（7.57→8）；8–10s 无对白。
**提示：** Image 21/Image 22 为情绪与视觉锚点，中间的火后白痕是同一镜的短暂主观记忆，不要生成独立的新场景人物。"""

LINE = "水里的白影、火后的白痕、路上的白脚印……现在，连人都会说出自己不知道的话了。"


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


class NormEnv:
    """normalize 元数据信封（beats 由确定性路径生成，不由 LLM 拆拍）。"""

    async def structured_call(self, system, user, schema, **kw):
        from app.services.pipeline.schemas import Screenplay
        assert schema is Screenplay, f"unexpected schema {schema}"
        return Screenplay.model_validate({
            "script_type": "storyboard", "logline": "小墨在村道前回忆三条旧线索",
            "characters": [{"name": "小墨", "design": "外观以参考图为准"},
                           {"name": "吕满春", "design": "外观以参考图为准"}],
            "beats": [{"title": "占位"}], "notes": ""})


class FailLLM:
    """成稿直录 + 声明对齐路径下，plan/bind 都不应求助任何 LLM——被调用即失败。"""

    def __init__(self):
        self.calls = 0

    async def structured_call(self, *a, **kw):
        self.calls += 1
        raise AssertionError("成稿直录路径不应调用 LLM")


class PlanEnv:
    """润色开（既有行为）：plan 主遍 LLM 照跑，返回合格 H3 六段式。"""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self.calls = 0

    async def structured_call(self, system, user, schema, **kw):
        from app.services.pipeline.schemas import PlanBatchH3, PolishedPrompt
        self.calls += 1
        if schema is PolishedPrompt:
            return PolishedPrompt.model_validate({"prompt": self.prompt})
        assert schema is PlanBatchH3, f"unexpected schema {schema}"
        return PlanBatchH3.model_validate({"shots": [
            {"title": "LLM 镜", "description": "画面", "prompt": self.prompt,
             "keyframe_prompt": "k", "narration": "", "duration": 10,
             "gen_mode": "i2v", "wants_images": [], "wants_audios": [],
             "wants_videos": []}], "total": 1, "done": True})


def build_project(title: str, polish_off: bool) -> str:
    from app.harness.state import Material
    p = project_store.create(title, "16:9")
    pid = p["id"]

    def inject(prj):
        prj.materials.append(Material(id="script1", kind="script", text=SOURCE,
                                      filename="inline_script.txt", url=None))
        for i in range(1, 23):
            prj.materials.append(Material(
                id=f"img{i:02d}", kind="image",
                url=f"/api/files/image/shot_{i:02d}.png",
                filename=f"shot_{i:02d}.png"))
        for i in range(1, 10):
            prj.materials.append(Material(
                id=f"aud{i:02d}", kind="audio",
                url=f"/api/files/audio/aud_{i:02d}.mp3",
                filename=f"{i:02d}_第{i}句.mp3", duration=3.0))
        prj.materials.append(Material(
            id="aud10", kind="audio", url="/api/files/audio/aud_10.mp3",
            filename=f"10_{LINE}.mp3", duration=7.57))
        merged = prj.settings.model_dump()
        merged["video_model"] = "minimax_h3_zm_u24"
        merged["prompt_polish"] = not polish_off
        prj.settings = type(prj.settings).model_validate(merged)

    project_store.mutate(pid, inject)
    return pid


def main() -> None:
    # ---- 0. 机械适配纯函数 ----
    body = extract_h3_body(SOURCE)
    check("extract_h3_body：围栏成稿正文提取（含 detailed_description 骨架）",
          body is not None and body.startswith("subject_definitions:")
          and "not fantasy beams." in body and "```" not in body, str(body)[:120])
    tl = materialize_audio_timeline(SOURCE, body)
    check("materialize_audio_timeline：中文声明 → 英文小节（<Audio 1> + S1 说话人 + 台词逐字）",
          tl == ('0.0-8.0s | <Audio 1> | <Subject 1> (S1): "水里的白影、火后的白痕、路上的白脚印……'
                 '现在，连人都会说出自己不知道的话了。"\n8.0-10.0s | No dialogue.'), tl)
    check("materialize_audio_timeline：正文已有 Audio Timeline 时不重复",
          materialize_audio_timeline(SOURCE, body + "\nAudio Timeline:\n0.0-8.0s | x") == "")
    check("parse_picture_map：<Picture k> ← Image N 按 k 升序 → [21,22,4,5]",
          parse_picture_map(SOURCE) == [21, 22, 4, 5], str(parse_picture_map(SOURCE)))
    check("parse_audio_map：Audio 1=全局 10 → [10]（不是局部号 1）",
          parse_audio_map(SOURCE) == [10], str(parse_audio_map(SOURCE)))

    # ---- 1. normalize：单镜成稿确定性识别 ----
    pid = build_project("成稿直录-润色关", polish_off=True)
    asyncio.run(stages.run_normalize(pid, NormEnv()))
    sp = project_store.get(pid)["screenplay"]
    check("normalize：script_type=storyboard、1 拍、verbatim",
          sp["script_type"] == "storyboard" and len(sp["beats"]) == 1
          and sp["beats"][0]["source_fidelity"] == "verbatim",
          f"{sp['script_type']} beats={len(sp['beats'])}")
    beat_src = sp["beats"][0]["source_text"]
    check("normalize：拍原文含投搞头部（Duration/Reference 声明区）与围栏成稿",
          beat_src.startswith("**Duration：10 秒") and "**Reference 图片：**" in beat_src
          and "```text" in beat_src and "**提示：**" in beat_src, beat_src[:80])

    # ---- 2. plan（润色关）：零 LLM 逐字直录 ----
    fail_env = FailLLM()
    asyncio.run(stages.run_plan(pid, fail_env))
    proj = project_store.get(pid)
    check("plan：零 LLM 调用（成稿直录，连 plan 主遍都不跑）",
          fail_env.calls == 0, f"calls={fail_env.calls}")
    sc = proj["scenes"][0]
    prompt = sc["prompt"]
    check("plan：恰好 1 镜、duration=10（照抄 Duration 声明）",
          len(proj["scenes"]) == 1 and sc["duration"] == 10,
          f"scenes={len(proj['scenes'])} dur={sc['duration']}")
    for seg in ("0-2s stay on Xiao Mo hovering above the present road",
                "2-4s keep Xiao Mo in the foreground and replace only the background memory",
                "4-6s keep Xiao Mo visible",
                "6-8s dissolve the memory background into the present Gao path",
                "8-10s let the memory imagery vanish"):
        check(f"plan：时段逐字保留「{seg[:22]}…」", seg in prompt, "")
    for banned, why in (("[Shot", "镜头被拆成 [Shot 1]-[Shot 5] 子拍"),
                        ("3D animated", "凭空发明视觉风格"),
                        ("One continuous static shot", "凭空收紧摄影限制"),
                        ("before speaking", "配音被推迟（台词窗 0s 起）"),
                        ("cinematography:", "发明原文没有的工艺块"),
                        ("overall_soundscape:", "发明原文没有的工艺块")):
        check(f"plan：绝无{why}（'{banned}' 不出现）", banned not in prompt, "")
    check("plan：subject_definitions 逐字（<Picture 3>/<Picture 4> 锚定原样）",
          "<Subject 1> is 小墨, identity and costume strictly defined by <Picture 3>; "
          "one physical instance only." in prompt, "")
    check("plan：Audio Timeline 物化进 prompt（台词逐字）", f'"{LINE}"' in prompt, "")
    check("plan：narration 提取台词", LINE in sc["narration"], sc["narration"][:60])
    wants = proj["screenplay"]["wants"][0]
    check("plan：wants 按声明对号（Image 21/22/04/05 + 全局第 10 段音频）",
          [w["material_id"] for w in wants["wants_images"]] == ["img21", "img22", "img04", "img05"]
          and [w["material_id"] for w in wants["wants_audios"]] == ["aud10"],
          str(wants))

    # ---- 3. bind：声明对齐（加粗 Reference 行 / 全局音频写法）----
    asyncio.run(stages.run_bind(pid, fail_env))
    proj = project_store.get(pid)
    sc = proj["scenes"][0]
    check("bind：零 LLM 兜底（声明区可解析，对齐吃掉全部素材）",
          fail_env.calls == 0, f"calls={fail_env.calls}")
    check("bind：4 图按声明顺序绑定（21/22/4/5 的素材）",
          [b["material_id"] for b in sc["binding"]["images"]] == ["img21", "img22", "img04", "img05"],
          str(sc["binding"]["images"]))
    check("bind：音频绑到全局第 10 段（不是 <Audio 1> 兜底误绑的第 1 段）",
          sc["binding"]["audios"] == ["aud10"], str(sc["binding"]["audios"]))
    prompt = sc["prompt"]
    check("bind：锚点行重建（original storyboard Image 21 起，逐图身份行）",
          "<Picture 1> is the reference for:" in prompt
          and "(original storyboard Image 21)" in prompt
          and "(original storyboard Image 4)" in prompt, "")
    check("bind：首帧声明落在 <Picture 1>（首帧：Image 21）",
          "as the designated first frame." in prompt.splitlines()[1], prompt.splitlines()[1][:160])
    check("bind：正文时段仍逐字未被 bind 破坏",
          "0-2s stay on Xiao Mo hovering above the present road" in prompt
          and "[Shot" not in prompt, "")
    from app.harness.state import Material, Scene
    from app.services import generation_service
    vmodel = generation_service.resolve_video_model(proj["settings"]["video_model"])
    mats = {m["id"]: Material.model_validate(m) for m in proj["materials"]}
    req = generation_service.assemble_video_request(Scene.model_validate(sc), mats, vmodel)
    check("bind：组装闸门放行（contract_error 为空）", req["contract_error"] is None,
          str(req["contract_error"]))

    # ---- 4. 润色开：既有行为不变（plan 主遍 LLM 照跑） ----
    h3_prompt = (
        "subject_definitions:\n<Subject 1> is Xiao Mo in <Picture 1>.\n\n"
        "summary: [reference generation] Memory recall shot.\n\n"
        "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
        "detailed_description:\n[Shot 1] 0.0-10.0s | " + "The camera holds as memories pass. " * 40
        + "\n\ncinematography: smooth.\n\nlighting: soft.\n\nvisual_effects: none.\n\n"
        "visual_constraints: none.\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music: N/A")
    pid2 = build_project("成稿直录-润色开", polish_off=False)
    asyncio.run(stages.run_normalize(pid2, NormEnv()))
    plan_env = PlanEnv(h3_prompt)
    asyncio.run(stages.run_plan(pid2, plan_env))
    sc2 = project_store.get(pid2)["scenes"][0]
    check("润色开：plan 主遍 LLM 照跑（直录路径不启用）",
          plan_env.calls >= 1 and "The camera holds as memories pass." in sc2["prompt"],
          f"calls={plan_env.calls}")

    # ---- 5. 中文分镜稿（非 H3 成稿）润色关：仍走 LLM 路径 ----
    pid3 = project_store.create("中文分镜-润色关", "9:16")["id"]

    def put_cn(prj):
        prj.screenplay = {"script_type": "storyboard",
                          "beats": [{"title": "拍1", "description": "画面1", "narration": "台词",
                                     "source_text": "【分镜03｜换一种说法】\n0—1秒：吕满春掌心向下轻压一次。",
                                     "source_fidelity": "verbatim"}],
                          "characters": []}

    project_store.mutate(pid3, put_cn)
    project_store.mutate(pid3, lambda p: setattr(p.settings, "prompt_polish", False))
    plan_env3 = PlanEnv(h3_prompt)
    asyncio.run(stages.run_plan(pid3, plan_env3))
    check("中文分镜稿润色关：非 H3 成稿不适用直录，LLM 翻译路径保持",
          plan_env3.calls == 1 and len(project_store.get(pid3)["scenes"]) == 1,
          f"calls={plan_env3.calls}")

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
