"""pipeline 阶段实现：intake / normalize / plan / bind / review / generate / assemble。

设计不变量：
- LLM 只在 normalize / plan / bind-兜底 三处出现，且全部走 llm_client 信封
- 用户内容只在项目状态与 LLM 输入之间流动；分镜 Prompt 是 LLM 产物，但生成提交时
  原样读取（generation_service），不再被任何模型二次转述
- 素材清单在 plan 阶段就对 LLM 可见（根治"先拆镜后挂素材"的错配）
- 不确定的素材归属 → pending_bindings，绝不静默挂载
"""
import re
from pathlib import Path

from ...harness.errors import FatalError, NeedsUserInputError
from ...harness.state import (
    Character,
    ImageBinding,
    Material,
    PendingBinding,
    Project,
    Scene,
    clamp_duration,
    new_material_id,
    new_scene_id,
)
from .. import generation_service, material_match, project_store, prompt_timing, voice_service

# ---------------- 常量 ----------------

NORMALIZE_CHUNK_CHARS = 3500
PLAN_BATCHHint = 8  # 提示模型单批输出规模上限
PLAN_MAX_BATCHES = 25  # 200 镜安全上限
PLAN_GUARD_RETRIES = 2  # storyboard 保真闸门：镜数≠拍数时的纠错重规划次数
PLAN_WINDOW_SHOTS = 4  # storyboard 分窗：单窗拍数上限（思考+正文共用 max_tokens，窗大正文必空）
PLAN_WINDOW_CHARS = 4000  # storyboard 分窗：单窗原文累计字数上限
# plan 输出预算必须连推理过程一起覆盖：M3 是思考模型，16384 会被思考烧光
# （finish_reason=length、正文为空，2026-09-21 三连空事故）；窗小 + 预算大双保险
LLM_MAX_OUTPUT_TOKENS = 32768
BIND_CONFIDENCE_THRESHOLD = 0.7

NORMALIZE_SYSTEM = """你是剧本结构化引擎。把剧本解析为标准 JSON 结构（只输出 JSON，不要任何解释文字）。

剧本类型判定（script_type）：
- story: 故事文本/小说片段（无镜头语言）→ 由你拆成多个叙事节拍，一拍 3-8 秒画面
- screenplay: 标准编剧格式剧本（场景标题/动作/对话，如"内景-白天-诊室"）→ 每场拆成 1 个或多个节拍
- storyboard: 用户已有的成稿分镜。特征：分镜表、带镜头号的镜头列表、"片段N/第N镜/镜头N/分镜N"编号的段落、
  或"全局统一设定 + 逐段画面描述"结构（每段就是一个定稿镜头）→ 逐镜对录：一镜一拍，严禁合并、拆分、改写、增删
- ad: 广告/口播稿 → 按卖点与钩子分拍
- materials_only: 只有素材说明、没有剧本正文

输出 JSON：
{"script_type": "...", "logline": "一句话概述",
 "characters": [{"name": "...", "design": "30-60字外观设定"}],
 "beats": [{"title": "...", "description": "画面内容（视觉可拍摄）", "narration": "该拍旁白/台词原文",
            "source_hint": "原文摘录", "source_text": "该拍在原文中的逐字全文"}],
 "notes": "风格/注意事项"}

规则：
- beats 严格按播放顺序；description 写"镜头里能看见什么"；narration 写"这一拍说什么"，原文有台词就用原文
- storyboard 类型必须逐镜对录：narration/description 忠实摘录原文，source_hint 保留原句；用户写了镜头号/时长的在 description 里保留；beats 数必须等于用户的镜数，一镜不多、一镜不少
- storyboard 类型的 source_text 必须逐字复制该镜/片段在原文中的完整文本（从片段标题行到下一片段标题行之前，包含 Reference 声明、Audio Timeline、H3 Prompt 全部小节），严禁缩写、改写、省略"不要…"/"绝对不…"等否定约束
- characters 只列有戏份的主体角色，design 是跨镜一致性的外观锚点（发型/服装/年龄/体态）
- design 只写剧本明确写出的外观信息：剧本没写的性别/年龄/长相一律不猜（这是最常见的
  脑补来源）；外观由参考图锁定的角色写"外观以参考图为准"，绝不写"女性/男性医者"这类
  剧本没有的定性
- 没有角色/台词的纯风光片，characters 可为空数组
- beats 至少 1 拍；story 类型宁多勿漏（后续阶段会再规划分镜）；storyboard 类型拍数守恒，绝不多拍也绝不漏拍"""

PLAN_SYSTEM_COMMON = """你是分镜规划师。把剧本节拍规划成可执行的视频分镜，只输出 JSON。

剧本类型规则（script_type 附在用户消息开头，必须遵守）：
- storyboard（用户分镜已成稿）：一拍一镜，分镜总数必须严格等于节拍数。
  每拍附有【原文】全文（含 Reference 声明 / Audio Timeline / H3 Prompt 小节）——这是最高
  保真依据。title/description/narration 忠实取自对应拍；prompt 是原文的技术性润色重写：
  1) 保留原文的画面内容、镜头设计、否定约束（"不要…"/"绝对不…"必须原样保留，译成英文否定句）
  2) 全局 <Image N> 编号改写为本镜 <Picture k>（k 按本镜 wants_images 顺序从 1 起；
     素材清单标注了上传序号，第 N 张上传图即原文 <Image N>）
  3) <Audio N> 按本镜顺序从 1 起重编（提交时系统还会按实际传入音频自动重排）
  严禁增加、删除、拆分、合并镜头，严禁虚构原文没有的画面情节与交互
- story / screenplay / ad / materials_only：节拍是创作素材，可一拍一镜或一拍多镜，按叙事需要规划

每镜字段：
- title: ≤20字
- description: 画面概述
- prompt: 完整视频生成 Prompt（按下方【Prompt 结构规范】书写）
- keyframe_prompt: i2v 镜的关键帧图描述（构图+景别+主体+姿态+光线，≥20字）；t2v 镜留空
- narration: 本镜旁白/台词（无则空字符串）
- duration: 时长（整数秒，在模型允许范围内）
- gen_mode: "i2v"（有关键帧或参考图）或 "t2v"（纯文生，仅用于无主体一致性要求的抽象镜头）
- wants_images/wants_audios/wants_videos: 本镜需要的素材，元素为 {"material_id": "素材id", "description": "用途说明", "role": "reference"}
- 图片 role 只准填 reference：首帧(first_frame)只属于首尾帧模式且由用户手动指定，自动规划绝不使用

素材使用规则（重要）：
- 素材清单附在用户消息里（带 id/类型/文件名/摘要/时长/上传序号）。某镜需要某素材时，优先用 material_id 精确引用
- 用户上传的角色/风格参考图是外观锚点，需要一致性的镜都要引用同一素材
- 角色性别/年龄/五官等外观属性只能来自角色设定或剧本原文；两者都没写的一律不写
  （尤其性别——交给参考图锚定，不要按名字或职业人设推测）
- 用户上传的音频是"要烧进该镜成片的台词/配音"，按台词内容挂到对应镜（不要全挂到第一镜）
- 用不上的素材绝不引用；参考音频每镜最多 3 段，参考图每镜最多 9 张
- 挂了用户参考图的镜，Prompt 里的 <Picture N> 编号顺序必须与 wants_images 顺序一致
"""

