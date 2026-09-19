"""结构化 LLM 调用信封：坏输出绝不带病进入下一阶段。

调用链：网络退避重试 → JSON 解析（围栏剥离/括号配平扫描/字面量修复）→
Pydantic 校验 → 校验错误回喂修复（≤max_repair 轮）→ 仍失败则 FatalError。
每次调用/修复/重试全部写入 run trace。
"""
import asyncio
import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ...harness.errors import FatalError, RetryableError

T = TypeVar("T", bound=BaseModel)

NET_RETRIES = 3
NET_BACKOFF = (2, 5, 10)


def repair_json_literals(text: str) -> str:
    """把 JSON 字符串值里的真实换行/回车/制表符转成合法转义——模型经常直接输出多行文本。"""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch in ("\n", "\r", "\t"):
            out.append("\\n" if ch == "\n" else ("\\r" if ch == "\r" else "\\t"))
            continue
        out.append(ch)
    return "".join(out)


def scan_json_objects(text: str) -> list[str]:
    """按括号配平扫描文本里所有 {...} 片段（字符串/转义感知，容忍前后夹杂说明文字）。"""
    fragments: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    fragments.append(text[start : i + 1])
                    start = -1
    return fragments


def parse_json_candidates(text: str) -> list[dict]:
    """提取文本中所有能解析成 dict 的 {...}（容忍字符串值里的真实换行）。"""
    out: list[dict] = []
    for frag in scan_json_objects(text):
        parsed = None
        for cand in (frag, repair_json_literals(frag)):
            try:
                parsed = json.loads(cand)
            except json.JSONDecodeError:
                continue
            break
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


class LLMEnvelope:
    """结构化调用信封。llm 为 providers.llm 的适配器实例（.generate(messages, model, …)）。"""

    def __init__(self, llm, model: str, trace=None, max_repair: int = 2):
        self.llm = llm
        self.model = model
        self.trace = trace
        self.max_repair = max_repair
        self.calls = 0

    async def raw_call(self, messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 8000) -> str:
        """带网络退避重试的原始调用（一次网络抖动不再让整个 run 报废）。"""
        last_exc: Exception | None = None
        for attempt in range(NET_RETRIES):
            try:
                self.calls += 1
                if self.trace:
                    self.trace.log("llm_call", model=self.model, attempt=attempt + 1,
                                   user_tail=(messages[-1]["content"] or "")[-300:])
                out = await self.llm.generate(messages, self.model, temperature=temperature, max_tokens=max_tokens)
                if self.trace:
                    self.trace.count("llm_calls")
                    self.trace.log("llm_result", chars=len(out or ""), tail=(out or "")[-300:])
                return out or ""
            except Exception as exc:  # noqa: BLE001 - 网络/限流统一走重试
                last_exc = exc
                if self.trace:
                    self.trace.count("retries")
                    self.trace.log("llm_retry", attempt=attempt + 1, error=str(exc)[:200])
                if attempt < NET_RETRIES - 1:
                    await asyncio.sleep(NET_BACKOFF[min(attempt, len(NET_BACKOFF) - 1)])
        raise RetryableError(f"LLM 连续 {NET_RETRIES} 次调用失败：{str(last_exc)[:200]}")

    async def structured_call(
        self, system: str, user: str, schema: type[T], *,
        temperature: float = 0.3, max_tokens: int = 8000,
    ) -> T:
        """结构化调用：解析 + 校验 + 错误回喂修复。修不好抛 FatalError（带最后一次原始输出摘要）。"""
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_raw = ""
        for round_no in range(self.max_repair + 1):
            raw = await self.raw_call(messages, temperature=temperature, max_tokens=max_tokens)
            last_raw = raw
            parsed = self._parse(raw)
            if parsed is None:
                if self.trace:
                    self.trace.count("validation_errors")
                feedback = (
                    "上一轮输出无法解析为 JSON 对象。可能原因：输出被 max_tokens 截断、夹杂多余说明文字、"
                    "或字符串值里包含未转义的换行。请重新输出【一个完整合法的 JSON 对象】（不要代码块围栏、"
                    "不要任何 JSON 以外的文字、字符串值用 \\n 转义换行）。"
                )
                messages.append({"role": "assistant", "content": raw[:4000]})
                messages.append({"role": "user", "content": feedback})
                continue
            try:
                result = schema.model_validate(parsed)
                if self.trace:
                    self.trace.log("llm_validated", schema=schema.__name__, round=round_no + 1)
                return result
            except ValidationError as exc:
                errors = "; ".join(
                    f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
                )
                if self.trace:
                    self.trace.count("validation_errors")
                    self.trace.log("llm_invalid", schema=schema.__name__, errors=errors[:500])
                messages.append({"role": "assistant", "content": raw[:4000]})
                messages.append({"role": "user", "content": f"上一轮 JSON 校验失败，逐项修复后重新输出完整 JSON（错误清单：{errors}）。不要解释，只输出 JSON。"})
        raise FatalError(
            f"结构化输出连续 {self.max_repair + 1} 轮未通过 {schema.__name__} 校验，已中止"
            f"（最后输出前 200 字：{last_raw[:200]!r}）"
        )

    def _parse(self, raw: str) -> dict | None:
        text = (raw or "").strip()
        if not text:
            return None
        fenced = re.sub(r"^```(?:json)?\s*", "", text)
        fenced = re.sub(r"\s*```$", "", fenced)
        for cand in (fenced, repair_json_literals(fenced)):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        candidates = parse_json_candidates(raw)
        return candidates[-1] if candidates else None
