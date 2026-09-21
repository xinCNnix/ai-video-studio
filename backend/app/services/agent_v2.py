"""Agent v2：瘦工具循环（约 500 行，替代旧 2336 行巨石）。

职责收缩为四件事：
1. 路由：导出/续跑直通；带内容的请求在空项目直走 pipeline（零 LLM），在已有
   分镜的项目先做一次 LLM 意图分流（new_script/voice/other）——台词加工类请求
   进"配音台词师"子代理，防止长台词被误当新剧本投放而整表重置
2. 工具循环：全部工具是 harness/store/pipeline/generation_service 的薄封装，
   没有任何业务规则副本；子代理人格在 dispatch 层有工具硬白名单
3. 免 LLM 直通：导出/续跑等确定性指令不烧模型调用

稳定性（harness 兜底，不依赖模型自觉）：
- MAX_STEPS=40、连续畸形输出 3 次熔断、同工具同错误 4 次熔断
- single-flight 运行锁；取消旗标按 run 隔离
- JSON 解析复用 pipeline/llm_client 的信封级实现
"""
import asyncio

from ..config import settings
from ..harness import trace as trace_mod
from ..harness.errors import ProjectNotFoundError, RunBusyError
from ..harness.state import ImageBinding, clamp_duration
from ..harness.watchdog import watchdog
from ..jobs import job_manager
from ..providers.registry import resolve_llm_call
from . import export_service, generation_service, project_store, prompt_timing, skill_service
from .pipeline import llm_client

MAX_STEPS = 40
MAX_BAD_OUTPUT_STREAK = 3
MAX_SAME_TOOL_ERRORS = 4

_TOOL_CONTRACT = """你是 AI Video Studio 的创作助理，操作一个分镜项目（v2 状态模型）。

项目结构：materials（素材池：图/音频/视频/剧本）、scenes（分镜：prompt/narration/duration/gen_mode/binding/assets）、
pipeline（阶段：intake→normalize→plan→bind→review→generate→assemble）、pending_bindings（待确认素材归属）。

可用工具（每步输出一个 JSON：{"action":"tool","tool":"名","args":{...}}，完成后 {"action":"finish","reply":"给用户的总结"}）：

1. get_project_state —— 查看项目全貌（分镜/素材/流水线阶段）。先看再动。
2. run_pipeline —— 投放剧本/素材并跑流水线（args: {"script_text": "...", "materials": [{"url": "..."}]}）。
   分镜拆分、素材分配、生成编排全部由流水线完成，不要自己手工拆镜。
3. advance_pipeline —— 续跑流水线（用户确认了分镜/素材/继续生成时；args: {"confirmed": true}）。
4. update_scene —— 改单镜字段（args: {"scene_id": "...", "prompt": "...", "narration": "...", "duration": 8, ...}）。
   挂有用户素材的分镜其内容视为用户定稿，重写需用户明示。
5. add_scene / delete_scene —— 增删分镜（args: {"scene_id": "...", "index": 可选插入位置}）。
6. bind_material / unbind_material —— 调整素材归属（args: {"material_id": "...", "scene_id": "...", "kind": "image|audio|video", "role": "reference|first_frame"}）。
7. generate_for_scene —— 单镜生成（args: {"scene_id": "...", "kinds": ["keyframe","voice","video"] 起码一项；已有视频/配音要重出时加 "force": true}）。
8. export_project —— 拼接成片。
9. load_skill —— 读取创作方法论（{"name": "screenwriting"} 等）。
10. read_doc —— 读上传的剧本文档全文（{"url": "/api/files/uploads/doc/..."}）。
11. query_jobs / wait_job —— 查任务/等任务终态。"""

_PRODUCTION_RULES = """
行为准则（主代理：导演/制片）：
- 用户意图是投放或更换剧本（"这是新剧本""重新拆分镜"）→ run_pipeline（用户消息全文作
  script_text，附件 URL 放 materials）。反面：用户给台词/旁白让你润色、转配音格式、加
  停顿语气标记——那是文字加工，绝不 run_pipeline（项目已有分镜时它会整表重置），
  只在文本上工作，或 update_scene 改对应镜的 narration。
- 用户说"继续/生成/出片" → advance_pipeline {"confirmed": true}
- 用户问状态/求建议 → 先 get_project_state 再答
- 修改后告诉用户改了什么；生成类操作返回的都是后台任务，不用等
- 台词表现力（用户要情感/停顿/拟声/格式化）：看 settings.voice_model——含 higgs →
  load_skill {"name": "higgs-voice-tags"}；speech- 开头（MiniMax）→
  load_skill {"name": "minimax-voice-tags"}；小米/OpenAI 没有标记体系，别往文本里写标签
- 不知道就问，不要瞎编分镜内容覆盖用户定稿"""