# H3 Ref2VA 六段式：distilled from h3-prompt-writing skill（references/ref-en.txt + base-en.txt）。
# 全目录视频模型均为 MiniMax H3 工作流（多图+多音频参考生成），此规范即默认契约。
PLAN_SYSTEM_H3 = """【Prompt 结构规范：H3 Ref2VA 六段式（段名逐字使用，六段缺一不可，无配乐时 non_diegetic_music 写 N/A）】

subject_definitions: 定义本镜全部引用内容与标签，每项一行：
- <Subject N>：可复用的可见内容单元（角色/场景/服装/道具）。跨镜出现的主要角色必须给
  <Subject> 标签，外观来源引用对应锚点图，例：
  "<Subject 1> is the female doctor 吕满春 in <Picture 1>, wearing a clean white lab coat."
  只用于定义角色/风格、不作画面锚点的图不单列 <Picture> 行，写进对应 <Subject> 定义里
- <Picture N>：作为本镜画面/构图/状态锚点的参考图，第 N 张 = 本镜 wants_images 第 N 项，
  一行说明它锚定什么，例："<Picture 3> is the storyboard keyframe anchoring the studio
  composition and the vitiligo patch states for this shot."
- <Audio N>：本镜绑定的音频，第 N 段 = 本镜 wants_audios 第 N 项；对应说话人的写
  "<Audio 1> is the spoken line of <Subject 1> (S1)"（编号按本镜顺序从 1 起；
  只写是谁的话，严禁在这里引用台词原文）
标签一经定义，全文含义不变；任何段落不得出现 subject_definitions 里没有的标签。

summary: 一段英文概述，以方括号任务类型开头。类型只从这些里选并用 + 连接：
keyframe completion / reference generation / video editing / video continuation / audio reuse / audio reference。
参考图给画面参考→reference generation；上传台词音频直接烧录→audio reuse。
例："[reference generation + audio reuse] The target clip shows <Subject 1> explaining ... in the
virtual studio, using <Picture 1>-<Picture 3> as visual anchors and <Audio 1>/<Audio 2> as the spoken lines."

retention_analysis: 每个引用标签一行，说明在成片中的保留方式，关系标记用固定英文值：
可见内容（<Subject>/<Picture>）用 fully_preserved / partially_preserved / attribute_transfer / weak_reference；
音频用 fully_copy / partially_copy / reference / weak_reference。
例："<Subject 1> (appears in [Shot 1]): fully_preserved - identity, white coat and hairstyle follow the anchor image."

detailed_description: 主体段落（单镜成片通常 150-300 英文词）：
- 英文书写；画面内可见文字保留原语言；台词/歌词原文只出现在 Audio Timeline 小节
- 每镜是一条独立成片：正文以 "[Shot 1]" 开头（无时间戳）；镜内推进用自然英文描述，
  全部动作/台词的总时长必须等于该镜 duration，严禁写超出时长的时刻
- 镜头运动写成自然英文动作（类型+幅度+速度）：Zoom In / Push In / Pull Out / Pan Left / Pan Right /
  Truck Left / Truck Right / Tilt Up / Tilt Down / Pedestal Up / Arc Shot / Tracking Shot / Static Shot，
  例："The camera pushes in with small amplitude at slow speed toward the skin observation window."
- 说话人用稳定 ID：首次出现给身份描述并记 (S1)，后续复用不重编；台词文字严禁写进正文叙述
  ——<d> 标签、引号台词都不写：正文出现台词会让模型自行合成第二路语音，与 <Audio N>
  参考音频在成片里叠放成"多个声音同时说话"；正文只写说话动作与音频引用，例：
  <Subject 1> (S1) delivers the spoken line of <Audio 1> with lips synchronized；
  画外音用 "says in an off-screen voiceover" 并注明画面人物嘴唇不动
- Audio Timeline 小节（detailed_description 末尾，台词唯一出现处），每段一行：
  `0.0-3.4s | <Audio 1> | <Subject 1> (S1): "台词原文"`——秒数用英文小数与连字符，
  说话人写 <Subject N> 与 (S<编号>)，台词原文加英文双引号逐字保留原语言、不得增删改写；
  多段首尾相接、末段终点等于该镜 duration；本镜无绑定音频则不写此小节
- 首次出现的重要 <Subject N> 描述外观、画幅内位置与当前动作；画面可见文字加英文双引号
- 原文的否定约束必须保留为英文否定句，例："The edge never gets a black outline; the patch
  does not grow or shrink." / "No subtitles, no on-screen text."

overall_soundscape: 1-4 句英文概括环境音与物理声（对白不在此重复）；如 "Only the reference
audio and a faint virtual-studio room tone; no extra dialogue."
non_diegetic_music: 1-3 句英文描述观众可听、角色听不到的配乐（乐器/速度/动态）；无配乐写 N/A。

禁止：Action Timeline 分段表、中文段名、自创段落、剧情梗概式省略、悬空标签、时间轴与 duration 不符。
"""

PLAN_SYSTEM_LEGACY = """【Prompt 结构规范（非 H3 模型）】
prompt ≥100 字，结构：
1) 开头一句画面总述（主体+环境+基调）
2) ## Action Timeline：按镜内相对时间分段（0-{duration}s 各段之和=时长，严禁沿用剧本全局时刻）
3) 参考素材声明行：本镜绑定的第 i 张图写 <Picture i>：身份说明；第 i 段音频写 <Audio i>：内容说明
4) 音频/旁白设计：引用 <Audio i> 或描述环境音
"""

PLAN_SYSTEM_TAIL = """
输出 JSON：{"shots": [镜...], "total": 规划总镜数, "done": 是否全部输出完毕}
单批最多输出 8 镜；还有剩余镜时 done=false 并等我继续要求。"""


def plan_system_prompt(h3_style: bool) -> str:
    """按视频模型族选 Prompt 结构规范：H3 工作流 → Ref2VA 六段式；其余 → 旧结构。"""
    return PLAN_SYSTEM_COMMON + (PLAN_SYSTEM_H3 if h3_style else PLAN_SYSTEM_LEGACY) + PLAN_SYSTEM_TAIL


BIND_SYSTEM = """你是素材对号助手。把未绑定的素材分配到最合适的分镜，只输出 JSON。

规则：
- 只分配"有把握"的：confidence ≥ 0.7；拿不准的不要硬分配（返回低置信或不返回）
- 角色设定图/风格参考图可绑定到多个出现该角色的镜
- 音频按台词内容对号（文件名通常带台词摘要）
- role 固定填 reference：首帧(first_frame)只属于首尾帧模式且由用户手动指定，自动分配绝不使用
- 输出 JSON：{"decisions": [{"material_id": "...", "scene_index": 0, "kind": "image|audio|video", "role": "reference", "confidence": 0.9, "reason": "一句依据"}]}"""


# ---------------- 工具 ----------------


def _store_mutate(project_id: str, fn) -> dict:
    return project_store.mutate(project_id, fn)


def _script_source(project: Project) -> tuple[str, Material | None]:
    """取剧本正文：优先 kind=script 的素材，其次 doc 素材正文。

    多份剧本（草稿迭代）时取最后投放的一份——素材池按追加顺序即投放顺序，
    用户改稿后重传是常态，第一份往往是旧草稿（事故：md 旧稿先入池，定稿后入池，
    流水线却按旧稿的参考图编号对齐，全部错位）。"""
    candidates = [m for m in project.materials
                  if m.kind in ("script", "doc") and (m.text or "").strip()]
    for m in reversed(candidates):
        if m.kind == "script":
            return m.text, m
    if candidates:
        return candidates[-1].text, candidates[-1]
    return "", None


def materials_manifest(project: Project, with_order: bool = False) -> str:
    """素材清单文本。with_order=True 时给图/音频标注上传序号——storyboard 原稿的
    <Image N>/<Audio N> 全局编号按上传顺序对齐，序号是模型正确引用的锚。"""
    lines = []
    img_no = aud_no = 0
    for m in project.materials:
        if m.kind in ("script", "doc"):
            continue
        desc = m.filename or ""
        order_tag = ""
        if with_order:
            if m.kind == "image":
                img_no += 1
                order_tag = f"[第{img_no}张上传图→原文<Image {img_no}>] "
            elif m.kind == "audio":
                aud_no += 1
                order_tag = f"[第{aud_no}段上传音频→原文<Audio {aud_no}>] "
        if m.kind == "audio":
            # filename 优先：旧格式配音登记时已把台词合成进文件名（url 本身还是 uuid）
            tail = material_match.audio_semantic(m.filename or m.url)
            desc = f"{m.filename}（台词摘要：{tail}，时长 {m.duration or '?'}s）" if tail else m.filename
        lines.append(f"- id={m.id} 类型={m.kind} {order_tag}{desc}")
    return "\n".join(lines) or "（无文件素材）"


def _model_constraints(project: Project) -> str:
    vmodel = generation_service.resolve_video_model(project.settings.video_model)
    durations = vmodel.get("durations") or list(range(1, 16))
    modes = vmodel.get("modes") or ["text", "image", "reference"]
    return (
        f"视频模型「{vmodel['label']}」（{vmodel['id']}）：时长 {min(durations)}-{max(durations)}s 整数；"
        f"支持模式 {'/'.join(modes)}（t2v 需要 text 模式支持）。画幅 {project.aspect_ratio}。"
    )


# ---------------- intake（纯代码） ----------------


def run_intake(project_id: str, payload: dict, trace=None) -> dict:
    """登记素材入池：script 正文、doc 提取文本、图/音频/视频记录。幂等：同 URL 不重复登记，
    同身份（同类型+同语义文件名/同正文）的整包重传也不重复登记——重传拷贝会以新哈希
    前缀 URL 绕过 URL 判重，把素材池翻倍（复现事故：32 个素材里一半是重复拷贝）。

    音频时长探测由 async runner 在 intake 后调 probe_audio_durations 完成。
    """
    script_text = (payload.get("script_text") or "").strip()
    material_items = payload.get("materials") or []

    def fn(p: Project) -> None:
        existing_urls = {m.url for m in p.materials if m.url}
        existing_scripts = {
            material_match.norm_tts_text((m.text or "")[:4000])
            for m in p.materials if m.kind in ("script", "doc")
        }
        seen_keys = {
            (m.kind, material_match.semantic_file_key(m.filename or m.url or ""))
            for m in p.materials if m.kind not in ("script", "doc")
        }
        if script_text and material_match.norm_tts_text(script_text[:4000]) not in existing_scripts:
            p.materials.append(Material(
                id=new_material_id(), kind="script", text=script_text[:200000],
                filename="inline_script.txt", url=None,
            ))
        for item in material_items:
            url = str(item.get("url") or "")
            if not url or url in existing_urls:
                continue
            kind = str(item.get("kind") or _infer_kind(url))
            filename = str(item.get("filename") or url.rsplit("/", 1)[-1])[:200]
            key = (kind, material_match.semantic_file_key(filename))
            if key in seen_keys:  # 同名重传拷贝：已有同一张图/同一段音频，不重复入池
                continue
            seen_keys.add(key)
            p.materials.append(Material(
                id=new_material_id(), kind=kind, url=url,
                filename=filename,
                text=(item.get("text") or None),
                duration=(float(item["duration"]) if item.get("duration") else None),
            ))

    _store_mutate(project_id, fn)

    # doc 正文提取（同步读本地文件）
    def fn2(p: Project) -> None:
        from .. import file_service

        for m in p.materials:
            if m.kind == "doc" and not m.text and m.url:
                local = file_service.resolve_local(m.url)
                if local and Path(local).exists():
                    try:
                        m.text = Path(local).read_text(encoding="utf-8", errors="replace")[:200000]
                    except OSError:
                        pass

    _store_mutate(project_id, fn2)
    if trace:
        trace.log("stage_done", stage="intake", materials=len(project_store.get(project_id)["materials"]))
    return {"ok": True}


