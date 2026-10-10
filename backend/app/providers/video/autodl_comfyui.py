"""AutoDL ComfyUI 工作流视频适配器（https://autodl.art/docs/comfyui_api/）。

提交：POST https://autodl.art/api/v1/comfyui/comfyui_workflow/{workflow_id}
轮询：GET  https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}

模型 ID = 工作流 ID（如 minimax_h3_zm_u24）。
不同工作流的入参要求不同（是否必传参考图、是否收音频等），统一由
config.settings.AUTODL_WORKFLOW_SPECS 契约表描述：
- 提交前按契约做最后防线校验（API 层已提前 400，这里兜底防止其他调用路径漏校验）；
- 字段按契约过滤：默认参考图平铺为 ref_image_N（均无首帧语义）；契约声明
  image_layout="first_last" 的首尾帧工作流（如 minimax_h3_lightx2v）则把
  images[0]/last_images[0] 映射为独立的 first_frame/last_frame 入参；
  音频仅在契约允许时以 ref_audio_N 传入；options 只透传契约 extra_fields 声明的字段。
"""
import base64
import mimetypes
import random
from pathlib import Path

import httpx

from ...config import settings
from ..base import VideoProvider, ProviderError

DEFAULT_BASE = "https://autodl.art/api/v1"

STATUS_MAP = {
    "QUEUED": "running",
    "PENDING": "running",
    "RUNNING": "running",
    "SUCCESS": "success",
    "FAILED": "failed",
}


def _to_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"


def _build_body(params: dict, spec: dict | None = None) -> dict:
    """构造提交请求体（纯函数，便于离线验证）。

    spec 为该工作流的契约（settings.AUTODL_WORKFLOW_SPECS 的一条，可为 None）：
- audios 仅在 spec["accepts_audios"] 为真时编码进 ref_audio_N；
- 首尾帧工作流（spec["image_layout"] == "first_last"）把 images[0]/last_images[0]
  编码为 first_frame/last_frame（两帧缺失由契约层提前拦截，这里只编码实际存在
  的帧，绝不静默降级提交）；
- options 只透传 spec["extra_fields"] 白名单里的字段（AutoDL 严格校验，
  多传未定义字段会报"存在未定义的参数"，prompt_optimizer 等厂商选项会被剔除）。
已验证的通用字段：prompt / duration / resolution / ref_image_N。
    """
    spec = spec or {}
    body: dict = {}
    if params.get("prompt"):
        body["prompt"] = params["prompt"]
    if params.get("duration"):
        body["duration"] = int(params["duration"])
    if params.get("resolution") and params["resolution"] != "auto":
        res = str(params["resolution"])
        # 契约声明 resolution_labels 的工作流（枚举标签带像素后缀，如 768p横(1376*768)）
        # 按画幅取完整标签直接透传；命不中（如自定义档位）回退补方向后缀逻辑
        labels = spec.get("resolution_labels") or {}
        exact = (labels.get(res) or {}).get(str(params.get("aspect_ratio") or ""))
        if exact:
            body["resolution"] = exact
        else:
            # AutoDL 格式为 "768p横/768p竖/480p(1:1)" 等：按画幅自动补方向
            if "横" not in res and "竖" not in res and "(1:1)" not in res:
                if params.get("aspect_ratio") == "9:16":
                    res += "竖"
                elif params.get("aspect_ratio") == "1:1":
                    res += "(1:1)"
                else:
                    res += "横"
            body["resolution"] = res
    # 参考图按契约布局编码：首尾帧工作流映射为 first_frame/last_frame 独立入参，
    # 其余平铺为 ref_image_N（契约上限过滤，max_images=0 的纯文生工作流绝不携带）
    if spec.get("image_layout") == "first_last":
        if params.get("images"):
            body["first_frame"] = _to_data_url(params["images"][0])
        if params.get("last_images"):
            body["last_frame"] = _to_data_url(params["last_images"][0])
    elif spec.get("max_images") is None or spec.get("max_images") > 0:
        for i, path in enumerate(params.get("images") or []):
            body[f"ref_image_{i}"] = _to_data_url(path)
    if spec.get("accepts_audios", True):
        for i, path in enumerate(params.get("audios") or []):
            body[f"ref_audio_{i}"] = _to_data_url(path)
    # 仅透传契约声明的工作流特有字段（如 seed、档位）
    allowed = set(spec.get("extra_fields") or [])
    for key, value in (params.get("options") or {}).items():
        if key in allowed:
            body[key] = value
    # seed 随机化（抽卡的前提）：AutoDL 工作流的 seed 固化在云端工作流 JSON 里，
    # 不传则每次都用同一个默认种子——同参数重提产出逐帧相同的视频。契约声明可传
    # seed 的工作流，未显式提供（或提供了非数值）时注入随机种子；显式传数值则
    # 尊重（同 seed 可复现）。与 comfy_local 的 seed/noise_seed 随机化同一约定。
    if "seed" in allowed:
        try:
            body["seed"] = int(body.get("seed"))
        except (TypeError, ValueError):
            body["seed"] = random.randint(0, 2**31 - 1)
    return body