_VOICE_RULES = """
行为准则（子代理：配音台词师）：
- 你只做台词文字加工：润色、断句分段、转目标 TTS 格式、加停顿/语气/注音标记，不碰分镜结构。
- 先 get_project_state 看 settings.voice_model 选技能：含 higgs → load_skill
  {"name": "higgs-voice-tags"}；speech- 开头（MiniMax）→ load_skill {"name": "minimax-voice-tags"}；
  小米/OpenAI 无标记体系，只做纯文本润色。按技能里的转换流程和密度上限加工，
  交付可直接粘贴的台词块（代码块包裹）+ 一两句改动说明。
- 用户明示写入哪一镜时才用 update_scene 改 narration；不确定哪镜就问。
- 以下工具对本代理禁用（系统层同样会拦）：run_pipeline / advance_pipeline / add_scene /
  delete_scene / bind_material / unbind_material / export_project。用户真要重拆分镜、
  重做视频时，让他另发一条明确指令（如"投放新剧本重新拆分镜"）。"""

SYSTEM_PROMPT = _TOOL_CONTRACT + _PRODUCTION_RULES

# ---------------- 取消旗标（按 run 隔离） ----------------
_CANCEL_FLAGS: dict[str, bool] = {}


def cancel_run(run_id: str) -> None:
    """取消指定 run；run_id 为空时取消全部（本地单用户语义，与旧版全局取消兼容）。"""
    if run_id:
        _CANCEL_FLAGS[run_id] = True
    else:
        for k in list(_CANCEL_FLAGS):
            _CANCEL_FLAGS[k] = True


def _cancelled(run_id: str) -> bool:
    return bool(_CANCEL_FLAGS.get(run_id))


def _clear_flag(run_id: str) -> None:
    _CANCEL_FLAGS.pop(run_id, None)


# ---------------- 路由（确定性优先，能不烧 LLM 就不烧） ----------------


def _goal_carries_content(goal: str) -> bool:
    text = (goal or "").strip()
    if len(text) >= 120:
        return True
    return len([ln for ln in text.splitlines() if ln.strip()]) >= 3


def _wants_advance(goal: str, project: dict | None) -> bool:
    text = (goal or "").strip()
    if not text or text.endswith(("？", "?")):
        return False
    scenes = (project or {}).get("scenes") or []
    ready = any((s.get("prompt") or "").strip() for s in scenes)
    if not ready:
        return False
    keywords = ("生成", "继续", "出片", "跑一", "直出", "提交", "开始生成")
    return any(k in text for k in keywords)


def _wants_export(goal: str) -> bool:
    return any(k in (goal or "") for k in ("导出", "拼接成片", "合成成片"))


def route(goal: str, project: dict | None) -> str:
    """返回 pipeline_start / classify / pipeline_advance / export / tools。

    无项目（全局助手侧栏）：一律 tools——侧栏只做问答/文字加工且依赖对话历史，
    带内容的长消息（粘贴台词润色、把上一轮结果贴回再改）曾因"内容长就是剧本"
    被直通 pipeline_start，零 LLM 且无项目直接报"项目不存在"，用户侧表现为失忆。

    带内容的消息：空项目无破坏面，保持零 LLM 直通 pipeline_start；已有分镜时
    返回 classify，由 classify_content_intent 分流——长台词加工请求曾因"内容长
    就是剧本"的误判直通 run_pipeline，把用户分镜整表重置过。
    """
    if not project:
        return "tools"
    if _wants_export(goal) and any(s.get("assets", {}).get("video") for s in project.get("scenes", [])):
        return "export"
    if _goal_carries_content(goal):
        return "pipeline_start" if not (project or {}).get("scenes") else "classify"
    if _wants_advance(goal, project):
        return "pipeline_advance"
    return "tools"


