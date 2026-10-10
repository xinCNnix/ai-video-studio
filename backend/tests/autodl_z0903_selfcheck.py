"""autodl_z0903_selfcheck：AutoDL「H3六图三音频生视频（高质量音画融合）」接入离线自测。

背景：2026-10-01 接入 AutoDL 工作流 minimax_h3_z0903（官方入参 ref_image_0..5 共 6 图位 +
ref_audio_0..2 共 3 音频位；分辨率枚举标签带像素后缀，如 768p横(1376*768)，共 8 档无 1:1）。
本测试锁定修复后的不变量：

  A. 请求体映射：6 图平铺 ref_image_0..5、音频按位编码、seed 透传
  B. 分辨率标签：resolution_labels 按画幅精确映射完整枚举标签；
     未命中（自定义档位）回退补方向后缀；无标签契约的工作流走原后缀逻辑（回归）
  C. 契约闸门：text 模式被拒、第 7 张图/第 4 段音频/参考视频被拒、缺图被拒
  D. 目录注入：modes / 素材上限随契约下发，画幅不含 1:1

运行：.venv/Scripts/python.exe tests/autodl_z0903_selfcheck.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="autodl_z0903_check_")

from app.config import settings  # noqa: E402
from app.providers.video.autodl_comfyui import _build_body  # noqa: E402
from app.services import video_service  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63fcffff3f0300050201"
    "00a5f6450000000049454e44ae426082"
)
WAV = b"RIFF" + b"\x00" * 36


def tmp_file(name: str, blob: bytes) -> str:
    p = Path(tempfile.mkdtemp(prefix="z0903_")) / name
    p.write_bytes(blob)
    return str(p)


def six_images() -> list[str]:
    return [tmp_file(f"img{i}.png", PNG) for i in range(6)]


# ---------------- A. 请求体映射 ----------------

def t_body_images_and_audios() -> None:
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_z0903"]
    imgs = six_images()
    auds = [tmp_file(f"a{i}.wav", WAV) for i in range(2)]
    body = _build_body(
        {
            "prompt": "六图三音频高质量音画融合",
            "duration": 8,
            "resolution": "768p",
            "aspect_ratio": "16:9",
            "images": imgs,
            "audios": auds,
            "options": {"seed": 42, "prompt_optimizer": True},
        },
        spec,
    )
    check("映射: 6 图平铺 ref_image_0..5",
          all(f"ref_image_{i}" in body for i in range(6)) and "ref_image_6" not in body,
          str(sorted(k for k in body if k.startswith("ref_"))))
    check("映射: 音频按位编码",
          str(body.get("ref_audio_0", "")).startswith("data:audio/") and "ref_audio_1" in body
          and "ref_audio_2" not in body, str(sorted(k for k in body if k.startswith("ref_audio"))))
    check("映射: seed 透传 + 厂商选项剔除", body.get("seed") == 42 and "prompt_optimizer" not in body, str(body))


# ---------------- B. 分辨率标签 ----------------

def t_resolution_labels() -> None:
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_z0903"]
    cases = [
        ("480p", "9:16", "480p竖(480*864)"),
        ("768p", "16:9", "768p横(1376*768)"),
        ("1088p", "16:9", "1088p横(1920*1088)"),
        ("1088p", "9:16", "1088p竖(1088*1920)"),
        ("1440p", "9:16", "1440p竖(1440*2560)"),
    ]
    for res, aspect, want in cases:
        body = _build_body({"prompt": "p", "resolution": res, "aspect_ratio": aspect}, spec)
        check(f"标签: {res}+{aspect} -> {want}", body.get("resolution") == want, str(body.get("resolution")))
    # 自定义档位未命中映射：回退补方向后缀（由 AutoDL 侧校验枚举）
    body = _build_body({"prompt": "p", "resolution": "999p", "aspect_ratio": "16:9"}, spec)
    check("标签: 未命中回退补后缀", body.get("resolution") == "999p横", str(body.get("resolution")))
    # auto 档不发 resolution（走云端默认 768p竖）
    body = _build_body({"prompt": "p", "resolution": "auto", "aspect_ratio": "16:9"}, spec)
    check("标签: auto 不发送", "resolution" not in body, str(body))


def t_resolution_suffix_regression() -> None:
    """无 resolution_labels 契约（旧多图工作流）保持原补后缀逻辑。"""
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_lightx2v_v5_15s"]
    body = _build_body({"prompt": "p", "resolution": "768p", "aspect_ratio": "16:9"}, spec)
    check("回归: 旧工作流补后缀", body.get("resolution") == "768p横", str(body.get("resolution")))
    body = _build_body({"prompt": "p", "resolution": "480p", "aspect_ratio": "1:1"}, spec)
    check("回归: 旧工作流 1:1 后缀", body.get("resolution") == "480p(1:1)", str(body.get("resolution")))


# ---------------- C. 契约闸门 ----------------

def t_contract_gate() -> None:
    ok = video_service.autodl_contract_error(
        "minimax_h3_z0903", "image", six_images(), [], [], prompt="p")
    check("契约: 6 图 image 模式放行", ok is None, str(ok))
    ok = video_service.autodl_contract_error(
        "minimax_h3_z0903", "reference", six_images()[:1],
        [], [tmp_file("a.wav", WAV)] * 3, prompt="p")
    check("契约: reference 模式 1 图 3 音频放行", ok is None, str(ok))
    err = video_service.autodl_contract_error("minimax_h3_z0903", "text", [], [], [], prompt="p")
    check("契约: text 模式被拒", err and "仅支持" in err, str(err))
    err = video_service.autodl_contract_error(
        "minimax_h3_z0903", "image", six_images() + [tmp_file("x.png", PNG)], [], [], prompt="p")
    check("契约: 第 7 张图被拒", err and "最多支持 6 张" in err, str(err))
    err = video_service.autodl_contract_error(
        "minimax_h3_z0903", "image", [], [], [], prompt="p")
    check("契约: 缺参考图被拒", err and "至少 1 张" in err, str(err))
    err = video_service.autodl_contract_error(
        "minimax_h3_z0903", "reference", six_images()[:1],
        [], [tmp_file("a.wav", WAV)] * 4, prompt="p")
    check("契约: 第 4 段音频被拒", err and "最多支持 3 段" in err, str(err))
    err = video_service.autodl_contract_error(
        "minimax_h3_z0903", "reference", six_images()[:1], ["v.mp4"], [], prompt="p")
    check("契约: 参考视频被拒", err and "不支持参考视频" in err, str(err))


# ---------------- D. 目录注入 ----------------

def t_catalog_injection() -> None:
    entry = next((m for m in settings.VIDEO_MODELS if m["id"] == "minimax_h3_z0903"), None)
    check("目录: 条目存在且 provider=autodl", entry and entry.get("provider") == "autodl", str(entry))
    check("目录: 契约字段已注入",
          entry.get("modes") == ["image", "reference"] and entry.get("max_images") == 6
          and entry.get("max_audios") == 3 and entry.get("min_images") == 1, str(entry))
    check("目录: 画幅无 1:1（工作流无该档）", "1:1" not in entry.get("aspect_ratios", []), str(entry))
    check("目录: 分辨率含 1088p/1440p 新档",
          {"1088p", "1440p"} <= set(entry.get("resolutions", [])), str(entry))


if __name__ == "__main__":
    t_body_images_and_audios()
    t_resolution_labels()
    t_resolution_suffix_regression()
    t_contract_gate()
    t_catalog_injection()
    print(f"ALL PASS ({PASS} checks)")
