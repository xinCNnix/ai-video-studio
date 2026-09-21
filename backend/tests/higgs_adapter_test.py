"""higgs_adapter_test：Higgs 本地 TTS 适配器自检（隔离 DATA_DIR，不跑模型、不联网）。

覆盖：emotion/speed → Higgs 内联标签映射 / 长文本按句切分（自回归友好） /
音色资产解析（带 ref_text 样本 → 克隆；缺转写报错；未命中 → 零样本） /
registry 路由（provider=higgs 免凭据直达适配器）。
运行：.venv/Scripts/python.exe tests/higgs_adapter_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="higgs_adapter_test_")

from app.config import voice_assets  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.providers.tts.higgs_local import (  # noqa: E402
    HiggsLocalTTS, map_emotion, map_speed, split_for_ar, _prefill_tags, _resolve_voice,
)

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


# ---------------- emotion / speed 映射 ----------------
check("neutral → 无标签", map_emotion("neutral") == "")
check("happy → elation", map_emotion("happy") == "elation")
check("fearful → fear", map_emotion("fearful") == "fear")
check("原始标签透传", map_emotion("disgust") == "disgust")
check("未知情感 → 无标签", map_emotion("excited") == "")
check("speed 1.0 → 无标签", map_speed(1.0) == "")
check("speed 1.3 → speed_fast", map_speed(1.3) == "speed_fast")
check("speed 0.7 → speed_slow", map_speed(0.7) == "speed_slow")
check("speed 1.6 → speed_very_fast", map_speed(1.6) == "speed_very_fast")
check("组合标签：情感+语速", _prefill_tags("happy", 0.6) == "<|emotion:elation|><|prosody:speed_slow|>")

# ---------------- 长文本切分 ----------------
check("短文本单段", split_for_ar("你好。") == ["你好。"])
long_text = "。".join(f"第{i}句话" for i in range(60)) + "。"
chunks = split_for_ar(long_text)
check("长文本多段且全覆盖", len(chunks) > 1 and "".join(chunks) == long_text, f"{len(chunks)}段")
check("每段不超上限", all(len(c) <= 150 for c in chunks))
no_punct = "字" * 300
hard = split_for_ar(no_punct)
check("无标点硬切", len(hard) >= 2 and "".join(hard) == no_punct)
check("空文本 → 空列表", split_for_ar("  \n ") == [])

# ---------------- 音色资产解析 ----------------
sample = Path(os.environ["DATA_DIR"]) / "ref.wav"
sample.write_bytes(b"RIFF" + b"\x00" * 100)
a1 = voice_assets.create_asset("测试音色", "clone", sample_src=sample, ref_text="这是样本逐字稿")
path, rt = _resolve_voice(a1["id"])
check("命中资产 → 克隆参数", path is not None and rt == "这是样本逐字稿")
a2 = voice_assets.create_asset("无转写音色", "clone", sample_src=sample)
try:
    _resolve_voice(a2["id"])
    check("缺 ref_text → ProviderError", False)
except ProviderError as exc:
    check("缺 ref_text → ProviderError", "ref_text" in str(exc))
check("未命中/default → 零样本", _resolve_voice("default") == (None, None) and _resolve_voice("随便填") == (None, None))

# ---------------- registry 路由 ----------------
tts = registry.get_tts(None, "higgs")
check("provider=higgs 免凭据直达适配器", isinstance(tts, HiggsLocalTTS))
check("凭据标签显示本地模型", registry.resolve_credential_label(None, "higgs") == "Higgs 本地模型（无需 Key）")

print(f"\n{PASS} 项全部通过")
