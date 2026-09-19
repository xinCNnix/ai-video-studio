"""确定性生成流水线：素材齐备的分镜编排任务，全程不经过 LLM。

架构原则（对"复述截断 / 擅自改写 / 凭空补写"事故链的根治）：
用户内容（分镜 Prompt / 旁白 / 时长 / 参考图 / 配音）只在项目文件与任务参数之间
流动，永远不经过任何模型的输出通道。本模块只做三件事：

1. preflight：逐镜预检缺口。有缺口（缺 Prompt、无任何配音、工作流契约不满足）
   就在汇报里明确列出并跳过该镜——绝不猜（不代写台词、不自动 TTS、不补角色卡）。
2. run_pipeline：按固定顺序提交生成任务（音频按旁白文案匹配 ≤3 段、参考图直传、
   Prompt 从项目原样读取），复用 agent_service 的共享提交核心。
3. 模板汇报：确定性中文总结，不用 LLM 写。

对白音频走参考模式随视频请求传入（H3 会把音频烧进成片），因此流水线**不设置
scene.voice**——避免导出拼接时二次混音。
"""
from ..config import settings
from .agent_service import _Agent, _assemble_video_request, _submit_scene_video


def preflight(project: dict, synthesize: bool = False) -> dict:
    """逐镜检查生成前置条件。返回 {"ready": [idx...], "gaps": [{"index", "problems"}]}。

    ready 的分镜具备确定性生成的一切条件；gaps 只报告，不代用户做任何决定。
    synthesize=True（助手交接）：有旁白文案但没音频的分镜视为就绪——旁白是
    Agent 本次写的（非用户素材），流水线代为合成 TTS 属既定「旁白先行」流程；
    synthesize=False（用户已给素材的直跑）：一律要求用户已提供音频，绝不代写代配。
    """
    vmodel = settings.VIDEO_MODELS[0]
    ready: list[int] = []
    gaps: list[dict] = []
    for i, scene in enumerate(project.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        problems: list[str] = []
        if not scene.get("prompt"):
            problems.append("缺分镜 Prompt（在 Studio 补充分镜稿，或用助手按剧本拆分镜）")
        has_audio = bool(scene.get("voice")) or bool(
            [a for a in scene.get("ref_audios") or [] if isinstance(a, str)]
        )
        if not has_audio and not (synthesize and scene.get("narration")):
            problems.append(
                "没有任何配音（无上传音频、无已合成旁白）——直出流水线不代写台词、不自动配音；"
                "该镜确需配音请先上传，或把台词发给我代为处理"
            )
        # 交接模式会先按 keyframe_prompt 机械生成关键帧：契约按生成后的形态预检
        probe = scene
        if synthesize and not scene.get("image") and not (
            scene.get("ref_images") or scene.get("ref_videos") or scene.get("ref_audios")
        ):
            probe = {**scene, "image": "/placeholder_keyframe.png"}
        req = _assemble_video_request(probe, vmodel)
        if req["contract_error"]:
            problems.append(f"视频工作流不满足：{req['contract_error']}")
        if problems:
            gaps.append({"index": i, "problems": problems})
        else:
            ready.append(i)
    return {"ready": ready, "gaps": gaps}


def _build_reply(scenes: list[dict], report: dict, submitted: list[int],
                 skipped: list[int], failed: list[tuple[int, str]],
                 voice_notes: list[str] | None = None) -> str:
    lines: list[str] = []
    for g in report["gaps"]:
        lines.append(f"⚠️ 分镜{g['index'] + 1} 未提交：{'；'.join(g['problems'])}")
    for idx, err in failed:
        lines.append(f"✖ 分镜{idx + 1} 提交失败：{err}")
    for note in voice_notes or []:
        lines.append(f"🎙 {note}")
    for idx in skipped:
        lines.append(f"▪ 分镜{idx + 1}：本次已提交过视频任务，跳过")
    for idx in submitted:
        lines.append(f"✔ 分镜{idx + 1}：视频任务已提交（完成后自动写回分镜）")
    parts = [
        f"📦 生成流水线：{len(scenes)} 镜中 {len(submitted)} 镜已提交视频生成",
        f"（就绪 {len(report['ready'])}、缺口 {len(report['gaps'])}、跳过 {len(skipped)}、失败 {len(failed)}）。",
    ]
    if lines:
        parts.append("\n".join(lines))
    parts.append(
        "视频任务在后台执行，完成后自动写回对应分镜（前端任务面板可看进度）；"
        "全部完成后可在 Studio 点「导出成片」。"
    )
    if report["gaps"]:
        parts.append(
            "缺口处理：补齐素材（把分镜稿/参考图/配音直接发给我即可，我来录入挂载）后再说一声「继续生成」。"
        )
    return "\n".join(parts)


async def run_pipeline(agent: _Agent, goal: str, on_step=None, _start_step: int = 0,
                       synthesize_missing_voice: bool = False) -> dict:
    """执行确定性流水线，返回与 run_agent 一致的结果结构（前端零改动适配）。

    synthesize_missing_voice=True 仅用于创作路径交接（旁白是 Agent 本次写的，
    代为合成 TTS）；用户素材权威的直跑恒为 False（绝不代写代配）。
    """
    def _result(reply: str, steps: list[dict]) -> dict:
        return {
            "reply": reply, "steps": steps, "updates": {}, "jobs": agent.jobs,
            "exports": agent.exports, "voice_reused": agent.voice_reused,
            "project_changed": agent.project_changed,
        }

    steps: list[dict] = []
    n = _start_step

    def emit(step: dict) -> None:
        nonlocal n
        n += 1
        step["step"] = n
        steps.append(step)
        if on_step:
            try:
                on_step(step)
            except Exception:  # noqa: BLE001 - 进度回调失败不影响主流程
                pass

    project = agent._load_project()
    if not project:
        emit({"type": "note", "text": "当前没有打开的项目"})
        return _result("当前没有打开的项目，无法执行生成。", steps)
    scenes = [s for s in project.get("scenes") or [] if isinstance(s, dict)]
    if not scenes:
        return _result("项目还没有分镜。请先在 Studio 录入分镜（或用助手按剧本拆分镜）后再生成。", steps)

    report = preflight(project, synthesize=synthesize_missing_voice)
    # 创作路径交接场景：Agent 循环可能已对部分分镜提交过视频任务，不重复提交
    already_submitted = {
        j.get("scene_index") for j in agent.jobs if j.get("type") == "video"
    }
    submitted: list[int] = []
    skipped: list[int] = []
    failed: list[tuple[int, str]] = []
    voice_notes: list[str] = []
    for idx in report["ready"]:
        if idx in already_submitted:
            skipped.append(idx)
            continue
        # 关键帧补齐（仅交接模式）：创作阶段写好的 keyframe_prompt 在此机械执行生图，
        # 参考链=用户参考图优先，其次引用分镜 0 已生成图保持角色一致——LLM 不参与
        if synthesize_missing_voice:
            fresh0 = agent._load_project() or {}
            sc0 = (fresh0.get("scenes") or [])[idx] if 0 <= idx < len(fresh0.get("scenes") or []) else {}
            if not sc0.get("image") and not (sc0.get("ref_images") or []):
                ref_idx = 0 if idx > 0 and any(
                    s.get("image") for s in (fresh0.get("scenes") or [])[:idx]
                ) else None
                ir = await agent.generate_scene_image(
                    {"index": idx, "reference_index": ref_idx, "allow_missing_reference": True}
                )
                emit({"type": "tool", "tool": "generate_scene_image", "result": ir})
                if not ir.get("ok"):
                    failed.append((idx, f"关键帧生成失败：{ir.get('error')}"))
                    continue
        # 旁白先行（仅交接模式）：Agent 写的旁白先合成 TTS（含历史同文本沿用、时长校准），
        # 再提交视频——generate_scene_voice 会把结果与校准后的时长写回项目文件
        if synthesize_missing_voice:
            fresh = agent._load_project() or {}
            fresh_scenes = fresh.get("scenes") or []
            if 0 <= idx < len(fresh_scenes):
                sc = fresh_scenes[idx]
                if sc.get("narration") and not sc.get("voice") and not (sc.get("ref_audios") or []):
                    vr = await agent.generate_scene_voice({"index": idx})
                    emit({"type": "tool", "tool": "generate_scene_voice", "result": vr})
                    if not vr.get("ok"):
                        failed.append((idx, f"旁白合成失败：{vr.get('error')}"))
                        continue
                    voice_notes.append(f"分镜{idx + 1} 旁白已合成（{vr.get('audio_seconds', '?')}s）")
        # 提交前重读项目：合成配音可能已改写时长，且要与磁盘最新状态一致
        fresh = agent._load_project() or {}
        fresh_scenes = fresh.get("scenes") or []
        if not (0 <= idx < len(fresh_scenes)):
            failed.append((idx, "分镜在执行期间被删除"))
            continue
        r = _submit_scene_video(agent, fresh_scenes[idx], idx)
        emit({"type": "tool", "tool": "generate_scene_video", "result": r})
        if r.get("ok"):
            submitted.append(idx)
        else:
            failed.append((idx, str(r.get("error") or "未知错误")))
    reply = _build_reply(scenes, report, submitted, skipped, failed, voice_notes)
    return _result(reply, steps)
