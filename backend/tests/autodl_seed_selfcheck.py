"""autodl_seed_selfcheck：AutoDL 适配器 seed 随机化 + 元数据留痕离线自测。

背景：AutoDL 工作流的 seed 固化在云端工作流 JSON 里，请求不传则每次都用同一个
默认种子——同参数重提产出逐帧相同的视频（抽卡第二次和第一次一模一样）。
本测试锁定修复后的不变量：

  A. 契约允许 seed 且未显式提供：注入随机 int 种子，两次构造互不相同
  B. 显式提供数值 seed：原样透传（可复现）；字符串数字转 int
  C. 显式提供非数值 seed：回退随机
  D. 契约不含 seed 的工作流（minimax_h3_lightx2v_no_pic）：绝不携带 seed
  E. resolve_seed：AutoDL+seed 契约随机/显式尊重，其他厂商与无 seed 契约返回 None
  F. 提交接线：job 元数据记录的 seed == options 实际下发的 seed，重抽互不相同

运行：.venv/Scripts/python.exe tests/autodl_seed_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="autodl_seed_check_")

from app.config import settings  # noqa: E402
from app.providers.video.autodl_comfyui import _build_body  # noqa: E402
from app.services import video_service  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


SPEC = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_zm_u24"]
NO_SEED_SPEC = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_lightx2v_no_pic"]


def t_random_when_absent() -> None:
    body1 = _build_body({"prompt": "p"}, SPEC)
    body2 = _build_body({"prompt": "p"}, SPEC)
    s1, s2 = body1.get("seed"), body2.get("seed")
    check("随机: 未提供时注入 int", isinstance(s1, int) and 0 <= s1 <= 2**31 - 1, str(s1))
    check("随机: 两次构造互不相同", s1 != s2, f"{s1} vs {s2}")
    body = _build_body({"prompt": "p", "options": {}}, SPEC)
    check("随机: 空 options 同样注入", isinstance(body.get("seed"), int), str(body.get("seed")))


def t_explicit_respected() -> None:
    body = _build_body({"prompt": "p", "options": {"seed": 42}}, SPEC)
    check("透传: 数值 seed 原样保留", body.get("seed") == 42, str(body.get("seed")))
    body = _build_body({"prompt": "p", "options": {"seed": "123"}}, SPEC)
    check("透传: 字符串数字转 int", body.get("seed") == 123, str(body.get("seed")))
    body = _build_body({"prompt": "p", "options": {"seed": "abc"}}, SPEC)
    check("回退: 非数值 seed 转随机", isinstance(body.get("seed"), int) and body.get("seed") != "abc",
          str(body.get("seed")))


def t_no_seed_contract() -> None:
    body = _build_body({"prompt": "p"}, NO_SEED_SPEC)
    check("契约: 无 seed 工作流不携带", "seed" not in body, str(sorted(body)))


# ---------------- E. resolve_seed（元数据留痕的唯一决定点） ----------------

def t_resolve_seed() -> None:
    s = video_service.resolve_seed("autodl", "minimax_h3_zm_u24", None)
    check("决策: 随机返回 int", isinstance(s, int) and 0 <= s <= 2**31 - 1, str(s))
    check("决策: 显式数值尊重", video_service.resolve_seed("autodl", "minimax_h3_zm_u24", {"seed": 42}) == 42, "")
    check("决策: 字符串数字转 int", video_service.resolve_seed("autodl", "minimax_h3_zm_u24", {"seed": "7"}) == 7, "")
    s = video_service.resolve_seed("autodl", "minimax_h3_zm_u24", {"seed": "abc"})
    check("决策: 非数值回退随机", isinstance(s, int), str(s))
    check("决策: 非基 AutoDL 厂商无 seed",
          video_service.resolve_seed("minimax", "MiniMax-H3", {}) is None
          and video_service.resolve_seed("seedance", "doubao-seedance-2-0", {"seed": 1}) is None, "")
    check("决策: 无 seed 契约工作流返回 None",
          video_service.resolve_seed("autodl", "minimax_h3_lightx2v_no_pic", {}) is None, "")


# ---------------- F. 提交接线（元数据 seed == 实际下发 seed） ----------------

CAPTURE: dict = {}


async def fake_run_video_job(job_id: str, **kwargs) -> None:
    CAPTURE["calls"] = CAPTURE.get("calls", 0) + 1
    CAPTURE["options"] = kwargs.get("options")
    from app.jobs import job_manager
    job_manager.update(job_id, status="failed", error="fake stop（接线测试桩）")


async def wait_capture(n: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if CAPTURE.get("calls", 0) >= n:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"run_video_job 桩未被调用 {n} 次")


async def t_submit_wiring() -> None:
    from app.harness.state import ImageBinding, Material, Scene, SceneBinding
    from app.jobs import job_manager
    from app.services import generation_service, project_store

    video_service.run_video_job = fake_run_video_job
    generation_service.VIDEO_POLL_INTERVAL = 0.01

    p = project_store.create("seed接线测试", "9:16")
    pid, sid = p["id"], f"scene_1_{uuid.uuid4().hex[:6]}"

    def add(pobj) -> None:
        pobj.scenes.append(Scene(
            id=sid, title="镜1",
            prompt="A cinematic mystery shot, restrained realistic style.",
            duration=5, gen_mode="i2v",
            binding=SceneBinding(images=[ImageBinding(material_id="mat_img", role="reference")]),
        ))
        pobj.materials.append(Material(id="mat_img", kind="image", url="/api/files/uploads/ref.png"))

    project_store.mutate(pid, add)

    seeds = []
    for i in range(2):  # 重抽一次：两次 seed 必须不同（manual 绕过自动链路单次闸门）
        r = await generation_service.submit_scene_video(pid, sid, manual=True)
        check(f"接线#{i + 1}: 提交成功", r.get("ok") is True, str(r))
        await wait_capture(i + 1)
        job = job_manager.get(r["job_id"])
        seed = (job.get("params") or {}).get("seed")
        check(f"接线#{i + 1}: job 元数据记录 int seed",
              isinstance(seed, int) and 0 <= seed <= 2**31 - 1, str(job.get("params")))
        check(f"接线#{i + 1}: 元数据 seed == 实际下发 seed",
              (CAPTURE.get("options") or {}).get("seed") == seed, str(CAPTURE.get("options")))
        seeds.append(seed)
    check("接线: 重抽 seed 互不相同", seeds[0] != seeds[1], str(seeds))


if __name__ == "__main__":
    t_random_when_absent()
    t_explicit_respected()
    t_no_seed_contract()
    t_resolve_seed()
    asyncio.run(t_submit_wiring())
    print(f"ALL PASS ({PASS} checks)")
