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

from ...config import settings
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
# 剧本正文上限：与上传预览（api/files.py）同一常量（见 config/settings.py 注释）
MAX_SCRIPT_CHARS = settings.MAX_SCRIPT_CHARS
PLAN_BATCHHint = 8  # 提示模型单批输出规模上限
PLAN_MAX_BATCHES = 25  # 200 镜安全上限
PLAN_GUARD_RETRIES = 2  # storyboard 保真闸门：镜数≠拍数时的纠错重规划次数
PLAN_WINDOW_SHOTS = 4  # storyboard 分窗：单窗拍数上限（思考+正文共用 max_tokens，窗大正文必空）
PLAN_WINDOW_CHARS = 4000  # storyboard 分窗：单窗原文累计字数上限
# plan 输出预算必须连推理过程一起覆盖：思考型 LLM 的推理与正文共用 max_tokens，
# 预算过小会被思考烧光（finish_reason=length、正文为空，2026-09-21 三连空事故）；
# 窗小 + 预算大双保险。当前规划的 LLM 取自用户设置（设置→API Keys / 顶栏 LLM）
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
  4) 润色是增强、不是稀释（单调性铁律）：原文已是高质量英文成稿时，保留其全部细节、
     结构与工艺块照搬进润色稿，只做机械适配（标签改号/时长对齐）与查漏补全；
     压缩篇幅、删减时段、抹平镜头/光线/特效细节都是负优化，一律禁止
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
# 写作标准对齐官方指南原文：detailed_description 350-500 英文词、逐时段编排、风格开篇，
# 外加工艺四块与本镜专属否定清单——"平译原文"不达标，plan 质量闸门会带反馈打回重写。
PLAN_SYSTEM_H3 = """【Prompt 结构规范：H3 Ref2VA 六段式（段名逐字使用，六段缺一不可，无配乐时 non_diegetic_music 写 N/A）】

subject_definitions: 定义本镜全部引用内容与标签，每项一行：
- <Subject N>：可复用的可见内容单元（角色/场景/服装/道具）。跨镜出现的主要角色必须给
  <Subject> 标签，外观来源引用对应锚定图，例：
  "<Subject 1> is the doctor 吕满春 in <Picture 1>, wearing a clean white lab coat."
  定义只写剧本/原文明确写出的属性：性别、年龄、五官等原文没有的一律不写（交给锚定图）——
  示例句与金样例里的外观词同样不得照抄进无依据的角色
  只用于定义角色/风格、不作画面锚点的图不单列 <Picture> 行，写进对应 <Subject> 定义里
- <Picture N>：作为本镜画面/构图/状态锚点的参考图，第 N 张 = 本镜 wants_images 第 N 项，
  一行说明它锚定什么，例："<Picture 3> is the storyboard keyframe anchoring the studio
  composition and the vitiligo patch states for this shot."
- <Audio N>：本镜绑定的音频，第 N 段 = 本镜 wants_audios 第 N 项；对应说话人的写
  "<Audio 1> is the spoken line of <Subject 1> (S1)"（编号按本镜顺序从 1 起；
  只写是谁的话，严禁在这里引用台词原文）
- 出镜即锚定：会在本镜画面里出现的角色，其外观锚定图必须挂进 wants_images；
  没挂参考图的角色绝不入镜（只能以画外音存在）。定义行不得指向本次请求不会传入的图。
  detailed_description 不得出现未定义/未锚定角色名的任何可见身体描述（画外角色
  只能以视线方向存在，不得把其肩/手臂/剪影写进画面）。
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
未绑定进本镜请求的素材绝不写 fully_preserved（它根本不会传入）。

detailed_description: 主体段落（对齐官方 Ref2VA 指南：350-500 英文词；台词密集时优先
装下完整语音时间线，也不得注水凑词）：
- 英文书写；画面内可见文字保留原语言；台词/歌词原文只出现在 Audio Timeline 小节
- 风格开篇：[Shot 1] 之前用一两句英文定基调（题材/美术风格/光线质感），例：
  "The target clip is a high-quality 3D educational short set in a warm, stylized
  microscopic skin world with soft depth."
- 自包含铁律：每镜是一条独立生成的成片，开篇直接交代起始画面状态（机位/主体/环境）；
  严禁 "Continuing from the previous shot" / "延续上一镜" 这类引用不存在画面的写法
- 逐时段编排：按本镜时间轴分段推进（段界与 Audio Timeline 对齐，如 0.0-2.5s /
  2.5-5.0s / …），每段写清四件事——说话动作（delivers the spoken line / lip
  synchronization 句）必须落在台词窗起点所在的时段里：台词窗从 0.0s 起时，首个含
  说话动作的时段也必须从 0.0s 起；原文动作行的时间与 Audio Timeline 冲突时，以
  Audio Timeline 为准重排段界——严禁先铺完手势/铺垫再开口（会把台词整段推后，
  说话段里还在动别的角色时台词更会跟错人）
  ① 镜头：机位与运动（类型+幅度+速度+加减速），例 "the camera pushes in with small
    amplitude at slow speed toward the observation patch"
  ② 主体：动作与状态变化（物理可信、前后连续）。动作句主语必须是 <Subject N> 本身，
    严禁 "the hand closest to X … his knee" 这类无主悬空写法——动作会被视频模型
    错误归属到别的角色（尤其手势/点头等动作叠加在非说话人身上时，台词会跟错人）
  ③ 画面：焦点/景深/光线落点，例 "rack focus from the lens rim onto the sparse
    pigment points beneath the lens"
  ④ 段尾：该段结束时画面停在哪、与下一段如何衔接
- 各时段首尾相接、末段终点等于该镜 duration；严禁写超出总时长的动作或时刻
- 镜头运动写成自然英文动作句（类型+幅度+速度）：Zoom In / Push In / Pull Out / Pan Left / Pan Right /
  Truck Left / Truck Right / Tilt Up / Tilt Down / Pedestal Up / Arc Shot / Tracking Shot / Static Shot
- 连续运镜纪律：单镜内平滑加减速、屏幕方向一致；无原文依据不得突然切镜、甩镜、镜头
  抖动、极速移动、过量运动模糊（原文明确要求的镜头设计优先保留）
- 说话人用稳定 ID：首次出现给身份描述并记 (S1)，后续复用不重编；台词文字严禁写进正文叙述
  ——<d> 标签、引号台词都不写：正文出现台词会让模型自行合成第二路语音，与 <Audio N>
  参考音频在成片里叠放成"多个声音同时说话"；正文只写说话动作与音频引用，例：
  <Subject 1> (S1) delivers the spoken line of <Audio 1> with lips synchronized；
  画外音用 "says in an off-screen voiceover" 并注明画面人物嘴唇不动
- 首次出现的重要 <Subject N> 描述外观、画幅内位置与当前动作；画面可见文字加英文双引号
- 原文的否定约束必须保留为英文否定句，例："The edge never gets a black outline; the patch
  does not grow or shrink." / "No subtitles, no on-screen text."
- 工艺四块（detailed_description 正文之后、Audio Timeline 之前，块名逐字使用，各 1-3 句英文）：
  cinematography: 连续运镜如何衔接与收束（"One continuous move; smooth acceleration and
    deceleration at every transition; the camera eases to a complete stop at the end"）、
    屏幕方向一致性、前景遮挡/选择性焦点如何制造纵深
  lighting: 主光方向与质感、补光/逆光的作用、体积光的克制使用；需要对比的区域用
    可见结构差异表达，不靠一侧提亮
  visual_effects: 只写物理可信的效果（折射与边缘色散/接触阴影/次表面散射/轻微空气
    尘埃等按本镜取用）；无原文依据绝不添加发光、生长、全息、扫描环、界面图形
  visual_constraints: 本镜专属否定清单，逐条一行（从本镜风险推导）：No subtitles or
    on-screen text / no lip-sync when the audio is an off-screen voiceover / no characters
    beyond the anchored <Subject N> / no portal, scanning ring or extreme-speed transition
    ——按本镜实际裁剪，与原文否定约束合并，不得冲突
- Audio Timeline 小节（detailed_description 末尾，台词唯一出现处），每段一行：
  `0.0-3.4s | <Audio 1> | <Subject 1> (S1): "台词原文"`——秒数用英文小数与连字符，
  说话人写 <Subject N> 与 (S<编号>)，台词原文加英文双引号逐字保留原语言、不得增删改写；
  多段首尾相接、末段终点等于该镜 duration；本镜无绑定音频则不写此小节

detailed_description 写作范式（10s 单镜节选，展示结构与密度；"…"为节略标记，正式输出
按本镜内容同密度写满。范式仅示范写作密度与时段结构——机位、景别与前景遮挡元素
不得从中照搬：机位只能来自该镜原文，storyboard 锁定首帧的镜尤其如此，原文没写的
越肩/前景遮挡一律严禁发明）：
The target clip is a high-quality 3D educational short set in a warm, stylized microscopic
skin world with soft depth and gentle atmosphere.
[Shot 1] 0.0-2.5s | Over-shoulder push-in and optical reveal: the camera starts close
behind <Subject 1>'s shoulder, slightly above his eye level, and pushes in with small
amplitude at slow speed toward the magnifying glass as he tilts it over the pale patch;
focus racks from the lens rim onto the sparse brown pigment points beneath the glass,
with physically plausible magnification, subtle edge refraction and a restrained glint
sliding across the lens. <Subject 1> leans forward slightly to inspect the revealed
details, his mouth closed. 2.5-5.0s | Low tracking move: without cutting, the camera
glides down past the side of the lens into a skin-level tracking position and trucks
alongside <Subject 1> at matched speed while he takes several short steps forward; a
nearby follicle passes through the foreground, softening briefly out of focus and
creating natural parallax, and a longer pale stretch with far fewer follicles opens up
beyond it. …（5.0-8.0s / 8.0-10.0s 同密度推进，末段镜头 ease 到完全静止，留下稳定的
收尾构图）…
cinematography: One continuous, carefully choreographed move; smooth acceleration and
deceleration at every transition; consistent screen direction; foreground occlusion and
selective focus build depth; no cuts, no whip pans, no extreme-speed movement.
lighting: A soft warm key light enters diagonally from the upper left; a restrained cool
fill preserves shadow detail; a delicate backlight separates <Subject 1> from the
environment; both skin regions follow the same lighting logic.
visual_effects: Physically plausible magnifier refraction, soft contact shadows and
subtle subsurface scattering; no glowing pigment, no growing follicles, no interface
graphics.
visual_constraints: No subtitles or on-screen text; no lip-sync (the line is an off-screen
voiceover and <Subject 1>'s lips remain closed); no characters beyond the anchored
subjects; no portal, scanning ring or extreme-speed transition.
Audio Timeline:
0.0-8.0s | <Audio 1> | <Subject 2> (S1): "台词原文照抄"
8.0-10.0s | No dialogue. Gentle wide reveal and silent observation.

overall_soundscape: 1-4 句英文概括环境音与物理声（对白不在此重复）；如 "Only the reference
audio and a faint virtual-studio room tone; no extra dialogue."
non_diegetic_music: 1-3 句英文描述观众可听、角色听不到的配乐（乐器/速度/动态）；无配乐写 N/A。

禁止：Action Timeline 分段表、中文段名、自创段落、剧情梗概式省略、跨镜引用、
detailed_description 低于 350 英文词（台词密集除外）、时间轴与 duration 不符、悬空标签。
"""

