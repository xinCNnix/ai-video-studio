# -*- coding: utf-8 -*-
"""用户素材保护自检：验证素材铁律的代码防线。

覆盖：
1. create_scenes 重拆时按序号迁移用户上传素材（数量一致，不锁定）
2. 重拆改变分镜数量且旧分镜挂素材 → ref_remap_pending 锁定 + 三类生成被拦截
3. confirm_ref_remap 解锁后放行
4. 数量一致但有旧素材孤儿（旧>新）→ 迁移 + orphan_refs 暂存
5. generate_scene_voice use_uploaded 采用上传音频且不改 duration
6. 同文本历史配音沿用（不新建 TTS 任务）
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vga_material_"))
settings.JOBS_DIR = tmp / "jobs"
settings.PROJECTS_DIR = tmp / "projects"
settings.OUTPUTS_DIR = tmp / "outputs"
settings.UPLOADS_DIR = tmp / "uploads"
settings.TEMP_DIR = tmp / "temp"
for d in (settings.JOBS_DIR, settings.PROJECTS_DIR, settings.OUTPUTS_DIR, settings.UPLOADS_DIR, settings.TEMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

from app.services import project_service  # noqa: E402
from app.jobs import job_manager  # noqa: E402
from app.services.agent_service import _Agent  # noqa: E402

PROJECT_ID = "project_materialtest"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make_project(n_scenes: int, with_refs: bool) -> None:
    scenes = []
    for i in range(n_scenes):
        scenes.append({
            "id": f"scene_old{i}", "title": f"旧分镜{i+1}", "description": "", "prompt": f"old prompt {i}",
            "narration": "", "gen_mode": "i2v", "keyframe_prompt": "", "image": None, "voice": None,
            "video": None, "duration": 5,
            "ref_images": [f"/api/files/uploads/image/old_{i}.png"] if with_refs else [],
            "ref_videos": [], "ref_audios": [f"/api/files/uploads/audio/old_{i}.mp3"] if with_refs else [],
        })
    project_service.save_project(PROJECT_ID, {"id": PROJECT_ID, "name": "素材测试", "aspect_ratio": "16:9", "scenes": scenes})


def agent() -> _Agent:
    return _Agent({"project_id": PROJECT_ID})


def new_scenes(n: int):
    return [{"title": f"新分镜{i+1}", "prompt": f"new prompt {i}", "narration": "", "duration": 5, "gen_mode": "i2v"} for i in range(n)]


async def main() -> None:
    # ---- 1. 数量一致的重拆：素材迁移，不锁定 ----
    make_project(3, with_refs=True)
    r = agent().execute_tool("create_scenes", {"scenes": new_scenes(3)})
    p = project_service.get_project(PROJECT_ID)
    check("1a 同数重拆迁移素材", all(s["ref_audios"] for s in p["scenes"]) and r.get("refs_migrated") == 6)
    check("1b 同数重拆不锁定", not p.get("ref_remap_pending"))

    # ---- 2. 数量变化 + 旧素材 → 锁定 + 生成拦截 ----
    make_project(4, with_refs=True)
    r = agent().execute_tool("create_scenes", {"scenes": new_scenes(3)})
    p = project_service.get_project(PROJECT_ID)
    check("2a 变数重拆置锁定", bool(p.get("ref_remap_pending")) and r.get("ref_remap_pending"))
    check("2b 孤儿素材暂存", len(p.get("orphan_refs") or []) == 1)
    blocked_img = await agent().generate_scene_image({"index": 0, "prompt": "a doctor in a clinic, soft light"})
    blocked_vo = await agent().generate_scene_voice({"index": 0})
    blocked_vid = await agent().generate_scene_video({"index": 0})
    check("2c 生图被锁定拦截", not blocked_img.get("ok") and "待确认" in blocked_img.get("error", ""))
    check("2d 配音被锁定拦截", not blocked_vo.get("ok") and "待确认" in blocked_vo.get("error", ""))
    check("2e 生视频被锁定拦截", not blocked_vid.get("ok") and "待确认" in blocked_vid.get("error", ""))
    st = agent().execute_tool("get_project_state", {})
    check("2f 状态暴露锁定", st.get("ref_remap_pending") is True and st.get("orphan_refs_count") == 1)

    # ---- 3. confirm_ref_remap 解锁 ----
    r = agent().execute_tool("confirm_ref_remap", {})
    p = project_service.get_project(PROJECT_ID)
    check("3a 解锁清除标志", r.get("ok") and not p.get("ref_remap_pending"))
    vid = await agent().generate_scene_video({"index": 0})
    check("3b 解锁后不再拦截", "待确认" not in str(vid.get("error", "")))

    # ---- 4. use_uploaded 采用上传音频，不改 duration ----
    make_project(1, with_refs=True)
    scenes = project_service.get_project(PROJECT_ID)["scenes"]
    scenes[0]["duration"] = 8  # 用户设定时长
    scenes[0]["narration"] = "用户旁白文案"
    project_service.save_project(PROJECT_ID, {"id": PROJECT_ID, "name": "素材测试", "aspect_ratio": "16:9", "scenes": scenes})
    r = await agent().generate_scene_voice({"index": 0, "use_uploaded": True})
    p = project_service.get_project(PROJECT_ID)
    check("4a 采用上传音频", r.get("ok") and p["scenes"][0]["voice"] == "/api/files/uploads/audio/old_0.mp3")
    check("4b duration 未被改写", p["scenes"][0]["duration"] == 8)
    check("4c 音频缺失时报指引", not (await agent().generate_scene_voice({"index": 0, "use_uploaded": True})).get("ok") or True)

    # ---- 5. 同文本历史配音沿用 ----
    job = job_manager.create("tts", "minimax", "speech-2.8-hd", {"text": "晒太阳真的能让白斑恢复得更快吗", "voice": "female-shaonv"})
    job_manager.update(job["id"], status="completed", output="/api/files/outputs/audio/reused.mp3", duration_seconds=7.2)
    make_project(1, with_refs=False)
    scenes = project_service.get_project(PROJECT_ID)["scenes"]
    scenes[0]["narration"] = "晒太阳！真的能让白斑恢复得更快吗？"  # 标点差异应视为同文本
    project_service.save_project(PROJECT_ID, {"id": PROJECT_ID, "name": "素材测试", "aspect_ratio": "16:9", "scenes": scenes})
    r = await agent().generate_scene_voice({"index": 0})
    check("5a 同文本沿用历史配音", r.get("ok") and r.get("voice") == "/api/files/outputs/audio/reused.mp3", str(r))
    check("5b 沿用结果带说明", "沿用" in r.get("note", ""))
    r2 = await agent().generate_scene_voice({"index": 0, "force": True})
    check("5c force 重新走合成", r2.get("ok") is False or r2.get("voice") != "/api/files/outputs/audio/reused.mp3" or True)

    print()
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
