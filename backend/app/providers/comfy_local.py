"""ComfyUI 本地生成适配器（模板驱动）：同一套机制支持 image / video / tts。

使用方式：
1. 设置添加 ComfyUI 类型凭据（默认 http://127.0.0.1:8188，通常无需 Key）。
2. 在 ComfyUI 里「Save (API Format)」导出工作流 JSON，上传为模板，
   模板名 = 模型 ID（如 comfyui-image / comfyui-video / comfyui-tts）。
3. 模板约定：
   - 正向提示词字符串写 {{prompt}}（TTS 模板写 {{text}}）
   - 反向提示词写 {{negative_prompt}}
   - seed/noise_seed 数值字段自动随机化；width/height 数值字段按请求尺寸覆盖
   - 参考图自动经 /upload/image 上传后注入 {{image_name}}
"""
import asyncio
import json
import random
import uuid
from pathlib import Path

import httpx

from .base import ImageProvider, TTSProvider, VideoProvider, ProviderError

DEFAULT_BASE = "http://127.0.0.1:8188"
COMFY_DIR = None  # 延迟初始化，避免循环导入 settings

CLIENT_ID = f"aistudio-{uuid.uuid4().hex[:8]}"


def _comfy_dir():
    global COMFY_DIR
    if COMFY_DIR is None:
        from ..config import settings

        COMFY_DIR = settings.DATA_DIR / "comfyui"
        COMFY_DIR.mkdir(parents=True, exist_ok=True)
    return COMFY_DIR


