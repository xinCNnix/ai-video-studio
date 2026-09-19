"""AI Assistant：LLM + 当前页面上下文 + 少量内部动作。"""
import asyncio
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..providers.base import ProviderError
from ..providers.registry import resolve_llm_call
from ..services import agent_service, skill_service

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/skills")
async def list_skills():
    skills = [
        {"id": s["id"], "name": s["name"], "description": s["description"], "source": s["source"]}
        for s in skill_service.list_skills()
    ]
    return {"skills": skills}

SYSTEM_PROMPT = """你是 AI Video Studio 内置的 AI 助手，帮助用户创作 AI 视频（Prompt、图片、语音、视频）。

当前上下文：
- 页面：{page}
- 当前场景标题：{scene_title}
- 当前场景描述：{scene_description}
- 当前 Prompt：{current_prompt}

规则：
1. 用用户的语言简短回复。
1a. **当前是纯聊天模式，没有任何生成工具**。用户要求实际生成配音/语音/图片/视频/拆分镜并生成等产出时，
   不要只给文字方案、也不要声称已经生成——必须回复引导用户切换模式，格式：
   {{"reply": "聊天模式下我不能直接调用生成工具。请点上方切换到「⚡执行」模式，再把这段剧本发一次，我会实际调用 TTS 生成配音（可拆分镜逐镜配音）。", "action": {{"type": "none"}}}}
2. 如果用户要求修改 Prompt 或文案（例如“把提示词改得更适合H3”“镜头不要移动”“把这个镜头改成3D动画”），返回 JSON：
   {{"reply": "<一句话说明你做了什么>", "action": {{"type": "update_prompt", "value": "<修改后的完整 Prompt>"}}}}
   如果修改的是场景描述，则 type 用 "update_description"。
3. 如果是回答问题、给建议而不需要修改内容，返回 JSON：
   {{"reply": "<你的回答>", "action": {{"type": "none"}}}}
4. 修改 Prompt 时保持结构化分节格式（## Scene / ## Subject / ## Action Timeline / ## Camera / ## Visual Style / ## Lighting / ## Dialogue / Voice / ## Audio / ## Constraints）。
5. **用户要求生成/设计/编写视频 Prompt 时，reply 必须包含完整的结构化 Prompt 全文**（含全部分节与 Action Timeline 逐秒描述），不要概括、不要省略、不要以"已为你设计"代替内容本身；随后附一句简短说明。写 Prompt 前先按 h3-prompt-writing 技能的要点撰写。
6. 只输出 JSON，不要输出任何其他文字。"""


class AgentRequest(BaseModel):
    message: str
    history: list[dict] = []
    page: str = ""
    scene_title: str = ""
    scene_description: str = ""
    current_prompt: str = ""
    credential_id: str | None = None
    model: str | None = None


def _sanitize_history(history: list[dict], max_turns: int = 12, max_len: int = 4000) -> list[dict]:
    """前端传来的历史只保留 user/assistant 角色与限长文本，防止注入与膨胀。"""
    out: list[dict] = []
    for item in history[-max_turns:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()[:max_len]})
    return out


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"reply": text, "action": {"type": "none"}}


class AgentRunRequest(BaseModel):
    goal: str
    history: list[dict] = []
    page: str = ""
    scene_title: str = ""
    scene_description: str = ""
    scene_prompt: str = ""
    scene_narration: str = ""
    scene_image: str | None = None
    aspect_ratio: str = "16:9"
    project_id: str | None = None
    credential_id: str | None = None
    model: str | None = None
    force_route: str | None = None  # 已废弃：双模式路由已拆除，单一入口；参数仅为旧前端兼容保留（后端忽略）


@router.get("/llm-options")
async def llm_options():
    """Agent 侧栏模型选单：当前实际生效的模型 + 全部（模型, 凭据）可选项。

    current 与 run_agent 的解析保持一致：设置里的默认文本模型 > minimax 域凭据自动挑选。
    选项来自各 LLM 凭据端点的 /models 发现（registry 内 10 分钟缓存；同步 httpx 走线程池）。
    """
    from ..config import app_settings, key_manager
    from ..config.settings import PROVIDER_CATALOG
    from ..providers.registry import (
        _resolve_credential,
        list_models_for_credential,
        pick_default_model_for_credential,
    )

    exclude = ("tts", "asr", "voiceclone", "voicedesign", "music", "image", "embedding", "rerank", "whisper")
    options: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for pub in key_manager.list_keys():
        try:
            cred = key_manager.get_key(pub["id"])
        except key_manager.KeyNotFound:
            continue
        info = PROVIDER_CATALOG.get(cred["provider"]) or {}
        if "llm" not in info.get("capabilities", []):
            continue
        for mid in await asyncio.to_thread(list_models_for_credential, cred):
            if any(k in mid.lower() for k in exclude):
                continue
            if (mid, pub["id"]) in seen:
                continue
            seen.add((mid, pub["id"]))
            cname = pub.get("name") or cred["provider"]
            options.append({
                "model": mid,
                "credential_id": pub["id"],
                "provider": cred["provider"],
                "credential_name": cname,
                "label": f"{mid} · {cname}",
            })
    d = app_settings.get_default_llm()
    if d:
        current = {"model": d["model"], "credential_id": d.get("credential_id"), "source": "default"}
    else:
        cred = _resolve_credential(None, "minimax")
        current = {
            "model": await asyncio.to_thread(pick_default_model_for_credential, cred),
            "credential_id": cred["id"],
            "source": "auto",
        }
    return {"current": current, "options": options}


@router.post("/cancel")
async def cancel_agent_run():
    """前端手动停止当前 Agent 运行（下一个决策步生效）。"""
    agent_service.cancel_run()
    return {"ok": True}


@router.post("/run")
async def run(req: AgentRunRequest):
    if not req.goal.strip():
        raise HTTPException(status_code=400, detail="请描述要执行的任务")
    context = _run_context(req)
    try:
        return await agent_service.run_agent(
            req.goal.strip(),
            context,
            req.credential_id,
            req.model,
            history=_sanitize_history(req.history),
            force_route=req.force_route,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_context(req: AgentRunRequest) -> dict:
    return {
        "page": req.page,
        "scene_title": req.scene_title,
        "description": req.scene_description,
        "prompt": req.scene_prompt,
        "narration": req.scene_narration,
        "image": req.scene_image or "",
        "aspect_ratio": req.aspect_ratio,
        "project_id": req.project_id or "",
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post("/run/stream")
async def run_stream(req: AgentRunRequest):
    """流式执行 Agent：SSE 逐步推送工具步骤，结束推最终结果。

    事件格式（text/event-stream）：
    - {"type":"step","step":{...}}   每个决策步实时推送
    - {"type":"final","result":{...}} 与 POST /run 返回体一致
    - {"type":"error","message":"..."} 运行失败
    """
    if not req.goal.strip():
        raise HTTPException(status_code=400, detail="请描述要执行的任务")
    context = _run_context(req)

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        def on_step(step: dict) -> None:
            queue.put_nowait(step)

        task = asyncio.create_task(
            agent_service.run_agent(
                req.goal.strip(),
                context,
                req.credential_id,
                req.model,
                history=_sanitize_history(req.history),
                on_step=on_step,
                force_route=req.force_route,
            )
        )
        # 逐步推送，直到任务结束并排空队列
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
        except Exception as exc:  # noqa: BLE001 - 与 POST /run 的兜底一致
            yield _sse({"type": "error", "message": str(exc)[:500]})
            return
        yield _sse({"type": "final", "result": result})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
async def chat(req: AgentRequest):
    system = SYSTEM_PROMPT.format(
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
