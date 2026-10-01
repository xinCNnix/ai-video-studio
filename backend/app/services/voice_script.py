"""配音台词标记解析与分段合成引擎。

前端「配音工作台」把选中片段的表达力意图写成内联标记：
- 情绪片段 [happy]文字[/happy]        仅该片段用 happy 情绪合成
- 音调片段 [pitch:3]文字[/pitch]      -12~12（MiniMax 半音）
- 音量片段 [vol:2]文字[/vol]          1~10
- 语速片段 [speed:1.2]文字[/speed]    0.5~2.0
- 音色片段 [voice:音色ID]文字[/voice] 多角色对话
- 停顿 <#0.5#>                        沿用 MiniMax 原生写法，引擎统一转静音插入
标记可嵌套叠加（[happy][pitch:3]…[/pitch][/happy]），未闭合容错到文末；
不认识的 [xxx] 一律当正文原样保留。

MiniMax 无句内情绪标签（API 仅请求级 emotion），片段情绪 = 拆段分别合成再
拼接静音成条——与 MiniMax 网页工作台「选中文字加情绪」同一条实现路径，
且对小米 / OpenAI / Higgs 同样可用。

语气词 (laughs) 与注音 (he2) 是 MiniMax 专属内联标签：speech-2.8 系两者皆可，
其余 speech 系仅注音，其他厂商会被当英文念出来——由 adapt_inline_tags 按需剥离。
"""
import asyncio
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# 与 settings.TTS_EMOTIONS / MiniMax 官方 emotion 取值保持一致
EMOTIONS = {"neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}
PARAM_KEYS = {"pitch", "vol", "speed", "voice"}

# MiniMax 语气词标签全集（仅 speech-2.8-hd/turbo 支持；来自 skills/minimax-voice-tags.md）
PARALANGUAGE_TAGS = {
    "laughs", "chuckle", "sighs", "gasps", "breath", "pant", "inhale", "exhale",
    "coughs", "clear-throat", "sniffs", "sneezes", "groans", "snorts", "burps",
    "lip-smacking", "humming", "hissing", "emm",
}

_TOKEN = re.compile(r"\[(/?)([a-zA-Z_-]+)(?::([^\]]+))?\]|<#(\d+(?:\.\d{1,2})?)#>")
_PARATAG_RE = re.compile(r"\((" + "|".join(sorted(PARALANGUAGE_TAGS)) + r")\)")
_PINYIN_RE = re.compile(r"\([a-z]+[1-6]\)")
# 纯标点/空白（无可朗读字符）：不单独成段，并入前段保住韵律
_PUNCT_ONLY = re.compile(r"^[\s\W]+$", re.UNICODE)


@dataclass
class SpeechPart:
    text: str
    overrides: dict = field(default_factory=dict)  # emotion/pitch/vol/speed/voice 的片段级覆盖

    def params(self, defaults: dict) -> dict:
        """片段参数 = 全局默认 + 片段覆盖（忽略空值覆盖）。"""
        merged = dict(defaults)
        for k, v in self.overrides.items():
            if v is not None:
                merged[k] = v
        return merged


@dataclass
class PausePart:
    seconds: float


def _pop(stack: list[tuple[str, object]], key: str) -> None:
    for i in range(len(stack) - 1, -1, -1):
        if stack[i][0] == key:
            del stack[i]
            return


def _parse_value(key: str, raw: str | None) -> object | None:
    """标记值解析；非法值返回 None（标记本身仍被剥掉，不影响正文）。"""
    if key == "voice":
        v = (raw or "").strip()
        return v or None
    try:
        if key == "pitch":
            return max(-12, min(12, int(round(float(raw)))))
        if key == "vol":
            return max(1, min(10, int(round(float(raw)))))
        if key == "speed":
            return max(0.5, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return None
    return None


def parse(text: str) -> list[SpeechPart | PausePart]:
    """标记文本 → 片段序列（语音段携带参数覆盖快照，停顿独立成段）。"""
    parts: list[SpeechPart | PausePart] = []
    stack: list[tuple[str, object]] = []
    pos = 0

    def flush(end: int) -> None:
        nonlocal pos
        if end > pos:
            parts.append(SpeechPart(text=text[pos:end], overrides=dict(stack)))
        pos = end

    for m in _TOKEN.finditer(text):
        closing, key, value, pause = m.groups()
        if pause is None and key not in PARAM_KEYS and key not in EMOTIONS:
            continue  # 非法标记（如 markdown 链接）当正文原样保留
        flush(m.start())
        pos = m.end()
        if pause is not None:
            parts.append(PausePart(seconds=float(pause)))
        elif key in PARAM_KEYS:
            if closing:
                _pop(stack, key)
            else:
                parsed = _parse_value(key, value)
                if parsed is not None:
                    stack.append((key, parsed))
        else:  # 情绪开/闭标签，key 即情绪名
            if closing:
                _pop(stack, "emotion")
            else:
                stack.append(("emotion", key))
    flush(len(text))
    return merge(parts)


def merge(parts: list[SpeechPart | PausePart]) -> list[SpeechPart | PausePart]:
    """相邻同参数语音段合并；相邻停顿合并；纯空白段丢弃。

    纯标点段（如 [happy]真好啊[/happy]。 里闭合标签后残留的句号）不单独
    送合成——并入前一段（无前段时并入后段），保住标点自带的停顿韵律。
    """
    out: list[SpeechPart | PausePart] = []
    leading_punct = ""
    for p in parts:
        if isinstance(p, PausePart):
            if out and isinstance(out[-1], PausePart):
                out[-1].seconds += p.seconds
            elif out:  # 首部停顿丢弃（拼接前静音无意义）
                out.append(p)
            continue
        if not p.text.strip():
            continue
        if _PUNCT_ONLY.match(p.text):
            if out and isinstance(out[-1], SpeechPart):
                out[-1].text += p.text
            else:
                leading_punct += p.text
            continue
        if leading_punct:
            p = SpeechPart(text=leading_punct + p.text, overrides=p.overrides)
            leading_punct = ""
        if out and isinstance(out[-1], SpeechPart) and out[-1].overrides == p.overrides:
            out[-1].text += p.text
        else:
            out.append(p)
    return out


def strip_markers(text: str) -> str:
    """去掉全部工作台标记，得到纯朗读文本（用于文件名/预览）。"""
    return "".join(p.text for p in parse(text) if isinstance(p, SpeechPart))


def adapt_inline_tags(text: str, *, keep_paratags: bool, keep_pinyin: bool) -> str:
    """按厂商能力保留/剥离 MiniMax 专属内联标签。

    keep_paratags: 语气词标签 (laughs) 等（仅 MiniMax speech-2.8 系）
    keep_pinyin:   注音 (he2)（MiniMax speech 系）
    其余厂商两者都剥，避免英文标签被当正文念出来。
    """
    if not keep_paratags:
        text = _PARATAG_RE.sub("", text)
    if not keep_pinyin:
        text = _PINYIN_RE.sub("", text)
    return text


def has_markers(text: str) -> bool:
    """文本是否含工作台标记（决定走分段合成还是整段直合成）。"""
    parts = parse(text)
    if len(parts) != 1:
        return True
    only = parts[0]
    return isinstance(only, SpeechPart) and bool(only.overrides)


async def _make_silence(out: Path, seconds: float) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=32000:cl=mono", "-t", f"{seconds:.2f}",
        "-c:a", "libmp3lame", "-b:a", "128k", str(out),
    ]
    try:
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and out.exists() and out.stat().st_size > 0


async def concat_audio(segments: list[tuple[bytes, float]], reencode: bool = False) -> bytes:
    """[(音频字节, 段后停顿秒)] → 单条 mp3。

    有停顿或 reencode=True 时走 ffmpeg：每段落盘 + anullsrc 静音轨 +
    concat filter 重编码（统一编码参数，接缝最平滑；MP3 帧直拼插不了静音）。
    其余情况保持 MP3 帧直拼（与既有长文路径一致）；ffmpeg 不可用/失败
    退化为直拼（丢停顿，段仍连续可播）。分段合成路径建议 reencode=True。
    """
    audios = [a for a, _ in segments if a]
    if not audios:
        return b""
    need_silence = any(s >= 0.01 for _, s in segments)
    if len(audios) == 1 and not need_silence:
        return audios[0]
    if not need_silence and not reencode:
        return b"".join(audios)

    with tempfile.TemporaryDirectory(prefix="voice_concat_") as td:
        inputs: list[Path] = []
        for i, (audio, pause) in enumerate(segments):
            if not audio:
                continue
            p = Path(td) / f"seg{i:03d}.mp3"
            p.write_bytes(audio)
            inputs.append(p)
            if pause >= 0.01:
                s = Path(td) / f"sil{i:03d}.mp3"
                if await _make_silence(s, pause):
                    inputs.append(s)  # 静音生成失败仅丢该停顿，不断链
        if len(inputs) <= 1:
            return b"".join(audios)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for p in inputs:
            cmd += ["-i", str(p)]
        streams = "".join(f"[{i}:a]" for i in range(len(inputs)))
        cmd += [
            "-filter_complex", f"{streams}concat=n={len(inputs)}:v=0:a=1[out]",
            "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "128k",
            "-ar", "32000", "-ac", "1", "-f", "mp3", "pipe:1",
        ]
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return b"".join(audios)
        if proc.returncode != 0 or not proc.stdout:
            return b"".join(audios)
        return proc.stdout
