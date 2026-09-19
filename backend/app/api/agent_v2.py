"""Agent API（/api/agent，v2 实现）。

- run / run/stream：v2 agent（创作直通 pipeline、修改走瘦工具循环）
  SSE 协议保持旧格式（{"type":"step","step":{...}} / {"type":"final","result":{...}}），
  非 Studio 页的旧侧栏零改动可用
- chat：纯聊天（Prompt 页/无项目上下文），无工具
- skills / llm-options / cancel：与旧版一致
"""
import asyncio
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..providers.base import ProviderError
from ..providers.registry import resolve_llm_call
from ..services import agent_v2, skill_service

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/skills")
async def list_skills():
    return {
        "skills": [
            {"id": s["id"], "name": s["name"], "description": s["description"], "source": s["source"]}
            for s in skill_service.list_skills()
        ]
    }


@router.get("/llm-options")
async def llm_options():
    from ..config import app_settings, key_manager, model_store
    from ..providers.registry import list_models_for_credential

    default = app_settings.get_default_llm()
    options = []
    for pub in key_manager.list_keys():
        base = (pub.get("base_url") or "").lower()
        provider = pub.get("provider") or ""
        if provider not in ("minimax", "openai", "openai_compatible", "xiaomi", "ollama") and "minimax" not in base:
            continue
        try:
            ids = list_models_for_credential(key_manager.get_key(pub["id"]))
        except Exception:  # noqa: BLE001 - 单凭据发现失败不影响整体
            continue
        for mid in ids:
            if any(k in mid.lower() for k in ("tts", "asr", "voiceclone", "voicedesign", "music", "image", "embedding", "rerank")):
                continue
            options.append({
                "model": mid,
                "credential_id": pub["id"],
                "provider": provider,
                "credential_name": pub.get("name") or provider,
                "label": f"{mid}（{pub.get('name') or provider}）",
            })
    for m in model_store.list_custom().get("llm", []):
        if not any(o["model"] == m["id"] for o in options):
            options.append({
                "model": m["id"], "credential_id": m.get("credential_id"),
                "provider": m.get("provider", ""), "credential_name": m.get("provider", "自定义"),
                "label": f"{m['id']}（自定义）",
            })
    current_model = default["model"] if default else None
    current_cred = default.get("credential_id") if default else None
    if not current_model and options:
        current_model, current_cred = options[0]["model"], options[0]["credential_id"]
    return {
        "current": {"model": current_model, "credential_id": current_cred, "source": "default" if default else "auto"},
        "options": options[:60],
    }


# ---------------- run / stream ----------------


class AgentRunRequest(BaseModel):
    goal: str
    history: list[dict] = []
    page: str = ""
    scene_title: str = ""
    scene_description: str = ""
    scene_prompt: str = ""
    scene_narration: str = ""
    scene_image: str | None = None
    aspect_ratio: str | None = None
    project_id: str | None = None
    credential_id: str | None = None
    model: str | None = None
    force_route: str | None = None  # 兼容字段，忽略


def _context(req: AgentRunRequest) -> dict:
    return {
        "page": req.page,
        "scene_title": req.scene_title,
        "project_id": req.project_id or None,
        "scene_id": None,
        "aspect_ratio": req.aspect_ratio,
    }


def _sanitize_history(history: list[dict], max_turns: int = 12, max_len: int = 4000) -> list[dict]:
    out = []
    for h in history[-max_turns:]:
        role = h.get("role")
        content = str(h.get("content") or "")[:max_len]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