def _find_video_url(results) -> str | None:
    """results 可能是 URL 字符串列表，或带 url/type 字段的对象列表。"""
    if not isinstance(results, list):
        return results if isinstance(results, str) else None
    video_keys = ("video", "mp4")
    for item in results:  # 优先视频
        if isinstance(item, dict):
            t = str(item.get("type") or "").lower()
            url = item.get("url") or item.get("video_url")
            if url and any(k in t for k in video_keys):
                return url
    for item in results:
        if isinstance(item, dict) and (item.get("url") or item.get("video_url")):
            return item.get("url") or item.get("video_url")
        if isinstance(item, str) and item.startswith("http"):
            return item
    return None


class AutoDLComfyUI(VideoProvider):
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    async def submit(self, params: dict) -> str:
        workflow_id = params.get("model")
        if not workflow_id:
            raise ProviderError("AutoDL ComfyUI 需要 model = 工作流 ID（如 minimax_h3_zm_u24）。")
        spec = settings.AUTODL_WORKFLOW_SPECS.get(workflow_id)
        # 契约兜底校验（API 层已提前拦截，这里防止其他调用路径漏校验后打到云端）
        need = int((spec or {}).get("min_images") or 0)
        if len(params.get("images") or []) < need:
            raise ProviderError(
                f"工作流 {workflow_id} 必须携带至少 {need} 张参考图，当前请求未携带。"
            )
        if (spec or {}).get("image_layout") == "first_last" and not (params.get("last_images") or []):
            raise ProviderError(
                f"工作流 {workflow_id} 为首尾帧工作流，必须同时携带首帧与尾帧图片（当前缺少尾帧）。"
            )
        body = _build_body(params, spec)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/comfyui/comfyui_workflow/{workflow_id}",
                    json=body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"AutoDL 任务提交失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"AutoDL 提交返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        if data.get("code") not in (None, "Success"):
            raise ProviderError(f"AutoDL 提交失败：{data.get('msg') or str(data)[:300]}")
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"AutoDL 未返回任务 ID：{str(data)[:300]}")
        return task_id

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.base_url}/comfyui/comfyui_workflow/result/{task_id}",
                    headers=self._headers(),
                )
        except httpx.HTTPError:
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json().get("data") or {}
        raw_status = (data.get("status") or "").upper()
        status = STATUS_MAP.get(raw_status, "running")
        if status == "success":
            url = _find_video_url(data.get("results"))
            if not url:
                # 云端已 SUCCESS（钱已花）但结果列表还没出地址：按运行中继续轮询，
                # 由外层 MAX_POLL_SECONDS 兜底——不能在这里宣判失败把钱扔掉
                return ("running", None, None)
            return ("success", url, None)
        if status == "failed":
            return ("failed", None, data.get("message") or "AutoDL 工作流执行失败")
        return ("running", None, None)
