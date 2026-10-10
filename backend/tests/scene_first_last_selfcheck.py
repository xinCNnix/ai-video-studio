"""scene_first_last_selfcheck：分镜级首尾帧生成模式离线自测（不联网、不花钱）。

背景：2026-10-01 Studio 支持按镜覆盖生视频方式——默认参考生视频不变，特殊分镜
可单独选「首尾帧」（gen_mode=first_last：首帧=关键帧/first_frame 绑定，尾帧=
last_frame 绑定，不传参考素材）。本测试锁定核心不变量：

  A. 状态模型：last_frame 角色可表示、重复尾帧被不变量拒绝；gen_mode 可为 first_last
  B. 组装：首尾帧分支产出 mode/images/last_images；缺首帧/缺尾帧/参考互斥/悬空标签
     均给出可操作报错；参考模式回归不受影响
  C. 模型回退：项目模型不支持首尾帧时按镜换用同厂首尾帧模型；支持时不换
  D. 调谐：first_last 缺尾帧→阻塞；参考互斥→阻塞；齐备→keyframe/video 正常缺口

运行：.venv/Scripts/python.exe tests/scene_first_last_selfcheck.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="scene_flf_check_")

from app.harness.state import (  # noqa: E402
    ImageBinding, Material, Project, Scene, SceneBinding, check_invariants,
)
from app.config import settings  # noqa: E402
from app.services import generation_service  # noqa: E402
from app.services.reconcile import scene_gaps  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


def make_project(video_model: str = "minimax_h3_zm_u24") -> Project:
    return Project(id="p1", name="首尾帧自测", video_model_test=None) if False else Project(
        id="p1", name="首尾帧自测",
        settings={"video_model": video_model},  # type: ignore[arg-type]
    )


def make_scene(gen_mode: str = "first_last", **kw) -> Scene:
    defaults = dict(id="scene_1", title="镜1", prompt="A cinematic shot.", duration=5, gen_mode=gen_mode)  # type: ignore[arg-type]
    defaults.update(kw)
    return Scene(**defaults)


def mat(mid: str, kind: str = "image", url: str | None = None) -> Material:
    return Material(id=mid, kind=kind, url=url or f"/api/files/uploads/{mid}.png")  # type: ignore[arg-type]


# ---------------- A. 状态模型 ----------------

def t_state_model() -> None:
    p = make_project()
    p.materials = [mat("m1"), mat("m2"), mat("m3")]
    p.scenes = [make_scene(binding=SceneBinding(images=[
        ImageBinding(material_id="m1", role="first_frame"),
        ImageBinding(material_id="m2", role="last_frame"),
        ImageBinding(material_id="m3"),  # reference
    ]))]
    check("状态: first_last + 三角色绑定合法", check_invariants(p) == [], str(check_invariants(p)))
    p.scenes[0].binding.images.append(ImageBinding(material_id="m3", role="last_frame"))
    p.scenes[0].binding.images[0] = ImageBinding(material_id="m1")  # 避免重复素材触发另一不变量
    p.scenes[0].binding.images[-1] = ImageBinding(material_id="m3", role="last_frame")  # type: ignore[call-arg]
    v = check_invariants(p)
    check("状态: 重复尾帧被拒", any("尾帧角色绑定多于一个" in x for x in v), str(v))


# ---------------- B. 组装 ----------------

def vmodel_of(model_id: str) -> dict:
    return next(m for m in settings.VIDEO_MODELS if m["id"] == model_id)


def t_assemble_first_last() -> None:
    p = make_project()
    mats = {m.id: m for m in [mat("m1"), mat("m2")]}
    sc = make_scene(
        assets={"keyframe": "/api/files/outputs/image/kf.png"},  # type: ignore[arg-type]
        binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")]),
    )
    req = generation_service.assemble_video_request(sc, mats, vmodel_of("MiniMax-H3"))
    check("组装: 首尾帧 mode/images/last_images",
          req["mode"] == "first_last" and req["images"] == ["/api/files/outputs/image/kf.png"]
          and req["last_images"] == ["/api/files/uploads/m2.png"], str(req))
    check("组装: 首尾帧不传参考音视频", req["ref_audios"] == [] and req["ref_videos"] == [], str(req))

    # 首帧走 first_frame 绑定（无关键帧产物）也可
    sc2 = make_scene(binding=SceneBinding(images=[
        ImageBinding(material_id="m1", role="first_frame"),
        ImageBinding(material_id="m2", role="last_frame"),
    ]))
    req2 = generation_service.assemble_video_request(sc2, mats, vmodel_of("MiniMax-H3"))
    check("组装: 首帧可来自绑定", req2["images"] == ["/api/files/uploads/m1.png"], str(req2.get("images")))


def t_assemble_errors() -> None:
    mats = {m.id: m for m in [mat("m1"), mat("m2"), mat("m3")]}
    flf_model = vmodel_of("MiniMax-H3")
    # 缺尾帧
    req = generation_service.assemble_video_request(
        make_scene(assets={"keyframe": "/kf.png"}), mats, flf_model)  # type: ignore[arg-type]
    check("组装: 缺尾帧报错", "缺少尾帧" in (req.get("contract_error") or ""), str(req.get("contract_error")))
    # 缺首帧（有尾帧）
    req = generation_service.assemble_video_request(
        make_scene(binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")])),
        mats, flf_model)
    check("组装: 缺首帧报错", "缺少首帧" in (req.get("contract_error") or ""), str(req.get("contract_error")))
    # 参考互斥
    req = generation_service.assemble_video_request(
        make_scene(
            assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
            binding=SceneBinding(images=[
                ImageBinding(material_id="m2", role="last_frame"),
                ImageBinding(material_id="m3"),
            ])),
        mats, flf_model)
    check("组装: 参考图互斥报错", "不能绑定参考图" in (req.get("contract_error") or ""), str(req.get("contract_error")))
    # Audio/Picture 标签
    req = generation_service.assemble_video_request(
        make_scene(prompt="... <Audio 1> ... <Picture 2> ...",
                   assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
                   binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")])),
        mats, flf_model)
    check("组装: 音频标签报错", "首尾帧模式不随请求传参考音频" in (req.get("contract_error") or ""), str(req.get("contract_error")))
    req = generation_service.assemble_video_request(
        make_scene(prompt="... <Picture 2> ...",
                   assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
                   binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")])),
        mats, flf_model)
    check("组装: Picture 标签报错", "不传编号参考素材" in (req.get("contract_error") or ""), str(req.get("contract_error")))
    # 尾帧绑定挂在非首尾帧镜（有正常首帧来源）：不报错但给说明
    sc = make_scene(gen_mode="i2v", prompt="...",
                    assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
                    binding=SceneBinding(images=[ImageBinding(material_id="m1", role="last_frame")]))
    req = generation_service.assemble_video_request(sc, mats, vmodel_of("minimax_h3_zm_u24"))
    check("组装: 非首尾帧镜尾帧绑定不生效提示",
          not req.get("contract_error") and any("不生效" in n for n in req["notes"]),
          str((req.get("contract_error"), req.get("notes"))))


def t_assemble_reference_regression() -> None:
    mats = {m.id: m for m in [mat("m1"), mat("m2")]}
    sc = make_scene(gen_mode="i2v",
                    assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
                    binding=SceneBinding(images=[ImageBinding(material_id="m2")]))
    req = generation_service.assemble_video_request(sc, mats, vmodel_of("minimax_h3_zm_u24"))
    check("回归: i2v 关键帧+参考模式不变",
          req["mode"] == "reference" and req["last_images"] == []
          and req["images"] == ["/kf.png", "/api/files/uploads/m2.png"], str(req))


# ---------------- C. 模型回退 ----------------

def t_model_fallback() -> None:
    p = make_project("minimax_h3_zm_u24")  # autodl 多图工作流：无 first_last
    m, note = generation_service.resolve_scene_video_model(p, make_scene())
    check("回退: 同厂首尾帧工作流", m["id"] == "minimax_h3_lightx2v" and note and "自动改用" in note, f"{m['id']} {note}")
    p2 = make_project("doubao-seedance-2-5-260628")  # 目录自带 first_last 的模型
    m2, note2 = generation_service.resolve_scene_video_model(p2, make_scene())
    check("回退: 项目模型支持时不换", m2["id"] == "doubao-seedance-2-5-260628" and note2 is None, f"{m2['id']} {note2}")
    p3 = make_project("minimax_h3_zm_u24")
    m3, note3 = generation_service.resolve_scene_video_model(p3, make_scene(gen_mode="i2v"))
    check("回退: 非首尾帧镜严格用项目模型", m3["id"] == "minimax_h3_zm_u24" and note3 is None, f"{m3['id']} {note3}")


# ---------------- D. 调谐缺口 ----------------

def t_reconcile_gaps() -> None:
    mats_ids = ["m1", "m2", "m3"]
    # 缺尾帧 → 阻塞
    sc = make_scene(assets={"keyframe": "/kf.png"})  # type: ignore[arg-type]
    gaps, block = scene_gaps(sc, "p1")
    check("调谐: 缺尾帧阻塞", block and "缺少尾帧" in block, str((gaps, block)))
    # 参考互斥 → 阻塞
    sc = make_scene(binding=SceneBinding(images=[
        ImageBinding(material_id="m2", role="last_frame"),
        ImageBinding(material_id="m3"),
    ]))
    gaps, block = scene_gaps(sc, "p1")
    check("调谐: 参考互斥阻塞", block and "不能绑定参考图" in block, str((gaps, block)))
    # 齐备（关键帧+尾帧）→ video 缺口、无阻塞
    sc = make_scene(
        assets={"keyframe": "/kf.png"},  # type: ignore[arg-type]
        binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")]),
    )
    gaps, block = scene_gaps(sc, "p1")
    check("调谐: 齐备后 video 正常缺口", block is None and gaps == ["video"], str((gaps, block)))
    # 无关键帧但有尾帧 → keyframe 缺口（首帧与 i2v 同源；video 等首帧补齐后才进列表）
    sc = make_scene(binding=SceneBinding(images=[ImageBinding(material_id="m2", role="last_frame")]),
                    keyframe_prompt="opening frame")
    gaps, block = scene_gaps(sc, "p1")
    check("调谐: 首帧走 keyframe 生成", block is None and gaps == ["keyframe"], str((gaps, block)))


if __name__ == "__main__":
    t_state_model()
    t_assemble_first_last()
    t_assemble_errors()
    t_assemble_reference_regression()
    t_model_fallback()
    t_reconcile_gaps()
    print(f"ALL PASS ({PASS} checks)")