# director 润色遍：plan 草稿结构正确但文笔平（"平译原文"）；每镜一次专注调用推到
# GPT 级写作——同构于"拿单镜+原文喂 GPT"，专注预算全给一镜，不在多镜批任务里分摊。
POLISH_SYSTEM = """你是分镜 Prompt 润色导演（director pass）。输入是一镜的草稿 Prompt、该镜原文（或节拍）
与角色设定；你的唯一任务：把草稿润色到一线分镜导演水准（达到并力争超过 GPT 级写作）。
输出 ONLY 一个 JSON 对象：{"prompt": "<润色后的完整 H3 Ref2VA 六段式 Prompt>"}。

六段契约与全部写作标准如下，逐条执行：
""" + PLAN_SYSTEM_H3 + """

润色专则（优先级：原文事实 > 草稿已正确内容 > 写作标准）：
1. 单调性铁律：草稿已正确的参考标签（<Subject N>/<Picture N>/<Audio N> 及其编号）、
   Audio Timeline（台词原文逐字、时间边界）、否定约束、原文画面情节一律保留；
   润色 = 补密度、补工艺、补细节，不是重述情节、不是换标签
2. 原文已是高质量英文成稿：照搬其全部细节、结构与工艺块进输出，只做机械适配
   （时长对齐、标签核验）与查漏补全——输出质量不得低于原文
3. 草稿薄（平译/梗概式）：按规范逐时段重写，把原文全部画面信息铺满时间轴，
   每段给足镜头/主体/焦点光线/段尾四要素，工艺四块一个不少
4. 机位与出场保真（storyboard 逐镜对录/原文声明锁定首帧或"只出现"约束时逐字执行）：
   机位、景别、前景遮挡元素只能来自原文与首帧，严禁发明 over-shoulder、
   behind …'s shoulder 等原文没有的构图（金样例的机位只是写作密度示范，不是构图许可）；
   原文声明画外/只出现某些角色的，其身体任何部位绝不入镜（视线方向可以）；
   没挂锚定图的角色名不得出现在 detailed_description
5. 严禁输出 JSON 以外的任何文字
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
                id=new_material_id(), kind="script", text=script_text[:MAX_SCRIPT_CHARS],
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
                        m.text = Path(local).read_text(encoding="utf-8", errors="replace")[:MAX_SCRIPT_CHARS]
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


# normalize 分块喂给 LLM 时的提示词包装头，块边界拍没有自己的镜头头时模型会把它
# 抄进转述开头。原稿里不存在这串字，不剥掉则精确锚定必失败 → 整拍 llm_echo →
# 成稿直录全稿失守（2026-10-04 聊皮肤13集：26 拍仅第 20 拍被污染，13 窗慢路径）
_CHUNK_HEAD_RE = re.compile(r"^(?:【剧本片段\s*\d+\s*/\s*\d+】[ \t]*\r?\n?)+")


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
        raw = b.get("source_text")
        anchor_text = _CHUNK_HEAD_RE.sub("", raw, count=1) if raw else ""
        if raw and anchor_text != raw:
            b["source_text"] = anchor_text
        for anchor in (anchor_text, f"{b.get('title') or ''}{b.get('description') or ''}"):
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


_H1_SECTION_LINE_RE = re.compile(r"^(?:#[ \t]*[^#\r\n]|[一二三四五六七八九十百零]+\s*、\s*\S)", re.M)


def trim_trailing_sections(beats: list[dict]) -> list[dict]:
    """末拍切回稿尾全局章节之前：没有后续片段头时，末拍切片会一路吃到文件尾，
    把"六、节奏结构 / 七、视觉风格关键词"这类全稿设定吞成最后一镜的原文
    （它们是全局设定，不是该镜的拍摄内容）。在末拍正文里（跳过首行编号头）
    找第一个 h1 段落标题（markdown # 或中文序号"七、"），把切片收回到它之前；
    切完剩余过短视为误匹配，不切。
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


_SHOT_NO_RE = re.compile(r"(?:分镜|片段|镜头)\s*0*(\d+)([A-Za-z]\d?)?")


def _beat_shot_no(beat: dict) -> tuple[int, str] | None:
    """拍的身份编号：从标题或原文首行（片段头）取「分镜12A/片段5」式编号。"""
    head_parts = [str(beat.get("title") or "")]
    st = beat.get("source_text") or ""
    if st:
        head_parts.append(st.splitlines()[0][:120])
    m = _SHOT_NO_RE.search(" ".join(p for p in head_parts if p))
    if not m:
        return None
    return int(m.group(1)), (m.group(2) or "").upper()


def dedupe_storyboard_beats(beats: list[dict]) -> tuple[list[dict], list[dict]]:
    """storyboard 拍数守恒闸门：同编号多拍只留一拍（verbatim 优先，原文最长者胜）。

    事故（闯西游第四回）：分镜头格式不识 → LLM 分块把素材索引「Image 05｜分镜01｜…」
    数成 14 拍、正文又数 14 拍、分块包装头【剧本片段 5/5】再漏 1 拍，14 拍翻倍成 29，
    13 条配音每条绑两遍。同号即同镜——索引拍/包装拍与正文拍同号时剔除冗余。
    """
    groups: dict[tuple[int, str], list[int]] = {}
    order: list[tuple[int, str]] = []
    for i, b in enumerate(beats):
        key = _beat_shot_no(b)
        if key is None:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    drop: set[int] = set()
    for key in order:
        idxs = groups[key]
        if len(idxs) < 2:
            continue
        best = max(idxs, key=lambda i: (str(beats[i].get("source_fidelity")) == "verbatim",
                                        len(beats[i].get("source_text") or "")))
        drop.update(i for i in idxs if i != best)
    if not drop:
        return beats, []
    dropped = [beats[i] for i in sorted(drop)]
    return [b for i, b in enumerate(beats) if i not in drop], dropped


# 认「片段 N｜标题」「分镜 N｜标题」「镜头 N｜标题」及裸编号行「分镜 N」（行尾锚定，
# 散文里"分镜 2 的…"不在行首裸编号位不会误触）；【第N镜】与【分镜N｜标题】括号式
# （闯西游第四回：正文头全是【分镜01｜拦路与坦白】，不识就走 LLM 分块把素材索引也
# 数成分镜，14 拍翻倍成 29）。「镜头N｜标题」（闯西游第六回：## 镜头01｜古道轻谈）
# 必须带标题分隔符——裸「镜头 N」行不认：稿尾风险清单里就有"### 镜头05"这类
# 小节头，认了会把检查清单切成幻影拍。编号带字母后缀（12A/12B/12C）。
# 裸"片段N：7s"这类时长行由标题闸排除。
_FRAGMENT_HEADER_LINE_RE = re.compile(
    r"^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜|镜头)\s*\d+(?:[A-Za-z]\d?)?\s*[｜|:：]\s*[^\n]+"
    r"|^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜)\s*\d+(?:[A-Za-z]\d?)?\s*$"
    r"|^(?:#{1,4}\s*)?【第[一二三四五六七八九十百零\d]+\s*镜】[^\n]*"
    r"|^(?:#{1,4}\s*)?【(?:片段|分镜|镜头)\s*\d+(?:[A-Za-z]\d?)?\s*(?:[｜|:：][^】\n]*)?】[^\n]*",
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
            r"^(?:#{1,4}\s*)?(?:H3\s*)?(?:片段|分镜|镜头)\s*\d+(?:[A-Za-z]\d?)?\s*(?:[｜|:：]\s*)?"
            r"|^(?:#{1,4}\s*)?【第[一二三四五六七八九十百零\d]+\s*镜】\s*"
            r"|^(?:#{1,4}\s*)?【(?:片段|分镜|镜头)\s*\d+(?:[A-Za-z]\d?)?\s*(?:[｜|:：]\s*)?"
            r"|】\s*$",
            "", line).strip()
        if not title:
            # 裸编号头（"# 分镜 1"）：编号行本身当标题——"分镜/片段"两个汉字可过下方汉字闸
            title = re.sub(r"^#{1,4}\s*", "", line).strip()
        if not _CJK_RUN_RE.search(title) or re.fullmatch(r"[\d\s秒Ss.：:]+", title):
            continue
        headers.append((m.start(), title[:50]))
    return headers


