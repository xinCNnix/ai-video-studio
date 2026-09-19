"""统一生成服务：v2 项目的唯一生成入口（关键帧/配音/视频）。

手动按钮、agent 工具、pipeline generate 阶段全部经过这里——mode 判定、参考素材
排序、<Audio N> 重编号、契约校验、模型选择、时长钳制在整条链路只有这一份实现。
- 关键帧/配音：内联等待完成，直接经 project_store 写回
- 视频：提交即返回，后台任务轮询写回（Prompt 指纹校验，防旧 Prompt 的视频串进新剧本）
"""
import asyncio
import hashlib
import re

from ..config import model_store, settings, voice_assets
from ..config.key_manager import list_keys
from ..harness.state import Material, Project, Scene, clamp_duration, find_video_model
from ..jobs import job_manager
from ..jobs.manager import ACTIVE_STATUSES
from . import file_service, image_service, material_match, video_service, voice_service
from . import project_store

VIDEO_POLL_MAX = 25 * 60  # 写回侧轮询上限（生成侧 20min + 下载余量）


def prompt_fingerprint(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------- 模型解析 ----------------


def pick_image_model(preferred: str | None = None) -> dict:
    """项目设置优先，其次第一个有可用凭据厂商的模型（含自定义）。"""
    models = settings.IMAGE_MODELS + model_store.list_custom().get("image", [])
    if preferred:
        for m in models:
            if m.get("id") == preferred:
                return m
    providers = {k.get("provider") for k in list_keys()}
    for m in models:
        if m.get("provider") in providers:
            return m
    return models[0]


def resolve_video_model(preferred: str | None = None) -> dict:
    m = find_video_model(preferred)
    return m or settings.VIDEO_MODELS[0]


def _has_xiaomi_key() -> bool:
    return any(
        k.get("provider") == "xiaomi" or "xiaomimimo" in (k.get("base_url") or "").lower()
        for k in list_keys()
    )


def tts_provider_for_model(model: str) -> str:
    """按模型 ID 判定 TTS 协议厂商：mimo-* 走小米，其余走 MiniMax/通用协议。"""
    return "xiaomi" if model.strip().lower().startswith("mimo-") else "minimax"


def default_tts_setup() -> tuple[str, str, str]:
    """解析默认 TTS (provider, model, voice)：MiniMax 自定义模型优先，其次小米。"""
    from ..providers.tts.xiaomi_tts import XIAOMI_PRESET_VOICES

    custom = model_store.list_custom()
    m = next((x["id"] for x in custom.get("tts", []) if x.get("provider") == "minimax"), "")
    if m:
        return "minimax", m, next((v["id"] for v in custom.get("voice", [])), "female-shaonv")
    m = next((x["id"] for x in custom.get("tts", []) if x.get("provider") == "xiaomi"), "")
    if not m and _has_xiaomi_key():
        m = "mimo-v2.5-tts"
    if m:
        clone_asset = next((a for a in voice_assets.list_assets() if a["kind"] == "clone"), None)
        if clone_asset:
            return "xiaomi", m, clone_asset["id"]
        presets = {v["id"] for v in XIAOMI_PRESET_VOICES}
        voice = next((v["id"] for v in custom.get("voice", []) if v["id"] in presets), "冰糖")
        return "xiaomi", m, voice
    return "minimax", "", next((v["id"] for v in custom.get("voice", [])), "female-shaonv")


def find_completed_tts(text: str, limit: int = 1000) -> dict | None:
    """按文本身份查找已完成的配音任务（同文本沿用，重跑不重复烧钱）。"""
    target = material_match.norm_tts_text(text)
    if not target:
        return None
    need = min(len(target), 100)
    for j in job_manager.list(jtype="tts", status="completed", limit=limit):
        if not j.get("output"):
            continue
        ptext = material_match.norm_tts_text(str((j.get("params") or {}).get("text") or ""))
        if not ptext:
            continue
        if target == ptext or (len(ptext) >= need and target.startswith(ptext)):
            return j
    return None


def calibrate_duration(audio_seconds: float, old: int, model_id: str | None) -> tuple[int, str | None]:
    """按旁白实际时长校准分镜时长（唯一实现）。返回 (新时长, 跟进提示)。"""
    new_dur = clamp_duration(int(audio_seconds + 0.999), model_id)
    note = None
    if abs(new_dur - old) >= 1:
        note = f"该镜时长已从 {old}s 校准为 {new_dur}s（旁白实际 {audio_seconds:.1f}s）——Prompt 的 Action Timeline 需同步重排为 0-{new_dur}s"
    model = find_video_model(model_id)
    if model and model.get("durations") and new_dur >= max(model["durations"]):
        note = ((note + "；") if note else "") + (
            f"旁白 {audio_seconds:.1f}s 接近/超过模型时长上限 {max(model['durations'])}s，"
            "建议拆成两镜或精简台词"
        )
    return new_dur, note


# ---------------- 素材解析与请求组装 ----------------


def scene_material_urls(scene: Scene, materials: dict[str, Material]) -> dict:
    """把绑定关系解析成 URL 清单（关键帧=生成首帧或 first_frame 绑定素材）。"""
    ref_images: list[str] = []
    first_frame: str | None = scene.assets.keyframe
    for b in scene.binding.images:
        m = materials.get(b.material_id)
        if not m or not m.url:
            continue
        if b.role == "first_frame":
            first_frame = m.url
        else:
            ref_images.append(m.url)
    return {
        "first_frame": first_frame,
        "ref_images": ref_images,
        "ref_audios": [materials[x].url for x in scene.binding.audios
                       if x in materials and materials[x].url],
        "ref_videos": [materials[x].url for x in scene.binding.videos
                       if x in materials and materials[x].url],
    }


def assemble_video_request(scene: Scene, materials: dict[str, Material], vmodel: dict) -> dict:
    """纯函数：从 v2 分镜状态组装视频生成请求（mode/images/参考素材/契约校验）。

    组装规则（历史事故修复的唯一实现）：
    - i2v+参考混传：官方 V2 禁止，关键帧并入参考图走 reference 模式
    - 音频同台词去重、按台词位置排序、<Audio N> 按实际传入重编号
    - 参考图/视频按 <Picture/Video N> 声明顺序对齐
    - AutoDL 工作流契约提交前拦截
    """
    urls = scene_material_urls(scene, materials)
    ref_images = material_match.sort_ref_assets_by_prompt(urls["ref_images"], scene.prompt, "Picture")
    ref_videos = material_match.sort_ref_assets_by_prompt(urls["ref_videos"], scene.prompt, "Video")
    all_audios = urls["ref_audios"]
    unique_audios, dupe_audios = material_match.dedupe_same_text_audios(all_audios)
    notes: list[str] = []
    if dupe_audios:
        notes.append(f"{len(dupe_audios)} 段同台词重复音频未传入（同台词只传一段，多传会把同一句烧两遍）")
    ref_audios = material_match.sort_audios_by_prompt(unique_audios, scene.prompt, scene.narration)
    if len(ref_audios) > 3:
        ref_audios, dropped = material_match.pick_matching_audios(unique_audios, scene.prompt, scene.narration, limit=3)
        notes.append(f"该镜挂有 {len(all_audios)} 段音频超出上限 3 段，已按旁白文案自动挑选 {len(ref_audios)} 段传入")

    keyframe = urls["first_frame"]
    has_refs = bool(ref_images or ref_videos or ref_audios)
    if scene.gen_mode == "i2v":
        if keyframe:
            images = [keyframe, *ref_images]
            mode = "reference" if has_refs else "image"
        elif has_refs:
            # 无关键帧但挂了用户参考素材：参考模式直接吃用户图，不强制生成关键帧
            images = list(ref_images)
            mode = "reference"
        else:
            return {"contract_error": "i2v 镜既没有关键帧也没有参考素材：请先生成关键帧或绑定用户参考图"}
    else:
        images = list(ref_images)
        mode = "reference" if has_refs else "text"

    prompt = scene.prompt
    tag_error = ""
    audio_tags = material_match.audio_tags_in_order(prompt)
    if audio_tags and not ref_audios:
        tag_error = (
            f"Prompt 引用了 {'/'.join(f'<Audio {t}>' for t in audio_tags)} 标签，但本镜没有绑定音频："
            "悬空标签会让模型自行编造语音——请绑定音频或去掉 Prompt 里的音频标签"
        )
    elif len(audio_tags) > len(ref_audios):
        tag_error = (
            f"Prompt 引用了 {len(audio_tags)} 个音频标签，但本镜只传入 {len(ref_audios)} 段音频："
            "多余标签指向不存在的素材——请补绑音频或改写 Prompt"
        )
    else:
        prompt, tag_note = material_match.renumber_audio_tags(prompt, ref_audios)
        if tag_note:
            notes.append(tag_note)
    if not tag_error:
        for label, passed in (("Picture", images), ("Video", ref_videos)):
            nums = [int(n) for n in re.findall(rf"<{label}\s+(\d+)>", prompt)]
            if nums and max(nums) > len(passed):
                tag_error = (
                    f"Prompt 引用了 <{label} {max(nums)}> 标签，但本镜只传入 {len(passed)} 个参考"
                    f"{'图' if label == 'Picture' else '视频'}：悬空标签会让模型找不到指定素材"
                )
                break
    contract_error = tag_error or video_service.autodl_contract_error(
        vmodel["id"], mode, images, ref_videos, ref_audios, prompt=prompt
    )
    return {
        "mode": mode, "images": images, "ref_videos": ref_videos, "ref_audios": ref_audios,
        "prompt": prompt, "contract_error": contract_error, "notes": notes,
        "fingerprint": prompt_fingerprint(scene.prompt),
    }


# ---------------- 写回辅助 ----------------


def writeback_completed_video(project_id: str, scene_id: str, output: str, fingerprint: str = "") -> str | None:
    """把已完成的视频产物写回 v2 分镜（重启恢复路径复用；指纹校验）。

    返回 None = 成功；字符串 = 未写回原因。
    """
    try:
        project = project_store.get(project_id)
        if not project:
            return f"项目 {project_id} 不存在，无法写回"
        scene = next((s for s in project.scenes if s.id == scene_id), None)
        if not scene:
            return f"分镜 {scene_id} 已不存在，无法写回"
        current_fp = prompt_fingerprint(scene.get("prompt") or "")
        if fingerprint and current_fp != fingerprint:
            return f"分镜 {scene_id} 的 Prompt 在生成期间已被改写（提交指纹 {fingerprint}，当前 {current_fp}），产物未写回"

        def fn(p: Project) -> None:
            s = _find_scene(p, scene_id)
            if not s:
                return
            s.assets.video = output
            s.status = "done"
            s.error = None

        project_store.mutate(project_id, fn)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"写回分镜 {scene_id} 失败：{str(exc)[:150]}"


def _find_scene(p: Project, scene_id: str) -> Scene | None:
    return next((s for s in p.scenes if s.id == scene_id), None)


def job_active(job_id: str | None) -> bool:
    if not job_id:
        return False
    j = job_manager.get(job_id)
    return bool(j and j.get("status") in ACTIVE_STATUSES)


# ---------------- 关键帧生成 ----------------


async def generate_keyframe(project_id: str, scene_id: str, *, prompt_override: str | None = None,
                            reference_scene_id: str | None = None, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    if not project_dict:
        return {"ok": False, "error": "项目不存在"}
    project = Project.model_validate(project_dict)
    scene = _find_scene(project, scene_id)
    if not scene:
        return {"ok": False, "error": f"分镜不存在：{scene_id}"}
    if job_active(scene.assets.keyframe_job):
        return {"ok": False, "error": "该分镜已有进行中的关键帧任务", "job_id": scene.assets.keyframe_job}
    materials = {m.id: m for m in project.materials}
    prompt = (prompt_override or scene.keyframe_prompt or scene.description or scene.prompt or "")[:2000]
    if len(prompt.strip()) < 8:
        return {"ok": False, "error": "关键帧描述过短或缺失（keyframe_prompt），请先补充分镜的关键帧描述"}

    # 一致性参考：本镜绑定的用户参考图优先，其次指定分镜的既有关键帧（角色一致性锚点）
    references: list[str] = scene_material_urls(scene, materials)["ref_images"]
    if reference_scene_id:
        ref_scene = _find_scene(project, reference_scene_id)
        ref_url = ref_scene.assets.keyframe if ref_scene else None
        if not ref_url:
            return {"ok": False, "error": f"参考分镜 {reference_scene_id} 没有关键帧，无法作为一致性参考"}
        references.append(ref_url)

    aspect = project.aspect_ratio or "16:9"
    model_entry = pick_image_model(project.settings.image_model)
    if trace:
        trace.log("tool_call", tool="generate_keyframe", scene_id=scene_id, model=model_entry["id"])
    job = job_manager.create("image", model_entry["provider"], model_entry["id"], {
        "prompt": prompt[:200], "aspect_ratio": aspect, "scene_id": scene_id, "project_id": project_id,
    })
    project_store.mutate(project_id, lambda p: _mark(p, scene_id, keyframe_job=job["id"], status="generating", error=None))
    job_manager.attach(
        job["id"],
        image_service.run_image_job(
            job_id=job["id"], provider=model_entry["provider"], model=model_entry["id"],
            credential_id=None, prompt=prompt, size=None, n=1, references=references,
        ),
    )
    waited = 0.0
    while waited < 120:
        j = job_manager.get(job["id"])
        if j["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(2)
        waited += 2
    j = job_manager.get(job["id"]) or {}
    if j.get("status") != "completed":
        def _fail(p: Project) -> None:
            s = _find_scene(p, scene_id)
            if s:
                s.status = "failed"
                s.error = f"关键帧生成失败：{j.get('error') or '超时（120s）'}"
                s.attempts["keyframe"] = s.attempts.get("keyframe", 0) + 1
        project_store.mutate(project_id, _fail)
        if trace:
            trace.log("tool_result", tool="generate_keyframe", ok=False, error=j.get("error"))
        return {"ok": False, "error": f"关键帧生成未完成：{j.get('error') or '超时（120s）'}", "job_id": job["id"]}
    output = j["output"]

    def _write(p: Project) -> None:
        s = _find_scene(p, scene_id)
        if s:
            s.assets.keyframe = output
            s.assets.keyframe_job = job["id"]
            if s.status in ("generating", "failed"):
                s.status = "ready" if not s.assets.video else "done"
            s.error = None

    project_store.mutate(project_id, _write)
    if trace:
        trace.log("tool_result", tool="generate_keyframe", ok=True, output=output)
    return {"ok": True, "scene_id": scene_id, "keyframe": output, "self_check": j.get("self_check")}


# ---------------- 配音生成 ----------------


async def generate_voice(project_id: str, scene_id: str, *, text_override: str | None = None,
                         use_uploaded: bool = False, force: bool = False, model: str | None = None,
                         voice: str | None = None, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    if not project_dict:
        return {"ok": False, "error": "项目不存在"}
    project = Project.model_validate(project_dict)
    scene = _find_scene(project, scene_id)
    if not scene:
        return {"ok": False, "error": f"分镜不存在：{scene_id}"}
    if job_active(scene.assets.voice_job):
        return {"ok": False, "error": "该分镜已有进行中的配音任务", "job_id": scene.assets.voice_job}
    materials = {m.id: m for m in project.materials}
    uploaded = scene_material_urls(scene, materials)["ref_audios"]
    text = (text_override or scene.narration or "")[:20000]

    if use_uploaded:
        if not uploaded:
            if not text:
                return {"ok": False, "error": "该镜既没有绑定上传音频也没有旁白文案，无从落实配音"}
        else:
            picked, _ = material_match.pick_matching_audios(uploaded, scene.prompt, scene.narration, limit=1)
            audio_url = picked[0]
            audio_seconds = await voice_service.probe_audio_seconds(audio_url)
            project_store.mutate(project_id, lambda p: _set_voice(p, scene_id, audio_url))
            result = {
                "ok": True, "scene_id": scene_id, "voice": audio_url, "audio_seconds": audio_seconds,
                "note": "已采用绑定的上传音频作为该镜旁白（未调用 TTS），时长保持设定值；该音频仍会随视频生成传入",
            }
            if audio_seconds and scene.duration and audio_seconds > scene.duration + 0.5:
                result["warning"] = (
                    f"音频实际 {audio_seconds:.1f}s 长于该镜设定 {scene.duration}s，成片拼接时会被截断——是否加长该镜由用户决定"
                )
            if trace:
                trace.log("tool_result", tool="generate_voice", ok=True, mode="use_uploaded")
            return result

    if not text:
        return {"ok": False, "error": f"分镜 {scene.title} 没有旁白文案，请先填写 narration"}
    if uploaded and not force and text_override and \
            material_match.norm_tts_text(text_override) != material_match.norm_tts_text(scene.narration or ""):
        return {"ok": False, "error": "该镜绑有用户上传音频，且传入文本与旁白不一致——禁止用自拟台词覆盖用户素材语境；采用上传音频请传 use_uploaded=true"}

    # 同文本历史配音沿用（重跑不重复烧钱）；force/换模型/换音色时强制重配
    if not force and not model and not voice:
        hit = find_completed_tts(text)
        if hit:
            audio_seconds = float(hit.get("duration_seconds") or 0)
            new_dur, note = calibrate_duration(audio_seconds, scene.duration, project.settings.video_model) if audio_seconds else (scene.duration, None)
            project_store.mutate(project_id, lambda p: _set_voice(p, scene_id, hit["output"], new_dur))
            return {"ok": True, "scene_id": scene_id, "voice": hit["output"],
                    "audio_seconds": audio_seconds, "scene_duration": new_dur,
                    "note": "旁白文本与历史配音一致，已直接沿用（未重新合成）", "followup": note}

    d_provider, d_model, d_voice = default_tts_setup()
    model = model or (project.settings.voice_model or d_model)
    if not model:
        return {"ok": False, "error": "未配置语音模型：请先在项目设置选择语音模型，或在设置页配置 TTS"}
    provider = tts_provider_for_model(model)
    voice = voice or (project.settings.voice_id if (project.settings.voice_id and provider == d_provider) else None) \
        or (d_voice if provider == d_provider else ("冰糖" if provider == "xiaomi" else "female-shaonv"))
    if trace:
        trace.log("tool_call", tool="generate_voice", scene_id=scene_id, model=model, voice=voice)
    job = job_manager.create("tts", provider, model, {
        "text": text[:800], "voice": voice, "scene_id": scene_id, "project_id": project_id,
    })
    project_store.mutate(project_id, lambda p: _mark(p, scene_id, voice_job=job["id"], status="generating", error=None))
    job_manager.attach(
        job["id"],
        voice_service.run_voice_job(
            job_id=job["id"], provider=provider, model=model,
            credential_id=None, text=text, voice=voice, speed=1.0, emotion="neutral",
        ),
    )
    waited = 0.0
    while waited < 180:
        j = job_manager.get(job["id"])
        if j["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(2)
        waited += 2
    j = job_manager.get(job["id"]) or {}
    if j.get("status") != "completed":
        def _fail(p: Project) -> None:
            s = _find_scene(p, scene_id)
            if s:
                s.status = "failed"
                s.error = f"配音失败：{j.get('error') or '超时（180s）'}"
                s.attempts["voice"] = s.attempts.get("voice", 0) + 1
        project_store.mutate(project_id, _fail)
        return {"ok": False, "error": f"旁白合成未完成：{j.get('error') or '超时（180s）'}", "job_id": job["id"]}
    audio_seconds = float(j.get("duration_seconds") or 0)
    new_dur, note = calibrate_duration(audio_seconds, scene.duration, project.settings.video_model) if audio_seconds else (scene.duration, None)
    project_store.mutate(project_id, lambda p: _set_voice(p, scene_id, j["output"], new_dur))
    return {"ok": True, "scene_id": scene_id, "voice": j["output"],
            "audio_seconds": audio_seconds, "scene_duration": new_dur, "followup": note}


def _set_voice(p: Project, scene_id: str, url: str, duration: int | None = None) -> None:
    s = _find_scene(p, scene_id)
    if s:
        s.assets.voice = url
        if duration is not None:
            s.duration = duration
        if s.status in ("generating", "failed"):
            s.status = "ready" if not s.assets.video else "done"
        s.error = None


def _mark(p: Project, scene_id: str, *, status: str | None = None, error: str | None = None,
          keyframe_job: str | None = None, voice_job: str | None = None, video_job: str | None = None,
          bump_attempt: str | None = None) -> None:
    s = _find_scene(p, scene_id)
    if not s:
        return
    if keyframe_job is not None:
        s.assets.keyframe_job = keyframe_job
    if voice_job is not None:
        s.assets.voice_job = voice_job
    if video_job is not None:
        s.assets.video_job = video_job
    if status is not None:
        s.status = status  # type: ignore[assignment]
    if error is not None or error == "":
        s.error = error or None
    if bump_attempt:
        s.attempts[bump_attempt] = s.attempts.get(bump_attempt, 0) + 1


# ---------------- 视频生成 ----------------


def submit_scene_video(project_id: str, scene_id: str, *, wanted_duration: int | None = None, trace=None) -> dict:
    """提交一镜视频生成（非阻塞）。Prompt 从项目分镜原样读取，用户内容不经过
    任何模型输出通道；唯一确定性改写是 <Audio N> 重编号。"""
    project_dict = project_store.get(project_id)
    if not project_dict:
        return {"ok": False, "error": "项目不存在"}
    project = Project.model_validate(project_dict)
    scene = _find_scene(project, scene_id)
    if not scene:
        return {"ok": False, "error": f"分镜不存在：{scene_id}"}
    if not scene.prompt.strip():
        return {"ok": False, "error": f"分镜「{scene.title}」没有 Prompt，无法生成视频"}
    if job_active(scene.assets.video_job):
        return {"ok": False, "error": "该分镜已有进行中的视频任务", "job_id": scene.assets.video_job}
    if scene.assets.video:
        return {"ok": False, "error": "该分镜已有视频，重新生成请先清除", "video": scene.assets.video}

    vmodel = resolve_video_model(project.settings.video_model)
    materials = {m.id: m for m in project.materials}
    req = assemble_video_request(scene, materials, vmodel)
    if req["contract_error"]:
        return {"ok": False, "error": f"当前视频模型「{vmodel['label']}」：{req['contract_error']}"}

    duration = clamp_duration(wanted_duration or scene.duration, project.settings.video_model)
    fingerprint = req["fingerprint"]
    job = job_manager.create("video", vmodel["provider"], vmodel["id"], {
        "mode": req["mode"], "prompt": req["prompt"][:300], "duration": duration,
        "aspect_ratio": project.aspect_ratio, "scene_id": scene_id, "project_id": project_id,
        "prompt_fingerprint": fingerprint,
    })
    project_store.mutate(
        project_id,
        lambda p: _mark(p, scene_id, video_job=job["id"], status="generating", error=None, bump_attempt="video"),
    )
    job_manager.attach(
        job["id"],
        video_service.run_video_job(
            job_id=job["id"], provider=vmodel["provider"], model=vmodel["id"],
            credential_id=None, mode=req["mode"], prompt=req["prompt"],
            duration=duration, aspect_ratio=project.aspect_ratio, resolution=project.settings.resolution or None,
            images=req["images"], last_images=[], videos=req["ref_videos"], audios=req["ref_audios"],
            options={},
        ),
    )
    if trace:
        trace.log("tool_call", tool="submit_video", scene_id=scene_id, job_id=job["id"],
                  model=vmodel["id"], mode=req["mode"], duration=duration)
    # 后端兜底写回：前端关页也不丢结果（指纹校验后落盘）
    asyncio.get_running_loop().create_task(_writeback_scene_video(project_id, scene_id, job["id"], fingerprint, trace))
    notes = "; ".join(req["notes"])
    note = "后台生成中" + (f"；{notes}" if notes else "")
    if wanted_duration and wanted_duration != duration:
        note += f"；该镜设定 {wanted_duration}s 超出模型范围，已按 {duration}s 提交"
    return {"ok": True, "job_id": job["id"], "scene_id": scene_id, "duration": duration, "note": note}


async def _writeback_scene_video(project_id: str, scene_id: str, job_id: str, fingerprint: str, trace=None) -> None:
    """轮询视频任务到终态并经 project_store 写回（Prompt 指纹校验）。"""
    waited = 0.0
    while waited < VIDEO_POLL_MAX:
        j = job_manager.get(job_id)
        if not j:
            return
        if j["status"] == "completed":
            break
        if j["status"] == "failed":
            project_store.mutate(project_id, lambda p: _mark(p, scene_id, status="failed", error=(j.get("error") or "视频生成失败")[:300]))
            if trace:
                trace.log("tool_result", tool="submit_video", ok=False, job_id=job_id, error=j.get("error"))
            return
        await asyncio.sleep(5)
        waited += 5
    else:
        project_store.mutate(project_id, lambda p: _mark(p, scene_id, status="failed", error="视频任务轮询超时"))
        return
    output = j.get("output")

    def _write(p: Project) -> None:
        s = _find_scene(p, scene_id)
        if not s:
            return
        current_fp = prompt_fingerprint(s.prompt)
        if fingerprint and current_fp != fingerprint:
            s.status = "planned"
            s.error = "视频生成期间 Prompt 已被修改，产物未写回（可在任务历史中下载旧视频）"
            return
        s.assets.video = output
        s.assets.video_job = job_id
        s.status = "done"
        s.error = None

    project_store.mutate(project_id, _write)
    if trace:
        trace.log("tool_result", tool="submit_video", ok=True, job_id=job_id, output=output)
