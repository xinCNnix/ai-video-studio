"""plan/bind prompt 质量改动离线回归（2026-09-22）：

1. _reference_table 反引号格式（`<Image N>`：身份）不再整表丢失（第07集事故：
   编号表解析为空 → 定义行全部回退「本镜参考图」占位符）
2. rewrite_aligned_prompt：未绑定图降级文本中性化（不再向视频模型描述一张
   不会传入的图）、retention fully_preserved 同步降级、重跑幂等
3. fix_prompt_boilerplate：跨镜引用开场白剔除、唯一 [Shot N] 记号归一为 [Shot 1]
4. h3_prompt_quality_issues：跨镜引用/正文过薄检出
5. detect_h3_prompt_boundaries：英文成稿（GPT 六段式直投）拍边界识别
6. run_normalize 英文成稿确定性分拍（一拍一镜，不交给模型猜）
7. run_plan 质量软闸门：薄正文触发带反馈重写一轮并采纳更优结果

运行：.venv/Scripts/python.exe tests/prompt_quality_selfcheck.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="prompt_quality_")

from app.services import project_store  # noqa: E402
from app.services.pipeline import llm_client, stages  # noqa: E402
from app.services.pipeline.stages import (  # noqa: E402
    _reference_table, detect_h3_prompt_boundaries, fix_prompt_boilerplate,
    h3_prompt_quality_issues, rewrite_aligned_prompt)

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


def main() -> None:
    # ---- 1. _reference_table 反引号格式 ----
    src = (
        "## Reference Index\n\n"
        "- `<Image 1>`：吕满春多角度人物定妆参考\n"
        "  - 锁定身份、年龄、五官\n"
        "- `<Image 2>`：小墨角色定妆参考\n"
        "- `<Image 5>`：关键帧1｜两块区域形成强烈反差\n"
        "- <Image 7>｜演播室双皮肤3D模型\n"
    )
    table = _reference_table(src)
    check("反引号标签身份解析（第07集事故回归）",
          table.get(1) == "吕满春多角度人物定妆参考"
          and table.get(2) == "小墨角色定妆参考"
          and table.get(5) == "关键帧1｜两块区域形成强烈反差"
          and table.get(7) == "演播室双皮肤3D模型", f"{table}")
    real = Path(__file__).resolve().parents[2] / "data" / "projects" / "project_9cd6e61e.json"
    if real.exists():
        d = json.loads(real.read_text(encoding="utf-8"))
        script = next(m["text"] for m in d["materials"] if m.get("kind") == "script")
        check("真实第07集编号表 9 项全解析", len(_reference_table(script)) == 9,
              f"{sorted(_reference_table(script))}")
    else:
        print("skip 真实第07集数据不存在，跳过实库校验")

    # ---- 2. 未绑定图降级中性化 + retention 降级 + 幂等 ----
    prompt = (
        "subject_definitions:\n<Subject 1> is A in <Picture 1>.\n"
        "<Subject 2> is B anchored by <Picture 2>.\n"
        "retention_analysis:\n<Picture 2>: fully_preserved - follows its anchor.\n"
        "detailed_description:\n[Shot 1] The body uses <Picture 1> and glances at <Picture 2>.\n"
    )
    out = rewrite_aligned_prompt(
        prompt, image_ns=[1], table={1: "小墨"},
        wants_mids=["m1", "m2"], bound_mids=["m1"], global_mids=["m1", "m2"])
    check("wants 降级文本中性化（不再描述不会传入的图）",
          "reference image 2 of the original plan" not in out
          and stages._UNBOUND_NEUTRAL in out and "<Picture 2>" not in out, out)
    check("retention 对未绑定引用降级 fully_preserved→weak_reference",
          any("weak_reference" in ln and stages._UNBOUND_NEUTRAL in ln
              for ln in out.splitlines()), out)
    out2 = rewrite_aligned_prompt(
        out, image_ns=[1], table={1: "小墨"},
        wants_mids=["m1", "m2"], bound_mids=["m1"], global_mids=["m1", "m2"])
    check("中性化后重跑幂等（重绑定不再破坏已修复 Prompt）", out2 == out, out2)

    # ---- 3. fix_prompt_boilerplate ----
    fixed = fix_prompt_boilerplate(
        "[Shot 2] Continuing from the previous shot, the camera rises. 延续上一镜，镜头升高。")
    check("跨镜引用剔除 + 唯一 [Shot N] 归一 [Shot 1]",
          fixed.startswith("[Shot 1] the camera rises.") and "Continuing" not in fixed
          and "延续上一镜" not in fixed, fixed)
    multi = fix_prompt_boilerplate("[Shot 1] A. [Shot 2] At 00:05.000, B.")
    check("多 [Shot N] 记号（镜内多拍）不动", multi == "[Shot 1] A. [Shot 2] At 00:05.000, B.", multi)

    # ---- 4. h3_prompt_quality_issues ----
    thin = "detailed_description:\n[Shot 1] short body.\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music: N/A"
    check("薄正文检出（<250 英文词）", any("过薄" in i for i in h3_prompt_quality_issues(thin)),
          f"{h3_prompt_quality_issues(thin)}")
    fat = ("detailed_description:\n" + "The camera observes the patch carefully and moves forward. " * 30
           + "\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music: N/A")
    check("达标正文无问题", h3_prompt_quality_issues(fat) == [], f"{h3_prompt_quality_issues(fat)}")
    check("跨镜引用问题检出",
          any("跨镜引用" in i for i in h3_prompt_quality_issues(
              "detailed_description:\n[Shot 1] Continuing from the previous shot, it goes. " * 1
              + "\n\noverall_soundscape:\nQuiet.")))

    # ---- 5. detect_h3_prompt_boundaries ----
    shot_tpl = ("subject_definitions:\n<Subject 1> is X.\n\nsummary:\n[reference generation] Y.\n\n"
                "detailed_description:\n[Shot 1] Body.\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music:\nN/A\n\n")
    eng_script = "# Intro notes\n\n" + shot_tpl + shot_tpl
    bounds = detect_h3_prompt_boundaries(eng_script)
    check("英文成稿两镜边界识别", len(bounds) == 2 and bounds[0][1] == "H3 Shot 1"
          and bounds[1][1] == "H3 Shot 2", f"{bounds}")
    check("单镜成稿（一处 subject_definitions + detailed_description 骨架）→ 确定性 1 拍",
          detect_h3_prompt_boundaries(shot_tpl) == [(0, "H3 Shot 1")],
          f"{detect_h3_prompt_boundaries(shot_tpl)}")
    check("单次出现但无 H3 骨架（正文偶然提及）不触发",
          detect_h3_prompt_boundaries("故事正文提到 subject_definitions: 一词而已。") == [])
    check("中文片段头优先：中文成稿不被 H3 边界兜底误抢",
          len(stages.detect_fragment_headers("# H3片段1｜开场画面\n" + shot_tpl
                                             + "# H3片段2｜发展画面\n" + shot_tpl)) == 2)

    # ---- 6. run_normalize：英文成稿确定性分拍 ----
    class MetaLLM:
        async def generate(self, messages, model, temperature=0.7, max_tokens=None):
            return json.dumps({
                "script_type": "storyboard", "logline": "english h3 storyboard",
                "characters": [{"name": "X", "design": "外观以参考图为准"}],
                "beats": [{"title": "占位", "description": "", "narration": "", "source_text": ""}],
                "notes": "",
            }, ensure_ascii=False)

    pid = project_store.create("英文成稿分拍", "9:16")["id"]

    def put_script(p):
        from app.harness.state import Material
        p.materials.append(Material(id="mat_s", kind="script", text=eng_script,
                                    filename="inline_script.txt", url=None))

    project_store.mutate(pid, put_script)
    asyncio.run(stages.run_normalize(pid, llm_client.LLMEnvelope(MetaLLM(), "m")))
    scr = project_store.get(pid)["screenplay"]
    check("英文成稿逐字分拍：2 镜、全部 verbatim",
          len(scr["beats"]) == 2 and all(b["source_fidelity"] == "verbatim" for b in scr["beats"]),
          f"{[b.get('source_fidelity') for b in scr['beats']]}")

    # ---- 7. run_plan 导演润色遍：薄草稿 → 专职润色调用采纳；润色炸 → 保留草稿 ----
    class PolishAwareEnvelope:
        def __init__(self, fail_polish: bool = False):
            self.calls = 0
            self.fail_polish = fail_polish

        async def structured_call(self, system, user, schema, **kw):
            self.calls += 1
            if schema.__name__ == "PolishedPrompt":
                if self.fail_polish:
                    raise RuntimeError("润色遍连续修复失败（模拟）")
                from app.services.pipeline.schemas import PolishedPrompt
                body = ("The camera observes the patch carefully and moves forward. " * 30
                        + "The lens racks focus onto the sparse pigment points beneath the glass.")
                return PolishedPrompt.model_validate({
                    "prompt": ("subject_definitions:\n<Subject 1> is X.\n\nsummary:\n[reference generation] Y.\n\n"
                               "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
                               f"detailed_description:\n[Shot 1] {body}\n\noverall_soundscape:\nQuiet.\n\n"
                               "non_diegetic_music: N/A")})
            from app.services.pipeline.schemas import PlanBatch
            return PlanBatch.model_validate({
                "shots": [{"title": "镜1", "description": "画面",
                           "prompt": ("subject_definitions:\n<Subject 1> is X.\n\nsummary:\n[reference generation] Y.\n\n"
                                      "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
                                      "detailed_description:\n[Shot 1] thin body.\n\noverall_soundscape:\nQuiet.\n\n"
                                      "non_diegetic_music: N/A"),
                           "keyframe_prompt": "k", "narration": "", "duration": 5,
                           "gen_mode": "i2v", "wants_images": [], "wants_audios": [],
                           "wants_videos": []}],
                "total": 1, "done": True})

    def put_beat(p):
        p.screenplay = {"script_type": "storyboard",
                        "beats": [{"title": "拍1", "description": "画面1", "narration": "",
                                   "source_text": shot_tpl, "source_fidelity": "verbatim"}],
                        "characters": []}

    pid2 = project_store.create("润色采纳", "9:16")["id"]
    project_store.mutate(pid2, put_beat)
    fake = PolishAwareEnvelope()
    asyncio.run(stages.run_plan(pid2, fake))
    sc = project_store.get(pid2)["scenes"][0]
    check("薄草稿经导演润色遍采纳更优结果",
          fake.calls == 2 and "racks focus onto the sparse pigment points" in sc["prompt"],
          f"calls={fake.calls} prompt={sc['prompt'][:120]}")

    pid3 = project_store.create("润色失败保草稿", "9:16")["id"]
    project_store.mutate(pid3, put_beat)
    fake2 = PolishAwareEnvelope(fail_polish=True)
    asyncio.run(stages.run_plan(pid3, fake2))
    sc3 = project_store.get(pid3)["scenes"][0]
    check("润色遍失败不炸规划、保留草稿（单调性由构造保证）",
          fake2.calls == 2 and "thin body" in sc3["prompt"], f"calls={fake2.calls} prompt={sc3['prompt'][:120]}")

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