# 裸段名独占行（原投稿格式）；**加粗段名**+正文同行（markdown 六段式成稿，
# 2026-10-04 聊皮肤13集：10 镜成稿 0 命中 → LLM 分块把 10 镜拆成 26 拍）
_H3_BOUNDARY_RE = re.compile(
    r"^(?:(?:#{1,4}\s*)?subject_definitions\s*:\s*$"
    r"|(?:#{1,4}\s*)?\*\*\s*subject_definitions\s*:\s*\*\*)",
    re.I | re.M)


def _expand_shot_header_upward(source: str, pos: int) -> int:
    """单镜成稿的拍起点向上扩：把紧邻投搞头部（**Duration/Reference 声明**加粗行、
    围栏开头、空行）并入拍原文——Reference 声明区留在 beat 里，bind 的素材对号与
    首帧声明识别才有依据。遇到其他内容（正文散文/标题）即停。
    镜头标题行（## Shot N｜…）是头部的最外层：吸收它即停——再往上就是上一镜的
    加粗尾段（non_diegetic_music 等），会切走上一镜正文。
    """
    head = source[:pos]
    line_starts = [m.start() for m in re.finditer(r"^", head, re.M)]
    start = pos
    for i in range(len(line_starts) - 1, -1, -1):
        ln = head[line_starts[i]:].split("\n", 1)[0]
        s = ln.strip()
        # 投搞头部行形态：围栏开头，或「**加粗标签：** 内容」声明行（标签加粗、内容
        # 不加粗——整行 fullmatch 加粗会漏掉这种投稿式写法）
        if not s or ln.startswith("```") or re.match(r"\*\*[^*]+?\*\*", s):
            start = line_starts[i]
            if pos - start > 4000:  # 头部护栏：异常长的不像投搞头部，弃用
                return pos
            continue
        if re.match(r"#{1,4}\s+\S", s):
            start = line_starts[i]
            if pos - start > 4000:
                return pos
            break
        break
    return start


# detailed_description 段名存在性检查：裸/加粗段名均可（成稿骨架的第二段）
_H3_DDET_DECL_RE = re.compile(r"(?im)^(?:\*\*\s*)?detailed_description\s*:")
# 块首镜头头「## Shot N｜标题」→ 标题（beat 标题保真，不用工艺化的 H3 Shot N）
_H3_SHOT_TITLE_RE = re.compile(r"(?i)^#{1,4}\s*(?:shot|fragment)\s*0*\d+\s*[｜|:：—–-]?\s*(.+)$")


def _h3_shot_title(source: str, pos: int, fallback_no: int) -> str:
    first = (source[pos:pos + 200] or "").split("\n", 1)[0].strip()
    m = _H3_SHOT_TITLE_RE.match(first)
    return (m.group(1).strip()[:80] if m else "") or f"H3 Shot {fallback_no}"


def detect_h3_prompt_boundaries(source: str) -> list[tuple[int, str]]:
    """英文成稿分镜（每镜一份完整 H3 六段式 Prompt、无中文片段头）的拍边界识别：
    每个行首 subject_definitions: 段名即一镜起点（六段式首段，单镜恰一次，不会切在镜中），
    段名裸写或 **加粗** 皆可；起点向上并入镜头头（## Shot N｜标题 + Duration/Reference
    声明区），Reference 声明留在拍内，bind 的素材对号才有依据。仅在中文片段头识别
    失败时兜底——让英文成稿也能一拍一镜确定性对录，分拍不交给模型猜；plan 阶段
    照常润色（改写稀释由 PLAN_SYSTEM_COMMON 单调性铁律约束）。

    全稿只有一处 subject_definitions 骨架（单镜成稿直投，2026-10-01 事故：不足 2 处
    不认 → LLM 慢路径把单连续镜重拆重排）也算：起点向上并入投搞头部，返回 1 拍。
    """
    src = source or ""
    starts = [m.start() for m in _H3_BOUNDARY_RE.finditer(src)]
    if not starts:
        return []
    if len(starts) >= 2:
        out: list[tuple[int, str]] = []
        prev = -1
        for i, s in enumerate(starts):
            b = _expand_shot_header_upward(src, s)
            if b <= prev:
                b = s  # 上扩越界（吞进上一镜）：退回段名行，切片绝不交叉
            out.append((b, _h3_shot_title(src, b, i + 1)))
            prev = b
        return out
    # 单镜：必须带 H3 骨架（detailed_description 段名）才算成稿，正文偶然提到
    # subject_definitions 一词的故事稿不在此列
    if not _H3_DDET_DECL_RE.search(src):
        return []
    return [(_expand_shot_header_upward(src, starts[0]), _h3_shot_title(src, starts[0], 1))]


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
    if not deterministic:
        # 英文成稿分镜（GPT 六段式 Prompt 直投）：按 H3 段名边界确定性分拍
        h3_bounds = detect_h3_prompt_boundaries(source)
        if h3_bounds:
            frag_headers = h3_bounds
            deterministic = True
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
        "source_text": source[:MAX_SCRIPT_CHARS],
        "global_notes": "",
    }
    if script_type == "storyboard":
        beats, preamble = recover_storyboard_source(beats, source)
        beats = trim_trailing_sections(beats)
        beats, dropped_beats = dedupe_storyboard_beats(beats)
        if dropped_beats and trace:
            trace.log("normalize_storyboard_beats_dedup", kept=len(beats),
                      dropped=[b.get("title") for b in dropped_beats][:10])
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


# ---------------- plan prompt 质量闸门（确定性） ----------------

# 跨镜引用开场白：每镜是独立生成请求，"上一镜"在本次请求里不存在
_CROSS_SHOT_RE = re.compile(
    r"(?i)\bcontinuing from (?:the|this) (?:previous|last) (?:shot|fragment|scene)[,.]?\s*"
    r"|延续上一(?:镜|片段|拍)[，,。]?\s*")
_SHOT_MARK_RE = re.compile(r"\[Shot (\d+)\]")
_DD_SECTION_RE = re.compile(
    r"detailed_description\s*:\s*([\s\S]*?)(?=\n\s*overall_soundscape\s*:|\Z)", re.I)


def fix_prompt_boilerplate(prompt: str) -> str:
    """单镜成片自包含清洗：剔除跨镜引用开场白；全文只有一个 [Shot N] 记号且 N>1 时
    归一为 [Shot 1]（多个记号=镜内多拍叙事，不动）。"""
    prompt = _CROSS_SHOT_RE.sub("", prompt)
    marks = set(_SHOT_MARK_RE.findall(prompt))
    if len(marks) == 1 and "1" not in marks:
        prompt = _SHOT_MARK_RE.sub("[Shot 1]", prompt)
    return prompt


def h3_prompt_quality_issues(prompt: str) -> list[str]:
    """H3 六段式 prompt 质量软闸门：跨镜引用残留、detailed_description 过薄
    （对齐官方指南 350-500 词，闸门线放宽到 250）。非六段式不判词数。"""
    issues: list[str] = []
    if _CROSS_SHOT_RE.search(prompt):
        issues.append("残留跨镜引用（previous shot / 延续上一镜）")
    m = _DD_SECTION_RE.search(prompt)
    if m:
        words = len(re.findall(r"[A-Za-z]+", m.group(1)))
        if words < 250:
            issues.append(f"detailed_description 过薄（约 {words} 英文词 < 250）")
    return issues


# storyboard 润色保真闸门（2026-09-22 第4集三连事故）：
# 镜3 润色发明 "behind <Subject 1>'s shoulder" 与首帧锁定构图互斥 → 双吕满春；
# 镜4 把原文画外的吕满春肩臂写进画面（该镜无锚定图 → 模型胡画幻影）；
# 镜8 四人镜凭空标 over-shoulder → 又一双吕满春。共因：金样例的越肩/前景遮挡
# 先验泄漏 + 闸门只查词数。以下为确定性检出，命中即判负优化、保留草稿。
_OS_INVENT_RE = re.compile(
    r"(?i)over[- ]shoulder"
    r"|behind <subject \d+>'s (?:right |left )?shoulder"
    r"|over [a-z]+(?: [a-z]+)?'s shoulder")
_SRC_OS_RE = re.compile(r"越肩|肩后|过肩")
_PERSON_WORD_RE = re.compile(r"(?i)\b(shoulder|arm|face|figure|person|man|character|silhouette)\b")
_CAP_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)+\b")
_CAP_NAME_STOP = {"Shot", "Subject", "Picture", "Audio", "Timeline", "The", "No", "One", "Two"}


def _phantom_names(cand: str) -> list[str]:
    """detailed_description 里出现、但 subject_definitions 未定义、且与身体/可见词
    同现的人名——未锚定角色被拽进画面（原文画外角色只准以视线/画外音存在）。"""
    norm = cand.replace("ü", "u").replace("é", "e")
    mz = re.search(r"(?is)subject_definitions\s*:?(.*?)(?:\nsummary\s*:|\Z)", norm)
    dz = re.search(r"(?is)detailed_description\s*:?(.*?)(?:\ncinematography\s*:|\noverall_soundscape\s*:|\Z)", norm)
    if not mz or not dz:
        return []
    dd = dz.group(1)
    allowed = set(_CAP_NAME_RE.findall(mz.group(1)))
    out: list[str] = []
    for name in set(_CAP_NAME_RE.findall(dd)):
        if name in allowed or any(w in _CAP_NAME_STOP for w in name.split()):
            continue
        for m in re.finditer(re.escape(name), dd):
            window = dd[max(0, m.start() - 90):m.end() + 90]
            if _PERSON_WORD_RE.search(window):
                out.append(name)
                break
    return sorted(out)


def _dd_zone(cand: str) -> str | None:
    dz = re.search(r"(?is)detailed_description\s*:?(.*?)(?:\ncinematography\s*:|\noverall_soundscape\s*:|\Z)", cand)
    return dz.group(1) if dz else None


