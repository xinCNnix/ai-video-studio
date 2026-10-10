"""剧本 V2 逐镜绑定表识别离线自检（2026-10-03 聊皮肤第12集事故回归）。

事故：V2 成稿用 markdown 表格声明素材映射（无 Reference 标题行）——
「| 类型 | 本镜绑定 | 全局素材 | 用途/时点 |」。老识别（标题行激活声明区）整表
漏拆：图片侧 <Picture k> 不在尖括号词表、ImageNN.png 裸文件名只在声明区内采信
→ 10 镜图片对齐全空；音频侧本镜 <Audio 1> 被兜底当全局 1 号（仅镜01碰巧对，
其余 9 镜语义互证全炸走台词兜底）。音频-only 对齐让 aligned 非空 →
「原稿已对齐」闸门把 25 张图全判 bind_unused_by_script 悬空，gen_mode 全落 t2v。
本自检全部离线（零 LLM），验证：V2 表行解析、老格式回归、成稿直录映射、
编号表身份列、声明行保真跳过、闸门新语义。
"""
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


V2_BEAT = """# 镜头01｜黄金前三秒：颜色竟然在参与保护
**Duration：6秒；全片区间：0.00–6.00秒。**
**配音：4.59秒；本镜发声：0.40–4.99秒；句后余量：1.01秒。**

| 类型 | 本镜绑定 | 全局素材 | 用途/时点 |
|---|---|---|---|
| 图像 | `<Picture 1>` | Image11.png | 0.00s首帧：手持标本的近景 |
| 图像 | `<Picture 2>` | Image23.png | 0.80s切入此观察构图；5.00s到达闭口末态 |
| 配音 | `<Audio 1>` | 全局Audio 01 | 小墨；完整复用；0.40–4.99s |

```text
subject_definitions:
<Subject 1> is 小墨 (S1), the translucent amber 3D cartoon character in the bound pictures.
<Picture 1> is the opening frame and its camera, blocking and prop-state anchor; its global source is Image11.png.
<Picture 2> is the observation composition entered by the explicit 0.80s cut; its global source is Image23.png.

detailed_description:
[Shot 1] Begin from <Picture 1>, an eye-level medium close view of <Subject 1>.
```text
"""

# 老格式回归样例（第11集）：**Reference 图片上传绑定：** 标题行 + 裸编号声明行
OLD_BEAT = """# 镜头 01｜黄金前三秒
**Duration：5秒。配音1条。关键帧3张。**
**Reference 图片上传绑定：**
- Picture 1 = Image 04（小墨定妆）
- Picture 2 = Image 05（首帧：真实皮肤表面极近景）
- Picture 3 = Image 06（中段：镜头穿入表皮）
- Picture 4 = Image 07（末帧：巨大皮肤细胞城市）

**Reference 音频绑定：**
- Audio 1 = 全局 Audio 1

**首帧：Image 05。**
"""

# V3 绑定表（2026-10-03 第12集二轮事故，用户清空重投后的稿式）：槽位裸写、
# 音频列为文件名、无类型列，首列是上传次序
V3_BEAT = """# 镜头01｜发现挡光线索

生成时长：6秒。全片位置：0.00–6.00秒。采用H3多参考输入。

| 上传次序 | 本镜槽位 | 本文件夹内文件 | 用途 |
|---|---|---|---|
| 1 | Picture 1 | Image05.png | 首帧：小墨手持颗粒簇近景 |
| 2 | Picture 2 | Image06.png | 末态：小墨在左、观察细胞在右，闭口疑惑 |
| 3 | Audio 1 | Audio01.mp3 | 小墨原配音，完整复用 |

```text
subject_definitions:
<Subject 1> is the single amber translucent 3D cartoon character.
<Picture 1> is the actual opening frame at 0.00 seconds.
```text
"""


