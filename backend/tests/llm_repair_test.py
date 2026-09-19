"""离线验证 llm_client JSON 修复链：未转义英文双引号 / 字面量换行 / 围栏剥离。

复现线上问题：normalize 阶段模型在 logline 里输出 围绕"白斑边缘变清楚"这一 ——
字符串值内的 ASCII 引号让 json.loads 提前断串，连续 3 轮修复失败后整跑中止。
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TEST_DATA = BACKEND.parent / "repro" / "test_data_repair"
if TEST_DATA.exists():
    shutil.rmtree(TEST_DATA)
TEST_DATA.mkdir(parents=True)
os.environ["DATA_DIR"] = str(TEST_DATA)

from app.harness.errors import FatalError  # noqa: E402
from app.services.pipeline.llm_client import (  # noqa: E402
    LLMEnvelope,
    parse_json_candidates,
    repair_unescaped_quotes,
)
from app.services.pipeline.schemas import Screenplay  # noqa: E402

# 用户报错样本的还原：字符串值里夹 ASCII 双引号（logline 与 beats 都有）
BROKEN_SCREENPLAY = (
    '{"script_type":"storyboard",'
    '"logline":"在3D卡通科普空间中，皮肤科普讲解员吕满春与助手小墨围绕"白斑边缘变清楚"'
    '这一观察现象展开讲解，指出边缘变清楚只是一个观察线索，不能直接等同于病情稳定。",'
    '"characters":[{"name":"吕满春","design":"3D卡通女性形象，穿白大褂"},{"name":"小墨","design":"墨滴形象"}],'
    '"beats":[{"title":"开场","description":"讲解员说"大家好"，开始讲解","narration":"大家好","source_hint":"原文"}],'
    '"notes":""}'
)


class FakeLLM:
    """generate 永远返回预置文本（模拟固执的坏输出）。"""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def generate(self, messages, model, **kw):
        self.calls += 1
        return self.text


def test_repair_function() -> None:
    # 内容引号转义、终结符保留
    src = '{"a":"围绕"X"这一"}'
    fixed = repair_unescaped_quotes(src)
    assert json.loads(fixed)["a"] == '围绕"X"这一', fixed
    # 合法 JSON（含已转义引号、值后带空白）原样保留，幂等
    for ok in ('{"a":"He said \\"hi\\" ok"}', '{"a":"v" , "b":1}', '{"a":"b"}'):
        assert repair_unescaped_quotes(ok) == ok, ok
    # 键后冒号前字符串、嵌套结构里的内容引号
    nested = '{"k":"v","list":[{"t":"他说"好"然后走了"}],"n":2}'
    assert json.loads(repair_unescaped_quotes(nested))["list"][0]["t"] == '他说"好"然后走了'
    print("ok  repair_unescaped_quotes：内容引号转义、合法输入不变、幂等")


def test_parse_paths() -> None:
    env = LLMEnvelope(None, "fake")
    parsed = env._parse(BROKEN_SCREENPLAY)
    assert isinstance(parsed, dict) and parsed["script_type"] == "storyboard", parsed
    sp = Screenplay.model_validate(parsed)
    assert len(sp.beats) == 1 and sp.logline.startswith("在3D卡通科普空间")
    # 围栏 + 前后夹杂说明文字
    fenced = f"好的，以下是解析结果：\n```json\n{BROKEN_SCREENPLAY}\n```\n以上。"
    cands = parse_json_candidates(fenced)
    assert cands and cands[-1]["script_type"] == "storyboard", cands
    # 真实换行 + 引号叠加
    mixed = '{"logline":"第一行\n围绕"白斑"继续","beats":[{"title":"t"}]}'
    assert env._parse(mixed)["logline"].startswith("第一行")
    print("ok  _parse / parse_json_candidates：用户样本、围栏夹杂、换行+引号叠加全部可解析")


async def test_structured_call() -> None:
    # 坏输出一次命中：修复器兜住，不进修复轮
    fake = FakeLLM(f"```json\n{BROKEN_SCREENPLAY}\n```")
    env = LLMEnvelope(fake, "fake")
    sp = await env.structured_call("sys", "user", Screenplay)
    assert fake.calls == 1 and sp.script_type == "storyboard"
    # 真垃圾输出：错误路径不回归（3 轮后 FatalError）
    fake2 = FakeLLM("这不是 JSON，我不输出。")
    env2 = LLMEnvelope(fake2, "fake")
    try:
        await env2.structured_call("sys", "user", Screenplay)
        raise AssertionError("应抛 FatalError")
    except FatalError as e:
        assert "Screenplay" in str(e) and fake2.calls == 3, (str(e), fake2.calls)
    print("ok  structured_call：坏样本 1 次通过；真垃圾仍 3 轮后 FatalError")


async def main() -> int:
    test_repair_function()
    test_parse_paths()
    await test_structured_call()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