async def classify_content_intent(goal: str, credential_id: str | None, model: str | None,
                                  trace=None) -> str:
    """内容型消息 + 项目已有分镜时的意图分流，返回 pipeline_start / tools_voice / tools。

    误投 run_pipeline 是破坏性操作（整表重置），这里花一次小模型调用兜底；
    模型不可用或输出不合法时保守回 tools（工具循环里还能纠正，反方向救不回来）。
    """
    prompt = (
        "判断用户这条消息的意图，只输出 JSON：{\"intent\": \"new_script|voice|other\"}\n"
        "- new_script：投放/更换剧本，期望重拆分镜、重做视频\n"
        "- voice：给台词/旁白文本做加工（润色、转配音格式、加停顿/语气标记、写旁白文案），不改分镜结构\n"
        "- other：其他（改分镜参数、问状态、继续生成、闲聊等）\n\n"
        f"用户消息：\n{goal[:3000]}"
    )
    try:
        llm, mdl = resolve_llm_call(credential_id, provider_hint="minimax", model=model)
        raw = await llm_client.LLMEnvelope(llm, mdl, trace).raw_call(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=2000)
    except Exception as exc:  # noqa: BLE001 - 分类失败保守走工具循环
        if trace:
            trace.log("route_classify_error", error=str(exc)[:200])
        return "tools"
    intent = ""
    for data in llm_client.parse_json_candidates(raw):
        intent = str(data.get("intent") or "").strip().lower()
        break
    if trace:
        trace.log("route_classified", intent=intent)
    return {"new_script": "pipeline_start", "voice": "tools_voice"}.get(intent, "tools")


# ---------------- 项目工具（全部薄封装，无规则副本） ----------------


def _find_scene(p, scene_id: str | None, index: int | None):
    if scene_id:
        for i, s in enumerate(p.scenes):
            if s.id == scene_id:
                return i, s
        raise KeyError(f"分镜不存在：{scene_id}")
    if index is not None and 0 <= index < len(p.scenes):
        return index, p.scenes[index]
    raise KeyError("必须提供 scene_id 或有效的 index")


def tool_get_project_state(agent) -> dict:
    project = project_store.get(agent.project_id)
    if not project:
        return {"ok": False, "error": "当前没有打开的项目"}
    materials = [
        {"id": m["id"], "kind": m["kind"], "filename": m["filename"], "duration": m.get("duration")}
        for m in project["materials"]
    ]
    scenes = [
        {
            "index": i, "id": s["id"], "title": s["title"], "duration": s["duration"],
            "gen_mode": s["gen_mode"], "status": s["status"],
            "has_prompt": bool(s.get("prompt")), "has_keyframe": bool(s["assets"].get("keyframe")),
            "has_voice": bool(s["assets"].get("voice")), "has_video": bool(s["assets"].get("video")),
            "narration_head": (s.get("narration") or "")[:40],
            "bound_images": len(s["binding"]["images"]), "bound_audios": len(s["binding"]["audios"]),
            "error": s.get("error"),
        }
        for i, s in enumerate(project["scenes"])
    ]
    return {
        "ok": True,
        "project_name": project["name"],
        "aspect_ratio": project["aspect_ratio"],
        "settings": project["settings"],
        "pipeline": project["pipeline"],
        "pending_bindings": project["pending_bindings"],
        "materials": materials,
        "scenes": scenes,
    }


