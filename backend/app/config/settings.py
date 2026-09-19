"""全局路径与模型目录配置。data/ 统一保存在项目根目录。"""
import os
from pathlib import Path

# backend/app/config/settings.py -> parents[3] 为项目根目录
APP_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", APP_ROOT))
DATA_DIR = Path(os.environ.get("DATA_DIR") or (PROJECT_ROOT / "data"))

CONFIG_DIR = DATA_DIR / "config"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
JOBS_DIR = DATA_DIR / "jobs"
PROJECTS_DIR = DATA_DIR / "projects"
PRESETS_DIR = DATA_DIR / "presets"
TEMP_DIR = DATA_DIR / "temp"

for _d in (CONFIG_DIR, JOBS_DIR, PROJECTS_DIR, PRESETS_DIR, TEMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _kind in ("image", "audio", "video"):
    (UPLOADS_DIR / _kind).mkdir(parents=True, exist_ok=True)
    (OUTPUTS_DIR / _kind).mkdir(parents=True, exist_ok=True)

APP_NAME = "AI Video Studio"
APP_VERSION = "1.0.0"

# ---------------- Provider 目录 ----------------
# capabilities: llm / tts / image / video
PROVIDER_CATALOG: dict[str, dict] = {
    "minimax": {
        "label": "MiniMax",
        "capabilities": ["llm", "tts", "video", "image"],
        "default_base_url": "https://api.minimax.cn",
        "fields": ["group_id"],
    },
    "openai": {
        "label": "OpenAI",
        "capabilities": ["llm", "image", "tts"],
        "default_base_url": "https://api.openai.com/v1",
        "fields": [],
    },
    "openai_compatible": {
        "label": "OpenAI Compatible",
        "capabilities": ["llm", "tts"],
        "default_base_url": "",
        "fields": [],
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "capabilities": ["llm", "tts"],
        "default_base_url": "https://api.xiaomimimo.com/v1",
        "fields": [],
    },
    "ollama": {
        "label": "Ollama (本地)",
        "capabilities": ["llm"],
        "default_base_url": "http://localhost:11434/v1",
        "fields": [],
    },
    "generic_video": {
        "label": "Generic Video API",
        "capabilities": ["video"],
        "default_base_url": "",
        "fields": [],
    },
    "seedance": {
        "label": "Seedance (火山方舟)",
        "capabilities": ["video"],
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "fields": [],
    },
    "wan": {
        "label": "Wan (阿里云百炼)",
        "capabilities": ["video", "image"],
        "default_base_url": "https://dashscope.aliyuncs.com/api/v1",
        "fields": [],
    },
    "bfl": {
        "label": "FLUX (BFL)",
        "capabilities": ["image"],
        "default_base_url": "https://api.bfl.ai/v1",
        "fields": [],
    },
    "google": {
        "label": "Google Gemini",
        "capabilities": ["image"],
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "fields": [],
    },
    "autodl": {
        "label": "AutoDL ComfyUI (云端)",
        "capabilities": ["video"],
        "default_base_url": "https://autodl.art/api/v1",
        "fields": [],
    },
    "comfyui": {
        "label": "ComfyUI (本地)",
        "capabilities": ["image", "video", "tts"],
        "default_base_url": "http://127.0.0.1:8188",
        "fields": [],
    },
}

# ---------------- 模型目录 ----------------
# 说明：只保留厂商旗舰作为未配置凭据时的兜底。
# LLM / 生图模型优先从凭据的 /models 接口自动发现，或在设置中添加自定义模型 ID。
LLM_MODELS: list[dict] = [
    {"id": "MiniMax-M2", "label": "MiniMax M2", "provider": "minimax"},
]

# TTS 模型：小米 MiMo 系列接口协议私有（chat/completions 形态），模型 ID 固定，
# 预制为兜底；其他厂商可在「设置 → Models」添加自定义模型 ID，语音页也支持直接输入。
TTS_MODELS: list[dict] = [
    {"id": "mimo-v2.5-tts", "label": "MiMo TTS 预置音色", "provider": "xiaomi"},
    {"id": "mimo-v2.5-tts-voicedesign", "label": "MiMo TTS 音色设计（描述即音色）", "provider": "xiaomi"},
    {"id": "mimo-v2.5-tts-voiceclone", "label": "MiMo TTS 声音克隆（上传样本即音色）", "provider": "xiaomi"},
]

# 音色不预制：优先从凭据的 get_voice 接口发现（系统音色 + 克隆音色），
# 也可在设置中添加自定义音色 ID。
TTS_VOICES: list[dict] = []

TTS_EMOTIONS: list[str] = ["neutral", "happy", "sad", "angry", "fearful", "surprised", "calm"]

IMAGE_MODELS: list[dict] = [
    {"id": "gpt-image-2", "label": "GPT Image 2", "provider": "openai"},
    {"id": "doubao-seedream-5-0-260128", "label": "Seedream 5.0", "provider": "seedance"},
    {"id": "doubao-seedream-4-5-251128", "label": "Seedream 4.5", "provider": "seedance"},
    {"id": "gemini-3-pro-image-preview", "label": "Nano Banana Pro (Gemini 3)", "provider": "google"},
    {"id": "flux-2-pro", "label": "FLUX.2 Pro", "provider": "bfl"},
    {"id": "qwen-image", "label": "Qwen-Image", "provider": "wan"},
    {"id": "image-01", "label": "MiniMax Image-01", "provider": "minimax"},
    {"id": "comfyui-image", "label": "ComfyUI 本地工作流", "provider": "comfyui"},
]

VIDEO_MODELS: list[dict] = [
    # ---- AutoDL ComfyUI 云端工作流：ID = 工作流 ID，共用一个 autodl 凭据 ----
    # 注意：autodl 条目不在这里写 modes——modes 与参考素材契约统一由
    # AUTODL_WORKFLOW_SPECS 注入（见下），保证单一事实来源。
    {
        "id": "minimax_h3_zm_u24",
        "label": "H3 多图多音频生视频(升级画质)",
        "provider": "autodl",
        "durations": list(range(4, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "480p", "768p"],
    },
    {
        # ✅ AutoDL 官方名称：H3多图生视频15秒（LightX2V 版，非文生视频）
        "id": "minimax_h3_lightx2v_v5_15s",
        "label": "H3 多图生视频 15s",
        "provider": "autodl",
        "durations": list(range(4, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "480p", "768p"],
    },
    {
        # ✅ AutoDL 官方名称：H3多图多音频生视频15秒
        "id": "minimax_h3_image_audio_to_video_v2_15s",
        "label": "H3 多图多音频生视频 15s",
        "provider": "autodl",
        "durations": list(range(4, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "480p", "768p"],
    },
    {
        # ✅ AutoDL 官方名称：H3多图生视频12秒；该工作流分辨率仅支持 736p
        "id": "minimax_h3_b99_003_12s",
        "label": "H3 多图生视频 12s",
        "provider": "autodl",
        "durations": list(range(4, 13)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "736p"],
    },
    {
        # ✅ AutoDL 官方名称：H3文生视频（纯 t2v：仅 prompt/duration/resolution，无任何参考素材入参）
        "id": "minimax_h3_lightx2v_no_pic",
        "label": "H3 文生视频",
        "provider": "autodl",
        "durations": list(range(4, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "480p", "768p"],
    },
    {
        "id": "MiniMax-H3",
        "label": "MiniMax H3",
        "provider": "minimax",
        "durations": list(range(4, 16)),  # 官方 4-15 秒
        # 官方 ratio 枚举（t2v 必填；i2v 固定 adaptive，由适配器处理）
        "aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        # 官方 resolution 枚举为 480P/768P/2K；H3 支持 768P/2K，auto=适配器回退 768P
        "resolutions": ["auto", "768p", "2k"],
        "modes": ["text", "image", "reference"],
    },
    {
        "id": "generic",
        "label": "Generic Video",
        "provider": "generic_video",
        "durations": list(range(1, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto"],
        "modes": ["text", "image"],
    },
    {
        # Seedance 2.5：t2v/i2v/参考/首尾帧统一入口，单段最长 30s
        "id": "doubao-seedance-2-5-260628",
        "label": "Seedance 2.5",
        "provider": "seedance",
        "durations": list(range(4, 31)),
        "aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "resolutions": ["auto", "480p", "720p", "1080p"],
        "modes": ["text", "image", "reference", "first_last"],
    },
    {
        # 万相 3.0：All-in-One，文生/图生/首尾帧统一，2-30s，画幅自适应
        "id": "wan3.0-video",
        "label": "Wan 3.0 Video",
        "provider": "wan",
        "durations": list(range(2, 31)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto", "480p", "720p", "1080p"],
        "modes": ["text", "image", "reference", "first_last"],
    },
    {
        "id": "comfyui-video",
        "label": "ComfyUI 本地工作流",
        "provider": "comfyui",
        "durations": list(range(1, 16)),
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["auto"],
        "modes": ["text", "image", "first_last"],
    },
]

# ---------------- AutoDL ComfyUI 工作流入参契约 ----------------
# 单一事实来源：每个工作流支持的模式、参考素材的最低/最高要求与可接受类型。
# 后端提交前校验（video_service.autodl_contract_error）、适配器字段过滤
# （providers/video/autodl_comfyui.py）、前端素材区显隐与本地校验全部以此为准；
# VIDEO_MODELS 中 autodl 条目的 modes / min_images / accepts_* 等也由下方循环注入。
#
# 字段说明：
#   modes           允许的生成模式（text=纯文生 / image=图生 / reference=参考素材）
#   min_images      必须携带的最少参考图数量（ref_image_0 是否 required）
#   max_images      参考图上限（AutoDL 定义 ref_image_0..8，最多 9 张）
#   accepts_audios  是否接受参考音频（ref_audio_0..2，最多 3 段）
#   accepts_videos  是否接受参考视频（当前工作流均不支持，避免"上传了却没生效"）
#   requires_prompt 工作流是否强制要求 Prompt（四个 H3 工作流均为必填）
#   extra_fields    options 中允许透传给 AutoDL 的字段名（AutoDL 严格校验，
#                  传未定义字段会报"存在未定义的参数"；prompt_optimizer 等厂商选项一律不透传）
#
# 数据来源：✅ 2026-09-10 与 AutoDL 官方接口逐一核对
# （GET https://autodl.art/api/v1/comfyui/workflows/{id} 的 input_rules）。
AUTODL_WORKFLOW_SPECS: dict[str, dict] = {
    "minimax_h3_zm_u24": {
        # ✅ H3多图多音频生视频(升级画质)：ref_image_0 必填，9图+3音频，1-15s
        "modes": ["image", "reference"],
        "min_images": 1,
        "max_images": 9,
        "accepts_audios": True,
        "max_audios": 3,
        "accepts_videos": False,
        "requires_prompt": True,
        "extra_fields": ["seed"],
    },
    "minimax_h3_lightx2v_v5_15s": {
        # ✅ H3多图生视频15秒（注意：不是文生视频）：ref_image_0 必填，9图，无音频，1-15s
        "modes": ["image", "reference"],
        "min_images": 1,
        "max_images": 9,
        "accepts_audios": False,
        "accepts_videos": False,
        "requires_prompt": True,
        "extra_fields": ["seed"],
    },
    "minimax_h3_image_audio_to_video_v2_15s": {
        # ✅ H3多图多音频生视频15秒：全部素材选填（ref_image_0 有空白图默认值），
        #    9图+3音频，1-15s；Prompt 必填
        "modes": ["text", "image", "reference"],
        "min_images": 0,
        "max_images": 9,
        "accepts_audios": True,
        "max_audios": 3,
        "accepts_videos": False,
        "requires_prompt": True,
        "extra_fields": ["seed"],
    },
    "minimax_h3_b99_003_12s": {
        # ✅ H3多图生视频12秒：ref_image_0 必填，9图，无音频，1-12s，分辨率仅 736p
        "modes": ["image", "reference"],
        "min_images": 1,
        "max_images": 9,
        "accepts_audios": False,
        "accepts_videos": False,
        "requires_prompt": True,
        "extra_fields": ["seed"],
    },
    "minimax_h3_lightx2v_no_pic": {
        # ✅ H3文生视频：仅 prompt 必填；时长 1-15 / 分辨率 480p·768p 横竖+1:1 可选；
        #    无图/音/视频入参，也无 seed
        "modes": ["text"],
        "min_images": 0,
        "max_images": 0,
        "accepts_audios": False,
        "accepts_videos": False,
        "requires_prompt": True,
        "extra_fields": [],
    },
}

# 把契约注入 VIDEO_MODELS 的 autodl 条目（modes / min_images / accepts_* 随目录下发前端）
for _m in VIDEO_MODELS:
    if _m.get("provider") == "autodl" and _m["id"] in AUTODL_WORKFLOW_SPECS:
        _spec = AUTODL_WORKFLOW_SPECS[_m["id"]]
        _m["modes"] = list(_spec["modes"])
        _m.update(
            {k: v for k, v in _spec.items() if k not in ("modes", "extra_fields")}
        )

PROMPT_MODES: list[str] = ["text_to_video", "image_to_video", "reference_video", "first_last_frame"]

REFINE_ACTIONS: list[str] = ["shorter", "detailed", "motion", "camera", "timing", "regenerate"]
