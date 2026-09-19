"""文件上传与静态文件服务。"""
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..services import file_service

router = APIRouter(prefix="/api", tags=["files"])

MAX_UPLOAD_SIZE = 200 * 1024 * 1024


@router.post("/uploads")
async def upload_file(file: UploadFile = File(...), kind: str = Form("image")):
    if kind not in ("image", "audio", "video", "doc"):
        raise HTTPException(status_code=400, detail="kind 必须是 image / audio / video / doc")
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大（上限 200MB）")
    if not content:
        raise HTTPException(status_code=400, detail="空文件")
    result = await file_service.save_upload(kind, file.filename or "upload.bin", content)
    # 剧本等文本文档：一并返回正文，前端把它内联进助手消息，模型直接读到全文
    if kind == "doc":
        try:
            result["text"] = content.decode("utf-8", errors="replace")[:60000]
        except Exception:  # noqa: BLE001
            result["text"] = ""
    return result


@router.get("/files/{bucket}/{kind}/{name}")
async def get_file(bucket: str, kind: str, name: str, dl: int = 0):
    if bucket not in ("uploads", "outputs") or kind not in ("image", "audio", "video", "doc"):
        raise HTTPException(status_code=404)
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=404)
    base = file_service.settings.UPLOADS_DIR if bucket == "uploads" else file_service.settings.OUTPUTS_DIR
    path = base / kind / name
    if not path.exists():
        raise HTTPException(status_code=404)
    if dl:
        # 下载时给可读文件名：aistudio_video_20260906_153012_ab12.mp4
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dl_name = f"aistudio_{kind}_{stamp}_{name[:6]}{path.suffix}"
        return FileResponse(path, media_type=file_service.guess_mime(path), filename=dl_name)
    return FileResponse(path, media_type=file_service.guess_mime(path), filename=name)
