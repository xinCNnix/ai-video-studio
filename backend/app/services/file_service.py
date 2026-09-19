"""文件服务：上传保存、生成结果落盘、引用素材物化、文件 URL。"""
import base64
import mimetypes
import re
import uuid
from pathlib import Path

import aiofiles
import httpx

from ..config import settings

EXT_BY_KIND = {"image": ".png", "audio": ".mp3", "video": ".mp4", "doc": ".md"}


def _sanitize_upload_stem(filename: str) -> str:
    """保留上传文件名里有意义的字符（用户常用文件名前缀标记素材用途/归属），
    去掉路径分隔与 Windows 保留字符，限长防溢出。"""
    stem = Path(filename).stem
    stem = re.sub(r'[\\/:*?"<>|\r\n\t#%&{}$!\'@+`=?~^\[\]]+', "", stem).strip(". _-")
    return (stem[:60].strip() or "upload")


async def save_upload(kind: str, filename: str, content: bytes) -> dict:
    ext = Path(filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".mp4", ".mov", ".webm", ".md", ".txt"):
        ext = EXT_BY_KIND.get(kind, ".bin")
    # uuid 前缀防同名冲突，后接原始文件名摘要：Agent/用户看 URL 即知素材内容
    name = f"{uuid.uuid4().hex[:8]}_{_sanitize_upload_stem(filename)}{ext}"
    dir_path = settings.UPLOADS_DIR / kind
    dir_path.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(dir_path / name, "wb") as fh:
        await fh.write(content)
    return {"url": f"/api/files/uploads/{kind}/{name}", "name": name, "size": len(content)}


async def save_output(kind: str, content: bytes, ext: str | None = None) -> str:
    name = f"{uuid.uuid4().hex[:12]}{ext or EXT_BY_KIND.get(kind, '.bin')}"
    dir_path = settings.OUTPUTS_DIR / kind
    dir_path.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(dir_path / name, "wb") as fh:
        await fh.write(content)
    return f"/api/files/outputs/{kind}/{name}"


def list_recent_uploads(per_kind: int = 8) -> dict:
    """素材池：最近上传的文件清单（按修改时间倒序）。

    Agent 挂载用户上传的参考图/配音用——用户上传后没有在 Studio 手动挂到分镜时，
    助手从这里看到素材并用 attach_scene_refs 挂载，而不是"素材给了却用不上"。
    """
    out: dict[str, list[dict]] = {}
    for kind in ("image", "audio", "video", "doc"):
        d = settings.UPLOADS_DIR / kind
        if not d.exists():
            continue
        items = []
        for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:per_kind]:
            if p.is_file():
                items.append({"name": p.name, "url": f"/api/files/uploads/{kind}/{p.name}"})
        if items:
            out[kind] = items
    return out


async def download_to_outputs(kind: str, url: str, ext: str | None = None) -> str:
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return await save_output(kind, resp.content, ext)


def resolve_local(url: str) -> Path | None:
    """/api/files/{bucket}/{kind}/{name} -> data/ 本地路径。"""
    prefix = "/api/files/"
    if not url.startswith(prefix):
        return None
    parts = url[len(prefix):].split("/")
    if len(parts) != 3 or parts[0] not in ("uploads", "outputs"):
        return None
    bucket, kind, name = parts
    if "/" in name or ".." in name or "\\" in name:
        return None
    base = settings.UPLOADS_DIR if bucket == "uploads" else settings.OUTPUTS_DIR
    path = base / kind / name
    return path if path.exists() else None


async def materialize(reference: str) -> Path:
    """把引用（本地文件 URL / data URL / http URL）物化为本地文件。"""
    local = resolve_local(reference)
    if local:
        return local
    if reference.startswith("data:"):
        _, _, payload = reference.partition(",")
        data = base64.b64decode(payload)
        path = settings.TEMP_DIR / f"{uuid.uuid4().hex[:12]}.bin"
        path.write_bytes(data)
        return path
    if reference.startswith("http"):
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(reference)
            resp.raise_for_status()
            path = settings.TEMP_DIR / f"{uuid.uuid4().hex[:12]}.bin"
            path.write_bytes(resp.content)
            return path
    p = Path(reference)
    if p.exists():
        return p
    raise FileNotFoundError(f"找不到引用素材：{reference[:120]}")


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def output_counts() -> dict:
    counts = {}
    for kind in ("image", "audio", "video"):
        d = settings.OUTPUTS_DIR / kind
        counts[kind] = len(list(d.glob("*"))) if d.exists() else 0
    return counts


def cleanup_uploads(days: int = 7, keep_paths: "set | None" = None) -> int:
    """删除 data/uploads 中 N 天前的素材，返回删除数量。

    keep_paths：仍被项目分镜引用的文件（参考图/配音/参考视频等用户素材），
    被引用的文件无论多旧都不删——清掉它们会让分镜引用直接 404。
    """
    import time

    keep = keep_paths or set()
    cutoff = time.time() - days * 86400
    deleted = 0
    for kind in ("image", "audio", "video"):
        d = settings.UPLOADS_DIR / kind
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file() or f in keep:
                continue
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                except OSError:
                    continue
    return deleted
