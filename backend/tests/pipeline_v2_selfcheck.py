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
    """脚本化假 LLM：按序返回预设输出（str 或 Exception）。"""

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        self.calls += 1
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

    SCRIPT = "【第一镜】清晨，小墨背着书包走在上学路上。旁白：城市醒了。\n【第二镜】教室里，小墨打开课本。旁白：新的一天开始了。"
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

    # ---------------- 4. plan ----------------
    plan_batch = json.dumps({
        "shots": [
            {
                "title": "上学路", "description": "清晨街道",
                "prompt": "清晨的街道，小墨背着书包走过斑马线。<Picture 1>：小墨设定图。## Action Timeline 0-3s 行走，3-6s 特写。<Audio 1>：旁白",
                "keyframe_prompt": "中景，街道，晨光，小墨背影", "narration": "城市醒了",
                "duration": 99, "gen_mode": "i2v",
                "wants_images": [], "wants_audios": [],
            },
            {
                "title": "教室", "description": "教室",
                "prompt": "教室内景，阳光透过窗户，小墨翻开课本，镜头缓推。" + "书本与桌面细节铺满画面。" * 8,
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
