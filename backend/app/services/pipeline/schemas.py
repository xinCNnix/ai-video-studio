"""pipeline 结构化输出模型：normalize / plan / bind 三个 LLM 阶段的输出契约。

Pydantic 校验失败 → llm_client 把错误清单回喂修复；修不好 → Fatal 中止。
这是"坏输出绝不带病进入下一阶段"的骨架边界。
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ScriptType = Literal["story", "screenplay", "storyboard", "ad", "materials_only"]


class ScreenplayCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(max_length=40)
    design: str = ""


class ScreenplayBeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    description: str = ""
    narration: str = ""
    source_hint: str = ""

    @field_validator("title")
    @classmethod
    def _title_default(cls, v: str) -> str:
        return (v or "").strip()


class Screenplay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    script_type: ScriptType = "story"
    logline: str = ""
    characters: list[ScreenplayCharacter] = Field(default_factory=list)
    beats: list[ScreenplayBeat] = Field(min_length=1)
    notes: str = ""


class MaterialWant(BaseModel):
    """分镜对素材的声明：material_id 直接引用（plan 阶段看得见素材清单），
    或 description 语义描述（交给 bind 阶段做确定性/LLM 匹配）。"""

    model_config = ConfigDict(extra="ignore")

    material_id: str | None = None
    description: str = ""
    role: Literal["reference", "first_frame"] = "reference"


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(max_length=50)
    description: str = Field(default="", max_length=500)
    prompt: str = ""
    keyframe_prompt: str = ""
    narration: str = ""
    duration: int = Field(default=5, ge=1, le=120)
    gen_mode: Literal["i2v", "t2v"] = "i2v"
    wants_images: list[MaterialWant] = Field(default_factory=list)
    wants_audios: list[MaterialWant] = Field(default_factory=list)
    wants_videos: list[MaterialWant] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_thin(cls, v: str) -> str:
        if len((v or "").strip()) < 40:
            raise ValueError(f"分镜 Prompt 过短（{len((v or '').strip())} 字 < 40）：需要完整的结构化视频 Prompt（画面主体/动作时间轴/镜头语言/音频设计）")
        return v


class PlanBatch(BaseModel):
    """plan 阶段分批输出：每批 ≤ batch_size 镜，done=全部输出完毕。"""

    model_config = ConfigDict(extra="ignore")

    shots: list[ShotPlan] = Field(min_length=1)
    total: int = Field(ge=1, le=200)
    done: bool = False


class BindDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    material_id: str
    scene_index: int = Field(ge=0)
    kind: Literal["image", "audio", "video"]
    role: Literal["reference", "first_frame"] = "reference"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v: float) -> float:
        return round(float(v), 2)


class BindDecisionList(BaseModel):
    """bind 阶段 LLM 兜底的输出契约。"""

    model_config = ConfigDict(extra="ignore")

    decisions: list[BindDecision] = Field(default_factory=list)
