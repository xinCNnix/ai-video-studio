"""pipeline 阶段实现：intake / normalize / plan / bind / review / generate / assemble。

设计不变量：
- LLM 只在 normalize / plan / bind-兜底 三处出现，且全部走 llm_client 信封
- 用户内容只在项目状态与 LLM 输入之间流动；分镜 Prompt 是 LLM 产物，但生成提交时
  原样读取（generation_service），不再被任何模型二次转述
- 素材清单在 plan 阶段就对 LLM 可见（根治"先拆镜后挂素材"的错配）
- 不确定的素材归属 → pending_bindings，绝不静默挂载
"""
import re
from pathlib import Path

from ...harness.errors import FatalError, NeedsUserInputError
from ...harness.state import (
    Character,
    ImageBinding,
    Material,
    PendingBinding,
    Project,
    Scene,
    clamp_duration,
    new_material_id,
    new_scene_id,
)
from .. import generation_service, material_match, project_store, voice_service

# ---------------- 常量 ----------------

NORMALIZE_CHUNK_CHARS = 3500
PLAN_BATCHHint = 8  # 提示模型单批输出规模上限
PLAN_MAX_BATCHES = 25  # 200 镜安全上限
BIND_CONFIDENCE_THRESHOLD = 0.7

NORMALIZE_SYSTEM = """你是剧本结构化引擎。把剧本解析为标准 JSON 结构（只输出 JSON，不要任何解释文字）。

剧本类型判定（script_type）：
- story: 故事文本/小说片段（无镜头语言）→ 由你拆成多个叙事节拍，一拍 3-8 秒画面
- screenplay: 标准剧本（场景标题/动作/对话）→ 每场拆成 1 个或多个节拍
- storyboard: 用户已有分镜表/分镜稿 → 逐镜对录：一镜一拍，严禁合并、拆分、改写、增删
- ad: 广告/口播稿 → 按卖点与钩子分拍
- materials_only: 只有素材说明、没有剧本正文

输出 JSON：
{"script_type": "...", "logline": "一句话概述",
 "characters": [{"name": "...", "design": "30-60字外观设定"}],
 "beats": [{"title": "...", "description": "画面内容（视觉可拍摄）", "narration": "该拍旁白/台词原文", "source_hint": "原文摘录"}],
 "notes": "风格/注意事项"}

规则：
- beats 严格按播放顺序；description 写"镜头里能看见什么"；narration 写"这一拍说什么"，原文有台词就用原文
- storyboard 类型必须逐镜对录：narration/description 忠实摘录原文，source_hint 保留原句；用户写了镜头号/时长的在 description 里保留
- characters 只列有戏份的主体角色，design 是跨镜一致性的外观锚点（发型/服装/年龄/体态）
- 没有角色/台词的纯风光片，characters 可为空数组
- beats 至少 1 拍；宁多勿漏，后续阶段会再规划分镜"""

PLAN_SYSTEM = """你是分镜规划师。把剧本节拍规划成可执行的视频分镜，只输出 JSON。

每镜字段：
- title: ≤20字
- description: 画面概述
- prompt: 完整视频生成 Prompt（≥100字），结构：
  1) 开头一句画面总述（主体+环境+基调）
  2) ## Action Timeline：按镜内相对时间分段（0-{duration}s 各段之和=时长，严禁沿用剧本全局时刻）
  3) 参考素材声明行：本镜绑定的第 i 张图写 <Picture i>：身份说明；第 i 段音频写 <Audio i>：内容说明——编号按本镜绑定顺序从 1 起
  4) 音频/旁白设计：引用 <Audio i> 或描述环境音
- keyframe_prompt: i2v 镜的关键帧图描述（构图+景别+主体+姿态+光线，≥20字）；t2v 镜留空
- narration: 本镜旁白/台词（无则空字符串）
- duration: 时长（整数秒，在模型允许范围内）
- gen_mode: "i2v"（有关键帧或参考图）或 "t2v"（纯文生，仅用于无主体一致性要求的抽象镜头）
- wants_images/wants_audios/wants_videos: 本镜需要的素材，元素为 {"material_id": "素材id", "description": "用途说明", "role": "reference|first_frame"}

素材使用规则（重要）：
- 素材清单附在用户消息里（带 id/类型/文件名/摘要/时长）。某镜需要某素材时，优先用 material_id 精确引用
- 用户上传的角色/风格参考图是外观锚点，需要一致性的镜都要引用同一素材
- 用户上传的音频是"要烧进该镜成片的台词/配音"，按台词内容挂到对应镜（不要全挂到第一镜）
- 用不上的素材绝不引用；参考音频每镜最多 3 段，参考图每镜最多 9 张
- 挂了用户参考图的镜，Prompt 里的 <Picture N> 声明顺序要与 wants_images 顺序一致

输出 JSON：{"shots": [镜...], "total": 规划总镜数, "done": 是否全部输出完毕}
单批最多输出 8 镜；还有剩余镜时 done=false 并等我继续要求。"""

