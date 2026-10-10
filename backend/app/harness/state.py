"""项目 v2 状态模型 + 写入口不变量：非法状态不可表示。

所有写路径（API 命令、pipeline 阶段、生成写回、agent 工具）都必须经过
Project 模型与 check_invariants——素材引用不存在、超契约数量、时长越界等
非法状态在落盘前被拒绝，而不是等到提交视频 API 时才炸。
"""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import settings

MaterialKind = Literal["image", "audio", "video", "doc", "script"]
ImageRole = Literal["reference", "first_frame", "last_frame"]
# first_last：按镜覆盖的首尾帧模式——首帧走关键帧/first_frame 绑定，尾帧走
# last_frame 绑定；该镜不传参考图/音频（H3 首尾帧与参考素材互斥）
GenMode = Literal["i2v", "t2v", "first_last"]
SceneStatus = Literal["planned", "ready", "generating", "done", "failed"]
PipelineStage = Literal["empty", "intake", "normalize", "plan", "bind", "review", "generate", "assemble"]
PipelineState = Literal["idle", "running", "paused", "failed", "done"]

# 契约上界（AutoDL ref_image_0..8 / ref_audio_0..2 为最紧约束，取全模型交集）
MAX_IMAGES_PER_SCENE = 9
MAX_AUDIOS_PER_SCENE = 3
MAX_VIDEOS_PER_SCENE = 3
MAX_SCENE_DURATION = 60
ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}


def new_scene_id() -> str:
    return f"scene_{uuid.uuid4().hex[:8]}"


def new_material_id() -> str:
    return f"mat_{uuid.uuid4().hex[:8]}"


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_model: str | None = None
    image_model: str | None = None
    voice_model: str | None = None
    voice_id: str | None = None
    resolution: str = "auto"
    # review 门禁旁路：True 时 bind 完成直接进 generate，不等用户确认
    auto_generate: bool = False
    # 润色总开关：False = 不改稿。H3 成稿拍（原文已是定稿 H3 Prompt）plan 阶段零 LLM
    # 逐字直录（时段/镜头结构/风格/摄影原样进模型，仅做 Reference→wants、Audio
    # Timeline 物化等机械适配）；其余稿件跳过 director 润色遍、直接采用草稿 Prompt。
    # storyboard 成稿或润色反复破坏原文锁定构图时建议关闭（2026-09-22 第4集镜3/4/8
    # 润色发明机位三连事故；2026-10-01 单镜成稿被重拆 [Shot 1]-[Shot 5]/时段重排）
    prompt_polish: bool = True


class Material(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: MaterialKind
    # 文件型素材的站点路径（/api/files/...）；script 类型可为空
    url: str | None = None
    filename: str = ""
    # doc/script 提取的正文（素材对号的依据之一）
    text: str | None = None
    # audio 探测时长（秒）
    duration: float | None = None
    added_at: str = ""


class ImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: str
    role: ImageRole = "reference"


class SceneBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[ImageBinding] = Field(default_factory=list)
    audios: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)


