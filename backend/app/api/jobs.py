"""生成任务查询 API。"""
import re
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import settings
from ..jobs import job_manager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(type: str | None = None, status: str | None = None, limit: int = 30):
    return {"jobs": job_manager.list(jtype=type, status=status, limit=limit)}


@router.get("/batch-download/zip")
async def batch_download_zip(type: str = "tts", limit: int = 100, ids: str | None = None):
    """打包任务的产物为 zip。传 ids（逗号分隔）时只打包指定任务，否则打包最近已完成的任务。"""
    if ids:
        job_list = []
        for jid in [i.strip() for i in ids.split(",") if i.strip()][:200]:
            j = job_manager.get(jid)
            if j and j.get("status") == "completed" and j.get("output"):
                job_list.append(j)
        job_list.sort(key=lambda j: str(j.get("created_at", "")), reverse=True)
        jobs = job_list
    else:
        if type not in ("tts", "image", "video"):
            raise HTTPException(status_code=400, detail="type 必须是 tts / image / video")
        jobs = job_manager.list(jtype=type, status="completed", limit=max(1, min(limit, 200)))
    pairs = []
    for j in reversed(jobs):  # 时间正序打包
        out = j.get("output")
        if not out:
            continue
        local = _local_path(out)
        if local and local.exists():
            pairs.append((j, local))
    if not pairs:
        raise HTTPException(status_code=404, detail="没有可下载的已完成任务")
    stamp = pairs[-1][0]["created_at"][:10].replace("-", "")
    zip_path = settings.TEMP_DIR / f"batch_{type}_{stamp}_{len(pairs)}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for i, (job, local) in enumerate(pairs, start=1):
            snippet = re.sub(r'[\/:*?"<>|\s]+', "", str(job.get("params", {}).get("text", ""))[:12])
            name = f"{i:03d}_{snippet or job['id']}{local.suffix}"
            while name in seen:
                name = f"{i:03d}_{job['id']}_{len(seen)}{local.suffix}"
            seen.add(name)
            zf.write(local, name)
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


def _local_path(output: str):
    """把 /api/files/outputs/audio/xxx.mp3 这类 URL 映射回本地文件。"""
    parts = output.strip("/").split("/")
    if len(parts) < 4 or parts[:2] != ["api", "files"]:
        return None
    bucket, kind, name = parts[2], parts[3], parts[4]
    base = settings.UPLOADS_DIR if bucket == "uploads" else settings.OUTPUTS_DIR
    return base / kind / name


@router.get("/{job_id}/download")
async def download_job_output(job_id: str):
    """下载任务产物，文件名带时间和文本摘要，便于分辨内容与顺序。"""
    job = job_manager.get(job_id)
    if not job or not job.get("output"):
        raise HTTPException(status_code=404, detail="任务不存在或没有产物")
    local = _local_path(job["output"])
    if not local or not local.exists():
        raise HTTPException(status_code=404, detail="产物文件缺失")
    hhmmss = str(job.get("created_at", ""))[11:19].replace(":", "")
    snippet = re.sub(r'[\/:*?"<>|\s]+', "", str(job.get("params", {}).get("text", ""))[:10])
    name = f"{job.get('type', 'file')}_{hhmmss}_{snippet or job_id}{local.suffix}"
    return FileResponse(local, media_type="application/octet-stream", filename=name)


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.delete("/{job_id}")
async def delete_job(job_id: str):
    if not job_manager.delete(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}
