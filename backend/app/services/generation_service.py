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
from ..harness.state import (
    MAX_SCENE_DURATION,
    Material,
    Project,
    Scene,
    clamp_duration,
    find_video_model,
)
from ..jobs import job_manager
from ..jobs.manager import ACTIVE_STATUSES
from . import file_service, image_service, material_match, prompt_timing, video_service, voice_service
from . import project_store

VIDEO_POLL_MAX = 25 * 60  # 写回侧轮询上限（生成侧 20min + 下载余量）
VIDEO_POLL_INTERVAL = 5

# 「任务已完成、产物尚未写回」的在途登记（project_id, scene_id, job_id）。
# 占位拦截靠它区分「写回中」（拦截重提交）与「写回协程已死」（放行重生成）；
# 进程重启后为空——恰好对应写回协程确实已死的事实，方向永不判反。
_pending_video_writebacks: set[tuple[str, str, str]] = set()


def video_writeback_pending(project_id: str, scene_id: str, job_id: str | None) -> bool:
    """该镜视频是否处于「任务已终态、产物写回中/在途」状态。

    提交侧与调谐侧共用：这一窗口内该镜不是缺口，也绝不允许再提交（重复计费）。"""
    return bool(job_id) and (project_id, scene_id, job_id) in _pending_video_writebacks


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


def resolve_scene_video_model(project: Project, scene: Scene) -> tuple[dict, str | None]:
    """按分镜解析视频模型：首尾帧镜在项目模型不支持该模式时自动回退。

    返回 (模型, 回退说明)。回退规则：同厂商优先，其次目录内任一支持首尾帧的
    模型（目录顺序确定，行为可预期）；找不到回退模型时原样返回项目模型，
    由 assemble_video_request 的契约闸门给出明确报错。其余模式不做回退——
    参考生视频等行为与项目设置严格一致，避免悄悄换模型换计费。
    """
    base = resolve_video_model(project.settings.video_model)
    if scene.gen_mode != "first_last" or "first_last" in (base.get("modes") or []):
        return base, None
    alt = next(
        (m for m in settings.VIDEO_MODELS
         if m.get("provider") == base.get("provider") and "first_last" in (m.get("modes") or [])),
        None,
    ) or next(
        (m for m in settings.VIDEO_MODELS if "first_last" in (m.get("modes") or [])),
        None,
    )
    if alt and alt["id"] != base["id"]:
        return alt, (f"该镜为「首尾帧」模式，项目模型「{base['label']}」不支持，"
                     f"本镜已自动改用「{alt['label']}」生成（其余分镜不受影响）")
    return base, None


def _has_xiaomi_key() -> bool:
    return any(
        k.get("provider") == "xiaomi" or "xiaomimimo" in (k.get("base_url") or "").lower()
        for k in list_keys()
    )


def tts_provider_for_model(model: str) -> str:
    """按模型 ID 判定 TTS 协议厂商：higgs 走本地，mimo-* 走小米，其余走 MiniMax/通用协议。"""
    m = model.strip().lower()
    if "higgs" in m:
        return "higgs"
    return "xiaomi" if m.startswith("mimo-") else "minimax"


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
        note = f"该镜时长已从 {old}s 校准为 {new_dur}s（旁白实际 {audio_seconds:.1f}s）——Prompt 时间轴已同步缩放为 0-{new_dur}s"
    model = find_video_model(model_id)
    if model and model.get("durations") and new_dur >= max(model["durations"]):
        note = ((note + "；") if note else "") + (
            f"旁白 {audio_seconds:.1f}s 接近/超过模型时长上限 {max(model['durations'])}s，"
            "建议拆成两镜或精简台词"
        )
    return new_dur, note


AUDIO_FIT_TOL = 0.5  # 台词总时长与镜头时长的容忍差（秒）


