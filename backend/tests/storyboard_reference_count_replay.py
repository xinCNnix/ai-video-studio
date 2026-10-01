"""闯西游第二回事故回归（project_f1a9f3d4）：双倍行距分镜稿的 Reference 声明区解析。

事故（2026-09-21）：用户稿每个片段的 Reference 区列明该镜参考图（如片段2 共 5 张），
但稿子是 markdown 双倍行距——「Reference」标题后的第一个空行把声明区判死，7 拍全部
解析为空 → bind 的「storyboard 已按原稿对齐」保护失效 → plan 猜测 + 兜底 LLM 把
<Image 6>（3.png）塞进 6 个镜：剧本写 5 张、实际绑 6 张，用户被迫逐镜手删。

重放真实项目的 screenplay/scenes/materials（plan 的错误 wants 原样带入），验证：
1. 7 镜全部按原稿 Reference 声明确定性对齐：图片张数与集合逐镜等于声明（3/5/5/5/4/5/4）
2. plan 的多余 wants 被对齐覆盖：原稿没声明的图绝不进该镜（场景1 不再多绑 3.png）
3. 对齐吃掉全部素材，兜底 LLM 一次都不被调用；prompt 声明行身份取自原稿编号表
4. 组装闸门放行（音频标签数=传入音频数，无悬空引用）

运行：.venv/Scripts/python.exe tests/storyboard_reference_count_replay.py
数据源：../data/projects/project_f1a9f3d4.json（若不存在则跳过）
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DATA_SRC = Path(__file__).resolve().parents[2] / "data" / "projects" / "project_f1a9f3d4.json"
if not DATA_SRC.exists():
    print("skip：找不到真实项目数据，跳过重放")
    sys.exit(0)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="xiyou_refcount_")

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

# 原稿 Reference 声明（编号=上传顺序）→ 期望绑定（语义键=剥哈希前缀的文件名，按声明顺序）
# 片段1: Image 3,1,2｜片段2: 4,5,1,2,3｜片段3: 6或7,1,2,3｜片段4: 7,5,1,2,3
# 片段5: 10,3,2,8｜片段6: 8,9,3,1,2｜片段7: 3,9,1,2
EXPECTED_IMAGES = [
    ["6", "吕满春", "小墨"],
    ["1", "2", "吕满春", "小墨", "6"],
    ["3", "4", "吕满春", "小墨", "6"],
    ["4", "2", "吕满春", "小墨", "6"],
    ["7", "6", "小墨", "5"],
    ["5", "126", "6", "吕满春", "小墨"],
    ["6", "126", "吕满春", "小墨"],
]
# 原稿 Audio Timeline 声明的台词数（片段7 全镜无对白）
EXPECTED_AUDIO_COUNTS = [2, 2, 2, 2, 1, 1, 0]


class NoFallbackLLM:
    """对齐吃掉全部素材后，bind 不应再求助兜底 LLM——被调用即测试失败"""

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        self.calls += 1
        return ""


def main() -> None:
    p = project_store.create("闯西游第二回回归", "9:16")
    PID = p["id"]

    def inject(prj):
        from app.harness.state import Material, Scene
        for m in REAL["materials"]:
            prj.materials.append(Material(**{k: m[k] for k in ("id", "kind", "url", "filename", "text", "duration") if k in m}))
        # screenplay 原样注入：wants 里带着事故现场 plan 的过度绑定意图，bind 必须覆盖它
        prj.screenplay = json.loads(json.dumps(REAL["screenplay"]))
        prj.scenes = [Scene(**{**{k: v for k, v in s.items() if k != "binding"}, "binding": {}}) for s in REAL["scenes"]]
        merged = prj.settings.model_dump()
        merged["video_model"] = "minimax_h3_zm_u24"
        prj.settings = type(prj.settings).model_validate(merged)

    project_store.mutate(PID, inject)

    nollm = NoFallbackLLM()
    env = llm_client.LLMEnvelope(nollm, "m")
    r = asyncio.run(stages.run_bind(PID, env))
    proj = project_store.get(PID)

    from app.services.material_match import semantic_file_key
    mats = {m["id"]: m for m in proj["materials"]}

    def bound_keys(scene):
        return [semantic_file_key(mats[x["material_id"]]["filename"]) for x in scene["binding"]["images"]]

    check("7 镜全部按原稿 Reference 声明对齐（空行不再判死声明区）",
          r.get("reference_aligned_scenes") == 7, json.dumps(r, ensure_ascii=False)[:200])
    for i, (s, exp) in enumerate(zip(proj["scenes"], EXPECTED_IMAGES), 1):
        check(f"镜{i} 参考图={exp}（张数与集合逐镜等于原稿声明，无 LLM 加料）",
              bound_keys(s) == exp, f"{bound_keys(s)}")
    for i, (s, n) in enumerate(zip(proj["scenes"], EXPECTED_AUDIO_COUNTS), 1):
        check(f"镜{i} 台词音频 {n} 段", len(s["binding"]["audios"]) == n,
              f"{[mats[x]['filename'] for x in s['binding']['audios']]}")
    check("兜底 LLM 零调用（原稿没引用的素材不强塞任何镜）", nollm.calls == 0, f"calls={nollm.calls}")
    check("prompt 无全局 <Image N> 残留", all("<Image " not in s["prompt"] for s in proj["scenes"]))
    check("定义行身份来自原稿编号表（Image 3=五行山基准图，非模型猜测）",
          "<Picture 1> is the reference for: 五行山 + 孙悟空基础场景 (original storyboard Image 3)"
          in proj["scenes"][0]["prompt"],
          "\n".join(ln for ln in proj["scenes"][0]["prompt"].splitlines() if "<Picture" in ln)[:300])

    # 组装闸门：音频标签数=传入音频数、无悬空 <Picture k>，contract_error 必须为空
    from app.harness.state import Material, Scene
    from app.services import generation_service
    from app.services.material_match import audio_tags_in_order
    vmodel = generation_service.resolve_video_model(proj["settings"]["video_model"])
    mats_full = {m["id"]: Material.model_validate(m) for m in proj["materials"]}
    for i, s in enumerate(proj["scenes"], 1):
        tags = sorted(set(audio_tags_in_order(s["prompt"])))
        n_bound = len(s["binding"]["audios"])
        check(f"镜{i} 音频编号统一为本镜 1..{n_bound}", tags == list(range(1, n_bound + 1)), f"tags={tags}")
        req = generation_service.assemble_video_request(Scene.model_validate(s), mats_full, vmodel)
        check(f"镜{i} 组装闸门放行", req["contract_error"] is None, str(req["contract_error"]))

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
