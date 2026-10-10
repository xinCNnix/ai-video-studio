"""storyboard 标题识别/分窗离线自检（2026-09-21 吕满春西游事故回归）。

事故：用户稿用「# 分镜 N」编号（无片段｜标题、无【第N镜】括号式），detect_fragment_headers
不认识 → 走 LLM 分块慢路径（6.5 分钟）且块边界切断分镜 2；末拍吞进稿尾全局章节；
plan 一锅端全文 → M3 思考烧尽 max_tokens、正文三连空。
2026-09-26 第六回事故：「## 镜头N｜标题」格式不识 → LLM 拆拍把镜头02 从时间轴中间
劈成两拍（16 镜变 17），plan 忠实对录、保真闸门拿 17 当基准越守越错。
本自检全部离线（零 LLM），验证：标题识别、逐字切片、末拍截尾、plan 分窗、散文不误触。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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

    # ---------------- 4. 【分镜N｜标题】括号式头（闯西游第四回事故） ----------------
    # 正文头形如【分镜01｜拦路与坦白】/【分镜12A｜月下孤影】，旧正则 0 命中 → LLM 分块
    # 把素材索引「Image 05｜分镜01｜四人山路对峙…」也数成分镜，14 拍翻倍成 29
    bracket_frags = []
    titles = ["拦路与坦白", "越解释越糊涂", "换一种说法", "真正的疑点", "你们是我们世界里的故事",
              "特别有名的那种", "故事不是未来", "让开一条路", "夜间营地建立", "右边山上动了一下",
              "记下位置", "月下孤影", "悟空也察觉了", "空山失色"]
    nos = [f"{i:02d}" for i in range(1, 12)] + ["12A", "12B", "12C"]
    for no, t in zip(nos, titles):
        bracket_frags.append(
            f"【分镜{no}｜{t}】\nDuration：7秒。配音1条，参考图3张。\n"
            "Reference 图片上传绑定：Picture 1=Image 01；Picture 2=Image 02；Picture 3=Image 05。\n"
            "Reference 音频绑定：本镜Audio 1=全局Audio 03。\n\n"
            "0—2秒：从Image 05四人已经停步对视的姿态开始，摄影机沿山路平稳前推。" + "表演细节。" * 20 + "\n"
        )
    bracket_src = (
        "# 《测试》\n\n# 一、执行规则\n\n本回14个独立生成片段。\n\n"
        "Image 05｜分镜01｜四人山路对峙：悟空左、吕满春右。\n"
        "Audio 01｜孙悟空｜原长4.77秒→槽位5秒\n俺老孙忍了一路了。\n\n"
        "六、独立分镜正文\n\n" + "\n".join(bracket_frags)
        + "\n七、独立生成与拼接\n\n拼接顺序：01→02。全部台词各用一次。\n"
    )
    bh = stages.detect_fragment_headers(bracket_src)
    check("【分镜N｜标题】括号式头识别 14 个", len(bh) == 14, f"{len(bh)}")
    check("括号式标题剥离编号", [t for _, t in bh][:2] == ["拦路与坦白", "越解释越糊涂"],
          str([t for _, t in bh][:3]))
    check("字母后缀（12A/12B/12C）不并号", [t for _, t in bh][-3:] == ["月下孤影", "悟空也察觉了", "空山失色"],
          str([t for _, t in bh][-3:]))
    check("素材索引行「Image 05｜分镜01｜…」不误识为头（行首锚定）",
          all(st not in ("四人山路对峙：悟空左、吕满春右。",) for _, st in bh))
    check("散文「例如分镜06：图片依次上传」不误触",
          stages.detect_fragment_headers("例如分镜06：图片依次上传Image 02、03。\n") == [])

    bb = stages._beats_from_headers(bracket_src, bh)
    check("括号式一镜一拍共 14 拍全 verbatim",
          len(bb) == 14 and all(b["source_fidelity"] == "verbatim" for b in bb))
    bt = stages.trim_trailing_sections(bb)
    check("末拍切掉中文序号稿尾章节「七、」", "独立生成与拼接" not in bt[-1]["source_text"],
          bt[-1]["source_text"][-80:])
    check("截尾后末拍仍是完整片段（含时间轴正文）", bt[-1]["source_text"].startswith("【分镜12C"))
    check("截尾不动其他拍", all("独立生成与拼接" not in b["source_text"] for b in bt))

    # 同行 Reference 头 + 裸编号声明（本稿格式）：图片/音频/全局号优先
    img_ns, aud_ns, _ = stages._reference_tokens(bb[0]["source_text"])
    check("同行 Reference 头的裸编号声明被解析", img_ns == [1, 2, 5] and aud_ns == [3], f"{img_ns} {aud_ns}")
    img2, aud2, _ = stages._reference_tokens(
        "Reference 音频绑定：本镜Audio 1=全局Audio 03；本镜Audio 2=全局Audio 04。\n"
        "0—2秒：从Image 05的姿态开始推进，这是散文叙述不是声明行，镜头缓缓前推并保持稳定。")
    check("全局Audio 优先于本镜局部号（4 条音频不并成两镜各 4 条）", aud2 == [3, 4], f"{aud2}")
    check("散文行里的 Image 提及不入声明", img2 == [], f"{img2}")

    # 同号去重闸门：索引拍 + 正文拍 + 分块包装拍同号 → 只留 verbatim 正文拍
    dup = [
        {"title": "分镜01", "source_text": "Image 05｜分镜01｜四人山路对峙。", "source_fidelity": "llm_echo"},
        {"title": "拦路与坦白", "source_text": "【分镜01｜拦路与坦白】\nDuration：13秒。",
         "source_fidelity": "verbatim"},
        {"title": "片段1", "source_text": "【剧本片段 1/5】\n Duration：13秒。", "source_fidelity": "llm_echo"},
    ]
    kept, dropped = stages.dedupe_storyboard_beats(dup)
    check("同号去重：只留 verbatim 正文拍", len(kept) == 1 and kept[0]["title"] == "拦路与坦白"
          and len(dropped) == 2, f"kept={[k['title'] for k in kept]}")
    kept2, dropped2 = stages.dedupe_storyboard_beats(bb)
    check("无重复时闸门零动作", kept2 == bb and not dropped2)

    # ---------------- 5. 「## 镜头N｜标题」格式（闯西游第六回事故） ----------------
    # 正文头形如 ## 镜头02｜同一块石头，旧正则 0 命中 → LLM 分块把镜头02 从时间轴中间
    # 劈成两拍（16 镜变 17，切点在小墨跟随吕满春视线低头。｜**2.0—5.0秒** 处）
    shot_frags = []
    shot_meta = [("01", "古道轻谈", "Audio 1"), ("02", "同一块石头", "Audio 3"), ("03", "路线闭环", "Audio 5")]
    for no, t, aud in shot_meta:
        shot_frags.append(
            f"## 镜头{no}｜{t}\n\n**Duration：{12 if no == '02' else 7}秒**\n\n"
            "### Reference\n\n- Picture 1 = Image 01｜吕满春\n- Picture 2 = Image 02｜小墨\n\n"
            "### Timeline\n\n**0.0—2.0秒**\n\n吕满春停止脚步。\n小墨跟随吕满春视线低头。\n\n"
            "**2.0—5.0秒**\n\n摄影机向黑岩方向缓慢推近约30厘米。" + "表演细节。" * 30 + "\n\n"
            f"### End Frame\n\n定格。\n\n---\n"
        )
    shot_src = (
        "# 《吕满春和小墨闯西游》第六回\n\n# 一、全局参考资产\n\n"
        "- **Image 06—21**：镜头01—16对应首帧\n- **Image 22**：白影固定外观参考\n\n"
        "# 三、Audio全局锁定\n\nAudio 1｜小墨｜5.72秒→6秒槽\n“台词。”\n\n"
        "# 四、逐镜生成提示词\n\n" + "\n".join(shot_frags)
        + "\n# 五、生成顺序建议\n\n1. Image 06｜古道和重复地标\n2. Image 22｜白影统一资产\n\n"
        "# 六、H3高风险镜头\n\n### 镜头02\n\n必须确认：\n- 摄影没有偷偷转向\n\n### 镜头03\n\n必须确认：\n- 远山作为固定参照\n"
    )
    sh = stages.detect_fragment_headers(shot_src)
    check("「## 镜头N｜标题」识别 3 个（第六回事故格式）", len(sh) == 3, f"{len(sh)}: {[t for _, t in sh]}")
    check("镜头标题剥离编号", [t for _, t in sh] == ["古道轻谈", "同一块石头", "路线闭环"],
          str([t for _, t in sh]))
    check("稿尾风险清单裸「### 镜头N」不误识为头", "必须确认" not in str(sh))

    sb = stages._beats_from_headers(shot_src, sh)
    sb = stages.trim_trailing_sections(sb)
    check("一镜一拍共 3 拍（镜头02 不再被劈成两拍）", len(sb) == 3, f"{len(sb)}")
    check("每拍 source_text 以自身「## 镜头N｜」头开始",
          all(b["source_text"].startswith(f"## 镜头{no}｜") for b, (no, _, _) in zip(sb, shot_meta)))
    check("镜头02 完整（0—2秒段与2.0—5.0秒段同拍）",
          "小墨跟随吕满春视线低头。" in sb[1]["source_text"]
          and "2.0—5.0秒" in sb[1]["source_text"] and "### End Frame" in sb[1]["source_text"])
    check("末拍切掉稿尾章节（生成顺序/高风险清单不入原文）",
          all("生成顺序" not in b["source_text"] and "高风险" not in b["source_text"] for b in sb))
    check("散文中提及「镜头 N」不误触",
          stages.detect_fragment_headers("镜头01的构图以首帧为准，镜头缓缓推进。\n这里是叙述，镜头02在文中被提及。\n") == [])
    check("「镜头 3｜标题」空格+半角竖线变体仍识别",
          len(stages.detect_fragment_headers("镜头 3 | 发展")) == 1)
    check("「镜头1：7s」时长行仍被排除", stages.detect_fragment_headers("镜头1：7s\n镜头2：8s") == [])
    check("【镜头N｜标题】括号式变体识别", len(stages.detect_fragment_headers("【镜头01｜开场】\n内容")) == 1)

    # 慢路径遗留拆分拍的保险：同「镜头02」编号两拍 → 去重闸门合并
    dup_shot = [
        {"title": "同一块石头", "source_text": "## 镜头02｜同一块石头\n**Duration：12秒**\n0.0—5.0秒内容。"
         + "细节。" * 80, "source_fidelity": "verbatim"},
        {"title": "镜头02", "source_text": "小墨跟随吕满春视线低头。\n**2.0—5.0秒**\n后续内容。",
         "source_fidelity": "verbatim"},
    ]
    kept_shot, dropped_shot = stages.dedupe_storyboard_beats(dup_shot)
    check("「镜头N」编号入去重闸门：同号两拍只留全文拍",
          len(kept_shot) == 1 and kept_shot[0]["source_text"].startswith("## 镜头02")
          and len(dropped_shot) == 1, f"kept={[k['title'] for k in kept_shot]}")

    # ---------------- 6. **加粗段名** markdown 六段式成稿（2026-10-04 聊皮肤13集事故） ----------------
    # 稿式：## Shot NN｜标题 + **Duration:** 4.00 s（冒号在加粗内部）+ **References:** 同行声明
    # + **subject_definitions:** 正文同行。旧识别只认「裸段名独占行」→ 0 命中 →
    # normalize 走 LLM 分块把 10 镜拆成 26 拍（保真闸门拿错误基准越守越错），
    # 分块包装头【剧本片段 i/N】再污染 1 拍致直录全灭，13 窗慢路径 1 小时+
    bold_frags = []
    for no, (t, dur, imgs) in enumerate([("发现", 4, 2), ("确认", 5, 2), ("解释", 6, 3)], 1):
        refs = "; ".join(f"`<Picture {k}>` ← Image0{k}_K{no:02d}A at 0.00" for k in range(1, imgs + 1))
        refs += "; `<Audio 1>` ← Audio01 (pending)."
        bold_frags.append(
            f"## Shot {no:02d}｜{t}\n\n"
            f"**Duration:** {dur}.00 s. **Global:** 00:00.00—00:0{dur}.00.\n\n"
            f"**References:** {refs}\n\n"
            f"**subject_definitions:** `<Subject 1>` is Xiao Mo. `<Subject 2>` is A in shot {no}.\n\n"
            f"**summary:** Shot {no} summary line.\n\n"
            f"**detailed_description:** [Shot {no}] content with movement." + " Detail." * 10 + "\n\n"
            f"**overall_soundscape:** Soft.\n\n**non_diegetic_music:** N/A.\n"
        )
    bold_src = ("# 第N集\n\n## 全局参考图总表\n\nImage01：小墨。\n\n---\n\n"
                + "\n".join(bold_frags) + "\n## 交付核对\n\n28 张关键帧。\n")
    bh3 = stages.detect_h3_prompt_boundaries(bold_src)
    check("加粗六段式成稿识别 3 镜（原 0 命中）", len(bh3) == 3, f"{len(bh3)}")
    check("镜头标题从「## Shot N｜标题」提取", [t for _, t in bh3] == ["发现", "确认", "解释"],
          str([t for _, t in bh3]))
    bb3 = stages._beats_from_headers(bold_src, bh3)
    check("加粗式一镜一拍全 verbatim 且以镜头头开头",
          len(bb3) == 3 and all(b["source_fidelity"] == "verbatim" for b in bb3)
          and all(b["source_text"].startswith(f"## Shot {i + 1:02d}｜") for i, b in enumerate(bb3)))
    check("References/Duration 声明留在拍内（bind 素材对号依据）",
          all("References:" in b["source_text"] and "Duration:" in b["source_text"] for b in bb3))
    check("上扩不跨镜：每拍 non_diegetic_music 属于自己、不含别镜标题",
          all("non_diegetic_music" in b["source_text"] for b in bb3)
          and "确认" not in bb3[0]["source_text"] and "发现" not in bb3[1]["source_text"])
    check("加粗式 extract_h3_body 全部成功", all(stages.extract_h3_body(b["source_text"]) for b in bb3))
    check("加粗 **Duration:**（冒号在加粗内）时长被解析",
          [stages._DURATION_DECL_RE.search(b["source_text"]).group(1) for b in bb3] == ["4.00", "5.00", "6.00"])
    img_ns, aud_ns, _ = stages._reference_tokens(bb3[0]["source_text"])
    check("同行 References 声明解析（图 2 张 + 音 1 段）", img_ns == [1, 2] and aud_ns == [1], f"{img_ns} {aud_ns}")
    fp = SimpleNamespace(materials=[SimpleNamespace(kind="image", id=f"m{i}") for i in range(5)]
                         + [SimpleNamespace(kind="audio", id="a1")])
    adopted = stages.adopt_verbatim_h3_shots(fp, bb3)
    check("加粗式成稿直录全稿可用（零 LLM 路径）", adopted is not None and len(adopted) == 3)
    if adopted:
        check("直录时长逐镜正确（非默认 5）", [s["duration"] for s in adopted] == [4, 5, 6],
              str([s["duration"] for s in adopted]))
        check("直录 wants_images 按声明对号", [len(s["wants_images"]) for s in adopted] == [2, 2, 3],
              str([len(s["wants_images"]) for s in adopted]))

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
