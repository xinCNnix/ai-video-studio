"""配音工作台（片段标记 + 分段合成）离线自测。

不调真实 TTS/不依赖凭据；concat_audio 用 ffmpeg 真跑（本机依赖），
其余为纯逻辑断言。直接运行：python backend/tests/voice_segment_selfcheck.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import voice_script as vs  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"FAIL {name} {detail}")
        raise SystemExit(1)
    print(f"ok   {name}")


def test_parse_basic():
    parts = vs.parse("[happy]今天真好[/happy]<#0.5#>明天更好。")
    check("parse: 片段数", len(parts) == 3, str(parts))
    check("parse: 情绪覆盖", parts[0].overrides == {"emotion": "happy"}, str(parts[0].overrides))
    check("parse: 无覆盖段", parts[2].overrides == {}, str(parts[2].overrides))
    check("parse: 停顿秒数", abs(parts[1].seconds - 0.5) < 1e-9)


def test_parse_nest_and_merge():
    # 嵌套叠加：happy 段内 pitch 覆盖切出独立段，pitch 收束后回到 happy；
    # 后续同情绪文本与首段参数相同，但被中间段隔开不跨段合并
    parts = vs.parse("[happy]前半句[pitch:3]关键词[/pitch]收尾[/happy]，标签外的句子。")
    check("nest: 按参数切四段", len(parts) == 4, str(len(parts)))
    check("nest: 段1 仅情绪", parts[0].overrides == {"emotion": "happy"} and parts[0].text == "前半句")
    check("nest: 段2 情绪+音调", parts[1].overrides == {"emotion": "happy", "pitch": 3} and parts[1].text == "关键词")
    check("nest: 段3 回落情绪", parts[2].overrides == {"emotion": "happy"} and parts[2].text == "收尾")
    check("nest: 段4 标签外默认", parts[3].overrides == {} and parts[3].text == "，标签外的句子。")

    # 参数不同的相邻段不合并；同参数相邻文本合并成一段
    parts = vs.parse("[happy]喜[/happy][sad]悲[/sad]")
    check("nest: 异参不合并", len(parts) == 2)
    check("nest: 后段情绪", parts[1].overrides == {"emotion": "sad"})
    parts = vs.parse("[happy]喜一[/happy][happy]喜二[/happy]")
    check("nest: 同参相邻合并", len(parts) == 1 and parts[0].text == "喜一喜二", str(parts))


def test_parse_robust():
    # 未闭合标记容错到文末；非法 [xxx]（markdown 链接等）当正文保留
    parts = vs.parse("[pitch:5]一直高兴到结束")
    check("robust: 未闭合仍生效", parts[0].overrides == {"pitch": 5}, str(parts[0].overrides))
    text = "看 [这个链接](http://x) 和 [vol:99]超限钳位[/vol]"
    parts = vs.parse(text)
    plain = "".join(p.text for p in parts if isinstance(p, vs.SpeechPart))
    check("robust: markdown 保留", "[这个链接]" in plain, plain)
    vol = [v for k, v in parts[-1].overrides.items() if k == "vol"]
    check("robust: 值钳位 1-10", vol == [10], str(parts[-1].overrides))
    # 非法值标记剥掉但正文不丢
    parts = vs.parse("[pitch:abc]文字[/pitch]")
    check("robust: 非法值忽略", parts[0].text == "文字" and parts[0].overrides == {})


def test_params_defaults():
    part = vs.SpeechPart(text="x", overrides={"pitch": 3, "voice": None})
    merged = part.params({"voice": "v1", "speed": 1.0, "emotion": "neutral", "pitch": 0, "vol": 1.0})
    check("params: 覆盖+默认", merged["pitch"] == 3 and merged["voice"] == "v1" and merged["speed"] == 1.0)


def test_strip_and_adapt():
    s = vs.strip_markers("[happy]高兴[pitch:2]调高[/pitch][/happy]<#1#>平淡结尾")
    check("strip: 纯文本", s == "高兴调高平淡结尾", s)
    s = vs.adapt_inline_tags("笑了(laughs)名字(he2)平", keep_paratags=True, keep_pinyin=True)
    check("adapt: MiniMax 2.8 全保留", s == "笑了(laughs)名字(he2)平", s)
    s = vs.adapt_inline_tags("笑了(laughs)名字(he2)平", keep_paratags=False, keep_pinyin=True)
    check("adapt: 旧款 speech 剥语气词留注音", s == "笑了名字(he2)平", s)
    s = vs.adapt_inline_tags("笑了(laughs)名字(he2)平", keep_paratags=False, keep_pinyin=False)
    check("adapt: 非 MiniMax 全剥", s == "笑了名字平", s)
    s = vs.adapt_inline_tags("正常括号（不是标签）(laugh)近似词", keep_paratags=False, keep_pinyin=False)
    check("adapt: 不误伤正文", "正常括号" in s and "(laugh)" in s, s)


def test_has_markers():
    check("markers: 纯文本", not vs.has_markers("普通一句话。"))
    check("markers: 片段情绪", vs.has_markers("[happy]一句[/happy]"))
    check("markers: 停顿", vs.has_markers("一句<#0.5#>两句"))


def test_concat():
    async def run():
        # 无停顿：单块直返 / 多块直拼
        one = await vs.concat_audio([(b"AAA", 0.0)])
        check("concat: 单块直返", one == b"AAA")
        two = await vs.concat_audio([(b"AAA", 0.0), (b"BBB", 0.0)])
        check("concat: 无停顿直拼", two == b"AAABBB")
        # 有停顿：走 ffmpeg（本机装有）；产物应为合法 mp3 帧头
        import subprocess
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        except OSError:
            print("skip concat: ffmpeg 不可用")
            return
        tone = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=0.4", "-c:a", "libmp3lame",
             "-b:a", "128k", "-f", "mp3", "pipe:1"],
            capture_output=True, timeout=30,
        ).stdout
        out = await vs.concat_audio([(tone, 0.5), (tone, 0.0)])
        check("concat: 带静音产出", len(out) > 1000 and out[:3] == tone[:3])
        # mp3 从管道探测时长为 N/A（头部无总时长），落临时文件再 ffprobe
        tmp = Path(__file__).parent / "_concat_check.mp3"
        tmp.write_bytes(out)
        try:
            dur = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(tmp)],
                capture_output=True, timeout=10,
            )
            seconds = float(dur.stdout.strip())
        finally:
            tmp.unlink(missing_ok=True)
        # 0.4+0.5+0.4 ≈ 1.3s，容差 0.3
        check("concat: 静音计入时长", abs(seconds - 1.3) < 0.3, str(seconds))

    asyncio.run(run())


def test_punct_merge():
    # 闭合标签后残留的句号不单独成段：并入前段（保住标点韵律）
    parts = vs.parse("今天天气[happy]真好啊[/happy]。")
    check("punct: 句号并入情绪段", len(parts) == 2 and parts[1].text == "真好啊。", str(parts))
    check("punct: 前段默认", parts[0].overrides == {} and parts[0].text == "今天天气")
    # 开头标点并入后段；中文不是 \W（不会被误判成纯标点）
    parts = vs.parse("——[sad]迟来的道歉。[/sad]")
    check("punct: 开头标点并入后段", len(parts) == 1 and parts[0].text == "——迟来的道歉。", str(parts))


def main():
    test_parse_basic()
    test_parse_nest_and_merge()
    test_parse_robust()
    test_params_defaults()
    test_strip_and_adapt()
    test_has_markers()
    test_punct_merge()
    test_concat()
    print("\nvoice_segment_selfcheck 全部通过")


if __name__ == "__main__":
    main()