def fit_duration_to_audios(total_seconds: float, old: int, model_id: str | None) -> tuple[int | None, str | None, str | None]:
    """按绑定台词音频总时长校准分镜时长（bind 落盘/视频提交双闸门的唯一实现）。

    视频模型放不下台词时会把台词压缩变形后烧进音轨（复现事故：7.96s 台词配 7s 镜头，
    第二句语速/停顿全变）。装得下→自动加长并说明；模型上限仍装不下→返回阻断错误，
    绝不按原时长提交。返回 (新时长, 校准说明, 阻断错误)。
    """
    if total_seconds <= old + AUDIO_FIT_TOL:
        return old, None, None
    new_dur = clamp_duration(int(total_seconds + 0.999), model_id)
    if total_seconds > new_dur + AUDIO_FIT_TOL:
        model = find_video_model(model_id)
        top = max(model["durations"]) if model and model.get("durations") else MAX_SCENE_DURATION
        return None, None, (
            f"该镜绑定台词音频共 {total_seconds:.1f}s，超过视频模型最长 {top}s——"
            "请拆成两镜或精简台词后再生成"
        )
    note = (
        f"该镜时长已从 {old}s 校准为 {new_dur}s（绑定台词音频共 {total_seconds:.1f}s，"
        "剧本看不到配音精确时长由系统兜底）——Prompt 时间轴（含原文块时长引用）"
        f"已同步缩放为 0-{new_dur}s"
    )
    return new_dur, note, None


# ---------------- 素材解析与请求组装 ----------------