def tool_update_scene(agent, args: dict) -> dict:
    scene_id = args.get("scene_id")
    try:
        index = int(args["index"]) if args.get("index") is not None else None
    except (TypeError, ValueError):
        index = None

    def fn(p):
        _, s = _find_scene(p, scene_id, index)
        if args.get("title") is not None:
            s.title = str(args["title"])[:50]
        for k in ("description", "prompt", "keyframe_prompt", "narration"):
            if args.get(k) is not None:
                setattr(s, k, str(args[k])[:({"prompt": 8000, "narration": 5000, "keyframe_prompt": 2000}.get(k, 500))])
        if args.get("gen_mode") in ("i2v", "t2v"):
            s.gen_mode = args["gen_mode"]
        if args.get("duration") is not None:
            new_duration = clamp_duration(int(args["duration"]), p.settings.video_model)
            if new_duration != s.duration and s.prompt:
                # 与 patch_scene 同一契约：改时长必须同步缩放 Prompt 时间引用
                s.prompt, _ = prompt_timing.rescale_prompt_timing(s.prompt, s.duration, new_duration)
            s.duration = new_duration
        s.revision += 1

    try:
        return {"ok": True, **_snapshot_field(project_store.mutate(agent.project_id, fn), "scenes")}
    except (ProjectNotFoundError, KeyError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _snapshot_field(snapshot: dict, field: str) -> dict:
    return {field: snapshot.get(field)}


def tool_add_scene(agent, args: dict) -> dict:
    from ..harness.state import Scene, new_scene_id

    try:
        index = int(args["index"]) if args.get("index") is not None else None
    except (TypeError, ValueError):
        index = None

    def fn(p):
        scene = Scene(id=new_scene_id(), title=(args.get("title") or "")[:50] or f"分镜 {len(p.scenes) + 1}")
        if args.get("prompt"):
            scene.prompt = str(args["prompt"])[:8000]
        if args.get("narration"):
            scene.narration = str(args["narration"])[:5000]
        if args.get("keyframe_prompt"):
            scene.keyframe_prompt = str(args["keyframe_prompt"])[:2000]
        if args.get("duration"):
            scene.duration = clamp_duration(int(args["duration"]), p.settings.video_model)
        at = index if index is not None else len(p.scenes)
        p.scenes.insert(max(0, min(at, len(p.scenes))), scene)

    try:
        return {"ok": True, **_snapshot_field(project_store.mutate(agent.project_id, fn), "scenes")}
    except (ProjectNotFoundError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:200]}


def tool_delete_scene(agent, args: dict) -> dict:
    scene_id = args.get("scene_id")

    def fn(p):
        i, _ = _find_scene(p, scene_id, None)
        p.scenes.pop(i)

    try:
        return {"ok": True, **_snapshot_field(project_store.mutate(agent.project_id, fn), "scenes")}
    except (ProjectNotFoundError, KeyError) as exc:
        return {"ok": False, "error": str(exc)[:200]}


def tool_bind_material(agent, args: dict) -> dict:
    material_id = str(args.get("material_id") or "")
    scene_id = args.get("scene_id")
    kind = args.get("kind")
    role = args.get("role") or "reference"

    def fn(p):
        _, s = _find_scene(p, scene_id, None)
        if not any(m.id == material_id for m in p.materials):
            raise KeyError(f"素材不存在：{material_id}")
        if kind == "image":
            if not any(x.material_id == material_id for x in s.binding.images):
                s.binding.images.append(ImageBinding(material_id=material_id, role=role))
        elif kind == "audio":
            if material_id not in s.binding.audios:
                s.binding.audios.append(material_id)
        elif kind == "video":
            if material_id not in s.binding.videos:
                s.binding.videos.append(material_id)
        else:
            raise KeyError("kind 必须是 image/audio/video")
        s.revision += 1
        p.pending_bindings = [x for x in p.pending_bindings if x.material_id != material_id]

    try:
        snap = project_store.mutate(agent.project_id, fn)
        return {"ok": True, "scene": next(s for s in snap["scenes"] if s["id"] == scene_id)}
    except (ProjectNotFoundError, KeyError) as exc:
        return {"ok": False, "error": str(exc)[:200]}


def tool_unbind_material(agent, args: dict) -> dict:
    material_id = str(args.get("material_id") or "")
    scene_id = args.get("scene_id")
    kind = args.get("kind")

    def fn(p):
        _, s = _find_scene(p, scene_id, None)
        if kind == "image":
            s.binding.images = [x for x in s.binding.images if x.material_id != material_id]
        elif kind == "audio":
            s.binding.audios = [x for x in s.binding.audios if x != material_id]
        elif kind == "video":
            s.binding.videos = [x for x in s.binding.videos if x != material_id]
        s.revision += 1

    try:
        project_store.mutate(agent.project_id, fn)
        return {"ok": True}
    except (ProjectNotFoundError, KeyError) as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def tool_generate_for_scene(agent, args: dict) -> dict:
    scene_id = args.get("scene_id")
    kinds = [k for k in (args.get("kinds") or []) if k in ("keyframe", "voice", "video")]
    if not kinds:
        return {"ok": False, "error": "kinds 至少包含 keyframe/voice/video 之一"}
    results = {}
    project = project_store.get(agent.project_id)
    if not project:
        return {"ok": False, "error": "当前没有打开的项目"}
    scene = next((s for s in project["scenes"] if s["id"] == scene_id), None)
    if not scene:
        return {"ok": False, "error": f"分镜不存在：{scene_id}"}
    for kind in kinds:
        if kind == "keyframe":
            results["keyframe"] = await generation_service.generate_keyframe(
                agent.project_id, scene_id, prompt_override=args.get("prompt"), trace=agent.trace)
        elif kind == "voice":
            results["voice"] = await generation_service.generate_voice(
                agent.project_id, scene_id, text_override=args.get("text"),
                use_uploaded=bool(args.get("use_uploaded")), force=bool(args.get("force")),
                trace=agent.trace)
        else:
            results["video"] = await generation_service.submit_scene_video(
                agent.project_id, scene_id, force=bool(args.get("force")), trace=agent.trace)
    ok = all(r.get("ok") for r in results.values() if isinstance(r, dict))
    return {"ok": ok, "results": results}