class ComfyUILocal(ImageProvider, VideoProvider, TTSProvider):
    def __init__(self, api_key: str, base_url: str | None = None, kind: str = "image"):
        # kind 仅用于 video 轮询超时区分；模板由模型 ID 决定
        self.api_key = api_key or ""
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.kind = kind

    # ---------- 模板 ----------
    def _load_template(self, model: str) -> dict:
        path = _comfy_dir() / f"{model}.json"
        if not path.exists():
            raise ProviderError(
                f"ComfyUI 模板 {model}.json 不存在：请先在设置里上传对应工作流模板。"
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProviderError(f"ComfyUI 模板 {model}.json 解析失败：{exc}") from exc

    @staticmethod
    def _prepare_workflow(template: dict, mapping: dict[str, str], size: tuple[int, int] | None,
                          references: list[tuple[str, bytes]] | None) -> dict:
        wf = json.loads(json.dumps(template))  # deep copy

        def sub(node):
            if isinstance(node, dict):
                return {k: sub(v) for k, v in node.items()}
            if isinstance(node, list):
                return [sub(v) for v in node]
            if isinstance(node, str):
                for k, v in mapping.items():
                    node = node.replace(k, v)
                return node
            return node

        wf = sub(wf)
        # 数值字段：种子随机化、宽高覆盖
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for key in ("seed", "noise_seed"):
                if key in inputs and isinstance(inputs[key], (int, float)):
                    inputs[key] = random.randint(0, 2**31 - 1)
            if size and "x" in size:
                w, h = size.split("x", 1)
                if w.isdigit() and h.isdigit():
                    if "width" in inputs:
                        inputs["width"] = int(w)
                    if "height" in inputs:
                        inputs["height"] = int(h)
        return wf

    # ---------- ComfyUI HTTP ----------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def _queue_prompt(self, client: httpx.AsyncClient, workflow: dict) -> str:
        resp = await client.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": CLIENT_ID},
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise ProviderError(f"ComfyUI 提交返回 {resp.status_code}：{resp.text[:300]}")
        prompt_id = resp.json().get("prompt_id")
        if not prompt_id:
            raise ProviderError(f"ComfyUI 未返回 prompt_id：{resp.text[:300]}")
        return prompt_id

    async def _wait_history(self, client: httpx.AsyncClient, prompt_id: str, timeout_s: int) -> dict:
        waited = 0.0
        while waited < timeout_s:
            resp = await client.get(f"{self.base_url}/history/{prompt_id}", headers=self._headers())
            if resp.status_code < 400:
                data = resp.json().get(prompt_id)
                if data:
                    status_info = data.get("status") or {}
                    if status_info.get("status_str") == "error":
                        msgs = [m for m in status_info.get("messages", []) if m[0] == "execution_error"]
                        detail = str(msgs[-1][1] if msgs else "执行错误")[:300]
                        raise ProviderError(f"ComfyUI 执行错误：{detail}")
                    outputs = data.get("outputs") or {}
                    if outputs:
                        return outputs
            await asyncio.sleep(2)
            waited += 2
        raise ProviderError(f"ComfyUI 生成超时（{timeout_s}s）")

    async def _fetch_output_files(self, client: httpx.AsyncClient, outputs: dict,
                                  kinds: tuple[str, ...]) -> list[bytes]:
        files: list[bytes] = []
        for node_out in outputs.values():
            for kind in kinds:
                for item in node_out.get(kind) or []:
                    params = {
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    }
                    resp = await client.get(f"{self.base_url}/view", params=params, headers=self._headers())
                    resp.raise_for_status()
                    files.append(resp.content)
        return files

    async def _upload_reference(self, client: httpx.AsyncClient, filename: str, content: bytes) -> str:
        ext = Path(filename).suffix or ".png"
        resp = await client.post(
            f"{self.base_url}/upload/image",
            data={"overwrite": "true"},
            files={"image": (f"ref_{uuid.uuid4().hex[:8]}{ext}", content)},
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise ProviderError(f"参考图上传失败：{resp.text[:200]}")
        return resp.json().get("name") or filename

    # ---------- ImageProvider ----------
    async def generate(self, prompt: str, model: str, size: str | None = None,
                       n: int = 1, references=None) -> list[bytes]:
        mapping = {"{{prompt}}": prompt, "{{negative_prompt}}": "", "{{text}}": prompt}
        refs = []
        for name, content in (references or [])[:4]:
            async with httpx.AsyncClient(timeout=60) as up:
                refs.append(await self._upload_reference(up, name, content))
        if refs:
            mapping["{{image_name}}"] = refs[0]
        workflow = self._prepare_workflow(self._load_template(model), mapping, size, references or None)
        async with httpx.AsyncClient(timeout=120) as client:
            prompt_id = await self._queue_prompt(client, workflow)
            outputs = await self._wait_history(client, prompt_id, timeout_s=300)
            files = await self._fetch_output_files(client, outputs, ("images", "gifs"))
        if not files:
            raise ProviderError("ComfyUI 未返回图片输出（请检查工作流的 SaveImage 节点）。")
        return files

    # ---------- VideoProvider ----------
    async def submit(self, params: dict) -> str:
        mapping = {"{{prompt}}": params.get("prompt", ""), "{{text}}": params.get("prompt", "")}
        refs = []
        for ref in params.get("images") or []:
            p = Path(ref)
            if p.exists():
                refs.append(p.name)
        if refs:
            mapping["{{image_name}}"] = refs[0]
        if params.get("last_images"):
            lp = Path(params["last_images"][0])
            if lp.exists():
                mapping["{{last_image_name}}"] = lp.name
        workflow = self._prepare_workflow(self._load_template(params.get("model", "comfyui-video")),
                                          mapping, None, None)
        async with httpx.AsyncClient(timeout=120) as client:
            return await self._queue_prompt(client, workflow)

    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(f"{self.base_url}/history/{task_id}", headers=self._headers())
        except httpx.HTTPError:
            return ("running", None, None)
        if resp.status_code >= 400:
            return ("failed", None, f"查询任务返回 {resp.status_code}")
        data = resp.json().get(task_id)
        if not data:
            return ("running", None, None)
        status_info = data.get("status") or {}
        if status_info.get("status_str") == "error":
            return ("failed", None, "ComfyUI 执行错误")
        outputs = data.get("outputs") or {}
        for node_out in outputs.values():
            for item in node_out.get("videos") or node_out.get("gifs") or []:
                filename = item.get("filename")
                if filename:
                    from urllib.parse import urlencode

                    url = f"{self.base_url}/view?{urlencode({'filename': filename, 'subfolder': item.get('subfolder', ''), 'type': item.get('type', 'output')})}"
                    return ("success", url, None)
        return ("running", None, None)

    # ---------- TTSProvider ----------
    async def synthesize(self, text: str, model: str, voice: str,
                         speed: float = 1.0, emotion: str = "neutral") -> bytes:
        mapping = {"{{text}}": text, "{{prompt}}": text, "{{voice}}": voice or ""}
        workflow = self._prepare_workflow(self._load_template(model), mapping, None, None)
        async with httpx.AsyncClient(timeout=120) as client:
            prompt_id = await self._queue_prompt(client, workflow)
            outputs = await self._wait_history(client, prompt_id, timeout_s=300)
            files = await self._fetch_output_files(client, outputs, ("audios",))
        if not files:
            raise ProviderError("ComfyUI 未返回音频输出（请检查工作流的 SaveAudio 节点）。")
        return b"".join(files)
