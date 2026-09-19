"""成片导出服务（v2）：按分镜顺序拼接视频（自动混入各镜旁白），产出完整成片。

依赖系统 ffmpeg（PATH）。流程：
1. 收集有视频的分镜（按顺序），缺视频的分镜跳过并在结果中提示；
2. 逐镜归一化（统一分辨率/帧率/编码），无旁白的分镜补静音轨（concat -c copy
   要求各片段音轨一致——历史上"部分分镜有旁白"的项目导出即失败，已修复）；
3. concat demuxer 无损拼接 → outputs/video/ 下的成片文件 → project_store 写回。
"""
import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from ..config import settings
from . import file_service

ASPECT_SIZE = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (960, 960),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
}


def ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("未找到 ffmpeg：请安装 ffmpeg 并加入 PATH 后重试。")
    return p


async def _run_ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg 执行超时（10 分钟），已终止") from None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败：{stderr.decode(errors='ignore')[-400:]}")


def _local(url: str) -> Path | None:
    return file_service.resolve_local(url)


async def export_project_v2(project_id: str) -> dict:
    """v2 项目导出：归一化（含静音轨修复）→ concat → 写回 final_video。"""
    from ..harness.state import Project
    from . import project_store

    project_dict = project_store.get(project_id)
    if not project_dict:
        raise RuntimeError("项目不存在")
    project = Project.model_validate(project_dict)
    scenes = project.scenes
    ready = [(i, s) for i, s in enumerate(scenes) if s.assets.video]
    skipped = [i + 1 for i, s in enumerate(scenes) if not s.assets.video]
    if not ready:
        raise RuntimeError("没有任何分镜已生成视频，无法导出成片")

    w, h = ASPECT_SIZE.get(project.aspect_ratio, (1280, 720))
    ff = ffmpeg_path()
    work = settings.TEMP_DIR / f"export_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    normalized: list[Path] = []
    try:
        for i, scene in ready:
            clip = _local(scene.assets.video or "")
            if not clip or not clip.exists():
                raise RuntimeError(f"分镜 {i + 1} 的视频文件缺失")
            out = work / f"norm_{i:03d}.mp4"
            cmd = [ff, "-y", "-i", str(clip)]
            voice = _local(scene.assets.voice or "") if scene.assets.voice else None
            if voice:
                cmd += ["-i", str(voice), "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            else:
                # 无旁白分镜补静音轨：所有归一化片段统一带 AAC 音轨，
                # concat -c copy 才不会因音轨有无不一致而失败/错位
                cmd += [
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                ]
            cmd += [
                "-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                "-r", "30",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100",
                str(out),
            ]
            await _run_ffmpeg(cmd)
            normalized.append(out)

        listfile = work / "list.txt"
        listfile.write_text(
            "\n".join(f"file '{p.as_posix().replace(chr(39), chr(39) * 2)}'" for p in normalized),
            encoding="utf-8",
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"aistudio_final_{stamp}_{uuid.uuid4().hex[:4]}.mp4"
        final_tmp = work / final_name
        await _run_ffmpeg([
            ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(final_tmp),
        ])
        out_dir = settings.OUTPUTS_DIR / "video"
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / final_name
        shutil.move(str(final_tmp), str(final_path))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    url = f"/api/files/outputs/video/{final_name}"
    project_store.mutate(project_id, lambda p: setattr(p, "final_video", url))
    return {
        "url": url,
        "scenes_exported": len(normalized),
        "scenes_skipped": skipped,
        "project_name": project.name,
    }
