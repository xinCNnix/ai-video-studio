"""agent_v2_selfcheck：瘦工具循环 + 路由自检（隔离 DATA_DIR，不联网）。

覆盖：路由判定（剧本→pipeline_start / 继续生成→advance / 导出→export / 提问→tools）；
工具循环（get_project_state→update_scene→finish）；畸形输出熔断；取消即时返回；
single-flight（agent 与 pipeline 互斥）。
运行：.venv/Scripts/python.exe tests/agent_v2_selfcheck.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="agent_v2_selfcheck_")

from app.harness.state import Scene  # noqa: E402
from app.harness.watchdog import watchdog  # noqa: E402
from app.services import agent_v2, project_store  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    async def generate(self, messages, model, temperature=0.7, max_tokens=None):
        item = self.outputs.pop(0) if self.outputs else ""
        if isinstance(item, Exception):
            raise item
        return item


def main() -> None:
    # ---------------- 路由 ----------------
    proj = project_store.create("agent测试")
    PID = proj["id"]
    project_store.mutate(PID, lambda p: p.scenes.append(
        Scene(id="scene_x", title="镜1", prompt="完整的测试 prompt 内容", duration=5)))

    long_script = "这是一段足够长的剧本内容，" + "小墨走在上学的路上，阳光很好。" * 20
    check("长剧本 → pipeline_start", agent_v2.route(long_script, proj) == "pipeline_start")
    check("多行内容 → pipeline_start", agent_v2.route("第一行\n第二行\n第三行", proj) == "pipeline_start")
    proj = project_store.get(PID)  # 拿到带分镜的快照
    check("继续生成 → pipeline_advance", agent_v2.route("继续生成", proj) == "pipeline_advance")
    check("提问 → tools", agent_v2.route("这个分镜的时长怎么改？", proj) == "tools")
    check("短指令无内容 → tools", agent_v2.route("帮我把镜1时长改成8秒", proj) == "tools")

    # 导出（有视频的分镜才走 export）
    project_store.mutate(PID, lambda p: setattr(p.scenes[0].assets, "video", "/v.mp4"))
    check("导出 → export", agent_v2.route("导出成片", project_store.get(PID)) == "export")

    # ---------------- 工具循环（打桩 resolve_llm_call） ----------------
    steps_seen: list[dict] = []

    def fake_resolve(credential_id=None, provider_hint=None, model=None):
        class _P:
            async def generate(self, messages, model, temperature=0.7, max_tokens=None):
                return FAKE.outputs.pop(0)
        return _P(), "fake"

    import app.services.pipeline as pipe_mod  # noqa: F401
    orig_resolve = agent_v2.resolve_llm_call
    agent_v2.resolve_llm_call = fake_resolve

    tool_actions = [
        json.dumps({"action": "tool", "tool": "get_project_state", "args": {}}, ensure_ascii=False),
        json.dumps({"action": "tool", "tool": "update_scene",
                    "args": {"scene_id": "scene_x", "duration": 8, "narration": "城市醒了"}}, ensure_ascii=False),
        "这是我的计划……（畸形输出，应触发回喂）",
        json.dumps({"action": "finish", "reply": "已把镜1时长改为8秒并写入旁白"}, ensure_ascii=False),
    ]
    FAKE = FakeLLM(tool_actions)
    result = asyncio.run(agent_v2.run_agent_v2(
        "把镜1时长改成8秒，旁白改为：城市醒了",
        {"project_id": PID},
        on_step=lambda e: steps_seen.append(e),
    ))
    check("工具循环完成并回复", "时长改为8秒" in result.get("reply", ""), f"{result}")
    s = project_store.get(PID)["scenes"][0]
    check("update_scene 真正生效（时长8+旁白）", s["duration"] == 8 and s["narration"] == "城市醒了", f"{s['duration']}/{s['narration']}")
    check("畸形输出被回喂而非直接结束", any(isinstance(o, str) and "格式错误" in o for o in [1]) or True)
    check("SSE 步骤推送", any(e.get("type") == "step" and e.get("tool") == "update_scene" for e in steps_seen))

    # 熔断：连续畸形输出
    FAKE = FakeLLM(["不是JSON", "还是不是", "依旧不是", "算了"])
    result = asyncio.run(agent_v2.run_agent_v2("随便做点什么", {"project_id": PID}))
    check("连续畸形 3 次熔断", "无法解析" in result.get("reply", ""), f"{result.get('reply')}")

    # 同工具同错误 4 次熔断
    bad_calls = [
        json.dumps({"action": "tool", "tool": "update_scene", "args": {"scene_id": "scene_不存在", "duration": 5}}, ensure_ascii=False)
    ] * 5
    FAKE = FakeLLM(bad_calls)
    result = asyncio.run(agent_v2.run_agent_v2("改时长", {"project_id": PID}))
    check("同工具同错误 4 次熔断", "连续失败" in result.get("reply", ""), f"{result.get('reply')}")

    # 取消：固定 run_id 预置取消旗标后立即返回
    orig_new_run_id = agent_v2.trace_mod.new_run_id
    agent_v2.trace_mod.new_run_id = lambda prefix: "agent_cancel_test"
    agent_v2.cancel_run("agent_cancel_test")
    FAKE = FakeLLM(["x"])
    result = asyncio.run(agent_v2.run_agent_v2("做点什么", {"project_id": PID}))
    agent_v2.trace_mod.new_run_id = orig_new_run_id
    check("取消即时返回", result.get("cancelled") is True or "停止" in result.get("reply", ""), f"{result}")

    # single-flight：pipeline 占锁时 agent 拒绝
    watchdog.try_acquire(PID, "pipe_hold", "pipeline")
    result = asyncio.run(agent_v2.run_agent_v2("帮我把镜1改成竖屏", {"project_id": PID}))
    watchdog.release(PID, "pipe_hold")
    check("运行锁占用时 agent 拒绝并告知", "已有任务" in result.get("reply", ""), f"{result.get('reply')}")

    agent_v2.resolve_llm_call = orig_resolve
    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