# 说话动作句（用于时段对齐检查）：只用规范钦定的两个标准句式，避免 nobody/no one
# speaks 这类否定语境误判；画外音写法 says in an off-screen voiceover 也不匹配
_SPEAK_ACT_RE = re.compile(r"(?i)delivers the spoken line|lip synchronization|lips? synchronized")
# 段头 "0.0-2.0s |"：负向后顾防止在 0.0-2.0s 的第二个字符处误切出 "0-2.0s"
_SEG_HEAD_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)s\s*\|")


def _speech_timing_issues(cand: str) -> list[str]:
    """台词时段对齐（2026-09-22 镜11 事故：Audio Timeline 声明 0.0-4.0s 说话，正文却把
    lip-sync 句放进 2.0-4.0s 段、0-2s 只铺手势——模型 2 秒后才开口、3.94s 音频一播到
    6s+，且说话段里唯一在动的是小墨 → 台词跟错人+整段错位）。Audio Timeline 是台词
    唯一权威：首个含说话动作的时段，其起点不得晚于音频窗起点 0.3s 以上。"""
    tl = re.search(r"(?is)Audio Timeline\s*:\s*(.*?)(?:\noverall_soundscape|\Z)", cand)
    if not tl:
        return []
    m = re.search(r"([\d.]+)\s*-\s*[\d.]+\s*s?\s*\|\s*<Audio \d+>", tl.group(1))
    if not m:
        return []
    a0 = float(m.group(1))
    dd = _dd_zone(cand)
    if not dd:
        return []
    for seg in re.split(r"(?<![\d.])(?=\d+(?:\.\d+)?-\d+(?:\.\d+)?s\s*\|)", dd):
        head = _SEG_HEAD_RE.match(seg.strip())
        if head and _SPEAK_ACT_RE.search(seg):
            if float(head.group(1)) > a0 + 0.3:
                return [f"台词时段错位：Audio 台词窗从 {a0}s 起，正文却把说话动作放到 "
                        f"{head.group(1)}s 段（段界必须与 Audio Timeline 对齐，先铺完手势再开口会把台词整段推后）"]
            return []
    return []


def align_speech_segments(prompt: str) -> tuple[str, bool]:
    """台词时段对齐——基础操作（2026-09-22 镜11 事故，bind 阶段全路径无条件执行，
    不依赖润色遍开关）：正文首个含说话动作的时段晚于 Audio 台词窗起点时，把
    [台词窗起点所在时段, 说话时段] 确定性合并成一个从台词窗起点起标的时段——
    段内原文一字不动，只动段标签；说话动作从此落在台词窗内。幂等。"""
    if not _speech_timing_issues(prompt):
        return prompt, False
    tl = re.search(r"(?is)Audio Timeline\s*:\s*(.*?)(?:\noverall_soundscape|\Z)", prompt)
    tm = re.search(r"([\d.]+)\s*-\s*[\d.]+\s*s?\s*\|\s*<Audio \d+>", tl.group(1)) if tl else None
    dz = re.search(r"(?is)detailed_description\s*:?(.*?)(?:\ncinematography\s*:|\noverall_soundscape\s*:|\Z)", prompt)
    if not (tm and dz):
        return prompt, False
    a0_text, a0 = tm.group(1), float(tm.group(1))
    dd = dz.group(1)
    chunks = re.split(r"(?<![\d.])(?=\d+(?:\.\d+)?-\d+(?:\.\d+)?s\s*\|)", dd)
    timed = [(idx, _SEG_HEAD_RE.match(ch.strip()))
             for idx, ch in enumerate(chunks)]
    timed = [(idx, h) for idx, h in timed if h]
    speak_k = next((k for k, (idx, h) in enumerate(timed)
                    if _SPEAK_ACT_RE.search(chunks[idx])), None)
    if speak_k is None:
        return prompt, False
    # 合并起点：第一个与台词窗有交叠的时段（段尾 > 台词窗起点）；全在窗后起时直接以台词窗起点重标
    start_k = next((k for k, (idx, h) in enumerate(timed)
                    if k <= speak_k and float(h.group(2)) > a0), speak_k)
    j_idx, j_head = timed[start_k]
    i_idx, i_head = timed[speak_k]
    new_start = j_head.group(1) if float(j_head.group(1)) <= a0 else a0_text
    texts = []
    for idx in range(j_idx, i_idx + 1):
        s = chunks[idx].strip()
        h = _SEG_HEAD_RE.match(s)
        body = s[h.end():].strip() if h else s
        if body:
            texts.append(body)
    merged = f"{new_start}-{i_head.group(2)}s | " + " ".join(texts)
    new_dd = "".join(chunks[:j_idx]) + merged + "".join(chunks[i_idx + 1:])
    return prompt[:dz.start(1)] + new_dd + prompt[dz.end(1):], True


def storyboard_polish_issues(cand: str, source: str) -> list[str]:
    """storyboard 拍的润色保真检查（确定性，对照该镜原文）：
    1) 发明机位——原文无越肩设计，润色稿出现越肩/肩后机位（金样例机位不得照搬）
    2) 幻影角色——正文出现未定义角色的可见身体描述"""
    issues: list[str] = []
    if not _SRC_OS_RE.search(source) and not _OS_INVENT_RE.search(source) \
            and _OS_INVENT_RE.search(cand):
        issues.append("发明越肩机位：原文与首帧均无越肩设计，机位只能来自原文/首帧")
    phantoms = _phantom_names(cand)
    if phantoms:
        issues.append(f"幻影角色入镜：正文出现未定义角色 {phantoms} 的可见身体描述（原文画外/未锚定角色不得入镜）")
    issues += _speech_timing_issues(cand)
    return issues


# ---------------- 成稿直录（润色关闭时的零 LLM 路径） ----------------
# 事故（2026-10-01 单镜成稿直投，prompt_polish 已关）：开关只关掉了 director 润色遍，
# plan 主遍照跑 LLM 重写——单连续镜被拆成 [Shot 1]-[Shot 5]、水影时段 0-2s 并成
# 0.0-4.0s、白痕起点丢失、配音被推迟到 "before speaking"，PlanBatchH3 的六段齐全
# 闸门还逼模型发明原文没有的视觉风格（"3D animated short"）与摄影限制（"One
# continuous static shot"）。成稿的时段/镜头结构/风格/摄影必须逐字进视频模型。

# 段名裸写或 **加粗**（markdown 六段式成稿，正文与段名同行）皆可
_H3_BODY_HEAD_RE = re.compile(r"(?im)^(?:\*\*\s*)?subject_definitions\s*:\s*(?:\*\*)?")
# 时长声明：V2/V3 稿用中文「生成时长：6秒」（2026-10-03 第12集 V3 全部落 5 秒默认值），
# 全片位置行（0.00–6.00秒）不在此列；标签可加粗（**Duration:** 4.00 s，第13集）
_DURATION_DECL_RE = re.compile(r"(?:\*\*)?(?:Duration|生成时长)\s*(?:\*\*)?\s*[：:]\s*(?:\*\*)?\s*(\d+(?:\.\d+)?)\s*(?:秒|s|S)")
# 声明 token 与箭头/编号之间允许 markdown 装饰（`<Picture 1>` ← **Image02** 反引号
# 包裹+加粗，连续编号版 v3）——不设防时 parse_picture_map 全空，wants_images 逐镜
# 为空，角色设定卡把全角色注入每镜（第9镜无孙悟空却画出甲胄猴子，9s 报废）
# 连接符含表格竖线（V2/V3 绑定表：`<Picture 1>` | Image11.png 或裸槽位
# Picture 1 | Image05.png 的全局素材列）——竖线后只允许空白/反引号即见 Image，
# 后续单元格的散文提及不会误采；槽位与全局素材的尖括号/「全局」字样均可省（V3）
_PIC_MAP_RE = re.compile(r"(?i)<?Picture\s+(\d+)\s*>?[\s`*(（]*(?:←|<=|=|＝|\|+)[\s`*(（]*Image\s*0*(\d{1,3})(?!\d)")
_AUD_MAP_GLOBAL_RE = re.compile(r"(?i)(?:<)?Audio\s*(\d+)\s*(?:>)?[\s`*(（]*(?:←|<=|=|＝|\|+)[\s`*(（]*(?:全局[\s`*(（]*)?(?:Audio[\s`*(（]*)?0*(\d{1,3})(?!\d)")
_AUD_MAP_TAG_RE = re.compile(r"(?i)<Audio\s+(\d+)\s*>[\s`*(（]*(?:←|<=|=|＝)[\s`*]*<Audio\s+(\d+)>")
_AUDIO_TL_DECL_RE = re.compile(r"(?im)^(?:\*\*)?\s*Audio\s*Timeline\s*[：:]\s*(?:\*\*)?\s*(.+)$")
_TL_WINDOW_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[—–\-]\s*(\d+(?:\.\d+)?)\s*s?")
_TL_AUDIO_TAG_RE = re.compile(r"(?i)(?:<)?\s*Audio\s*0*(\d+)\s*(?:>)?")
_TL_QUOTE_RE = re.compile(r"[“\"]([^”\"]+)[”\"]")


def extract_h3_body(frag: str) -> str | None:
    """从拍原文提取成稿 H3 Prompt 正文：subject_definitions 行起到围栏闭合行为止
    （无围栏时掐掉其后的投搞包装行）。缺 H3 骨架（无行首 subject_definitions: 或
    detailed_description:）返回 None——调用方据此退回 LLM 路径。"""
    frag = frag or ""
    m = _H3_BODY_HEAD_RE.search(frag)
    if not m or not _H3_DDET_DECL_RE.search(frag):
        return None
    rest = frag[m.end():]
    close = re.search(r"^```[ \t]*$", rest, re.M)
    if close:
        return frag[m.start():m.end() + close.start()].rstrip()
    lines = frag[m.start():].splitlines()
    while len(lines) > 3 and re.match(
            r"(?:\*\*)?(?:Audio Timeline|提示|备注|Duration)\s*[：:]|\*\*[^*].*\*\*$",
            lines[-1].strip()):
        lines.pop()
    body = "\n".join(lines).rstrip()
    return body or None