def _infer_kind(url: str) -> str:
    u = url.lower()
    if "/image/" in u:
        return "image"
    if "/audio/" in u:
        return "audio"
    if "/video/" in u:
        return "video"
    if "/doc/" in u:
        return "doc"
    return "file"


async def probe_audio_durations(project_id: str) -> None:
    """补齐音频素材时长（runner 在 intake 后调用）。"""
    project_dict = project_store.get(project_id)
    if not project_dict:
        return
    project = Project.model_validate(project_dict)
    updates: dict[str, float] = {}
    for m in project.materials:
        if m.kind == "audio" and m.duration is None and m.url:
            try:
                sec = await voice_service.probe_audio_seconds(m.url)
            except Exception:  # noqa: BLE001
                continue
            if sec:
                updates[m.id] = sec
    if updates:
        def fn(p: Project) -> None:
            for m in p.materials:
                if m.id in updates:
                    m.duration = updates[m.id]
        _store_mutate(project_id, fn)


# ---------------- normalize（LLM 信封） ----------------


def _split_source(text: str, limit: int = NORMALIZE_CHUNK_CHARS) -> list[str]:
    """确定性输入分块：按空行/段落切到 ≤limit，防 LLM 输出被 max_tokens 截断整段丢稿。"""
    if len(text) <= limit:
        return [text]
    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def _norm_for_match(t: str) -> str:
    """保真回收用的宽松归一：只去空白，标点保留（标点是原文的一部分）。"""
    return "".join((t or "").split())


# ---------------- 角色外观接地：参考图锚定的角色禁止模型编性别/年龄 ----------------
# 复现事故：剧本通篇未写吕满春性别（42 行相关文本零性别词），normalize 按名字+医者人设
# 脑补"女性医者形象"，渗进每条 prompt（"<Subject 1> is 吕满春, a female skin doctor…"）。
# 规则：剧本语境里没有性别依据的角色，任何性别词一律剔除；外观交给参考图锚定。
_GENDER_TOKENS_ZH = (
    "女性", "男性", "女子", "男子", "女人", "男人", "女孩", "男孩", "女生", "男生",
    "女医生", "男医生", "女医者", "男医者", "女大夫", "男大夫", "女士", "大叔", "阿姨",
    "大妈", "美女", "帅哥", "大爷", "大婶",
)
_GENDER_TOKENS_EN = ("female", "male", "woman", "women", "man", "men", "girl", "boy")


def _has_gender_basis(context: str) -> bool:
    """角色的剧本语境里是否出现过性别信息（性别词或她/他指代）。

    注意"其他"含"他"——台词"还要看看其他地方…"与角色同行会让裸"他"误判
    有性别依据（事故：female doctor 因此漏网）。先剔掉"其他"再查单字指代。
    """
    ctx = (context or "").replace("其他", "")
    low = ctx.lower()
    return any(t in ctx for t in _GENDER_TOKENS_ZH) or \
        any(t in low for t in _GENDER_TOKENS_EN) or \
        any(t in ctx for t in ("女", "男", "她", "他"))


def _strip_gender_words(text: str) -> str:
    """剔除文本里的性别词（中文词整词删除；英文词带边界删除并收拢空格）。"""
    out = text
    for t in sorted(_GENDER_TOKENS_ZH, key=len, reverse=True):
        out = out.replace(t, "")
    out = re.sub(r"\b(" + "|".join(_GENDER_TOKENS_EN) + r")\b\s*", "", out, flags=re.I)
    return re.sub(r"[，,、]{2,}", "，", re.sub(r"\s{2,}", " ", out)).strip("，。, ")


def ground_characters(characters: dict[str, str], source_text: str) -> tuple[dict[str, str], list[str]]:
    """normalize 角色设定接地校准（确定性，不信任模型自觉）：

    1) 剧本语境无性别依据的角色 → 剔除设定里的性别词（模型推测不算依据）
    2) 全局参考图编号表能按名字指认的角色 → 追加"外观严格以参考图为准"锚定声明
    返回 (校准后的设定, 变更说明)。
    """
    table = _reference_table(source_text or "")
    lines = (source_text or "").splitlines()
    out: dict[str, str] = {}
    notes: list[str] = []
    for name, design in characters.items():
        ctx = "\n".join(ln for ln in lines if name in ln)
        new_design = design
        if not _has_gender_basis(ctx):
            stripped = _strip_gender_words(design)
            if stripped != design:
                notes.append(f"角色「{name}」的性别描述无剧本依据（模型推测），已剔除并交给参考图锚定")
                new_design = stripped
        anchor = next(
            (f"原稿 <Image {n}>：{title}" for n, title in sorted(table.items()) if name in (title or "")),
            None)
        if anchor and "参考图" not in new_design and "参考 <Image" not in new_design:
            new_design = (new_design.rstrip("。；;.").rstrip()
                          + f"；外观（含性别/年龄/五官/发型）严格以{anchor}为准，不得自行设定")
        out[name] = new_design
    return out, notes


# 正文 <d> 台词标签（含 [语言] 前缀）：模型会照它自行合成第二路语音，与 <Audio N> 参考
# 音频在成片里叠放成"多个声音同时说话"——台词只保留在文末原文块的 Audio Timeline
_D_TAG_RE = re.compile(r"<d>\s*(?:\[[^\]\n]{1,20}\]\s*)?[\s\S]*?</d>")


# LLM 回显的原文块（输入里的 beats 原文可能被抄进输出）：对视频模型无用且中文台词行
# 会诱导叠配音轨，plan 产出确定性剥除
_ECHOED_VERBATIM_RE = re.compile(r"\n\s*##\s*原文[\s\S]*$")


def scrub_d_tags(prompt: str) -> tuple[str, int]:
    """删除 prompt 正文里的 <d>…</d>（连同台词内容）。确定性兜底：LLM 可能无视规范
    写 <d>；删了不丢信息——detailed_description 末尾的 Audio Timeline 有台词副本。
    同时剥除 LLM 回显的「## 原文」块（原文本就不该再进 prompt）。"""
    prompt = _ECHOED_VERBATIM_RE.sub("", prompt)
    return _D_TAG_RE.subn("", prompt)


def scrub_prompt_gender(prompt: str, ungrounded_names: list[str]) -> str:
    """按句剔除 prompt 中无剧本依据角色的性别词（句子含该角色名才动，避免误伤别的角色）。"""
    if not ungrounded_names:
        return prompt
    sentences = re.split(r"(?<=[。．！？!?\n])", prompt)
    out = []
    for sent in sentences:
        if any(name in sent for name in ungrounded_names):
            sent = _strip_gender_words(sent)
        out.append(sent)
    return "".join(out)


def _locate_in_source(anchor: str, norm_source: str, idx_map: list[int], source: str) -> int | None:
    """把归一化锚文本定位回原文下标；找不到或不在行首返回 None。

    行首校验防误定位：旁白/台词也逐字出现在片段中部，拿它当锚会把切片起点
    拉到 Audio Timeline 一带，Reference 声明段整个丢掉。片段起点必然是行首。
    """
    needle = _norm_for_match(anchor)
    if len(needle) < 6:
        return None
    pos = norm_source.find(needle)
    while pos >= 0:
        orig = idx_map[pos]
        if orig == 0 or source[orig - 1] == "\n":
            return orig
        pos = norm_source.find(needle, pos + 1)
    return None


def recover_storyboard_source(beats: list[dict], source: str) -> tuple[list[dict], str]:
    """storyboard 逐字保真回收：LLM 的 source_text 只当定位锚，能定位就以原文切片为准。

    片段 i 的原文 = [片段 i 起点, 片段 i+1 起点)；起点用 source_text/title 的归一化
    包含匹配定位。定位失败的拍保留 LLM 版本并打标。返回 (校准后的 beats, 原文头部
    到首个片段起点的全局设定 preamble)。非 storyboard 调用方不应使用。
    """
    norm_source = ""
    idx_map: list[int] = []
    for i, ch in enumerate(source):
        if not ch.isspace():
            norm_source += ch
            idx_map.append(i)
    starts: list[int | None] = []
    for b in beats:
        for anchor in (b.get("source_text") or "", f"{b.get('title') or ''}{b.get('description') or ''}"):
            pos = _locate_in_source(anchor[:120], norm_source, idx_map, source)
            if pos is not None:
                starts.append(pos)
                break
        else:
            starts.append(None)
    # 单调性保护：片段必须按出现顺序排列，定位出的起点倒挂说明锚匹配到了别处，弃用
    last = -1
    for i, p in enumerate(starts):
        if p is not None:
            if p < last:
                starts[i] = None
            else:
                last = p
    first_known = next((p for p in starts if p is not None), 0)
    preamble = source[:first_known].strip()
    for i, b in enumerate(beats):
        st = starts[i]
        if st is None:
            b.setdefault("source_text", "")
            b["source_fidelity"] = "llm_echo"  # 未能从原文定位，仅保留 LLM 转述
            continue
        end = next((p for p in starts[i + 1:] if p is not None), len(source))
        verbatim = source[st:end].strip()
        if verbatim:
            b["source_text"] = verbatim
            b["source_fidelity"] = "verbatim"
    return beats, preamble


_H1_SECTION_LINE_RE = re.compile(r"^#[ \t]*[^#\r\n]", re.M)


