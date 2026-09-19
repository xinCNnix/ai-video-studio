"""MiniMax H3 视频适配器 —— 官方 V2 多模态接口（POST /v2/video_generation）。

与 v1 旧接口（T2V-01/S2V-01 时代的 first_frame_image / subject_reference 字段）完全不同：
- 统一 content 数组：text 条目必填；图片/视频/音频条目用 role 表达语义
  （first_frame / last_frame / reference_image / reference_video / reference_audio）
- 首帧/尾帧与参考素材互斥（官方校验，不能同请求混传）
- resolution 必填（480P/768P/2K；H3 支持 768P/2K），ratio 替代 aspect_ratio，
  无 prompt_optimizer 等厂商选项字段
- 创建后用 /v1/query/video_generation 轮询（状态为 PascalCase，成功返回 file_id），
  再经 /v1/files/retrieve 换取临时下载链接
已核对官方文档（platform.minimax.io video-generation-v2-create / video-generation-query，
2026-09-10）；三个域名 api.minimax.cn / api.minimax.io / api.minimax.cn 均已确认 /v2 路由在线。
"""
import base64
import mimetypes
from pathlib import Path

import httpx

from ..base import VideoProvider, ProviderError

# 内联 base64 的按类型上限（官方：图片 30MB / 视频 50MB / 音频 15MB）
_INLINE_CAPS = {"image": 30 * 1024 * 1024, "video": 50 * 1024 * 1024, "audio": 15 * 1024 * 1024}
# 请求体总预算 64MB（base64 计入），预留 JSON 结构开销后内联素材净载荷上限
_TOTAL_INLINE_BUDGET = 45 * 1024 * 1024
# 官方参考素材数量上限
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 3
_VALID_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}


def _to_data_url(path: Path, kind: str) -> str:
    cap = _INLINE_CAPS.get(kind)
    if cap is not None and path.stat().st_size > cap:
        kind_label = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, kind)
        raise ProviderError(
            f"参考{kind_label} {path.name} 超过官方单文件上限 {cap // (1024 * 1024)}MB，请压缩后再试。"
        )
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _map_resolution(value: str | None) -> str:
    """内部档位 → 官方 resolution 枚举（480P/768P/2K）。auto/未识别回退 768P（H3 默认）。"""
    v = (value or "").strip().lower()
    if v in ("2k", "2160p", "4k"):
        return "2K"
    if v in ("480p", "480"):
        return "480P"
    return "768P"


def _map_ratio(mode: str, aspect: str | None) -> str | None:
    """内部 aspect_ratio → 官方 ratio。

    官方规则：t2v 必填且不能 adaptive；i2v/首尾帧固定 adaptive（传了也被忽略，直接不发）；
    参考模式可选，默认 adaptive。
    """
    r = (aspect or "").strip()
    if r not in _VALID_RATIOS:
        r = ""
    if mode in ("image", "first_last"):
        return None
    if mode == "text":
        return r if r and r != "adaptive" else "16:9"
    return r or None


