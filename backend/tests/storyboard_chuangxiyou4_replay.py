"""闯西游第四回事故回归（project_79e4d651）：拆镜翻倍 + 配音绑两遍。

事故（2026-09-22）：正文头全是【分镜01｜拦路与坦白】（方括号 + 字母后缀 12A/12B/12C），
detect_fragment_headers 0 命中 → 走 LLM 分块慢路径：素材索引「Image 05｜分镜01｜…」被数成
14 拍、正文又数 14 拍、分块包装头【剧本片段 5/5】再漏 1 拍 = 29 拍；plan「一拍一镜守恒」
忠实放大成 29 场，故事演两遍；13 条配音每条绑两遍（场01/15 同一条「俺老孙」）。
连带事故：
- 声明行「Reference 图片上传绑定：Picture 1=Image 01；…」标签与内容同行、编号无尖括号，
  _reference_tokens 两头不认 → 对齐失效，绑定全靠 plan 猜（场10 绑 6 张图）。
- trim_trailing_sections 缩进事故：keep 赋值落在 return 后的死代码里，从未生效。

重放真实项目的 materials/screenplay（零 LLM），验证：
1. 原稿确定性识别 14 个【分镜N｜标题】头，14 拍全 verbatim，末拍切掉「七、独立生成与拼接」
2. 事故现场的 29 拍经同号去重闸门 → 14 拍（索引拍/包装拍剔除，verbatim 正文拍保留）
3. 修复后的 14 拍喂 bind：图片张数与编号逐镜等于原稿 Reference 声明；
   13 条配音各绑一次（无缺无重），对齐吃掉全部素材，兜底 LLM 零调用
4. 组装闸门逐镜放行（音频标签数=传入音频数，无悬空引用）

运行：.venv/Scripts/python.exe tests/storyboard_chuangxiyou4_replay.py
数据源：../data/projects/project_79e4d651.json（若不存在则跳过）
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DATA_SRC = Path(__file__).resolve().parents[2] / "data" / "projects" / "project_79e4d651.json"
if not DATA_SRC.exists():
    print("skip：找不到真实项目数据，跳过重放")
    sys.exit(0)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="xiyou_ch4_")

from app.services import project_store  # noqa: E402
from app.services.pipeline import llm_client, stages  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


REAL = json.loads(DATA_SRC.read_text(encoding="utf-8"))
SRC = REAL["screenplay"]["source_text"]
# 事故现场（29 拍）取自修复前备份；项目本体已修复后此节自动跳过
_backup = sorted((DATA_SRC.parents[1] / "backups").glob("project_79e4d651.pre-repair-*.json"))
BROKEN = json.loads(_backup[-1].read_text(encoding="utf-8")) if _backup else None

# 原稿 Reference 声明（上传顺序全局号）→ 期望逐镜绑定
EXPECTED_IMAGES = [
    [1, 2, 3, 4, 5], [2, 3, 6], [1, 2, 7], [3, 8], [1, 3, 4, 9], [2, 3, 10],
    [1, 2, 11], [1, 2, 3, 4, 12], [1, 2, 3, 4, 13], [1, 2, 3, 4, 13], [1, 2, 14],
    [15], [3, 16], [17],
]
EXPECTED_AUDIOS = [
    [1, 2], [3, 4], [5], [6], [7], [8, 9], [10], [11], [], [12], [13], [], [], [],
]


class NoFallbackLLM:
    """对齐吃掉全部素材后，bind 不应再求助兜底 LLM——被调用即测试失败"""

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        self.calls += 1
        return ""


def main() -> None:
    # ---------------- 1. 确定性拆拍（真实原稿） ----------------
    headers = stages.detect_fragment_headers(SRC)
    check("真实原稿识别 14 个【分镜N｜标题】头", len(headers) == 14, f"{len(headers)}")
    beats = stages._beats_from_headers(SRC, headers)
    beats = stages.trim_trailing_sections(beats)
    check("一镜一拍共 14 拍全 verbatim",
          len(beats) == 14 and all(b["source_fidelity"] == "verbatim" for b in beats), f"{len(beats)}")
    check("末拍切掉稿尾「七、独立生成与拼接」章节", "独立生成与拼接" not in beats[-1]["source_text"])
    check("12A/12B/12C 三拍齐全", [beats[i]["title"] for i in (11, 12, 13)] == ["月下孤影", "悟空也察觉了", "空山失色"])

    # ---------------- 2. 事故现场去重：29 拍 → 14 拍 ----------------
    if BROKEN is None:
        print("skip 事故现场去重检查（无修复前备份）")
    else:
        broken = BROKEN["screenplay"]["beats"]
        check("事故现场确为 29 拍（拆镜翻倍）", len(broken) == 29, f"{len(broken)}")
        kept, dropped = stages.dedupe_storyboard_beats(broken)
        check("同号去重闸门修复存量：29 → 14 拍", len(kept) == 14 and len(dropped) == 15,
              f"kept={len(kept)} dropped={len(dropped)}")
        check("去重保留的全是 verbatim 正文拍", all(b.get("source_fidelity") == "verbatim" for b in kept))

    # ---------------- 3. bind 对齐重放（真实素材 + 修复后的 14 拍） ----------------
    p = project_store.create("闯西游第四回回归", "9:16")
    PID = p["id"]

    def inject(prj):
        from app.harness.state import Material, Scene
        for m in REAL["materials"]:
            prj.materials.append(Material(**{k: m[k] for k in ("id", "kind", "url", "filename", "text", "duration") if k in m}))
        sp = json.loads(json.dumps(REAL["screenplay"]))
        sp["beats"] = beats  # 修复后的 14 拍；wants 清空（旧 wants 指向已废弃的 29 场）
        sp["wants"] = []
        prj.screenplay = sp
        # 合成 plan 风格 prompt：正文引用原稿全局号 <Image N>/<Audio N>——bind 的
        # 声明行重写与音频两域改号要在这个底稿上工作（空 prompt 无从重写）
        def synth_prompt(exp_img: list[int], exp_aud: list[int]) -> str:
            lines = ["subject_definitions:"]
            for k, n in enumerate(exp_img, 1):
                lines.append(f"<Subject {k}> anchored by <Image {n}> per the original storyboard.")
            if exp_aud:
                refs = " and ".join(f"<Audio {n}>" for n in exp_aud)
                lines.append(f"summary: spoken lines reuse {refs} as-is.")
            return "\n".join(lines) + "\n"

        prj.scenes = [
            Scene(id=f"scene_r{i}", title=b["title"], description=b.get("description") or "",
                  narration=b.get("narration") or "", duration=8, gen_mode="i2v",
                  prompt=synth_prompt(ei, ea))
            for i, (b, ei, ea) in enumerate(zip(beats, EXPECTED_IMAGES, EXPECTED_AUDIOS), 1)
        ]
        merged = prj.settings.model_dump()
        merged["video_model"] = "minimax_h3_zm_u24"
        prj.settings = type(prj.settings).model_validate(merged)

    project_store.mutate(PID, inject)

    nollm = NoFallbackLLM()
    env = llm_client.LLMEnvelope(nollm, "m")
    r = asyncio.run(stages.run_bind(PID, env))
    proj = project_store.get(PID)

    img_order = [m["id"] for m in proj["materials"] if m["kind"] == "image"]
    aud_order = [m["id"] for m in proj["materials"] if m["kind"] == "audio"]

    check("14 镜全部按原稿 Reference 声明对齐", r.get("reference_aligned_scenes") == 14,
          json.dumps(r, ensure_ascii=False)[:200])
    all_audios: list[str] = []
    for i, (s, exp_img, exp_aud) in enumerate(zip(proj["scenes"], EXPECTED_IMAGES, EXPECTED_AUDIOS), 1):
        got_img = [img_order.index(x["material_id"]) + 1 for x in s["binding"]["images"]]
        got_aud = [aud_order.index(x) + 1 for x in s["binding"]["audios"]]
        check(f"镜{i:02d} 参考图编号={exp_img}（张数与编号逐镜等于原稿声明）", got_img == exp_img, f"{got_img}")
        check(f"镜{i:02d} 配音编号={exp_aud}", got_aud == exp_aud, f"{got_aud}")
        all_audios.extend(s["binding"]["audios"])
    check("13 条配音各绑一次（无缺无重，不再翻倍绑两遍）",
          len(all_audios) == 13 and len(set(all_audios)) == 13, f"n={len(all_audios)} uniq={len(set(all_audios))}")
    check("兜底 LLM 零调用", nollm.calls == 0, f"calls={nollm.calls}")

    # ---------------- 4. 组装闸门 ----------------
    from app.harness.state import Material, Scene
    from app.services import generation_service
    from app.services.material_match import audio_tags_in_order
    vmodel = generation_service.resolve_video_model(proj["settings"]["video_model"])
    mats_full = {m["id"]: Material.model_validate(m) for m in proj["materials"]}
    for i, s in enumerate(proj["scenes"], 1):
        tags = sorted(set(audio_tags_in_order(s["prompt"])))
        n_bound = len(s["binding"]["audios"])
        check(f"镜{i:02d} 音频编号统一为本镜 1..{n_bound}", tags == list(range(1, n_bound + 1)), f"tags={tags}")
        req = generation_service.assemble_video_request(Scene.model_validate(s), mats_full, vmodel)
        check(f"镜{i:02d} 组装闸门放行", req["contract_error"] is None, str(req["contract_error"]))

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