BIND_SYSTEM = """你是素材对号助手。把未绑定的素材分配到最合适的分镜，只输出 JSON。

规则：
- 只分配"有把握"的：confidence ≥ 0.7；拿不准的不要硬分配（返回低置信或不返回）
- 角色设定图/风格参考图可绑定到多个出现该角色的镜
- 音频按台词内容对号（文件名通常带台词摘要）
- 输出 JSON：{"decisions": [{"material_id": "...", "scene_index": 0, "kind": "image|audio|video", "role": "reference", "confidence": 0.9, "reason": "一句依据"}]}"""


# ---------------- 工具 ----------------


def _store_mutate(project_id: str, fn) -> dict:
    return project_store.mutate(project_id, fn)


def _script_source(project: Project) -> tuple[str, Material | None]:
    """取剧本正文：优先 kind=script 的素材，其次 doc 素材正文。"""
    for m in project.materials:
        if m.kind == "script" and (m.text or "").strip():
            return m.text, m
    for m in project.materials:
        if m.kind == "doc" and (m.text or "").strip():
            return m.text, m
    return "", None


def materials_manifest(project: Project) -> str:
    lines = []
    for m in project.materials:
        if m.kind in ("script", "doc"):
            continue
        desc = m.filename or ""
        if m.kind == "audio":
            tail = material_match.audio_semantic(m.url or m.filename)
            desc = f"{m.filename}（台词摘要：{tail}，时长 {m.duration or '?'}s）" if tail else m.filename
        lines.append(f"- id={m.id} 类型={m.kind} {desc}")
    return "\n".join(lines) or "（无文件素材）"


def _model_constraints(project: Project) -> str:
    vmodel = generation_service.resolve_video_model(project.settings.video_model)
    durations = vmodel.get("durations") or list(range(1, 16))
    modes = vmodel.get("modes") or ["text", "image", "reference"]
    return (
        f"视频模型「{vmodel['label']}」（{vmodel['id']}）：时长 {min(durations)}-{max(durations)}s 整数；"
        f"支持模式 {'/'.join(modes)}（t2v 需要 text 模式支持）。画幅 {project.aspect_ratio}。"
    )


# ---------------- intake（纯代码） ----------------


def run_intake(project_id: str, payload: dict, trace=None) -> dict:
    """登记素材入池：script 正文、doc 提取文本、图/音频/视频记录。幂等：同 URL 不重复登记。

    音频时长探测由 async runner 在 intake 后调 probe_audio_durations 完成。
    """
    script_text = (payload.get("script_text") or "").strip()
    material_items = payload.get("materials") or []

    def fn(p: Project) -> None:
        existing_urls = {m.url for m in p.materials if m.url}
        if script_text:
            p.materials.append(Material(
                id=new_material_id(), kind="script", text=script_text[:200000],
                filename="inline_script.txt", url=None,
            ))
        for item in material_items:
            url = str(item.get("url") or "")
            if not url or url in existing_urls:
                continue
            kind = str(item.get("kind") or _infer_kind(url))
            p.materials.append(Material(
                id=new_material_id(), kind=kind, url=url,
                filename=str(item.get("filename") or url.rsplit("/", 1)[-1])[:200],
                text=(item.get("text") or None),
            ))

    _store_mutate(project_id, fn)

    # doc 正文提取（同步读本地文件）
    def fn2(p: Project) -> None:
        from .. import file_service

        for m in p.materials:
            if m.kind == "doc" and not m.text and m.url:
                local = file_service.resolve_local(m.url)
                if local and Path(local).exists():
                    try:
                        m.text = Path(local).read_text(encoding="utf-8", errors="replace")[:200000]
                    except OSError:
                        pass

    _store_mutate(project_id, fn2)
    if trace:
        trace.log("stage_done", stage="intake", materials=len(project_store.get(project_id)["materials"]))
    return {"ok": True}


def _infer_kind(url: str) -> str:
    u = url.lower()
    if "/image/" in u:
        return "image"
    if "/audio/" in u:
        return "audio"
    if "/video/" in u:
        return "video"
    if "/doc/" in u:
        return "doc"
    return "file"


