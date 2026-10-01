"""pipeline_v2_selfcheck：新流水线端到端 + LLM 信封故障注入矩阵（隔离 DATA_DIR，不联网）。

覆盖：
- 信封：干净/围栏/字符串换行/混排 JSON 解析；坏 schema 回喂修复；持续失败 Fatal；网络错误退避重试
- normalize：剧本→Screenplay 落盘
- plan：t2v 遇纯参考模型自动转 i2v、时长钳制、角色卡注入、wants 持久化
- bind：音频台词对号分发、未命中进 pending、LLM 高置信绑定参考图
- runner：start→review 暂停；advance(confirmed) → generate/assemble（打桩）→ done；
  single-flight 互斥；崩溃悬空 running 可续跑
运行：.venv/Scripts/python.exe tests/pipeline_v2_selfcheck.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pipeline_v2_selfcheck_")

from app.harness.errors import FatalError, RunBusyError  # noqa: E402
from app.harness.watchdog import watchdog  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.services import project_store  # noqa: E402
from app.services.pipeline import advance_pipeline, start_pipeline  # noqa: E402
import app.services.pipeline as pipe_mod  # noqa: E402
from app.services.pipeline import llm_client, stages  # noqa: E402
from app.services.pipeline.schemas import Screenplay  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


class FakeLLM:
    """脚本化假 LLM：按序返回预设输出（str 或 Exception）。

    director 润色遍（system 含「润色导演」）以达标 canned 六段式应答——润色遍是
    run_plan 的必经调用，不接管会吃空脚本序列、触发空输出退避重试。"""

    POLISH_OK = (
        "subject_definitions:\n<Subject 1> is a stylized 3D character.\n\n"
        "summary:\n[reference generation] One continuous shot.\n\n"
        "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - identity follows the anchor.\n\n"
        "detailed_description:\n[Shot 1] "
        + "The camera moves forward at slow speed while the subject walks ahead steadily. " * 40
        + "\n\noverall_soundscape:\nQuiet morning city tone.\n\nnon_diegetic_music: N/A"
    )

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        self.calls += 1
        if messages and "润色导演" in (messages[0].get("content") or ""):
            return json.dumps({"prompt": self.POLISH_OK}, ensure_ascii=False)
        item = self.outputs.pop(0) if self.outputs else ""
        if isinstance(item, Exception):
            raise item
        return item


async def _wait_pipeline(pid, timeout_s=10.0, newer_than: str | None = None):
    """等流水线到暂停/终态。newer_than：只认发生时间晚于该时间戳的状态变化（防读到旧状态）。"""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proj = project_store.get(pid)
        pipeline = proj["pipeline"]
        last_at = (pipeline.get("history") or [{}])[-1].get("at", "")
        if newer_than is None or last_at > newer_than:
            if pipeline["state"] in ("paused", "failed", "done"):
                return proj
        await asyncio.sleep(0.05)
    return project_store.get(pid)


def _last_history_at(pid: str) -> str:
    proj = project_store.get(pid)
    return (proj["pipeline"].get("history") or [{}])[-1].get("at", "")


def main() -> None:
    # ---------------- 1. 信封故障注入矩阵 ----------------
    GOOD = {
        "script_type": "story", "logline": "测试",
        "characters": [{"name": "小墨", "design": "黑色短发男孩，蓝色卫衣"}],
        "beats": [{"title": "开场", "description": "清晨的城市街道", "narration": "城市醒了", "source_hint": ""}],
        "notes": "",
    }
    GOOD_JSON = json.dumps(GOOD, ensure_ascii=False)

    env = llm_client.LLMEnvelope(FakeLLM([GOOD_JSON]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("干净 JSON 一次通过", r.script_type == "story")

    env = llm_client.LLMEnvelope(FakeLLM([f"```json\n{GOOD_JSON}\n```"]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("围栏 JSON 解析", r.beats[0].title == "开场")

    bad_newlines = '{"script_type": "story", "beats": [{"title": "多行\n标题", "description": "d", "narration": "n"}], "characters": []}'
    env = llm_client.LLMEnvelope(FakeLLM([bad_newlines]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("字符串内真实换行被修复", r.beats[0].title == "多行\n标题")

    env = llm_client.LLMEnvelope(FakeLLM([f"好的，以下是解析结果：\n{GOOD_JSON}\n以上。"]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("混排文本中扫描出 JSON", r.script_type == "story")

    env = llm_client.LLMEnvelope(FakeLLM(['{"script_type": "story", "beats": []}', GOOD_JSON]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("校验失败回喂修复成功", len(r.beats) == 1 and env.llm.calls == 2)

    env = llm_client.LLMEnvelope(FakeLLM(["不是 JSON", "还是不是"]), "m", max_repair=1)
    try:
        asyncio.run(env.structured_call("s", "u", Screenplay))
        check("持续失败应 Fatal", False)
    except FatalError:
        check("持续失败抛 Fatal", True)

    env = llm_client.LLMEnvelope(FakeLLM([ProviderError("网络抖动"), ProviderError("超时"), GOOD_JSON]), "m")
    r = asyncio.run(env.structured_call("s", "u", Screenplay))
    check("网络错误退避重试后成功", r.script_type == "story" and env.llm.calls == 3)

    # ---------------- 2. 建项目 + intake ----------------
    p = project_store.create("流水线测试", "16:9")
    PID = p["id"]

    def set_model(model_id: str) -> None:
        def fn(prj):
            merged = prj.settings.model_dump()
            merged["video_model"] = model_id
            prj.settings = type(prj.settings).model_validate(merged)
        project_store.mutate(PID, fn)

    set_model("minimax_h3_zm_u24")  # 仅支持 image/reference，4-15s

    SCRIPT = "【第一镜】清晨，男孩小墨背着书包走在上学路上。旁白：城市醒了。\n【第二镜】教室里，小墨打开课本。旁白：新的一天开始了。"
    stages.run_intake(PID, {"script_text": SCRIPT, "materials": [
        {"url": "/api/files/uploads/image/aaa_小墨设定图.png", "kind": "image", "filename": "小墨设定图.png"},
        {"url": "/api/files/uploads/audio/bbb_tts_1_城市醒了.mp3", "kind": "audio", "filename": "tts_城市醒了.mp3"},
        {"url": "/api/files/uploads/audio/ccc_tts_1_完全无关的句子.mp3", "kind": "audio", "filename": "tts_完全无关的句子.mp3"},
    ]})
    proj = project_store.get(PID)
    kinds = sorted(m["kind"] for m in proj["materials"])
    check("intake 登记 script+2音频+1图", kinds == ["audio", "audio", "image", "script"], f"{kinds}")

    # ---------------- 3. normalize ----------------
    norm_output = json.dumps({
        "script_type": "storyboard", "logline": "上学",
        "characters": [{"name": "小墨", "design": "黑色短发男孩，蓝色卫衣"}],
        "beats": [
            {"title": "上学路", "description": "清晨街道，小墨背书包", "narration": "城市醒了", "source_hint": "【第一镜】"},
            {"title": "教室", "description": "教室里翻开课本", "narration": "新的一天开始了", "source_hint": "【第二镜】"},
        ],
    }, ensure_ascii=False)
    env = llm_client.LLMEnvelope(FakeLLM([norm_output]), "m")
    r = asyncio.run(stages.run_normalize(PID, env))
    check("normalize 落盘 screenplay", r["beats"] == 2 and project_store.get(PID)["screenplay"]["script_type"] == "storyboard")

    # ---------------- 4. plan（H3 模型 → Ref2VA 六段式契约） ----------------
    H3_PROMPT_S1 = (
        "subject_definitions:\n"
        "<Subject 1> is the boy 小墨 anchored by <Picture 1>.\n"
        "<Picture 1> is the identity anchor for 小墨 in this shot.\n"
        "<Audio 1> is the spoken narration line for this shot.\n\n"
        "summary:\n[reference generation + audio reuse] The target clip shows <Subject 1> walking to school, "
        "using <Picture 1> as the identity anchor and <Audio 1> as the spoken line.\n\n"
        "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - identity follows the anchor image.\n"
        "<Audio 1>: fully_copy - reused as the spoken line.\n\n"
        "detailed_description:\n[Shot 1] A quiet morning street; <Subject 1> crosses the crosswalk. "
        "The camera pushes in with small amplitude at slow speed. "
        "<Subject 1> (S1) delivers the spoken line of <Audio 1> with lips synchronized.\n\n"
        "overall_soundscape: Light morning traffic ambience with a faint breeze.\n\n"
        "non_diegetic_music: N/A"
    )
    H3_PROMPT_S2 = (
        "subject_definitions:\n"
        "<Subject 1> is the boy 小墨 in a sunny classroom.\n\n"
        "summary:\n[reference generation] The target clip shows <Subject 1> opening a textbook in class.\n\n"
        "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved.\n\n"
        "detailed_description:\n[Shot 1] Sunlight through windows; <Subject 1> opens the textbook. " + "Detail filler. " * 12 + "\n\n"
        "overall_soundscape: Soft classroom room tone.\n\n"
        "non_diegetic_music: N/A"
    )
    plan_batch = json.dumps({
        "shots": [
            {
                "title": "上学路", "description": "清晨街道",
                "prompt": H3_PROMPT_S1,
                "keyframe_prompt": "中景，街道，晨光，小墨背影", "narration": "城市醒了",
                "duration": 99, "gen_mode": "i2v",
                "wants_images": [], "wants_audios": [],
            },
            {
                "title": "教室", "description": "教室",
                "prompt": H3_PROMPT_S2,
                "keyframe_prompt": "", "narration": "新的一天开始了",
                "duration": 5, "gen_mode": "t2v", "wants_images": [], "wants_audios": [],
            },
        ],
        "total": 2, "done": True,
    }, ensure_ascii=False)
    env = llm_client.LLMEnvelope(FakeLLM([plan_batch]), "m")
    r = asyncio.run(stages.run_plan(PID, env))
    proj = project_store.get(PID)
    s1, s2 = proj["scenes"]
    check("plan 产出分镜并持久化", len(proj["scenes"]) == 2)
    check("时长 99 被钳制到模型上限 15", s1["duration"] == 15, f"{s1['duration']}")
    check("t2v 遇纯参考模型自动转 i2v 且补关键帧描述", s2["gen_mode"] == "i2v" and s2["keyframe_prompt"], f"{s2['gen_mode']}")
    check("角色设定卡注入无参考图镜", "Character Reference" in s2["prompt"] and "小墨" in s2["prompt"])
    check("wants 持久化到 screenplay", len(proj["screenplay"]["wants"]) == 2)

    # H3 六段式闸门：非六段式 prompt 被拒且错误点名缺失段（信封会把清单回喂修复）
    from app.services.pipeline.schemas import PlanBatchH3

    legacy_shot = {"title": "旧式", "description": "d",
                   "prompt": "清晨街道画面总述。## Action Timeline 0-3s 行走，3-6s 特写。",
                   "keyframe_prompt": "", "narration": "", "duration": 5, "gen_mode": "i2v",
                   "wants_images": [], "wants_audios": [], "wants_videos": []}
    try:
        PlanBatchH3.model_validate({"shots": [legacy_shot], "total": 1, "done": True})
        check("H3 闸门拒绝非六段式 prompt", False)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        check("H3 闸门拒绝非六段式 prompt 并点名缺失段",
              "subject_definitions" in str(exc) and "non_diegetic_music" in str(exc), str(exc)[:120])
    h3_ok = PlanBatchH3.model_validate(
        {"shots": [{**legacy_shot, "prompt": H3_PROMPT_S1}], "total": 1, "done": True})
    check("H3 闸门放行六段式 prompt", len(h3_ok.shots) == 1)

    # 角色性别接地（吕满春"女性医者"事故）：剧本无依据的性别词剔除并锚定参考图；有依据保留
    grounded, _ = stages.ground_characters(
        {"吕满春": "女性医者形象，穿白大褂。", "小墨": "3D卡通角色。"},
        "<Image 1>｜吕满春人物多角度参考\nH3片段1\n吕满春：白斑边缘变清楚了。")
    check("接地：无剧本依据的性别词剔除+参考图锚定",
          "女性" not in grounded["吕满春"] and "医者形象" in grounded["吕满春"]
          and "原稿 <Image 1>" in grounded["吕满春"], f"{grounded}")
    keep, _ = stages.ground_characters({"王芳": "女医生，白大褂。"}, "王芳是位女医生。\n<Image 1>｜环境参考")
    check("接地：剧本写明的性别保留", "女医生" in keep["王芳"], f"{keep}")
    check("接地：'其他'不构成性别依据（他≠其他）",
          not stages._has_gender_basis("吕满春：还要看看白斑面积有没有变化，其他地方有没有新出现的白斑。"))
    check("接地：'她'指代构成性别依据", stages._has_gender_basis("吕满春提问，她看着小墨。"))

    # 剧本取最新投放稿（草稿迭代，旧稿先入池不能赢）
    from app.harness.state import Material as _Mat, Project as _Project
    p_latest = project_store.create("草稿迭代", "16:9")["id"]

    def _inject_scripts(p):
        p.materials.append(_Mat(id="mat_old", kind="script", text="旧草稿：第一镜……", filename="old.txt"))
        p.materials.append(_Mat(id="mat_new", kind="script", text="定稿：第一镜（新版）", filename="new.txt"))

    project_store.mutate(p_latest, _inject_scripts)
    src, mat = stages._script_source(_Project.model_validate(project_store.get(p_latest)))
    check("剧本取最后投放的一份（定稿）", src.startswith("定稿") and mat.id == "mat_new", f"{src[:20]}")

    # ---------------- 5. bind ----------------
    img_id = next(m["id"] for m in proj["materials"] if m["kind"] == "image")
    bind_output = json.dumps({"decisions": [
        {"material_id": img_id, "scene_index": 0, "kind": "image", "role": "reference", "confidence": 0.95, "reason": "角色设定图"},
    ]}, ensure_ascii=False)
    env = llm_client.LLMEnvelope(FakeLLM([bind_output]), "m")
    r = asyncio.run(stages.run_bind(PID, env))
    proj = project_store.get(PID)
    s1, s2 = proj["scenes"]
    check("LLM 兜底高置信绑定参考图", any(b["material_id"] == img_id for b in s1["binding"]["images"]))
    audio_hit = next(m["id"] for m in proj["materials"] if "城市醒了" in m["filename"])
    check("音频按台词对号分发到镜1", audio_hit in s1["binding"]["audios"], f"{s1['binding']}")
    check("未命中台词音频进 pending 不硬塞", any("完全无关" in (pb["note"] or "") for pb in proj["pending_bindings"]), f"{proj['pending_bindings']}")
    check("镜2 不背无关素材", s2["binding"]["audios"] == [] and s2["binding"]["images"] == [])

    # 首帧闸门：LLM 产物（wants/兜底）自标 first_frame 一律按 reference 落盘——
    # 首帧仅首尾帧模式由用户手动指定（历史事故：plan 给一镜标双首帧，不变量整单拒绝导致 bind 卡死）
    ff2, ff3, ff4 = "mat_ff_gate_2", "mat_ff_gate_3", "mat_ff_gate_4"

    def _inject_ff_wants(p):
        from app.harness.state import Material
        for mid, fname in ((ff2, "状态A图.png"), (ff3, "状态B图.png"), (ff4, "状态C图.png")):
            p.materials.append(Material(id=mid, kind="image",
                                        url=f"/api/files/uploads/image/zzz_{fname}", filename=fname))
        p.screenplay["wants"] = [
            {"scene_id": s1["id"], "wants_images": [
                {"material_id": img_id, "description": "设定图", "role": "reference"},
                {"material_id": ff2, "description": "状态A", "role": "first_frame"},
                {"material_id": ff3, "description": "状态B", "role": "first_frame"},
            ], "wants_audios": [], "wants_videos": []},
        ]

    project_store.mutate(PID, _inject_ff_wants)
    ff_bind_output = json.dumps({"decisions": [
        {"material_id": ff4, "scene_index": 0, "kind": "image", "role": "first_frame", "confidence": 0.95, "reason": "状态C"},
    ]}, ensure_ascii=False)
    try:
        asyncio.run(stages.run_bind(PID, llm_client.LLMEnvelope(FakeLLM([ff_bind_output]), "m")))
        bind_ok = True
    except Exception as exc:  # noqa: BLE001 - 不变量拒绝会让 run_bind 直接抛错
        bind_ok = False
        print(f"    首帧闸门 bind 异常：{exc}")
    ff_roles = {b["role"] for b in project_store.get(PID)["scenes"][0]["binding"]["images"]}
    check("LLM 自标首帧一律降为 reference 落盘", bind_ok and ff_roles == {"reference"}, f"{ff_roles}")

    # ---------------- 5.5 素材匹配防回归（吕满春第6集事故） ----------------
    from app.services import material_match as mm

    check("台词摘要剥序号前缀（02台词…/tts时间戳）",
          mm.audio_semantic("2457ab0b_02是不是边界清楚了，就说明已经稳定啦？.mp3") == "是不是边界清楚了就说明已经稳定啦"
          and mm.audio_semantic("x_tts_085821_哎呀！原来如此.mp3") == "哎呀原来如此")
    check("语义文件键折叠同名重传（剥哈希前缀）",
          mm.semantic_file_key("61b19f99_lvmanchun.png") == mm.semantic_file_key("964f0b41_lvmanchun.png")
          and mm.semantic_file_key("33265da3_uuid-a.png") == mm.semantic_file_key("4ca0102e_uuid-a.png"))

    # 悬空 <Picture k> 防回归（吕满春白斑集事故）：润色正文把原稿全局 <Image 4>（小墨）
    # 的编号直接抄进标签，而本镜只绑 2-3 张图——组装闸门按悬空标签永久拦截。
    # 按全局上传顺序对号：已绑定改号到绑定位，未绑定降级纯文本
    from app.services.pipeline.stages import rewrite_aligned_prompt

    g7 = ["g1", "g2", "g3", "g4", "g5", "g6", "g7"]  # 上传顺序：第4张=小墨
    out = rewrite_aligned_prompt(
        "<Subject 1> is 小墨, anchored by <Picture 4>.\nsee also <Picture 5>.",
        image_ns=[4, 7], table={4: "小墨", 7: "扩大视野关键帧"},
        bound_mids=["g4", "g7"], global_mids=g7)
    check("悬空 <Picture 4>（原稿全局号）按上传顺序改号到绑定位",
          "anchored by <Picture 1>" in out, f"{out}")
    check("未绑定的全局号 <Picture 5> 降级纯文本（不再悬空）",
          "original storyboard Image 5" in out and "<Picture 5>" not in out, f"{out}")
    out2 = rewrite_aligned_prompt(
        "小墨 anchored by <Picture 4>，构图参考 <Picture 2>。",
        image_ns=[4, 5, 6], table={},
        wants_mids=["g4", "g6", "g7"], bound_mids=["g4", "g5", "g6"], global_mids=g7)
    check("三编号域统一：wants 域 <Picture 2>→绑定位3，全局域 <Picture 4>→绑定位1",
          "小墨 anchored by <Picture 1>，构图参考 <Picture 3>。" in out2, f"{out2}")
    # 幂等：重跑 bind 不再破坏已重写的 Prompt（事故：每次重新绑定，pass1 把已改号的
    # <Picture 2> 按 wants 域再解释一遍，dualren 标签被误降级成纯文本）
    out3 = rewrite_aligned_prompt(out, image_ns=[4, 7], table={4: "小墨", 7: "扩大视野关键帧"},
                                  wants_mids=["g1", "g5"], bound_mids=["g4", "g7"], global_mids=g7)
    check("重写幂等：对已重写 Prompt 再跑不再误降级/改号", out3 == out, f"{out3}")

    PD = project_store.create("重传去重", "16:9")["id"]
    stages.run_intake(PD, {"materials": [
        {"url": "/api/files/uploads/image/61b19f99_lvmanchun.png", "kind": "image", "filename": "61b19f99_lvmanchun.png"},
        {"url": "/api/files/uploads/audio/af4b389a_02台词甲.mp3", "kind": "audio", "filename": "af4b389a_02台词甲.mp3"},
        {"url": "/api/files/uploads/image/964f0b41_lvmanchun.png", "kind": "image", "filename": "964f0b41_lvmanchun.png"},
        {"url": "/api/files/uploads/audio/c8da5933_02台词甲.mp3", "kind": "audio", "filename": "c8da5933_02台词甲.mp3"},
    ]})
    check("intake 同身份重传不重复登记", len(project_store.get(PD)["materials"]) == 2,
          f"{[m['filename'] for m in project_store.get(PD)['materials']]}")

    # ---------------- 6. runner：start → review 暂停 → advance → done ----------------
    from app.services import export_service, reconcile

    def _fake_reconcile(project_id, trace=None, cancel_check=None):
        async def _r():
            return {"keyframe": 0, "voice": 0, "video": 0, "failed": [], "blocked": []}
        return _r()

    async def _fake_export(project_id):
        url = "/api/files/outputs/video/fake_final.mp4"
        project_store.mutate(project_id, lambda p: setattr(p, "final_video", url))
        return {"url": url, "scenes_exported": 2, "scenes_skipped": []}

    orig = (reconcile.run_generate_stage, export_service.export_project_v2, pipe_mod._envelope)
    reconcile.run_generate_stage = _fake_reconcile
    export_service.export_project_v2 = _fake_export
    _env_scripted = llm_client.LLMEnvelope(FakeLLM([norm_output, plan_batch, bind_output]), "m")
    pipe_mod._envelope = lambda *a, **kw: _env_scripted

    try:
        async def _runner_flow() -> None:
            global_start = start_pipeline(PID, payload={"script_text": SCRIPT, "materials": []})
            check("start 返回 run_id", bool(global_start.get("run_id")))
            proj = await _wait_pipeline(PID)
            check("流水线在 review 门禁暂停", proj["pipeline"]["state"] == "paused" and proj["pipeline"]["awaiting"] == "review",
                  f"{proj['pipeline']['state']}/{proj['pipeline'].get('awaiting')}/{proj['pipeline'].get('error')}")

            # single-flight：占用运行锁时 advance 应拒绝
            watchdog.try_acquire(PID, "run_occupied", "agent")
            try:
                advance_pipeline(PID, confirmed=True)
                check("运行锁占用时 advance 应拒绝", False)
            except RunBusyError:
                check("运行锁占用时 advance 被拒", True)
            watchdog.release(PID, "run_occupied")

            advance_pipeline(PID, confirmed=True)
            proj = await _wait_pipeline(PID, newer_than=_last_history_at(PID))
            check("确认后流水线跑到 done", proj["pipeline"]["state"] == "done", f"{proj['pipeline']}")
            check("assemble 写回 final_video", proj["final_video"] == "/api/files/outputs/video/fake_final.mp4")

            # 崩溃悬空 running → 可续跑
            def _dangling(p):
                p.pipeline.state = "running"
                p.pipeline.awaiting = None
            project_store.mutate(PID, _dangling)
            advance_pipeline(PID)
            proj = await _wait_pipeline(PID, newer_than=_last_history_at(PID))
            check("悬空 running 可从检查点续跑", proj["pipeline"]["state"] == "done", f"{proj['pipeline']}")

        asyncio.run(_runner_flow())
    finally:
        reconcile.run_generate_stage, export_service.export_project_v2, pipe_mod._envelope = orig

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