def scene_material_urls(scene: Scene, materials: dict[str, Material]) -> dict:
    """把绑定关系解析成 URL 清单（关键帧=生成首帧或 first_frame 绑定素材，
    尾帧=last_frame 绑定素材——仅首尾帧模式使用）。"""
    ref_images: list[str] = []
    first_frame: str | None = scene.assets.keyframe
    last_frame: str | None = None
    for b in scene.binding.images:
        m = materials.get(b.material_id)
        if not m or not m.url:
            continue
        if b.role == "first_frame":
            first_frame = m.url
        elif b.role == "last_frame":
            last_frame = m.url
        else:
            ref_images.append(m.url)
    return {
        "first_frame": first_frame,
        "last_frame": last_frame,
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
    - 参考图/视频按绑定顺序原样传入（bind 已把 Prompt 声明行重写成绑定顺序＝
     上传顺序对齐；此前按文件名语义重排，"小墨"撞进"吕满春+小墨双人"声明行，
     小墨图与双人图互换槽位——角色参考张冠李戴）
    - AutoDL 工作流契约提交前拦截
    """
    urls = scene_material_urls(scene, materials)
    ref_images = urls["ref_images"]
    ref_videos = urls["ref_videos"]
    all_audios = urls["ref_audios"]
    unique_audios, dupe_audios = material_match.dedupe_same_text_audios(all_audios)
    notes: list[str] = []
    if scene.gen_mode != "first_last" and urls.get("last_frame"):
        notes.append("1 张「尾帧」绑定在当前生成模式下不生效（仅首尾帧模式使用）")
    if dupe_audios:
        notes.append(f"{len(dupe_audios)} 段同台词重复音频未传入（同台词只传一段，多传会把同一句烧两遍）")
    ref_audios = material_match.sort_audios_by_prompt(unique_audios, scene.prompt, scene.narration)
    if len(ref_audios) > 3:
        ref_audios, dropped = material_match.pick_matching_audios(unique_audios, scene.prompt, scene.narration, limit=3)
        notes.append(f"该镜挂有 {len(all_audios)} 段音频超出上限 3 段，已按旁白文案自动挑选 {len(ref_audios)} 段传入")
    # TTS 配音镜：旁白音频不在绑定里而在 assets.voice（reconcile 先配音后视频）。H3 契约
    # 要求台词必须引用真实传入的音频——配音作为 <Audio 1> 传入，否则悬空标签被闸门拦截，
    # 模型也会凭 Audio Timeline 的台词引号自行编造语音。已绑用户音频时以绑定的为准
    # （导出侧 assets.voice 整轨替换音轨，不会与模型烧录的同一句叠放）
    if scene.assets.voice and not ref_audios and scene.gen_mode != "first_last":
        ref_audios = [scene.assets.voice]

    # 台词时长防线（纯函数）：已知时长的绑定音频总时长超过镜头时长即拦截。
    # ensure_scene_audio_fit 已探测+校准，这里兜探测失败/时长缺失仍可知的漏网
    dur_by_url = {m.url: m.duration for m in materials.values() if m.url}
    known_durations = [dur_by_url[u] for u in ref_audios if dur_by_url.get(u)]
    audio_total = sum(known_durations)
    audio_fit_error = ""
    if audio_total > scene.duration + AUDIO_FIT_TOL:
        audio_fit_error = (
            f"该镜绑定台词音频共 {audio_total:.1f}s，超过镜头时长 {scene.duration}s——"
            "视频模型放不下会把台词压缩变形，请加长该镜时长或拆成两镜"
        )

    keyframe = urls["first_frame"]
    last_frame = urls.get("last_frame")
    has_refs = bool(ref_images or ref_videos or ref_audios)
    last_images: list[str] = []
    if scene.gen_mode == "first_last":
        # 首尾帧镜：首帧=关键帧/first_frame 绑定，尾帧=last_frame 绑定；
        # 不传任何参考素材（H3 官方首尾帧与参考互斥，AutoDL 首尾帧工作流也无参考入参）。
        # 配音不随请求传入（FLF 模型自带音频生成），导出时 assets.voice 整轨替换
        if not keyframe:
            return {"contract_error": "首尾帧镜缺少首帧：请先生成关键帧，或绑定一张角色为「首帧」的图"}
        if not last_frame:
            return {"contract_error": "首尾帧镜缺少尾帧：请绑定一张角色为「尾帧」的图（素材池选图时设为尾帧）"}
        if ref_images or ref_videos:
            return {"contract_error": "首尾帧镜不能绑定参考图/视频（首尾帧与参考素材互斥）：请解绑参考素材或改回图生模式"}
        if ref_audios:
            # 台词时长闸门已按绑定音频校准过；这里只说明不随请求传入的事实
            notes.append(f"{len(ref_audios)} 段绑定音频未随首尾帧请求传入（首尾帧与参考素材互斥，模型自行生成音效；配音导出时整轨替换）")
            ref_audios = []
        images = [keyframe]
        last_images = [last_frame]
        mode = "first_last"
    elif scene.gen_mode == "i2v":
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
    if audio_tags and mode == "first_last":
        tag_error = (
            f"Prompt 引用了 {'/'.join(f'<Audio {t}>' for t in audio_tags)} 标签，但首尾帧模式不随请求传参考音频"
            "（模型自行生成音效，配音在导出时整轨替换）：请去掉音频标签或改回参考模式"
        )
    elif audio_tags and not ref_audios:
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
        if mode == "first_last":
            for label in ("Picture", "Video"):
                nums = [int(n) for n in re.findall(rf"<{label}\s+(\d+)>", prompt)]
                if nums:
                    tag_error = (
                        f"Prompt 引用了 <{label} {max(nums)}> 标签，但首尾帧模式不传编号参考素材"
                        "（首尾帧以画面直接约束）：请去掉该标签或改回参考模式"
                    )
                    break
        else:
            for label, passed in (("Picture", images), ("Video", ref_videos)):
                nums = [int(n) for n in re.findall(rf"<{label}\s+(\d+)>", prompt)]
                if nums and max(nums) > len(passed):
                    tag_error = (
                        f"Prompt 引用了 <{label} {max(nums)}> 标签，但本镜只传入 {len(passed)} 个参考"
                        f"{'图' if label == 'Picture' else '视频'}：悬空标签会让模型找不到指定素材"
                    )
                    break
    contract_error = tag_error or audio_fit_error or video_service.autodl_contract_error(
        vmodel["id"], mode, images, ref_videos, ref_audios, prompt=prompt,
        last_images=last_images,
    )
    return {
        "mode": mode, "images": images, "last_images": last_images,
        "ref_videos": ref_videos, "ref_audios": ref_audios,
        "prompt": prompt, "contract_error": contract_error, "notes": notes,
        "fingerprint": prompt_fingerprint(scene.prompt),
    }


# ---------------- 写回辅助 ----------------


def writeback_completed_video(project_id: str, scene_id: str, output: str, fingerprint: str = "", job_id: str | None = None) -> str | None:
    """把已完成的视频产物写回 v2 分镜（提交写回与重启恢复共用）。

    钱已经花掉，产物必须落回分镜——判定（接管/指纹）与写回在同一把项目锁内
    原子完成。指纹失配时释放 video_job 槽位（否则该镜永久卡在「已完成未写回」，
    人工重生成也会被占位拦截）。返回 None = 成功；字符串 = 未写回原因。
    """
    try:
        if not project_store.get(project_id):
            return f"项目 {project_id} 不存在，无法写回"
        result: list[str | None] = [None]

        def fn(p: Project) -> None:
            s = _find_scene(p, scene_id)
            if not s:
                result[0] = f"分镜 {scene_id} 已不存在，无法写回"
                return
            if job_id and s.assets.video_job and s.assets.video_job != job_id:
                result[0] = f"分镜 {scene_id} 已由新任务接管（当前 job {s.assets.video_job}），产物未写回"
                return
            if fingerprint and prompt_fingerprint(s.prompt) != fingerprint:
                reason = (f"分镜 {scene_id} 的 Prompt 在生成期间已被改写"
                          f"（提交指纹 {fingerprint}，当前 {prompt_fingerprint(s.prompt)}），"
                          "产物未写回，可在任务历史中下载")
                s.assets.video_job = None
                s.status = "planned"
                s.error = reason
                result[0] = reason
                return
            s.assets.video = output
            if job_id:
                s.assets.video_job = job_id
            s.status = "done"
            s.error = None

        project_store.mutate(project_id, fn)
        return result[0]
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
            if audio_seconds and scene.duration and audio_seconds > scene.duration + AUDIO_FIT_TOL:
                result["warning"] = (
                    f"音频实际 {audio_seconds:.1f}s 长于该镜设定 {scene.duration}s；视频生成前会按该镜"
                    "绑定台词总时长自动校准镜头时长"
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
        or (d_voice if provider == d_provider else ("default" if provider == "higgs" else ("冰糖" if provider == "xiaomi" else "female-shaonv")))
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
        if duration is not None and duration != s.duration:
            if s.prompt:  # 配音实测时长校准镜长：Prompt 时间引用同步缩放，不留自相矛盾指令
                s.prompt, _ = prompt_timing.rescale_prompt_timing(s.prompt, s.duration, duration)
                s.revision += 1
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


async def ensure_scene_audio_fit(project: Project, scene: Scene, project_id: str) -> tuple[str | None, str | None]:
    """视频提交前的台词时长兜底：绑定音频缺时长的先探测，总时长超镜头则自动校准。

    返回 (校准说明, 阻断错误)。探测失败（时长未知）的音频不计入总和——
    assemble_video_request 里还有一道按已知时长的确定性防线。
    """
    materials = {m.id: m for m in project.materials}
    bound = [materials[mid] for mid in scene.binding.audios if mid in materials]
    if not bound:
        return None, None
    to_probe = [m for m in bound if m.duration is None and m.url]
    if to_probe:
        probed: dict[str, float] = {}
        for m in to_probe:
            sec = await voice_service.probe_audio_seconds(m.url)
            if sec:
                probed[m.id] = sec
        if probed:
            def _set_dur(p: Project) -> None:
                mats = {x.id: x for x in p.materials}
                for mid, sec in probed.items():
                    if mid in mats:
                        mats[mid].duration = sec
            project_store.mutate(project_id, _set_dur)
            for m in bound:  # 本地副本同步，供下面求和
                if m.id in probed:
                    m.duration = probed[m.id]
    total = sum(m.duration for m in bound if m.duration)
    if not total:
        return None, None
    new_dur, note, error = fit_duration_to_audios(total, scene.duration, project.settings.video_model)
    if error:
        return None, error
    if note:
        if scene.prompt:  # 校准必须同步 Prompt 时间引用（含原文块），否则残留旧时长与 duration 参数自相矛盾
            scene.prompt, _ = prompt_timing.rescale_prompt_timing(scene.prompt, scene.duration, new_dur)
            scene.revision += 1
        scene.duration = new_dur

        def _apply_fit(p: Project) -> None:
            s = _find_scene(p, scene.id)
            if s:
                s.duration = new_dur
                s.prompt = scene.prompt
                s.revision += 1

        project_store.mutate(project_id, _apply_fit)
    return note, None


async def submit_scene_video(project_id: str, scene_id: str, *, wanted_duration: int | None = None, trace=None, force: bool = False, manual: bool = False) -> dict:
    """提交一镜视频生成（非阻塞）。Prompt 从项目分镜原样读取，用户内容不经过
    任何模型输出通道；唯一确定性改写是 <Audio N> 重编号。

    计费安全——检查与占位在同一次 project_store.mutate（项目互斥锁）内完成，
    通过后才 attach 服务商调用；任何被拦截的提交不产生服务商请求、零计费：
    - 进行中任务 / 已完成但产物写回中 / 已有视频（需 force）→ 拦截
    - 自动链路（manual=False，pipeline/调谐）对同一镜只许提交一次，
      之后的重生成必须人工触发（按钮/agent），attempts>=1 即拦截
    - 提交期间 Prompt 被并发修改（指纹漂移）→ 拦截：按旧 Prompt 生成的产物
      会被写回侧丢弃，等于白烧钱
    """
    project_dict = project_store.get(project_id)
    if not project_dict:
        return {"ok": False, "error": "项目不存在"}
    project = Project.model_validate(project_dict)
    scene = _find_scene(project, scene_id)
    if not scene:
        return {"ok": False, "error": f"分镜不存在：{scene_id}"}

    # 台词时长闸门：绑定音频总时长放不下时模型会把台词压缩变形，先校准/拦截
    fit_note, fit_error = await ensure_scene_audio_fit(project, scene, project_id)
    if fit_error:
        if trace:
            trace.log("tool_result", tool="submit_video", ok=False, scene_id=scene_id, error=fit_error)
        return {"ok": False, "error": fit_error}

    vmodel, model_note = resolve_scene_video_model(project, scene)
    materials = {m.id: m for m in project.materials}

    # 显式时长优先于分镜时长，但不允许低于台词总时长（闸门刚把 scene.duration 校准到位）
    duration = clamp_duration(wanted_duration or scene.duration, project.settings.video_model)
    audio_total = sum(
        m.duration for m in (materials[x] for x in scene.binding.audios if x in materials) if m.duration
    )
    if audio_total and duration + AUDIO_FIT_TOL < audio_total:
        duration = clamp_duration(int(audio_total + 0.999), project.settings.video_model)
    if duration != scene.duration and scene.prompt:
        # 显式改时长与 patch_scene 同一契约：Prompt 时间引用缩放到实际提交时长
        scene.prompt, _ = prompt_timing.rescale_prompt_timing(scene.prompt, scene.duration, duration)
        scene.revision += 1

        def _apply_dur(p: Project) -> None:
            s = _find_scene(p, scene_id)
            if s:
                s.prompt = scene.prompt
                s.duration = duration
                s.revision += 1

        project_store.mutate(project_id, _apply_dur)
        scene.duration = duration

    req = assemble_video_request(scene, materials, vmodel)
    if req["contract_error"]:
        return {"ok": False, "error": f"当前视频模型「{vmodel['label']}」：{req['contract_error']}"}

    fingerprint = req["fingerprint"]
    # seed 建任务前一次性决定：同值写入 job 元数据与 options 下发，保证
    # 「记录的 seed = 实际使用的 seed」（适配器随机化仅兜底未走本函数的路径）
    seed = video_service.resolve_seed(vmodel["provider"], vmodel["id"], None)
    job = job_manager.create("video", vmodel["provider"], vmodel["id"], {
        "mode": req["mode"], "prompt": req["prompt"][:300], "duration": duration,
        "aspect_ratio": project.aspect_ratio, "scene_id": scene_id, "project_id": project_id,
        "prompt_fingerprint": fingerprint,
        **({"seed": seed} if seed is not None else {}),
    })

    # 原子占位：全部拦截判定与 video_job 指派在同一把项目锁内完成，并发提交
    # 只有一个能赢。未通过则任务从未 attach——服务商零调用零计费，删记录即可。
    verdict: dict = {}

    def _claim(p: Project) -> None:
        s = _find_scene(p, scene_id)
        if not s:
            verdict["error"] = f"分镜不存在：{scene_id}"
            return
        if not s.prompt.strip():
            verdict["error"] = f"分镜「{s.title}」没有 Prompt，无法生成视频"
            return
        if fingerprint and prompt_fingerprint(s.prompt) != fingerprint:
            verdict["error"] = "提交期间 Prompt 已被修改，已拦截（旧 Prompt 的产物写回会被丢弃，白烧钱）；请重新提交"
            return
        if not manual and s.attempts.get("video", 0) >= 1:
            verdict["error"] = "自动重提交已收回：该镜已有提交记录，重生成需人工触发"
            return
        prev_job = s.assets.video_job
        if prev_job and prev_job != job["id"]:
            pj = job_manager.get(prev_job)
            if pj and pj.get("status") in ACTIVE_STATUSES:
                verdict["error"] = "该分镜已有进行中的视频任务"
                verdict["job_id"] = prev_job
                return
            if pj and pj.get("status") == "completed" and not s.assets.video \
                    and (project_id, scene_id, prev_job) in _pending_video_writebacks:
                verdict["error"] = "上一任务已完成、产物写回中，已拦截重复提交（避免重复计费）"
                verdict["job_id"] = prev_job
                return
        if s.assets.video and not force:
            verdict["error"] = "该分镜已有视频，重新生成需 force 覆盖"
            verdict["video"] = s.assets.video
            return
        _mark(p, scene_id, video_job=job["id"], status="generating", error=None, bump_attempt="video")
        if force and s.assets.video:
            s.assets.video = None  # force 重生成：占位成功这一刻才清旧视频
        verdict["ok"] = True

    try:
        project_store.mutate(project_id, _claim)
    except Exception as exc:  # noqa: BLE001
        job_manager.delete(job["id"])
        return {"ok": False, "error": f"提交占位失败（未调用服务商）：{str(exc)[:200]}"}
    if not verdict.get("ok"):
        job_manager.delete(job["id"])
        if trace:
            trace.log("tool_result", tool="submit_video", ok=False, scene_id=scene_id, error=verdict.get("error"))
        return {"ok": False, "error": verdict.get("error"),
                **{k: v for k, v in verdict.items() if k not in ("error", "ok")}}

    if force and trace:
        trace.log("tool_result", tool="submit_video", scene_id=scene_id, force=True, cleared_video=True)
    job_manager.attach(
        job["id"],
        video_service.run_video_job(
            job_id=job["id"], provider=vmodel["provider"], model=vmodel["id"],
            credential_id=None, mode=req["mode"], prompt=req["prompt"],
            duration=duration, aspect_ratio=project.aspect_ratio, resolution=project.settings.resolution or None,
            images=req["images"], last_images=req.get("last_images") or [], videos=req["ref_videos"], audios=req["ref_audios"],
            options={"seed": seed} if seed is not None else {},
        ),
    )
    if trace:
        trace.log("tool_call", tool="submit_video", scene_id=scene_id, job_id=job["id"],
                  model=vmodel["id"], mode=req["mode"], duration=duration)
    # 后端兜底写回：前端关页也不丢结果（指纹校验后落盘）。
    # 在途登记必须先于任务挂起同步完成，占位拦截从此刻起即可见。
    _pending_video_writebacks.add((project_id, scene_id, job["id"]))
    asyncio.get_running_loop().create_task(_writeback_scene_video(project_id, scene_id, job["id"], fingerprint, trace))
    notes = "; ".join(req["notes"])
    note = "后台生成中" + (f"；{notes}" if notes else "")
    if model_note:
        note += f"；{model_note}"
    if fit_note:
        note += f"；{fit_note}"
    if wanted_duration and wanted_duration != duration:
        note += f"；该镜设定 {wanted_duration}s 受模型范围/台词时长限制，已按 {duration}s 提交"
    return {"ok": True, "job_id": job["id"], "scene_id": scene_id, "duration": duration, "note": note}


async def _writeback_scene_video(project_id: str, scene_id: str, job_id: str, fingerprint: str, trace=None) -> None:
    """轮询视频任务到终态并写回（Prompt 指纹校验）。

    钱已花，产物必须落回分镜：成功走 writeback_completed_video（接管/指纹判定
    在锁内原子完成）；失败把分镜标 failed（保留任务历史可人工重试）。finally
    清除在途登记——每个出口都把分镜带到明确终态，不留给调谐循环重新判缺口的窗口。
    """
    key = (project_id, scene_id, job_id)
    _pending_video_writebacks.add(key)

    def _mark_failed_if_current(p: Project, error: str) -> None:
        s = _find_scene(p, scene_id)
        if s and (not s.assets.video_job or s.assets.video_job == job_id):
            s.status = "failed"
            s.error = error[:300]

    try:
        waited = 0.0
        while waited < VIDEO_POLL_MAX:
            j = job_manager.get(job_id)
            if not j:
                return
            if j["status"] == "completed":
                note = writeback_completed_video(
                    project_id, scene_id, j.get("output") or "", fingerprint, job_id=job_id,
                )
                if trace:
                    trace.log("tool_result", tool="submit_video", ok=not note, job_id=job_id,
                              output=None if note else j.get("output"), error=note)
                return
            if j["status"] == "failed":
                err = j.get("error") or "视频生成失败"
                project_store.mutate(project_id, lambda p: _mark_failed_if_current(p, err))
                if trace:
                    trace.log("tool_result", tool="submit_video", ok=False, job_id=job_id, error=err)
                return
            await asyncio.sleep(VIDEO_POLL_INTERVAL)
            waited += VIDEO_POLL_INTERVAL
        else:
            project_store.mutate(project_id, lambda p: _mark_failed_if_current(p, "视频任务轮询超时"))
            return
    finally:
        _pending_video_writebacks.discard(key)