def trim_trailing_sections(beats: list[dict]) -> list[dict]:
    """末拍切回稿尾全局章节之前：没有后续片段头时，末拍切片会一路吃到文件尾，
    把"六、节奏结构 / 七、视觉风格关键词"这类全稿设定吞成最后一镜的原文
    （它们是全局设定，不是该镜的拍摄内容）。在末拍正文里（跳过首行编号头）
    找第一个 h1 段落标题，把切片收回到它之前；切完剩余过短视为误匹配，不切。
    """
    if not beats:
        return beats
    last = beats[-1]
    st = (last.get("source_text") or "").strip()
    if "\n" not in st:
        return beats
    m = _H1_SECTION_LINE_RE.search(st, st.find("\n") + 1)
    if not m:
        return beats
    keep = re.sub(r"(?:\s*-{3,})+\s*$", "", st[: m.start()]).rstrip()
    if len(keep) < 80:
        return beats
    last["source_text"] = keep
    return beats


# 认「片段 N｜标题」「分镜 N｜标题」及裸编号行「分镜 N」（行尾锚定，散文里"分镜 2 的…"
# 不在行首裸编号位不会误触）；【第N镜】括号式。裸"片段N：7s"这类时长行由标题闸排除。
_FRAGMENT_HEADER_LINE_RE = re.compile(
    r"^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜)\s*\d+\s*[｜|:：]\s*[^\n]+"
    r"|^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜)\s*\d+\s*$"
    r"|^(?:#{1,4}\s*)?【第[一二三四五六七八九十百零\d]+\s*镜】[^\n]*",
    re.M,
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\ufaff]{2,}")


def detect_fragment_headers(source: str) -> list[tuple[int, str]]:
    """识别分镜稿的片段标题行 → [(起始下标, 标题)]。

    标题必须含 ≥2 个连续汉字且不是纯时长——"片段1：7s" 这类时长汇总行会被排除。
    ≥2 个头才认定"全局统一设定 + 逐片段成稿"的分镜稿结构。
    """
    headers: list[tuple[int, str]] = []
    for m in _FRAGMENT_HEADER_LINE_RE.finditer(source or ""):
        line = m.group(0).strip()
        title = re.sub(
            r"^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜)\s*\d+\s*(?:[｜|:：]\s*)?"
            r"|^(?:#{1,4}\s*)?【第[一二三四五六七八九十百零\d]+\s*镜】\s*",
            "", line).strip()
        if not title:
            # 裸编号头（"# 分镜 1"）：编号行本身当标题——"分镜/片段"两个汉字可过下方汉字闸
            title = re.sub(r"^#{1,4}\s*", "", line).strip()
        if not _CJK_RUN_RE.search(title) or re.fullmatch(r"[\d\s秒Ss.：:]+", title):
            continue
        headers.append((m.start(), title[:50]))
    return headers


def _extract_narration(frag: str) -> str:
    """确定性提取片段台词/旁白：引号台词 + "旁白：xxx"（bind 台词对号的依据之一）。"""
    quotes = re.findall(r"[“「\"]([^”」\"\n]{2,})[”」\"]", frag)
    asides = re.findall(r"旁白[:：]\s*([^\n]+)", frag)
    parts = [p.strip() for p in (*quotes, *asides) if p.strip()]
    return "\n".join(parts)[:2000]


def _beats_from_headers(source: str, headers: list[tuple[int, str]]) -> list[dict]:
    """按片段头切原文成 beats：一镜一拍由代码保证，source_text 逐字切片。

    零信任 LLM 分拍——事故：定稿 4 片段被模型按台词/时间轴拆成 11 拍，
    保真闸门拿 11 当基准，越守越错。
    """
    beats = []
    for i, (start, title) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else len(source)
        frag = source[start:end].strip()
        beats.append({
            "title": title or f"片段{i + 1}",
            "description": title[:200],
            "narration": _extract_narration(frag),
            "source_hint": frag.splitlines()[0][:200] if frag else "",
            "source_text": frag,
            "source_fidelity": "verbatim",
        })
    return beats