class SceneAssets(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keyframe: str | None = None
    voice: str | None = None
    video: str | None = None
    keyframe_job: str | None = None
    voice_job: str | None = None
    video_job: str | None = None


class Character(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    design: str = ""


class Scene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    description: str = ""
    prompt: str = ""
    keyframe_prompt: str = ""
    narration: str = ""
    duration: int = 5
    gen_mode: GenMode = "i2v"
    binding: SceneBinding = Field(default_factory=SceneBinding)
    assets: SceneAssets = Field(default_factory=SceneAssets)
    status: SceneStatus = "planned"
    error: str | None = None
    revision: int = 0
    # 调谐器自动重试计数（kind → 次数）：keyframe/voice/video
    attempts: dict[str, int] = Field(default_factory=dict)


class PendingBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    material_id: str
    note: str = ""
    suggested_scene_id: str | None = None


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stage: PipelineStage = "empty"
    state: PipelineState = "idle"
    run_id: str | None = None
    # 暂停原因标记："review"（门禁确认）/"bindings"（歧义素材）/"resume"（中断续跑）…
    awaiting: str | None = None
    error: str | None = None
    history: list[dict] = Field(default_factory=list)


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = 2
    id: str
    name: str = "未命名项目"
    aspect_ratio: str = "16:9"
    # 乐观锁版本号：每次成功落盘 +1；外部写必须带基线版本
    version: int = 0
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    materials: list[Material] = Field(default_factory=list)
    characters: list[Character] = Field(default_factory=list)
    # normalize 阶段产物（自由结构，由 pipeline/schemas 严格校验）
    screenplay: dict | None = None
    scenes: list[Scene] = Field(default_factory=list)
    pipeline: Pipeline = Field(default_factory=Pipeline)
    pending_bindings: list[PendingBinding] = Field(default_factory=list)
    final_video: str | None = None
    created_at: str = ""
    updated_at: str = ""


def check_invariants(p: Project) -> list[str]:
    """写入口不变量检查：返回违规清单（空 = 通过）。"""
    violations: list[str] = []
    if p.aspect_ratio not in ASPECT_RATIOS:
        violations.append(f"画幅 {p.aspect_ratio} 不在支持列表 {sorted(ASPECT_RATIOS)}")
    ids = [s.id for s in p.scenes]
    if len(ids) != len(set(ids)):
        violations.append("分镜 id 重复")
    material_ids = {m.id for m in p.materials}
    for i, s in enumerate(p.scenes):
        label = f"分镜{i}({s.title[:20] or s.id})"
        if not s.title.strip():
            violations.append(f"{label}: 标题为空")
        if not 1 <= s.duration <= MAX_SCENE_DURATION:
            violations.append(f"{label}: 时长 {s.duration}s 越界（1-{MAX_SCENE_DURATION}）")
        b = s.binding
        if len(b.images) > MAX_IMAGES_PER_SCENE:
            violations.append(f"{label}: 参考图 {len(b.images)} 张超过契约上限 {MAX_IMAGES_PER_SCENE}")
        if len(b.audios) > MAX_AUDIOS_PER_SCENE:
            violations.append(f"{label}: 参考音频 {len(b.audios)} 段超过契约上限 {MAX_AUDIOS_PER_SCENE}")
        if len(b.videos) > MAX_VIDEOS_PER_SCENE:
            violations.append(f"{label}: 参考视频 {len(b.videos)} 个超过上限 {MAX_VIDEOS_PER_SCENE}")
        img_ids = [x.material_id for x in b.images]
        if len(img_ids) != len(set(img_ids)):
            violations.append(f"{label}: 参考图列表存在重复素材")
        if len(b.audios) != len(set(b.audios)):
            violations.append(f"{label}: 参考音频列表存在重复素材")
        if len(b.videos) != len(set(b.videos)):
            violations.append(f"{label}: 参考视频列表存在重复素材")
        if sum(1 for x in b.images if x.role == "first_frame") > 1:
            violations.append(f"{label}: 首帧角色绑定多于一个")
        if sum(1 for x in b.images if x.role == "last_frame") > 1:
            violations.append(f"{label}: 尾帧角色绑定多于一个")
        missing = [mid for mid in [*img_ids, *b.audios, *b.videos] if mid not in material_ids]
        if missing:
            violations.append(f"{label}: 绑定了不存在的素材 {missing}")
    for pb in p.pending_bindings:
        if pb.material_id not in material_ids:
            violations.append(f"待确认素材 {pb.material_id} 不在素材池")
    return violations


# ---------------- 模型目录与时长钳制 ----------------
# 注：Prompt 内 <Picture/Video/Audio N> 悬空标签的提交级校验唯一实现位于
# generation_service.assemble_video_request（对着组装后实际传入的素材列表检查），
# 不要在此再复制一份。


def find_video_model(model_id: str | None) -> dict | None:
    """从目录解析视频模型描述（durations/modes 等契约）。"""
    if not model_id:
        return None
    return next((m for m in settings.VIDEO_MODELS if m.get("id") == model_id), None)


def clamp_duration(duration: int, model_id: str | None = None) -> int:
    """时长唯一钳制点：模型档位存在时钳到模型支持范围，否则通用上界。"""
    duration = max(1, int(duration))
    model = find_video_model(model_id)
    if model and model.get("durations"):
        allowed = model["durations"]
        duration = max(min(duration, max(allowed)), min(allowed))
    return min(duration, MAX_SCENE_DURATION)
