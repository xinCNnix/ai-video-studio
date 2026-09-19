"""Agent 技能系统：从内置 skills/ 与用户 data/skills/ 加载 SKILL.md 式知识文件。

技能是带 frontmatter（name/description）的 Markdown 知识文件，
Agent 写 Prompt 前可通过 load_skill 工具加载对应知识强化生成质量。
"""
import os
import re
from pathlib import Path

from ..config import settings

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "skills"
USER_DIR = settings.DATA_DIR / "skills"
USER_DIR.mkdir(parents=True, exist_ok=True)


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        meta, body = m.group(1), m.group(2).strip()
        name = re.search(r"name:\s*(.+)", meta)
        desc = re.search(r"description:\s*(.+)", meta)
        return {
            "id": path.stem,
            "name": (name.group(1).strip() if name else path.stem),
            "description": (desc.group(1).strip() if desc else ""),
            "body": body,
            "source": "user" if path.parent == USER_DIR else "builtin",
        }
    return {"id": path.stem, "name": path.stem, "description": "", "body": text.strip(), "source": "builtin"}


def list_skills() -> list[dict]:
    """列出全部技能（不含正文）。用户技能与内置技能同 id 时用户覆盖内置。"""
    seen: dict[str, dict] = {}
    for d in (BUILTIN_DIR, USER_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if "_menu" in f.name.lower():
                continue
            try:
                sk = _parse(f)
            except Exception:  # noqa: BLE001
                continue
            seen[sk["id"]] = sk
    return list(seen.values())


def get_skill(skill_id: str) -> dict | None:
    if not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id:
        return None
    for sk in list_skills():
        if sk["id"] == skill_id:
            return sk
    return None


def list_skill_files(skill_id: str) -> list[str]:
    """列出某技能自带的参考文件（skills/<id>/references/ 下的文件名）。"""
    if not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id:
        return []
    d = BUILTIN_DIR / skill_id / "references"
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir() if f.is_file())


def get_skill_file(skill_id: str, filename: str) -> str | None:
    """读取技能的参考文件（references/<filename>）。路径经过净化，只能读该目录内文件。"""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    d = BUILTIN_DIR / skill_id / "references"
    try:
        path = (d / filename).resolve()
        if not str(path).startswith(str(d.resolve()) + os.sep):
            return None
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError:  # noqa: BLE001
        return None