async def run_normalize(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    source, mat = _script_source(project)
    if not source.strip():
        raise NeedsUserInputError("项目还没有剧本正文：请投放剧本文本，或上传 .md/.txt 剧本文件后再启动流水线")

    beats: list[dict] = []
    characters: dict[str, str] = {}
    script_type = ""
    notes: list[str] = []
    logline = ""
    from .schemas import Screenplay

    frag_headers = detect_fragment_headers(source)
    deterministic = len(frag_headers) >= 2
    if deterministic:
        # 分镜稿结构可识别：逐片段拆拍由代码完成，LLM 只出元数据（角色/概述/备注）
        meta_user = ("解析以下分镜稿，只需要 script_type / logline / characters / notes 四项元数据"
                     "（beats 给 1 条占位即可，严禁自行逐镜拆分——拆拍由系统按片段标题确定性完成）：\n\n"
                     + source[:NORMALIZE_CHUNK_CHARS * 2])
        result = await envelope.structured_call(NORMALIZE_SYSTEM, meta_user, Screenplay, max_tokens=LLM_MAX_OUTPUT_TOKENS)
        script_type = "storyboard"
        logline = result.logline
        notes.append(result.notes)
        for c in result.characters:
            characters.setdefault(c.name, c.design)
        beats = _beats_from_headers(source, frag_headers)
        if trace:
            trace.log("normalize_deterministic_beats", fragments=len(beats))
    else:
        chunks = _split_source(source)
        for i, chunk in enumerate(chunks):
            part = f"【剧本片段 {i + 1}/{len(chunks)}】\n{chunk}" if len(chunks) > 1 else chunk
            extra = f"\n\n（整部剧本已分 {len(chunks)} 片段，本片段类型沿用首片段判定：{script_type}）" if script_type and len(chunks) > 1 else ""
            result = await envelope.structured_call(NORMALIZE_SYSTEM, f"解析以下剧本：\n\n{part}{extra}", Screenplay, max_tokens=LLM_MAX_OUTPUT_TOKENS)
            script_type = script_type or result.script_type
            logline = logline or result.logline
            notes.append(result.notes)
            for c in result.characters:
                characters.setdefault(c.name, c.design)
            beats.extend(b.model_dump() for b in result.beats)
            if trace:
                trace.log("normalize_chunk", chunk=i + 1, total=len(chunks), beats=len(result.beats))

    # 角色设定接地校准：剔除无剧本依据的性别词、补参考图锚定声明（确定性，
    # 不信任模型自觉——"女性医者"式脑补从事故现场来）
    characters, ground_notes = ground_characters(characters, source)
    if ground_notes and trace:
        trace.log("normalize_characters_grounded", notes=ground_notes)

    screenplay = {
        "script_type": script_type,
        "logline": logline,
        "characters": [{"name": k, "design": v} for k, v in characters.items()],
        "beats": beats,
        "source_material_id": mat.id if mat else None,
        # 原文全文 + 全局设定 preamble（参考图编号表在这里）：后续阶段对齐/声明行
        # 重写的确定性依据，绝不依赖 LLM 的转述保真
        "source_text": source[:200000],
        "global_notes": "",
    }
    if script_type == "storyboard":
        beats, preamble = recover_storyboard_source(beats, source)
        beats = trim_trailing_sections(beats)
        screenplay["beats"] = beats
        screenplay["global_notes"] = preamble[:20000]
        if trace:
            fidelity = [b.get("source_fidelity") for b in beats]
            trace.log("normalize_source_recovered", verbatim=fidelity.count("verbatim"),
                      llm_echo=fidelity.count("llm_echo"), preamble=len(preamble))

    def fn(p: Project) -> None:
        p.screenplay = screenplay
        p.characters = [Character(name=k, design=v) for k, v in characters.items()]

    _store_mutate(project_id, fn)
    return {"ok": True, "script_type": script_type, "beats": len(beats), "characters": list(characters)}


# ---------------- plan（LLM 信封 + 分批） ----------------


async def run_plan(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    if not project.screenplay:
        raise FatalError("项目缺少剧本结构（screenplay），请先执行 normalize 阶段")
    beats = project.screenplay.get("beats") or []
    if not beats:
        raise FatalError("剧本节拍为空，无法规划分镜（请检查 normalize 阶段产出）")
    script_type = str(project.screenplay.get("script_type") or "")
    is_storyboard = script_type == "storyboard"
    constraints = _model_constraints(project)
    chars = project.screenplay.get("characters") or []

    global_notes = str(project.screenplay.get("global_notes") or "").strip()
    global_block = f"\n\n全局设定与参考图编号表（原文头部，<Image N> 身份以此为准）：\n{global_notes}\n" if is_storyboard and global_notes else ""
    manifest = materials_manifest(project, with_order=is_storyboard)
    chars_block = "\n".join(f"- {c['name']}：{c['design']}" for c in chars) or "（无）"
    common_mid = (
        f"\n\n视频模型约束：{constraints}\n\n角色设定（跨镜一致，Prompt 中需体现或用参考图锚定）：\n{chars_block}"
        f"{global_block}\n素材清单（需引用素材时用 material_id）：\n{manifest}\n\n"
    )

    def storyboard_beats_text(items: list[dict], start_no: int) -> str:
        return "\n\n".join(
            f"===== 全稿第 {start_no + i} 拍【原文】（逐字，规划此镜时以它为最高依据）=====\n{(b.get('source_text') or b.get('description') or '').strip()}"
            + (f"\n（摘要：{b.get('description') or ''}｜旁白：{b.get('narration') or ''}）" if not b.get("source_text") else "")
            for i, b in enumerate(items)
        )

    def storyboard_windows() -> list[tuple[list[dict], str]]:
        """storyboard 分窗：单窗 ≤PLAN_WINDOW_SHOTS 拍且原文累计 ≤PLAN_WINDOW_CHARS 字。

        事故（2026-09-21 吕满春西游 6 镜稿）：全稿原文一锅端进一条消息，MiniMax-M3
        是推理模型、思考与正文共用 max_tokens，一次规划多个 H3 六段式长 prompt 时
        预算被思考烧尽、正文为空，连续 3 轮失败。窗小 → 单次任务小，思考与输出
        都落回预算内；每窗自带一拍一镜数量闸门，全稿总数闸门保持不变。
        """
        windows: list[list[dict]] = []
        cur: list[dict] = []
        cur_chars = 0
        for b in beats:
            size = len(b.get("source_text") or b.get("description") or "") + 100
            if cur and (len(cur) >= PLAN_WINDOW_SHOTS or cur_chars + size > PLAN_WINDOW_CHARS):
                windows.append(cur)
                cur, cur_chars = [], 0
            cur.append(b)
            cur_chars += size
        if cur:
            windows.append(cur)
        out: list[tuple[list[dict], str]] = []
        no = 1
        for win in windows:
            note = (
                f"剧本类型：storyboard（用户分镜已成稿，全稿共 {len(beats)} 镜）→ 本窗为全稿第 {no}–{no + len(win) - 1} 镜，"
                f"一拍一镜，必须恰好输出 {len(win)} 镜，逐拍对录，严禁增删拆合与虚构画面；"
                "素材清单已标注上传序号，第 N 张上传图=原文 <Image N>"
            )
            base = (
                f"{note}{common_mid}剧本节拍（本窗 {len(win)} 拍，按播放顺序）：\n{storyboard_beats_text(win, no)}\n\n"
                f"规划本窗全部分镜，共 {len(win)} 镜，一次性全部输出（done=true）。"
            )
            out.append((win, base))
            no += len(win)
        return out

    if not is_storyboard:
        type_note = f"剧本类型：{script_type or 'unknown'} → 节拍是创作素材，可按叙事需要规划分镜"
        beats_text = "\n".join(
            f"{i + 1}. [{b.get('title') or ''}] 画面：{b.get('description') or ''}｜旁白：{b.get('narration') or ''}"
            for i, b in enumerate(beats)
        )
        base_user = (
            f"{type_note}{common_mid}剧本节拍（按播放顺序，共 {len(beats)} 拍）：\n{beats_text}\n\n"
            f"规划全部分镜。总数不超过 {PLAN_BATCHHint} 镜时一次性全部输出（done=true）；"
            f"更多时先输出发音完整的前 {PLAN_BATCHHint} 镜（done=false），后续我会要求继续。"
        )

    from .schemas import PlanBatch, PlanBatchH3

    vmodel = generation_service.resolve_video_model(project.settings.video_model)
    # 全目录当前均为 MiniMax H3 工作流：Ref2VA 六段式契约 + schema 级格式闸门
    h3_style = "h3" in str(vmodel.get("id") or "").lower()
    plan_system = plan_system_prompt(h3_style)
    plan_schema = PlanBatchH3 if h3_style else PlanBatch

    async def collect_batches(base: str, extra_note: str = "") -> list[dict]:
        first = base + (f"\n\n{extra_note}" if extra_note else "")
        shots: list[dict] = []
        for batch_no in range(PLAN_MAX_BATCHES):
            # 续批消息重带完整 base：structured_call 每次都是全新会话，
            # 只发已出镜清单会让模型在没有原文的情况下凭标题续写（幻觉源）
            user = first if batch_no == 0 else (
                base + f"\n\n【继续】本批分镜尚未输出完，已输出 {len(shots)} 镜：\n"
                + "\n".join(f"- {i + 1}. {s['title']}" for i, s in enumerate(shots))
                + f"\n请从第 {len(shots) + 1} 镜继续，一次性输出剩余全部，输出完时 done=true。"
            )
            batch = await envelope.structured_call(plan_system, user, plan_schema, max_tokens=LLM_MAX_OUTPUT_TOKENS)
            shots.extend(s.model_dump() for s in batch.shots)
            if trace:
                trace.log("plan_batch", batch=batch_no + 1, got=len(batch.shots), total_planned=batch.total, done=batch.done)
            if batch.done or not batch.shots:
                break
        return shots

    async def plan_storyboard_windows(extra_note: str = "") -> list[dict]:
        acc: list[dict] = []
        for w_no, (win, wbase) in enumerate(storyboard_windows()):
            ws = await collect_batches(wbase, extra_note)
            if len(ws) != len(win):
                if trace:
                    trace.log("plan_window_retry", window=w_no + 1, expected=len(win), got=len(ws))
                ws = await collect_batches(
                    wbase,
                    f"【数量错误】上一轮输出 {len(ws)} 镜，本窗共 {len(win)} 拍，必须一拍一镜恰好 {len(win)} 镜："
                    "逐拍对录，不得增加、删除、拆分、合并。")
            if trace:
                trace.log("plan_window", window=w_no + 1, beats=len(win), got=len(ws), total=len(acc) + len(ws))
            acc.extend(ws)
        return acc

    all_shots = await (plan_storyboard_windows() if is_storyboard else collect_batches(base_user))
    if not all_shots:
        raise FatalError("plan 阶段没有产出任何分镜")

    # storyboard 保真闸门：镜数必须等于拍数。提示词约束可能被模型无视，这里确定性把关——
    # 不满足时带纠错反馈重规划，仍失败则干净报错，绝不把扩写结果静默落盘
    if is_storyboard:
        for attempt in range(1, PLAN_GUARD_RETRIES + 1):
            if len(all_shots) == len(beats):
                break
            if trace:
                trace.log("plan_guard_retry", attempt=attempt, expected=len(beats), got=len(all_shots))
            all_shots = await plan_storyboard_windows(
                f"【数量错误】上一轮共输出 {len(all_shots)} 镜，但用户分镜稿共 {len(beats)} 镜（一拍一镜）。"
                f"必须每窗恰好对录：不得增加、删除、拆分、合并。"
            )
        if len(all_shots) != len(beats):
            raise FatalError(
                f"storyboard 保真校验失败：用户分镜 {len(beats)} 镜，规划产出 {len(all_shots)} 镜"
                f"（已纠错重试 {PLAN_GUARD_RETRIES} 轮），已中止且未落盘；请重试或改用对话方式调整分镜"
            )

    modes = set(vmodel.get("modes") or ["text", "image", "reference"])
    notes: list[str] = []
    scenes: list[Scene] = []
    d_scrubbed = 0
    for i, sp in enumerate(all_shots):
        gen_mode = sp["gen_mode"]
        duration = clamp_duration(sp["duration"], project.settings.video_model)
        if gen_mode == "t2v" and "text" not in modes:
            gen_mode = "i2v"
            notes.append(f"分镜{i + 1} 规划为 t2v 但当前模型不支持纯文生，已转为 i2v（请补充关键帧）")
        keyframe_prompt = sp["keyframe_prompt"]
        if gen_mode == "i2v" and not keyframe_prompt.strip():
            keyframe_prompt = sp["description"] or sp["prompt"][:200]
        sp["prompt"], n = scrub_d_tags(sp["prompt"])
        d_scrubbed += n
        scenes.append(Scene(
            id=new_scene_id(),
            title=(sp["title"] or f"分镜 {i + 1}")[:50],
            description=sp["description"][:500],
            prompt=sp["prompt"][:8000],
            keyframe_prompt=keyframe_prompt[:2000],
            narration=sp["narration"][:5000],
            duration=duration,
            gen_mode=gen_mode,
        ))
    if d_scrubbed and trace:
        trace.log("plan_d_tags_scrubbed", count=d_scrubbed)

    # 角色性别接地：剧本无性别依据的角色，模型在 prompt 里自行写出的性别词按句剔除
    # （句子里含该角色名才动，不误伤其他有依据的角色；外观由参考图锚定）
    src_text = str(project.screenplay.get("source_text") or "")
    if src_text and chars:
        src_lines = src_text.splitlines()
        ungrounded = [c["name"] for c in chars
                      if not _has_gender_basis("\n".join(ln for ln in src_lines if c["name"] in ln))]
        if ungrounded:
            for scene in scenes:
                scene.prompt = scrub_prompt_gender(scene.prompt, ungrounded)
            if trace:
                trace.log("plan_gender_scrubbed", names=ungrounded)

    # 原文不拼进 Prompt：对视频生成模型完全无用，且原文块里的中文台词行与 <d> 同罪——
    # 诱导模型自行合成第二路语音与 <Audio N> 叠放。原文保留在 screenplay.beats.source_text
    # （bind 台词对号、前端分镜抽屉单独窗口展示都用它），台词在 Prompt 里的唯一落点是
    # detailed_description 末尾的英文 Audio Timeline 小节。

    # 角色设定卡注入：无参考图锚定的镜，Prompt 末尾强制注入（跨镜一致性兜底）
    char_block = ""
    if chars:
        char_block = "\n\n## Character Reference（跨镜一致，外观逐字遵守）\n" + "\n".join(
            f"- {c['name']}：{c['design']}" for c in chars
        )
    if char_block:
        for i, sp in enumerate(all_shots):
            if sp.get("wants_images"):
                continue  # 挂参考图的镜外观以图为准，不注入文字设定卡
            if not any(c.get("design", "")[:20] in scenes[i].prompt for c in chars if c.get("design")):
                scenes[i].prompt = (scenes[i].prompt.rstrip() + char_block)[:8000]

    def fn(p: Project) -> None:
        p.scenes = [Scene(**s.model_dump()) for s in scenes]
        p.characters = [Character(name=c["name"], design=c.get("design", "")) for c in chars]
        # 分镜重规划：旧生成物对新剧本是脏数据，不迁移；素材池不动，绑定由 bind 阶段重建
        p.pending_bindings = []

    _store_mutate(project_id, fn)
    # wants 不落盘到 Scene（属 LLM 规划中间产物），存 screenplay 供 bind 阶段使用
    def fn2(p: Project) -> None:
        if p.screenplay:
            p.screenplay["wants"] = [
                {
                    "scene_id": scenes[i].id,
                    "wants_images": sp.get("wants_images") or [],
                    "wants_audios": sp.get("wants_audios") or [],
                    "wants_videos": sp.get("wants_videos") or [],
                }
                for i, sp in enumerate(all_shots)
            ]

    _store_mutate(project_id, fn2)
    return {"ok": True, "shots": len(scenes), "notes": notes[:10]}


# ---------------- bind（确定性优先 + LLM 兜底） ----------------


_REF_HEADER_RE = re.compile(r"^Reference[:：]?$", re.I)
_REF_END_RE = re.compile(r"^(Audio\s+Timeline|H3\s*Prompt|subject_definitions|summary|##)", re.I)


def _reference_tokens(frag: str) -> tuple[list[int], list[int], list[int]]:
    """从片段原文提取 Reference 声明里的 <Image N>/<Audio N>/<Video N> 编号（保持声明顺序）。

    优先解析独立 Reference 段（用户分镜稿的标准结构）；没有该段时退回全文 token
    首现顺序。这是"素材分配照抄原视频"的锚点——用户脚本编号 = 素材上传顺序。
    """
    lines = (frag or "").splitlines()
    img_ns: list[int] = []
    aud_ns: list[int] = []
    vid_ns: list[int] = []
    in_ref = False
    ref_seen = False
    for ln in lines:
        s = ln.strip()
        if _REF_HEADER_RE.match(s):
            in_ref, ref_seen = True, True
            continue
        if in_ref:
            if not s or _REF_END_RE.match(s):
                in_ref = False
                continue
            for kind, n in re.findall(r"<(Image|Audio|Video)\s+(\d+)>", s):
                {"Image": img_ns, "Audio": aud_ns, "Video": vid_ns}[kind].append(int(n))
    if not ref_seen:
        for kind, n in re.findall(r"<(Image|Audio|Video)\s+(\d+)>", frag or ""):
            {"Image": img_ns, "Audio": aud_ns, "Video": vid_ns}[kind].append(int(n))
    return list(dict.fromkeys(img_ns)), list(dict.fromkeys(aud_ns)), list(dict.fromkeys(vid_ns))


def _reference_table(source_text: str) -> dict[int, str]:
    """解析原稿全局参考图编号表：<Image N>｜身份说明 → {N: 说明}（首现优先，全局表在片段前）。

    身份说明截到下一个标签/反引号/星号——markdown 草稿整篇一行时表格被压成
    `…参考图 * `<Image 2>`：…`，不截断会把后续条目全吞进来。
    """
    table: dict[int, str] = {}
    for m in re.finditer(r"<Image\s+(\d+)\s*>\s*[｜|:：]?\s*([^\n]+)", source_text or ""):
        identity = re.split(r"`|／?\s\*\s|<Image\s|<Audio\s|<Video\s|<Subject\s", m.group(2))[0]
        identity = identity.strip().strip("：:|｜").strip()
        if identity:
            table.setdefault(int(m.group(1)), identity[:120])
    return table


def rewrite_aligned_prompt(prompt: str, image_ns: list[int], table: dict[int, str],
                           wants_mids: list[str] | None = None, bound_mids: list[str] | None = None,
                           global_mids: list[str] | None = None) -> str:
    """按实际绑定重写分镜 Prompt：原稿全局 <Image N> 统一改成本镜 <Picture k>
    （k = 绑定顺序），并重建参考图定义行——身份说明取自原稿编号表而不是模型猜测。
    UUID 文件名对模型完全不可读，靠它写的身份说明就是幻觉（复现事故：片段4 的
    演播室总结图被写成"远处新发小白斑参考"挂给了片段3）。

    wants_mids/bound_mids：plan 润色段里的 <Picture k> 是按 plan 自己的 wants 顺序编号的；
    bind 对齐覆盖绑定后顺序可能变了，这里把旧编号按"plan 想要的素材 → 最终绑定位置"
    重映射，被覆盖掉的素材降级为纯文本——三处编号（原文/润色段/定义行）全部一致。
    H3 六段式 prompt 的 <Picture k> 定义行写在 subject_definitions 段内（英文句式），
    旧结构 prompt 重建为中文声明行——两种格式都以绑定顺序为准。

    global_mids：全局上传顺序的素材 id（原稿 <Image N> 的 N 即此表第 N 项）。润色正文
    还会漏第三种编号——把原稿全局号直接抄进 <Picture N>（复现事故：镜2 只绑 3 张图，
    正文却写 anchored by <Picture 4>＝原稿的小墨，组装闸门按悬空标签永久拦截）。两轮
    重映射后仍超出本镜绑定数的 <Picture k> 按全局号对号：第 k 张图已绑定则改号到绑定
    位置，未绑定则降级纯文本。
    """
    # 已重写过的 Prompt（定义行带 original storyboard Image /（原稿 Image N）标记或
    # wants 降级文本）其 <Picture k> 已是绑定序号域——再按 wants 域解释会把 dualren 的
    # <Picture 2> 当成 wants 第 2 张误降级（重跑 bind 必坏，事故：每次重新绑定都破坏上次的修复）
    _rewritten = ("original storyboard image" in prompt.lower()
                  or "（原稿 image" in prompt.lower() or "of the original plan (not bound" in prompt)
    if wants_mids and bound_mids and not _rewritten:
        def _remap(m: re.Match) -> str:
            k = int(m.group(1))
            if 0 < k <= len(wants_mids):
                mid = wants_mids[k - 1]
                if mid in bound_mids:
                    return f"<Picture {bound_mids.index(mid) + 1}>"
                return f"reference image {k} of the original plan (not bound to this shot)"
            return m.group(0)

        prompt = re.sub(r"<Picture\s+(\d+)>", _remap, prompt)

    mapping = {n: k for k, n in enumerate(image_ns, start=1)}

    def _sub(m: re.Match) -> str:
        n = int(m.group(1))
        if n in mapping:
            return f"<Picture {mapping[n]}>"
        # 正文提到但 Reference 清单没绑定的全局图：不能留悬空标签（生成器会当成
        # 不存在的参考位），降级为纯文本指代
        return f"Image {n}"

    prompt = re.sub(r"<Image\s+(\d+)>", _sub, prompt)

    # 3) 残留悬空 <Picture k>（k 超出本镜绑定数）＝正文把原稿全局号抄进了标签。按
    #    全局上传顺序对号改号/降级。逐行处理且跳过定义行（^\s*<Picture k>）——定义行
    #    下面会被整体重建，降级成纯文本反而会让旧行逃过替换留在正文里
    if global_mids and bound_mids:

        def _sub_dangling(m: re.Match) -> str:
            k = int(m.group(1))
            if 0 < k <= len(bound_mids):
                return m.group(0)
            if 0 < k <= len(global_mids) and global_mids[k - 1] in bound_mids:
                return f"<Picture {bound_mids.index(global_mids[k - 1]) + 1}>"
            return f"original storyboard Image {k}"

        prompt = "\n".join(
            ln if re.match(r"^\s*<Picture\s+\d+\s*>", ln)
            else re.sub(r"<Picture\s+(\d+)>", _sub_dangling, ln)
            for ln in prompt.splitlines()
        )
    h3_style = "subject_definitions" in prompt.lower()
    decl_lines = []
    for k, n in enumerate(image_ns, start=1):
        identity = table.get(n) or "本镜参考图"
        if h3_style:
            decl_lines.append(
                f"<Picture {k}> is the reference for: {identity} (original storyboard Image {n}); "
                "it anchors this shot's composition and visual states.")
        else:
            # 尾注（原稿 Image N）是重写标记：重跑 bind 靠它识别"已是绑定序号域"跳过
            # wants 重映射，与 H3 行的 (original storyboard Image N) 同等效力
            decl_lines.append(f"<Picture {k}>：{identity}（原稿 Image {n}）")
    if not decl_lines:
        return prompt
    out: list[str] = []
    replaced = False
    for ln in prompt.splitlines():
        if re.match(r"^\s*<Picture\s+\d+\s*>", ln):
            if not replaced:
                out.extend(decl_lines)
                replaced = True
            continue
        out.append(ln)
    if replaced:
        return "\n".join(out)
    # 没有既有定义行可替换：H3 插到 subject_definitions: 段首之后，旧结构附在末尾
    if h3_style:
        for i, ln in enumerate(out):
            if "subject_definitions" in ln.lower():
                out[i + 1:i + 1] = decl_lines
                return "\n".join(out)
    return "\n".join(out).rstrip() + "\n" + "\n".join(decl_lines)


def _dedupe_materials(materials: list) -> tuple[list, list]:
    """素材池身份去重（整包重传是常态）：同 kind + 同语义文件名（剥哈希前缀）只留
    第一个；script/doc 按正文前缀判重。返回 (有效集, 重复集)。重复拷贝绝不进入
    任何分配路径——否则 bind 兜底会把它们"高置信"地再绑一遍，同镜挂双份同名图。"""
    keep, dups = [], []
    seen: set[tuple] = set()
    for m in materials:
        if m.kind in ("script", "doc"):
            key = (m.kind, material_match.norm_tts_text((m.text or "")[:4000]))
        else:
            key = (m.kind, material_match.semantic_file_key(m.filename or m.url or ""))
        if key[1] and key in seen:
            dups.append(m)
            continue
        seen.add(key)
        keep.append(m)
    return keep, dups


async def run_bind(project_id: str, envelope, trace=None) -> dict:
    project_dict = project_store.get(project_id)
    project = Project.model_validate(project_dict)
    if not project.scenes:
        raise FatalError("项目没有分镜，请先执行 plan 阶段")

    # 0) 身份去重：同一批素材整包重传（哈希前缀不同、内容相同）只算一份
    keep_mats, dup_mats = _dedupe_materials(project.materials)
    materials = {m.id: m for m in keep_mats}
    if dup_mats and trace:
        trace.log("bind_dup_materials_ignored", count=len(dup_mats),
                  detail=[m.filename for m in dup_mats][:10])

    # 0.5) storyboard 参考编号对齐：原稿 Reference 声明的 <Image N>/<Audio N> 按上传
    # 顺序确定性对号，覆盖 plan 阶段对不透明文件名（UUID 图）的语义猜测。
    # 这是对"素材分配必须照原视频"的硬保证——对齐结果就是该镜图片绑定的最终权威
    images_in_order = [m for m in keep_mats if m.kind == "image"]
    audios_in_order = [m for m in keep_mats if m.kind == "audio"]
    videos_in_order = [m for m in keep_mats if m.kind == "video"]
    beats = (project.screenplay or {}).get("beats") or []
    is_storyboard = (project.screenplay or {}).get("script_type") == "storyboard"
    aligned: dict[str, dict] = {}  # scene_id -> {"image_ns": [...], "image_mids": [...], "audio_mids": [...]}
    skipped_audio_align: list[tuple] = []
    audio_decl: dict[str, list[int]] = {}  # scene_id -> 片段原文 Reference 声明的全局音频号（声明顺序）
    if is_storyboard:
        ref_table = _reference_table((project.screenplay or {}).get("source_text") or "")
        for i, scene in enumerate(project.scenes):
            beat = beats[i] if i < len(beats) else {}
            frag = beat.get("source_text") or " ".join(
                filter(None, [beat.get("title"), beat.get("description"), beat.get("narration")]))
            img_ns, aud_ns, vid_ns = _reference_tokens(frag)
            audio_decl[scene.id] = aud_ns
            img_mids = [images_in_order[n - 1].id for n in img_ns if 0 < n <= len(images_in_order)]
            aud_mids = []
            aud_pairs = []  # (原稿全局号, 素材id)：Prompt 音频编号重编为本镜号的映射依据
            for n in aud_ns:
                if not (0 < n <= len(audios_in_order)):
                    continue
                mat = audios_in_order[n - 1]
                semantic = material_match.audio_semantic(mat.filename or mat.url or "")
                hay = material_match.norm_tts_text(f"{scene.prompt}{scene.narration}{frag}")
                # 上传顺序与台词内容双信号互证：文件名台词不在该镜文本里说明顺序不可信
                # （复现现场：台词01的音频排在最后上传），放弃对齐交给台词对号兜底
                if semantic and semantic not in hay:
                    skipped_audio_align.append((scene.title, n, mat.filename))
                    continue
                aud_mids.append(mat.id)
                aud_pairs.append((n, mat.id))
            vid_ns_kept = [n for n in vid_ns if 0 < n <= len(videos_in_order)]
            vid_mids = [videos_in_order[n - 1].id for n in vid_ns_kept]
            if img_mids or aud_mids or vid_mids:
                aligned[scene.id] = {
                    "image_ns": [n for n in img_ns if 0 < n <= len(images_in_order)],
                    "image_mids": img_mids, "audio_mids": aud_mids, "audio_pairs": aud_pairs,
                    "video_mids": vid_mids, "video_pairs": list(zip(vid_ns_kept, vid_mids)),
                }
        if aligned and trace:
            trace.log("bind_reference_aligned", scenes=len(aligned),
                      detail={sc.title: aligned[sc.id]["image_ns"] for sc in project.scenes if sc.id in aligned})
        if skipped_audio_align and trace:
            trace.log("bind_audio_align_crosscheck_failed", count=len(skipped_audio_align),
                      detail=[f"{t}:Audio{n}≠{f[:30]}" for t, n, f in skipped_audio_align[:8]])

    wants_map = {w["scene_id"]: w for w in ((project.screenplay or {}).get("wants") or [])}
    decisions: list[tuple[str, str, str, str]] = []  # (scene_id, kind, material_id, role)
    pending: list[dict] = []
    claimed: set[str] = set()
    dropped_wants: list[dict] = []  # plan 引用了不存在/类型不符/重复的素材：留痕供排查

    for scene_id, a in aligned.items():
        for mid in a["image_mids"] + a["video_mids"]:
            decisions.append((scene_id, "image" if mid in a["image_mids"] else "video", mid, "reference"))
            claimed.add(mid)
        for mid in a["audio_mids"]:
            decisions.append((scene_id, "audio", mid, "reference"))
            claimed.add(mid)

    # 1) wants 里的 material_id 精确引用；幻觉 id 不生效（素材保持未认领，交给兜底），
    #    但绝不静默——记入 dropped_wants，方便排查"模型以为引用了实际没挂上"。
    #    已按原稿对齐的镜跳过对应 wants：对齐是权威，plan 的猜测作废
    for scene in project.scenes:
        a = aligned.get(scene.id) or {}
        w = wants_map.get(scene.id) or {}
        want_images = [] if a.get("image_mids") else (w.get("wants_images") or [])
        want_audios = [] if a.get("audio_mids") else (w.get("wants_audios") or [])
        for want in want_images:
            mid = want.get("material_id")
            if mid and mid in materials:
                # role 恒为 reference：LLM 无权定首帧（首帧仅首尾帧模式由用户手动绑定）
                decisions.append((scene.id, "image", mid, "reference"))
                claimed.add(mid)
            elif mid:
                dropped_wants.append({"scene": scene.title, "kind": "image", "material_id": mid,
                                      "note": "素材不存在或重复拷贝已折叠"})
        for want in want_audios:
            mid = want.get("material_id")
            if mid and mid in materials and materials[mid].kind == "audio":
                decisions.append((scene.id, "audio", mid, "reference"))
                claimed.add(mid)
            elif mid:
                dropped_wants.append({
                    "scene": scene.title, "kind": "audio", "material_id": mid,
                    "note": "素材不存在" if mid not in materials else "素材类型不是音频",
                })
        for want in (w.get("wants_videos") or []):
            mid = want.get("material_id")
            if mid and mid in materials:
                decisions.append((scene.id, "video", mid, "reference"))
                claimed.add(mid)
            elif mid:
                dropped_wants.append({"scene": scene.title, "kind": "video", "material_id": mid})
    if dropped_wants and trace:
        trace.log("bind_want_dropped", count=len(dropped_wants),
                  detail=[d["material_id"] for d in dropped_wants][:10])

    # 2) 未被引用的上传音频：确定性台词对号分发（同台词的重复拷贝不再进 pending——
    #    它们不是"没命中"，是"已经以另一份拷贝挂上了"）
    def _audio_text(mid_or_mat) -> str:
        m = materials.get(mid_or_mat) if isinstance(mid_or_mat, str) else mid_or_mat
        return material_match.norm_tts_text(material_match.audio_semantic((m.filename or m.url or ""))) if m else ""

    claimed_texts = {t for t in (_audio_text(mid) for mid in claimed if mid in materials) if t}
    free_audios = [m for m in keep_mats
                   if m.kind == "audio" and m.url and m.id not in claimed and _audio_text(m) not in claimed_texts]
    dup_audio_skipped = [m for m in keep_mats
                         if m.kind == "audio" and m.url and m.id not in claimed and _audio_text(m) in claimed_texts]
    if dup_audio_skipped and trace:
        trace.log("bind_dup_audios_skipped", count=len(dup_audio_skipped),
                  detail=[m.filename for m in dup_audio_skipped][:10])
    if free_audios:
        scene_texts = [(s.prompt, s.narration) for s in project.scenes]
        # 语义载体用 filename 而非 url：旧格式配音（uuid 命名）登记时已合成台词文件名，
        # 新格式两者同源；distribute_audios 按它提台词对号，回查素材也按同一键
        keys = [(m.filename or m.url) for m in free_audios]
        claims, unmatched, overflow = material_match.distribute_audios(keys, scene_texts)
        key_to_mat = {(m.filename or m.url): m for m in keep_mats}
        for idx, ks in claims.items():
            for u in ks:
                decisions.append((project.scenes[idx].id, "audio", key_to_mat[u].id, "reference"))
                claimed.add(key_to_mat[u].id)
        for u in unmatched:
            pending.append({
                "material_id": key_to_mat[u].id,
                "note": f"音频「{u.rsplit('/', 1)[-1]}」的台词没有命中任何分镜",
                "suggested_scene_id": None,
            })

    # 3) 未绑定的图/视频素材 → LLM 兜底（带置信度）；同镜不重复挂同身份的图。
    #    storyboard 已按原稿对齐时不再兜底强塞：原稿没引用的素材就是用户明确不用的
    #    （"用不上的素材绝不引用"），交给 pending/用户而不是替用户决定
    storyboard_aligned = is_storyboard and bool(aligned)
    free_others = [m for m in keep_mats
                   if m.kind in ("image", "video") and m.url and m.id not in claimed
                   and not storyboard_aligned]
    if storyboard_aligned:
        unused = [m for m in keep_mats if m.kind in ("image", "video") and m.url and m.id not in claimed]
        if unused and trace:
            trace.log("bind_unused_by_script", count=len(unused), detail=[m.filename for m in unused][:10])
    if free_others and len(project.scenes) > 0:
        manifest = "\n".join(
            f"- material_id={m.id} 类型={m.kind} {m.filename}" for m in free_others
        )
        scenes_desc = "\n".join(
            f"- scene_index={i} {s.title}：{(s.description or s.prompt)[:80]}｜旁白：{(s.narration or '')[:40]}"
            for i, s in enumerate(project.scenes)
        )
        from .schemas import BindDecisionList

        result = await envelope.structured_call(
            BIND_SYSTEM,
            f"未分配素材：\n{manifest}\n\n分镜列表：\n{scenes_desc}\n\n把每个素材分配到最合适的分镜（一个素材可多次出现）。",
            BindDecisionList,
            max_tokens=LLM_MAX_OUTPUT_TOKENS,
        )
        scene_img_tokens: dict[str, set[str]] = {}
        for scene_id2, kind, mid, _role in decisions:
            if kind == "image" and mid in materials:
                scene_img_tokens.setdefault(scene_id2, set()).update(
                    material_match.asset_name_tokens(materials[mid].filename or materials[mid].url or ""))
        for d in result.decisions:
            if d.material_id not in materials or not (0 <= d.scene_index < len(project.scenes)):
                continue
            if d.material_id in claimed and d.kind == "audio":
                continue
            if d.confidence >= BIND_CONFIDENCE_THRESHOLD:
                if d.kind == "image":
                    toks = set(material_match.asset_name_tokens(
                        materials[d.material_id].filename or materials[d.material_id].url or ""))
                    if toks and toks & scene_img_tokens.get(project.scenes[d.scene_index].id, set()):
                        if trace:
                            trace.log("bind_fallback_dup_identity_skipped", material=d.material_id,
                                      scene=project.scenes[d.scene_index].title)
                        continue
                    scene_img_tokens.setdefault(project.scenes[d.scene_index].id, set()).update(toks)
                # 同上：兜底分配的图片 role 恒为 reference，忽略 LLM 返回的首帧标注
                decisions.append((project.scenes[d.scene_index].id, d.kind, d.material_id, "reference"))
                claimed.add(d.material_id)
            elif d.confidence >= 0.3:
                pending.append({
                    "material_id": d.material_id,
                    "note": f"低置信（{d.confidence}）：{d.reason[:80]}",
                    "suggested_scene_id": project.scenes[d.scene_index].id,
                })

    # 落盘绑定（尊重契约上限：音频每镜≤3、图每镜≤9，超限的丢弃并记录）
    duration_adjusted: list[dict] = []  # 台词总时长超镜长 → 自动加长的记录
    duration_overflow: list[dict] = []  # 模型上限也装不下的（留给视频提交闸门拦截）

    def fn(p: Project) -> None:
        audio_count: dict[str, int] = {}
        image_count: dict[str, int] = {}
        for scene in p.scenes:
            scene.binding = type(scene.binding)()
        for scene_id, kind, mid, role in decisions:
            scene = next((s for s in p.scenes if s.id == scene_id), None)
            if not scene or mid not in {m.id for m in p.materials}:
                continue
            if kind == "image":
                if image_count.get(scene_id, 0) >= 9:
                    continue
                scene.binding.images.append(ImageBinding(material_id=mid, role=role))
                image_count[scene_id] = image_count.get(scene_id, 0) + 1
            elif kind == "audio":
                if audio_count.get(scene_id, 0) >= 3:
                    continue
                scene.binding.audios.append(mid)
                audio_count[scene_id] = audio_count.get(scene_id, 0) + 1
            else:
                scene.binding.videos.append(mid)
        # 去重保序
        for scene in p.scenes:
            seen: set[tuple] = set()
            scene.binding.images = [x for x in scene.binding.images if (x.material_id, x.role) not in seen and not seen.add((x.material_id, x.role))]
            scene.binding.audios = list(dict.fromkeys(scene.binding.audios))
            scene.binding.videos = list(dict.fromkeys(scene.binding.videos))
        # 台词时长闸门：绑定台词音频总时长 > 分镜时长 → 自动加长（放不下会被视频模型
        # 压缩变形后烧进音轨）。时长未知的音频不计入；视频提交侧还有探测兜底
        mats_by_id = {m.id: m for m in p.materials}
        for scene in p.scenes:
            total = sum(mats_by_id[mid].duration or 0 for mid in scene.binding.audios if mid in mats_by_id)
            if not total:
                continue
            new_dur, note, err = generation_service.fit_duration_to_audios(
                total, scene.duration, p.settings.video_model)
            if err:
                duration_overflow.append({"scene": scene.title, "error": err})
            elif note:
                duration_adjusted.append({"scene": scene.title, "old": scene.duration, "new": new_dur})
                if scene.prompt:
                    # 校准同步缩放 Prompt 时间引用（含原文块）：残留旧时长会与
                    # duration 参数自相矛盾，"以本节为准"的原文块尤其如此
                    scene.prompt, _ = prompt_timing.rescale_prompt_timing(scene.prompt, scene.duration, new_dur)
                    scene.revision += 1
                scene.duration = new_dur
        p.pending_bindings = [PendingBinding(**pb) for pb in pending]

    _store_mutate(project_id, fn)

    # 4) 对齐镜的 Prompt 声明行确定性重写：<Image N>→<Picture k>（k=绑定顺序），
    #    身份说明取自原稿参考图编号表——生成提交的参考图顺序与声明从此严格一致。
    #    音频/视频同理：原文块沿用全集全局编号（<Audio 3>=全片第 3 段台词），正文却是
    #    本镜编号——两域并存时组装闸门数出 N 个标签配 M 段音频直接拦截，这里统一改号
    if aligned:
        ref_table = _reference_table((project.screenplay or {}).get("source_text") or "")
        all_wants = {w["scene_id"]: w for w in ((project.screenplay or {}).get("wants") or [])}
        renumber_log: list[dict] = []

        def fn3(p: Project) -> None:
            for scene in p.scenes:
                a = aligned.get(scene.id) or {}
                if a.get("image_mids"):
                    bound_order = [x.material_id for x in scene.binding.images]
                    ns = [n for n, mid in zip(a["image_ns"], a["image_mids"]) if mid in bound_order]
                    w = all_wants.get(scene.id) or {}
                    wants_mids = [x.get("material_id") for x in (w.get("wants_images") or [])]
                    scene.prompt = rewrite_aligned_prompt(
                        scene.prompt, ns, ref_table, wants_mids=wants_mids, bound_mids=bound_order,
                        global_mids=[m.id for m in images_in_order])[:8000]
                    scene.revision += 1
                # 音频编号两域统一：原文块/片段声明的全局号 → 本镜绑定序号。上传顺序
                # 对齐（audio_pairs，全局号↔素材id）是权威；对齐没吃上的（互证失败走
                # 台词兜底绑定）按声明顺序位置配对兜底——绑定顺序即台词时间顺序
                bound_audios = list(scene.binding.audios)
                audio_map = {n: i + 1 for i, n in enumerate(audio_decl.get(scene.id) or [])
                             if i < len(bound_audios)}
                for n, mid in a.get("audio_pairs") or []:
                    if mid in bound_audios:
                        audio_map[n] = bound_audios.index(mid) + 1
                bound_videos = list(scene.binding.videos)
                video_map = {n: bound_videos.index(mid) + 1
                             for n, mid in a.get("video_pairs") or [] if mid in bound_videos}
                if audio_map or video_map:
                    scene.prompt, note = material_match.remap_ref_numbers(scene.prompt, audio_map, video_map)
                    scene.prompt = scene.prompt[:8000]
                    if note:
                        renumber_log.append({"scene": scene.title, "renumbered": note})
                        scene.revision += 1

        _store_mutate(project_id, fn3)
        if renumber_log and trace:
            trace.log("bind_ref_numbers_renumbered", count=len(renumber_log), detail=renumber_log[:8])
    if duration_adjusted and trace:
        trace.log("bind_duration_fitted", count=len(duration_adjusted), detail=duration_adjusted[:8])
    if duration_overflow and trace:
        trace.log("bind_duration_overflow", count=len(duration_overflow),
                  detail=[d["error"][:120] for d in duration_overflow[:8]])
    if trace:
        trace.log("stage_done", stage="bind", decisions=len(decisions), pending=len(pending),
                  dups_ignored=len(dup_mats), aligned_scenes=len(aligned))
    result = {"ok": True, "bound": len(decisions), "pending": len(pending),
              "ignored_duplicates": len(dup_mats)}
    if aligned:
        result["reference_aligned_scenes"] = len(aligned)
    if duration_adjusted:
        result["duration_adjusted"] = duration_adjusted
    if duration_overflow:
        result["duration_overflow"] = duration_overflow
    if pending:
        result["pending_bindings"] = pending
    if dropped_wants:
        result["dropped_wants"] = dropped_wants[:20]
    return result
