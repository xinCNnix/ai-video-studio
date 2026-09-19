"""视频生成任务执行服务：提交 -> 轮询 -> 下载。"""
import asyncio
import re

from ..config import settings
from ..jobs import job_manager
from ..providers import registry
from . import file_service, self_check

POLL_INTERVAL = 5
MAX_POLL_SECONDS = 20 * 60

_MODE_LABELS = {
    "text": "文生视频",
    "image": "图生视频",
    "reference": "参考素材生成",
    "first_last": "首尾帧",
}


def reference_tag_error(model: str, prompt: str, images: list, videos: list,
                        audios: list) -> str | None:
    """H3 参考标签悬空校验（Studio 手动路径用；Agent 路径已在组装层自动重编号）。

    H3 标签按**本次请求实际传入的素材**顺序编号：第 1 张图 = <Picture 1>、
    第 1 段音频 = <Audio 1>。悬空标签（编号超过传入数，或有音频标签却没传音频）
    会让模型找不到指定素材、自行编造语音/画面（复现事故：全片全局编号 <Audio 2/3/4>
    配单段音频，成片配音与台词完全错乱且自检全绿）。手动路径不代用户改写 Prompt，
    只拦截并说明编号规则。非 H3 系模型不校验（标签语义是 H3 特有的）。
    """
    spec = settings.AUTODL_WORKFLOW_SPECS.get(model)
    if spec is None and "h3" not in str(model or "").lower():
        return None
    kinds = {"Picture": "图", "Video": "视频", "Audio": "音频"}
    for label, passed in (("Picture", images), ("Video", videos), ("Audio", audios)):
        nums = [int(n) for n in re.findall(rf"<{label}\s+(\d+)>", prompt or "")]
        if nums and max(nums) > len(passed):
            return (
                f"Prompt 引用了 <{label} {max(nums)}>，但本次只传入了 {len(passed)} 个参考"
                f"{kinds[label]}：H3 标签按本次请求传入的素材顺序编号（第 1 个 = <{label} 1>，"
                f"逐个对应），悬空标签会让模型自行编造内容。请核对附件数量/顺序与标签编号，"
                "或去掉多余标签"
            )
    return None


def autodl_contract_error(model: str, mode: str, images: list, videos: list, audios: list,
                          prompt: str | None = None) -> str | None:
    """AutoDL 工作流入参契约校验（settings.AUTODL_WORKFLOW_SPECS 为单一事实来源）。

    返回中文错误信息，None 表示通过。非 AutoDL 模型（无契约）直接通过。
    在建任务/提交前调用，避免请求打到 AutoDL 才被工作流报错。
    契约数据已与 AutoDL 官方 input_rules 逐一核对（2026-09-10）。
    """
    spec = settings.AUTODL_WORKFLOW_SPECS.get(model)
    if not spec:
        return None
    if spec.get("requires_prompt", True) and not (prompt or "").strip():
        return "该工作流必须填写视频 Prompt（AutoDL 侧为必填参数）"
    if mode not in spec["modes"]:
        allowed = "/".join(_MODE_LABELS.get(m, m) for m in spec["modes"])
        return f"该工作流不支持{_MODE_LABELS.get(mode, mode)}模式，仅支持：{allowed}"
    need = int(spec.get("min_images") or 0)
    if len(images or []) < need:
        return f"该工作流必须携带至少 {need} 张参考图（仅传音频/视频或纯文本会被工作流拒绝）"
    max_images = spec.get("max_images")
    if max_images is not None and len(images or []) > max_images:
        if max_images == 0:
            return "该工作流为纯文生视频，不支持参考图，请移除后重试"
        return f"该工作流最多支持 {max_images} 张参考图（当前 {len(images)} 张）"
    max_audios = spec.get("max_audios")
    if audios and not spec.get("accepts_audios"):
        return "该工作流不支持参考音频，请移除后重试"
    if max_audios is not None and len(audios or []) > max_audios:
        return f"该工作流最多支持 {max_audios} 段参考音频（当前 {len(audios)} 段）"
    if videos and not spec.get("accepts_videos"):
        return "该工作流不支持参考视频，请移除后重试"
    return None


async def run_video_job(job_id: str, provider: str, model: str, credential_id: str | None,
                        mode: str, prompt: str, duration: int | None, aspect_ratio: str | None,
                        resolution: str | None, images: list[str], last_images: list[str],
                        videos: list[str], audios: list[str], options: dict) -> None:
    jm = job_manager
    jm.update(job_id, status="submitted")
    video_provider = registry.get_video(provider, credential_id)

    async def materialize_all(refs: list[str]) -> list[str]:
        local = []
        for ref in refs:
            path = await file_service.materialize(ref)
            local.append(str(path))
        return local

    local_images = await materialize_all(images)
    local_last_images = await materialize_all(last_images)
    local_videos = await materialize_all(videos)
    local_audios = await materialize_all(audios)

    task_id = await video_provider.submit(
        {
            "model": model,
            "prompt": prompt,
            "mode": mode,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "images": local_images,
            "last_images": local_last_images,
            "videos": local_videos,
            "audios": local_audios,
            "options": options or {},
        }
    )
    jm.update(job_id, provider_task_id=task_id, status="generating")

    waited = 0.0
    while True:
        status, url, error = await video_provider.poll(task_id)
        if status == "success" and url:
            break
        if status == "failed":
            raise RuntimeError(error or "视频生成失败")
        if waited >= MAX_POLL_SECONDS:
            raise RuntimeError("视频生成超时，请稍后在历史中查看或重试。")
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    jm.update(job_id, status="downloading")
    output = await file_service.download_to_outputs("video", url)
    # 读取已落盘文件做基础自检（无需二次下载）
    from ..config import settings as cfg

    local_path = cfg.OUTPUTS_DIR / "video" / output.rsplit("/", 1)[-1]
    content = local_path.read_bytes() if local_path.exists() else b""
    jm.update(
        job_id,
        status="completed",
        output=output,
        outputs=[output],
        self_check=self_check.check_video(content, requested_duration=int(duration) if duration else None),
    )
