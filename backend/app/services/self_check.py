"""生成结果基础自检：文件完整性、格式可识别、时长合理性、图片尺寸、Prompt 分节覆盖。

不做内容级审核（需要模型参与，非 v1.0 范围），只做可确定性校验；
结果随任务写入 job.self_check，前端可展示徽标。
"""
import struct


def _pack(checks: list[tuple[str, bool, str]]) -> dict:
    return {
        "ok": all(c[1] for c in checks),
        "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in checks],
    }


def _sniff_image(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None


def check_image(content: bytes, requested_size: str | None = None) -> dict:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("非空", len(content) > 0, f"{len(content)} 字节"))
    fmt = _sniff_image(content)
    checks.append(("格式可识别", fmt is not None, fmt or "未知格式"))
    checks.append(("体积合理", len(content) >= 1024, f"{len(content)} 字节"))
    if fmt == "png" and requested_size and "x" in requested_size:
        try:
            w, h = struct.unpack(">II", content[16:24])
            rw, rh = (int(x) for x in requested_size.split("x", 1))
            ok = (w, h) == (rw, rh)
            checks.append(("尺寸匹配", ok, f"实际 {w}x{h}，请求 {rw}x{rh}"))
        except Exception:  # noqa: BLE001
            pass
    return _pack(checks)


def estimate_audio_seconds(content: bytes, bitrate_kbps: int = 128) -> float:
    """按码率估算音频时长（秒）。粗略值，用于时长校准与下限校验。"""
    return len(content) * 8 / (bitrate_kbps * 1000)


def check_audio(content: bytes) -> dict:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("非空", len(content) > 0, f"{len(content)} 字节"))
    is_mp3 = content[:3] == b"ID3" or (len(content) > 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0)
    checks.append(("MP3 可识别", is_mp3, "ID3/帧头" if is_mp3 else "未知格式"))
    est_seconds = estimate_audio_seconds(content)
    checks.append(("时长合理", est_seconds >= 1.0, f"预估 ≥{est_seconds:.1f}s（按 128kbps 估算）"))
    return _pack(checks)


def check_video(content: bytes, requested_duration: int | None = None) -> dict:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("非空", len(content) > 0, f"{len(content)} 字节"))
    is_mp4 = len(content) > 12 and content[4:8] == b"ftyp"
    checks.append(("MP4 容器", is_mp4, "ftyp box" if is_mp4 else "未找到 ftyp"))
    duration = None
    if is_mp4:
        # mvhd box：idx 指向 'mvhd'：[mvhd:4][version+flags:4][creation:4][modification:4][timescale:4][duration:4]
        idx = content.find(b"mvhd")
        if idx > 0 and len(content) >= idx + 24:
            version = content[idx + 4]
            if version == 0:
                timescale = struct.unpack(">I", content[idx + 16 : idx + 20])[0]
                dur = struct.unpack(">I", content[idx + 20 : idx + 24])[0]
                if timescale > 0:
                    duration = dur / timescale
    checks.append(("时长可解析", duration is not None, f"{duration:.1f}s" if duration else "未找到 mvhd"))
    if duration is not None:
        checks.append(("时长合理", 0.5 <= duration <= 180, f"{duration:.1f}s"))
        if requested_duration:
            ok = abs(duration - requested_duration) <= 3.5
            checks.append(("与请求时长一致", ok, f"实际 {duration:.1f}s，请求 {requested_duration}s（容差 3.5s）"))
    checks.append(("体积合理", len(content) >= 20480, f"{len(content)} 字节"))
    return _pack(checks)


PROMPT_REQUIRED_SECTIONS = ["Duration", "Scene", "Subject", "Action Timeline", "Camera", "Constraints"]

# H3 官方 skill 的两种合法字段结构（基础模式三字段 / 全参考模式六分节）
H3_BASE_FIELDS = ["integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"]
H3_REF_FIELDS = ["subject_definitions:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]


def check_prompt_text(text: str) -> dict:
    """H3 官方格式自检：原文必须包含基础模式三字段，或全参考模式的关键分节。"""
    low = text.lower()
    if all(f in low for f in H3_BASE_FIELDS):
        checks = [("H3 三核心字段", True, "integrated_multimodal_description / overall_soundscape / non_diegetic_music")]
    elif all(f in low for f in H3_REF_FIELDS):
        checks = [("H3 全参考分节", True, "subject_definitions + detailed_description 结构")]
    else:
        missing = [f.rstrip(":") for f in H3_BASE_FIELDS if f not in low]
        checks = [("H3 三核心字段", False, "缺失：" + "、".join(missing))]
    return _pack(checks)


def check_prompt_sections(sections: list[dict]) -> dict:
    titles = [s.get("title", "").strip().lower() for s in sections]
    checks: list[tuple[str, bool, str]] = []
    for req in PROMPT_REQUIRED_SECTIONS:
        ok = any(req.lower() in t for t in titles)
        checks.append((f"包含 {req}", ok, "有" if ok else "缺失"))
    return _pack(checks)
