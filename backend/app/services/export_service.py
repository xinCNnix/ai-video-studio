"""成片导出服务：按分镜顺序拼接视频（自动混入各镜旁白），产出一条完整成片。

依赖系统 ffmpeg（PATH）。流程：
1. 收集有视频的分镜（按顺序），缺视频的分镜跳过并在结果中提示；
2. 逐镜归一化（统一分辨率/帧率/编码），分镜有旁白则混入音频（-shortest 对齐）；
3. concat demuxer 无损拼接 → outputs/video/ 下的成片文件。
"""
import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from ..config import settings
from . import file_service, project_service

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
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败：{stderr.decode(errors='ignore')[-400:]}")


def _local(url: str) -> Path | None:
    return file_service.resolve_local(url)


async def export_project(project_id: str) -> dict:
    project = project_service.get_project(project_id)
    if not project:
        raise RuntimeError("项目不存在")

    scenes = project.get("scenes", [])
    ready = [(i, s) for i, s in enumerate(scenes) if s.get("video")]
    skipped = [i + 1 for i, s in enumerate(scenes) if not s.get("video")]
    if not ready:
        raise RuntimeError("没有任何分镜已生成视频，无法导出成片")

    w, h = ASPECT_SIZE.get(project.get("aspect_ratio", "16:9"), (1280, 720))
    ff = ffmpeg_path()
    work = settings.TEMP_DIR / f"export_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    normalized: list[Path] = []
    try:
        for i, scene in ready:
            clip = _local(scene["video"])
            if not clip or not clip.exists():
                raise RuntimeError(f"分镜 {i + 1} 的视频文件缺失")
            out = work / f"norm_{i:03d}.mp4"
            cmd = [ff, "-y", "-i", str(clip)]
            voice = _local(scene["voice"]) if scene.get("voice") else None
            if voice:
                cmd += ["-i", str(voice), "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
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
    return {
        "url": url,
        "scenes_exported": len(normalized),
        "scenes_skipped": skipped,
        "project_name": project.get("name", ""),
    }