def parse_picture_map(frag: str) -> list[int]:
    """投搞头部「<Picture k> ← Image N」声明 → 按 k 升序的全局图号列表。"""
    pairs: dict[int, int] = {}
    for m in _PIC_MAP_RE.finditer(frag or ""):
        pairs[int(m.group(1))] = int(m.group(2))
    return [pairs[k] for k in sorted(pairs)]


def parse_audio_map(frag: str) -> list[int]:
    """投搞头部音频声明（「Audio k=全局 N」/「<Audio k> ← <Audio N>」）→ 按 k 升序的
    全局音频号列表（bind 对齐的预填 wants；对不上时以 bind 对齐为准）。"""
    pairs: dict[int, int] = {}
    for m in _AUD_MAP_GLOBAL_RE.finditer(frag or ""):
        pairs[int(m.group(1))] = int(m.group(2))
    for m in _AUD_MAP_TAG_RE.finditer(frag or ""):
        pairs.setdefault(int(m.group(1)), int(m.group(2)))
    return [pairs[k] for k in sorted(pairs)]


def materialize_audio_timeline(frag: str, body: str) -> str:
    """把投搞的中文 Audio Timeline 声明行确定性地转成 H3 Audio Timeline 小节
    （`0.0-8.0s | <Audio 1> | <Subject 1> (S1): "台词"`）。正文已有该小节或声明行
    不可解析时返回空——绝不经手 LLM，段界/台词逐字来自用户声明。说话人按
    subject_definitions 的 <Subject N> is 名字对号；对不上就不写说话人标签。"""
    if re.search(r"(?im)^Audio Timeline\s*:", body):
        return ""
    m = _AUDIO_TL_DECL_RE.search(frag or "")
    if not m:
        return ""
    subjects = [(n, name.strip()) for n, name in
                re.findall(r"(?im)^<Subject\s+(\d+)>\s+is\s+([^\n,，.]+)", body) if name.strip()]

    def _subject_of(speaker: str) -> str:
        for n, name in subjects:
            if name in speaker:
                return n
        return ""

    out: list[str] = []
    s_no = 0
    seen_subjects: set[str] = set()
    for part in re.split(r"[；;]", m.group(1)):
        part = part.strip()
        win = _TL_WINDOW_RE.search(part[:60])
        if not win:
            continue
        a, b = float(win.group(1)), float(win.group(2))
        quote = _TL_QUOTE_RE.search(part)
        tag = _TL_AUDIO_TAG_RE.search(part)
        if quote and tag:
            speaker_zone = part[win.end():quote.start()]
            speaker = re.sub(r"(?i)(?:<)?\s*Audio\s*\d+\s*(?:>)?", "", speaker_zone)
            speaker = re.sub(r"[（(].*$", "", speaker).strip(" ：:，,、")
            subj = _subject_of(speaker)
            spk = ""
            if subj:
                if subj not in seen_subjects:
                    s_no += 1
                    seen_subjects.add(subj)
                spk = f"<Subject {subj}> (S{s_no}): "
            out.append(f"{a:.1f}-{b:.1f}s | <Audio {int(tag.group(1))}> | {spk}\"{quote.group(1)}\"")
        else:
            out.append(f"{a:.1f}-{b:.1f}s | No dialogue.")
    return "\n".join(out)


def _adopt_verbatim_h3_shots_beatwise(project: Project, beats: list[dict]) -> list[dict | None]:
    """成稿直录（逐拍）：每拍原文都是 H3 成稿（行首 subject_definitions+detailed_description
    骨架，source_fidelity=verbatim）→ 逐字落为该镜 Prompt。只做确定性
    机械适配：Reference 声明 → wants、中文 Audio Timeline → 英文小节、Duration → 时长。
    缺 H3 工艺块（cinematography/lighting/…）不补——补段就是发明（事故里的风格/摄影
    限制正是六段齐全闸门逼出来的）。失守拍以 None 占位，调用方决定整稿放弃还是逐拍回退
    （run_plan 的逐拍回退：失守拍仅占少数时只对它们走 LLM 分窗，直录拍零调用）。
    """
    images = [m for m in project.materials if m.kind == "image"]
    audios = [m for m in project.materials if m.kind == "audio"]
    shots: list[dict | None] = []
    for i, b in enumerate(beats):
        frag = (b.get("source_text") or "").strip()
        if not frag or b.get("source_fidelity") not in (None, "verbatim"):
            shots.append(None)
            continue
        body = extract_h3_body(frag)
        if not body:
            shots.append(None)
            continue
        prompt = body
        timeline = materialize_audio_timeline(frag, body)
        if timeline:
            prompt = body.rstrip() + "\n\nAudio Timeline:\n" + timeline
        sm = re.search(r"(?im)^(?:\*\*\s*)?summary\s*(?:\*\*)?:\s*(?:\*\*)?\s*(.+)$", body)
        summary = (sm.group(1).strip() if sm else "")[:500]
        dm = _DURATION_DECL_RE.search(frag)
        img_ns = parse_picture_map(frag)
        aud_ns = parse_audio_map(frag)
        shots.append({
            # 标题保真：beat 标题是用户原稿「## 镜头NN｜标题」逐字提取（中文、人可读），
            # 优先于 summary 首段（英文工艺句，第十回 14 镜全英文标题的可读性事故）
            "title": ((b.get("title") or "").strip()
                      or summary.split(",")[0].split("，")[0][:50]
                      or f"成稿镜 {i + 1}"),
            "description": summary or "用户成稿分镜（逐字直录）",
            "prompt": prompt,
            "keyframe_prompt": "",
            "narration": _extract_narration(frag)[:5000],
            "duration": int(float(dm.group(1))) if dm else 5,
            "gen_mode": "i2v" if img_ns else "t2v",
            "wants_images": [
                {"material_id": images[n - 1].id, "description": f"原稿 <Picture {k}> ← Image {n}", "role": "reference"}
                for k, n in enumerate(img_ns, 1) if 0 < n <= len(images)],
            "wants_audios": [
                {"material_id": audios[n - 1].id, "description": f"原稿 Audio {k} = 全局第 {n} 段", "role": "reference"}
                for k, n in enumerate(aud_ns, 1) if 0 < n <= len(audios)],
            "wants_videos": [],
        })
    return shots