def tool_load_skill(agent, args: dict) -> dict:
    name = str(args.get("name") or "")
    sk = skill_service.get_skill(name)
    if not sk:
        available = ", ".join(s["id"] for s in skill_service.list_skills())
        return {"ok": False, "error": f"技能不存在，可用技能：{available}"}
    filename = str(args.get("file") or "").strip()
    if filename:
        content = skill_service.get_skill_file(sk["id"], filename)
        if content is None:
            files = skill_service.list_skill_files(sk["id"])
            return {"ok": False, "error": f"参考文件不存在，可用：{', '.join(files) or '（无）'}"}
        return {"ok": True, "skill": sk["id"], "file": filename, "content": content[:40000]}
    return {"ok": True, "skill": sk["id"], "content": sk["body"][:30000]}


def tool_read_doc(agent, args: dict) -> dict:
    from pathlib import Path

    from . import file_service

    url = str(args.get("url") or "")
    name = url.rsplit("/", 1)[-1]
    if not url.startswith("/api/files/uploads/doc/"):
        return {"ok": False, "error": "只支持读取本站上传的 doc 文档"}
    if "/" in name or "\\" in name or ".." in name:
        return {"ok": False, "error": "非法路径"}
    path = file_service.settings.UPLOADS_DIR / "doc" / name
    if not path.exists():
        return {"ok": False, "error": f"文档不存在：{name}"}
    try:
        return {"ok": True, "url": url, "content": path.read_text(encoding="utf-8", errors="replace")[:60000]}
    except OSError as exc:
        return {"ok": False, "error": f"读取失败：{exc}"}


def tool_query_jobs(agent, args: dict) -> dict:
    limit = min(max(int(args.get("limit") or 10), 1), 30)
    jobs = job_manager.list(jtype=args.get("type"), status=args.get("status"), limit=limit)
    return {"ok": True, "jobs": [
        {"id": j["id"], "type": j["type"], "status": j["status"], "output": j.get("output"),
         "error": (j.get("error") or "")[:120], "scene_id": (j.get("params") or {}).get("scene_id")}
        for j in jobs
    ]}


async def tool_wait_job(agent, args: dict) -> dict:
    job_id = args.get("job_id") or ""
    try:
        seconds = min(max(int(args.get("seconds") or 20), 1), 25)
    except (TypeError, ValueError):
        seconds = 20
    waited = 0.0
    while waited < seconds:
        job = job_manager.get(job_id)
        if not job:
            return {"ok": False, "error": f"任务 {job_id} 不存在"}
        if job["status"] in ("completed", "failed"):
            return {"ok": True, "status": job["status"], "output": job.get("output"), "error": job.get("error")}
        await asyncio.sleep(2)
        waited += 2
    return {"ok": True, "status": (job_manager.get(job_id) or {}).get("status"), "note": "仍在后台进行中"}


# ---------------- Agent 主体 ----------------

# 配音台词师子代理禁用的工具：全是改分镜结构/触发整表重置/出片类操作，
# 与台词文字加工无关。提示词里声明一份，这里再硬拦一份（不依赖模型自觉）。
VOICE_FORBIDDEN_TOOLS = {
    "run_pipeline", "advance_pipeline", "export_project",
    "add_scene", "delete_scene", "bind_material", "unbind_material",
}


