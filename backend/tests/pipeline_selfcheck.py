# -*- coding: utf-8 -*-
"""确定性流水线自检：验证「素材直出」架构的关键行为。

覆盖：
1. _assemble_video_request：用户参考图直传参考模式（不强制关键帧）、>3 段音频按文案匹配
2. preflight：素材齐备→ready；缺 Prompt / 无任何配音→gap（直出模式不代配）
3. run_pipeline（直出）：提交视频任务；Prompt 原样（指纹不变）；scene.voice 不被设置（防导出双混音）
4. 路由：素材+生成措辞→pipeline（零 LLM 调用）；创作措辞→agent；提问→交给分类器
5. 创作路径交接：create_scenes 写完分镜后自动触发流水线并合成旁白（TTS 假 runner）
全部离线：假 LLM + 假生成 runner + 临时目录，不触碰 data/ 真实数据、不烧任何额度。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vga_pipeline_"))
settings.JOBS_DIR = tmp / "jobs"
settings.PROJECTS_DIR = tmp / "projects"
settings.OUTPUTS_DIR = tmp / "outputs"
settings.UPLOADS_DIR = tmp / "uploads"
settings.TEMP_DIR = tmp / "temp"
for d in (settings.JOBS_DIR, settings.PROJECTS_DIR, settings.OUTPUTS_DIR, settings.UPLOADS_DIR, settings.TEMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

from app.services import agent_service, image_service, orchestrator, project_service, video_service, voice_service  # noqa: E402
from app.jobs import job_manager  # noqa: E402
from app.services.agent_service import _Agent, _assemble_video_request, _prompt_fingerprint  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


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

PROMPT = (
    "# 分镜1｜晒太阳？小墨一下兴奋了\n## Duration\n7 seconds\n## Audio Timeline\n"
    '- 00:00 - 00:02.86｜医生\n  "晒太阳，真的能让白斑恢复得更快吗？"\n'
    '- 00:02.86 - 00:07｜小墨\n  "太阳一出来，我是不是就能加班造颜色啦？"\n'
    "## subject_definitions\n<Picture 1> is the doctor ..."
)
AUDIOS = [
    "/api/files/uploads/audio/aada1887_tts_085821_哎呀！原来太阳也得讲.mp3",
    "/api/files/uploads/audio/17eeff26_tts_085821_懂啦！不是拼命晒，是.mp3",
    "/api/files/uploads/audio/a25ca6a1_tts_085821_嘿！那我多晒一会儿，.mp3",
    "/api/files/uploads/audio/0676e706_tts_085821_科学用光，才能更好地.mp3",
    "/api/files/uploads/audio/3494715e_tts_085821_可不是越多越好。暴晒.mp3",
    "/api/files/uploads/audio/30da8308_tts_085821_晒太阳，真的能让白斑.mp3",
    "/api/files/uploads/audio/08f4a2ed_tts_085821_适当的光照，确实可能.mp3",
    "/api/files/uploads/audio/31f9d2d9_tts_085821_太阳一出来，我是不是.mp3",
    "/api/files/uploads/audio/6bed7a39_tts_085821_真正用于治疗的光疗，.mp3",
]
REF_IMGS = [
    "/api/files/uploads/image/3950be11_doctor_threeview.png",
    "/api/files/uploads/image/58b5421a_小墨.png",
]
DOCTOR_IMG, XIAOMO_IMG = REF_IMGS
UUID_IMG = "/api/files/uploads/image/b4d61513_9c92ff34-9ec4-4d38-addb-0aa7387f4fae.png"
IMG_PROMPT = (
    "# 分镜1｜顺序对齐测试\n## subject_definitions\n"
    "<Picture 1> is the approved three-view reference image of the doctor.\n"
    "<Picture 2> is the approved reference image of Xiao Mo. Preserve Xiao Mo's exact 3D style.\n"
    "## Character Reference（跨镜一致）\n"
    "- 医生：<Picture 1> 是医生已获批准的三视图参考图像。\n"
    "- 小墨：<Picture 2> 是小墨已获批准的参考图像。\n"
)


def make_project(pid: str, scenes: list[dict], name: str = "流水线自检") -> dict:
    project = {"id": pid, "name": name, "aspect_ratio": "16:9",
               "created_at": "2026-09-13T00:00:00", "scenes": scenes}
    project_service.save_project(pid, project)
    return project


def user_scene() -> dict:
    """复刻用户真实场景：完整分镜稿 + 2 张参考图 + 9 段配音全挂一镜，无关键帧。"""
    return {
        "id": "scene_s0", "title": "分镜1", "description": "", "prompt": PROMPT,
        "narration": "晒太阳，真的能让白斑恢复得更快吗？太阳一出来，我是不是就能加班造颜色啦？",
        "gen_mode": "i2v", "keyframe_prompt": "", "image": None, "voice": None, "video": None,
        "duration": 7, "ref_images": list(REF_IMGS), "ref_videos": [], "ref_audios": list(AUDIOS),
    }


class FailLlm:
    """任何 LLM 调用都视为失败——用于证明路由/流水线零 LLM 参与。"""
    async def generate(self, messages, model, **kw):
        raise AssertionError("LLM 不应被调用（确定性路径）")


class ScriptLlm:
    """按脚本逐条返回模型输出（创作路径交接测试）。"""
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


async def main():
    vmodel = settings.VIDEO_MODELS[0]

    # ---- 1. 请求组装：参考图直传参考模式 + 音频文案匹配 ----
    scene = user_scene()
    req = _assemble_video_request(scene, vmodel)
    check("1a 无关键帧但有参考图 → 参考模式直传用户图", req["mode"] == "reference" and req["images"] == REF_IMGS,
          f"mode={req['mode']} images={req['images']}")
    check("1b 9 段音频 → 按文案选中 2 段台词配音",
          len(req["ref_audios"]) == 2 and any("晒太阳" in u for u in req["ref_audios"])
          and any("太阳一出来" in u for u in req["ref_audios"]), f"got={req['ref_audios']}")
    check("1c 契约无错误", not req["contract_error"], str(req["contract_error"]))
    kf_scene = {**scene, "image": "/outputs/kf.png"}
    req2 = _assemble_video_request(kf_scene, vmodel)
    check("1d 有关键帧+参考素材 → 关键帧并入参考图（不占首帧）",
          req2["mode"] == "reference" and req2["images"][0] == "/outputs/kf.png" and len(req2["images"]) == 3)

    # ---- 1e-1i 参考素材顺序对齐（<Picture/Video/Audio N> ↔ ref_*_N）+ 同台词去重 ----
    rev_scene = user_scene()
    rev_scene["ref_audios"] = list(reversed(AUDIOS))
    req3 = _assemble_video_request(rev_scene, vmodel)
    check("1e 反序挂载配音 → 按 Audio Timeline 台词顺序传入",
          len(req3["ref_audios"]) == 2 and "晒太阳" in req3["ref_audios"][0]
          and "太阳一出来" in req3["ref_audios"][1], f"got={req3['ref_audios']}")

    dup_scene = user_scene()
    dup_scene["ref_audios"] = [AUDIOS[7], AUDIOS[5], AUDIOS[5].replace("30da8308", "30da8309")]
    req4 = _assemble_video_request(dup_scene, vmodel)
    check("1f 同台词两份拷贝只传一份并提示",
          len(req4["ref_audios"]) == 2 and sum("晒太阳" in u for u in req4["ref_audios"]) == 1
          and "重复文件" in req4["audio_selection_note"],
          f"got={req4['ref_audios']} note={req4['audio_selection_note']}")
    check("1g ≤3 段直传也按台词顺序排",
          "晒太阳" in req4["ref_audios"][0] and "太阳一出来" in req4["ref_audios"][1],
          f"got={req4['ref_audios']}")

    img_scene = {**user_scene(), "prompt": IMG_PROMPT, "ref_audios": [],
                 "ref_images": [XIAOMO_IMG, DOCTOR_IMG]}
    req5 = _assemble_video_request(img_scene, vmodel)
    check("1h 反序挂载参考图 → 按 <Picture N> 声明顺序纠正",
          req5["images"] == [DOCTOR_IMG, XIAOMO_IMG], f"got={req5['images']}")

    elim_scene = {**user_scene(), "prompt": IMG_PROMPT, "ref_audios": [],
                  "ref_images": [UUID_IMG, XIAOMO_IMG]}
    req6 = _assemble_video_request(elim_scene, vmodel)
    check("1i uuid 无语义文件名 + 数量与声明一致 → 消元保序不打乱",
          req6["images"] == [UUID_IMG, XIAOMO_IMG], f"got={req6['images']}")

    # ---- 2. preflight ----
    make_project("p_ok", [user_scene()])
    report = orchestrator.preflight(project_service.get_project("p_ok"))
    check("2a 素材齐备 → ready", report["ready"] == [0] and not report["gaps"], str(report))
    make_project("p_noprompt", [{**user_scene(), "prompt": ""}])
    report2 = orchestrator.preflight(project_service.get_project("p_noprompt"))
    check("2b 缺 Prompt → gap 且零 ready", report2["ready"] == [] and len(report2["gaps"]) == 1, str(report2))
    no_audio = {k: v for k, v in user_scene().items()}
    no_audio["ref_audios"] = []
    make_project("p_noaudio", [no_audio])
    report3 = orchestrator.preflight(project_service.get_project("p_noaudio"))
    check("2c 直出模式无任何配音 → gap（不代配）", report3["ready"] == [] and len(report3["gaps"]) == 1)
    report4 = orchestrator.preflight(project_service.get_project("p_noaudio"), synthesize=True)
    check("2d 交接模式有旁白 → 就绪（代为合成）", report4["ready"] == [0],
          str(report4))

    # ---- 3. run_pipeline 直出：提交视频、不改用户数据 ----
    make_project("p_run", [user_scene()])
    before_fp = _prompt_fingerprint(PROMPT)
    agent = _Agent({"project_id": "p_run", "aspect_ratio": "16:9"})
    res = await orchestrator.run_pipeline(agent, "生成视频")
    video_jobs = [j for j in agent.jobs if j.get("type") == "video"]
    check("3a 提交了 1 个视频任务", len(video_jobs) == 1, str(agent.jobs))
    check("3b 步骤含 generate_scene_video 且 ok",
          any(s.get("tool") == "generate_scene_video" and (s.get("result") or {}).get("ok") for s in res["steps"]))
    after = project_service.get_project("p_run")
    check("3c 分镜 Prompt 原样未动", _prompt_fingerprint(after["scenes"][0]["prompt"]) == before_fp)
    check("3d 未设置 scene.voice（对白随参考模式进视频，防导出双混音）", not after["scenes"][0].get("voice"))
    check("3e 旁白未改写", "晒太阳，真的能让白斑" in (after["scenes"][0].get("narration") or ""))
    job = job_manager.get(video_jobs[0]["job_id"])
    check("3f 任务参数携带选中音频与项目归属",
          job["params"]["scene_index"] == 0 and job["params"]["project_id"] == "p_run", str(job["params"]))

    # 残留的 agent 合成旁白（outputs/）在传入对白音频时被清掉，防导出双混音；用户上传的不动
    stale = {**user_scene(), "voice": "/api/files/outputs/audio/old_tts.mp3"}
    make_project("p_clear", [stale])
    agent_c = _Agent({"project_id": "p_clear", "aspect_ratio": "16:9"})
    await orchestrator.run_pipeline(agent_c, "生成视频")
    check("3g 残留合成旁白被清除", not project_service.get_project("p_clear")["scenes"][0].get("voice"))
    upload_voice = {**user_scene(), "voice": "/api/files/uploads/audio/user_voice.mp3"}
    make_project("p_keep", [upload_voice])
    agent_k = _Agent({"project_id": "p_keep", "aspect_ratio": "16:9"})
    await orchestrator.run_pipeline(agent_k, "生成视频")
    check("3h 用户上传配音不被清除",
          project_service.get_project("p_keep")["scenes"][0].get("voice") == "/api/files/uploads/audio/user_voice.mp3")

    # ---- 4. 路由（零 LLM 快路径的启发式判定）----
    proj = project_service.get_project("p_run")
    check("4a 素材+生成措辞 → pipeline",
          agent_service._heuristic_route("我给了分镜提示词、参考图、配音，生成分镜视频", proj) == "pipeline")
    check("4b 创作措辞 → agent",
          agent_service._heuristic_route("把这个新剧本重新拆分镜写文案", proj) == "agent")
    check("4c 提问 → 交给助手循环", agent_service._heuristic_route("这个分镜的 Prompt 有什么问题？", proj) is None)
    empty_proj = {"scenes": [{"prompt": "x"}]}
    check("4d 无素材项目 → 交给助手循环", agent_service._heuristic_route("生成视频", empty_proj) is None)

    # 素材齐备且纯执行指令：run_agent 全程不调 LLM（快路径）
    make_project("p_route", [user_scene()])
    agent_service.resolve_llm_call = lambda *a, **kw: (FailLlm(), "no-llm")
    r = await agent_service.run_agent("我给了分镜提示词、参考图、配音，生成分镜视频",
                                      {"project_id": "p_route"}, None, None)
    check("4e 快路径零 LLM 调用且提交了视频",
          any(j.get("type") == "video" for j in r.get("jobs", [])) and "流水线" in r.get("reply", ""),
          r.get("reply", "")[:200])

    # ---- 5. 创作路径交接：create_scenes 后自动跑流水线并合成旁白 ----
    cs = {"action": "tool", "tool": "create_scenes", "args": {"scenes": [
        {"title": "镜1", "prompt": "I2V shot 1 full prompt with action timeline",
         "keyframe_prompt": "Medium shot, character A in clinic, warm light", "narration": "第一镜旁白文案",
         "duration": 5, "gen_mode": "i2v"},
        {"title": "镜2", "prompt": "I2V shot 2 full prompt with action timeline",
         "keyframe_prompt": "Wide shot, character A walking outside, soft daylight", "narration": "第二镜旁白文案",
         "duration": 5, "gen_mode": "i2v"},
        {"title": "镜3", "prompt": "I2V shot 3 full prompt with action timeline",
         "keyframe_prompt": "Close-up, character A smiling, golden hour rim light", "narration": "第三镜旁白文案",
         "duration": 5, "gen_mode": "i2v"},
    ]}}
    finish = {"action": "finish", "summary": "分镜已录入，系统将自动生成。"}
    make_project("p_handoff", [])
    import json as _json
    script_llm = ScriptLlm([
        _json.dumps(cs, ensure_ascii=False),   # 双模式路由已拆除：无路由分类调用
        _json.dumps(finish, ensure_ascii=False),
    ])
    agent_service.resolve_llm_call = lambda *a, **kw: (script_llm, "fake")
    r2 = await agent_service.run_agent("把我的剧本拆成分镜并生成视频", {"project_id": "p_handoff"}, None, None)
    hproj = project_service.get_project("p_handoff")
    check("5a 交接后每镜生成了关键帧并合成旁白",
          all(s.get("image") and s.get("voice") for s in hproj["scenes"]),
          str([(s.get("image"), s.get("voice")) for s in hproj["scenes"]]))
    check("5b 交接后每镜提交了视频任务",
          len([j for j in r2.get("jobs", []) if j.get("type") == "video"]) == 3, str(r2.get("jobs")))
    check("5c 回复包含流水线汇报", "系统流水线" in r2.get("reply", ""))

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