@router.post("/run")
async def run(req: AgentRunRequest):
    if not req.goal.strip():
        raise HTTPException(status_code=400, detail="请描述要执行的任务")
    try:
        return await agent_v2.run_agent_v2(
            req.goal.strip(), _context(req), req.credential_id, req.model, history=_sanitize_history(req.history)
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post("/run/stream")
async def run_stream(req: AgentRunRequest):
    """SSE：{"type":"step","step":{...}} → {"type":"final","result":{...}}（与旧协议一致）。"""
    if not req.goal.strip():
        raise HTTPException(status_code=400, detail="请描述要执行的任务")

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()
        step_no = 0

        def on_step(event: dict) -> None:
            nonlocal step_no
            step_no += 1
            step = {"step": step_no, "type": str(event.get("type") or "info")}
            if event.get("tool"):
                step["type"] = "tool"
                step["tool"] = event["tool"]
                if event.get("ok") is not None:
                    step["ok"] = bool(event["ok"])
                step["text"] = f"{event['tool']} {'✓' if event.get('ok', True) else '✕'}"
            elif event.get("mode"):
                step["text"] = f"路由：{event['mode']}"
            queue.put_nowait(step)

        task = asyncio.create_task(
            agent_v2.run_agent_v2(
                req.goal.strip(), _context(req), req.credential_id, req.model,
                history=_sanitize_history(req.history), on_step=on_step,
            )
        )
        while True:
            try:
                step = queue.get_nowait()
            except asyncio.QueueEmpty:
                if task.done():
                    while not queue.empty():
                        yield _sse({"type": "step", "step": queue.get_nowait()})
                    break
                await asyncio.sleep(0.15)
                continue
            yield _sse({"type": "step", "step": step})
        try:
            result = task.result()
        except ProviderError as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)[:500]})
            return
        result.setdefault("project_changed", False)
        yield _sse({"type": "final", "result": result})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class RunCancelRequest(BaseModel):
    run_id: str = ""


@router.post("/cancel")
async def cancel(req: RunCancelRequest):
    agent_v2.cancel_run(req.run_id.strip())
    return {"ok": True}


# ---------------- 纯聊天（Prompt 页等无项目上下文场景） ----------------

CHAT_SYSTEM_PROMPT = """你是 AI Video Studio 内置的 AI 助手，帮助用户创作 AI 视频（Prompt、图片、语音、视频）。

当前上下文：
- 页面：{page}
- 当前场景标题：{scene_title}
- 当前场景描述：{scene_description}
- 当前 Prompt：{current_prompt}

规则：
1. 用用户的语言简短回复。
2. 如果用户要求修改 Prompt 或文案，返回 JSON：
   {{"reply": "<一句话说明>", "action": {{"type": "update_prompt", "value": "<修改后的完整 Prompt>"}}}}
   修改场景描述时 type 用 "update_description"。
3. 回答问题/给建议不修改内容：{{"reply": "<回答>", "action": {{"type": "none"}}}}
4. 保持结构化分节格式（## Scene / ## Subject / ## Action Timeline / ## Camera / ## Visual Style / ## Lighting / ## Dialogue / Voice / ## Audio / ## Constraints）。
5. 用户要求生成/设计视频 Prompt 时，reply 必须包含完整结构化 Prompt 全文，不要概括省略。
6. 只输出 JSON。"""


class AgentChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    page: str = ""
    scene_title: str = ""
    scene_description: str = ""
    current_prompt: str = ""
    credential_id: str | None = None
    model: str | None = None


def _extract_json(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return {"reply": text or "（空回复）", "action": {"type": "none"}}
    for cand in (m.group(0), m.group(0).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")):
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return {"reply": text or "（空回复）", "action": {"type": "none"}}


@router.post("/chat")
async def chat(req: AgentChatRequest):
    system = CHAT_SYSTEM_PROMPT.format(
        page=req.page or "未知",
        scene_title=req.scene_title or "（无）",
        scene_description=(req.scene_description or "（无）")[:500],
        current_prompt=(req.current_prompt or "（无）")[:3000],
    )
    try:
        llm, model = resolve_llm_call(req.credential_id, provider_hint="minimax", model=req.model)
        conversation = [
            {"role": "system", "content": system},
            *_sanitize_history(req.history),
            {"role": "user", "content": req.message},
        ]
        raw = await llm.generate(conversation, model, temperature=0.5, max_tokens=8000)
        if not raw or not raw.strip():
            raise HTTPException(status_code=502, detail="模型返回空内容，请重试。")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = _extract_json(raw)
    reply = str(data.get("reply") or "").replace("\\n", "\n").strip() or "（空回复）"
    action = data.get("action") or {"type": "none"}
    if action.get("type") not in ("none", "update_prompt", "update_description"):
        action = {"type": "none"}
    return {"reply": reply, "action": action}
