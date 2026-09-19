"""reconcile_selfcheck：调谐器 + 请求组装自检（隔离 DATA_DIR，不碰真实 data/，不联网）。

覆盖：scene_gaps 差距计算与前置依赖 / assemble_video_request 的 mode 判定与契约拦截 /
<Audio N> 重编号 / 调谐循环（顺序补齐、幂等跳过、重试预算耗尽转 failed、取消即时返回）。
运行：.venv/Scripts/python.exe tests/reconcile_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="reconcile_selfcheck_")

from app.harness.state import ImageBinding, Material, Project, Scene  # noqa: E402
from app.services import generation_service, project_store, reconcile  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


# ---------------- scene_gaps ----------------
s = Scene(id="s1", title="镜1", prompt="一个测试 prompt", narration="旁白文案", duration=5, keyframe_prompt="全景，城市天际线，清晨光线")
gaps, block = reconcile.scene_gaps(s)
check("i2v 无关键帧无参考 → keyframe+voice，无 video（前置不满足）", gaps == ["keyframe", "voice"] and block is None, f"{gaps}")

s2 = Scene(id="s2", title="镜2", prompt="p", narration="n", duration=5)
s2.assets.keyframe = "/api/files/outputs/image/x.png"
gaps, _ = reconcile.scene_gaps(s2)
check("有 keyframe 有 voice 缺 → voice 后补 video 前置", gaps == ["voice"], f"{gaps}")

s3 = Scene(id="s3", title="镜3", prompt="p", duration=5)
s3.assets.keyframe = "/k.png"
s3.assets.video = "/v.mp4"
s3.status = "done"
gaps, _ = reconcile.scene_gaps(s3)
check("已完成镜无差距", gaps == [])

s4 = Scene(id="s4", title="镜4", prompt="p", duration=5, gen_mode="t2v")
gaps, _ = reconcile.scene_gaps(s4)
check("t2v 无 keyframe 需求，直接 video", gaps == ["video"], f"{gaps}")

s5 = Scene(id="s5", title="镜5", prompt="p", duration=5, gen_mode="i2v")
s5.binding.images.append(ImageBinding(material_id="m1"))
gaps, _ = reconcile.scene_gaps(s5)
check("绑了参考图的 i2v 不需要 keyframe", gaps == ["video"], f"{gaps}")

s6 = Scene(id="s6", title="空镜", prompt="", duration=5)
gaps, _ = reconcile.scene_gaps(s6)
check("空镜不参与调谐", gaps == [])

s7 = Scene(id="s7", title="无描述", prompt="p", duration=5, gen_mode="i2v")
gaps, block = reconcile.scene_gaps(s7)
check("i2v 无关键帧描述 → 阻塞原因", gaps == [] and block and "关键帧描述" in block)

s8 = Scene(id="s8", title="用户音频镜", prompt="p", narration="n", duration=5)
s8.binding.audios.append("m1")
s8.assets.keyframe = "/k.png"
gaps, _ = reconcile.scene_gaps(s8)
check("绑了用户音频的镜不需要 TTS voice", gaps == ["video"], f"{gaps}")

# ---------------- assemble_video_request ----------------
MATS = {
    "m1": Material(id="m1", kind="image", url="/api/files/uploads/image/a_小墨.png", filename="小墨.png"),
    "m2": Material(id="m2", kind="audio", url="/api/files/uploads/audio/x_tts_1_哎呀原来如此.mp3", filename="哎呀原来如此.mp3"),
}
VMODEL = next(m for m in __import__("app.config.settings", fromlist=["VIDEO_MODELS"]).VIDEO_MODELS
              if m["id"] == "minimax_h3_image_audio_to_video_v2_15s")

sc = Scene(id="sc", title="组装", prompt="画面，<Audio 3>台词", narration="哎呀原来如此", duration=5)
sc.assets.keyframe = "/kf.png"
sc.binding.audios.append("m2")
req = generation_service.assemble_video_request(sc, MATS, VMODEL)
check("keyframe+音频 → reference 模式且 keyframe 在首位", req["mode"] == "reference" and req["images"][0] == "/kf.png", f"{req['mode']}")
check("全局 <Audio 3> 重编号为 <Audio 1>", "<Audio 1>" in req["prompt"], req["prompt"])
check("无契约错误", not req["contract_error"], req["contract_error"])

sc2 = Scene(id="sc2", title="悬空标签", prompt="画面 <Picture 2>", duration=5, gen_mode="t2v")
req2 = generation_service.assemble_video_request(sc2, {}, VMODEL)
check("悬空 <Picture 2> 被拦截", req2["contract_error"] and "Picture" in req2["contract_error"])

sc3 = Scene(id="sc3", title="音频标签无音频", prompt="<Audio 1> 台词", duration=5, gen_mode="t2v")
req3 = generation_service.assemble_video_request(sc3, {}, VMODEL)
check("有音频标签但没绑音频被拦截", req3["contract_error"] and "音频" in req3["contract_error"])

sc4 = Scene(id="sc4", title="i2v 无素材", prompt="p", duration=5, gen_mode="i2v")
req4 = generation_service.assemble_video_request(sc4, {}, VMODEL)
check("i2v 无 keyframe 无参考被拦截", req4["contract_error"] and "关键帧" in req4["contract_error"])

# t2v 镜遇到仅支持 image/reference 的工作流（no_pic 是纯 t2v；zm_u24 是纯参考）——反向验证契约
VMODEL_REF_ONLY = next(m for m in __import__("app.config.settings", fromlist=["VIDEO_MODELS"]).VIDEO_MODELS
                       if m["id"] == "minimax_h3_zm_u24")
sc5 = Scene(id="sc5", title="t2v 遇纯参考工作流", prompt="纯文生画面", duration=5, gen_mode="t2v")
req5 = generation_service.assemble_video_request(sc5, {}, VMODEL_REF_ONLY)
check("t2v 遇仅参考工作流被契约拦截", req5["contract_error"] and "模式" in req5["contract_error"], req5["contract_error"])

# ---------------- 调谐循环（打桩 generation_service） ----------------
p = project_store.create("调谐测试")
PID = p["id"]


def _mk_scene(sid: str, **kw) -> Scene:
    return Scene(id=sid, title=kw.pop("title", sid), prompt=kw.pop("prompt", "测试 prompt 很完整"),
                 narration=kw.pop("narration", ""), duration=5,
                 keyframe_prompt=kw.pop("keyframe_prompt", "中景，主体居中，柔和光线"), **kw)


def add_scenes(scenes: list[Scene], materials: list[Material] | None = None) -> None:
    def fn(prj: Project) -> None:
        prj.scenes.extend(scenes)
        if materials:
            prj.materials.extend(materials)
    project_store.mutate(PID, fn)


calls: list[tuple[str, str]] = []


async def fake_keyframe(project_id, scene_id, **kw):
    calls.append(("keyframe", scene_id))
    def fn(p: Project) -> None:
        s = next(x for x in p.scenes if x.id == scene_id)
        s.assets.keyframe = f"/kf_{scene_id}.png"
        s.status = "ready"
    project_store.mutate(project_id, fn)
    return {"ok": True}


async def fake_voice(project_id, scene_id, **kw):
    calls.append(("voice", scene_id))
    def fn(p: Project) -> None:
        s = next(x for x in p.scenes if x.id == scene_id)
        s.assets.voice = f"/v_{scene_id}.mp3"
        s.status = "ready"
    project_store.mutate(project_id, fn)
    return {"ok": True}


fake_video_job_id = {"n": 0}


def fake_video(project_id, scene_id, **kw):
    calls.append(("video", scene_id))
    fake_video_job_id["n"] += 1
    job = f"video_fake_{fake_video_job_id['n']}"
    def fn(p: Project) -> None:
        s = next(x for x in p.scenes if x.id == scene_id)
        s.assets.video = f"/vid_{scene_id}.mp4"
        s.assets.video_job = job
        s.status = "done"
    project_store.mutate(project_id, fn)
    return {"ok": True, "job_id": job}


originals = (generation_service.generate_keyframe, generation_service.generate_voice, generation_service.submit_scene_video)


def stub(fk=fake_keyframe, fv=fake_voice, fvid=fake_video):
    generation_service.generate_keyframe = fk
    generation_service.generate_voice = fv
    generation_service.submit_scene_video = fvid


def restore():
    generation_service.generate_keyframe, generation_service.generate_voice, generation_service.submit_scene_video = originals


# 场景 A：顺序补齐 keyframe→voice→video
add_scenes([_mk_scene("a1", narration="旁白文案")])
stub()
summary = asyncio.run(reconcile.run_generate_stage(PID))
restore()
order_ok = calls == [("keyframe", "a1"), ("voice", "a1"), ("video", "a1")]
check("单镜按 keyframe→voice→video 顺序补齐", order_ok, f"{calls}")
check("差距清零后循环退出", summary["failed"] == [] and summary["blocked"] == [])

# 场景 B：幂等——已完成不重跑
calls.clear()
add_scenes([_mk_scene("b1")])
stub()
summary = asyncio.run(reconcile.run_generate_stage(PID))
restore()
check("已有产物不重复提交（仅处理新镜 b1）", [c for c in calls if c[1] == "a1"] == [], f"{calls}")

# 场景 C：失败重试预算——voice 恒败，2 次后转 failed 且不无限循环
calls.clear()
fail_count = {"n": 0}


async def failing_voice(project_id, scene_id, **kw):
    fail_count["n"] += 1
    calls.append(("voice-fail", scene_id))
    def fn(p: Project) -> None:
        s = next(x for x in p.scenes if x.id == scene_id)
        s.status = "failed"
        s.error = "TTS 挂了"
        s.attempts["voice"] = s.attempts.get("voice", 0) + 1
    project_store.mutate(project_id, fn)
    return {"ok": False, "error": "TTS 挂了"}


add_scenes([_mk_scene("c1", narration="n")])
stub(fv=failing_voice)
summary = asyncio.run(reconcile.run_generate_stage(PID))
restore()
check("失败 2 次后预算耗尽转 failed", fail_count["n"] == 2 and any(f["scene_id"] == "c1" for f in summary["failed"]), f"{fail_count} {summary['failed']}")

# 场景 D：取消即时返回
calls.clear()
add_scenes([_mk_scene("d1")])
stub()
summary = asyncio.run(reconcile.run_generate_stage(PID, cancel_check=lambda: True))
restore()
check("取消后立即返回不执行任何生成", calls == [] and summary.get("cancelled") is True, f"{calls}")
# 取消测试留下的 d1 先正常补齐，避免污染后续场景
stub()
asyncio.run(reconcile.run_generate_stage(PID))
restore()

# 场景 E：活跃视频 job 不重复提交
calls.clear()
sc_e = _mk_scene("e1")
sc_e.assets.keyframe = "/kf.png"
sc_e.assets.video_job = "video_active_1"

from app.jobs import job_manager  # noqa: E402

job_manager.jobs["video_active_1"] = {"id": "video_active_1", "status": "generating"}
def _boom_video(pid, sid, **kw):
    raise AssertionError("不该提交视频")


add_scenes([sc_e])
stub(fvid=_boom_video)
summary = asyncio.run(reconcile.run_generate_stage(PID))
restore()
check("活跃 job 的镜不重复提交视频", ("video", "e1") not in calls)

print(f"\n全部通过：{PASS} 项")
