"""video_billing_guard_selfcheck：视频计费安全闸门离线自测（不联网、不调真实服务商）。

背景事故（2026-09-28，project_7278137e）：18 镜合计 127s 的分镜稿被提交了 34 个
任务（237s 成功 + 5s 云端成功未取址 ≈ 9.68 元）。根因：「任务已完成」与「产物写回
分镜」之间的空窗里，调谐循环把该镜重新判成缺口重复提交；另有提交侧检查-占位
非原子（TOCTOU）与指纹漂移白烧钱两条隐患。本测试锁定修复后的不变量：

  A. 提交占位原子：并发提交 / 写回空窗 / 自动重提交 / 指纹漂移全部拦截，
     服务商调用数（= 计费事件）一条不多
  B. 调谐循环每镜恰好提交一次（今日 14×2 事故回归）
  C. 钱已花的产物必须能落回：正常写回、指纹失配释放槽位、重启补写回、
     云端成功未取到地址的再取回、provider 层 SUCCESS 无地址不再判死

运行：.venv/Scripts/python.exe tests/video_billing_guard_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="video_billing_guard_")

from app.jobs import job_manager  # noqa: E402
from app.harness.state import ImageBinding, Material, Scene, SceneBinding  # noqa: E402
from app.services import generation_service, project_store, video_service  # noqa: E402
from app.services import job_recovery as job_recovery_mod  # noqa: E402
from app.services import reconcile  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


# ---------------- 测试桩：假服务商（计费事件 = fake_run_video_job 调用数） ----------------

STATE = {"calls": 0, "mode": "ok", "delay": 0.02}


async def fake_run_video_job(job_id: str, **kwargs) -> None:
    STATE["calls"] += 1
    job_manager.update(job_id, status="generating")
    await asyncio.sleep(STATE["delay"])
    if STATE["mode"] == "fail":
        raise RuntimeError("fake: provider failed")
    out = f"/api/files/outputs/video/{job_id}.mp4"
    job_manager.update(job_id, status="completed", output=out, outputs=[out])


video_service.run_video_job = fake_run_video_job
video_service.autodl_contract_error = lambda *a, **k: ""  # 跳过工作流契约目录依赖
generation_service.VIDEO_POLL_INTERVAL = 0.01
reconcile.JOB_POLL_INTERVAL = 0.01

# 可闸住的写回：gate 关闭时模拟「任务已完成但产物尚未写回」的事故空窗
_real_writeback = generation_service._writeback_scene_video
_gate = asyncio.Event()


async def _gated_writeback(project_id: str, scene_id: str, job_id: str, fingerprint: str, trace=None) -> None:
    try:
        await _gate.wait()
        await _real_writeback(project_id, scene_id, job_id, fingerprint, trace)
    finally:
        generation_service._pending_video_writebacks.discard((project_id, scene_id, job_id))


def use_gated_writeback(gated: bool, *, closed: bool = True) -> None:
    generation_service._writeback_scene_video = _gated_writeback if gated else _real_writeback
    if gated and closed:
        _gate.clear()
    else:
        _gate.set()


# ---------------- 项目与等待辅助 ----------------

def make_project(n_scenes: int = 1) -> tuple[str, list[str]]:
    p = project_store.create("计费闸门测试", "9:16")
    pid = p["id"]
    sids = [f"scene_{i}_{uuid.uuid4().hex[:6]}" for i in range(n_scenes)]

    def add(pobj) -> None:
        for i, sid in enumerate(sids):
            pobj.scenes.append(Scene(
                id=sid, title=f"镜{i + 1}",
                prompt="A cinematic mystery shot, restrained realistic style.",
                duration=5, gen_mode="i2v",
                binding=SceneBinding(images=[ImageBinding(material_id="mat_img", role="reference")]),
            ))
        pobj.materials.append(Material(id="mat_img", kind="image", url="/api/files/uploads/ref.png"))

    project_store.mutate(pid, add)
    return pid, sids


def scene_of(pid: str, sid: str) -> dict:
    p = project_store.get(pid)
    return next(s for s in p["scenes"] if s["id"] == sid)


async def wait_job_terminal(job_id: str, timeout: float = 5.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        j = job_manager.get(job_id)
        if j and j["status"] in ("completed", "failed"):
            return j
        await asyncio.sleep(0.01)
    raise TimeoutError(f"job {job_id} 未到终态")


async def wait_scene(pid: str, sid: str, pred, timeout: float = 5.0, what: str = "") -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        s = scene_of(pid, sid)
        if pred(s):
            return s
        await asyncio.sleep(0.01)
    raise TimeoutError(f"分镜条件未满足：{what}")


async def wait_scene_status(pid: str, sid: str, status: str) -> dict:
    return await wait_scene(pid, sid, lambda s: s["status"] == status, what=f"status={status}")


# ---------------- A. 提交占位原子性 ----------------

async def t_race_window_intercepted() -> None:
    """今日事故复现：任务已完成、产物写回前，任何重复提交都必须被拦下。"""
    use_gated_writeback(True, closed=True)
    try:
        pid, (sid,) = make_project(1)
        base = STATE["calls"]
        r1 = await generation_service.submit_scene_video(pid, sid)  # 自动链路首次提交
        check("事故复现: 首次提交成功", r1.get("ok") is True, str(r1))
        await wait_job_terminal(r1["job_id"])  # 云端完成，写回仍被闸住

        # 调谐视角：写回空窗内该镜不是缺口
        sc = Scene(**scene_of(pid, sid))
        gaps, _ = reconcile.scene_gaps(sc, pid)
        check("事故复现: 写回空窗不判 video 缺口", "video" not in gaps, str(gaps))

        r_auto = await generation_service.submit_scene_video(pid, sid)  # 自动重提交
        check("事故复现: 自动重提交被收回", not r_auto.get("ok") and "自动重提交" in r_auto.get("error", ""), str(r_auto))
        r_manual = await generation_service.submit_scene_video(pid, sid, manual=True)  # 写回中人工抢跑
        check("事故复现: 写回中拦截重复提交", not r_manual.get("ok") and "写回中" in r_manual.get("error", ""), str(r_manual))
        check("事故复现: 服务商零新增调用", STATE["calls"] == base + 1, f"calls={STATE['calls']}")

        _gate.set()
        s = await wait_scene(pid, sid, lambda x: x["assets"]["video"], what="产物写回")
        check("事故复现: 开闸后产物落地", s["status"] == "done" and s["assets"]["video"], s["status"])

        r_dup = await generation_service.submit_scene_video(pid, sid, manual=True)
        check("已有视频: 无 force 拦截", not r_dup.get("ok") and "force" in r_dup.get("error", ""), str(r_dup))
        r_force = await generation_service.submit_scene_video(pid, sid, manual=True, force=True)
        check("已有视频: force 人工重生成放行", r_force.get("ok") is True, str(r_force))
        await wait_job_terminal(r_force["job_id"])
        await wait_scene(pid, sid, lambda x: x["status"] == "done" and x["assets"]["video_job"] == r_force["job_id"],
                         what="force 重生成写回")
        check("服务商调用数 = 首次+force 共 2", STATE["calls"] == base + 2, f"calls={STATE['calls']}")
    finally:
        use_gated_writeback(False)


async def t_concurrent_submit() -> None:
    """并发双击：只有一个提交能到达服务商。"""
    pid, (sid,) = make_project(1)
    base = STATE["calls"]
    r = await asyncio.gather(
        generation_service.submit_scene_video(pid, sid),
        generation_service.submit_scene_video(pid, sid, manual=True),
    )
    oks = [x for x in r if x.get("ok")]
    check("并发: 恰好一个成功", len(oks) == 1, str([x.get("error") for x in r]))
    check("并发: 服务商只调用一次", STATE["calls"] == base + 1, f"calls={STATE['calls']}")
    await wait_scene_status(pid, sid, "done")


async def t_auto_ban_after_failure() -> None:
    """失败后：自动链路不再重试，人工重试放行。"""
    STATE["mode"] = "fail"
    try:
        pid, (sid,) = make_project(1)
        base = STATE["calls"]
        r1 = await generation_service.submit_scene_video(pid, sid)
        check("失败链路: 首次自动提交成功", r1.get("ok") is True, str(r1))
        await wait_scene_status(pid, sid, "failed")
        r2 = await generation_service.submit_scene_video(pid, sid)  # 自动重试
        check("失败链路: 自动重试被收回", not r2.get("ok") and "自动重提交" in r2.get("error", ""), str(r2))
        STATE["mode"] = "ok"
        r3 = await generation_service.submit_scene_video(pid, sid, manual=True)  # 人工重试
        check("失败链路: 人工重试放行", r3.get("ok") is True, str(r3))
        await wait_scene_status(pid, sid, "done")
        check("失败链路: 服务商共 2 次", STATE["calls"] == base + 2, f"calls={STATE['calls']}")
    finally:
        STATE["mode"] = "ok"


async def t_fingerprint_drift_blocked() -> None:
    """提交期间 Prompt 被改：拦截在占位阶段，服务商零调用（不烧必被丢弃的钱）。"""
    pid, (sid,) = make_project(1)
    base = STATE["calls"]
    orig_ensure = generation_service.ensure_scene_audio_fit

    async def hooked_ensure(project, scene, project_id):
        def bump(pobj):
            s = next(x for x in pobj.scenes if x.id == scene.id)
            s.prompt = s.prompt + "  <用户中途改稿>"
        project_store.mutate(project_id, bump)
        return None, None

    generation_service.ensure_scene_audio_fit = hooked_ensure
    try:
        r = await generation_service.submit_scene_video(pid, sid, manual=True)
        check("指纹漂移: 提交被拦截", not r.get("ok") and "已被修改" in r.get("error", ""), str(r))
        check("指纹漂移: 服务商零调用", STATE["calls"] == base, f"calls={STATE['calls']}")
    finally:
        generation_service.ensure_scene_audio_fit = orig_ensure
    s = scene_of(pid, sid)
    check("指纹漂移: 分镜保持原状（无在途任务）", not s["assets"]["video_job"] and not s["assets"]["video"],
          str(s["assets"]))


async def t_fingerprint_release_slot() -> None:
    """生成期间改稿 → 产物不落镜但槽位释放，人工可重生成。"""
    use_gated_writeback(True, closed=True)
    try:
        pid, (sid,) = make_project(1)
        base = STATE["calls"]
        r1 = await generation_service.submit_scene_video(pid, sid, manual=True)
        await wait_job_terminal(r1["job_id"])
        project_store.mutate(pid, lambda p: setattr(
            next(x for x in p.scenes if x.id == sid), "prompt", "改稿后的全新 Prompt"))
        _gate.set()
        s = await wait_scene(pid, sid, lambda x: x["assets"]["video_job"] is None, what="槽位释放")
        check("指纹失配: 产物不落镜+槽位释放", s["status"] == "planned" and not s["assets"]["video"]
              and "未写回" in (s.get("error") or ""), str(s))
        r2 = await generation_service.submit_scene_video(pid, sid, manual=True)
        check("指纹失配: 人工重生成放行", r2.get("ok") is True, str(r2))
        await wait_job_terminal(r2["job_id"])
        await wait_scene_status(pid, sid, "done")
        check("指纹失配: 服务商共 2 次", STATE["calls"] == base + 2, f"calls={STATE['calls']}")
    finally:
        use_gated_writeback(False)


# ---------------- B. 调谐循环回归 ----------------

async def t_reconcile_exactly_once() -> None:
    """调谐循环：N 镜恰好 N 次提交（今日 14×2 事故回归）。"""
    pid, sids = make_project(3)
    base = STATE["calls"]
    summary = await reconcile.run_generate_stage(pid)
    check("调谐: 每镜恰好一次提交", STATE["calls"] == base + 3, f"calls={STATE['calls']}")
    check("调谐: summary 计数一致", summary.get("video") == 3, str(summary))
    check("调谐: 无 failed", not summary.get("failed"), str(summary.get("failed")))
    for sid in sids:
        s = scene_of(pid, sid)
        check(f"调谐: {sid[:14]} done", s["status"] == "done" and s["assets"]["video"], s["status"])


# ---------------- C. 已花钱产物必须能落回 ----------------

async def t_restart_unlanded_writeback() -> None:
    """重启落在「任务完成→写回」窗口：启动钩子把已付产物补写回分镜。"""
    use_gated_writeback(True, closed=True)
    try:
        pid, (sid,) = make_project(1)
        base = STATE["calls"]
        r = await generation_service.submit_scene_video(pid, sid, manual=True)
        await wait_job_terminal(r["job_id"])
        # 模拟重启：写回协程消亡 = 在途登记清空
        generation_service._pending_video_writebacks.clear()
        n = await job_recovery_mod.recover_interrupted_jobs()
        check("重启补写回: 恢复数量>=1", n >= 1, str(n))
        s = await wait_scene(pid, sid, lambda x: x["assets"]["video"], what="补写回落地")
        check("重启补写回: 状态 done", s["status"] == "done", s["status"])
        check("重启补写回: 恢复阶段服务商零新增调用", STATE["calls"] == base + 1, f"calls={STATE['calls']}")
    finally:
        use_gated_writeback(False)


async def t_repay_no_url_job() -> None:
    """云端 SUCCESS 但地址没取到（已计费）：重启后再试取回并写回。"""
    pid, (sid,) = make_project(1)
    s0 = scene_of(pid, sid)
    fp = generation_service.prompt_fingerprint(s0["prompt"])
    job = job_manager.create("video", "autodl", "minimax_h3_fake", {
        "project_id": pid, "scene_id": sid, "prompt_fingerprint": fp, "duration": 5,
    })
    job_manager.update(job["id"], provider_task_id="cloud-task-1", status="failed",
                       error="任务成功但未找到视频地址：{'status': 'SUCCESS'}")
    project_store.mutate(pid, lambda p: setattr(
        next(x for x in p.scenes if x.id == sid).assets, "video_job", job["id"]))

    class FakeProvider:
        async def poll(self, task_id):
            return ("success", "http://fake-cdn/v.mp4", None)

    class FakeFileService:
        @staticmethod
        async def download_to_outputs(kind, url):
            return "/api/files/outputs/video/repaid.mp4"

    orig_get_video = job_recovery_mod.registry.get_video
    orig_dl = job_recovery_mod.file_service.download_to_outputs
    job_recovery_mod.registry.get_video = lambda provider, cred: FakeProvider()
    job_recovery_mod.file_service.download_to_outputs = FakeFileService.download_to_outputs
    try:
        n = await job_recovery_mod.recover_interrupted_jobs()
        check("再取回: 恢复数量>=1", n >= 1, str(n))
        await wait_job_terminal(job["id"])
        s = await wait_scene(pid, sid, lambda x: x["assets"]["video"], what="再取回写回")
        check("再取回: 产物落镜", s["assets"]["video"] == "/api/files/outputs/video/repaid.mp4"
              and s["status"] == "done", str(s["assets"]))
    finally:
        job_recovery_mod.registry.get_video = orig_get_video
        job_recovery_mod.file_service.download_to_outputs = orig_dl


def t_provider_success_no_url_polls_on() -> None:
    """provider 层：云端 SUCCESS 但无地址 → 按 running 继续轮询，不判死扔钱。"""
    from app.providers.video import autodl_comfyui, generic_video

    class FakeResp:
        status_code = 200
        text = ""
        payload: dict = {}

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResp()

    async def run_one(label: str, provider, payload: dict) -> None:
        import httpx as _httpx

        FakeResp.payload = payload
        orig = _httpx.AsyncClient
        _httpx.AsyncClient = FakeClient
        try:
            st, url, err = await provider.poll("task-1")
        finally:
            _httpx.AsyncClient = orig
        check(f"provider {label}: 继续 running", st == "running" and url is None and err is None,
              f"st={st} url={url} err={err}")

    prov = autodl_comfyui.AutoDLComfyUI.__new__(autodl_comfyui.AutoDLComfyUI)
    prov.base_url, prov.group_id, prov.api_key = "http://fake", None, "k"
    prov2 = generic_video.GenericVideoProvider.__new__(generic_video.GenericVideoProvider)
    prov2.base_url, prov2.api_key = "http://fake", "k"

    async def run() -> None:
        await run_one("autodl SUCCESS 无地址", prov, {"data": {"status": "SUCCESS", "results": []}})
        await run_one("generic completed 无地址", prov2, {"status": "completed"})

    asyncio.run(run())


async def main() -> None:
    await t_race_window_intercepted()
    await t_concurrent_submit()
    await t_auto_ban_after_failure()
    await t_fingerprint_drift_blocked()
    await t_fingerprint_release_slot()
    await t_reconcile_exactly_once()
    await t_restart_unlanded_writeback()
    await t_repay_no_url_job()


if __name__ == "__main__":
    t_provider_success_no_url_polls_on()  # 独立事件循环，避免与主循环嵌套
    asyncio.run(main())
    print(f"ALL PASS ({PASS} checks)")
