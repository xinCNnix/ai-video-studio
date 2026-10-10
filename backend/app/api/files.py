"""文件上传与静态文件服务。"""
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..config import settings
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
    # 剧本等文本文档：一并返回正文，前端把它内联进助手消息，模型直接读到全文。
    # 上限与 intake 入库一致（MAX_SCRIPT_CHARS）——曾卡 60000 把 14 镜剧本无声砍成
    # 9 镜；超限时带 text_truncated=true 让前端明示，绝不静默截断
    if kind == "doc":
        try:
            decoded = content.decode("utf-8", errors="replace")
            result["text"] = decoded[: settings.MAX_SCRIPT_CHARS]
            result["text_truncated"] = len(decoded) > settings.MAX_SCRIPT_CHARS
        except Exception:  # noqa: BLE001
            result["text"] = ""
            result["text_truncated"] = False
    # 配音即刻返回精确时长：分镜时长要和台词对齐，用户在投放/绑定当场就要看到
    if kind == "audio":
        from ..services import voice_service

        result["duration"] = await voice_service.probe_audio_seconds(result["url"])
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
    # 内联播放：filename 一旦以默认 attachment 类型下发，Chromium 会把 <audio>/<video>
    # 的资源流交给下载管理器，播放解冻在开头几十毫秒（试听点播放停在 00:00 不出声）。
    # 文件名仍需保留（中文语义名），用 inline 类型明确告知浏览器直接播
    return FileResponse(
        path,
        media_type=file_service.guess_mime(path),
        filename=name,
        content_disposition_type="inline",
    )
