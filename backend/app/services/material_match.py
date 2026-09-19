"""素材↔分镜确定性匹配纯函数（自 agent_service 移植，v2 全链路单一实现）。

这些函数承载历史事故的修复经验（同台词重复烧录、<Audio N> 全局编号错位、
参考图与声明行张冠李戴），generation_service（提交对齐）与 pipeline bind
阶段（素材对号）共用，严禁再复制第二份。
"""
import re

_AUDIO_TAG_RE = re.compile(r"<Audio\s+(\d+)>")


def norm_tts_text(t: str) -> str:
    """配音文本身份归一化：去空白与标点，只比内容。"""
    return re.sub(r"[\s，。？！,.?!…—·、：:；;\"'（）()\[\]{}]+", "", t or "")


def audio_semantic(url_or_name: str) -> str:
    """从上传音频文件名提取语义文本（用户上传名自带台词前缀，如 xxxx_tts_085821_哎呀！原来…）。"""
    name = (url_or_name or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "_" in name:
        name = name.rsplit("_", 1)[-1]
    return "".join(re.findall(r"[\u4e00-\ufaffA-Za-z0-9]+", name))


def dedupe_same_text_audios(audios: list[str]) -> tuple[list[str], list[str]]:
    """同台词多份拷贝只留第一个（用户整包重传是常态：9 句 × 6 拷贝 = 54 文件）。

    多份同台词音频传给工作流会被当成多段台词逐段烧进成片——同一句说两遍的直接
    来源之一。文件名提不出台词文本的按 URL 身份去重，不误伤。
    """
    seen: set[str] = set()
    kept, dropped = [], []
    for u in audios:
        key = norm_tts_text(audio_semantic(u)) or u
        if key in seen:
            dropped.append(u)
        else:
            kept.append(u)
            seen.add(key)
    return kept, dropped


def sort_audios_by_prompt(audios: list[str], prompt: str, narration: str) -> list[str]:
    """按台词文本在分镜 Prompt 里首次出现的位置排序（稳定排序，未命中排最后）。

    Prompt 的 <Audio N> / Audio Timeline 按台词时间顺序编号，ref_audio_N 却按传入
    顺序平铺——错位即张冠李戴（复现事故：分镜3 把小墨的音频烧进医生 0-7s 的时间槽）。
    """
    hay = norm_tts_text(str(prompt or "") + str(narration or ""))

    def _key(item: tuple[int, str]) -> tuple[int, int, int]:
        i, u = item
        t = norm_tts_text(audio_semantic(u))
        pos = hay.find(t) if (t and len(t) >= 4 and hay) else -1
        return (1, 0, i) if pos < 0 else (0, pos, i)

    return [u for _, u in sorted(enumerate(audios), key=_key)]


def asset_name_tokens(url: str) -> list[str]:
    """从参考素材文件名提取身份 token：丢弃哈希/uuid 片段，保留 CJK 串（≥2 字）与
    latin 单词（≥2 字，小写）。`b4d61513_9c92ff34-….png` → []；`58b5421a_小墨.png`
    → ["小墨"]；`3950be11_doctor_threeview.png` → ["doctor", "threeview"]。"""
    name = (url or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    toks: list[str] = []
    for part in re.split(r"[_\-\s]+", name):
        if re.fullmatch(r"[0-9a-f]{6,}", part.lower()):
            continue
        toks += re.findall(r"[\u4e00-\ufaff]{2,}|[a-z]{2,}", part.lower())
    return toks


def sort_ref_assets_by_prompt(urls: list[str], prompt: str, label: str) -> list[str]:
    """参考图/参考视频按 Prompt 的 <Picture N> / <Video N> 声明顺序对齐（稳定排序）。

    身份绑定写在声明行里——错位即张冠李戴。同一编号的声明行可能多处出现，按 N
    合并后匹配：文件名身份 token 出现在哪个编号的声明文本里就排第 N 位；文件名无
    语义时若素材数与声明编号数一致，用消元法把无信号素材按原相对顺序补进空缺编号。
    两类信号都没有的保持原序排在末尾。
    """
    decl: dict[int, str] = {}
    for n, line in re.findall(rf"<{label}\s+(\d+)>([^<\n]*)", prompt or ""):
        k = int(n)
        decl[k] = f"{decl.get(k, '')} {line}"
    if not decl or not urls:
        return list(urls)
    norm = {n: norm_tts_text(t).lower() for n, t in decl.items()}
    rank: dict[str, int | None] = {}
    for u in urls:
        toks = [t for t in asset_name_tokens(u) if len(t) >= 2]
        rank[u] = min(
            (n for n, nl in norm.items() if any(t in nl for t in toks)),
            default=None,
        )
    bound = [u for u in urls if rank[u] is not None]
    unbound = [u for u in urls if rank[u] is None]
    if len(urls) == len(decl) and len(unbound) == len(decl) - len(set(rank[u] for u in bound)):
        free_ns = sorted(set(decl) - {rank[u] for u in bound})
        for u, n in zip(unbound, free_ns):
            rank[u] = n
        bound, unbound = urls, []
    order = {u: i for i, u in enumerate(urls)}
    return sorted(bound, key=lambda u: (rank[u], order[u])) + unbound


def pick_matching_audios(audios: list[str], prompt: str, narration: str, limit: int = 3) -> tuple[list[str], list[str]]:
    """从多段上传音频里挑与该镜旁白文案匹配的（≤limit 段）。返回 (选中, 落选)。

    全部不匹配时退回前 limit 段——绝不静默丢弃，调用方需转告用户。
    """
    hay = norm_tts_text(str(prompt or "") + str(narration or ""))
    matched, rest = [], []
    for u in audios:
        t = norm_tts_text(audio_semantic(u))
        (matched if (t and len(t) >= 4 and t in hay) else rest).append(u)
    unique_matched, _ = dedupe_same_text_audios(matched)
    pool = unique_matched or dedupe_same_text_audios(audios)[0]
    picked = sort_audios_by_prompt(pool, prompt, narration)[:limit]
    dropped = [u for u in audios if u not in picked]
    return picked, dropped


def distribute_audios(
    audios: list[str], scene_texts: list[tuple[str, str]]
) -> tuple[dict[int, list[str]], list[str], list[int]]:
    """把整条片的配音段按台词对号分到各镜（pipeline bind 阶段的确定性核心）。

    scene_texts: 每镜 (prompt, narration)。台词文本出现在哪镜就分给哪镜，每镜上限
    3 段（工作流硬限）。返回 (镜序号→音频列表, 未命中音频, 超限被截掉的镜序号)。
    未命中的绝不瞎塞——返回给上层进 pending_bindings 由用户定夺。
    """
    claims: dict[int, list[str]] = {}
    seen_texts: dict[int, set[str]] = {}
    unmatched: list[str] = []
    overflow: set[int] = set()
    for u in audios:
        t = norm_tts_text(audio_semantic(u))
        if not t or len(t) < 4:
            unmatched.append(u)
            continue
        hay_list = [norm_tts_text(str(p or "") + str(n or "")) for p, n in scene_texts]
        hit = next((i for i, hay in enumerate(hay_list) if t in hay), None)
        if hit is None:
            unmatched.append(u)
            continue
        # 同台词重复拷贝不占坑（否则 6 份拷贝挤满 3 段上限，同镜的第二句台词反而挂不上）
        if t in seen_texts.setdefault(hit, set()):
            continue
        bucket = claims.setdefault(hit, [])
        if len(bucket) < 3:
            bucket.append(u)
            seen_texts[hit].add(t)
        else:
            overflow.add(hit)
    for i, bucket in claims.items():
        claims[i] = sort_audios_by_prompt(bucket, *scene_texts[i])
    return claims, unmatched, sorted(overflow)


def audio_tags_in_order(prompt: str) -> list[int]:
    """Prompt 里的 <Audio N> 标签按首次出现顺序去重（首现顺序 = 台词时间顺序）。"""
    tags: list[int] = []
    for m in _AUDIO_TAG_RE.finditer(prompt or ""):
        n = int(m.group(1))
        if n not in tags:
            tags.append(n)
    return tags


def renumber_audio_tags(prompt: str, audios: list[str]) -> tuple[str, str | None]:
    """把 Prompt 的 <Audio N> 编号对齐到本镜请求实际传入的音频（确定性、幂等）。

    H3 参考标签按单次请求实际传入的素材编号；分镜 Prompt 却常沿用全片全局编号，
    悬空标签让模型自行编造语音（复现事故：4 镜全部全局编号，第 2/3/4 镜配音错乱）。
    传入音频已按台词位置排序，故按首现顺序第 i 个标签 ↔ 第 i 段音频统一重编为
    <Audio 1..K>。已正确时不改动。返回（新 Prompt，变更说明或 None）。
    """
    tags = audio_tags_in_order(prompt)
    if not tags or not audios:
        return prompt, None
    mapping = {n: i + 1 for i, n in enumerate(tags)}
    if all(mapping[n] == n for n in tags):
        return prompt, None
    new_prompt = _AUDIO_TAG_RE.sub(lambda m: f"<Audio {mapping[int(m.group(1))]}>", prompt)
    changed = "、".join(f"<Audio {n}>→<Audio {mapping[n]}>" for n in tags if mapping[n] != n)
    return new_prompt, f"Prompt 音频标签为全片全局编号（{changed}），已按本镜实际传入的 {len(audios)} 段音频重排"
