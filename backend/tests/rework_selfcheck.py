# -*- coding: utf-8 -*-
"""返工回归自检：验证「用户不管怎么给素材都要能出片」的新架构。

覆盖（对应 2026-09-13 返工的全部诉求）：
1. 一键出片：有旁白自动合成配音（含时长校准）后提交；无旁白按静音镜提交；契约预检先于配音
2. 防串剧本：纯指令对旧 Prompt 放行；消息携带新内容且分镜未更新才拦截；确认后放行
3. use_uploaded 无上传音频时回落合成（不再死胡同报错）
4. create_scenes：1-2 镜的完整分镜稿放行；模型偷懒的短 Prompt 仍拦
5. attach_scene_refs：素材池挂载到分镜；非法路径拒绝
6. 快路径：素材齐备 + 纯指令零 LLM 出片；有缺口不进快路径（走助手循环消化消息里的新内容）
7. 兜底网：用户要视频、模型只回文字没调工具 → 流水线照样出片
8. 贴分镜稿出片：聊天里给完整分镜稿（单镜）→ 助手录入 → 交接流水线出片
全部离线：假 LLM + 假生成 runner + 临时目录。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vga_rework_"))
settings.JOBS_DIR = tmp / "jobs"
settings.PROJECTS_DIR = tmp / "projects"
settings.OUTPUTS_DIR = tmp / "outputs"
settings.UPLOADS_DIR = tmp / "uploads"
settings.TEMP_DIR = tmp / "temp"
for d in (settings.JOBS_DIR, settings.PROJECTS_DIR, settings.OUTPUTS_DIR, settings.UPLOADS_DIR, settings.TEMP_DIR):
    for sub in ("", "image", "audio", "video"):
        (d / sub).mkdir(parents=True, exist_ok=True)

from app.services import agent_service, file_service, image_service, orchestrator, project_service, video_service, voice_service  # noqa: E402
from app.jobs import job_manager  # noqa: E402
from app.services.agent_service import _Agent  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


async def _fake_video(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/video.mp4", duration_seconds=5.0)
    return {"url": "/fake/video.mp4"}


async def _fake_voice(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/voice.mp3", duration_seconds=3.5)
    return {"url": "/fake/voice.mp3"}


async def _fake_image(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/keyframe.png")
    return {"url": "/fake/keyframe.png"}


video_service.run_video_job = _fake_video
voice_service.run_voice_job = _fake_voice
image_service.run_image_job = _fake_image


class FailLlm:
    async def generate(self, messages, model, **kw):
        raise AssertionError("LLM 不应被调用（快路径应零 LLM）")


class ScriptLlm:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, model, **kw):
        self.calls += 1
        return self.outputs.pop(0)


async def fake_caps():
    return {"tts_models": "tts-test", "voices": "female-shaonv", "fixed_voices": "", "image_models": "img-test", "video_models": "h3-test"}


agent_service._capability_snapshot = fake_caps
agent_service.skill_service.list_skills = lambda: []

LONG_PROMPT = ("## Scene\n诊室内，医生与小墨对话。" + "镜头缓推，暖光铺满桌面，角色表情从疑惑转为恍然。0-3s 医生开口，3-6s 小墨追问。" * 6)


def make_project(pid: str, scenes: list[dict]) -> dict:
    project = {"id": pid, "name": pid, "aspect_ratio": "16:9",
               "created_at": "2026-09-13T00:00:00", "scenes": scenes}
    project_service.save_project(pid, project)
    return project


def shot(narration: str = "这一镜的旁白文案。", image: str = "/outputs/kf.png") -> dict:
    return {
        "id": "scene_s0", "title": "分镜1", "description": "", "prompt": LONG_PROMPT,
        "narration": narration, "gen_mode": "i2v", "keyframe_prompt": "",
        "image": image, "voice": None, "video": None, "duration": 5,
        "ref_images": [], "ref_videos": [], "ref_audios": [],
    }


def tool_call(tool: str, args: dict) -> str:
    return json.dumps({"action": "tool", "tool": tool, "args": args}, ensure_ascii=False)


async def main() -> None:
    # ---- 1. 一键出片 ----
    make_project("r1", [shot()])
    a = _Agent({"project_id": "r1"})
    r = await a.generate_scene_video({"index": 0})
    check("1a 有旁白镜自动合成配音后提交视频", r.get("ok") and "自动合成旁白" in str(r.get("note", "")), str(r))
    p = project_service.get_project("r1")
    check("1b 旁白写入分镜且时长按语音校准", p["scenes"][0].get("voice") and p["scenes"][0]["duration"] == 4,
          f"voice={p['scenes'][0].get('voice')} dur={p['scenes'][0]['duration']}")
    check("1c 视频任务已提交", any(j.get("type") == "video" for j in a.jobs))

    make_project("r1s", [shot(narration="")])
    a = _Agent({"project_id": "r1s"})
    r = await a.generate_scene_video({"index": 0})
    check("1d 无旁白镜按静音镜直接提交", r.get("ok") and "静音镜" in str(r.get("note", "")), str(r))

    make_project("r1k", [{**shot(), "image": None, "ref_images": []}])
    a = _Agent({"project_id": "r1k"})
    r = await a.generate_scene_video({"index": 0})
    check("1e 契约预检先于配音（无图报关键帧错误而非合成旁白）",
          not r.get("ok") and "关键帧" in str(r.get("error", "")), str(r))

    # ---- 2. 防串剧本 ----
    make_project("r2", [shot()])
    a = _Agent({"project_id": "r2"})  # 纯指令（无 goal 内容）
    r = await a.generate_scene_video({"index": 0})
    check("2a 纯指令对旧 Prompt 放行并注明", r.get("ok") and "本次任务之前写入" in str(r.get("note", "")), str(r))

    long_goal = "新分镜稿如下：" + LONG_PROMPT
    a = _Agent({"project_id": "r2"}, goal=long_goal)
    r = await a.generate_scene_video({"index": 0})
    check("2b 消息携带新内容且分镜未更新 → 拦截", not r.get("ok") and "confirm_stale_prompt" in str(r.get("error", "")), str(r))
    r = await a.generate_scene_video({"index": 0, "confirm_stale_prompt": True})
    check("2c 确认后放行", r.get("ok"), str(r))

    # ---- 3. use_uploaded 回落 ----
    make_project("r3", [shot()])
    a = _Agent({"project_id": "r3"})
    r = await a.generate_scene_voice({"index": 0, "use_uploaded": True})
    check("3a 无上传音频回落为合成", r.get("ok") and r.get("voice"), str(r))

    # ---- 4. create_scenes 镜数放宽 ----
    make_project("r4", [])
    a = _Agent({"project_id": "r4"})
    r = a.execute_tool("create_scenes", {"scenes": [{"title": "唯一镜", "prompt": LONG_PROMPT,
                                                     "narration": "旁白", "duration": 5, "gen_mode": "i2v"}]})
    check("4a 单镜完整分镜稿放行", r.get("ok") and r.get("scene_count") == 1, str(r))
    make_project("r4b", [])
    a = _Agent({"project_id": "r4b"})
    r = a.execute_tool("create_scenes", {"scenes": [{"title": "偷懒", "prompt": "短提示词", "duration": 5}]})
    check("4b 模型偷懒的 1 镜短 Prompt 仍拦", not r.get("ok") and "分镜数少于 3" in str(r.get("error", "")), str(r))

    # ---- 5. attach_scene_refs ----
    (settings.UPLOADS_DIR / "image" / "pool_img.png").write_bytes(b"x")
    make_project("r5", [shot(narration="")])
    a = _Agent({"project_id": "r5"})
    r = a.execute_tool("attach_scene_refs", {"index": 0, "ref_images": ["/api/files/uploads/image/u1.png"],
                                             "ref_audios": ["/api/files/uploads/audio/u1_tts_台词.mp3"]})
    check("5a 素材池挂载到分镜", r.get("ok") and len(r.get("mounted", [])) == 2, str(r))
    p = project_service.get_project("r5")
    check("5b 挂载后分镜带参考图与音频",
          p["scenes"][0]["ref_images"] == ["/api/files/uploads/image/u1.png"]
          and p["scenes"][0]["ref_audios"] == ["/api/files/uploads/audio/u1_tts_台词.mp3"])
    r = a.execute_tool("attach_scene_refs", {"index": 0, "ref_images": ["https://evil.example/x.png"]})
    check("5c 非本站素材路径拒绝", not r.get("ok") and "uploads/outputs" in str(r.get("error", "")), str(r))
    st = a.execute_tool("get_project_state", {})
    check("5d 状态含素材池入口", isinstance(st.get("recent_uploads"), dict), str(list(st.keys())))

    # ---- 6. 快路径与逃生 ----
    ready = shot()
    ready["ref_images"] = ["/api/files/uploads/image/ref.png"]
    ready["ref_audios"] = ["/api/files/uploads/audio/voice.mp3"]  # 提示词+参考图+配音齐备
    make_project("r6", [ready])
    agent_service.resolve_llm_call = lambda *a, **kw: (FailLlm(), "no-llm")
    r = await agent_service.run_agent("我给了分镜提示词、参考图、配音，生成分镜视频", {"project_id": "r6"}, None, None)
    check("6a 素材齐备+纯指令 → 零 LLM 快路径出片",
          any(j.get("type") == "video" for j in r.get("jobs", [])) and "流水线" in r.get("reply", ""),
          r.get("reply", "")[:120])

    make_project("r6b", [{**shot(), "prompt": "", "image": None}])  # 缺 Prompt + 无素材
    storyboard_goal = "分镜稿：\n## 分镜1\n" + LONG_PROMPT + "\n配音：用我上传的音频。生成视频。"
    llm = ScriptLlm([tool_call("create_scenes", {"scenes": [
        {"title": "分镜1", "prompt": LONG_PROMPT, "narration": "这一镜的旁白文案。", "duration": 5, "gen_mode": "i2v",
         "keyframe_prompt": "中景，角色在诊室，暖光"}]}),
        '{"action":"finish","summary":"分镜已录入"}'])
    agent_service.resolve_llm_call = lambda *a, **kw: (llm, "fake")
    r = await agent_service.run_agent(storyboard_goal, {"project_id": "r6b"}, None, None)
    check("6b 贴分镜稿+项目缺素材 → 助手循环录入（不再只报缺口）",
          any(j.get("type") == "video" for j in r.get("jobs", [])), str(r.get("jobs")))
    check("6c 录入后交接流水线出片（单镜项目）", "系统流水线" in r.get("reply", ""), r.get("reply", "")[:120])
    p = project_service.get_project("r6b")
    check("6d 交接补齐关键帧与旁白", p["scenes"][0].get("image") and p["scenes"][0].get("voice"),
          str({k: p["scenes"][0].get(k) for k in ("image", "voice")}))

    # ---- 7. 兜底网：模型只回文字不出片 → 流水线接管 ----
    make_project("r7", [shot()])
    llm = ScriptLlm(['{"action":"finish","summary":"好的，马上安排。"}'])
    agent_service.resolve_llm_call = lambda *a, **kw: (llm, "fake")
    r = await agent_service.run_agent("make me a video", {"project_id": "r7"}, None, None)
    check("7a 模型空手收尾时兜底网出片",
          any(j.get("type") == "video" for j in r.get("jobs", [])) and "系统流水线" in r.get("reply", ""),
          r.get("reply", "")[:120])

    # 纯配音任务不触发视频兜底
    make_project("r7b", [shot()])
    llm = ScriptLlm(['{"action":"finish","summary":"配音已重新生成。"}'])
    agent_service.resolve_llm_call = lambda *a, **kw: (llm, "fake")
    r = await agent_service.run_agent("重新生成配音", {"project_id": "r7b"}, None, None)
    check("7b 纯配音诉求不触发视频兜底", not any(j.get("type") == "video" for j in r.get("jobs", [])),
          str(r.get("jobs")))

    # ---- 8. 项目级素材分发：5 镜 + 9 段配音 + 2 张参考图（用户复现场景）----
    lines = ["晒太阳真的能让白斑恢复得更快吗", "太阳一出来我是不是就能加班造颜色啦",
             "哎呀原来太阳也得讲科学", "懂啦不是拼命晒是会晒伤", "适当的光照确实可能有帮助"]
    scenes5 = []
    for i, ln in enumerate(lines):
        scenes5.append({
            "id": f"scene_d{i}", "title": f"分镜{i+1}", "description": "",
            "prompt": f"## Scene\n分镜{i+1}。## Audio Timeline\n角色说：{ln}\n## subject_definitions\n<Picture 1> 主角。0-5s 镜头缓推。" * 2,
            "narration": ln, "gen_mode": "i2v", "keyframe_prompt": "", "image": None,
            "voice": None, "video": None, "duration": 5,
            "ref_images": [], "ref_videos": [], "ref_audios": [],
        })
    aud9 = [f"/api/files/uploads/audio/d{i}_tts_085821_{ln}.mp3" for i, ln in enumerate(lines)]
    aud9 += [f"/api/files/uploads/audio/x{i}_tts_085821_无关台词第{i}段.mp3" for i in range(4)]
    img2 = ["/api/files/uploads/image/doctor.png", "/api/files/uploads/image/小墨.png"]
    make_project("r8", scenes5)
    a = _Agent({"project_id": "r8"})
    r = a.execute_tool("attach_scene_refs", {"ref_audios": aud9, "ref_images": img2})
    check("8a 整片素材无 index 分发成功", r.get("ok") and r.get("distributed"), str(r))
    check("8b 5 段台词配音各归各镜", r.get("per_scene") == {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1}, str(r.get("per_scene")))
    check("8c 4 段无关配音报告未命中", len(r.get("unmatched_audios") or []) == 4, str(r.get("unmatched_audios")))
    p = project_service.get_project("r8")
    check("8d 各镜挂上自己的配音段",
          all(p["scenes"][i]["ref_audios"] == [aud9[i]] for i in range(5)),
          str([s["ref_audios"] for s in p["scenes"]]))
    check("8e 参考图挂到全部分镜", all(s["ref_images"] == img2 for s in p["scenes"]),
          str([s["ref_images"] for s in p["scenes"]]))
    # 分发完直接出片：5 镜全部提交（不再只有 1 镜能生成）
    a2 = _Agent({"project_id": "r8"})
    res = await orchestrator.run_pipeline(a2, "生成分镜视频")
    vid_jobs = [j for j in a2.jobs if j.get("type") == "video"]
    check("8f 分发后 5 镜全部提交视频", len(vid_jobs) == 5, str(len(vid_jobs)))

    # ---- 9. md 剧本文档：read_doc ----
    doc_dir = settings.UPLOADS_DIR / "doc"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "be01_我的剧本.md").write_text("# 剧本\n## 分镜1\n医生与小墨讨论晒太阳。", encoding="utf-8")
    make_project("r9", [])
    a = _Agent({"project_id": "r9"})
    r = a.execute_tool("read_doc", {"url": "/api/files/uploads/doc/be01_我的剧本.md"})
    check("9a read_doc 读取正文", r.get("ok") and "医生与小墨" in str(r.get("content", "")), str(r)[:100])
    r = a.execute_tool("read_doc", {"url": "/api/files/uploads/image/evil.png"})
    check("9b 非 doc 路径拒绝", not r.get("ok"), str(r))
    r = a.execute_tool("read_doc", {"url": "/api/files/uploads/doc/不存在.md"})
    check("9c 不存在文档报错", not r.get("ok") and "不存在" in str(r.get("error", "")), str(r))

    # ---- 10. 重启恢复：接着轮询云端并写回分镜 ----
    from app.services import job_recovery
    from app.providers import registry as reg

    class FakeCloud:
        def __init__(self):
            self.polled = []
        async def poll(self, task_id):
            self.polled.append(task_id)
            return ("success", "http://cloud/v.mp4", None)

    async def fake_dl(kind, url, ext=None):
        return "/api/files/outputs/video/recovered.mp4"

    fake_cloud = FakeCloud()
    orig_gv, orig_dl = reg.get_video, file_service.download_to_outputs
    reg.get_video = lambda provider, cred: fake_cloud
    file_service.download_to_outputs = fake_dl
    try:
        make_project("r10", [shot()])
        fp0 = agent_service._prompt_fingerprint(LONG_PROMPT)
        job = job_manager.create("video", "autodl", "minimax_h3_zm_u24",
                                 {"scene_index": 0, "project_id": "r10", "duration": 5,
                                  "prompt_fingerprint": fp0})
        job_manager.update(job["id"], provider_task_id="cloud_t123", status="generating")
        await job_recovery._recover_video_job(job_manager.get(job["id"]))
        j = job_manager.get(job["id"])
        check("10a 恢复轮询云端并下载完成", j.get("status") == "completed"
              and j.get("output") == "/api/files/outputs/video/recovered.mp4"
              and fake_cloud.polled == ["cloud_t123"], str(j)[:120])
        p = project_service.get_project("r10")
        check("10b 恢复结果写回分镜", p["scenes"][0].get("video") == "/api/files/outputs/video/recovered.mp4",
              str(p["scenes"][0].get("video")))
        # 指纹不一致（分镜已被改写）：不写回，留说明
        make_project("r10b", [{**shot(), "prompt": "换了新剧本的 Prompt" * 20}])
        job2 = job_manager.create("video", "autodl", "minimax_h3_zm_u24",
                                  {"scene_index": 0, "project_id": "r10b", "duration": 5,
                                   "prompt_fingerprint": fp0})
        job_manager.update(job2["id"], provider_task_id="cloud_t456", status="generating")
        await job_recovery._recover_video_job(job_manager.get(job2["id"]))
        j2 = job_manager.get(job2["id"])
        p2 = project_service.get_project("r10b")
        check("10c 分镜已改写时拒绝写回并留说明",
              j2.get("status") == "completed" and not p2["scenes"][0].get("video")
              and "已被改写" in str(j2.get("writeback_note", "")), str(j2.get("writeback_note"))[:80])
    finally:
        reg.get_video = orig_gv
        file_service.download_to_outputs = orig_dl

    print()
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
