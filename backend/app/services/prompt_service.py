"""Prompt Agent 服务：LLM 生成 / 精修 / 改写视频 Prompt。"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..config.key_manager import KeyNotFound
from ..providers.base import ProviderError
from ..providers.registry import resolve_llm_call
from ..prompt import h3_prompt, generic_prompt
from . import self_check, skill_service


def parse_sections(text: str) -> list[dict]:
    """把 '## Title' 分节文本解析为 [{title, content}]。"""
    sections: list[dict] = []
    title: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if m and not line.startswith("#!"):
            if title is not None:
                content = "\n".join(lines).strip()
                if content:
                    sections.append({"title": title, "content": content})
            title = m.group(1).strip("—-").strip()
            lines = []
        elif title is not None:
            lines.append(line)
    if title is not None:
        content = "\n".join(lines).strip()
        if content:
            sections.append({"title": title, "content": content})
    if not sections and text.strip():
        sections = [{"title": "Prompt", "content": text.strip()}]
    return sections


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _system_prompt(target_model: str) -> str:
    base = generic_prompt.GENERIC_SYSTEM_PROMPT if target_model == "generic" else h3_prompt.H3_SYSTEM_PROMPT
    # H3 模式注入官方 skill「h3-prompt-writing」的完整写作指南原文
    if target_model != "generic":
        skills_dir = Path(__file__).resolve().parents[1] / "skills"
        for name, title in (("h3-base-en.txt", "基础模式指南（T2VA / I2VA / FL2VA / L2VA）"),
                            ("h3-ref-en.txt", "全参考模式指南（Ref2VA）")):
            try:
                guide = (skills_dir / name).read_text(encoding="utf-8").strip()
                if guide:
                    base += f"\n\n# 官方写作指南：{title}\n{guide}"
            except OSError:  # noqa: BLE001
                pass
    return base


def _refine_instruction(target_model: str, action: str) -> str:
    if target_model == "generic":
        return generic_prompt.refine_instruction(action)
    return h3_prompt.refine_instruction(action)


async def generate_prompt(
    description: str,
    mode: str = "text_to_video",
    target_model: str = "minimax_h3",
    credential_id: str | None = None,
    model: str | None = None,
    duration: int | None = None,
    provider: str | None = None,
) -> dict:
    llm, model = resolve_llm_call(credential_id, provider_hint=provider, model=model)
    mode_note = h3_prompt.MODE_INSTRUCTIONS.get(mode, "")
    duration_note = f"\n目标时长：约 {duration} 秒。" if duration else ""
    messages = [
        {"role": "system", "content": _system_prompt(target_model)},
        {
            "role": "user",
            "content": f"用户描述：{description}\n{mode_note}{duration_note}\n请生成结构化视频 Prompt。",
        },
    ]
    raw = _strip_fences(
        await llm.generate(messages, model, temperature=0.7, max_tokens=8000)
    )
    sections = parse_sections(raw)
    # 基础自检：H3 校验官方三字段/六分节，通用模型校验分节标题；缺失时自动重试一次
    check = self_check.check_prompt_text(raw) if target_model != "generic" else self_check.check_prompt_sections(sections)
    if not check["ok"]:
        retry_note = "上一版缺少必要字段（" + "、".join(
            c["name"] for c in check["checks"] if not c["pass"]
        ) + "）。请严格按系统提示中官方指南的字段名与顺序重新输出。"
        raw2 = _strip_fences(
            await llm.generate(
                messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": retry_note},
                ],
                model,
                temperature=0.5,
                max_tokens=8000,
            )
        )
        sections2 = parse_sections(raw2)
        check2 = self_check.check_prompt_text(raw2) if target_model != "generic" else self_check.check_prompt_sections(sections2)
        if check2["ok"] or len(sections2) > len(sections):
            raw, sections, check = raw2, sections2, check2
    return {"prompt": raw, "sections": sections, "self_check": check}


async def refine_prompt(
    action: str,
    current_prompt: str,
    description: str = "",
    target_model: str = "minimax_h3",
    credential_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict:
    llm, model = resolve_llm_call(credential_id, provider_hint=provider, model=model)
    instruction = _refine_instruction(target_model, action)
    user_parts = []
    if description:
        user_parts.append(f"原始需求：{description}")
    user_parts.append(f"当前 Prompt：\n{current_prompt}")
    user_parts.append(f"修改要求：{instruction}\n请输出修订后的完整 Prompt，保持相同分节格式。")
    messages = [
        {"role": "system", "content": _system_prompt(target_model)},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    raw = _strip_fences(
        await llm.generate(messages, model, temperature=0.7, max_tokens=8000)
    )
    return {"prompt": raw, "sections": parse_sections(raw)}


async def improve_prompt(
    prompt: str,
    target_model: str = "minimax_h3",
    credential_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    """Video 页面内的 Improve Prompt：直接返回改写后的完整 Prompt。"""
    llm, model = resolve_llm_call(credential_id, provider_hint=provider, model=model)
    if target_model == "generic":
        system = "你是视频 Prompt 专家。把用户给的视频生成 Prompt 改写为更适合通用视频模型的版本：保留原意，补充动作时间线、镜头与光线描述，直接输出改写后的完整 Prompt，不要解释。"
    else:
        system = "你是 MiniMax H3 视频模型 Prompt 专家。把用户给的视频生成 Prompt 改写为更适合 H3 模型的版本：保留原意，补充主体、动作时间线、镜头运动、光线与限制条件，直接输出改写后的完整 Prompt，不要解释。"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    return _strip_fences(await llm.generate(messages, model, temperature=0.7, max_tokens=8000))


async def improve_image_prompt(
    prompt: str,
    image_model: str = "",
    credential_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    """Image 页面内的 AI 润色：按生图技能知识改写为结构化生图提示词。"""
    llm, model = resolve_llm_call(credential_id, provider_hint=provider, model=model)
    system = (
        "你是文生图 Prompt 专家。按下面的写作知识把用户的描述改写为一条可直接使用的生图提示词："
        "覆盖主体、构图景别、视觉风格与光线、画面文字要求（无文字则注明不出现文字）、负面约束；"
        "只输出最终提示词本身，不要解释、不要分节标题。\n\n"
    )
    try:
        sk = skill_service.get_skill("image-prompt-writing")
        if sk:
            system += "# 写作知识（Skill: image-prompt-writing）\n" + sk["body"]
    except Exception:  # noqa: BLE001
        pass
    model_note = f"\n目标生图模型：{image_model}（按该模型的提示词偏好调整写法与长度）。" if image_model else ""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt + model_note},
    ]
    return _strip_fences(await llm.generate(messages, model, temperature=0.7, max_tokens=2000))


# ---------------- Prompt 预设 ----------------
def save_preset(title: str, content: str) -> dict:
    preset = {
        "id": f"prompt_{uuid.uuid4().hex[:8]}",
        "title": title or "未命名 Prompt",
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (settings.PRESETS_DIR / f"{preset['id']}.json").write_text(
        json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return preset


def list_presets() -> list[dict]:
    presets = []
    for f in settings.PRESETS_DIR.glob("prompt_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            presets.append({"id": data["id"], "title": data["title"], "created_at": data.get("created_at")})
        except Exception:
            continue
    presets.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return presets


def get_preset(preset_id: str) -> dict | None:
    path = settings.PRESETS_DIR / f"{preset_id}.json"
    if not path.exists() or ".." in preset_id or "/" in preset_id:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
