"""离线验证 storyboard 保真闸门：不依赖真实 LLM，用假信封驱动 run_plan。

场景1：首轮 16 镜 → 纠错重试后 4 镜 → 通过
场景2：永远 16 镜 → 重试 2 轮后 FatalError，且不落盘
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TEST_DATA = BACKEND.parent / "repro" / "test_data_guard"
if TEST_DATA.exists():
    shutil.rmtree(TEST_DATA)
TEST_DATA.mkdir(parents=True)
os.environ["DATA_DIR"] = str(TEST_DATA)

from app.harness.errors import FatalError  # noqa: E402
from app.services import project_store  # noqa: E402
from app.services.pipeline import stages  # noqa: E402


class FakeEnvelope:
    """structured_call 返回预置的 PlanBatch dict 序列。"""

    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = 0

    async def structured_call(self, system, user, schema, **kw):
        from app.services.pipeline.schemas import PlanBatch
        self.calls += 1
        item = self.plans.pop(0)
        return PlanBatch.model_validate(item)


def _shot(n):
    return {
        "title": f"镜{n}", "description": f"画面{n}", "prompt": f"prompt {n} " * 20 + "## Action Timeline",
        "keyframe_prompt": f"关键帧{n}", "narration": "", "duration": 5,
        "gen_mode": "i2v", "wants_images": [], "wants_audios": [], "wants_videos": [],
    }


def make_project():
    proj = project_store.create("guard-test")
    pid = proj["id"]

    def fn(p):
        p.screenplay = {
            "script_type": "storyboard",
            "beats": [{"title": f"拍{i}", "description": f"画面{i}", "narration": ""} for i in range(4)],
            "characters": [],
        }
    project_store.mutate(pid, fn)
    return pid


async def main() -> int:
    # 场景1：16 镜 → 纠错后 4 镜
    pid = make_project()
    fake = FakeEnvelope([
        {"shots": [_shot(i) for i in range(16)], "total": 16, "done": True},
        {"shots": [_shot(i) for i in range(4)], "total": 4, "done": True},
    ])
    r = await stages.run_plan(pid, fake)
    p = project_store.get(pid)
    assert len(p["scenes"]) == 4, f"场景1失败：{len(p['scenes'])} 镜"
    assert fake.calls == 2, f"场景1失败：应调用 2 轮（首轮+纠错），实际 {fake.calls}"
    print("ok  场景1：16 镜触发纠错重规划，二轮 4 镜通过，已落盘 4 镜")

    # 场景2：永远 16 镜 → FatalError 且不落盘
    pid2 = make_project()
    fake2 = FakeEnvelope([
        {"shots": [_shot(i) for i in range(16)], "total": 16, "done": True}
    ] * (1 + stages.PLAN_GUARD_RETRIES))
    try:
        await stages.run_plan(pid2, fake2)
        print("FAIL 场景2：应抛 FatalError")
        return 1
    except FatalError as e:
        assert "保真校验失败" in str(e), f"场景2异常信息不符：{e}"
    p2 = project_store.get(pid2)
    assert not p2["scenes"], f"场景2失败：失败结果不应落盘，实际 {len(p2['scenes'])} 镜"
    expected_calls = 1 + stages.PLAN_GUARD_RETRIES
    assert fake2.calls == expected_calls, f"场景2失败：应调用 {expected_calls} 轮，实际 {fake2.calls}"
    print(f"ok  场景2：连续 {expected_calls} 轮 16 镜 → FatalError，未落盘任何分镜")

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