def adopt_verbatim_h3_shots(project: Project, beats: list[dict]) -> list[dict] | None:
    """成稿直录（全稿全有或全无）：任一拍失守返回 None，调用方走整稿 LLM 路径。"""
    beatwise = _adopt_verbatim_h3_shots_beatwise(project, beats)
    return list(beatwise) if all(s is not None for s in beatwise) else None


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

    def storyboard_windows(sel: list[int] | None = None) -> list[tuple[list[dict], str]]:
        """storyboard 分窗：单窗 ≤PLAN_WINDOW_SHOTS 拍且原文累计 ≤PLAN_WINDOW_CHARS 字。

        事故（2026-09-21 吕满春西游 6 镜稿）：全稿原文一锅端进一条消息，MiniMax-M3
        是推理模型、思考与正文共用 max_tokens，一次规划多个 H3 六段式长 prompt 时
        预算被思考烧尽、正文为空，连续 3 轮失败。窗小 → 单次任务小，思考与输出
        都落回预算内；每窗自带一拍一镜数量闸门，全稿总数闸门保持不变。
        sel=None 规划全稿；否则只对选中的拍（0 基拍号，逐拍回退用）分窗，
        窗内提示与【原文】标注沿用全稿绝对拍号，模型不感知缺拍。
        """
        idxs = list(range(len(beats))) if sel is None else sorted(sel)
        windows: list[tuple[list[int], list[dict]]] = []
        cur_i: list[int] = []
        cur: list[dict] = []
        cur_chars = 0
        for i in idxs:
            b = beats[i]
            size = len(b.get("source_text") or b.get("description") or "") + 100
            if cur and (len(cur) >= PLAN_WINDOW_SHOTS or cur_chars + size > PLAN_WINDOW_CHARS):
                windows.append((cur_i, cur))
                cur_i, cur, cur_chars = [], [], 0
            cur_i.append(i)
            cur.append(b)
            cur_chars += size
        if cur:
            windows.append((cur_i, cur))
        out: list[tuple[list[dict], str]] = []
        for idx_win, win in windows:
            no = idx_win[0] + 1
            span = f"第 {no}–{idx_win[-1] + 1} 镜" if len(win) > 1 else f"第 {no} 镜"
            note = (
                f"剧本类型：storyboard（用户分镜已成稿，全稿共 {len(beats)} 镜）→ 本窗为全稿{span}，"
                f"一拍一镜，必须恰好输出 {len(win)} 镜，逐拍对录，严禁增删拆合与虚构画面；"
                "素材清单已标注上传序号，第 N 张上传图=原文 <Image N>"
            )
            base = (
                f"{note}{common_mid}剧本节拍（本窗 {len(win)} 拍，按播放顺序）：\n{storyboard_beats_text(win, no)}\n\n"
                f"规划本窗全部分镜，共 {len(win)} 镜，一次性全部输出（done=true）。"
            )
            out.append((win, base))
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
    # 润色开关的完整承诺是"不改稿"：不能只关 director 遍、留着 plan 主遍重写。
    # 关闭且为 H3 成稿拍 → 零 LLM 逐字直录（时段/镜头结构/风格/摄影原样进模型）
    polish_on = h3_style and getattr(project.settings, "prompt_polish", True)

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

    async def plan_storyboard_windows(extra_note: str = "", sel: list[int] | None = None) -> list[dict]:
        acc: list[dict] = []
        for w_no, (win, wbase) in enumerate(storyboard_windows(sel)):
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

    all_shots: list[dict] | None = None
    if is_storyboard and not polish_on:
        per_beat = _adopt_verbatim_h3_shots_beatwise(project, beats)
        failed = [i for i, s in enumerate(per_beat) if s is None]
        if not failed:
            all_shots = list(per_beat)
            if trace:
                trace.log("plan_adopted_verbatim", shots=len(all_shots))
        else:
            # 逐拍回退：一拍失守不再全稿陪葬——直录拍零 LLM，仅失守拍（如分片头
            # 污染导致的 llm_echo）走分窗规划后按拍序回填；回填数量不对则整体
            # 落回全稿规划，保真闸门与总数闸门保持不变
            rescued = await plan_storyboard_windows(sel=failed)
            if len(rescued) == len(failed):
                it = iter(rescued)
                all_shots = [s if s is not None else next(it) for s in per_beat]
                if trace:
                    trace.log("plan_adopted_verbatim_partial",
                              verbatim=len(beats) - len(failed), llm_rescued=len(failed))
            elif trace:
                trace.log("plan_partial_rescue_fallback", expected=len(failed), got=len(rescued))
    if all_shots is None:
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

    # director 润色遍（H3）：每镜一次专注调用，把"结构正确的草稿"推到导演级写作。
    # 容错铁律：润色失败/不达标一律保留草稿——质量遍绝不让规划失败（单调性由构造保证）
    # 用户开关 settings.prompt_polish=False 时整遍跳过（storyboard 成稿/润色反复
    # 破坏原文锁定构图时用户可关，2026-09-22 第4集三连事故）；成稿拍此时已在上方
    # 零 LLM 直录，根本不会走到这里
    polished_ct = kept_ct = 0
    if polish_on:
        from .schemas import PolishedPrompt
        for i, sp in enumerate(all_shots):
            beat = beats[i] if i < len(beats) else {}
            src = "\n".join(filter(None, [
                (beat.get("source_text") or "").strip(),
                (beat.get("description") or "").strip(),
                (f"旁白：{beat['narration']}" if beat.get("narration") else "")])).strip()
            user = (
                f"本镜时长 {sp.get('duration')} 秒；画幅 {project.aspect_ratio}。\n\n"
                f"角色设定（跨镜一致）：\n{chars_block}\n\n"
                f"该镜原文/节拍（最高保真依据，逐字）：\n{src[:PLAN_WINDOW_CHARS]}\n\n"
                f"草稿 Prompt（其中正确的标签/时间轴/否定约束必须原样保留）：\n{sp['prompt']}"
            )
            try:
                r = await envelope.structured_call(POLISH_SYSTEM, user, PolishedPrompt,
                                                   max_tokens=LLM_MAX_OUTPUT_TOKENS)
                cand = fix_prompt_boilerplate((r.prompt or "").strip())
                issues = h3_prompt_quality_issues(cand)
                if is_storyboard:
                    issues += storyboard_polish_issues(cand, src)
                if not issues and len(cand) <= 7900:
                    sp["prompt"] = cand
                    polished_ct += 1
                else:
                    kept_ct += 1
            except Exception:  # noqa: BLE001 — 修复轮次/网络重试耗尽：保留草稿
                kept_ct += 1
        if trace:
            trace.log("plan_director_polish", shots=len(all_shots),
                      polished=polished_ct, kept_draft=kept_ct, enabled=polish_on)

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
        sp["prompt"] = fix_prompt_boilerplate(sp["prompt"])
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

    # 角色设定卡注入：无参考图锚定的镜，Prompt 末尾强制注入（跨镜一致性兜底）。
    # 只注入该镜原文（beat 原文/成稿 Prompt/台词）实际出场的角色——原文没有的角色
    # 写进设定卡就是幻觉源头（第9镜无孙悟空却被全角色注入，生成出甲胄猴子，9s 报废）。
    # 名字带姓氏前缀时同时认去姓变体（孙悟空/悟空），英文成稿里中文名以
    # "Sun Wukong (孙悟空)" 形式存在，中文匹配即可命中
    def _shot_chars(i: int) -> list[dict]:
        hay = "\n".join(filter(None, [
            (beats[i].get("source_text") or "") if i < len(beats) else "",
            scenes[i].prompt, scenes[i].narration]))
        out = []
        for c in chars:
            name = (c["name"] or "").strip()
            if not name:
                continue
            variants = {name} | ({name[1:]} if len(name) >= 3 else set())
            if any(v in hay for v in variants if v):
                out.append(c)
        return out

    for i, sp in enumerate(all_shots):
        if sp.get("wants_images"):
            continue  # 挂参考图的镜外观以图为准，不注入文字设定卡
        scene_chars = _shot_chars(i)
        if not scene_chars:
            continue
        if not any(c.get("design", "")[:20] in scenes[i].prompt for c in scene_chars if c.get("design")):
            block = "\n\n## Character Reference（跨镜一致，外观逐字遵守）\n" + "\n".join(
                f"- {c['name']}：{c['design']}" for c in scene_chars
            )
            scenes[i].prompt = (scenes[i].prompt.rstrip() + block)[:8000]

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
# 「Reference 图片上传绑定：Picture 1=Image 01；…」标签与声明同一行（闯西游第四回）：
# 只认整行恰为 Reference 的旧正则匹配不到 → 声明区整体漏拆 → 对齐失效退回 plan 猜测。
# 行首允许 markdown 装饰（**Reference 图片：** 加粗投搞头部，2026-10-01 单镜成稿
# 投稿式：**Reference 图片：** <Picture 1> ← Image 21；…）——不认就对齐全失效，
# 音频还会被正文 <Audio 1> 标签的兜底错绑到全局第 1 段
_REF_HEADER_INLINE_RE = re.compile(r"^(?:#{1,4}\s*)?\**\s*Reference\s*[^\s：:]{0,40}\s*[:：]\s*(.*)$", re.I)
# 已知小节标题（声明区的合法终点）；“##”兼容整段压成一行的 markdown 草稿
_REF_END_RE = re.compile(
    r"^(Audio\s+Timeline|H3\s*Prompt|H3\s*硬约束|subject_definitions|summary|retention_analysis"
    r"|detailed_description|overall_soundscape|non_diegetic_music|时长[:：]|##)", re.I)
# 无标签长行且不以冒号收尾＝正文散文：正文提到 <Image N> 是叙述引用，不是绑定声明
_REF_PROSE_RE = re.compile(r"^.{41,}$")
_REF_TAG_RE = re.compile(r"<(?:Image|Audio|Video)\s+\d+\s*>")
# 独立声明行（「Picture 1=Image 01」「Audio 3｜…」）：行首即编号素材词才是声明，
# 「0—2秒：从Image 05 …」这类散文叙述行不在此列（仍由散文闸终止声明区）。
# 「- 第1张：」清单行（连续编号版分镜稿 v3：`<Picture 1>` ← **Image02**（…））同样
# 自证为声明——编号+冒号+箭头映射，v3 不认则整稿图片对齐全空（39 张参考图悬空）
_REF_DECL_LINE_RE = re.compile(
    r"^(?:(?:-\s*)?(?:Picture|Image|Audio|Video)\s*\d+|-\s*第[0-9一二三四五六七八九十百]+张\s*[:：])", re.I)
# 裸编号 token（无尖括号）：Lookbehind 挡住英文前缀（MyImage01）与标识符连接符
# （上传图片_Image01-39/Image04.png 的目录模板——v3 稿每条声明行都带，「_Image01-」
# 下划线+短横线两侧都不设防，7 个未声明 Image01 的镜全被塞了一张 Image01 幻影参考），
# 不挡中文前缀（本镜Audio 1）；「全局Audio N」与「Audio k=全局 N」是全局号（投稿式
# 声明），与局部号「本镜Audio K」并存时只认全局
_PLAIN_IMG_RE = re.compile(r"(?<![A-Za-z0-9>_])Image\s*0*(\d{1,3})(?!\d)")
_PLAIN_VID_RE = re.compile(r"(?<![A-Za-z0-9>_])Video\s*0*(\d{1,3})(?!\d)")
_PLAIN_AUD_GLOBAL_RE = re.compile(
    r"(?i)(?:全局\s*Audio\s*|Audio\s*\d+\s*=\s*全局\s*(?:Audio\s*)?)0*(\d{1,3})(?!\d)")
_PLAIN_AUD_RE = re.compile(r"(?<![A-Za-z0-9>_])Audio\s*0*(\d{1,3})(?!\d)")

# 剧本 V2/V3 逐镜绑定表（2026-10-03 第12集两轮事故）：markdown 表格声明素材映射，
# 没有 Reference 标题行。V2：「| 类型 | 本镜绑定 | 全局素材 | 用途/时点 |」，槽位带
# 尖括号（`<Picture 1>`）、音频列「全局Audio 01」；V3：「| 上传次序 | 本镜槽位 |
# 本文件夹内文件 | 用途 |」，槽位裸写（Picture 1）、音频列为文件名（Audio01.mp3）。
# 共同结构＝某单元格恰为槽位名（Picture/Image/Video/Audio k，反引号/尖括号可选），
# 其后单元格恰为全局素材（ImageNN.png / VideoNN / 全局Audio NN / AudioNN.mp3）。
# 老识别（Reference 标题行激活声明区）两版整表漏拆：图片侧不在尖括号词表、裸
# 文件名只在声明区内采信 → 图片对齐全空；音频侧本镜槽位号被兜底当全局 1 号
# （仅镜01碰巧对，其余语义互证全炸）。槽位单元格全匹配＋素材单元格紧随＝无散文
# 误采风险
_V2_SLOT_CELL_RE = re.compile(r"^`?\s*<?(Picture|Image|Video|Audio)\s+0*(\d{1,3})\s*>?`?$", re.I)
_V2_FILE_IMG_RE = re.compile(r"(?<![A-Za-z0-9>_])Image\s*0*(\d{1,3})(?!\d)", re.I)
_V2_FILE_VID_RE = re.compile(r"(?<![A-Za-z0-9>_])Video\s*0*(\d{1,3})(?!\d)", re.I)
_V2_FILE_AUD_RE = re.compile(r"(?:全局\s*)?Audio\s*0*(\d{1,3})(?!\d)", re.I)


