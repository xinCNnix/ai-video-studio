"""时长修正后的 Prompt 时间轴同步（确定性纯函数，无 LLM）。

分镜时长被改（手动/台词时长自动校准）后，Prompt 里停在前时长的引用不会自己变
——生成提交的就是自相矛盾的指令（时间轴总和 ≠ duration，模型会按超出或不足的
时刻编排动作）。这里按比例缩放全文的时刻与时间段。

「## 原文」逐字保真块同样缩放时间数字：该块自称"与上文冲突时以本节为准"，
里面残留的旧总时长/时间轴（如"视频长度：7秒"配实际 8s）会把正确的 duration
参数盖掉——保真保的是台词与画面描述文字，时间引用必须跟随实际时长。
块的 Audio Timeline 与正文时间轴是两条独立序列，块边界处链式游标归零重算。

缩放规则：
- 行首时间段（Action Timeline 分段行）：链式整数边界——每段长度 = round(段长×k)、
  下段起点接上段终点，总时长引用（原终点=旧时长）对齐到新时长。整数边界保持
  分段衔接且总和精确等于新时长，round 单调保证不倒挂。
- 正文里的时间段/单个时刻：独立等比缩放（保留 ≤1 位小数）。
"""
import math
import re

# 逐字保真块：从「## 原文」标题行起到 prompt 结尾。块内时间数字同样缩放
# （残留旧时长会借"以本节为准"盖掉正确参数），但块边界要重置链式游标——
# 块内 Audio Timeline 是从 0 重新起算的独立序列
_VERBATIM_HEAD_RE = re.compile(r"^##\s*原文.*$", re.M)

# 时间段（3-8s / 3~8秒）与单个时刻（at 4s / 第 4 秒 / 8 seconds）。
# 单位用先行断言、不吞字符；数值左右边界防 44.1kHz / 24fps / <Audio 12> 被误伤
# （后面不是时间单位的数字不匹配；前置 (?<![\d.]) 防 12s 里截出 2s）。
_TOKEN_RE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[-–~]\s*(\d{1,3}(?:\.\d+)?)(?=\s*(?:s\b|sec|second|秒))"
    r"|(?<![\d.])(\d{1,3}(?:\.\d+)?)(?=\s*(?:s\b|sec|second|秒))"
)

_LINE_PREFIX_RE = re.compile(r"[-*>\s]*$")


def _fmt(v: float) -> str:
    return f"{v:g}"


def _round_half_up(v: float) -> int:
    return math.floor(v + 0.5)


def _scale(t: float, k: float, old: int, new: int) -> float:
    """独立时刻缩放：达到/超出旧时长的引用是总时长指称，直接对齐新时长。"""
    if t >= old:
        return float(new)
    v = t * k
    r = _round_half_up(v)
    return float(r) if abs(v - r) < 1e-9 else round(v, 1)


_SENTENCE_TAIL_RE = re.compile(r"[。．.；;！!？?]\s*$")


def _at_segment_start(prompt: str, pos: int) -> bool:
    """pos 处是否为分段序列起点：行首（允许 -/*/>/缩进），或句末标点之后
    （同一行"0-5秒：…。5-10秒：…"这种句号分隔的时间轴也是分段序列）。"""
    line_head = prompt[:pos].rsplit("\n", 1)[-1]
    if _LINE_PREFIX_RE.fullmatch(line_head):
        return True
    return bool(_SENTENCE_TAIL_RE.search(line_head))


def rescale_prompt_timing(prompt: str, old: int, new: int) -> tuple[str, int]:
    """把 prompt 全文（含「## 原文」保真块）的时间引用从 old 秒等比缩放到 new 秒。

    返回（新 prompt, 修正处数）。old==new 或无匹配时原样返回 0。
    """
    if not prompt or old <= 0 or new <= 0 or old == new:
        return prompt, 0
    m = _VERBATIM_HEAD_RE.search(prompt)
    parts = [prompt[: m.start()], prompt[m.start():]] if m else [prompt]
    k = new / old
    count = 0
    prev_hi = 0.0  # 时间轴分段行的链式边界游标

    def _sub(mm: re.Match, text: str) -> str:
        nonlocal count, prev_hi
        count += 1
        if mm.group(3) is not None:  # 单个时刻：匹配体只有数字本身（单位在断言里）
            return _fmt(_scale(float(mm.group(3)), k, old, new))
        lo, hi = float(mm.group(1)), float(mm.group(2))
        sep_m = re.fullmatch(re.escape(mm.group(1)) + r"(\s*[-–~]\s*)" + re.escape(mm.group(2)), mm.group(0))
        sep = sep_m.group(1) if sep_m else "-"
        if _at_segment_start(text, mm.start()):
            if lo < prev_hi:  # 起点回退＝新时间轴序列（正文/原文各有 0 起点时间轴），游标归零
                prev_hi = 0.0
            lo_i = prev_hi if hi < old or prev_hi > 0 else _round_half_up(lo * k)
            seg = max(1.0, float(_round_half_up((hi - lo) * k)))
            hi_i = float(new) if hi >= old else min(float(new), lo_i + seg)
            if hi_i <= lo_i:
                hi_i = min(float(new), lo_i + 1.0)
            prev_hi = hi_i
            return f"{_fmt(lo_i)}{sep}{_fmt(hi_i)}"
        return f"{_fmt(_scale(lo, k, old, new))}{sep}{_fmt(_scale(hi, k, old, new))}"

    out: list[str] = []
    for i, part in enumerate(parts):
        if i:
            prev_hi = 0.0  # 进入原文块：块内时间轴从 0 重新起算
        out.append(_TOKEN_RE.sub(lambda mm: _sub(mm, part), part))
    return "".join(out), count
