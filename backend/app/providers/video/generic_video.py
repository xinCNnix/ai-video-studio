"""通用视频 API 适配器（OpenAI Sora 风格：POST /videos + GET /videos/{id}）。"""
import base64
import mimetypes
import re
from pathlib import Path

import httpx

from ..base import VideoProvider, ProviderError

ASPECT_SIZES = {"16:9": "1280x720", "9:16": "720x1280", "1:1": "1024x1024", "3:4": "768x1024", "4:3": "1024x768"}

_WXH_RE = re.compile(r"^\d{2,5}x\d{2,5}$")

# 参考素材内联 base64 上传的单文件上限
MAX_INLINE_REF_BYTES = 30 * 1024 * 1024


def _to_data_url(path: Path, max_bytes: int | None = None) -> str:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ProviderError(
            f"参考文件 {path.name} 超过 {max_bytes // (1024 * 1024)}MB，内联上传过大，请压缩后再试。"
        )
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _build_body(params: dict) -> dict:
    """构造提交请求体（纯函数，便于离线验证）。"""
    body: dict = {
        "model": params.get("model", "generic"),
        "prompt": params.get("prompt", ""),
    }
    if params.get("duration"):
        body["seconds"] = params["duration"]
    aspect = params.get("aspect_ratio")
    if aspect and aspect in ASPECT_SIZES:
        body["size"] = ASPECT_SIZES[aspect]
    # 显式 "宽x高" 分辨率覆盖按比例推算的 size；768p 这类字符串由厂商自行解析
    resolution = params.get("resolution")
    if resolution and _WXH_RE.match(resolution.strip()):
        body["size"] = resolution.strip()
    images = params.get("images") or []
    last_images = params.get("last_images") or []
    if params.get("mode") == "first_last":
        if images:
            body["input_reference"] = _to_data_url(Path(images[0]))
        if last_images:
            body["last_frame"] = _to_data_url(Path(last_images[0]))
    elif params.get("mode") in ("image", "reference") and images:
        body["input_reference"] = _to_data_url(Path(images[0]))
    if params.get("mode") == "image" and len(images) > 1:
        body["input_references"] = [_to_data_url(Path(p)) for p in images]
    if params.get("mode") == "reference":
        # 多参考素材全部透传，字段名集中在适配器内便于适配不同厂商
        if images:
            body["input_references"] = [_to_data_url(Path(p)) for p in images]
        if params.get("videos"):
            body["reference_videos"] = [
                _to_data_url(Path(p), MAX_INLINE_REF_BYTES) for p in params["videos"]
            ]
        if params.get("audios"):
            body["reference_audios"] = [
                _to_data_url(Path(p), MAX_INLINE_REF_BYTES) for p in params["audios"]
            ]
    return body


def _find_url(data: dict) -> str | None:
    for key in ("url", "video_url", "download_url"):
        if isinstance(data.get(key), str):
            return data[key]
    output = data.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    contents = data.get("data")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    return None


class GenericVideoProvider(VideoProvider):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ProviderError("Generic Video 需要在凭据中配置 Base URL。")

    async def submit(self, params: dict) -> str:
        body = _build_body(params)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/videos",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"视频任务提交失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"视频任务提交返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise ProviderError(f"视频任务提交未返回任务 ID：{str(data)[:300]}")
        return task_id

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.base_url}/videos/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError:
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json()
        status = (data.get("status") or "queued").lower()
        if status in ("completed", "succeeded", "success"):
            url = _find_url(data)
            if not url:
                return ("failed", None, f"任务完成但未找到视频地址：{str(data)[:200]}")
            return ("success", url, None)
        if status in ("failed", "error"):
            return ("failed", None, data.get("error") or "视频生成失败")
        return ("running", None, None)