def _v2_rows(frag: str):
    """V2/V3 绑定表行 → (素材kind: image/video/audio, 本镜槽位k, 全局号n, 身份说明)。

    kind 按文件列判定（Picture 槽位配 VideoNN 文件按视频算）；身份取行末用途列。"""
    for ln in (frag or "").splitlines():
        s = ln.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 3:
            continue
        for i, c in enumerate(cells):
            m = _V2_SLOT_CELL_RE.match(c)
            if not m:
                continue
            slot_kind, k = m.group(1).lower(), int(m.group(2))
            rest = "|".join(cells[i + 1:])
            if slot_kind == "audio":
                fm = _V2_FILE_AUD_RE.search(rest)
                if fm:
                    yield "audio", k, int(fm.group(1)), cells[-1]
            else:
                fm = _V2_FILE_IMG_RE.search(rest) or _V2_FILE_VID_RE.search(rest)
                if fm:
                    vid = fm.re is _V2_FILE_VID_RE
                    yield ("video" if vid else "image"), k, int(fm.group(1)), cells[-1]
            break


def _v2_table_tokens(frag: str) -> tuple[list[int], list[int], list[int]]:
    """V2/V3 绑定表行 → 按行序的全局图片/音频/视频号。命中即为表驱动声明，调用方
    以此为权威——本镜槽位号（<Audio 1>/Audio 1）绝不走老路径兜底（会被误当全局号）。"""
    img_ns: list[int] = []
    vid_ns: list[int] = []
    aud_ns: list[int] = []
    for kind, _k, n, _ident in _v2_rows(frag):
        {"image": img_ns, "video": vid_ns, "audio": aud_ns}[kind].append(n)
    return list(dict.fromkeys(img_ns)), list(dict.fromkeys(aud_ns)), list(dict.fromkeys(vid_ns))


def _reference_tokens(frag: str) -> tuple[list[int], list[int], list[int]]:
    """从片段原文提取 Reference 声明里的 <Image N>/<Audio N>/<Video N> 编号（保持声明顺序）。

    优先解析独立 Reference 段（用户分镜稿的标准结构）；没有该段时退回全文尖括号
    token 首现顺序。这是"素材分配照抄原视频"的锚点——用户脚本编号 = 素材上传顺序。
    裸编号（Picture 1=Image 01／本镜Audio 1=全局Audio 03）只在 Reference 头行与
    独立声明行里采信，散文里的 Image 提及绝不入表。
    """
    lines = (frag or "").splitlines()
    img_ns: list[int] = []
    aud_ns: list[int] = []
    vid_ns: list[int] = []

    v2 = _v2_table_tokens(frag)
    if any(v2):
        return v2

    def _take_angle(text: str) -> None:
        for kind, n in re.findall(r"<(Image|Audio|Video)\s+(\d+)>", text):
            {"Image": img_ns, "Audio": aud_ns, "Video": vid_ns}[kind].append(int(n))

    def _take_plain(text: str) -> None:
        for n in _PLAIN_IMG_RE.findall(text):
            img_ns.append(int(n))
        for n in _PLAIN_VID_RE.findall(text):
            vid_ns.append(int(n))
        # 音频全局号优先：局部号从 1 重排，混采会把 4 条绑成 2 镜各 4 条
        auds = _PLAIN_AUD_GLOBAL_RE.findall(text) or _PLAIN_AUD_RE.findall(text)
        for n in auds:
            aud_ns.append(int(n))

    in_ref = False
    for ln in lines:
        s = ln.strip()
        if _REF_HEADER_RE.match(s):
            in_ref = True
            continue
        m = _REF_HEADER_INLINE_RE.match(s)
        if m:
            in_ref = True
            _take_angle(m.group(1))
            _take_plain(m.group(1))
            continue
        if not in_ref:
            continue
        if _REF_END_RE.match(s):
            in_ref = False
            continue
        if not s:
            # 空行不终止声明区：markdown 双倍行距的清单行间就是空行（事故：闯西游第二回
            # Reference 标题后的第一个空行把整个区判死，7 拍全空 → 对齐失效 → 兜底 LLM
            # 往每镜塞多余参考图，剧本写 5 张实际绑 6 张）
            continue
        if _REF_DECL_LINE_RE.match(s):
            _take_angle(s)
            _take_plain(s)
            continue
        if _REF_PROSE_RE.match(s) and not _REF_TAG_RE.search(s) and not s.endswith(("：", ":")):
            in_ref = False
            continue
        _take_angle(s)
    if not (img_ns or aud_ns or vid_ns):
        # 兜底：没有独立 Reference 段（整段压一行/## 内联式），或段识别失败一无所获——
        # 按全文尖括号 token 首现顺序。裸编号不进兜底：散文满篇都是 Image 提及。
        # 宁可宽进（后续有音频语义互证、图片声明数对账），不可空手
        _take_angle(frag or "")
    return list(dict.fromkeys(img_ns)), list(dict.fromkeys(aud_ns)), list(dict.fromkeys(vid_ns))


def _reference_table(source_text: str) -> dict[int, str]:
    """解析原稿全局参考图编号表：<Image N>｜身份说明 → {N: 说明}（首现优先，全局表在片段前）。

    身份说明截到下一个标签/反引号/星号——markdown 草稿整篇一行时表格被压成
    `…参考图 * `<Image 2>`：…`，不截断会把后续条目全吞进来。
    """
    table: dict[int, str] = {}
    for m in re.finditer(
            # 裸编号表行必须带「｜/:/（」分隔符：规则散文「Image 13使用去除…的修正版」不是表项；
            # 「（」覆盖投稿式「Image 04（小墨….png）」内联身份（2026-10-01 单镜成稿）
            r"(?:<Image\s+(\d+)\s*>\s*[｜|:：]?|(?<![A-Za-z0-9>_])Image\s*0*(\d{1,3})(?!\d)\s*[｜|:：（(])\s*([^\n]+)",
            source_text or ""):
        # 反引号包裹的标签（`<Image N>`：身份）：捕获串以 `/： 开头时先剥壳再切分，
        # 否则 split 首段为空、身份被丢弃——第07集整张编号表因此解析为空，
        # 定义行全部回退「本镜参考图」占位符（复现事故）
        raw = m.group(3).lstrip("` ｜|:：（(\t")
        identity = re.split(r"`|←|→|／?\s\*\s|<Image\s|<Audio\s|<Video\s|<Subject\s|<Picture\s", raw)[0]
        # 素材索引行「Image 05｜分镜01｜四人山路对峙…」「Image 13｜分镜09、10共用｜…」：
        # 编号轴（含枚举共用）不是身份，剥掉
        identity = re.sub(r"^(?:分镜|片段)\s*\d+(?:[A-Za-z]\d?)?[^｜|:：]{0,12}[｜|:：]\s*", "", identity)
        identity = identity.strip().strip("：:|｜）) ").strip()
        if identity:
            table.setdefault(int(m.group(1) or m.group(2)), identity[:120])
    # V2/V3 绑定表行：全局素材列取号、行末用途列取身份（「Image05.png | 首帧：小墨
    # 手持颗粒簇近景」）
    for kind, _k, n, identity in _v2_rows(source_text or ""):
        identity = (identity or "").strip().strip("`：:|｜ ").strip()
        if kind != "audio" and identity:
            table.setdefault(n, identity[:120])
    return table


# wants 降级文本（rewrite_aligned_prompt 产出）：对视频模型是"一张本次请求不会传入的图"，
# 留在正文里会诱导 H3 当作参考位。中性化为纯设计指代，retention 声明同步降级。
_UNBOUND_WANT_RE = re.compile(r"reference image \d+ of the original plan \(not bound to this shot\)")
_UNBOUND_NEUTRAL = "the original plan's global design reference (no image attached to this request)"


def _neutralize_unbound_refs(prompt: str) -> str:
    """把未绑定图的降级占位改成中性指代，并修正 retention_analysis 的保留级别。"""
    if not _UNBOUND_WANT_RE.search(prompt):
        return prompt
    prompt = _UNBOUND_WANT_RE.sub(_UNBOUND_NEUTRAL, prompt)
    return "\n".join(
        ln.replace("fully_preserved", "weak_reference")
        if _UNBOUND_NEUTRAL in ln and "fully_preserved" in ln else ln
        for ln in prompt.splitlines())


# 三视图/定妆类人物图识别：身份行命中即"身份锚"而非"构图锚"（第4集事故：
# 统一措辞把三视图表误导成构图锚，模型把多视图排版当构图复现 → 双吕满春）
_TURNAROUND_SHEET_RE = re.compile(r"三视图|多视图|多角度|定妆|人物图|身份")
# 该镜首帧声明："首帧：Image 12" / "首帧:Image 07"（全局编号）
_KEYFRAME_DECL_RE = re.compile(r"首帧[：:]\s*Image\s*0*(\d+)")


def _append_visual_constraint(prompt: str, line: str) -> str:
    """把否定约束句追加到 visual_constraints 段尾（无该段则补一段）。"""
    m = re.search(r"(?im)^(\s*visual_constraints\s*:\s*)(.*)$", prompt)
    if m:
        end = m.end(2)
        return prompt[:end].rstrip() + " " + line + prompt[end:]
    return prompt.rstrip() + "\nvisual_constraints: " + line


