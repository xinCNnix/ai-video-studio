"""整改自检：验证防串剧本三道防线 + 事件循环不再被同步 IO 阻塞。

在临时目录里跑，不触碰 data/ 真实数据。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vga_guard_"))
settings.JOBS_DIR = tmp / "jobs"
settings.PROJECTS_DIR = tmp / "projects"
settings.OUTPUTS_DIR = tmp / "outputs"
for d in (settings.JOBS_DIR, settings.PROJECTS_DIR, settings.OUTPUTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

from app.services import project_service  # noqa: E402
from app.jobs import job_manager  # noqa: E402
from app.services.agent_service import _Agent, _prompt_fingerprint  # noqa: E402

PROJECT_ID = "project_guardtest"
OLD_PROMPT = "old script prompt: 1960s computer laboratory"
NEW_PROMPT = "new script prompt: doctor and cartoon sun"


def make_project(prompt: str) -> None:
    project_service.save_project(PROJECT_ID, {
        "id": PROJECT_ID,
        "name": "guard-test",
        "aspect_ratio": "16:9",
        "scenes": [{
            "id": "scene_a1", "title": "s0", "description": "",
            "prompt": prompt, "narration": "", "gen_mode": "i2v",
            "keyframe_prompt": "", "image": "/api/files/outputs/image/fake.png",
            "voice": "/api/files/outputs/audio/fake.mp3", "video": None,
            "duration": 5, "ref_images": [], "ref_videos": [], "ref_audios": [],
        }],
    })


async def main() -> None:
    ok_all = True

    # ---- 防线 1：纯执行指令（消息无新内容）对旧 Prompt 直接放行，结果带说明 ----
    # 旧实现一律弹回要 confirm_stale_prompt，模型被弹得来回空转；现在只有消息携带
    # 新内容（疑似新剧本）而分镜没更新时才拦截。写回层指纹比对仍是主防线（防线 2）。
    make_project(OLD_PROMPT)
    agent = _Agent({"project_id": PROJECT_ID})  # goal 为空 = 纯执行指令
    r = await agent.generate_scene_video({"index": 0})
    assert r["ok"] and "本次任务之前写入" in (r.get("note") or ""), f"防线1失效: {r}"
    print("PASS 防线1: 纯指令对旧 Prompt 一键放行，结果注明来源")

    jid1 = r["job_id"]
    job = job_manager.get(jid1)
    assert job["params"]["prompt_fingerprint"] == _prompt_fingerprint(OLD_PROMPT)
    assert job["params"]["project_id"] == PROJECT_ID
    print("PASS 防线1b: 放行任务记录指纹/project_id")

    # ---- 防线 1c：用户消息携带新内容（疑似新剧本）而分镜 Prompt 未更新 → 仍拦截 ----
    long_goal = "新剧本如下：" + "主角走进诊所，与医生对话，讨论晒太阳的利弊。" * 12
    agent_1c = _Agent({"project_id": PROJECT_ID}, goal=long_goal)
    r_1c = await agent_1c.generate_scene_video({"index": 0})
    assert not r_1c["ok"] and "confirm_stale_prompt" in r_1c["error"], f"防线1c失效: {r_1c}"
    r_1d = await agent_1c.generate_scene_video({"index": 0, "confirm_stale_prompt": True})
    assert r_1d["ok"], f"确认后应放行: {r_1d}"
    print("PASS 防线1c: 消息携带新内容且分镜未更新时拦截，确认后放行")

    # ---- 防线 2：写回前比对指纹——分镜被改写后，旧视频绝不写进新分镜 ----
    job_manager.update(jid1, status="completed", output="/api/files/outputs/video/old.mp4")
    # 模拟视频生成期间用户换了新剧本
    project = project_service.get_project(PROJECT_ID)
    project["scenes"][0]["prompt"] = NEW_PROMPT
    project_service.save_project(PROJECT_ID, project)
    await agent._writeback_scene_video(jid1, 0, 5, _prompt_fingerprint(OLD_PROMPT))
    project = project_service.get_project(PROJECT_ID)
    assert not project["scenes"][0]["video"], "防线2失效: 旧视频被写进新剧本分镜"
    assert job_manager.get(jid1).get("writeback_note"), "应留下未写回说明"
    print("PASS 防线2: 指纹不一致拒绝写回，且记录 writeback_note")

    # ---- 防线 2b：指纹一致才正常写回 ----
    await agent._writeback_scene_video(jid1, 0, 5, _prompt_fingerprint(NEW_PROMPT))
    project = project_service.get_project(PROJECT_ID)
    assert project["scenes"][0]["video"] == "/api/files/outputs/video/old.mp4"
    print("PASS 防线2b: 指纹一致正常写回")

    # ---- 防线 3：set_scene_prompt / create_scenes 标记本次会话写入 → 直接放行 ----
    agent2 = _Agent({"project_id": PROJECT_ID})
    agent2.execute_tool("set_scene_prompt", {"index": 0, "prompt": NEW_PROMPT})
    assert 0 in agent2.prompt_touched
    r = await agent2.generate_scene_video({"index": 0})
    assert r["ok"], f"本会话写入过的 Prompt 应直接放行: {r}"
    jid2 = r["job_id"]
    print("PASS 防线3: set_scene_prompt 后无需确认直接生成")

    # ---- create_scenes 整体重写后全部标记 ----
    agent3 = _Agent({"project_id": PROJECT_ID})
    r = agent3.execute_tool("create_scenes", {"scenes": [
        {"title": "a", "prompt": "p1", "narration": "n1", "duration": 5, "gen_mode": "t2v"},
        {"title": "b", "prompt": "p2", "narration": "n2", "duration": 5, "gen_mode": "t2v"},
        {"title": "c", "prompt": "p3", "narration": "n3", "duration": 5, "gen_mode": "t2v"},
    ]})
    assert r["ok"] and agent3.prompt_touched == {0, 1, 2}, f"create_scenes 标记异常: {agent3.prompt_touched}"
    print("PASS 防线3b: create_scenes 重写后全部标记为本会话写入")

    # ---- jobs 列表带 project_id（前端绑定项目用）----
    assert all(j.get("project_id") == PROJECT_ID for j in agent2.jobs + agent3.jobs)
    print("PASS jobs 携带 project_id")

    # ---- run_agent 的 on_step 回调（SSE 依赖）----
    from app.services import agent_service as svc

    async def fake_llm(messages, model, temperature=0.7, max_tokens=None):
        return '{"action":"finish","summary":"done"}'

    async def fake_snapshot() -> dict:
        return {"tts_models": "", "voices": "", "fixed_voices": "", "image_models": "", "video_models": ""}

    steps_seen = []
    orig = svc.resolve_llm_call
    svc.resolve_llm_call = lambda *a, **k: (type("L", (), {"generate": staticmethod(fake_llm)})(), "fake")
    orig_snap = svc._capability_snapshot
    svc._capability_snapshot = fake_snapshot
    try:
        res = await svc.run_agent("hi", {}, None, None, on_step=steps_seen.append)
    finally:
        svc.resolve_llm_call = orig
        svc._capability_snapshot = orig_snap
    assert steps_seen and steps_seen[-1]["type"] == "finish", f"on_step 未回调: {steps_seen}"
    print("PASS run_agent on_step 回调")

    print("\nALL GUARD CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