class MiniMaxH3Video(VideoProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.minimax.cn", group_id: str | None = None):
        base = (base_url or "https://api.minimax.cn").rstrip("/")
        for suffix in ("/v1", "/v2"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]  # 路径版本由适配器决定，避免拼成 /v1/v2
        self.api_key = api_key
        self.base_url = base
        self.group_id = group_id or None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _inline(self, path_str: str, kind: str, budget: list[int]) -> str:
        path = Path(path_str)
        url = _to_data_url(path, kind)
        budget[0] += path.stat().st_size
        return url

    def _media_item(self, kind: str, path_str: str, role: str, budget: list[int]) -> dict:
        return {
            "type": f"{kind}_url",
            f"{kind}_url": {"url": self._inline(path_str, kind, budget)},
            "role": role,
        }

    async def submit(self, params: dict) -> str:
        model = params.get("model", "MiniMax-H3")
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("MiniMax H3（V2 接口）要求 content 中必须包含非空 text 描述")
        mode = params.get("mode", "text")
        images = params.get("images") or []
        last_images = params.get("last_images") or []
        videos = params.get("videos") or []
        audios = params.get("audios") or []

        budget = [0]  # 内联素材累计字节数
        # text 条目必填（官方限制 7000 字符）
        content: list[dict] = [{"type": "text", "text": prompt[:7000]}]

        if mode == "first_last":
            # 首尾帧：first_frame/last_frame 各 ≤1，官方禁止与参考素材同请求
            if images:
                content.append(self._media_item("image", images[0], "first_frame", budget))
            if last_images:
                content.append(self._media_item("image", last_images[0], "last_frame", budget))
        elif mode == "image" and images:
            # i2v：仅首帧。官方禁止首帧与参考混传，多余图片不随请求发送
            content.append(self._media_item("image", images[0], "first_frame", budget))
        elif mode == "reference":
            # 参考模式：全部作参考素材，不带首帧（官方上限 9 图 / 3 视频 / 3 音频）
            if len(images) > MAX_REFERENCE_IMAGES:
                raise ProviderError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张（当前 {len(images)} 张）")
            if len(videos) > MAX_REFERENCE_VIDEOS:
                raise ProviderError(f"参考视频最多 {MAX_REFERENCE_VIDEOS} 段（当前 {len(videos)} 段，每段 2-15s）")
            if len(audios) > MAX_REFERENCE_AUDIOS:
                raise ProviderError(f"参考音频最多 {MAX_REFERENCE_AUDIOS} 段（当前 {len(audios)} 段，每段 2-15s）")
            for p in images:
                content.append(self._media_item("image", p, "reference_image", budget))
            for p in videos:
                content.append(self._media_item("video", p, "reference_video", budget))
            for p in audios:
                content.append(self._media_item("audio", p, "reference_audio", budget))

        if budget[0] > _TOTAL_INLINE_BUDGET:
            raise ProviderError(
                f"参考素材内联总量约 {budget[0] // (1024 * 1024)}MB，超过请求体 64MB 预算，请压缩或减少素材。"
            )

        body: dict = {
            "model": model,
            "content": content,
            "resolution": _map_resolution(params.get("resolution")),
            "duration": min(max(int(params.get("duration") or 5), 4), 15),
        }
        ratio = _map_ratio(mode, params.get("aspect_ratio"))
        if ratio:
            body["ratio"] = ratio

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/v2/video_generation",
                    json=body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"视频任务提交失败：{exc}") from exc
        if resp.status_code == 404:
            raise ProviderError(
                f"视频任务提交返回 404：{self.base_url}/v2/video_generation 不存在，"
                "请检查凭据 base_url（国际站 https://api.minimax.io，国内站 https://api.minimax.cn）"
            )
        if resp.status_code >= 400:
            raise ProviderError(f"视频任务提交返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        # v2 错误体为 OpenAI 风格 {error: {message, ...}}
        error = data.get("error") or {}
        if error:
            raise ProviderError(f"视频任务提交错误：{error.get('message') or str(error)[:200]}")
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise ProviderError(f"视频任务提交未返回 task_id：{str(data)[:300]}")
        return task_id

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.base_url}/v1/query/video_generation",
                    params={"task_id": task_id, **({"GroupId": self.group_id} if self.group_id else {})},
                    headers=self._headers(),
                )
        except httpx.HTTPError:
            # 网络抖动不视为任务失败，继续轮询
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json()
        status = (data.get("status") or "").lower()
        if status == "success":
            # v2 成功只返回 file_id，需要再换临时下载链接
            file_id = data.get("file_id")
            if not file_id:
                return ("failed", None, f"任务成功但未返回 file_id：{str(data)[:200]}")
            try:
                url = await self._download_url(str(file_id))
            except ProviderError as exc:
                return ("failed", None, str(exc))
            if not url:
                return ("failed", None, "文件接口未返回 download_url，请稍后在历史中重试")
            return ("success", url, None)
        if status == "fail":
            base_resp = data.get("base_resp") or {}
            return ("failed", None, base_resp.get("status_msg") or "视频生成失败")
        return ("running", None, None)

    async def _download_url(self, file_id: str) -> str | None:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{self.base_url}/v1/files/retrieve",
                params={"file_id": file_id, **({"GroupId": self.group_id} if self.group_id else {})},
                headers=self._headers(),
            )
        if resp.status_code >= 400:
            raise ProviderError(f"获取视频下载链接失败（{resp.status_code}）：{resp.text[:200]}")
        file_info = resp.json().get("file") or {}
        return file_info.get("download_url")
