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
    """structured_call 按用户消息应答：普通调用回首轮预案，带【数量错误】回纠错案；
    director 润色遍（PolishedPrompt schema）回达标 canned 六段式。

    storyboard 分窗后重试有两级（窗口级 + 全稿闸门级），调用次数由消息驱动。"""

    POLISH_OK = (
        "subject_definitions:\n<Subject 1> is a stylized 3D character.\n\n"
        "summary:\n[reference generation] One continuous shot.\n\n"
        "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - ok.\n\n"
        "detailed_description:\n[Shot 1] "
        + "The camera moves forward at slow speed while the subject walks ahead steadily. " * 40
        + "\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music: N/A"
    )

    def __init__(self, first_plan, retry_plan=None):
        self.first_plan = first_plan
        self.retry_plan = retry_plan if retry_plan is not None else first_plan
        self.calls = 0

    async def structured_call(self, system, user, schema, **kw):
        self.calls += 1
        if schema.__name__ == "PolishedPrompt":
            return schema.model_validate({"prompt": self.POLISH_OK})
        from app.services.pipeline.schemas import PlanBatch
        return PlanBatch.model_validate(self.retry_plan if "数量错误" in user else self.first_plan)


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
    fake = FakeEnvelope(
        {"shots": [_shot(i) for i in range(16)], "total": 16, "done": True},
        {"shots": [_shot(i) for i in range(4)], "total": 4, "done": True},
    )
    r = await stages.run_plan(pid, fake)
    p = project_store.get(pid)
    assert len(p["scenes"]) == 4, f"场景1失败：{len(p['scenes'])} 镜"
    # 2 次规划调用（首轮 + 窗口纠错）+ 4 次导演润色遍（每镜一次专注调用）
    assert fake.calls == 6, f"场景1失败：应调用 6 轮（规划2+润色4），实际 {fake.calls}"
    print("ok  场景1：16 镜触发纠错重规划，二轮 4 镜通过，润色遍逐镜执行，已落盘 4 镜")

    # 场景2：永远 16 镜 → FatalError 且不落盘
    pid2 = make_project()
    always_16 = {"shots": [_shot(i) for i in range(16)], "total": 16, "done": True}
    fake2 = FakeEnvelope(always_16)
    try:
        await stages.run_plan(pid2, fake2)
        print("FAIL 场景2：应抛 FatalError")
        return 1
    except FatalError as e:
        assert "保真校验失败" in str(e), f"场景2异常信息不符：{e}"
    p2 = project_store.get(pid2)
    assert not p2["scenes"], f"场景2失败：失败结果不应落盘，实际 {len(p2['scenes'])} 镜"
    # 每轮全稿规划 = 窗口首调 + 窗口纠错重试共 2 次调用；全稿闸门最多再规划 PLAN_GUARD_RETRIES 轮
    expected_calls = 2 * (1 + stages.PLAN_GUARD_RETRIES)
    assert fake2.calls == expected_calls, f"场景2失败：应调用 {expected_calls} 轮，实际 {fake2.calls}"
    print(f"ok  场景2：连续 {expected_calls} 次仍 16 镜 → FatalError，未落盘任何分镜")

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