def _inject_identity_constraints(prompt: str, inject_turnaround: bool) -> str:
    """绑定三视图人物表时的构造性保险（幂等，不指望润色遍自觉）：
    1) 多视图≠多角色 + 每角色恰好一实例/两臂两手（原文"不把多视图生成成多个
       角色"否定约束在第4集被润色 14/14 全丢 → 双吕满春/悟空多手）
    2) 定义了多个 <Subject> 且仅一人说话时：非说话人嘴唇全程闭合（镜11 台词
       张冠李戴的 prompt 级缓解）"""
    if inject_turnaround and not re.search(r"(?i)no extra limbs", prompt):
        prompt = _append_visual_constraint(
            prompt,
            "Multi-view turnaround references define identity only; their layout must never be "
            "rendered as multiple characters; each anchored character appears exactly once, with "
            "exactly two arms and two hands — no extra limbs; each referenced prop (such as the "
            "character's weapon) also appears exactly once in the shot.")
    m = re.search(r"(?is)Audio Timeline\s*:\s*(.*?)(?:\noverall_soundscape|\Z)", prompt)
    if m:
        speakers = set(re.findall(r"<Subject (\d+)>", m.group(1)))
        subjects = set(re.findall(r"<Subject (\d+)> is", prompt))
        others = subjects - speakers
        if len(subjects) >= 2 and len(speakers) == 1 and others \
                and not re.search(r"(?i)keeps? the mouth closed|lips? (?:remain|stay) closed"
                                  r"|mouths? (?:remain|stay) closed", prompt):
            tags = ", ".join(f"<Subject {n}>" for n in sorted(others, key=int))
            prompt = _append_visual_constraint(
                prompt,
                f"During the dialogue only the tagged speaker's lips move; {tags} "
                "keeps the mouth closed at all times.")
    return prompt


def _verbatim_h3_prompt(prompt: str, beat_src: str, n_bound: int) -> bool:
    """成稿直录保真判定：该镜 Prompt 仍是拍原文的 H3 成稿（正文包含原文 body），且
    原文行首 <Picture> 声明号恰覆盖 1..N（＝绑定序）。V2/V3 绑定表成稿满足——声明行
    是用户手写的权威锚定句，重建会换成模板句。LLM 重写稿（body 已被改写、声明身份
    是模型猜测）不满足——其声明行必须按原稿编号表重建（闯西游五行山基准图事故）。"""
    body = extract_h3_body(beat_src or "")
    if not body or body not in (prompt or ""):
        return False
    ns = {int(k) for k in re.findall(r"(?m)^\s*<Picture\s+(\d+)\s*>", body)}
    return bool(ns) and ns == set(range(1, n_bound + 1))


def rewrite_aligned_prompt(prompt: str, image_ns: list[int], table: dict[int, str],
                           wants_mids: list[str] | None = None, bound_mids: list[str] | None = None,
                           global_mids: list[str] | None = None,
                           keyframe_ns: set[int] | None = None,
                           turnaround_guard: bool = False) -> str:
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

    keyframe_ns：该镜原文声明为首帧的全局 <Image N>（"首帧：Image 12"）。锚点行按
    角色分流（2026-09-22 双吕满春事故：统一写"锚定本镜构图"把三视图人物表误导成
    构图锚，模型把多视图排版当构图复现）——首帧才锚构图/起始姿态/光线；三视图/
    定妆类人物图只锚身份外观，并明示"多视图排版不得生成多角色"。
    turnaround_guard：绑定三视图人物表（或原文声明多视图约束）时，构造性注入
    防复制/肢体数约束与（单人说话镜的）非说话人闭嘴约束——不指望润色遍自觉保留
    原文否定约束（第4集 14/14 镜全丢），幂等。
    """
    # 已重写过的 Prompt（定义行带 original storyboard Image /（原稿 Image N）标记或
    # wants 降级文本）其 <Picture k> 已是绑定序号域——再按 wants 域解释会把 dualren 的
    # <Picture 2> 当成 wants 第 2 张误降级（重跑 bind 必坏，事故：每次重新绑定都破坏上次的修复）
    _rewritten = ("original storyboard image" in prompt.lower()
                  or "（原稿 image" in prompt.lower() or "of the original plan (not bound" in prompt
                  or _UNBOUND_NEUTRAL in prompt)
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
    keyframe_ns = keyframe_ns or set()
    sheet_bound = turnaround_guard
    decl_lines = []
    for k, n in enumerate(image_ns, start=1):
        identity = table.get(n) or "本镜参考图"
        if h3_style:
            if n in keyframe_ns:
                tail = ("it anchors this shot's composition, starting poses and lighting "
                        "as the designated first frame.")
            elif _TURNAROUND_SHEET_RE.search(identity):
                sheet_bound = True
                tail = ("it anchors the character's identity, costume and proportions; it is a "
                        "turnaround sheet — its multi-view layout must never be reproduced as "
                        "multiple characters or duplicated props.")
            else:
                tail = "it anchors the referenced content's appearance and visual states in this shot."
            decl_lines.append(
                f"<Picture {k}> is the reference for: {identity} (original storyboard Image {n}); {tail}")
        else:
            # 尾注（原稿 Image N）是重写标记：重跑 bind 靠它识别"已是绑定序号域"跳过
            # wants 重映射，与 H3 行的 (original storyboard Image N) 同等效力
            decl_lines.append(f"<Picture {k}>：{identity}（原稿 Image {n}）")
    if not decl_lines:
        return _inject_identity_constraints(_neutralize_unbound_refs(prompt), sheet_bound)
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
        return _inject_identity_constraints(_neutralize_unbound_refs("\n".join(out)), sheet_bound)
    # 没有既有定义行可替换：H3 插到 subject_definitions: 段首之后，旧结构附在末尾
    if h3_style:
        for i, ln in enumerate(out):
            if "subject_definitions" in ln.lower():
                out[i + 1:i + 1] = decl_lines
                return _inject_identity_constraints(
                    _neutralize_unbound_refs("\n".join(out)), sheet_bound)
    return _inject_identity_constraints(
        _neutralize_unbound_refs("\n".join(out).rstrip() + "\n" + "\n".join(decl_lines)),
        sheet_bound)


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
    #    storyboard 已按原稿对齐图片/视频时不再兜底强塞：原稿没引用的素材就是用户
    #    明确不用的（"用不上的素材绝不引用"），交给 pending/用户而不是替用户决定。
    #    只有音频对上不算（第12集 V2 表事故：图片声明解析缺口→仅音频互证单镜通过→
    #    aligned 非空→25 张图全被判"剧本未用"悬空）——图片解析失败时必须允许兜底
    storyboard_aligned = is_storyboard and any(
        a["image_mids"] or a["video_mids"] for a in aligned.values())
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
    # 出镜↔锚定一致性留痕（建议性）：plan 想挂但原稿 Reference 未声明而被降级的图
    unanchored: list[dict] = []
    if aligned:
        ref_table = _reference_table((project.screenplay or {}).get("source_text") or "")
        all_wants = {w["scene_id"]: w for w in ((project.screenplay or {}).get("wants") or [])}
        renumber_log: list[dict] = []
        unanchored_seen: set[tuple[str, str]] = set()

        def fn3(p: Project) -> None:
            for si, scene in enumerate(p.scenes):
                a = aligned.get(scene.id) or {}
                if a.get("image_mids"):
                    bound_order = [x.material_id for x in scene.binding.images]
                    ns = [n for n, mid in zip(a["image_ns"], a["image_mids"]) if mid in bound_order]
                    w = all_wants.get(scene.id) or {}
                    wants_mids = [x.get("material_id") for x in (w.get("wants_images") or [])]
                    beat_src = (beats[si].get("source_text") or "") if si < len(beats) else ""
                    guard = bool(re.search(r"多视图|三视图|定妆", beat_src))
                    if _verbatim_h3_prompt(scene.prompt, beat_src, len(bound_order)):
                        # V2/V3 成稿直录：声明行/时间轴即绑定顺序，逐字保真，只做中性化
                        # 与构造性保险注入（2026-10-03 第12集：重建曾把手写锚定句换模板句）
                        scene.prompt = _inject_identity_constraints(
                            _neutralize_unbound_refs(scene.prompt), guard)[:8000]
                    else:
                        scene.prompt = rewrite_aligned_prompt(
                            scene.prompt, ns, ref_table, wants_mids=wants_mids, bound_mids=bound_order,
                            global_mids=[m.id for m in images_in_order],
                            keyframe_ns={int(x) for x in _KEYFRAME_DECL_RE.findall(beat_src)},
                            turnaround_guard=guard)[:8000]
                    scene.revision += 1
                    for mid in wants_mids:
                        mat = next((m for m in p.materials if m.id == mid), None)
                        if (mat and mat.kind == "image" and mid not in bound_order
                                and (scene.id, mid) not in unanchored_seen):
                            unanchored_seen.add((scene.id, mid))
                            unanchored.append({
                                "scene": scene.title, "material": mat.filename,
                                "note": "plan 想引用但原稿 Reference 未声明、未绑定；"
                                        "若其主体本镜出镜请手动绑定以锚定外观，仅画外音可忽略"})
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
        if unanchored and trace:
            trace.log("bind_unanchored_wants", count=len(unanchored), detail=unanchored[:8])

    # 4b) 台词时段对齐（基础操作，全路径无条件执行——storyboard/story、润色开/关、
    #     手改 prompt 一视同仁）：正文说话段晚于 Audio 台词窗起点 → 确定性合并时段
    timing_fixed: list[str] = []

    def fn4(p: Project) -> None:
        for scene in p.scenes:
            new_prompt, fixed = align_speech_segments(scene.prompt)
            if fixed:
                scene.prompt = new_prompt[:8000]
                scene.revision += 1
                timing_fixed.append(scene.title)

    _store_mutate(project_id, fn4)
    if timing_fixed and trace:
        trace.log("bind_speech_timing_aligned", count=len(timing_fixed), detail=timing_fixed[:8])
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
    if unanchored:
        result["unanchored_references"] = unanchored[:20]
    if duration_adjusted:
        result["duration_adjusted"] = duration_adjusted
    if duration_overflow:
        result["duration_overflow"] = duration_overflow
    if pending:
        result["pending_bindings"] = pending
    if dropped_wants:
        result["dropped_wants"] = dropped_wants[:20]
    return result
