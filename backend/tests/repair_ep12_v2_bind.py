"""聊皮肤第12集（project_68b98e4a）bind 修复 v3（用户 14:48 重跑后，旧后端进程所致）。

现状：14:48 用户清空重投（剧本+25图+10音频）并跑完 intake→…→bind→review(pause)，
但运行中的后端进程仍是修复前代码：V2 表不识别 → 图 0 绑定、音频仅镜01（互证炸+
台词兜底未命中）、gen_mode 全 t2v、prompt 为成稿直录（这次是干净的）。
动作（离线零 LLM，用已修复的 stages.py）：
1. 备份
2. 重跑 run_bind：V2 表对齐 → 图/音频按表绑定；缺图镜（3/9 空、7/10 部分——
   Image26-30 服务器上没有，属闯西游的图，用户需补传聊皮肤的）
3. 有图镜 gen_mode t2v→i2v（对齐修复后 plan 本应产出的值；缺图镜保持 t2v，
   防止调谐器抢生成关键帧）
4. 校验：绑定、音频、prompt 保真（成稿直录逐字不动）
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services import project_store  # noqa: E402
from app.services.pipeline import stages  # noqa: E402

PROJECT = "project_68b98e4a"
# V3 稿（14:48 重投）：镜i 绑 Image(2i+3)/Image(2i+4) + Audio i（Image05-24 全在池）
EXPECT_IMAGES = {i: [f"image{2 * i + 3}", f"image{2 * i + 4}"] for i in range(10)}


class _NoLLUEnvelope:
    async def structured_call(self, *_a, **_k):
        raise RuntimeError("LLM 不应被触发：V2 对齐应全覆盖")


def fname_key(filename: str) -> str:
    return (filename or "").rsplit("_", 1)[-1].lower().rsplit(".", 1)[0]


def main() -> None:
    raw_path = Path(project_store._path(PROJECT))
    backup = settings.DATA_DIR / "backups" / f"{PROJECT}.pre_v2fix3.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_path, backup)
    print(f"备份：{backup}")
    prompts_before = {s["id"]: s["prompt"] for s in json.loads(
        raw_path.read_text(encoding="utf-8"))["scenes"]}

    r = asyncio.run(stages.run_bind(PROJECT, _NoLLUEnvelope(), trace=None))
    print("run_bind:", json.dumps(
        {k: r.get(k) for k in ("ok", "bound", "pending", "reference_aligned_scenes")},
        ensure_ascii=False))

    def fix_gen_mode(p) -> None:
        for s in p.scenes:
            if s.binding.images:
                s.gen_mode = "i2v"
    project_store.mutate(PROJECT, fix_gen_mode)

    d = json.loads(raw_path.read_text(encoding="utf-8"))
    mats = {m["id"]: m for m in d["materials"]}
    beats = (d.get("screenplay") or {}).get("beats") or []
    fails = 0
    for i, s in enumerate(d["scenes"]):
        got = [fname_key(mats[b["material_id"]]["filename"]) for b in s["binding"]["images"]]
        ga = [mats[mid]["filename"] for mid in s["binding"]["audios"]]
        ga_key = [(f.split("tts_")[1][:6] if "tts_" in f else f) for f in ga]
        ok_prompt = prompts_before[s["id"]] == s["prompt"]
        ok = got == EXPECT_IMAGES[i] and len(ga) == 1 and ga_key[0] == f"{i + 1:02d}" and ok_prompt
        fails += 0 if ok else 1
        print(f" {'✓' if ok else '✗'} 镜{i + 1:02d} {s['title'][:14]:16s} 图={got} 音={ga_key} "
              f"gen={s['gen_mode']} prompt保真={ok_prompt}")
    print(f"pending={len(d.get('pending_bindings') or [])}；缺图待用户补传：镜3/9 全缺，"
          f"镜7 缺第2张，镜10 缺第1/2张（Image26-30）")
    if fails:
        print(f"校验失败 {fails} 镜")
        sys.exit(1)
    print("全部校验通过")


if __name__ == "__main__":
    main()
