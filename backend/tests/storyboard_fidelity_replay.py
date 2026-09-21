"""storyboard 保真回归：用真实事故项目（吕满春第6集，project_6bfb8d19）的原剧本与
双批次重复素材离线重放新流水线，验证三件事：

1. 素材分配照抄原视频：原稿 Reference 声明的 <Image N>/<Audio N> 按上传顺序确定性
   对齐（片段1 拿 Image 1/3/5，片段3 拿 Image 7 而不是片段4 的 Image 8……），
   plan 阶段对 UUID 图的瞎猜被覆盖
2. normalize 逐字保真：片段原文（含 Reference/Audio Timeline/H3 Prompt/否定约束）
   从原稿切片回收存 screenplay.beats（前端抽屉窗口展示用）；prompt 不再拼原文块
   （对视频模型无用且中文台词行会诱导叠配音轨），<Image N> 重编号为 <Picture k>
3. 整包重传的重复素材在 intake/bind 两层都被折叠，不再灌进绑定、不再进 pending

运行：.venv/Scripts/python.exe tests/storyboard_fidelity_replay.py
数据源：../data/projects/project_6bfb8d19.json（若不存在则跳过）
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DATA_SRC = Path(__file__).resolve().parents[2] / "data" / "projects" / "project_6bfb8d19.json"
if not DATA_SRC.exists():
    print("skip：找不到真实项目数据，跳过重放")
    sys.exit(0)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="storyboard_replay_")

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


class FakeLLM:
    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        self.calls += 1
        item = self.outputs.pop(0) if self.outputs else ""
        if isinstance(item, Exception):
            raise item
        return item


REAL = json.loads(DATA_SRC.read_text(encoding="utf-8"))
SCRIPT = next(m["text"] for m in REAL["materials"] if m["kind"] == "script" and "第6集｜" in (m["text"] or ""))
# 两批重复素材原样重放（batch1 先、batch2 后，与事故现场一致）
FILE_MATS = [m for m in REAL["materials"] if m["kind"] in ("image", "audio")]
FIRST_BATCH = FILE_MATS[: len(FILE_MATS) // 2]
SECOND_BATCH = FILE_MATS[len(FILE_MATS) // 2:]

# 原稿片段边界：H3片段N 标题行
FRAG_STARTS = [m.start() for m in re.finditer(r"^H3片段\d", SCRIPT, re.M)]


def frag(i: int) -> str:
    end = FRAG_STARTS[i + 1] if i + 1 < len(FRAG_STARTS) else len(SCRIPT)
    return SCRIPT[FRAG_STARTS[i]:end]


def sem(name: str) -> str:
    from app.services.material_match import semantic_file_key
    return semantic_file_key(name)


def main() -> None:
    p = project_store.create("吕满春第6集回归", "9:16")
    PID = p["id"]

    def set_model(model_id: str) -> None:
        def fn(prj):
            merged = prj.settings.model_dump()
            merged["video_model"] = model_id
            prj.settings = type(prj.settings).model_validate(merged)
        project_store.mutate(PID, fn)

    set_model("minimax_h3_zm_u24")

    # ---- intake：双批次重传，身份去重后不得翻倍 ----
    payload_mats = [{"url": m["url"], "kind": m["kind"], "filename": m["filename"]} for m in FIRST_BATCH + SECOND_BATCH]
    stages.run_intake(PID, {"script_text": SCRIPT, "materials": payload_mats})
    proj = project_store.get(PID)
    imgs = [m for m in proj["materials"] if m["kind"] == "image"]
    auds = [m for m in proj["materials"] if m["kind"] == "audio"]
    check("intake 双批次重传被折叠：8图6音1剧本", len(imgs) == 8 and len(auds) == 6 and len(proj["materials"]) == 15,
          f"imgs={len(imgs)} auds={len(auds)} total={len(proj['materials'])}")

    # ---- normalize：FakeLLM 按块响应（只回缩写版 source_text），逐字回收必须以原文切片为准 ----
    titles = ["H3片段1", "H3片段2", "H3片段3", "H3片段4"]

    class ChunkAwareNormLLM:
        async def generate(self, messages, model, temperature=0.7, max_tokens=None):
            user = messages[-1]["content"]
            beats = []
            for i, t in enumerate(titles):
                if t in user:  # 该片段落在本块
                    beats.append({
                        "title": t, "description": f"{t} 的转述摘要", "narration": "……", "source_hint": "",
                        # 缩写回声：片段前 400 字 + 省略号——回收应定位起点并切到下一片段头
                        "source_text": frag(i)[:400] + "……（后略）",
                    })
            if not beats:
                raise AssertionError("分块里找不到任何片段标题，测试构造有误")
            return json.dumps({
                "script_type": "storyboard", "logline": "白斑科普",
                # 复现真实事故：剧本通篇无性别词，normalize 脑补"女性医者形象"
                "characters": [
                    {"name": "吕满春", "design": "女性医者形象，参考人物图，站立，穿白大褂，五官、年龄感、发型和整体气质严格保持一致。"},
                    {"name": "小墨", "design": "3D卡通角色，参考角色图，保持既定3D卡通形象与比例，表情生动活泼。"},
                ],
                "beats": beats,
            }, ensure_ascii=False)

    env = llm_client.LLMEnvelope(ChunkAwareNormLLM(), "m")
    asyncio.run(stages.run_normalize(PID, env))
    proj = project_store.get(PID)
    scr = proj["screenplay"]
    check("normalize 全部片段逐字回收", all(b.get("source_fidelity") == "verbatim" for b in scr["beats"]),
          f"{[b.get('source_fidelity') for b in scr['beats']]}")
    check("片段1 原文含 Reference 声明与否定约束",
          "Reference" in scr["beats"][0]["source_text"] and "绝对不要出现黑色描边" in scr["beats"][0]["source_text"])
    check("全局参考图编号表进 global_notes", "<Image 8>｜演播室总结" in scr.get("global_notes", ""),
          scr.get("global_notes", "")[:80])
    lv = next(c for c in scr["characters"] if c["name"] == "吕满春")
    check("角色性别接地：无剧本依据的'女性医者'被剔除并锚定参考图",
          "女性" not in lv["design"] and "医者形象" in lv["design"] and "原稿 <Image 1>" in lv["design"]
          and "不得自行设定" in lv["design"], lv["design"])

    # ---- plan：重放事故现场的错误 wants（片段1挂小墨图、片段3挂Image8），bind 应覆盖 ----
    # H3 契约：prompt 必须六段式（PlanBatchH3 闸门）。真实旧 prompt 是 Action Timeline
    # 式，这里剥掉旧声明行后包进六段骨架——夹具只验证结构契约与 bind 覆盖逻辑
    def to_h3(body: str, wants_note: str) -> str:
        body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("<Picture"))
        return (
            "subject_definitions:\n"
            "<Subject 1> is a female doctor 吕满春 anchored by <Picture 1>.\n"
            f"<Picture 1> is the identity anchor for 吕满春（plan 猜测：{wants_note}）.\n"
            "<Picture 2> is the layout anchor guessed by plan.\n"
            "<Picture 3> is the shot keyframe anchor guessed by plan.\n"
            "<Audio 1> is the spoken line for this shot.\n\n"
            "summary:\n[reference generation + audio reuse] The target clip shows <Subject 1> in the virtual "
            "studio, using <Picture 1>-<Picture 3> as visual anchors and <Audio 1> as the spoken line.\n\n"
            "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - identity follows the anchor.\n"
            "<Picture 1>: fully_preserved - identity anchor.\n\n"
            f"detailed_description:\n[Shot 1] {body}\n\n"
            "overall_soundscape: Only the reference audio and a faint virtual-studio room tone.\n\n"
            "non_diegetic_music: N/A"
        )

    real_scenes = REAL["scenes"]
    wrong_wants = {w["scene_id"]: w for w in (REAL["screenplay"].get("wants") or [])}
    plan_shots = []
    for i, s in enumerate(real_scenes):
        w = wrong_wants.get(s["id"]) or {}
        plan_shots.append({
            "title": s["title"], "description": s["description"],
            "prompt": to_h3(s["prompt"], f"镜{i + 1}错误wants"),
            "keyframe_prompt": s.get("keyframe_prompt") or "", "narration": s["narration"],
            "duration": s["duration"], "gen_mode": "i2v",
            "wants_images": w.get("wants_images") or [], "wants_audios": w.get("wants_audios") or [],
            "wants_videos": [],
        })
    plan_output = json.dumps({"shots": plan_shots, "total": len(plan_shots), "done": True}, ensure_ascii=False)
    env = llm_client.LLMEnvelope(FakeLLM([plan_output]), "m")
    asyncio.run(stages.run_plan(PID, env))
    proj = project_store.get(PID)
    check("prompt 不再拼原文块（原文只在 screenplay.beats 供前端展示）",
          all("## 原文" not in s["prompt"] for s in proj["scenes"]))
    check("prompt 里模型脑补的性别词全被剔除（female/女性，中文正文与英文润色段）",
          all("female" not in s["prompt"].lower() and "女性" not in s["prompt"] for s in proj["scenes"]),
          "\n".join(ln for s in proj["scenes"] for ln in s["prompt"].splitlines()
                    if "female" in ln.lower() or "女性" in ln)[:200])

    # ---- bind：无需 LLM（对齐吃掉全部素材），兜底 envelope 给空输出防意外调用 ----
    env = llm_client.LLMEnvelope(FakeLLM([]), "m")
    r = asyncio.run(stages.run_bind(PID, env))
    proj = project_store.get(PID)
    mats = {m["id"]: m for m in proj["materials"]}

    def bound(scene):
        return [sem(mats[x["material_id"]]["filename"]) for x in scene["binding"]["images"]]

    def bound_audio(scene):
        from app.services.material_match import audio_semantic
        return [audio_semantic(mats[x]["filename"]) for x in scene["binding"]["audios"]]

    s1, s2, s3, s4 = proj["scenes"]
    check("对齐了 4 镜", r.get("reference_aligned_scenes") == 4, f"{r}")
    check("片段1 参考图=Image1/3/5（双人图替换瞎猜的小墨图）",
          bound(s1) == ["lvmanchun", "lvmanchundualren", "04281dce-5817-4ac1-a57f-5ac61b9e2814"], f"{bound(s1)}")
    check("片段2 参考图=Image1/4/6",
          bound(s2) == ["lvmanchun", "小墨", "99045a54-7f43-478c-bb7d-0fe78d339f1c"], f"{bound(s2)}")
    check("片段3 参考图=Image1/4/7（不再错挂片段4的Image8）",
          bound(s3) == ["lvmanchun", "小墨", "6007421b-3d21-4733-913d-c8831f827ede"], f"{bound(s3)}")
    check("片段4 参考图=Image1/3/8（自己的总结图回来了）",
          bound(s4) == ["lvmanchun", "lvmanchundualren", "dc74a41d-1a91-4d07-9bcc-d6e95e89d681"], f"{bound(s4)}")
    check("片段1 台词音频=Audio1+Audio2（序号前缀不再脱靶；顺序由提交时按台词位置重排）",
          set(bound_audio(s1)) == {"白斑边缘突然变清楚了这说明什么呢", "是不是边界清楚了就说明已经稳定啦"}, f"{bound_audio(s1)}")
    check("片段3 台词音频=Audio4+Audio5",
          set(bound_audio(s3)) == {"还要看看白斑面积有没有变化其他地方有没有新出现的白斑", "明白了不能只盯着边界还得看一段时间里的整体变化"}, f"{bound_audio(s3)}")
    check("片段2/4 台词音频各就各位",
          set(bound_audio(s2)) == {"不一定边缘变化是一个观察线索但不能只靠这一点判断"} and
          set(bound_audio(s4)) == {"对发现边缘明显变化可以拍照记录复诊时结合其他表现一起判断"},
          f"s2={bound_audio(s2)} s4={bound_audio(s4)}")
    check("原稿未引用的 Image2（单人演播室图）不强塞任何镜",
          all("lvmanchundanren00" not in x for x in (bound(s1), bound(s2), bound(s3), bound(s4))))
    check("没有待确认绑定（重复拷贝不再进 pending）", proj["pending_bindings"] == [] and r["pending"] == 0,
          f"{proj['pending_bindings']}")

    check("prompt 里 <Image N> 标签清零（原文块不再进 prompt，无全局编号残留）",
          all("<Image " not in s["prompt"] for s in proj["scenes"]))
    check("六段式契约保持（H3 段名齐全）",
          all(all(m in s["prompt"].lower() for m in
                  ("subject_definitions", "summary", "retention_analysis", "detailed_description",
                   "overall_soundscape", "non_diegetic_music")) for s in proj["scenes"]))
    check("片段1 参考图定义行来自原稿编号表（H3 subject_definitions 格式）",
          "<Picture 2> is the reference for: 吕满春 + 小墨演播室参考" in s1["prompt"]
          and "<Picture 3> is the reference for: 演播室：边缘由模糊到清楚" in s1["prompt"],
          "\n".join(ln for ln in s1["prompt"].splitlines() if "<Picture" in ln))
    check("片段3 定义行身份不再是幻觉的'新发小白斑'",
          "<Picture 3> is the reference for: 微观世界：不能只看边界" in s3["prompt"])
    check("plan 猜错被覆盖的图在润色段降级为纯文本（无悬空 <Picture> 错位）",
          "not bound to this shot" in s1["prompt"] or "<Picture 2> is the reference for: 吕满春 + 小墨演播室参考" in s1["prompt"])

    # ---- 音频编号两域统一（复现事故：正文用本镜 <Audio 1>，原文块沿用全集全局号
    #      <Audio 3~6>，组装闸门"引用了 N 个音频标签但只传入 M 段音频"拦下第 2/3/4 镜）----
    from app.harness.state import Material, Scene
    from app.services import generation_service
    from app.services.material_match import audio_tags_in_order
    vmodel = generation_service.resolve_video_model(proj["settings"]["video_model"])
    mats_full = {m["id"]: Material.model_validate(m) for m in proj["materials"]}
    for i, s in enumerate(proj["scenes"]):
        tags = sorted(set(audio_tags_in_order(s["prompt"])))
        n_bound = len(s["binding"]["audios"])
        check(f"镜{i + 1} 音频编号统一为本镜 1..{n_bound}（原文块全局号已改，无两域并存）",
              tags == list(range(1, n_bound + 1)), f"tags={tags} bound={n_bound}")
        req = generation_service.assemble_video_request(Scene.model_validate(s), mats_full, vmodel)
        check(f"镜{i + 1} 组装闸门放行（无 contract_error）", req["contract_error"] is None,
              str(req["contract_error"]))

    # ---- 老项目直接治愈验证：32 素材的既有脏池（intake 没拦住）在 bind 层折叠 ----
    from app.harness.state import Material
    p2 = project_store.create("脏池治愈", "9:16")
    PID2 = p2["id"]

    def inject(p):
        for m in REAL["materials"]:
            p.materials.append(Material(**{k: m[k] for k in ("id", "kind", "url", "filename", "text", "duration") if k in m}))
        # 直接复用上一段流水线的 screenplay/scenes 结构，只换项目与素材池
        src = project_store.get(PID)
        p.screenplay = json.loads(json.dumps(src["screenplay"]))
        # screenplay.wants 指向 PID 的 scene id，PID2 重建同构 scenes
        from app.harness.state import Scene
        p.scenes = [Scene(**{**{k: v for k, v in s.items() if k != "binding"}, "binding": {}}) for s in src["scenes"]]
        # 修正 wants 指向：按位置对应
        old_ids = [s["id"] for s in src["scenes"]]
        new_ids = [s.id for s in p.scenes]
        remap = dict(zip(old_ids, new_ids))
        p.screenplay["wants"] = [
            {"scene_id": remap.get(w["scene_id"], w["scene_id"]),
             "wants_images": w.get("wants_images") or [], "wants_audios": w.get("wants_audios") or [],
             "wants_videos": w.get("wants_videos") or []}
            for w in p.screenplay.get("wants") or []
        ]

    project_store.mutate(PID2, inject)
    env = llm_client.LLMEnvelope(FakeLLM([]), "m")
    r2 = asyncio.run(stages.run_bind(PID2, env))
    proj2 = project_store.get(PID2)
    scene_bindings = [len(s["binding"]["images"]) for s in proj2["scenes"]]
    check("脏素材池（32个，含重复拷贝）bind 层折叠后分配正确",
          r2.get("ignored_duplicates", 0) >= 16 and scene_bindings == [3, 3, 3, 3] and proj2["pending_bindings"] == [],
          f"dups={r2.get('ignored_duplicates')} bindings={scene_bindings} pending={len(proj2['pending_bindings'])}")

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
