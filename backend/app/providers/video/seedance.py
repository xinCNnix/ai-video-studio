"""Seedance（火山方舟 doubao-seedance）视频适配器。

方舟任务式接口：POST /contents/generations/tasks 提交，GET 同路径轮询。
已核对官方文档（创建视频生成任务 docs/82379/1520757、Seedance 2.5 教程 docs/82379/2607688，
2026-09-10）：

- 参数用 body 字段 resolution / ratio / duration（强校验，官方推荐），
  不再用旧的文本指令追加方式（--rs/--dur/--rt，弱校验，写错被静默忽略）
- content 数组：text + 媒体条目；图片条目用 role 表达语义
  （first_frame / last_frame / reference_image），首帧、首尾帧、参考三种场景互斥
- 参考视频仅支持公网 URL 或素材 ID（不支持 Base64 内联），本地上传的参考视频
  无法直接投递，显式报错而不是静默丢弃
- 参考图片 / 参考音频支持 Base64 内联（单张 ≤30MB / 单段 ≤15MB）
"""
import base64
import mimetypes
from pathlib import Path

import httpx

from ..base import VideoProvider, ProviderError

DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"

# 内联单文件上限（官方：参考图片 ≤30MB/张，参考音频 ≤15MB/段）
_IMAGE_INLINE_CAP = 30 * 1024 * 1024
_AUDIO_INLINE_CAP = 15 * 1024 * 1024
_VALID_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
_VALID_RESOLUTIONS = {"480p", "720p", "1080p"}


def _to_data_url(path: Path, cap: int, label: str) -> str:
    if path.stat().st_size > cap:
        raise ProviderError(f"参考{label} {path.name} 超过官方单文件上限 {cap // (1024 * 1024)}MB，请压缩后再试。")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


class SeedanceVideo(VideoProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    async def submit(self, params: dict) -> str:
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("Seedance 要求非空提示词（text 条目）")
        mode = params.get("mode", "text")
        images = params.get("images") or []
        last_images = params.get("last_images") or []
        videos = params.get("videos") or []
        audios = params.get("audios") or []

        content: list[dict] = [{"type": "text", "text": prompt}]
        if mode == "first_last":
            # 首尾帧：与参考素材互斥，各 ≤1 张
            if images:
                content.append({"type": "image_url", "image_url": {"url": _to_data_url(Path(images[0]), _IMAGE_INLINE_CAP, "图片")}, "role": "first_frame"})
            if last_images:
                content.append({"type": "image_url", "image_url": {"url": _to_data_url(Path(last_images[0]), _IMAGE_INLINE_CAP, "图片")}, "role": "last_frame"})
        elif mode == "image" and images:
            # i2v 首帧：仅一张。首帧与参考互斥，多余图片不随请求发送
            content.append({"type": "image_url", "image_url": {"url": _to_data_url(Path(images[0]), _IMAGE_INLINE_CAP, "图片")}, "role": "first_frame"})
        elif mode == "reference":
            # 参考模式：图片/音频 Base64 内联；参考视频官方仅收公网 URL / 素材 ID
            if videos:
                raise ProviderError(
                    "Seedance 参考视频仅支持公网 URL 或素材 ID（不支持本地上传内联），"
                    "请移除参考视频，或改用参考图片/参考音频。"
                )
            if not images and not videos:
                raise ProviderError("Seedance 参考模式不可单独输入音频，请至少提供一张参考图片或一段参考视频。")
            for p in images:
                content.append({"type": "image_url", "image_url": {"url": _to_data_url(Path(p), _IMAGE_INLINE_CAP, "图片")}, "role": "reference_image"})
            for p in audios:
                content.append({"type": "audio_url", "audio_url": {"url": _to_data_url(Path(p), _AUDIO_INLINE_CAP, "音频")}, "role": "reference_audio"})

        body: dict = {"model": params.get("model", "doubao-seedance-2-5-260628"), "content": content}
        # 强校验参数直接放 body（官方推荐），不再追加 --rs/--dur/--rt 文本指令
        resolution = (params.get("resolution") or "").strip().lower()
        if resolution and resolution != "auto":
            if resolution not in _VALID_RESOLUTIONS:
                raise ProviderError(f"Seedance 分辨率仅支持 480p/720p/1080p（当前 {resolution}）")
            body["resolution"] = resolution
        ratio = (params.get("aspect_ratio") or "").strip()
        if mode == "text" and ratio in _VALID_RATIOS and ratio != "adaptive":
            body["ratio"] = ratio
        elif mode == "reference" and ratio in _VALID_RATIOS:
            body["ratio"] = ratio
        # i2v / 首尾帧：官方默认 adaptive（首帧决定画幅），不传 ratio
        if params.get("duration"):
            body["duration"] = int(params["duration"])
        options = params.get("options") or {}
        if options.get("generate_audio") is not None:
            body["generate_audio"] = bool(options["generate_audio"])

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/contents/generations/tasks",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Seedance 任务提交失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Seedance 提交返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        error = data.get("error")
        if error:
            raise ProviderError(f"Seedance 提交错误：{(error or {}).get('message') or str(error)[:200]}")
        task_id = data.get("id")
        if not task_id:
            raise ProviderError(f"Seedance 未返回任务 ID：{str(data)[:300]}")
        return task_id

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.base_url}/contents/generations/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError:
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json()
        status = data.get("status")
        if status == "succeeded":
            url = (data.get("content") or {}).get("video_url")
            if not url:
                return ("failed", None, f"任务成功但未返回视频地址：{str(data)[:200]}")
            return ("success", url, None)
        if status == "failed":
            err = (data.get("error") or {}).get("message") or "Seedance 生成失败"
            return ("failed", None, err)
        if status in ("cancelled", "canceled", "expired"):
            return ("failed", None, f"Seedance 任务终止：{status}")
        return ("running", None, None)