async def probe_audio_durations(project_id: str) -> None:
    """补齐音频素材时长（runner 在 intake 后调用）。"""
    project_dict = project_store.get(project_id)
    if not project_dict:
        return
    project = Project.model_validate(project_dict)
    updates: dict[str, float] = {}
    for m in project.materials:
        if m.kind == "audio" and m.duration is None and m.url:
            try:
                sec = await voice_service.probe_audio_seconds(m.url)
            except Exception:  # noqa: BLE001
                continue
            if sec:
                updates[m.id] = sec
    if updates:
        def fn(p: Project) -> None:
            for m in p.materials:
                if m.id in updates:
                    m.duration = updates[m.id]
        _store_mutate(project_id, fn)


# ---------------- normalize（LLM 信封） ----------------


def _split_source(text: str, limit: int = NORMALIZE_CHUNK_CHARS) -> list[str]:
    """确定性输入分块：按空行/段落切到 ≤limit，防 LLM 输出被 max_tokens 截断整段丢稿。"""
    if len(text) <= limit:
        return [text]
    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


async def run_normalize(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    source, mat = _script_source(project)
    if not source.strip():
        raise NeedsUserInputError("项目还没有剧本正文：请投放剧本文本，或上传 .md/.txt 剧本文件后再启动流水线")

    chunks = _split_source(source)
    beats: list[dict] = []
    characters: dict[str, str] = {}
    script_type = ""
    notes: list[str] = []
    logline = ""
    from .schemas import Screenplay

    for i, chunk in enumerate(chunks):
        part = f"【剧本片段 {i + 1}/{len(chunks)}】\n{chunk}" if len(chunks) > 1 else chunk
        extra = f"\n\n（整部剧本已分 {len(chunks)} 片段，本片段类型沿用首片段判定：{script_type}）" if script_type and len(chunks) > 1 else ""
        result = await envelope.structured_call(NORMALIZE_SYSTEM, f"解析以下剧本：\n\n{part}{extra}", Screenplay)
        script_type = script_type or result.script_type
        logline = logline or result.logline
        notes.append(result.notes)
        for c in result.characters:
            characters.setdefault(c.name, c.design)
        beats.extend(b.model_dump() for b in result.beats)
        if trace:
            trace.log("normalize_chunk", chunk=i + 1, total=len(chunks), beats=len(result.beats))

    screenplay = {
        "script_type": script_type,
        "logline": logline,
        "characters": [{"name": k, "design": v} for k, v in characters.items()],
        "beats": beats,
        "source_material_id": mat.id if mat else None,
    }

    def fn(p: Project) -> None:
        p.screenplay = screenplay
        p.characters = [Character(name=k, design=v) for k, v in characters.items()]

    _store_mutate(project_id, fn)
    return {"ok": True, "script_type": script_type, "beats": len(beats), "characters": list(characters)}


# ---------------- plan（LLM 信封 + 分批） ----------------


async def run_plan(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    if not project.screenplay:
        raise FatalError("项目缺少剧本结构（screenplay），请先执行 normalize 阶段")
    beats = project.screenplay.get("beats") or []
    manifest = materials_manifest(project)
    constraints = _model_constraints(project)
    chars = project.screenplay.get("characters") or []

    beats_text = "\n".join(
        f"{i + 1}. [{b.get('title') or ''}] 画面：{b.get('description') or ''}｜旁白：{b.get('narration') or ''}"
        for i, b in enumerate(beats)
    )
    base_user = (
        f"视频模型约束：{constraints}\n\n角色设定（跨镜一致，Prompt 中需体现或用参考图锚定）：\n"
        + ("\n".join(f"- {c['name']}：{c['design']}" for c in chars) or "（无）")
        + f"\n\n素材清单（需引用素材时用 material_id）：\n{manifest}\n\n剧本节拍（按播放顺序，共 {len(beats)} 拍）：\n{beats_text}\n\n"
        f"规划全部分镜。先输出发音完整的前 {PLAN_BATCHHint} 镜（done=false），后续我会要求继续。"
    )

    from .schemas import PlanBatch

    all_shots: list[dict] = []
    messages_extra = ""
    for batch_no in range(PLAN_MAX_BATCHES):
        user = base_user if batch_no == 0 else (
            f"继续输出分镜。已输出 {len(all_shots)} 镜：\n"
            + "\n".join(f"- {i + 1}. {s['title']}" for i, s in enumerate(all_shots))
            + f"\n\n请从第 {len(all_shots) + 1} 镜继续，单批最多 {PLAN_BATCHHint} 镜，全部输出完时 done=true。{messages_extra}"
        )
        batch = await envelope.structured_call(PLAN_SYSTEM, user, PlanBatch)
        all_shots.extend(s.model_dump() for s in batch.shots)
        if trace:
            trace.log("plan_batch", batch=batch_no + 1, got=len(batch.shots), total_planned=batch.total, done=batch.done)
        if batch.done or not batch.shots:
            break
    if not all_shots:
        raise FatalError("plan 阶段没有产出任何分镜")

    vmodel = generation_service.resolve_video_model(project.settings.video_model)
    modes = set(vmodel.get("modes") or ["text", "image", "reference"])
    notes: list[str] = []
    scenes: list[Scene] = []
    for i, sp in enumerate(all_shots):
        gen_mode = sp["gen_mode"]
        duration = clamp_duration(sp["duration"], project.settings.video_model)
        if gen_mode == "t2v" and "text" not in modes:
            gen_mode = "i2v"
            notes.append(f"分镜{i + 1} 规划为 t2v 但当前模型不支持纯文生，已转为 i2v（请补充关键帧）")
        keyframe_prompt = sp["keyframe_prompt"]
        if gen_mode == "i2v" and not keyframe_prompt.strip():
            keyframe_prompt = sp["description"] or sp["prompt"][:200]
        scenes.append(Scene(
            id=new_scene_id(),
            title=(sp["title"] or f"分镜 {i + 1}")[:50],
            description=sp["description"][:500],
            prompt=sp["prompt"][:8000],
            keyframe_prompt=keyframe_prompt[:2000],
            narration=sp["narration"][:5000],
            duration=duration,
            gen_mode=gen_mode,
        ))

    # 角色设定卡注入：无参考图锚定的镜，Prompt 末尾强制注入（跨镜一致性兜底）
    char_block = ""
    if chars:
        char_block = "\n\n## Character Reference（跨镜一致，外观逐字遵守）\n" + "\n".join(
            f"- {c['name']}：{c['design']}" for c in chars
        )
    if char_block:
        for i, sp in enumerate(all_shots):
            if sp.get("wants_images"):
                continue  # 挂参考图的镜外观以图为准，不注入文字设定卡
            if not any(c.get("design", "")[:20] in scenes[i].prompt for c in chars if c.get("design")):
                scenes[i].prompt = (scenes[i].prompt.rstrip() + char_block)[:8000]

    def fn(p: Project) -> None:
        p.scenes = [Scene(**s.model_dump()) for s in scenes]
        p.characters = [Character(name=c["name"], design=c.get("design", "")) for c in chars]
        # 分镜重规划：旧生成物对新剧本是脏数据，不迁移；素材池不动，绑定由 bind 阶段重建
        p.pending_bindings = []

    _store_mutate(project_id, fn)
    # wants 不落盘到 Scene（属 LLM 规划中间产物），存 screenplay 供 bind 阶段使用
    def fn2(p: Project) -> None:
        if p.screenplay:
            p.screenplay["wants"] = [
                {
                    "scene_id": scenes[i].id,
                    "wants_images": sp.get("wants_images") or [],
                    "wants_audios": sp.get("wants_audios") or [],
                    "wants_videos": sp.get("wants_videos") or [],
                }
                for i, sp in enumerate(all_shots)
            ]

    _store_mutate(project_id, fn2)
    return {"ok": True, "shots": len(scenes), "notes": notes[:10]}


# ---------------- bind（确定性优先 + LLM 兜底） ----------------


async def run_bind(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    if not project.scenes:
        raise FatalError("项目没有分镜，请先执行 plan 阶段")
    materials = {m.id: m for m in project.materials}
    wants_map = {w["scene_id"]: w for w in ((project.screenplay or {}).get("wants") or [])}

    decisions: list[tuple[str, str, str, str]] = []  # (scene_id, kind, material_id, role)
    pending: list[dict] = []
    claimed: set[str] = set()

    # 1) wants 里的 material_id 精确引用
    for scene in project.scenes:
        w = wants_map.get(scene.id) or {}
        for want in w.get("wants_images") or []:
            mid = want.get("material_id")
            if mid and mid in materials:
                decisions.append((scene.id, "image", mid, want.get("role") or "reference"))
                claimed.add(mid)
        for want in w.get("wants_audios") or []:
            mid = want.get("material_id")
            if mid and mid in materials and materials[mid].kind == "audio":
                decisions.append((scene.id, "audio", mid, "reference"))
                claimed.add(mid)
        for want in w.get("wants_videos") or []:
            mid = want.get("material_id")
            if mid and mid in materials:
                decisions.append((scene.id, "video", mid, "reference"))
                claimed.add(mid)

    # 2) 未被引用的上传音频：确定性台词对号分发
    free_audios = [m.url for m in project.materials if m.kind == "audio" and m.url and m.id not in claimed]
    if free_audios:
        scene_texts = [(s.prompt, s.narration) for s in project.scenes]
        claims, unmatched, overflow = material_match.distribute_audios(free_audios, scene_texts)
        for idx, urls in claims.items():
            for u in urls:
                mid = next(m.id for m in project.materials if m.url == u)
                decisions.append((project.scenes[idx].id, "audio", mid, "reference"))
                claimed.add(mid)
        for u in unmatched:
            mid = next(m.id for m in project.materials if m.url == u)
            pending.append({
                "material_id": mid,
                "note": f"音频「{u.rsplit('/', 1)[-1]}」的台词没有命中任何分镜",
                "suggested_scene_id": None,
            })

    # 3) 未绑定的图/视频素材 → LLM 兜底（带置信度）
    free_others = [m for m in project.materials if m.kind in ("image", "video") and m.url and m.id not in claimed]
    if free_others and len(project.scenes) > 0:
        manifest = "\n".join(
            f"- material_id={m.id} 类型={m.kind} {m.filename}" for m in free_others
        )
        scenes_desc = "\n".join(
            f"- scene_index={i} {s.title}：{(s.description or s.prompt)[:80]}｜旁白：{(s.narration or '')[:40]}"
            for i, s in enumerate(project.scenes)
        )
        from .schemas import BindDecisionList

        result = await envelope.structured_call(
            BIND_SYSTEM,
            f"未分配素材：\n{manifest}\n\n分镜列表：\n{scenes_desc}\n\n把每个素材分配到最合适的分镜（一个素材可多次出现）。",
            BindDecisionList,
        )
        for d in result.decisions:
            if d.material_id not in materials or not (0 <= d.scene_index < len(project.scenes)):
                continue
            if d.material_id in claimed and d.kind == "audio":
                continue
            if d.confidence >= BIND_CONFIDENCE_THRESHOLD:
                decisions.append((project.scenes[d.scene_index].id, d.kind, d.material_id, d.role))
                claimed.add(d.material_id)
            elif d.confidence >= 0.3:
                pending.append({
                    "material_id": d.material_id,
                    "note": f"低置信（{d.confidence}）：{d.reason[:80]}",
                    "suggested_scene_id": project.scenes[d.scene_index].id,
                })

    # 落盘绑定（尊重契约上限：音频每镜≤3、图每镜≤9，超限的丢弃并记录）
    def fn(p: Project) -> None:
        audio_count: dict[str, int] = {}
        image_count: dict[str, int] = {}
        for scene in p.scenes:
            scene.binding = type(scene.binding)()
        for scene_id, kind, mid, role in decisions:
            scene = next((s for s in p.scenes if s.id == scene_id), None)
            if not scene or mid not in {m.id for m in p.materials}:
                continue
            if kind == "image":
                if image_count.get(scene_id, 0) >= 9:
                    continue
                scene.binding.images.append(ImageBinding(material_id=mid, role=role))
                image_count[scene_id] = image_count.get(scene_id, 0) + 1
            elif kind == "audio":
                if audio_count.get(scene_id, 0) >= 3:
                    continue
                scene.binding.audios.append(mid)
                audio_count[scene_id] = audio_count.get(scene_id, 0) + 1
            else:
                scene.binding.videos.append(mid)
        # 去重保序
        for scene in p.scenes:
            seen: set[tuple] = set()
            scene.binding.images = [x for x in scene.binding.images if (x.material_id, x.role) not in seen and not seen.add((x.material_id, x.role))]
            scene.binding.audios = list(dict.fromkeys(scene.binding.audios))
            scene.binding.videos = list(dict.fromkeys(scene.binding.videos))
        p.pending_bindings = [PendingBinding(**pb) for pb in pending]

    _store_mutate(project_id, fn)
    if trace:
        trace.log("stage_done", stage="bind", decisions=len(decisions), pending=len(pending))
    result = {"ok": True, "bound": len(decisions), "pending": len(pending)}
    if pending:
        result["pending_bindings"] = pending
    return result
