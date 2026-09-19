"""服务重启后的视频任务恢复：接着轮询云端已提交的生成，结果照常落盘写回分镜。

复现事故：视频提交后在云端正常生成，本地服务一重启，任务被标成 failed——
钱花了、片生成好了，却永远取不回来。视频 provider 的 poll 只依赖
provider_task_id（已随任务文件持久化），重启后完全可以续上：
轮询 → 下载到 outputs → 按提交时的 Prompt 指纹经 project_store 写回分镜。
图片/TTS 任务没有可轮询的云端任务号，维持「重启即失败请重试」。
"""
import asyncio

from ..config import settings
from ..jobs import job_manager
from ..jobs.manager import ACTIVE_STATUSES
from ..providers import registry
from ..services import file_service, generation_service, self_check, video_service


async def _recover_video_job(job: dict) -> None:
    jid = job["id"]
    params = job.get("params") or {}
    try:
        provider = registry.get_video(job.get("provider") or "", None)
    except Exception as exc:  # noqa: BLE001
        job_manager.update(jid, status="failed", error=f"重启恢复失败：无法构造视频服务：{str(exc)[:150]}")
        return
    job_manager.update(jid, status="generating")
    waited = 0.0
    while True:
        try:
            status, url, error = await provider.poll(job["provider_task_id"])
        except Exception as exc:  # noqa: BLE001
            job_manager.update(jid, status="failed", error=f"重启恢复轮询失败：{str(exc)[:200]}")
            return
        if status == "success" and url:
            break
        if status == "failed":
            job_manager.update(jid, status="failed", error=error or "视频生成失败（重启恢复轮询）")
            return
        if waited >= video_service.MAX_POLL_SECONDS:
            job_manager.update(
                jid, status="failed",
                error="重启恢复轮询超时（云端可能仍在生成，可稍后重试该任务）",
            )
            return
        await asyncio.sleep(video_service.POLL_INTERVAL)
        waited += video_service.POLL_INTERVAL
    try:
        job_manager.update(jid, status="downloading")
        output = await file_service.download_to_outputs("video", url)
        local_path = settings.OUTPUTS_DIR / "video" / output.rsplit("/", 1)[-1]
        content = local_path.read_bytes() if local_path.exists() else b""
        job_manager.update(
            jid, status="completed", output=output, outputs=[output],
            self_check=self_check.check_video(
                content, requested_duration=int(params.get("duration") or 0) or None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        job_manager.update(jid, status="failed", error=f"重启恢复下载失败：{str(exc)[:200]}")
        return
    # 写回分镜（v2）：与提交路径同一份指纹校验逻辑
    if params.get("project_id") and params.get("scene_id"):
        note = generation_service.writeback_completed_video(
            params["project_id"], params["scene_id"], output,
            str(params.get("prompt_fingerprint") or ""),
        )
        if note:
            job_manager.update(jid, writeback_note=note)


async def recover_interrupted_jobs() -> int:
    """启动钩子：为重启前仍在生成中的视频任务重新挂上轮询协程。返回恢复数量。"""
    targets = []
    for j in job_manager.list(limit=100000):
        if j.get("type") != "video" or not j.get("provider_task_id"):
            continue
        if j.get("status") in ACTIVE_STATUSES:
            targets.append(j)
        elif j.get("status") == "failed" and "服务重启" in str(j.get("error") or ""):
            job_manager.update(j["id"], error=None)
            targets.append(j)
    for j in targets:
        job_manager.attach(j["id"], _recover_video_job(j))
    return len(targets)
