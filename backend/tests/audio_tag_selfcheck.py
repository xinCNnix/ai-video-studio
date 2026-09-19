"""整改自检：参考标签编号契约（<Audio/Picture N> ↔ 单次请求实际传入素材）。

复现事故（2026-09-17 吕满春项目）：分镜 Prompt 沿用全片全局音频编号
（第 k 镜写 <Audio k>），而每镜视频请求只带该镜自己的 1 段音频——
悬空标签让 H3 找不到指定素材，成片配音与台词完全错乱。

在临时目录里跑，不触碰 data/ 真实数据。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vga_autiotag_"))
settings.JOBS_DIR = tmp / "jobs"
settings.PROJECTS_DIR = tmp / "projects"
settings.OUTPUTS_DIR = tmp / "outputs"
for d in (settings.JOBS_DIR, settings.PROJECTS_DIR, settings.OUTPUTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

from app.services.agent_service import (  # noqa: E402
    _Agent, _assemble_video_request, _audio_tags_in_order, _renumber_audio_tags,
)
from app.services import project_service  # noqa: E402

VMODEL = {"id": "minimax_h3_zm_u24", "durations": [4, 8, 15], "label": "H3"}


def scene_with(prompt: str, audios: list[str] | None = None, images: list[str] | None = None) -> dict:
    return {
        "id": "scene_t1", "title": "t", "description": "", "prompt": prompt,
        "narration": "", "gen_mode": "t2v", "keyframe_prompt": "",
        "image": None, "voice": None, "video": None, "duration": 5,
        "ref_images": images or [], "ref_videos": [], "ref_audios": audios or [],
    }


async def main() -> None:
    # ---- 1. 纯函数：全局编号重编 / 正确编号幂等 ----
    np_, note = _renumber_audio_tags("Reuse <Audio 2> exactly. <Audio 2> is clean.", ["a.mp3"])
    assert np_ == "Reuse <Audio 1> exactly. <Audio 1> is clean.", np_
    assert note and "<Audio 2>→<Audio 1>" in note
    np2, note2 = _renumber_audio_tags("Reuse <Audio 1> exactly.", ["a.mp3"])
    assert np2 == "Reuse <Audio 1> exactly." and note2 is None
    np3, _ = _renumber_audio_tags("<Audio 1> at 0-3s; <Audio 3> at 3-6s.", ["a.mp3", "b.mp3"])
    assert _audio_tags_in_order(np3) == [1, 2], np3
    print("PASS 1: 全局编号重编为按传入素材连续编号，正确编号不动")

    # ---- 2. 事故形态：每镜只传 1 段音频、Prompt 却写 <Audio 2/3/4> ----
    for tag in (2, 3, 4):
        req = _assemble_video_request(
            scene_with(f"speaks <Audio {tag}> with lip sync",
                       audios=["x_台词.mp3"], images=["u_角色.png"]), VMODEL)
        assert req["contract_error"] is None, req["contract_error"]
        assert _audio_tags_in_order(req["prompt"]) == [1], req["prompt"]
        assert "<Audio 1>" in req["prompt"] and f"<Audio {tag}>" not in req["prompt"]
    print("PASS 2: 事故形态（全局 <Audio 2/3/4> + 单段音频）自动重编为 <Audio 1>")

    # ---- 3. 悬空拦截：标签多于传入音频 / 有标签零音频 / 图片悬空 ----
    r = _assemble_video_request(
        scene_with("<Audio 1> and <Audio 2>", audios=["a.mp3"], images=["u_角色.png"]), VMODEL)
    assert r["contract_error"] and "2 个音频标签" in r["contract_error"]
    r2 = _assemble_video_request(
        scene_with("speaks <Audio 1>", images=["u_角色.png"]), VMODEL)
    assert r2["contract_error"] and "没有音频随请求传入" in r2["contract_error"]
    r3 = _assemble_video_request(
        scene_with("<Picture 3> is the doctor", images=["a.png", "b.png"]), VMODEL)
    assert r3["contract_error"] and "<Picture 3>" in r3["contract_error"]
    print("PASS 3: 悬空标签（音频超额/零音频/图片越界）一律拦截并给出指引")

    # ---- 4. 提交链路：任务参数里的 Prompt 预览用的是重编稿，指纹仍是项目原稿 ----
    project_service.save_project("project_tagtest", {
        "id": "project_tagtest", "name": "tag-test", "aspect_ratio": "16:9",
        "scenes": [scene_with("Xiao Mo delivers <Audio 2> with mouth sync.",
                              audios=["u_想知道.mp3"], images=["u_小墨.png", "u_医生.png"])],
    })
    agent = _Agent({"project_id": "project_tagtest"})
    res = await agent.generate_scene_video({"index": 0})
    assert res.get("ok"), res
    from app.jobs import job_manager  # noqa: E402
    job = job_manager.get(res["job_id"])
    preview = job["params"]["prompt"]
    assert "<Audio 1>" in preview and "<Audio 2>" not in preview, preview
    fp = job["params"]["prompt_fingerprint"]
    from app.services.agent_service import _prompt_fingerprint  # noqa: E402
    scene_now = project_service.get_project("project_tagtest")["scenes"][0]
    assert fp == _prompt_fingerprint(scene_now["prompt"]), "指纹必须对齐项目原稿（写回校验语义）"
    assert scene_now["prompt"].startswith("Xiao Mo delivers <Audio 2>"), "项目文件不得被改写"
    print("PASS 4: 提交用重编稿、指纹与项目原稿对齐、项目文件不被改写")

    # ---- 5. Studio 手动路径：悬空标签 400 拦截（不代改，只报错）----
    from app.services import video_service  # noqa: E402
    err = video_service.reference_tag_error(
        "minimax_h3_zm_u24", "speaks <Audio 2> with lip sync",
        images=["a.png"], videos=[], audios=["a.mp3"])
    assert err and "<Audio 2>" in err and "<Audio 1>" in err, err
    err2 = video_service.reference_tag_error(
        "minimax_h3_zm_u24", "speaks <Audio 1>", images=["a.png"], videos=[], audios=[])
    assert err2 and "0 个参考音频" in err2, err2
    ok = video_service.reference_tag_error(
        "minimax_h3_zm_u24", "speaks <Audio 1>", images=["a.png"], videos=[], audios=["a.mp3"])
    assert ok is None, ok
    fl = video_service.reference_tag_error(
        "minimax_h3_zm_u24", "Picture 1 aligns 0.00s; Picture 2 aligns 4.00s",
        images=["a.png", "b.png"], videos=[], audios=[])
    assert fl is None, fl
    non_h3 = video_service.reference_tag_error(
        "seedance", "speaks <Audio 9>", images=[], videos=[], audios=[])
    assert non_h3 is None, "非 H3 模型不做标签校验"
    print("PASS 5: 手动路径悬空标签拦截、合法编号/首尾帧/非H3不误伤")

    print("\nALL AUDIO TAG CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