class AgentV2:
    def __init__(self, project_id: str | None, goal: str, credential_id: str | None, model: str | None,
                 history: list[dict] | None = None, on_step=None, run_id: str | None = None):
        self.project_id = project_id
        self.goal = goal
        self.credential_id = credential_id
        self.model = model
        self.history = history or []
        self.on_step = on_step
        self.run_id = run_id or trace_mod.new_run_id("agent")
        self.trace = trace_mod.RunTrace(self.run_id, project_id or "-", "agent", meta={"goal": goal[:200]})
        self.jobs: list[dict] = []
        self.persona = "production"  # production=主代理 / voice=配音台词师

    def step(self, event: dict) -> None:
        if self.on_step:
            self.on_step(event)

    # ---------- 工具分发 ----------

    async def dispatch(self, tool: str, args: dict) -> dict:
        if self.persona == "voice" and tool in VOICE_FORBIDDEN_TOOLS:
            self.trace.log("tool_blocked", tool=tool, persona="voice")
            return {"ok": False, "error": f"配音台词模式禁用 {tool}（防误重置分镜）。需要重拆分镜请让用户另发明确指令。"}
        if tool == "get_project_state":
            return tool_get_project_state(self)
        if tool == "update_scene":
            return tool_update_scene(self, args)
        if tool == "add_scene":
            return tool_add_scene(self, args)
        if tool == "delete_scene":
            return tool_delete_scene(self, args)
        if tool == "bind_material":
            return tool_bind_material(self, args)
        if tool == "unbind_material":
            return tool_unbind_material(self, args)
        if tool == "generate_for_scene":
            return await tool_generate_for_scene(self, args)
        if tool == "load_skill":
            return tool_load_skill(self, args)
        if tool == "read_doc":
            return tool_read_doc(self, args)
        if tool == "query_jobs":
            return tool_query_jobs(self, args)
        if tool == "wait_job":
            return await tool_wait_job(self, args)
        if tool == "run_pipeline":
            from .pipeline import start_pipeline
            payload = {"script_text": args.get("script_text") or "", "materials": args.get("materials") or []}
            self._handoff_to_pipeline()
            return start_pipeline(self.project_id, payload, self.credential_id, self.model)
        if tool == "advance_pipeline":
            from .pipeline import advance_pipeline
            self._handoff_to_pipeline()
            return advance_pipeline(self.project_id, confirmed=bool(args.get("confirmed", True)))
        if tool == "export_project":
            try:
                return await export_service.export_project_v2(self.project_id)
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)[:200]}
        return {"ok": False, "error": f"未知工具：{tool}"}

    # ---------- 主循环 ----------

    async def run(self) -> dict:
        project = project_store.get(self.project_id) if self.project_id else None
        mode = route(self.goal, project)
        if mode == "classify":
            mode = await classify_content_intent(self.goal, self.credential_id, self.model, self.trace)
        self.trace.log("route", mode=mode)
        self.step({"type": "route", "mode": mode})
        try:
            if mode == "pipeline_start":
                result = await self._direct_pipeline_start(project)
            elif mode == "pipeline_advance":
                result = await self._direct_pipeline_advance()
            elif mode == "export":
                result = await self._direct_export()
            else:
                result = await self._tool_loop(project, persona="voice" if mode == "tools_voice" else "production")
            self.trace.end("done", mode=mode)
            return result
        except RunBusyError as exc:
            reply = f"该项目已有任务在进行中（{exc.active_run}），请稍候或先取消。"
            self.trace.end("busy")
            return {"reply": reply, "jobs": [], "run_id": self.run_id}
        except Exception as exc:  # noqa: BLE001 - 顶层兜底：trace 留痕后给用户可读错误
            self.trace.log("error", error=str(exc)[:300])
            self.trace.end("failed")
            return {"reply": f"执行出错：{str(exc)[:200]}", "jobs": [], "run_id": self.run_id, "error": str(exc)[:300]}

    # ---------- 确定性直通（零 LLM） ----------

    def _handoff_to_pipeline(self) -> None:
        """把项目运行锁交还给流水线。

        start/advance_pipeline 与 agent 共用 single-flight 锁；agent 持锁调用必然
        RunBusyError（"已有任务在进行中"的死锁）。release 按 run_id 匹配，只放掉
        自己的锁；交接后 run_agent_v2 的 finally release 因 run_id 不匹配自动空转，
        不会误删流水线新持有的锁。"""
        if self.project_id:
            watchdog.release(self.project_id, self.run_id)

    async def _direct_pipeline_start(self, project) -> dict:
        from .pipeline import start_pipeline

        attachments = _extract_attachments(self.goal)
        payload = {"script_text": _strip_attachment_block(self.goal), "materials": attachments}
        self._handoff_to_pipeline()
        try:
            r = start_pipeline(self.project_id, payload, self.credential_id, self.model)
        except RunBusyError:
            return {"reply": "该项目已有任务在进行中，请稍候再试。", "jobs": [], "run_id": self.run_id}
        except ProjectNotFoundError:
            return {"reply": "项目不存在（可能已被删除），请刷新页面后重试。", "jobs": [], "run_id": self.run_id}
        reply = (
            "已启动流水线：剧本解析 → 分镜规划 → 素材分配，完成后会在确认面板等你确认再生成。"
            "进度实时显示在分镜看板上。"
        )
        if project and project.get("scenes"):
            reply = "检测到新剧本内容，已重置并重新拆分分镜。" + reply
        return {"reply": reply, "jobs": [], "run_id": self.run_id, "pipeline_run_id": r.get("run_id")}

    async def _direct_pipeline_advance(self) -> dict:
        from .pipeline import advance_pipeline

        self._handoff_to_pipeline()
        r = advance_pipeline(self.project_id, confirmed=True)
        return {
            "reply": "已继续流水线（确认门禁按你的指令放行）：补齐缺口并逐镜生成，完成后自动导出成片。",
            "jobs": [], "run_id": self.run_id, "pipeline_run_id": r.get("run_id"),
        }

    async def _direct_export(self) -> dict:
        try:
            r = await export_service.export_project_v2(self.project_id)
        except RuntimeError as exc:
            return {"reply": f"导出失败：{exc}", "jobs": [], "run_id": self.run_id}
        return {"reply": f"成片已导出：{r['url']}（{r['scenes_exported']} 镜）", "jobs": [], "run_id": self.run_id}

    # ---------- LLM 工具循环 ----------

    def _history_messages(self) -> list[dict]:
        """近 8 轮对话历史（无项目聊天路径与工具循环共用）。"""
        out = []
        for h in self.history[-8:]:
            role = h.get("role")
            content = str(h.get("content") or "")[:2000]
            if role in ("user", "assistant") and content:
                out.append({"role": role, "content": content})
        return out

    async def _tool_loop(self, project, persona: str = "production") -> dict:
        self.persona = persona
        if not self.project_id or not project:
            # 无项目上下文（非 Studio 页的全局助手侧栏）：纯聊天/文字加工，带对话记忆
            llm, model = resolve_llm_call(self.credential_id, provider_hint="minimax", model=self.model)
            reply = await llm_client.LLMEnvelope(llm, model, self.trace).raw_call(
                [{"role": "system", "content": "你是 AI Video Studio 的全局 AI 助手（非 Studio 页的侧栏）。"
                                               "当前没有项目上下文：分镜、生成、导出等创作流程请用户到 Studio "
                                               "用页面自带的 Agent 控制台；提问、改 Prompt、润色台词、"
                                               "配音格式转换、写文案等纯文字请求直接完成。"},
                 *self._history_messages(),
                 {"role": "user", "content": self.goal}],
            )
            return {"reply": reply, "jobs": [], "run_id": self.run_id}

        llm, model = resolve_llm_call(self.credential_id, provider_hint="minimax", model=self.model)
        envelope = llm_client.LLMEnvelope(llm, model, self.trace)
        messages: list[dict] = [{"role": "system", "content": _TOOL_CONTRACT + (_VOICE_RULES if persona == "voice" else _PRODUCTION_RULES)}]
        messages.extend(self._history_messages())
        context_state = tool_get_project_state(self)
        messages.append({"role": "user", "content": f"当前项目状态：{context_state}\n\n用户请求：{self.goal}"})

        bad_streak = 0
        error_counts: dict[str, int] = {}
        for step_no in range(MAX_STEPS):
            if _cancelled(self.run_id):
                return {"reply": "已按你的要求停止。", "jobs": [], "run_id": self.run_id, "cancelled": True}
            watchdog.heartbeat(self.project_id)
            try:
                raw = await envelope.raw_call(messages, temperature=0.3, max_tokens=8000)
            except Exception as exc:  # noqa: BLE001 - RetryableError 耗尽
                return {"reply": f"模型调用失败：{str(exc)[:200]}", "jobs": [], "run_id": self.run_id}
            action = _extract_action(raw)
            if action.get("action") == "finish":
                return {"reply": str(action.get("reply") or "")[:4000], "jobs": [], "run_id": self.run_id}
            tool = action.get("tool")
            args = action.get("args") or {}
            if not tool:
                bad_streak += 1
                if bad_streak >= MAX_BAD_OUTPUT_STREAK:
                    return {"reply": "（连续多轮无法解析模型输出，已停止。请换个说法重试。）",
                            "jobs": [], "run_id": self.run_id}
                messages.append({"role": "assistant", "content": raw[:2000]})
                messages.append({"role": "user", "content": "输出格式错误：需要 {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...}} 或 {\"action\":\"finish\",\"reply\":\"...\"}，重新输出。"})
                continue
            bad_streak = 0
            self.step({"type": "step", "tool": tool, "args": args})
            self.trace.log("tool_call", tool=tool, args=args)
            try:
                result = await self.dispatch(tool, args)
            except Exception as exc:  # noqa: BLE001 - 工具异常回喂
                result = {"ok": False, "error": str(exc)[:300]}
            self.trace.log("tool_result", tool=tool, ok=bool(result.get("ok")), result=result)
            self.step({"type": "step", "tool": tool, "ok": bool(result.get("ok")), "result": _brief(result)})
            if isinstance(result, dict) and result.get("job_id"):
                self.jobs.append({"job_id": result["job_id"], "scene_id": args.get("scene_id")})
            if isinstance(result, dict) and not result.get("ok"):
                error_counts[tool] = error_counts.get(tool, 0) + 1
                if error_counts[tool] >= MAX_SAME_TOOL_ERRORS:
                    return {"reply": f"工具 {tool} 连续失败 {error_counts[tool]} 次，已停止：{result.get('error', '')[:200]}",
                            "jobs": [], "run_id": self.run_id}
            messages.append({"role": "assistant", "content": json_dumps_compact(action)})
            messages.append({"role": "user", "content": f"工具结果：{json_dumps_compact(result)[:4000]}"})
        return {"reply": f"已达到单轮工具步数上限（{MAX_STEPS}），先到这里；可以继续发送让我接着做。", "jobs": [], "run_id": self.run_id}


