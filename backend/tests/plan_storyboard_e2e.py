"""真实 LLM 端到端验证：用户 4 分镜剧本 → normalize+plan → 必须恰好 4 镜（H3 结构）。

复现线上问题：4 分镜剧本被 plan 扩成 16 镜。本脚本用真实凭据跑 normalize+plan，
断言 storyboard 类型判定、拍数=镜数、prompt 含 H3 关键结构（Action Timeline）。
数据隔离：DATA_DIR 指向临时目录（拷贝凭据），不碰真实项目。
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TEST_DATA = BACKEND.parent / "repro" / "test_data_storyboard"
REAL_CONFIG = BACKEND.parent / "data" / "config"

# 数据隔离：必须在导入 app 模块之前设置 DATA_DIR
if TEST_DATA.exists():
    shutil.rmtree(TEST_DATA)
(TEST_DATA / "config").mkdir(parents=True)
for name in ("providers.enc", "secret.key", "custom_models.json", "app_settings.json"):
    src = REAL_CONFIG / name
    if src.exists():
        shutil.copy(src, TEST_DATA / "config" / name)
os.environ["DATA_DIR"] = str(TEST_DATA)

from app.services import project_store  # noqa: E402
from app.services.pipeline import stages  # noqa: E402
from app.services.pipeline.llm_client import LLMEnvelope  # noqa: E402


class PrintTrace:
    def log(self, event, **kw):
        print(f"  [{event}] " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def count(self, key, n=1):
        pass


async def main() -> int:
    script = (BACKEND.parent / "repro" / "user_script_ep6.md").read_text(encoding="utf-8")
    proj = project_store.create("storyboard保真-e2e", aspect_ratio="9:16")
    pid = proj["id"]
    stages.run_intake(pid, {"script_text": script})

    from app.providers.registry import resolve_llm_call
    llm, model = resolve_llm_call(None, provider_hint="minimax")
    print(f"LLM 模型：{model}")
    envelope = LLMEnvelope(llm, model, trace=PrintTrace())

    r1 = await stages.run_normalize(pid, envelope)
    print("normalize:", r1)
    r2 = await stages.run_plan(pid, envelope)
    print("plan:", {k: v for k, v in r2.items() if k != "ok"})

    p = project_store.get(pid)
    sp = p["screenplay"]
    scenes = p["scenes"]
    failures = []
    if sp["script_type"] != "storyboard":
        failures.append(f"script_type={sp['script_type']}，期望 storyboard")
    if len(sp["beats"]) != 4:
        failures.append(f"beats={len(sp['beats'])}，期望 4")
    if len(scenes) != 4:
        failures.append(f"scenes={len(scenes)}，期望 4")
    for i, s in enumerate(scenes):
        if "Action Timeline" not in s["prompt"]:
            failures.append(f"镜{i + 1} prompt 缺 Action Timeline")
        if len(s["prompt"]) < 100:
            failures.append(f"镜{i + 1} prompt 过短（{len(s['prompt'])} 字）")

    print("\n===== 分镜结果 =====")
    for i, s in enumerate(scenes):
        print(f"{i + 1}. [{s['title']}] {s['duration']}s {s['gen_mode']}")
        print(f"   {s['description'][:80]}")
        print(f"   prompt 头 150 字：{s['prompt'][:150]}")

    if failures:
        print("\n失败：")
        for f in failures:
            print(" -", f)
        return 1
    print("\n通过：storyboard 判定正确，4 拍 → 4 镜，H3 结构齐全")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
