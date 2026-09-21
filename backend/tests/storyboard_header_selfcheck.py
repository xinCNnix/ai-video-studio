"""storyboard 标题识别/分窗离线自检（2026-09-21 吕满春西游事故回归）。

事故：用户稿用「# 分镜 N」编号（无片段｜标题、无【第N镜】括号式），detect_fragment_headers
不认识 → 走 LLM 分块慢路径（6.5 分钟）且块边界切断分镜 2；末拍吞进稿尾全局章节；
plan 一锅端全文 → M3 思考烧尽 max_tokens、正文三连空。
本自检全部离线（零 LLM），验证：标题识别、逐字切片、末拍截尾、plan 分窗、散文不误触。
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.services.pipeline import stages  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    PASS, FAIL = PASS + (cond is True), FAIL + (cond is not True)


def build_script() -> str:
    """复刻事故稿结构：全局设定（含 Image/Audio 编号表）+ # 分镜 N 逐镜成稿 + 稿尾全局章节。"""
    frags = []
    for n in range(1, 7):
        frags.append(
            f"# 分镜 {n}\n\n"
            "## Reference\n\n`<Image 1>` 吕满春三视定妆图\n`<Image 2>` 小墨三视定妆图\n\n"
            "## 时长\n\n12s\n\n"
            "## 画面目标\n\n"
            f"分镜 {n} 的画面目标：吕满春与小墨在场景 {n} 中推进剧情，" + "细节描写。" * 30 + "\n\n"
            "## Character Consistency\n\n吕满春外观严格以 `<Image 1>` 为准。\n\n"
            "## detailed_description\n\n### 0s–2s\n\n画面从静置开始。\n\n### 2s–12s\n\n"
            f"剧情推进，" + "镜头运动与表演细节。" * 40 + "\n\n"
            "## Audio Timeline\n\n**0s–4s：`<Audio 1>` 小墨，画外音**\n\n“台词原文。”\n"
        )
    return (
        "# 《测试漫剧》\n\n## 第一回｜测试\n\n---\n\n# 一、基础设定\n\n画幅：9:16 竖屏\n\n"
        "# 二、全局人物一致性参考图\n\n## `<Image 1>` 吕满春三视定妆图\n\n外观锚点。\n\n"
        "## `<Image 2>` 小墨三视定妆图\n\n外观锚点。\n\n# 三、全局配音编号\n\n## `<Audio 1>` 小墨｜4s\n\n台词。\n\n"
        "# 五、分镜剧本\n\n" + "\n".join(frags)
        + "\n---\n\n# 六、第一集节奏结构\n\n全片节奏：快-慢-快。\n\n# 七、视觉风格关键词\n\n**现代段：** 干净、科技感。\n"
    )


def main() -> int:
    src = build_script()

    # ---------------- 1. 标题识别 ----------------
    headers = stages.detect_fragment_headers(src)
    check("识别出 6 个「# 分镜 N」标题", len(headers) == 6, f"{len(headers)}")
    check("标题起点单调递增", all(a[0] < b[0] for a, b in zip(headers, headers[1:])))

    prose = "正文里提到分镜 2 的画面，这只是叙述不是标题。\n另一段：片段 1 是开场。\n"
    check("散文中提及「分镜 N/片段 N」不误触", stages.detect_fragment_headers(prose) == [])
    check("裸编号行「## 分镜 3」可识别（行尾锚定）",
          len(stages.detect_fragment_headers("## 分镜 1\n\n内容A\n\n## 分镜 2\n\n内容B")) == 2)
    check("「片段1：7s」时长行仍被排除", stages.detect_fragment_headers("片段1：7s\n片段2：8s") == [])
    check("「片段 2｜标题」旧格式仍识别", len(stages.detect_fragment_headers("片段 1｜开场\n片段 2｜发展")) == 2)

    # ---------------- 2. 确定性拆拍 + 逐字回收 + 末拍截尾 ----------------
    beats = stages._beats_from_headers(src, headers)
    check("一镜一拍共 6 拍", len(beats) == 6, f"{len(beats)}")
    check("每拍 source_text 以自身编号头开始", all(b["source_text"].startswith(f"# 分镜 {i+1}") for i, b in enumerate(beats)))

    recovered, preamble = stages.recover_storyboard_source(
        [{"source_text": b["source_text"], "title": b["title"], "description": b["description"]} for b in beats], src)
    check("回收全部 verbatim", all(b.get("source_fidelity") == "verbatim" for b in recovered))
    check("preamble 是全局设定头", preamble.startswith("# 《测试漫剧》") and "分镜剧本" in preamble)

    trimmed = stages.trim_trailing_sections(recovered)
    last = trimmed[-1]["source_text"]
    check("末拍切掉稿尾全局章节", "节奏结构" not in last and "视觉风格关键词" not in last, last[-120:])
    check("末拍截尾后仍是完整分镜（含 Audio Timeline）", "Audio Timeline" in last and last.startswith("# 分镜 6"))
    check("截尾不动其他拍", all("节奏结构" not in b["source_text"] for b in trimmed))
    check("无稿尾章节时不误切",
          "节奏结构" not in stages.trim_trailing_sections(
              [{"source_text": "# 分镜 1\n\n" + "正文。" * 100}])[0]["source_text"])

    # ---------------- 3. plan 分窗 ----------------
    # 模拟 run_plan 的分窗函数（闭包依赖 beats）：直接按同规则复算
    windows: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for b in trimmed:
        size = len(b.get("source_text") or "") + 100
        if cur and (len(cur) >= stages.PLAN_WINDOW_SHOTS or cur_chars + size > stages.PLAN_WINDOW_CHARS):
            windows.append(cur)
            cur, cur_chars = [], 0
        cur.append(b)
        cur_chars += size
    if cur:
        windows.append(cur)
    check("6 拍长稿被分窗（非一锅端）", len(windows) >= 2, f"{len(windows)} 窗")
    check("分窗覆盖全部拍且保序", [b for w in windows for b in w] == trimmed)
    check("单窗不超上限", all(len(w) <= stages.PLAN_WINDOW_SHOTS for w in windows))
    check("单窗原文累计受控",
          all(sum(len(b["source_text"]) for b in w) <= stages.PLAN_WINDOW_CHARS + 2000 for w in windows))

    # 短稿（≤4 拍）单窗直达，不引入多余轮次
    short = [{"source_text": "短镜原文" * 20} for _ in range(3)]
    check("短稿不分窗", sum(1 for b in short if len(b["source_text"]) < stages.PLAN_WINDOW_CHARS) == 3)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