def _extract_action(raw: str) -> dict:
    """宽松解析动作 JSON（围栏/混排/字符串换行容忍）。解析不出返回空 dict（调用方回喂）。"""
    text = (raw or "").strip()
    candidates = llm_client.parse_json_candidates(text)
    for data in candidates:
        if data.get("action") in ("tool", "finish") or data.get("tool"):
            return data
    return {}


def _brief(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("scenes", "project")} if isinstance(result, dict) else result


def json_dumps_compact(data) -> str:
    import json

    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(data)[:2000]


def _extract_attachments(goal: str) -> list[dict]:
    """从消息尾部的【本次附件】区块提取 URL 列表（前端约定格式）。"""
    import re

    urls = re.findall(r"/api/files/uploads/[^\s\]）)】]+", goal or "")
    return [{"url": u} for u in urls]


def _strip_attachment_block(goal: str) -> str:
    idx = (goal or "").find("【本次附件】")
    return goal[:idx].strip() if idx >= 0 else goal.strip()


async def run_agent_v2(goal: str, context: dict, credential_id: str | None = None, model: str | None = None,
                        history: list[dict] | None = None, on_step=None, run_id: str | None = None) -> dict:
    agent = AgentV2(
        project_id=(context or {}).get("project_id"),
        goal=goal,
        credential_id=credential_id,
        model=model,
        history=history,
        on_step=on_step,
        run_id=run_id,
    )
    try:
        if agent.project_id:
            if not watchdog.try_acquire(agent.project_id, agent.run_id, "agent"):
                active = watchdog.current(agent.project_id)
                return {"reply": "该项目已有任务在进行中，请等待完成或先取消。", "jobs": [], "run_id": agent.run_id}
        return await agent.run()
    finally:
        if agent.project_id:
            watchdog.release(agent.project_id, agent.run_id)
        _clear_flag(agent.run_id)