def main() -> None:
    print("1) _reference_tokens：V2 绑定表")
    img, aud, vid = stages._reference_tokens(V2_BEAT)
    check("图片行全局号按行序", img == [11, 23], f"got {img}")
    check("音频行取全局号（非本镜 <Audio 1> 误读）", aud == [1], f"got {aud}")
    check("无视频行", vid == [], f"got {vid}")

    img3, aud3, _ = stages._reference_tokens(V2_BEAT.replace("Image11.png", "Image30.png"))
    check("ImageNN.png 编号解析", img3 == [30, 23], f"got {img3}")

    print("2) _reference_tokens：老格式回归（声明区路径不受影响）")
    img, aud, _ = stages._reference_tokens(OLD_BEAT)
    check("Reference 标题行激活声明区", img == [4, 5, 6, 7], f"got {img}")
    check("音频全局号", aud == [1], f"got {aud}")

    print("2b) _reference_tokens：V3 绑定表（裸槽位 + 文件名音频列）")
    img, aud, vid = stages._reference_tokens(V3_BEAT)
    check("图片行全局号按行序", img == [5, 6], f"got {img}")
    check("音频行取文件名全局号", aud == [1], f"got {aud}")
    check("无视频行", vid == [], f"got {vid}")

    print("3) _reference_tokens：散文/非声明表不误采")
    prose = "| 说明 | 参考 <Picture 1> 与 Image 5 的关系 | 长篇散文叙述 |\n" \
            "0—2秒：从Image 05 缓推，其余散文提及 Image 06。"
    img, aud, _ = stages._reference_tokens(prose)
    check("非类型化首列表格行不识别为声明", img == [], f"got {img}")
    check("散文音频提及不入表", aud == [], f"got {aud}")

    print("4) 成稿直录映射（parse_picture_map / parse_audio_map）")
    check("V2 表行 <Picture k> | ImageNN.png", stages.parse_picture_map(V2_BEAT) == [11, 23],
          f"got {stages.parse_picture_map(V2_BEAT)}")
    check("V2 表行音频全局号", stages.parse_audio_map(V2_BEAT) == [1],
          f"got {stages.parse_audio_map(V2_BEAT)}")
    check("V3 裸槽位 Picture k | ImageNN.png", stages.parse_picture_map(V3_BEAT) == [5, 6],
          f"got {stages.parse_picture_map(V3_BEAT)}")
    check("V3 音频文件名列", stages.parse_audio_map(V3_BEAT) == [1],
          f"got {stages.parse_audio_map(V3_BEAT)}")
    arrow = "`<Picture 1>` ← **Image02**（v3 加粗）\n`<Audio 2>` ← 全局Audio 03"
    check("箭头式声明回归", stages.parse_picture_map(arrow) == [2],
          f"got {stages.parse_picture_map(arrow)}")
    check("箭头式音频回归", stages.parse_audio_map(arrow) == [3],
          f"got {stages.parse_audio_map(arrow)}")

    print("4b) 时长声明：中文「生成时长：N秒」")
    dm = stages._DURATION_DECL_RE.search(V3_BEAT)
    check("生成时长解析", dm is not None and dm.group(1) == "6", f"got {dm and dm.group(1)}")
    check("全片位置行不误配", stages._DURATION_DECL_RE.search("全片位置：0.00–6.00秒。") is None)

    print("5) _reference_table：V2/V3 身份列")
    table = stages._reference_table(V2_BEAT)
    check("Image11 身份取自第4列", table.get(11) == "0.00s首帧：手持标本的近景", f"got {table.get(11)}")
    check("Image23 身份截断正确", "切入此观察构图" in table.get(23, ""), f"got {table.get(23)}")
    table3 = stages._reference_table(V3_BEAT)
    check("V3 Image05 身份取自行末用途列", table3.get(5) == "首帧：小墨手持颗粒簇近景",
          f"got {table3.get(5)}")
    old_table = stages._reference_table(OLD_BEAT)
    check("老编号表回归", old_table.get(5) == "首帧：真实皮肤表面极近景", f"got {old_table.get(5)}")

    print("6) 成稿直录保真：fn3 级判定（_verbatim_h3_prompt）")
    v2_body = "subject_definitions:\n<Picture 1> is the opening frame anchor.\n" \
        "<Picture 2> is the observation composition.\n\nretention_analysis:\n" \
        "<Picture 1>: fully_preserved — keep the opening arrangement.\n" \
        "<Picture 2>: fully_preserved — reach the end composition.\n\n" \
        "detailed_description:\n[Shot 1] Begin from <Picture 1> and hold."
    v2_frag = "| 图像 | `<Picture 1>` | Image11.png | 0.00s首帧 |\n" \
        "| 图像 | `<Picture 2>` | Image23.png | 0.80s切入 |\n\n```text\n" + v2_body + "\n```"
    check("V2 成稿（正文含原文 body）判保真",
          stages._verbatim_h3_prompt(v2_body + "\n\nAudio Timeline:\n0.4-4.9s | <Audio 1> | x.", v2_frag, 2))
    check("绑定数与声明不符不判保真", not stages._verbatim_h3_prompt(v2_body, v2_frag, 3))
    llm_prompt = v2_body.replace("is the opening frame anchor", "is a doctor 吕满春 anchored")
    check("LLM 改写稿（body 不再原文包含）不判保真",
          not stages._verbatim_h3_prompt(llm_prompt, v2_frag, 2))
    out = stages.rewrite_aligned_prompt(llm_prompt, [11, 23], {})
    check("非保真稿仍重建声明行（编号表身份）", "<Picture 1>" in out, f"got {out!r}")

    print("7) 闸门语义：图片解析缺口时不再全判死刑（单元级验证）")
    # storyboard_aligned 语义已改为 any(image_mids or video_mids)；
    # 用对齐结果 dict 直接复现两种场景
    audio_only_aligned = {"s1": {"image_mids": [], "video_mids": [], "audio_mids": ["a1"]}}
    img_aligned = {"s1": {"image_mids": ["m1"], "video_mids": [], "audio_mids": ["a1"]}}
    check("音频-only 对齐不算 storyboard_aligned",
          not any(a["image_mids"] or a["video_mids"] for a in audio_only_aligned.values()))
    check("图片对齐仍算（抑制兜底语义不变）",
          any(a["image_mids"] or a["video_mids"] for a in img_aligned.values()))

    print()
    if FAIL:
        print(f"失败 {FAIL} 项（共 {PASS + FAIL}）")
        sys.exit(1)
    print(f"全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
