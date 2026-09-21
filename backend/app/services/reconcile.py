"""声明式调谐器：期望资产 vs 实际资产差值驱动的生成控制循环。

每镜的"期望"由状态声明：有 narration → 期望 voice；i2v 且无用户参考图 → 期望
keyframe；有 prompt → 期望 video。调谐循环反复求差、逐项补齐，直到差距清零或
重试预算耗尽（转 failed 交人工）。提交幂等：活跃 job / 已有产物的项不重复提交，
重跑不重复烧钱。
"""
import asyncio

from ..harness.state import Project, Scene
from ..harness.watchdog import watchdog
from ..jobs import job_manager
from . import generation_service, project_store

MAX_ATTEMPTS = {"keyframe": 2, "voice": 2, "video": 2}
JOB_POLL_INTERVAL = 5


def scene_gaps(scene: Scene) -> tuple[list[str], str | None]:
    """计算一镜的资产差距。返回 (可执行差距列表, 不可执行的阻塞原因)。

    顺序即执行顺序：keyframe → voice → video；前置不满足时 video 不进列表。
    """
    if scene.assets.video and scene.status == "done":
        return [], None
    if not (scene.prompt.strip() or scene.narration.strip()):
        return [], None  # 空镜（用户手动加的占位）不参与调谐
    gaps: list[str] = []
    has_user_audio = bool(scene.binding.audios)
    has_ref_images = bool(scene.binding.images)
    block: str | None = None

    need_keyframe = scene.gen_mode == "i2v" and not scene.assets.keyframe and not has_ref_images
    if need_keyframe:
        if (scene.keyframe_prompt or scene.description).strip():
            gaps.append("keyframe")
        else:
            block = "i2v 镜缺少关键帧描述（keyframe_prompt/description），无法生成关键帧"

    need_voice = bool(scene.narration.strip()) and not scene.assets.voice and not has_user_audio
    if need_voice:
        gaps.append("voice")

    prereq_ok = not need_keyframe and not need_voice and block is None
    if (
        prereq_ok
        and scene.prompt.strip()
        and not scene.assets.video
        and not generation_service.job_active(scene.assets.video_job)
    ):
        gaps.append("video")
    return gaps, block


async def run_generate_stage(project_id: str, trace=None, cancel_check=None) -> dict:
    """调谐循环：逐镜逐差距补齐直到清零。返回执行摘要。

    - 每轮迭代心跳看门狗（防 single-flight 误判过期）
    - 取消检查：外部取消后立即返回（已在跑的 job 不中止，写回任务会自行落盘）
    - 重试预算耗尽的镜标记 failed 并附最后一次错误，交人工/agent 处理
    """
    summary = {"keyframe": 0, "voice": 0, "video": 0, "failed": [], "blocked": []}
    while True:
        if cancel_check and cancel_check():
            summary["cancelled"] = True
            return summary
        watchdog.heartbeat(project_id)
        project_dict = project_store.get(project_id)
        if not project_dict:
            summary["error"] = "项目不存在"
            return summary
        project = Project.model_validate(project_dict)

        target = None
        for scene in project.scenes:
            gaps, block = scene_gaps(scene)
            if block:
                summary["blocked"].append({"scene_id": scene.id, "title": scene.title, "reason": block})
                _mark_failed(project_id, scene.id, block)
                continue
            actionable = [g for g in gaps if scene.attempts.get(g, 0) < MAX_ATTEMPTS.get(g, 2)]
            if gaps and not actionable:
                reason = f"重试预算耗尽（{'/'.join(gaps)} 各尝试 {scene.attempts.get(gaps[0], 0)} 次）：{(scene.error or '未知错误')[:200]}"
                summary["failed"].append({"scene_id": scene.id, "title": scene.title, "reason": reason})
                _mark_failed(project_id, scene.id, reason)
                continue
            if actionable:
                target = (scene, actionable[0])
                break
        if target is None:
            return summary
        scene, gap = target
        if gap == "keyframe":
            r = await generation_service.generate_keyframe(project_id, scene.id, trace=trace)
        elif gap == "voice":
            r = await generation_service.generate_voice(project_id, scene.id, trace=trace)
        else:
            r = await generation_service.submit_scene_video(project_id, scene.id, trace=trace)
            if r.get("ok"):
                summary["video"] += 1
                await _wait_video_job(project_id, r["job_id"], cancel_check)
                continue
        if r.get("ok"):
            summary[gap] = summary.get(gap, 0) + 1
        else:
            # 失败已由 generation_service 记入 scene.error/attempts，这里只统计
            summary.setdefault("errors", []).append({"scene_id": scene.id, "gap": gap, "error": r.get("error", "")[:200]})


def _mark_failed(project_id: str, scene_id: str, reason: str) -> None:
    def fn(p: Project) -> None:
        s = next((x for x in p.scenes if x.id == scene_id), None)
        if s and s.status != "failed":
            s.status = "failed"
            s.error = reason[:300]
    try:
        project_store.mutate(project_id, fn)
    except Exception:  # noqa: BLE001 - 状态标记失败不中断调谐
        pass


async def _wait_video_job(project_id: str, job_id: str, cancel_check=None) -> None:
    """等待视频任务终态（写回由 generation_service 的后台任务完成，这里只等结果）。"""
    while True:
        if cancel_check and cancel_check():
            return
        watchdog.heartbeat(project_id)
        j = job_manager.get(job_id)
        if not j or j["status"] in ("completed", "failed"):
            return
        await asyncio.sleep(JOB_POLL_INTERVAL)
