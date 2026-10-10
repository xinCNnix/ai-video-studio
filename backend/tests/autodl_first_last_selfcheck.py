"""autodl_first_last_selfcheck：AutoDL H3 首尾帧工作流接入离线自测（不联网、不花钱）。

背景：2026-10-01 接入 AutoDL「H3首尾帧生成视频」工作流（ID=minimax_h3_lightx2v，
官方入参 first_frame/last_frame 均必填，与多图参考工作流 ref_image_N 平铺布局不同）。
本测试锁定修复后的不变量：

  A. 请求体映射：首尾帧契约把 images[0]/last_images[0] 编码为 first_frame/last_frame，
     绝不混入 ref_image_N；多图参考工作流回归不受影响（仍平铺 ref_image_N）
  B. 契约闸门：不支持的模式的清晰报错、缺尾帧提前拦截（不打到云端才失败）
  C. 提交兜底：绕过 API 层直接调 submit 时，缺尾帧/缺参考图仍被 ProviderError 拦截
  D. 目录注入：VIDEO_MODELS 中该条目的 modes 与契约一致（前端按钮据此显隐）

运行：.venv/Scripts/python.exe tests/autodl_first_last_selfcheck.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="autodl_flf_check_")

from app.config import settings  # noqa: E402
from app.providers.video import autodl_comfyui  # noqa: E402
from app.providers.video.autodl_comfyui import AutoDLComfyUI, _build_body  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
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


def make_png(name: str) -> str:
    p = Path(tempfile.mkdtemp(prefix="flf_img_")) / name
    p.write_bytes(PNG)
    return str(p)


def is_png_data_url(v) -> bool:
    return isinstance(v, str) and v.startswith("data:image/png;base64,")


# ---------------- A. 请求体映射 ----------------

def t_body_first_last_mapping() -> None:
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_lightx2v"]
    first, last = make_png("first.png"), make_png("last.png")
    body = _build_body(
        {
            "prompt": "镜头从首帧缓缓推近到尾帧",
            "duration": 6,
            "resolution": "768p",
            "aspect_ratio": "9:16",
            "images": [first],
            "last_images": [last],
            "options": {"seed": 123, "prompt_optimizer": True},
        },
        spec,
    )
    check("映射: first_frame/last_frame 独立入参", is_png_data_url(body.get("first_frame"))
          and is_png_data_url(body.get("last_frame")), str(body.keys()))
    check("映射: 绝不混入 ref_image_N", not any(k.startswith("ref_image_") for k in body), str(body.keys()))
    check("映射: 画幅自动补方向", body.get("resolution") == "768p竖", str(body.get("resolution")))
    check("映射: seed 透传 + 厂商选项剔除", body.get("seed") == 123 and "prompt_optimizer" not in body, str(body))


def t_body_first_last_extra_images_ignored() -> None:
    """前端只传 [0]；若上层多传，适配器也只取首帧一张（AutoDL 侧只有两个入参位）。"""
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_lightx2v"]
    body = _build_body(
        {"prompt": "p", "images": [make_png("a.png"), make_png("b.png")],
         "last_images": [make_png("c.png"), make_png("d.png")]},
        spec,
    )
    check("映射: 多传图片只取各自首张",
          set(body) == {"prompt", "first_frame", "last_frame", "seed"}, str(sorted(body)))


def t_body_ref_layout_regression() -> None:
    spec = settings.AUTODL_WORKFLOW_SPECS["minimax_h3_lightx2v_v5_15s"]
    body = _build_body(
        {"prompt": "p", "images": [make_png("1.png"), make_png("2.png")],
         "last_images": [make_png("unused.png")]},
        spec,
    )
    check("回归: 多图工作流仍平铺 ref_image_N",
          is_png_data_url(body.get("ref_image_0")) and is_png_data_url(body.get("ref_image_1"))
          and "first_frame" not in body and "last_frame" not in body, str(body.keys()))


# ---------------- B. 契约闸门 ----------------

def t_contract_gate() -> None:
    flf = dict(prompt="p", images=["x.png"], videos=[], audios=[])
    err_text = video_service.autodl_contract_error("minimax_h3_lightx2v", "text", **flf)
    check("契约: 文生模式被拒并给出可用模式", "首尾帧" in (err_text or ""), str(err_text))
    err_ref = video_service.autodl_contract_error("minimax_h3_lightx2v", "reference", **flf)
    check("契约: 参考模式被拒", err_ref and "仅支持" in err_ref, str(err_ref))
    err_last = video_service.autodl_contract_error(
        "minimax_h3_lightx2v", "first_last", images=["x.png"], videos=[], audios=[],
        prompt="p", last_images=[],
    )
    check("契约: 缺尾帧提前拦截", err_last and "尾帧" in err_last, str(err_last))
    ok = video_service.autodl_contract_error(
        "minimax_h3_lightx2v", "first_last", images=["x.png"], videos=[], audios=[],
        prompt="p", last_images=["y.png"],
    )
    check("契约: 两帧齐备放行", ok is None, str(ok))
    # 多图参考工作流不受 last_images 校验影响（无该契约布局）
    ok2 = video_service.autodl_contract_error(
        "minimax_h3_lightx2v_v5_15s", "image", images=["x.png"], videos=[], audios=[], prompt="p")
    check("契约: 多图工作流不查尾帧", ok2 is None, str(ok2))


# ---------------- C. 提交兜底 ----------------

async def t_submit_bottom_line() -> None:
    prov = AutoDLComfyUI(api_key="k", base_url="http://127.0.0.1:9")
    try:
        await prov.submit({
            "model": "minimax_h3_lightx2v", "prompt": "p", "mode": "first_last",
            "images": [make_png("f.png")], "last_images": [],
        })
        check("兜底: 缺尾帧提交拦截", False, "未抛出 ProviderError")
    except ProviderError as e:
        check("兜底: 缺尾帧提交拦截", "尾帧" in str(e), str(e))
    try:
        await prov.submit({
            "model": "minimax_h3_lightx2v", "prompt": "p", "mode": "first_last",
            "images": [], "last_images": [make_png("l.png")],
        })
        check("兜底: 缺首帧提交拦截", False, "未抛出 ProviderError")
    except ProviderError as e:
        check("兜底: 缺首帧提交拦截", "参考图" in str(e), str(e))


# ---------------- D. 目录注入 ----------------

def t_catalog_injection() -> None:
    entry = next((m for m in settings.VIDEO_MODELS if m["id"] == "minimax_h3_lightx2v"), None)
    check("目录: 模型条目存在且 provider=autodl", entry and entry.get("provider") == "autodl", str(entry))
    check("目录: modes/契约字段已注入",
          entry.get("modes") == ["first_last"] and entry.get("min_images") == 1
          and entry.get("requires_prompt") is True, str(entry))
    check("目录: extra_fields 不下发前端", "extra_fields" not in entry, str(entry))


if __name__ == "__main__":
    t_body_first_last_mapping()
    t_body_first_last_extra_images_ignored()
    t_body_ref_layout_regression()
    t_contract_gate()
    asyncio.run(t_submit_bottom_line())
    t_catalog_injection()
    print(f"ALL PASS ({PASS} checks)")
