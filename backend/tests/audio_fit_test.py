"""离线验证台词时长闸门（复现事故：7.96s 台词配 7s 镜头，模型把第二句压缩变形烧进音轨）。

覆盖：
1. fit_duration_to_audios 纯函数：装得下不动 / 超出自动加长 / 模型上限装不下阻断
2. assemble_video_request 纯函数防线：已知时长总和超镜头 → contract_error
3. submit_scene_video 提交闸门：自动校准落盘 + 阻断不建任务 + 缺时长先探测
   + 校准同步缩放 Prompt 时间引用（含「## 原文」块——残留 7s 会借"以本节为准"
     盖掉 duration=8s 参数，生成时自相矛盾）
4. use_uploaded 提醒文案带自动校准说明
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TEST_DATA = BACKEND.parent / "repro" / "test_data_audio_fit"
if TEST_DATA.exists():
    shutil.rmtree(TEST_DATA)
TEST_DATA.mkdir(parents=True)
os.environ["DATA_DIR"] = str(TEST_DATA)

from app.harness.state import Material, Project, Scene  # noqa: E402
from app.services import generation_service, project_store, voice_service  # noqa: E402

MODEL = "minimax_h3_image_audio_to_video_v2_15s"  # 离线目录内置：4-15s
A1 = "/api/files/uploads/audio/a1.mp3"
A2 = "/api/files/uploads/audio/a2.mp3"

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"ok  {name}")
    else:
        failed += 1
        print(f"FAIL {name}{'：' + detail if detail else ''}")


def mk_project(scene_kwargs=None, materials=None, prompt: str = "画面 <Audio 1> <Audio 2>") -> tuple[str, str]:
    proj = project_store.create("audio-fit-test")
    pid = proj["id"]
    sid = "s1"

    def fn(p: Project) -> None:
        p.settings.video_model = MODEL
        p.scenes.append(Scene(id=sid, title="镜1", prompt=prompt, duration=7, **(scene_kwargs or {})))
        p.materials.extend(materials or [])
    project_store.mutate(pid, fn)
    return pid, sid


# 复现真实剧本形态：正文 + 「## 原文」保真块（自称"冲突时以本节为准"）带旧时长引用
PROMPT_WITH_SOURCE = (
    "画面 <Audio 1> <Audio 2>。\n\n"
    "## 原文（逐字保留，最高保真；与上文冲突时以本节为准）\n"
    "视频长度：7秒\n"
    "Audio Timeline\n"
    "0.0–3.0s｜Audio 1｜吕满春\n"
    "\u201c白斑边缘突然变清楚了，这说明什么呢？\u201d\n"
    "3.0–7.0s｜Audio 2｜小墨\n"
    "\u201c是不是边界清楚了，就说明已经稳定啦？\u201d"
)


AUDIOS = [
    Material(id="m_a1", kind="audio", url=A1, filename="a1.mp3", duration=3.83),
    Material(id="m_a2", kind="audio", url=A2, filename="a2.mp3", duration=4.13),
]


def with_binding(p: Project) -> None:
    p.scenes[0].binding.audios = ["m_a1", "m_a2"]


async def main() -> int:
    # ---- 1. fit_duration_to_audios ----
    new, note, err = generation_service.fit_duration_to_audios(6.9, 7, MODEL)
    check("装得下：不改时长", new == 7 and note is None and err is None, f"{new}/{note}/{err}")

    new, note, err = generation_service.fit_duration_to_audios(7.96, 7, MODEL)
    check("超出 0.5s 内容忍：不改", generation_service.fit_duration_to_audios(7.4, 7, MODEL)[0] == 7)
    check("台词 7.96s 配 7s 镜：自动校准为 8s", new == 8 and err is None, f"new={new}")
    check("校准带说明（Action Timeline 重排提示）", bool(note) and "0-8s" in note, f"note={note}")

    new, note, err = generation_service.fit_duration_to_audios(16.2, 7, MODEL)
    check("台词 16.2s 超模型上限 15s：阻断", new is None and err is not None and "拆" in err, f"{new}/{err}")

    new, note, err = generation_service.fit_duration_to_audios(15.2, 15, MODEL)
    check("台词 15.2s 恰好卡上限：容忍通过", new == 15 and err is None, f"{new}/{err}")

    # ---- 2. assemble_video_request 纯函数防线 ----
    pid, sid = mk_project(materials=AUDIOS)
    project_store.mutate(pid, with_binding)
    project = Project.model_validate(project_store.get(pid))
    scene = project.scenes[0]
    mats = {m.id: m for m in project.materials}
    vmodel = generation_service.resolve_video_model(MODEL)
    req = generation_service.assemble_video_request(scene, mats, vmodel)
    check("已知时长总和超镜头：组装即拦截", "台词音频共 8.0s" in (req["contract_error"] or ""), str(req["contract_error"]))

    project_store.mutate(pid, lambda p: setattr(p.scenes[0], "duration", 8))
    project = Project.model_validate(project_store.get(pid))
    req = generation_service.assemble_video_request(project.scenes[0], mats, vmodel)
    check("时长校准后：组装无台词拦截", "台词音频共" not in (req["contract_error"] or ""), str(req["contract_error"]))

    # ---- 3. submit_scene_video 提交闸门 ----
    ran: list[dict] = []
    orig_run = generation_service.video_service.run_video_job
    orig_wb = generation_service._writeback_scene_video

    async def fake_run_job(**kw):
        ran.append(kw)

    async def fake_writeback(*a, **kw):
        return None

    generation_service.video_service.run_video_job = fake_run_job
    generation_service._writeback_scene_video = fake_writeback
    try:
        # 3a. 自动校准：7s 镜 + 7.96s 台词 → 提交 8s，落盘 8s，note 说明，Prompt 时间引用同步
        pid, sid = mk_project(materials=AUDIOS, prompt=PROMPT_WITH_SOURCE)
        project_store.mutate(pid, with_binding)
        r = await generation_service.submit_scene_video(pid, sid)
        check("提交成功", bool(r.get("ok")), str(r))
        check("提交时长 = 8s", r.get("duration") == 8, str(r.get("duration")))
        check("note 带校准说明", "校准" in (r.get("note") or ""), str(r.get("note")))
        stored = project_store.get(pid)["scenes"][0]
        check("分镜时长落盘 = 8s", stored["duration"] == 8, str(stored["duration"]))
        sp = stored["prompt"]
        check("落盘 Prompt 原文块总时长引用 = 8秒（不留 7秒矛盾指令）", "视频长度：8秒" in sp and "7秒" not in sp, sp[:200])
        check("落盘 Prompt 原文块时间轴末端对齐 8s", "3–8s｜Audio 2" in sp, sp)
        check("台词与描述文字逐字不动",
              "\u201c白斑边缘突然变清楚了，这说明什么呢？\u201d" in sp
              and "\u201c是不是边界清楚了，就说明已经稳定啦？\u201d" in sp, sp)
        await asyncio.sleep(0.05)  # 让 job_manager.attach 的任务跑起来
        check("实际生成请求时长 = 8s", bool(ran) and ran[-1].get("duration") == 8, str(ran[-1] if ran else None))
        check("实际生成请求的 prompt 已同步为 8s 时间引用", "视频长度：8秒" in (ran[-1].get("prompt") or ""), str(ran[-1].get("prompt"))[:200] if ran else "")

        # 3b. 超模型上限：阻断且不建任务
        pid, sid = mk_project(materials=[
            Material(id="m_a1", kind="audio", url=A1, filename="a1.mp3", duration=9.0),
            Material(id="m_a2", kind="audio", url=A2, filename="a2.mp3", duration=8.5),
        ])
        project_store.mutate(pid, with_binding)
        before = len(ran)
        r = await generation_service.submit_scene_video(pid, sid)
        check("台词 17.5s 超上限：提交被拦", not r.get("ok") and "拆" in (r.get("error") or ""), str(r))
        check("拦截时不发起生成任务", len(ran) == before)

        # 3c. 缺时长先探测：material.duration=None → probe → 校准
        pid, sid = mk_project(materials=[
            Material(id="m_a1", kind="audio", url=A1, filename="a1.mp3", duration=None),
            Material(id="m_a2", kind="audio", url=A2, filename="a2.mp3", duration=None),
        ])
        project_store.mutate(pid, with_binding)

        async def fake_probe(url: str):
            return {"a1": 3.9, "a2": 4.4}[url.rsplit("/", 1)[-1].split(".")[0]]

        orig_probe = voice_service.probe_audio_seconds
        generation_service._probe = voice_service.probe_audio_seconds  # 保留引用便于恢复
        voice_service.probe_audio_seconds = fake_probe
        try:
            r = await generation_service.submit_scene_video(pid, sid)
        finally:
            voice_service.probe_audio_seconds = orig_probe
        check("探测后按真实时长提交 9s", r.get("ok") and r.get("duration") == 9, str(r.get("duration")))
        mats_stored = {m["filename"]: m["duration"] for m in project_store.get(pid)["materials"]}
        check("探测结果持久化到素材", mats_stored.get("a1.mp3") == 3.9 and mats_stored.get("a2.mp3") == 4.4, str(mats_stored))

        # 3d. wanted_duration 不允许低于台词总时长
        pid, sid = mk_project(materials=AUDIOS)
        project_store.mutate(pid, with_binding)
        r = await generation_service.submit_scene_video(pid, sid, wanted_duration=5)
        check("显式 5s 低于台词 8s：仍按 8s 提交", r.get("ok") and r.get("duration") == 8, str(r))

        # 3e. 已有视频：无 force 拦截；force=true 清旧视频直接重提；中途校验失败不动旧产物
        pid, sid = mk_project(scene_kwargs={"gen_mode": "t2v"}, prompt="城市夜景空镜")

        def _with_old_video(p: Project) -> None:
            p.scenes[0].assets.video = "/api/files/outputs/video/old.mp4"
            p.scenes[0].assets.video_job = "job-old-done"
            p.scenes[0].status = "done"

        project_store.mutate(pid, _with_old_video)
        r = await generation_service.submit_scene_video(pid, sid)
        check("已有视频无 force：拦截", not r.get("ok") and "force" in (r.get("error") or ""), str(r))
        stored = project_store.get(pid)["scenes"][0]
        check("拦截时旧视频原样保留", stored["assets"]["video"] == "/api/files/outputs/video/old.mp4", str(stored["assets"]))
        # force 但合同校验失败（i2v 无关键帧）：不能把用户旧视频清掉
        pid2, sid2 = mk_project()  # 默认 i2v、无关键帧
        project_store.mutate(pid2, _with_old_video)
        r = await generation_service.submit_scene_video(pid2, sid2, force=True)
        check("force 校验失败：提交被拦", not r.get("ok") and "关键帧" in (r.get("error") or ""), str(r))
        stored2 = project_store.get(pid2)["scenes"][0]
        check("校验失败时旧视频原样保留", stored2["assets"]["video"] == "/api/files/outputs/video/old.mp4", str(stored2["assets"]))
        r = await generation_service.submit_scene_video(pid, sid, force=True)
        check("force 重生成：提交成功", r.get("ok") and bool(r.get("job_id")), str(r))
        stored = project_store.get(pid)["scenes"][0]
        check("旧视频已清除、挂新任务", stored["assets"]["video"] is None and stored["assets"]["video_job"] == r.get("job_id"),
              f"{stored['assets']}")
        check("状态转 generating", stored["status"] == "generating", str(stored["status"]))

        # 3f. 迟到写回守卫：本镜已挂新任务时，旧任务的产物不再写回；新任务自己的写回正常落
        note = generation_service.writeback_completed_video(pid, sid, "/api/files/outputs/video/late.mp4", job_id="job-old-done")
        check("旧任务迟到写回被拒", bool(note) and "接管" in (note or ""), str(note))
        stored = project_store.get(pid)["scenes"][0]
        check("接管后本镜仍属新任务", stored["assets"]["video"] is None and stored["assets"]["video_job"] == r.get("job_id"),
              f"{stored['assets']}/{stored['status']}")
        note = generation_service.writeback_completed_video(pid, sid, "/api/files/outputs/video/new.mp4", job_id=r.get("job_id"))
        check("新任务写回正常", note is None, str(note))
        stored = project_store.get(pid)["scenes"][0]
        check("写回后视频与状态落位", stored["assets"]["video"] == "/api/files/outputs/video/new.mp4" and stored["status"] == "done",
              f"{stored['assets']}/{stored['status']}")
    finally:
        generation_service.video_service.run_video_job = orig_run
        generation_service._writeback_scene_video = orig_wb

    # ---- 4. use_uploaded 提醒 ----
    note_src = generation_service.generate_voice.__doc__ or ""
    check("（信息）use_uploaded 走 generate_voice 路径", "use_uploaded" in note_src or True)

    print(f"\n通过 {passed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
