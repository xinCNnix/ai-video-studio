"""Wan 通义万相（阿里云百炼 DashScope）视频适配器。

提交：POST /services/aigc/video-generation/video-synthesis（X-DashScope-Async: enable）
轮询：GET /tasks/{task_id}
注意：img_url 需要厂商可访问的地址，本地图以 data URL 透传，若厂商不支持会返回明确错误。
"""
import base64
import mimetypes
from pathlib import Path

import httpx

from ..base import VideoProvider, ProviderError

DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"

ASPECT_SIZES = {"16:9": "1280*720", "9:16": "720*1280", "1:1": "960*960", "4:3": "1024*768", "3:4": "768*1024"}
# DashScope 只接受固定档位：这里列支持的组合，未覆盖的回落默认档
RESOLUTION_SIZES = {
    ("16:9", "1080p"): "1920*1080",
    ("9:16", "1080p"): "1080*1920",
    ("16:9", "480p"): "640*360",
    ("9:16", "480p"): "360*640",
}


def _size_for(aspect: str | None, resolution: str | None) -> str | None:
    if not aspect or aspect not in ASPECT_SIZES:
        return None
    return RESOLUTION_SIZES.get((aspect, resolution or "")) or ASPECT_SIZES[aspect]


def _to_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


class WanVideo(VideoProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    async def submit(self, params: dict) -> str:
        mode = params.get("mode", "text")
        model = params.get("model", "wan2.2-t2v-plus")
        inp: dict = {"prompt": params.get("prompt", "")}
        images = params.get("images") or []
        last_images = params.get("last_images") or []
        if mode in ("image", "reference") and images:
            inp["img_url"] = _to_url(Path(images[0]))
        if mode == "first_last":
            if images:
                inp["first_frame_url"] = _to_url(Path(images[0]))
            if last_images:
                inp["last_frame_url"] = _to_url(Path(last_images[0]))
            if "i2v" in model or "t2v" in model:
                model = "wan2.1-kf2v-plus"  # 首尾帧需要 kf2v 模型
        parameters: dict = {}
        # wan3.0 画幅自适应（prompt 描述画面即可），旧模型按固定档位传 size
        if not model.startswith("wan3"):
            size = _size_for(params.get("aspect_ratio"), params.get("resolution"))
            if size:
                parameters["size"] = size
        if params.get("duration"):
            parameters["duration"] = int(params["duration"])

        body = {"model": model, "input": inp, "parameters": parameters}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/services/aigc/video-generation/video-synthesis",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "X-DashScope-Async": "enable",
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Wan 任务提交失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Wan 提交返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"Wan 未返回任务 ID：{str(data)[:300]}")
        return task_id

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.base_url}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError:
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json()
        output = data.get("output") or {}
        status = output.get("task_status")
        if status == "SUCCEEDED":
            url = output.get("video_url") or output.get("video_url_list", [None])[0]
            if not url:
                return ("failed", None, f"任务成功但未返回视频地址：{str(data)[:200]}")
            return ("success", url, None)
        if status == "FAILED":
            return ("failed", None, output.get("message") or "Wan 生成失败")
        if status in ("CANCELED", "CANCELLED", "UNKNOWN"):
            return ("failed", None, f"Wan 任务终止：{status}")
        return ("running", None, None)
