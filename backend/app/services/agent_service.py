"""Agent 任务模式：LLM + 内部工具循环。

可完成「写 Prompt → 设置场景 → 生成图片/语音 → 生成视频」的全流程编排。
每个工具返回结构化结果回传给 LLM 决定下一步；长耗时任务提交后由前端轮询。
"""
import asyncio
import hashlib
import json
import re
import uuid

from ..config import model_store, settings, voice_assets
from ..providers.base import ProviderError
from ..providers.registry import resolve_llm_call, get_video
from ..services import export_service, file_service, image_service, skill_service, voice_service, video_service
from ..services import project_service
from ..jobs import job_manager

MAX_STEPS = 1000
# 连续畸形输出（纯文本 / JSON 缺字段）的容忍上限：超过后把最后的文本降级为回复收尾，避免无限打转
MAX_BAD_OUTPUT_STREAK = 3
# 同一工具以相同错误连续失败的熔断阈值：达到后强制收尾，不再烧步数预算
MAX_SAME_TOOL_ERRORS = 4


def _pick_image_model() -> dict:
    """选第一个有可用凭据厂商的生图模型（含自定义模型），避免兜底到没配 Key 的厂商。"""
    from ..config.key_manager import list_keys

    models = settings.IMAGE_MODELS + model_store.list_custom().get("image", [])
    providers = {k.get("provider") for k in list_keys()}
    for m in models:
        if m.get("provider") in providers:
            return m
    return models[0]

# 正在运行的 Agent 各自持有独立取消标志：旧实现是单一全局标志，新 run 启动时重置会
# 清掉尚未生效的停止请求，取消也会波及无关的并发 run
class _CancelFlag:
    __slots__ = ("flag",)

    def __init__(self):
        self.flag = False


_ACTIVE_CANCEL_FLAGS: set = set()


def cancel_run():
    """请求手动停止当前所有正在运行的 Agent（各自在下一个决策步生效）。"""
    for flag in list(_ACTIVE_CANCEL_FLAGS):
        flag.flag = True


async def _safe_exec(agent: "_Agent", tool: str, args: dict) -> dict:
    try:
        if tool == "generate_scene_image":
            return await agent.generate_scene_image(args)
        if tool == "wait_job":
            return await agent.wait_job(args)
        if tool == "generate_scene_voice":
            return await agent.generate_scene_voice(args)
        if tool == "generate_scene_video":
            return await agent.generate_scene_video(args)
        if tool == "export_project":
            return await agent.export_project()
        return agent.execute_tool(tool, args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


def _goal_carries_content(goal: str) -> bool:
    """用户消息里是否携带新内容（分镜稿/剧本/长提示词），而不是纯执行指令。

    判据：去空白后 ≥120 字，或 ≥3 行非空文本——粘贴的分镜稿必然满足；
    「生成分镜视频」「继续生成」这类纯指令必然不满足。
    用于两处：快路径准入（消息带新内容时不进零 LLM 快路径，交给助手循环消化）、
    防串剧本拦截（只有消息带了新剧本而分镜 Prompt 没更新时才可能串旧稿）。
    """
    text = (goal or "").strip()
    if not text:
        return False
    if len(text) >= 120:
        return True
    return len([ln for ln in text.splitlines() if ln.strip()]) >= 3


def _scan_json_objects(text: str) -> list[str]:
    """按括号配平扫描文本里所有 {...} 片段（字符串/转义感知，容忍键序与前后夹杂文字）。"""
    fragments: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    fragments.append(text[start : i + 1])
                    start = -1
    return fragments


def _parse_json_objects(text: str) -> list[dict]:
    """提取文本中所有能解析成 dict 的 {...}（容忍字符串值里的真实换行等字面量）。"""
    out: list[dict] = []
    for frag in _scan_json_objects(text):
        parsed = None
        for cand in (frag, _repair_json_literals(frag)):
            try:
                parsed = json.loads(cand)
                break
            except Exception:  # noqa: BLE001 - 换下一种修复方式重试
                continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _extract_action(raw: str) -> tuple[dict, bool, bool]:
    """解析模型输出里的动作 JSON。返回 (data, strict, whole_parse)。

    - strict=True：raw 里解析出了真实 JSON。whole_parse=True 表示整体就是一个 JSON 对象
      （标准路径）；whole_parse=False 表示从混排文本（说明文字 + JSON）里扫描得到。
    - strict=False：没有任何可解析的动作 JSON —— 调用方必须按畸形输出回喂纠错，
      绝不能把纯文本（如"我先查看项目状态…"这类宣言）当作最终回复直接结束。
    """
    text = raw.strip()
    fenced = re.sub(r"^```(?:json)?\s*", "", text)
    fenced = re.sub(r"\s*```$", "", fenced)
    for cand in (fenced, _repair_json_literals(fenced)):
        try:
            data = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data, True, True
    for data in _parse_json_objects(raw):
        if "action" in data or "tool" in data:
            return data, True, False
    return {"action": "reply", "text": raw}, False, False


def _extract_json(text: str) -> dict:
    """兼容入口：只返回解析出的动作对象（解析失败时回落为纯文本 reply）。"""
    return _extract_action(text)[0]


def _embedded_tool_calls(raw: str) -> list[tuple[str, dict]]:
    """混排文本里内嵌的全部工具调用（按出现顺序，容忍键序变化）。"""
    out: list[tuple[str, dict]] = []
    for data in _parse_json_objects(raw):
        if data.get("action") == "tool" and data.get("tool"):
            args = data.get("args")
            out.append((str(data["tool"]), args if isinstance(args, dict) else {}))
    return out


def _fallback_summary(agent: "_Agent") -> str:
    """模型未给出有效文字总结时，从已执行步骤合成一条兜底回复（替代"任务已执行"）。"""
    count = sum(1 for s in agent.steps if s.get("type") == "tool")
    if not count:
        return "本次没有执行任何步骤：模型未能产出有效的动作输出，请重试或更换模型。"
    tools: list[str] = []
    for s in agent.steps:
        if s.get("type") == "tool" and s.get("tool") and s["tool"] not in tools:
            tools.append(s["tool"])
    shown = "、".join(tools[:8]) + ("等" if len(tools) > 8 else "")
    return f"已执行 {count} 步工具调用（{shown}），后台任务继续进行；但模型未返回有效的文字总结。"

SKILL_TOOL_HINT = """{{"action":"tool","tool":"load_skill","args":{{"name":"技能ID","file":"可选，参考文件名"}}}}"""

SYSTEM_PROMPT = """你是 AI Video Studio 的任务执行 Agent，通过调用内部工具完成视频创作全流程。

可用工具（每次只调用一个，严格输出 JSON）：
{{"action":"tool","tool":"load_skill","args":{{"name":"技能ID","file":"可选，读取技能的参考文件"}}}}
{{"action":"tool","tool":"set_prompt","args":{{"text":"..."}}}}
{{"action":"tool","tool":"set_description","args":{{"text":"..."}}}}
{{"action":"tool","tool":"set_narration","args":{{"text":"..."}}}}
{{"action":"tool","tool":"generate_image","args":{{"prompt":"...","aspect_ratio":"16:9"}}}}
{{"action":"tool","tool":"generate_voice","args":{{"text":"...","model":"...","voice":"..."}}}}
{{"action":"tool","tool":"generate_voices_batch","args":{{"lines":[{{"text":"每行原文逐字","voice":"可选，剧本标注的音色","emotion":"可选，中文/英文情绪词"}}],"script":"纯文本剧本时的兜底：原文逐字，每行一句，支持 文本|音色|情绪 行内标注"}}}}
{{"action":"tool","tool":"save_voice","args":{{"name":"音色名，如：深夜电台男声","desc":"可选，音色文字描述","job_id":"可选，要固化的配音任务ID","audio_url":"可选，音频URL"}}}}
{{"action":"tool","tool":"generate_video","args":{{"duration":5,"aspect_ratio":"16:9","with_image":false}}}}
{{"action":"tool","tool":"wait_job","args":{{"job_id":"...","seconds":20}}}}

分镜编排工具（用户给出完整剧本、要求拆分镜/批量生成视频时使用）：
{{"action":"tool","tool":"get_project_state","args":{{}}}}
{{"action":"tool","tool":"create_scenes","args":{{"characters":[{{"name":"小墨","design":"30-60字外观设定，逐字复述进每镜Subject"}}],"scenes":[{{"title":"开场","description":"一句话概述","prompt":"完整H3 Prompt（Subject复述设定卡）","narration":"旁白可空","duration":5,"gen_mode":"i2v","keyframe_prompt":"关键帧图描述"}}]}}}}
{{"action":"tool","tool":"set_scene_prompt","args":{{"index":0,"prompt":"..."}}}}
{{"action":"tool","tool":"set_scene_narration","args":{{"index":0,"text":"..."}}}}
{{"action":"tool","tool":"attach_scene_refs","args":{{"index":0,"ref_images":["/api/files/uploads/image/xxx.png"],"ref_audios":["/api/files/uploads/audio/xxx.mp3"]}}}}
{{"action":"tool","tool":"read_doc","args":{{"url":"/api/files/uploads/doc/xxx_剧本.md"}}}}
{{"action":"tool","tool":"generate_scene_video","args":{{"index":0}}}}
{{"action":"tool","tool":"generate_scene_voice","args":{{"index":0,"text":"可选覆盖旁白","model":"可选","voice":"可选","use_uploaded":false,"force":false}}}}
{{"action":"tool","tool":"generate_scene_image","args":{{"index":0,"prompt":"可选覆盖素材图描述","reference_index":null}}}}

generate_scene_video 是**一键出片**：旁白未配音时自动先合成（并按语音时长校准 duration）、
挂有上传音频时直接沿用用户音频、无旁白按静音镜提交——不需要先调配音工具，直接调它即可。
attach_scene_refs 挂载用户上传的素材，两种用法：
- **整条片的素材**（多段配音/多张参考图，用户没按镜区分）：**不带 index** 调用——配音按文件名
  台词自动对号分发到各镜、参考图挂全部分镜；未命中台词的段会在结果里列出，须转告用户确认；
- 只挂某一镜时带 index。用户上传了素材（消息【本次附件】或 get_project_state 的 recent_uploads）
  却没挂载时，**必须主动挂载/分发**，不许放着素材报缺口。
用户上传了 md/txt 剧本文档而消息里只有 URL 没有全文时，用 read_doc 读取全文再拆分镜。
用户消息末尾的【本次附件】区块是刚上传文件的清单（图/音/视频给 URL，md/txt 已内联全文），按上面规则使用。

状态与交付工具：
{{"action":"tool","tool":"query_jobs","args":{{"type":"video","status":"completed","limit":10}}}}
{{"action":"tool","tool":"get_voice_lines","args":{{"job_ids":"可选，逗号分隔；缺省查本会话全部配音"}}}}
{{"action":"tool","tool":"export_project","args":{{}}}}
{{"action":"tool","tool":"confirm_ref_remap","args":{{}}}}

**素材铁律（最高优先级，与下方任何流程冲突时以本条为准）**：
- 用户提供的素材（分镜提示词/时长/旁白/参考图/配音/音色情绪参考等）是权威输入：**原样使用**，
  禁止改写、重新拆分、合并、重新合成、重新生成或跳过不用；
- 可以探测比对（如音频实际时长 vs 设定时长、文本身份匹配），但探测结果**只用于向用户报告**；
  对用户素材及其设定的任何改动（时长/时间轴/挂载对应关系）**必须先征得用户同意**；
- 用户给的是**完整分镜稿**（每镜已有 Prompt/时长）时：create_scenes 一镜对一镜**原样录入**
  （Prompt、duration、旁白逐字复制），禁止重新创作、重新拆分或合并分镜；
- **分镜挂有用户上传素材（参考图/参考音频）且用户未提供新分镜稿时：该镜现有 Prompt/旁白/时长即用户定稿**，
  禁止 set_scene_prompt / set_scene_narration / create_scenes 重写，禁止用自拟台词重新合成配音——
  直接 generate_scene_video（系统自动放行旧 Prompt 并沿用用户素材）；
- 重写用户分镜稿时严禁省略或截断：新 Prompt 必须包含原稿全部分节，长度明显短于原稿会被系统拒绝；
- 重拆使用户素材的挂载对应关系发生变化时，系统会锁定生成（ref_remap_pending）：
  先与用户逐镜确认，用户确认后调用 confirm_ref_remap 解锁再继续。

说明：
-1. **纯问答/分析/建议**（用户在提问、求评价、要建议、改文案思路、闲聊，而不是要求产出物时）：
    不调用任何生成/修改类工具；若问题涉及当前项目、剧本或分镜（如"剧本有什么问题"），
    先调用 get_project_state 获取项目全貌再回答；分析剧本时可再 load_skill「screenwriting」提高质量。
    然后直接 finish：{{"action":"finish","summary":"<给用户看的完整回答，禁止为空、禁止概括为'已完成'>"}}。
    summary 就是聊天回复本身，必须把分析/建议的完整内容写在里面。
0. **纯配音任务**（用户给了剧本/旁白，只要求配音、不要求拆分镜或做视频时）：禁止走 create_scenes 分镜流程，
   必须调用 generate_voices_batch，**首选 lines 结构化数组**：把剧本整理为每行一个对象
   {{"text":"...","voice":"...","emotion":"..."}}——text 必须**逐字复制**剧本原文的一行
   （**严禁拆句、合并、增删改写任何文字**，行数必须等于剧本行数）；剧本中标注的音色/情绪说明
   （如"这句用开心的女声"）提取到 voice / emotion 字段（中文情绪词可直接给，如 开心/悲伤/惊讶），
   没有标注的字段省略。只有剧本完全无法结构化时才用 script 字段兜底（原文逐字，每行一句）。
   **重做规则：用户要求"重新生成/重配/换音色重做/不要用之前的"时，必须传 "force":true
   禁用断点接续，全部重新生成；只有用户明确接受续传（或未提及）时才默认沿用旧配音。**
   然后直接 finish（进度由界面实时显示，不要逐句 generate_voice、不要 wait_job 空等）。
   finish 回复中必须附上打包下载链接（原文逐字给出，界面会渲染成可点击链接）：
   http://127.0.0.1:8000/api/jobs/batch-download/zip?type=tts&limit=100
0a. **自定义音色（小米 voicedesign 与固化）**：
   - 用户要求特殊嗓音/特定音色（如"低沉沙哑的男声""御姐音""像个老教授"）且能力列表里有
     mimo-v2.5-tts-voicedesign 时：model 用 mimo-v2.5-tts-voicedesign，voice 填音色描述
     （1-4 句：性别年龄、音色质感、情绪语气、语速节奏；避免矛盾特征和"普通的"这类模糊词）；
   - **voicedesign 同一描述每次生成的音色都会不同**：设计满意后必须立即调用 save_voice
     （传该次配音的 job_id）把这一条音频固化为音色资产，并转告用户可试听确认；
   - 之后的配音统一用固化的音色名（或其 ID）作 voice 参数——系统自动走克隆协议稳定复用，
     **禁止再用同一段描述反复 voicedesign**（音色会漂移，前后不一致）；
   - 能力列表的「固化音色」里已有的音色直接按名使用，不要重复设计同名音色；
   - 用户要求换音色时：重新 voicedesign 出新样本 → save_voice 起个新名字 → 用新音色名重配。
0b. **纯生图任务**（用户只要求生成图片/画几张图/出概念图，没提分镜、旁白、视频成片时）：
   禁止走 create_scenes 分镜流程、禁止 generate_scene_voice；
   先 load_skill 加载「image-prompt-writing」，把用户描述按技能规则转成生图提示词，
   然后对每张图调用一次 generate_image（不同画面就改 prompt，同画面多张就只调一次），
   完成后 wait_job 或直接 finish 告知任务在后台出图即可。
   只有用户明确要求视频成片/分镜/配音时才进入下面的第 6 条分镜编排流程。
1. **写或改视频 Prompt 之前，必须先 load_skill 加载「h3-prompt-writing」技能**（涉及镜头设计可再加载「cinematography」），加载后按技能知识撰写，质量显著更高。
1a. **风格化成片任务**（3D 动画短片/品牌宣传片/极简产品广告/MV 字幕视频/双人游戏开场/纸艺定格讲解/纸拼贴讲解/手绘实景混合）：
    先 load_skill 加载对应风格技能，严格按技能的分步工作流执行（技能提到的参考文件用 file 参数读取）；
    技能要求"确认图/审批"时，用 generate_scene_image 出图并在回复中向用户展示等待确认，不要跳过；
    技能与下方第 6 条流程冲突时，以风格技能的工作流为准。
2. set_prompt / set_description / set_narration 会直接写入用户当前场景。
3. generate_image 若不给 prompt 则使用当前 Prompt；generate_voice 若不给 text 则使用当前旁白。
4. generate_video 基于当前 Prompt 生成；with_image=true 时场景图片作为参考图传入（当前默认工作流必须带参考图）。
5. wait_job 最多等 25 秒；超时未完成就继续下一步或 finish，告知用户任务已在后台继续。
6. **分镜编排流程**（用户给出剧本/创意、要求拆分镜并出视频时按此顺序。你的职责是 a-c 的**创作录入**，
   之后的生成由 d 条的系统流水线接管，不归你管）：
   a. 先 load_skill 加载「h3-prompt-writing」+「screenwriting」（涉及镜头设计再加「cinematography」）：
      screenwriting 决定分镜结构与节奏（钩子/信息曲线/转场/旁白），h3-prompt-writing 决定每镜 Prompt 的写法。
      **拆分镜质量必须过 screenwriting 的分镜自检清单**：分镜 0 两秒内出钩子、单镜单知识点、
      每镜有画面变化驱动力、相邻镜有转场设计（动势/相似形/声音先入/情绪对比）、旁白短句口语化、
      结尾给行动出口、总时长 30-45s（竖屏科普 6-9 镜）——禁止平淡的流水账拆分；
      **长短剧例外**：用户明确要长片（如 10 分钟短剧）时，总时长与镜数由叙事决定（每镜 3-5s，最多 200 镜），
      不适用 30-45s 限制；**超过 25 镜时分批调用 create_scenes**：每批 15-25 镜（一批装不下会被输出长度截断），
      第一批正常调用，后续批次加 "append":true 追加（角色设定卡每批都要带，会自动合并）；
   b. **角色设定卡先行**：拆分镜前为每个出场角色写设定卡（30-60 字外观描述：体型比例/颜色/材质/服装/标志特征），
      作为 create_scenes 的 characters 字段传入；
      **豁免**：分镜已挂载用户参考图（get_project_state 该镜 has_ref_images=true）时跳过设定卡——
      角色外观以参考图为准（视频模型直接吃图，Ref2VA），禁止凭空另写外观描述文字；
      一致性铁律：每个分镜 prompt 的 Subject 段必须逐字复述所用角色的设定卡特征（禁止只写角色名）；
   c. 拆分镜并调用 create_scenes，每镜字段：
      **用户已给完整分镜稿时逐字录入**（Prompt/duration/旁白照抄，镜数一致，不重拆不合并），
      以下字段要求适用于 agent 自行创作分镜的场景：
      narration（口语化短句）、prompt（完整 H3 结构化 Prompt，Subject 复述设定卡）、
      **gen_mode**（"i2v"=图生视频，凡有角色/明确主体出场的镜头一律用 i2v；"t2v"=文生视频，仅抽象/纯环境镜头）、
      **keyframe_prompt**（i2v 镜必填：该镜关键帧图描述=构图+景别+角色姿态+光线）；
      **时间轴铁律：每镜 Action Timeline 以该镜首帧为 0 时刻**（如 0-2s/2-4s，各段之和=该镜 duration），
      禁止沿用剧本全局时刻（如 3-7s）——每镜是独立生成的视频，全局时刻会导致前几秒空白；
      **参考标签编号铁律：<Picture/Video/Audio N> 按该镜实际传入的素材从 1 连续编号**——
      每镜是独立请求，只带该镜自己的素材，严禁全片全局编号（第 k 镜写 <Audio k> 是典型错误：
      该镜只传 1 段音频时标签悬空，模型会自行编造语音，配音与台词完全错乱）；
      初始 duration 按旁白字数粗估（约每秒 4 字），**用户分镜稿已标每镜时长的逐字沿用，禁止改写**；
   d. **生成由系统流水线接管（重要——你的职责到「分镜数据写好」为止）**：
      create_scenes（或逐镜写入）完成后，若用户要求的是生成视频，直接 finish 告知
      "分镜已录入，系统将自动逐镜生成"——关键帧生成（用你写好的 keyframe_prompt）、
      旁白合成、音频与分镜文案的匹配、参考图传入、视频提交、时长收敛全部由代码执行，
      **禁止逐镜调用 generate_scene_voice / generate_scene_image / generate_scene_video
      编排生成**（用户点名只要某一镜单独操作时除外）；
      上传音频的用途以用户分镜稿/提示词的说明为准：写明是某镜配音的无需采用动作
      （流水线按旁白文案自动匹配传入，超 3 段自动挑选），写明是音色/情绪参考的不做处理；
      **用户没说明用途时禁止瞎猜**：先 finish 主动询问；
      需要合成配音的创作分镜（Agent 自写旁白）在 create_scenes 时把 narration 写好即可；
      用户明确要求单独重配某镜时才用 generate_scene_voice（重配传 "force":true）。
   e. finish 总结：分镜数、角色、旁白录入情况，并告知系统会自动生成视频（或已生成）。
   f. 全部分镜视频完成后，若用户要成片/导出/合成完整视频，调用 export_project
      （按顺序拼接全部有视频的分镜并混入旁白；缺视频的分镜会跳过并在结果中提示）。
   注意：**剧本一致性铁律——用户给出的剧本必须先落到分镜数据里，才能生成视频**。
   若用户给了新剧本/改稿，必须先用 create_scenes 把新剧本写入分镜，严禁对旧 Prompt 直接生成。
   相反，用户只是要求"用现有分镜/素材生成视频"且未给新内容时，不要重写分镜（系统直接走流水线）。
7. **素材图生成**（用户要求根据剧本生成素材图/关键帧/角色设定图时）：
   {{"action":"tool","tool":"generate_scene_image","args":{{"index":0,"prompt":"可选，覆盖素材图描述","reference_index":null}}}}
   - generate_scene_image 为指定分镜生成素材图/关键帧并写入该场景（内联等待，约 10-60 秒）。
   - 一致性技巧：先生成分镜 0 的角色/场景设定图，后续分镜用 reference_index=0 引用它，保证角色长相一致。
   - 若用户要求"根据剧本生成素材图"，在 create_scenes 之后：分镜 0 先生成设定图，其余分镜带 reference_index=0 逐个生成。
8. 用用户的语言。只输出 JSON，不输出任何其他文字。

可用技能（load_skill 的 name 参数）：
{skills}

当前已配置的能力（重要——以下是你真实拥有的能力，用户询问时如实告知，不要声称没有权限）：
- TTS 语音合成：{tts_models}
- 可用音色：{voices}
- 固化音色（已保存样本，稳定复用，配音时优先使用，voice 参数用其名称或ID）：{fixed_voices}
- 生图模型：{image_models}
- 视频模型：{video_models}
若某能力列表为空，如实回复"该能力尚未配置，请到设置中添加对应的 API Key / 模型"，并继续完成其余能做的部分。

当前上下文（页面/场景/项目）：
{context}"""


_EMOTION_MAP = {
    "平静": "neutral", "中性": "neutral", "自然": "neutral", "neutral": "neutral",
    "开心": "happy", "高兴": "happy", "喜悦": "happy", "欢快": "happy", "happy": "happy",
    "悲伤": "sad", "伤心": "sad", "难过": "sad", "低落": "sad", "sad": "sad",
    "愤怒": "angry", "生气": "angry", "气愤": "angry", "angry": "angry",
    "恐惧": "fearful", "害怕": "fearful", "紧张": "fearful", "fearful": "fearful",
    "惊讶": "surprised", "吃惊": "surprised", "意外": "surprised", "surprised": "surprised",
    "沉着": "calm", "沉稳": "calm", "温柔": "calm", "平静沉稳": "calm", "calm": "calm",
}


def _norm_emotion(e) -> str | None:
    """把中文/英文情绪词统一为 TTS 接口值；无法识别时返回 None（用默认 neutral）。"""
    if not e:
        return None
    key = str(e).strip().lower()
    return _EMOTION_MAP.get(key) or _EMOTION_MAP.get(str(e).strip())


def _has_xiaomi_key() -> bool:
    from ..config.key_manager import list_keys

    return any(
        k.get("provider") == "xiaomi" or "xiaomimimo" in (k.get("base_url") or "").lower()
        for k in list_keys()
    )


def _tts_provider_for_model(model: str) -> str:
    """按模型 ID 判定 TTS 协议厂商：mimo-* 走小米，其余走 MiniMax/通用协议。"""
    return "xiaomi" if model.strip().lower().startswith("mimo-") else "minimax"


def _default_tts_setup() -> tuple[str, str, str]:
    """解析默认 TTS (provider, model, voice)：MiniMax 自定义模型优先，其次小米。

    小米 MiMo 的模型 ID / 预置音色固定，配了 Key 即可用（无需先添加自定义模型）。
    """
    from ..providers.tts.xiaomi_tts import XIAOMI_PRESET_VOICES

    custom = model_store.list_custom()
    m = next((m["id"] for m in custom.get("tts", []) if m.get("provider") == "minimax"), "")
    if m:
        return "minimax", m, next((v["id"] for v in custom.get("voice", [])), "female-shaonv")
    m = next((m["id"] for m in custom.get("tts", []) if m.get("provider") == "xiaomi"), "")
    if not m and _has_xiaomi_key():
        m = "mimo-v2.5-tts"
    if m:
        # 默认音色优先固化资产（稳定），其次自定义音色里的预置 ID，最后官方默认
        clone_asset = next((a for a in voice_assets.list_assets() if a["kind"] == "clone"), None)
        if clone_asset:
            return "xiaomi", m, clone_asset["id"]
        presets = {v["id"] for v in XIAOMI_PRESET_VOICES}
        voice = next((v["id"] for v in custom.get("voice", []) if v["id"] in presets), "冰糖")
        return "xiaomi", m, voice
    return "minimax", "", next((v["id"] for v in custom.get("voice", [])), "female-shaonv")


def _prompt_fingerprint(text: str) -> str:
    """分镜 Prompt 指纹：视频提交与写回时比对，防止分镜被改写后旧视频串进新剧本。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def writeback_video_to_scene(project_id: str | None, video_url: str, idx: int,
                             duration: int, fingerprint: str = "") -> str | None:
    """把已完成的视频写回项目分镜（指纹校验）。Agent 写回与重启恢复共用。

    返回 None 表示写回成功；返回字符串说明未写回的原因（调用方转告用户）。
    """
    try:
        if not project_id:
            return "项目已不存在，无法写回"
        project = project_service.get_project(project_id)
        if not project:
            return f"项目 {project_id} 已被删除，无法写回"
        scenes = project.get("scenes", [])
        if not (0 <= idx < len(scenes)):
            return f"分镜 {idx} 已不存在（现有 {len(scenes)} 镜），无法写回"
        current_fp = _prompt_fingerprint(scenes[idx].get("prompt") or "")
        if fingerprint and current_fp != fingerprint:
            return (
                f"未写回分镜 {idx}：该分镜 Prompt 在视频生成期间已被改写"
                f"（提交时指纹 {fingerprint}，当前 {current_fp}），视频属于旧 Prompt"
            )
        # 只更新目标分镜的 video/duration，其余字段以最新文件为准
        scenes[idx]["video"] = video_url
        scenes[idx]["duration"] = duration
        project_service.save_project(project_id, {**project, "scenes": scenes})
        return None
    except Exception as exc:  # noqa: BLE001
        return f"写回分镜 {idx} 失败：{str(exc)[:150]}"


def _norm_tts_text(t: str) -> str:
    """配音文本身份归一化：去空白与标点，只比内容。"""
    return re.sub(r"[\s，。？！,.?!…—·、：:；;\"'（）()\[\]{}]+", "", t or "")


def _audio_semantic(url: str) -> str:
    """从上传音频文件名提取语义文本（用户上传名自带台词前缀，如 xxxx_tts_085821_哎呀！原来…）。"""
    name = (url or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "_" in name:
        name = name.rsplit("_", 1)[-1]
    return "".join(re.findall(r"[\u4e00-\ufaffA-Za-z0-9]+", name))


def _dedupe_same_text_audios(audios: list[str]) -> tuple[list[str], list[str]]:
    """同台词多份拷贝只留第一个（用户整包重传是常态：9 句 × 6 拷贝 = 54 文件）。

    多份同台词音频传给工作流会被当成多段台词逐段烧进成片——同一句说两遍的直接
    来源之一。文件名提不出台词文本的按 URL 身份去重，不误伤。
    """
    seen: set[str] = set()
    kept, dropped = [], []
    for u in audios:
        key = _norm_tts_text(_audio_semantic(u)) or u
        if key in seen:
            dropped.append(u)
        else:
            kept.append(u)
            seen.add(key)
    return kept, dropped


def _sort_audios_by_prompt(audios: list[str], scene: dict) -> list[str]:
    """按台词文本在分镜 Prompt 里首次出现的位置排序（稳定排序，未命中排最后）。

    Prompt 的 <Audio N> / Audio Timeline 按台词时间顺序编号，ref_audio_N 却按传入
    顺序平铺——错位即张冠李戴（复现事故：分镜3 把小墨的音频烧进医生 0-7s 的时间槽，
    成片台词重复+乱语）。挂载顺序来自 Agent 工具参数、不可靠，提交前以 Prompt 文本
    位置为准做确定性对齐。
    """
    hay = _norm_tts_text(str(scene.get("prompt") or "") + str(scene.get("narration") or ""))

    def _key(item: tuple[int, str]) -> tuple[int, int, int]:
        i, u = item
        t = _norm_tts_text(_audio_semantic(u))
        pos = hay.find(t) if (t and len(t) >= 4 and hay) else -1
        return (1, 0, i) if pos < 0 else (0, pos, i)

    return [u for _, u in sorted(enumerate(audios), key=_key)]


def _asset_name_tokens(url: str) -> list[str]:
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


def _sort_ref_assets_by_prompt(urls: list[str], scene: dict, label: str) -> list[str]:
    """参考图/参考视频按 Prompt 的 <Picture N> / <Video N> 声明顺序对齐（稳定排序）。

    ref_image_N / ref_video_N 按传入顺序平铺，身份绑定却写在声明行里——错位即
    张冠李戴（<Picture 1> 声明是医生，ref_image_0 却传了小墨的图）。同一编号的
    声明行可能多处出现（英文 subject_definitions + 中文补充段），按 N 合并后匹配：
    文件名身份 token 出现在哪个编号的声明文本里就排第 N 位；文件名无语义（uuid
    命名）时若素材数与声明编号数一致，用消元法把无信号素材按原相对顺序补进空缺
    编号——绝不因为"认不出"就把已挂对的顺序打乱。两类信号都没有的保持原序排在末尾。
    """
    prompt = str(scene.get("prompt") or "")
    decl: dict[int, str] = {}
    for n, line in re.findall(rf"<{label}\s+(\d+)>([^<\n]*)", prompt):
        k = int(n)
        decl[k] = f"{decl.get(k, '')} {line}"
    if not decl or not urls:
        return list(urls)
    norm = {n: _norm_tts_text(t).lower() for n, t in decl.items()}
    rank: dict[str, int | None] = {}
    for u in urls:
        toks = [t for t in _asset_name_tokens(u) if len(t) >= 2]
        rank[u] = min(
            (n for n, nl in norm.items() if any(t in nl for t in toks)),
            default=None,
        )
    bound = [u for u in urls if rank[u] is not None]
    unbound = [u for u in urls if rank[u] is None]
    # 消元补位：素材数 == 声明编号数 且空缺数 == 无信号数 时，按原相对顺序对号空缺编号
    if len(urls) == len(decl) and len(unbound) == len(decl) - len(set(rank[u] for u in bound)):
        free_ns = sorted(set(decl) - {rank[u] for u in bound})
        for u, n in zip(unbound, free_ns):
            rank[u] = n
        bound, unbound = urls, []
    return sorted(bound, key=lambda u: (rank[u], urls.index(u))) + unbound


def _pick_matching_audios(audios: list[str], scene: dict, limit: int = 3) -> tuple[list[str], list[str]]:
    """从挂载的多段上传音频里挑与该镜旁白文案匹配的（≤limit 段）。

    返回 (选中, 落选)。匹配依据：文件名里的台词文本出现在该镜 Prompt（Audio Timeline
    逐字引用台词）或旁白里。全部不匹配时退回前 limit 段——绝不静默丢弃，调用方需转告用户。
    选中结果先按台词在 Prompt 里的出现顺序对齐再截断，保证 <Audio N> 编号与
    ref_audio_N 传入顺序一致。
    """
    hay = _norm_tts_text(str(scene.get("prompt") or "") + str(scene.get("narration") or ""))
    matched, rest = [], []
    for u in audios:
        t = _norm_tts_text(_audio_semantic(u))
        (matched if (t and len(t) >= 4 and t in hay) else rest).append(u)
    unique_matched, _ = _dedupe_same_text_audios(matched)
    pool = unique_matched or _dedupe_same_text_audios(audios)[0]
    picked = _sort_audios_by_prompt(pool, scene)[:limit]
    dropped = [u for u in audios if u not in picked]
    return picked, dropped


def _distribute_audios(audios: list[str], scenes: list[dict]) -> tuple[dict[int, list[str]], list[str], list[int]]:
    """把整条片的配音段按台词对号分到各镜（项目级素材分发的核心）。

    用户的心智模型是「这些配音是整条片的」，不是「每镜分别上传」。上传文件名自带
    台词前缀（如 xxx_tts_085821_哎呀！原来…），台词文本出现在哪一镜的 Prompt
    （Audio Timeline 逐字引用台词）或旁白里就分给哪镜，每镜上限 3 段（工作流硬限）。
    返回 (分镜序号→音频列表, 未命中音频, 超过 3 段被截掉的镜序号)。未命中的绝不
    瞎塞给某镜——返回给上层报告用户定夺。
    """
    claims: dict[int, list[str]] = {}
    seen_texts: dict[int, set[str]] = {}
    unmatched: list[str] = []
    overflow: set[int] = set()
    for u in audios:
        t = _norm_tts_text(_audio_semantic(u))
        if not t or len(t) < 4:
            unmatched.append(u)
            continue
        hit = next(
            (i for i, s in enumerate(scenes)
             if t in _norm_tts_text(str(s.get("prompt") or "") + str(s.get("narration") or ""))),
            None,
        )
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
    # 每镜桶内按 Prompt 台词顺序对齐（<Audio N> 编号 ↔ ref_audio_N 传入顺序必须一致）
    for i, bucket in claims.items():
        claims[i] = _sort_audios_by_prompt(bucket, scenes[i])
    return claims, unmatched, sorted(overflow)


_AUDIO_TAG_RE = re.compile(r"<Audio\s+(\d+)>")


def _audio_tags_in_order(prompt: str) -> list[int]:
    """Prompt 里的 <Audio N> 标签按首次出现顺序去重（首现顺序 = 台词时间顺序）。"""
    tags: list[int] = []
    for m in _AUDIO_TAG_RE.finditer(prompt or ""):
        n = int(m.group(1))
        if n not in tags:
            tags.append(n)
    return tags


def _renumber_audio_tags(prompt: str, audios: list[str]) -> tuple[str, str | None]:
    """把 Prompt 的 <Audio N> 编号对齐到本镜请求实际传入的音频（确定性、幂等）。

    H3 参考标签按**单次请求实际传入的素材**编号（各品类独立、从 1 起）；分镜 Prompt
    却常沿用全片全局编号（第 k 镜引用 <Audio k>），而每镜请求只带该镜自己的音频——
    悬空标签（编号 > 实际传入数）让模型找不到指定素材，转而自行编造语音，成片配音
    与台词完全对不上（复现事故：吕满春项目 4 镜全部全局编号，第 2/3/4 镜配音错乱）。
    传入音频已按台词在 Prompt 中的位置排序（_sort_audios_by_prompt），故按首现顺序
    第 i 个标签 ↔ 第 i 段音频：统一重编为 <Audio 1..K>。已正确时不改动。
    返回（新 Prompt，变更说明或 None）。标签数多于传入音频数的情形由调用方拦截。
    """
    tags = _audio_tags_in_order(prompt)
    if not tags or not audios:
        return prompt, None
    mapping = {n: i + 1 for i, n in enumerate(tags)}
    if all(mapping[n] == n for n in tags):
        return prompt, None
    new_prompt = _AUDIO_TAG_RE.sub(lambda m: f"<Audio {mapping[int(m.group(1))]}>", prompt)
    changed = "、".join(
        f"<Audio {n}>→<Audio {mapping[n]}>" for n in tags if mapping[n] != n
    )
    return new_prompt, f"Prompt 音频标签为全片全局编号（{changed}），已按本镜实际传入的 {len(audios)} 段音频重排"


def _assemble_video_request(scene: dict, vmodel: dict) -> dict:
    """纯函数：从分镜状态组装视频生成请求（mode/images/参考素材/契约校验）。

    Agent 工具与确定性流水线共用这一份组装逻辑，防止两套实现漂移。
    """
    ref_images = _sort_ref_assets_by_prompt(
        [v for v in scene.get("ref_images") or [] if isinstance(v, str)], scene, "Picture")
    ref_videos = _sort_ref_assets_by_prompt(
        [v for v in scene.get("ref_videos") or [] if isinstance(v, str)], scene, "Video")
    all_ref_audios = [a for a in scene.get("ref_audios") or [] if isinstance(a, str)]
    # 工作流参考音频上限 3 段：用户把整条片的配音全挂在一镜时（复现事故：9 段全挂分镜0，
    # 视频生成永远失败且 Agent 无工具可解），按该镜旁白文案自动挑选匹配段，其余不传入。
    # 同台词多份拷贝只传一段（多传会在成片里把同一句烧两遍）
    unique_audios, dupe_audios = _dedupe_same_text_audios(all_ref_audios)
    audio_selection_note = ""
    if len(unique_audios) > 3:
        ref_audios, dropped_audios = _pick_matching_audios(unique_audios, scene, limit=3)
        kept_names = "、".join(u.rsplit("/", 1)[-1] for u in ref_audios)
        audio_selection_note = (
            f"该镜挂有 {len(all_ref_audios)} 段上传音频（超出工作流上限 3 段），"
            f"已按旁白文案自动挑选 {len(ref_audios)} 段传入：{kept_names}；"
            f"其余 {len(dropped_audios)} 段保留在分镜上未传入——必须在总结中告知用户。"
        )
    else:
        ref_audios = _sort_audios_by_prompt(unique_audios, scene)
    if dupe_audios:
        audio_selection_note += (
            f"；{len(dupe_audios)} 段与已选音频同台词的重复文件未传入"
            f"（{ '、'.join(u.rsplit('/', 1)[-1] for u in dupe_audios[:3]) }"
            f"{'等' if len(dupe_audios) > 3 else ''}——同台词只传一段，多传成片会把同一句烧两遍）"
        )
    has_keyframe = bool(scene.get("image"))
    has_refs = bool(ref_images or ref_videos or ref_audios)
    gen_mode = str(scene.get("gen_mode") or ("i2v" if has_keyframe else "t2v")).lower()
    if gen_mode == "i2v":
        if has_keyframe:
            # 官方 V2 接口禁止首帧与参考素材混传：带参考素材时整镜走参考模式，
            # 关键帧也作为参考图之一（不再作首帧）
            images = [scene["image"], *ref_images]
            mode = "reference" if has_refs else "image"
        elif has_refs:
            # 无关键帧但挂了用户参考素材：参考模式直接吃用户图，不强制先生成关键帧——
            # 复现事故：模型为走 i2v 多生成了关键帧，反而引入与用户画风不一致的风险
            images = list(ref_images)
            mode = "reference"
        else:
            return {"contract_error": "i2v 镜既没有关键帧也没有参考素材：请先生成关键帧（generate_scene_image）或挂用户参考图"}
    else:
        images = list(ref_images)
        mode = "reference" if has_refs else "text"
    # 参考标签编号契约：H3 标签按单次请求实际传入的素材编号，Prompt 沿用全片全局编号
    # 会产生悬空标签（配音错乱事故的直接根因）。音频按台词位置重编号修复；
    # 图片/视频标签错位无法确定性修复（身份绑定写在声明行里），只能拦截。
    prompt = str(scene.get("prompt") or "")
    tag_error = ""
    audio_tags = _audio_tags_in_order(prompt)
    if audio_tags and not ref_audios:
        tag_error = (
            f"Prompt 引用了 {'/'.join(f'<Audio {t}>' for t in audio_tags)} 标签，但本镜没有音频随请求传入"
            "（未挂载上传音频）：悬空标签会让模型自行编造语音——请挂载对应音频，或去掉 Prompt 里的音频标签"
        )
    elif len(audio_tags) > len(ref_audios):
        tag_error = (
            f"Prompt 引用了 {len(audio_tags)} 个音频标签（<Audio {'/'.join(str(t) for t in audio_tags)}>），"
            f"但本镜只有 {len(ref_audios)} 段音频可传入：多余标签指向不存在的素材，"
            "模型会自行编造缺失的语音——请补挂音频或改写 Prompt 只引用本镜实际传入的音频"
        )
    else:
        prompt, tag_note = _renumber_audio_tags(prompt, ref_audios)
        if tag_note:
            audio_selection_note += f"；{tag_note}"
    if not tag_error:
        for label, passed in (("Picture", images), ("Video", ref_videos)):
            nums = [int(n) for n in re.findall(rf"<{label}\s+(\d+)>", prompt)]
            if nums and max(nums) > len(passed):
                tag_error = (
                    f"Prompt 引用了 <{label} {max(nums)}> 标签，但本镜只传入 {len(passed)} 个参考"
                    f"{'图' if label == 'Picture' else '视频'}：悬空标签会让模型找不到指定素材——"
                    "请核对参考素材挂载与 Prompt 声明"
                )
                break
    contract_error = tag_error or video_service.autodl_contract_error(
        vmodel["id"], mode, images, ref_videos, ref_audios, prompt=prompt
    )
    return {
        "mode": mode, "images": images, "ref_images": ref_images, "ref_videos": ref_videos,
        "ref_audios": ref_audios, "all_ref_audios": all_ref_audios, "prompt": prompt,
        "contract_error": contract_error, "audio_selection_note": audio_selection_note,
    }


def _submit_scene_video(agent: "_Agent", scene: dict, idx: int, wanted_duration=None) -> dict:
    """提交一镜视频生成（Agent 工具与流水线共用的核心）。

    Prompt 从项目分镜里原样读取——用户内容只在项目文件与任务参数之间流动，
    永远不经过任何模型的输出通道（这是对"复述截断/擅自改写"事故的架构级修复）。
    唯一的确定性改写是 _assemble_video_request 里的 <Audio N> 标签重编号
    （编号对齐本镜实际传入素材，防悬空标签导致配音错乱），项目文件不被改动。
    """
    vmodel = settings.VIDEO_MODELS[0]
    # 按模型官方时长范围收敛（如 H3 为 4-15s）——模型硬限制无法绕过，
    # 但用户设定被改动必须显式告知，不允许静默钳制
    dur_min, dur_max = min(vmodel["durations"]), max(vmodel["durations"])
    wanted = int(wanted_duration or scene.get("duration") or 5)
    duration = min(max(wanted, dur_min), dur_max)
    project = agent._load_project() or {}
    aspect = agent.context.get("aspect_ratio") or project.get("aspect_ratio") or "16:9"
    req = _assemble_video_request(scene, vmodel)
    if req["contract_error"]:
        return {"ok": False, "error": f"当前视频工作流「{vmodel['label']}」：{req['contract_error']}"}
    # 提交用重编后的 Prompt（音频标签已对齐本镜实际传入素材）；
    # 指纹仍取项目原始 Prompt——写回校验比对的是「分镜是否被改写」，而非提交稿
    prompt = req["prompt"]
    fingerprint = _prompt_fingerprint(scene.get("prompt") or "")
    job = job_manager.create("video", vmodel["provider"], vmodel["id"], {
        "mode": req["mode"],
        "prompt": str(prompt)[:300],
        "duration": duration,
        "aspect_ratio": aspect,
        "model": vmodel["id"],
        "refs": len(req["images"]) + len(req["ref_videos"]) + len(req["ref_audios"]),
        "scene_index": idx,
        "project_id": agent.project_id,
        "prompt_fingerprint": fingerprint,
    })
    job_manager.attach(
        job["id"],
        video_service.run_video_job(
            job_id=job["id"], provider=vmodel["provider"], model=vmodel["id"],
            credential_id=None, mode=req["mode"], prompt=str(prompt),
            duration=duration, aspect_ratio=aspect, resolution=None,
            images=req["images"], last_images=[], videos=req["ref_videos"], audios=req["ref_audios"],
            options={},
        ),
    )
    agent.jobs.append({"job_id": job["id"], "type": "video", "scene_index": idx,
                       "duration": duration, "project_id": agent.project_id})
    # 防导出双混音：对白音频已随参考模式传入视频请求（H3 会烧进成片）时，
    # 清掉残留的 agent 合成旁白（outputs/ 产物）；用户上传的配音（uploads/）不动，留给用户定夺
    voice_cleared = False
    if req["ref_audios"] and str(scene.get("voice") or "").startswith("/api/files/outputs/"):
        latest = agent._load_project() or {}
        latest_scenes = latest.get("scenes") or []
        if 0 <= idx < len(latest_scenes) and latest_scenes[idx].get("voice") == scene.get("voice"):
            latest_scenes[idx]["voice"] = None
            agent._save_project_scenes(latest_scenes)
            voice_cleared = True
    # 后端兜底写回：前端关页也不丢视频结果（写回时重新读项目文件，
    # 比对 Prompt 指纹后才落盘，分镜被改写则放弃写回）
    asyncio.create_task(agent._writeback_scene_video(job["id"], idx, duration, fingerprint))
    note = "后台生成中，无需等待"
    if req["audio_selection_note"]:
        note += f"；{req['audio_selection_note']}"
    if voice_cleared:
        note += "；已清掉旧的合成旁白（对白音频本次已随视频请求传入，避免导出时双混音）"
    if wanted != duration:
        note += (
            f"；⚠️ 该镜设定时长 {wanted}s 超出模型「{vmodel['label']}」支持范围"
            f"（{dur_min}-{dur_max}s），已按 {duration}s 提交——模型硬限制，请在总结中告知用户"
        )
    return {"ok": True, "job_id": job["id"], "scene_index": idx, "note": note}


def _find_completed_tts(text: str) -> dict | None:
    """按文本身份查找已完成的配音任务（agent 生成的配音以文本为身份，重跑沿用不重复合成）。

    历史任务 params 只存了文本前缀（旧 120 字），用"归一化前缀一致且长度足够"兼容截断存储。
    """
    target = _norm_tts_text(text)
    if not target:
        return None
    need = min(len(target), 100)
    for j in job_manager.list(jtype="tts", status="completed", limit=5000):
        if not j.get("output"):
            continue
        ptext = _norm_tts_text(str((j.get("params") or {}).get("text") or ""))
        if not ptext:
            continue
        if target == ptext or (len(ptext) >= need and target.startswith(ptext)):
            return j
    return None


def _repair_json_literals(text: str) -> str:
    """把 JSON 字符串值里的真实换行/回车/制表符转成合法转义——模型经常直接输出多行文本。"""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch in ("\n", "\r", "\t"):
            out.append("\\n" if ch == "\n" else ("\\r" if ch == "\r" else "\\t"))
            continue
        out.append(ch)
    return "".join(out)


class _Agent:
    def __init__(self, context: dict, goal: str = ""):
        self.context = context or {}
        self.goal = goal or ""  # 本次用户消息：防串剧本拦截与静音镜提示的判别依据
        self.updates: dict = {}
        self.jobs: list[dict] = []
        self.steps: list[dict] = []
        self.exports: list[str] = []
        self.voice_reused: list[str] = []  # 断点接续沿用的历史配音任务 id
        self.project_id: str | None = self.context.get("project_id") or None
        self.project_changed = False
        # 本次运行中被写入过 Prompt 的分镜序号：generate_scene_video 防串旧剧本的依据
        self.prompt_touched: set[int] = set()

    def _load_project(self) -> dict | None:
        if not self.project_id:
            return None
        return project_service.get_project(self.project_id)

    def _save_project_scenes(self, scenes: list[dict]) -> None:
        if not self.project_id:
            raise RuntimeError("当前没有打开的项目，无法写入分镜")
        existing = self._load_project() or {}
        project_service.save_project(self.project_id, {**existing, "scenes": scenes})
        self.project_changed = True

    async def _writeback_scene_video(self, job_id: str, idx: int, duration: int,
                                     fingerprint: str = "") -> None:
        """轮询视频任务直到终态，把结果写回项目文件（前端不在也能落盘）。

        写回前重新读取项目文件并比对 Prompt 指纹：分镜在视频生成期间被改写
        （换剧本/换 Prompt）时，旧 Prompt 生成的视频绝不写入新分镜。
        """
        waited = 0.0
        while waited < 1200:  # 最长等 20 分钟
            j = job_manager.get(job_id) or {}
            if j.get("status") in ("completed", "failed"):
                break
            await asyncio.sleep(5)
            waited += 5
        j = job_manager.get(job_id) or {}
        if j.get("status") != "completed" or not j.get("output"):
            return
        note = writeback_video_to_scene(self.project_id, j["output"], idx, duration, fingerprint)
        if note:
            job_manager.update(job_id, writeback_note=note)
            self.steps.append({
                "step": len(self.steps) + 1,
                "type": "note",
                "text": f"分镜 {idx} 的视频已生成但未写入分镜：生成期间分镜 Prompt 被改写，视频对应旧剧本。可在任务历史中查看该视频。",
            })

    def _scene(self, key: str) -> str:
        return (
            self.updates.get(key)
            or self.context.get(key)
            or ""
        )

    def _ref_remap_blocked(self) -> str | None:
        """素材对应关系待确认时锁定生成：重拆分镜动过用户素材的挂载，未获用户确认前禁止烧钱。"""
        project = self._load_project()
        if project and project.get("ref_remap_pending"):
            return (
                "项目处于「用户素材对应关系待确认」状态：分镜被重拆且旧分镜挂有用户上传素材，"
                "按序号迁移可能张冠李戴（参考 get_project_state 各镜的 ref_audio_files/ref_image_files "
                "与 narration_head）。必须先 finish 与用户逐镜确认对应关系：一致则调用 confirm_ref_remap "
                "解锁后继续；不一致请用户在 Studio 调整挂载后再继续。确认前禁止生成配音/图片/视频。"
            )
        return None

    async def generate_scene_image(self, args: dict) -> dict:
        """为指定分镜生成素材图/关键帧（内联等待完成），可引用其他分镜的图保持角色一致。"""
        blocked = self._ref_remap_blocked()
        if blocked:
            return {"ok": False, "error": blocked}
        project = self._load_project()
        if not project:
            return {"ok": False, "error": "当前没有打开的项目"}
        scenes = project.get("scenes", [])
        try:
            idx = int(args.get("index", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "index 必须是数字"}
        if idx < 0 or idx >= len(scenes):
            return {"ok": False, "error": f"分镜序号越界（0-{len(scenes) - 1}）"}
        scene = scenes[idx]
        prompt = str(
            args.get("prompt") or scene.get("keyframe_prompt") or scene.get("description") or scene.get("prompt") or ""
        )[:2000]
        if not prompt:
            return {"ok": False, "error": f"分镜 {idx} 缺少关键帧描述（keyframe_prompt），无法生成素材图"}
        if len(prompt.strip()) < 8:
            # 占位词/过短描述（如"kf"）会生成废图——曾出现 agent 用占位符直接生图的事故
            return {
                "ok": False,
                "error": (
                    f"素材图描述过短（「{prompt.strip()[:20]}」），生成出来只会是废图。"
                    "请写完整的关键帧描述（构图+景别+主体+姿态+光线）；不确定该画什么时，先 finish 向用户询问。"
                ),
            }
        # 参考项硬校验：有角色设定卡却没有任何设定图/参考时，暂停生图并让 Agent 询问用户，
        # 而不是凭空出图导致跨镜角色不一致
        allow_no_ref = bool(args.get("allow_missing_reference"))
        chars = project.get("characters") or []
        scene_refs = scene.get("ref_images") or []
        if chars and not allow_no_ref and not scene_refs and idx != 0:
            has_any_image = any(s.get("image") for s in scenes)
            if not has_any_image:
                return {
                    "ok": False,
                    "error": (
                        "缺少参考项：项目有角色设定卡，但还没有任何已生成的设定图/参考图。"
                        "为保证跨镜角色一致，标准流程是先生成分镜 0 的角色设定图，其余分镜用 reference_index=0 引用；"
                        "若用户没有提供参考、你也不确定该镜是否需要一致性，必须先 finish 并向用户询问，禁止凭空生成。"
                        "确要无参考生成时，在 args 里加 \"allow_missing_reference\": true。"
                    ),
                }
            if args.get("reference_index") is None or str(args.get("reference_index")) == "":
                return {
                    "ok": False,
                    "error": (
                        "项目里已有已生成的图片（如分镜设定图）。为保证角色一致，该镜生图必须带 reference_index "
                        "引用最相关的已生成图（通常是分镜 0 的设定图）；确要无参考生成时，在 args 里加 "
                        "\"allow_missing_reference\": true。"
                    ),
                }
        aspect = self.context.get("aspect_ratio") or project.get("aspect_ratio") or "16:9"

        # 一致性参考：用户上传的参考图最优先（用户给的素材是权威外观锚点，
        # MiniMax 仅取第一张作 subject_reference 人像参考），reference_index 的设定图随后
        references: list[str] = [r for r in scene_refs if isinstance(r, str)]
        ref_idx = args.get("reference_index")
        if ref_idx is not None and str(ref_idx) != "":
            ri = int(ref_idx)
            if 0 <= ri < len(scenes) and scenes[ri].get("image"):
                references.append(scenes[ri]["image"])
            else:
                return {"ok": False, "error": f"分镜 {ri} 还没有已生成的图片，无法作为参考"}

        model_entry = _pick_image_model()
        job = job_manager.create("image", model_entry["provider"], model_entry["id"], {
            "prompt": prompt[:200],
            "aspect_ratio": aspect,
            "scene_index": idx,
            "scene_title": scene.get("title"),
        })
        job_manager.attach(
            job["id"],
            image_service.run_image_job(
                job_id=job["id"], provider=model_entry["provider"], model=model_entry["id"],
                credential_id=None, prompt=prompt, size=None, n=1, references=references,
            ),
        )
        # 内联等待（图片生成一般 10-60s）
        waited = 0.0
        while waited < 90:
            j = job_manager.get(job["id"])
            if j["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(2)
            waited += 2
        j = job_manager.get(job["id"]) or {}
        if j.get("status") != "completed":
            return {"ok": False, "error": f"素材图生成未完成：{j.get('error') or '超时（90s）'}", "job_id": job["id"]}
        # 直接写入后端项目文件
        scene["image"] = j["output"]
        self._save_project_scenes(scenes)
        return {
            "ok": True,
            "scene_index": idx,
            "image": j["output"],
            "self_check": j.get("self_check"),
            "reference_used": bool(references),
            "note": "素材图已写入该分镜",
        }

    async def generate_scene_voice(self, args: dict) -> dict:
        """为指定分镜落实旁白：用户上传配音 use_uploaded 直接采用（不动时长）；
        agent 合成则按音频实际时长校准分镜时长；同文本历史配音沿用不重复合成。"""
        blocked = self._ref_remap_blocked()
        if blocked:
            return {"ok": False, "error": blocked}
        project = self._load_project()
        if not project:
            return {"ok": False, "error": "当前没有打开的项目"}
        scenes = project.get("scenes", [])
        try:
            idx = int(args.get("index", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "index 必须是数字"}
        if idx < 0 or idx >= len(scenes):
            return {"ok": False, "error": f"分镜序号越界（0-{len(scenes) - 1}）"}
        scene = scenes[idx]
        text = str(args.get("text") or scene.get("narration") or "")[:20000]
        uploaded = [a for a in (scene.get("ref_audios") or []) if isinstance(a, str)]
        # 上传音频的用途以用户分镜稿/提示词的说明为准：写明是配音的，Agent 用 use_uploaded 显式采用；
        # 写明是音色/情绪等参考的，不采用不合成（自动随视频生成传入）；用途不明时 Agent 必须先询问用户，
        # 本工具不做任何猜测。采用时 duration 与时间轴一律不改——用户分镜稿是权威设计值。
        if args.get("use_uploaded"):
            if not uploaded:
                # 无上传音频可采：回落为按旁白合成（旧实现直接报错，模型被弹回再试一轮；
                # 真正无从下手的是连旁白文案都没有的情形）
                if not text:
                    return {
                        "ok": False,
                        "error": (
                            f"分镜 {idx} 既没有挂载上传音频也没有旁白文案，无从生成配音；"
                            "请先 set_scene_narration 写入旁白，或让用户上传音频到该分镜。"
                        ),
                    }
                # 落到下方合成路径
            else:
                # 按旁白文案匹配选段（旧实现盲取第一段，9 段全挂一镜时经常张冠李戴）；
                # 匹配不到时才退回第一段，并在结果里把选中的文件名亮给用户核对
                picked, _dropped = _pick_matching_audios(uploaded, scene, limit=1)
                audio_url = picked[0]
                matched_note = None
                if audio_url != uploaded[0]:
                    matched_note = f"已按旁白文案从 {len(uploaded)} 段上传音频中选中「{audio_url.rsplit('/', 1)[-1]}」"
                elif not _norm_tts_text(_audio_semantic(audio_url)) or _norm_tts_text(_audio_semantic(audio_url)) not in _norm_tts_text(str(scene.get("prompt") or "") + str(scene.get("narration") or "")):
                    matched_note = (
                        f"⚠️ 选中「{audio_url.rsplit('/', 1)[-1]}」，但其文件名台词与该镜旁白文案不匹配"
                        f"（本镜共挂 {len(uploaded)} 段音频）——请与用户确认对应关系，不要 silently 换段"
                    )
                audio_seconds = await voice_service.probe_audio_seconds(audio_url)
                scene["voice"] = audio_url
                self._save_project_scenes(scenes)
                note = (
                    "已采用用户上传音频作为该镜旁白（未调用 TTS）；duration 保持用户设定未改写；"
                    "该音频仍会作为参考素材随视频生成一并传入"
                )
                if matched_note:
                    note += f"；{matched_note}"
                result = {
                    "ok": True,
                    "scene_index": idx,
                    "voice": audio_url,
                    "audio_seconds": audio_seconds,
                    "note": note,
                }
                if audio_seconds:
                    set_dur = int(scene.get("duration") or 0)
                    if set_dur and audio_seconds > set_dur + 0.5:
                        result["warning"] = (
                            f"采用音频实际 {audio_seconds:.1f}s，长于该镜设定时长 {set_dur}s——"
                            "成片拼接时旁白会被 -shortest 截断，需否加长该镜由用户决定，禁止自行改写时长或时间轴"
                        )
                return result
        if not text:
            return {"ok": False, "error": f"分镜 {idx} 没有旁白文案，请先 set_scene_narration"}
        # 素材铁律硬校验：该镜挂有用户上传音频时，禁止用与分镜旁白不一致的自拟台词合成覆盖。
        # 复现事故：模型自编台词 force 重配，把用户上传配音覆盖成了合成音
        if uploaded and not args.get("force"):
            arg_text = str(args.get("text") or "")
            if arg_text and _norm_tts_text(arg_text) != _norm_tts_text(scene.get("narration") or ""):
                return {
                    "ok": False,
                    "error": (
                        f"分镜 {idx} 挂有用户上传音频，且传入文本与该镜旁白不一致——禁止用自拟台词覆盖用户素材语境。"
                        "采用上传音频请传 \"use_uploaded\": true；用户明确要求重配该镜时才可传 \"force\": true。"
                    ),
                }
        # 断点接续：agent 生成的配音以文本为身份——同文本的历史配音直接沿用，重跑不重复烧钱；
        # 用户要求重配/换音色时 Agent 传 "force":true（换了 model/voice 也强制重配）
        if not args.get("force") and not any(args.get(k) for k in ("model", "voice")):
            hit = _find_completed_tts(text)
            if hit:
                scene["voice"] = hit["output"]
                audio_seconds = float(hit.get("duration_seconds") or 0)
                followup = None
                if audio_seconds > 0:
                    scene["duration"], followup = _calibrate_scene_duration(
                        audio_seconds, int(scene.get("duration") or 0)
                    )
                self._save_project_scenes(scenes)
                result = {
                    "ok": True,
                    "scene_index": idx,
                    "voice": hit["output"],
                    "audio_seconds": audio_seconds,
                    "scene_duration": scene["duration"],
                    "note": "旁白文本与历史配音一致，已直接沿用（未重新合成）；时长按该音频实际时长校准",
                }
                if followup:
                    result["followup"] = followup
                return result
        d_provider, d_model, d_voice = _default_tts_setup()
        model = str(args.get("model") or d_model)
        if not model:
            return {"ok": False, "error": "未配置语音模型：请先在设置里添加自定义语音模型，或添加小米 MiMo API Key"}
        provider = _tts_provider_for_model(model)
        default_voice = d_voice if provider == d_provider else ("冰糖" if provider == "xiaomi" else "female-shaonv")
        voice = str(args.get("voice") or default_voice)
        job = job_manager.create("tts", provider, model, {
            # params.text 是配音的文本身份（断点接续按它匹配）+ UI 摘要，存长一点保证可精确匹配
            "text": text[:800], "voice": voice, "scene_index": idx, "scene_title": scene.get("title"),
        })
        job_manager.attach(
            job["id"],
            voice_service.run_voice_job(
                job_id=job["id"], provider=provider, model=model,
                credential_id=None, text=text, voice=voice, speed=1.0, emotion="neutral",
            ),
        )
        # 内联等待（分段合成长文本可能较久）
        waited = 0.0
        while waited < 120:
            j = job_manager.get(job["id"])
            if j["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(2)
            waited += 2
        j = job_manager.get(job["id"]) or {}
        if j.get("status") != "completed":
            return {"ok": False, "error": f"旁白合成未完成：{j.get('error') or '超时（120s）'}", "job_id": job["id"]}
        # 按音频实际时长校准分镜时长（呼吸感+脱节/超限检测）
        audio_seconds = float(j.get("duration_seconds") or 0)
        followup = None
        if audio_seconds > 0:
            scene["duration"], followup = _calibrate_scene_duration(
                audio_seconds, int(scene.get("duration") or 0)
            )
        scene["voice"] = j["output"]
        self._save_project_scenes(scenes)
        result = {
            "ok": True,
            "scene_index": idx,
            "voice": j["output"],
            "audio_seconds": audio_seconds,
            "scene_duration": scene["duration"],
            "note": "旁白已写入该分镜，时长已按语音校准",
        }
        if followup:
            result["followup"] = followup
        return result

    async def generate_scene_video(self, args: dict) -> dict:
        """一键出片：自动解决可确定性完成的前置条件后提交视频任务。

        设计原则「能解决就不弹回」——旧实现用 confirm_stale_prompt / allow_no_voice
        把模型弹回来回重试，一次能做完的事烧了三四轮对话（复现事故：模型被三道
        护栏连续弹回，用户看到的全是拒绝）。现在：
        - 旧 Prompt：仅当用户本次消息携带新内容（疑似新剧本）而该镜 Prompt 不是
          本次会话写入时才拦截（真正可能串旧剧本的唯一情形）；纯「生成」指令直接放行；
        - 旁白先行：有旁白文案但没配音且无上传音频时，自动先调 generate_scene_voice
          （同文本沿用历史配音、按语音时长校准 duration）再提交视频；
        - 完全无旁白：按静音镜直接提交并在结果里注明，不再要求 allow_no_voice。
        仍然拦截的只有需要用户决策的事：素材对应关系待确认（ref_remap_pending）。
        """
        blocked = self._ref_remap_blocked()
        if blocked:
            return {"ok": False, "error": blocked}
        project = self._load_project()
        if not project:
            return {"ok": False, "error": "当前没有打开的项目"}
        try:
            idx = int(args.get("index", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "index 必须是数字"}
        scenes = project.get("scenes", [])
        if idx < 0 or idx >= len(scenes):
            return {"ok": False, "error": f"分镜序号越界（0-{len(scenes) - 1}）"}
        scene = scenes[idx]
        if not (scene.get("prompt") or ""):
            return {"ok": False, "error": f"分镜 {idx} 没有 Prompt，请先 set_scene_prompt"}
        user_mounted = any(scene.get(k) for k in ("ref_images", "ref_videos", "ref_audios"))
        # 防串剧本：写回层的 Prompt 指纹比对仍是主防线；提交层只在「消息带了新内容
        # 而分镜没更新」这一确有可能串旧稿的情形下拦截，其余视为当前定稿直接生成
        if (idx not in self.prompt_touched and not user_mounted
                and _goal_carries_content(self.goal) and not args.get("confirm_stale_prompt")):
            return {
                "ok": False,
                "error": (
                    f"分镜 {idx} 的 Prompt 写入于本次任务之前，而用户本次消息携带了新内容（疑似新剧本/新分镜稿）："
                    "必须先 create_scenes / set_scene_prompt 把新内容写入分镜再生成，严禁用旧 Prompt 出片；"
                    '确认旧 Prompt 就是用户要的内容时，在 args 加 "confirm_stale_prompt": true 重试。'
                ),
            }
        # 契约预检放在配音之前：视频根本提交不了时不浪费 TTS
        vmodel = settings.VIDEO_MODELS[0]
        probe = _assemble_video_request(scene, vmodel)
        if probe["contract_error"]:
            return {"ok": False, "error": f"当前视频工作流「{vmodel['label']}」：{probe['contract_error']}"}
        auto_voice = None
        silent_note = ""
        if not scene.get("voice") and not (scene.get("ref_audios") or []):
            if scene.get("narration"):
                auto_voice = await self.generate_scene_voice({"index": idx})
                if not auto_voice.get("ok"):
                    return {"ok": False, "error": f"自动合成旁白失败：{auto_voice.get('error')}"}
                fresh = self._load_project() or {}
                fscenes = fresh.get("scenes") or []
                if 0 <= idx < len(fscenes):
                    scene = fscenes[idx]  # duration 可能已被语音时长校准
            else:
                silent_note = "该镜既无旁白也无配音，已按静音镜提交"
        r = _submit_scene_video(self, scene, idx, wanted_duration=args.get("duration"))
        if r.get("ok"):
            notes = []
            if auto_voice and auto_voice.get("ok"):
                notes.append(
                    f"已自动合成旁白（{auto_voice.get('audio_seconds', '?')}s，"
                    "时长已按语音校准），无需再调 generate_scene_voice"
                )
            if silent_note:
                notes.append(silent_note)
            if idx not in self.prompt_touched and not user_mounted:
                notes.append("该镜 Prompt 为本次任务之前写入，已视为当前定稿直接生成")
            if notes:
                r["note"] = (r.get("note") or "") + ("；" if r.get("note") else "") + "；".join(notes)
        return r

    async def export_project(self) -> dict:
        """把当前项目的分镜视频按顺序拼接成片（ffmpeg），写入项目并记录地址。"""
        if not self.project_id:
            return {"ok": False, "error": "当前没有打开的项目"}
        try:
            result = await asyncio.to_thread(export_service.export_project, self.project_id)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        self.exports.append(result["url"])
        project = self._load_project()
        if project:
            project["final_video"] = result["url"]
            project_service.save_project(self.project_id, project)
            self.project_changed = True
        return {
            "ok": True,
            "final_video": result["url"],
            "scenes_exported": result["scenes_exported"],
            "scenes_skipped": result["scenes_skipped"],
        }

    def _save_voice(self, args: dict) -> dict:
        """把一段已完成配音固化为音色资产（克隆样本），供后续按名稳定复用。"""
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少 name（音色名，如：深夜电台男声）"}
        desc = str(args.get("desc") or "").strip()
        audio_url = str(args.get("audio_url") or "").strip()
        job_id = str(args.get("job_id") or "").strip()
        if job_id and not audio_url:
            j = job_manager.get(job_id) or {}
            if j.get("type") != "tts" or j.get("status") != "completed" or not j.get("output"):
                return {"ok": False, "error": f"任务 {job_id} 不是已完成的配音任务"}
            audio_url = j["output"]
        if not audio_url:
            # 兜底：本会话最近完成的配音
            for ref in reversed(self.jobs):
                if ref.get("type") != "tts":
                    continue
                j = job_manager.get(ref["job_id"]) or {}
                if j.get("status") == "completed" and j.get("output"):
                    audio_url = j["output"]
                    break
        if not audio_url:
            return {"ok": False, "error": "缺少音频来源：传 job_id（配音任务）或 audio_url，或先完成一次配音"}
        local = file_service.resolve_local(audio_url)
        if local is None:
            return {"ok": False, "error": f"音频不可用：{audio_url[:120]}"}
        try:
            asset = voice_assets.create_asset(name, "clone", desc, local)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "voice": asset["id"],
            "name": asset["name"],
            "note": (
                f"音色「{asset['name']}」已固化（id={asset['id']}）。之后所有配音的 voice 参数"
                "直接用该音色名或 ID，系统自动走克隆协议稳定复用，不再漂移。"
            ),
        }

    def execute_tool(self, tool: str, args: dict) -> dict:
        if tool == "save_voice":
            return self._save_voice(args)
        if tool == "query_jobs":
            jtype = args.get("type")
            status = args.get("status")
            limit = min(max(int(args.get("limit") or 10), 1), 30)
            jobs = job_manager.list(jtype=jtype, status=status, limit=limit)
            return {
                "ok": True,
                "jobs": [
                    {
                        "id": j["id"],
                        "type": j["type"],
                        "status": j["status"],
                        "output": j.get("output"),
                        "error": (j.get("error") or "")[:120],
                        "scene_index": (j.get("params") or {}).get("scene_index"),
                        "created_at": j.get("created_at"),
                    }
                    for j in jobs
                ],
            }
        if tool == "load_skill":
            name = str(args.get("name") or "")
            sk = skill_service.get_skill(name)
            if not sk:
                available = ", ".join(s["id"] for s in skill_service.list_skills())
                return {"ok": False, "error": f"技能不存在，可用技能：{available}"}
            # 可选 file 参数：读取技能自带的参考文件（references/<file>）
            filename = str(args.get("file") or "").strip()
            if filename:
                content = skill_service.get_skill_file(sk["id"], filename)
                if content is None:
                    files = skill_service.list_skill_files(sk["id"])
                    hint = f"，可用参考文件：{', '.join(files)}" if files else ""
                    return {"ok": False, "error": f"参考文件不存在{hint}"}
                return {"ok": True, "skill": sk["id"], "file": filename, "content": content[:40000]}
            result = {"ok": True, "skill": sk["id"], "content": sk["body"][:30000]}
            files = skill_service.list_skill_files(sk["id"])
            if files:
                result["files"] = files
                result["note"] = "该技能带有参考文件，需要时用 file 参数读取上述文件"
            return result
        if tool == "get_project_state":
            project = self._load_project()
            if not project:
                return {"ok": False, "error": "当前没有打开的项目"}
            scenes = [
                {
                    "index": i,
                    "title": s.get("title"),
                    "has_prompt": bool(s.get("prompt")),
                    "has_image": bool(s.get("image")),
                    "has_video": bool(s.get("video")),
                    "has_ref_images": bool(s.get("ref_images")),
                    "has_ref_video": bool(s.get("ref_videos")),
                    "has_ref_audio": bool(s.get("ref_audios")),
                    "duration": s.get("duration"),
                    # 素材对号依据：旁白摘要 + 上传文件名（文件名带用户自标记的内容摘要）
                    "narration_head": (s.get("narration") or "")[:40],
                    "ref_audio_files": [u.rsplit("/", 1)[-1] for u in (s.get("ref_audios") or []) if isinstance(u, str)][:8],
                    "ref_image_files": [u.rsplit("/", 1)[-1] for u in (s.get("ref_images") or []) if isinstance(u, str)][:8],
                }
                for i, s in enumerate(project.get("scenes", []))
            ]
            state = {
                "ok": True,
                "project_name": project.get("name"), "aspect_ratio": project.get("aspect_ratio"),
                "scenes": scenes,
                "ref_remap_pending": bool(project.get("ref_remap_pending")),
                "orphan_refs_count": len(project.get("orphan_refs") or []),
            }
            # 素材池：用户上传但尚未挂到分镜的参考图/配音——挂载入口见 attach_scene_refs
            try:
                recent = file_service.list_recent_uploads()
                if recent:
                    state["recent_uploads"] = recent
            except Exception:  # noqa: BLE001 - 素材池列举失败不影响状态返回
                pass
            if state["ref_remap_pending"]:
                state["note"] = (
                    "分镜被重拆且旧分镜挂有用户上传素材，对应关系待确认：先与用户逐镜核对，"
                    "一致后调用 confirm_ref_remap 解锁，不一致请用户在 Studio 重新挂载"
                )
            return state
        if tool == "create_scenes":
            raw_scenes = args.get("scenes")
            if not isinstance(raw_scenes, list) or not raw_scenes:
                return {"ok": False, "error": "scenes 必须是非空数组"}
            if len(raw_scenes) < 3 and not args.get("force"):
                # 下限只拦「模型对模糊创意偷懒拆两镜」：用户分镜稿一镜对一镜录入，
                # 每镜带完整长 Prompt（≥120 字）时 1-2 镜合法（复现：单镜项目被拦到无法出片）
                storyboard_like = all(
                    len(str(s.get("prompt") or "").strip()) >= 120
                    for s in raw_scenes if isinstance(s, dict)
                )
                if not storyboard_like:
                    return {"ok": False, "error": "分镜数少于 3：自行创作时需要钩子/展开/收尾的完整结构，请按 screenwriting 方法论重新拆分（含转场设计）；用户分镜稿本身只有 1-2 镜时照原样录入即可（每镜给完整 Prompt），无需凑数"}
            if len(raw_scenes) > 200:
                return {"ok": False, "error": "分镜数量上限 200 个（按每镜 3-5s，10 分钟短剧约 120-200 镜）；如需更长请分两个项目制作"}
            scenes = []
            for i, s in enumerate(raw_scenes):
                if not isinstance(s, dict):
                    continue
                gen_mode = str(s.get("gen_mode") or "i2v").lower()
                if gen_mode not in ("i2v", "t2v"):
                    gen_mode = "i2v"
                scenes.append(
                    {
                        "id": f"scene_{uuid.uuid4().hex[:8]}",
                        "title": str(s.get("title") or f"分镜 {i + 1}")[:50],
                        "description": str(s.get("description") or "")[:500],
                        "prompt": str(s.get("prompt") or "")[:6000],
                        "narration": str(s.get("narration") or "")[:5000],
                        "gen_mode": gen_mode,
                        "keyframe_prompt": str(s.get("keyframe_prompt") or "")[:2000],
                        "image": None,
                        "voice": None,
                        "video": None,
                        "duration": min(max(int(s.get("duration") or 5), 1), 60),
                        "ref_images": [],
                        "ref_videos": [],
                        "ref_audios": [],
                    }
                )
            if not scenes:
                return {"ok": False, "error": "没有有效的分镜"}
            # 角色设定卡：写入项目级 characters，供跨镜一致性引用
            characters = []
            for c in args.get("characters") or []:
                if isinstance(c, dict) and c.get("name"):
                    characters.append(
                        {"name": str(c["name"])[:40], "design": str(c.get("design") or "")[:1000]}
                    )
            # 一致性强制兜底：每镜 Prompt 末尾注入角色设定卡（LLM 复述遗漏时仍保证视频模型看到外观锚点）
            # 注意：必须在下方用户素材迁移之后执行——挂有用户参考图的分镜跳过注入，
            # 外观以参考图为准（Ref2VA），文字设定卡与"参考图即唯一外观依据"互相打架
            injected = 0
            # 镜内时间轴检测：Action Timeline 出现超过该镜时长的时刻 = 沿用了全局时刻，回传警告让 Agent 修正
            timeline_warnings = []
            for i, s in enumerate(scenes):
                for m in re.finditer(
                    r"(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)\s*(?:s|秒|\"|”)", s["prompt"]
                ):
                    if float(m.group(2)) > s["duration"] + 0.5:
                        timeline_warnings.append(
                            {"index": i, "found": m.group(0), "duration": s["duration"]}
                        )
                        break
            project = self._load_project() or {}
            append = bool(args.get("append")) and isinstance(project.get("scenes"), list) and project["scenes"]
            migrated, orphans = 0, []
            if append:
                # 追加模式：新增分镜的序号从旧列表末尾开始，旧分镜标记仍然有效
                self.prompt_touched.update(range(len(project["scenes"]), len(project["scenes"]) + len(scenes)))
                scenes = project["scenes"] + scenes
                # 追加模式合并角色设定卡，避免第二批覆盖第一批
                old_chars = {c["name"]: c for c in (project.get("characters") or []) if isinstance(c, dict)}
                for c in characters:
                    old_chars.setdefault(c["name"], c)
                characters = list(old_chars.values())
            else:
                # 整体重写：分镜全部替换，本次会话写入的 Prompt 覆盖全部新序号。
                # 用户上传的参考素材（ref_*）按序号迁移到新分镜——重拆分镜绝不能弄丢用户素材；
                # agent 生成物（image/voice/video）不迁移：旧内容对新剧本是脏数据，防止旧剧本画面串进新分镜
                old_scenes = project.get("scenes") or []
                for i, ns in enumerate(scenes):
                    if i >= len(old_scenes):
                        break
                    for k in ("ref_images", "ref_videos", "ref_audios"):
                        vals = [v for v in (old_scenes[i].get(k) or []) if isinstance(v, str)]
                        if vals:
                            ns[k] = vals
                            migrated += len(vals)
                # 新分镜数少于旧分镜时，多出分镜上的用户素材无法按序号对应：暂存到 orphan_refs 并警告，
                # 让 Agent 转告用户到 Studio 重新挂载，而不是静默丢弃
                for i in range(len(scenes), len(old_scenes)):
                    o = old_scenes[i]
                    o_refs = {k: [v for v in (o.get(k) or []) if isinstance(v, str)]
                              for k in ("ref_images", "ref_videos", "ref_audios")}
                    if any(o_refs.values()):
                        orphans.append({"from_scene_index": i, "title": o.get("title"), **o_refs})
                if orphans:
                    project["orphan_refs"] = (project.get("orphan_refs") or []) + orphans
                # 素材铁律：旧分镜挂有用户素材而重拆改变了分镜数量时，按序号迁移可能张冠李戴——
                # 锁定一切生成，Agent 必须先与用户确认对应关系（confirm_ref_remap 解锁）
                old_refs_total = sum(
                    len([v for v in (o.get(k) or []) if isinstance(v, str)])
                    for o in old_scenes for k in ("ref_images", "ref_videos", "ref_audios")
                )
                if old_refs_total and len(scenes) != len(old_scenes):
                    project["ref_remap_pending"] = True
                self.prompt_touched = set(range(len(scenes)))
            # 角色设定卡注入（在用户素材迁移之后：挂有用户参考图的分镜跳过，外观以图为准）
            if characters:
                ref_block = (
                    "\n\n## Character Reference（跨镜一致，外观逐字遵守）\n"
                    + "\n".join(f"- {c['name']}：{c['design']}" for c in characters)
                )
                for s in scenes:
                    if s.get("ref_images"):
                        continue
                    if not any(c["design"][:20] in s["prompt"] for c in characters if c["design"]):
                        s["prompt"] = (s["prompt"].rstrip() + ref_block)[:8000]
                        injected += 1
            project["scenes"] = scenes
            project["characters"] = characters
            project_service.save_project(self.project_id, project)
            self.project_changed = True
            result = {
                "ok": True,
                "scene_count": len(scenes),
                "characters": [c["name"] for c in characters],
                "prompt_injected": injected,
                "note": ("已追加到现有分镜之后" if append else "已写入项目并替换原有分镜") + "；角色设定卡已强制注入每镜 Prompt",
            }
            if not append and migrated:
                result["refs_migrated"] = migrated
                result["note"] += f"；已把旧分镜上 {migrated} 项用户上传的参考素材按序号迁移到新分镜（音色/情绪/配音等参考音频与参考图均在内）"
            if project.get("ref_remap_pending"):
                result["ref_remap_pending"] = True
                result["note"] += "；⚠️ 旧分镜挂有用户素材且分镜数量变化，已锁定生成——必须先与用户确认素材对应关系，确认后 confirm_ref_remap 解锁"
            orphan_warning = ""
            if orphans:
                orphan_warning = (
                    f"旧分镜 {orphans[0]['from_scene_index']}-{orphans[-1]['from_scene_index']} 上有 "
                    f"{len(orphans)} 镜的用户上传素材无法按序号对应到新分镜，已暂存到项目 orphan_refs 字段。"
                    "必须 finish 告知用户到 Studio 参考素材面板手动重新挂载（或让用户给出对应关系），严禁静默丢弃。"
                )
                result["orphan_refs"] = orphans
            if timeline_warnings:
                result["timeline_warnings"] = timeline_warnings
                result["warning_note"] = (
                    "以下分镜的 Action Timeline 时刻超过了该镜时长（疑似沿用了剧本全局时刻）："
                    + "；".join(
                        f"分镜{w['index']} 出现 {w['found']} 但时长仅 {w['duration']}s" for w in timeline_warnings
                    )
                    + "。必须用 set_scene_prompt 修正为镜内相对时间（该镜首帧=0s，各段之和=时长）。"
                ) + (f" {orphan_warning}" if orphan_warning else "")
            elif orphan_warning:
                result["warning_note"] = orphan_warning
            return result
        if tool in ("set_scene_prompt", "set_scene_narration"):
            project = self._load_project()
            if not project:
                return {"ok": False, "error": "当前没有打开的项目"}
            idx = int(args.get("index", -1))
            scenes = project.get("scenes", [])
            if idx < 0 or idx >= len(scenes):
                return {"ok": False, "error": f"分镜序号越界（0-{len(scenes) - 1}）"}
            key = "prompt" if tool == "set_scene_prompt" else "narration"
            new_val = str(args.get("prompt") or args.get("text") or "")
            # 截断防护：挂有用户素材的分镜，其 Prompt 视为用户定稿；重写稿明显短于原稿
            # 多为模型重发时的省略/截断（复现事故：4713 字分镜稿被覆盖成 544 字残片）
            if key == "prompt":
                old_val = scenes[idx].get("prompt") or ""
                user_mounted = any(scenes[idx].get(k) for k in ("ref_images", "ref_videos", "ref_audios"))
                if (user_mounted and old_val and len(new_val) < len(old_val) * 0.6
                        and not args.get("force")):
                    return {
                        "ok": False,
                        "error": (
                            f"分镜 {idx} 挂有用户上传素材，其 Prompt 视为用户定稿；新 Prompt"
                            f"（{len(new_val)} 字）比原稿（{len(old_val)} 字）短得多，疑似重发时的截断/省略。"
                            "用户未给新分镜稿时禁止改写，直接 generate_scene_video；"
                            "确属用户提供的新稿请在 args 加 \"force\": true。"
                        ),
                    }
            # 上限与 create_scenes 注入角色设定块后的 8000 保持一致，避免重写时切掉设定块
            scenes[idx][key] = new_val[:8000 if key == "prompt" else 5000]
            if key == "prompt":
                self.prompt_touched.add(idx)
            self._save_project_scenes(scenes)
            return {"ok": True, "index": idx}
        if tool == "attach_scene_refs":
            # 素材挂载/分发。用户上传素材但没在 Studio 手动挂载时，Agent 从
            # get_project_state.recent_uploads（或消息【本次附件】）选 URL：
            # - 带 index：挂到指定分镜；
            # - 不带 index：项目级分发——配音按文件名台词对号到各镜（整条片的 9 段配音
            #   自动拆到 5 个分镜，而不是全堆在一镜导致只有一镜能生成），参考图挂到
            #   全部分镜（角色设定图是全局外观锚点）。素材给了就必须能用上。
            project = self._load_project()
            if not project:
                return {"ok": False, "error": "当前没有打开的项目"}
            scenes = project.get("scenes", [])
            if not scenes:
                return {"ok": False, "error": "项目还没有分镜，请先 create_scenes 录入分镜稿"}
            for key in ("ref_images", "ref_videos", "ref_audios"):
                vals = args.get(key) or []
                if not isinstance(vals, list):
                    return {"ok": False, "error": f"{key} 必须是数组"}
                for v in vals:
                    if not str(v).startswith(("/api/files/uploads/", "/api/files/outputs/")):
                        return {"ok": False, "error": f"素材路径必须是本站 uploads/outputs 地址：{str(v)[:80]}"}
            has_index = args.get("index") is not None
            if has_index:
                try:
                    idx = int(args.get("index", -1))
                except (TypeError, ValueError):
                    return {"ok": False, "error": "index 必须是数字"}
                if idx < 0 or idx >= len(scenes):
                    return {"ok": False, "error": f"分镜序号越界（0-{len(scenes) - 1}）"}
                scene = scenes[idx]
                mounted: list[str] = []
                for key in ("ref_images", "ref_videos", "ref_audios"):
                    cur = [v for v in (scene.get(key) or []) if isinstance(v, str)]
                    for v in args.get(key) or []:
                        if v not in cur:
                            cur.append(v)
                            mounted.append(v)
                    scene[key] = cur
                # 挂载后立即按该镜 Prompt 的声明顺序对齐（<Picture/Video/Audio N> 编号
                # 与 ref_*_N 传入顺序一致；Agent 工具参数顺序不可作为绑定依据）
                if scene.get("ref_images"):
                    scene["ref_images"] = _sort_ref_assets_by_prompt(scene["ref_images"], scene, "Picture")
                if scene.get("ref_videos"):
                    scene["ref_videos"] = _sort_ref_assets_by_prompt(scene["ref_videos"], scene, "Video")
                if scene.get("ref_audios"):
                    scene["ref_audios"] = _sort_audios_by_prompt(scene["ref_audios"], scene)
                self._save_project_scenes(scenes)
                return {
                    "ok": True, "index": idx, "mounted": mounted,
                    "note": "素材已挂载到该分镜，生成时会作为参考直传视频模型；挂有用户素材的分镜其 Prompt/旁白/时长视为用户定稿",
                }
            # ---- 项目级分发 ----
            audios = [str(v) for v in (args.get("ref_audios") or [])]
            images = [str(v) for v in (args.get("ref_images") or [])]
            videos = [str(v) for v in (args.get("ref_videos") or [])]
            claims, unmatched, overflow = _distribute_audios(audios, scenes)
            for i, scene in enumerate(scenes):
                cur_a = [v for v in (scene.get("ref_audios") or []) if isinstance(v, str)]
                for u in claims.get(i, []):
                    if u not in cur_a:
                        cur_a.append(u)
                scene["ref_audios"] = _sort_audios_by_prompt(cur_a, scene)
                # 参考图/参考视频挂全部分镜（角色外观锚点；参考视频暂无工作流支持也先挂上留档），
                # 每镜按该镜 Prompt 的 <Picture/Video N> 声明顺序对齐
                for key, vals, label in (("ref_images", images, "Picture"), ("ref_videos", videos, "Video")):
                    cur = [v for v in (scene.get(key) or []) if isinstance(v, str)]
                    for v in vals:
                        if v not in cur:
                            cur.append(v)
                    scene[key] = _sort_ref_assets_by_prompt(cur, scene, label)
            self._save_project_scenes(scenes)
            per_scene = {str(i + 1): len(v) for i, v in sorted(claims.items())}
            note = (
                f"配音已按台词对号分发到 {len(claims)} 个分镜（{json.dumps(per_scene, ensure_ascii=False)}），"
                f"参考图 {len(images)} 张已挂到全部分镜"
            )
            result: dict = {"ok": True, "distributed": True, "per_scene": per_scene, "note": note}
            if unmatched:
                result["unmatched_audios"] = [u.rsplit("/", 1)[-1] for u in unmatched]
                result["note"] += (
                    f"；⚠️ {len(unmatched)} 段配音的台词没有命中任何分镜（文件名与各镜旁白/Prompt 对不上），"
                    "未强行分配——必须在总结中列出并请用户确认归属"
                )
            if overflow:
                result["overflow_scenes"] = [i + 1 for i in overflow]
                result["note"] += f"；分镜 {'、'.join(str(i + 1) for i in overflow)} 命中超过 3 段（工作流上限），只保留前 3 段"
            return result
        if tool == "read_doc":
            # 读取用户上传的剧本文档（md/txt）：消息里只给了文件名/URL 或需要重读全文时用
            url = str(args.get("url") or "")
            name = url.rsplit("/", 1)[-1]
            if not url.startswith("/api/files/uploads/doc/"):
                return {"ok": False, "error": "只支持读取本站上传的 doc 文档（/api/files/uploads/doc/…）"}
            if "/" in name or "\\" in name or ".." in name:
                return {"ok": False, "error": "非法路径"}
            path = file_service.settings.UPLOADS_DIR / "doc" / name
            if not path.exists():
                return {"ok": False, "error": f"文档不存在：{name}"}
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:60000]
            except OSError as exc:
                return {"ok": False, "error": f"读取失败：{exc}"}
            return {"ok": True, "url": url, "name": name, "content": text}
        if tool == "confirm_ref_remap":
            # 素材铁律解锁：Agent 已就"重拆后用户素材与新分镜的对应关系"征得用户同意
            project = self._load_project()
            if not project:
                return {"ok": False, "error": "当前没有打开的项目"}
            if not project.get("ref_remap_pending"):
                return {"ok": True, "note": "当前没有待确认的素材对应关系"}
            project.pop("ref_remap_pending", None)
            project_service.save_project(self.project_id, project)
            self.project_changed = True
            return {"ok": True, "note": "已确认用户素材与新分镜的对应关系，生成已解锁"}
        if tool == "set_prompt":
            self.updates["prompt"] = str(args.get("text", ""))[:6000]
            return {"ok": True}
        if tool == "set_description":
            self.updates["description"] = str(args.get("text", ""))[:2000]
            return {"ok": True}
        if tool == "set_narration":
            self.updates["narration"] = str(args.get("text", ""))[:5000]
            return {"ok": True}
        if tool == "generate_image":
            prompt = args.get("prompt") or self._scene("prompt") or self._scene("description")
            if not prompt:
                return {"ok": False, "error": "缺少图片 Prompt，请先 set_prompt 或在 args 里给 prompt"}
            aspect = args.get("aspect_ratio") or self.context.get("aspect_ratio") or "1:1"
            model = _pick_image_model()
            job = job_manager.create("image", model["provider"], model["id"], {"prompt": str(prompt)[:200], "aspect_ratio": aspect})
            job_manager.attach(
                job["id"],
                image_service.run_image_job(
                    job_id=job["id"], provider=model["provider"], model=model["id"],
                    credential_id=None, prompt=str(prompt), size=None, n=1, references=[],
                ),
            )
            self.jobs.append({"job_id": job["id"], "type": "image"})
            return {"ok": True, "job_id": job["id"]}
        if tool == "generate_voice":
            text = args.get("text") or self._scene("narration")
            if not text:
                return {"ok": False, "error": "缺少旁白文本，请先 set_narration 或在 args 里给 text"}
            d_provider, d_model, d_voice = _default_tts_setup()
            model = args.get("model") or d_model
            if not model:
                return {"ok": False, "error": "未配置语音模型：请先在设置里添加自定义语音模型，或添加小米 MiMo API Key"}
            provider = _tts_provider_for_model(model)
            default_voice = d_voice if provider == d_provider else ("冰糖" if provider == "xiaomi" else "female-shaonv")
            voice = args.get("voice") or default_voice
            emotion = _norm_emotion(args.get("emotion")) or "neutral"
            job = job_manager.create("tts", provider, model, {"text": str(text)[:120], "voice": voice, "emotion": emotion})
            job_manager.attach(
                job["id"],
                voice_service.run_voice_job(
                    job_id=job["id"], provider=provider, model=model,
                    credential_id=None, text=str(text), voice=voice, speed=1.0, emotion=emotion,
                ),
            )
            self.jobs.append({"job_id": job["id"], "type": "tts"})
            return {"ok": True, "job_id": job["id"]}
        if tool == "generate_voices_batch":
            script = str(args.get("script") or "").strip()
            lines: list[tuple[str, str | None, str | None]] = []
            if script:
                # 用户原文逐字传入：后端严格按换行拆分，禁止模型自行拆句/合并/改写。
                # 每行支持标注音色/情绪：`文本|音色|情绪`（竖线后两段可省略）。
                for ln in script.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    parts = [p.strip() for p in ln.split("|")]
                    text = parts[0]
                    v = parts[1] if len(parts) > 1 and parts[1] else None
                    e = parts[2] if len(parts) > 2 and parts[2] else None
                    lines.append((text, v, _norm_emotion(e)))
            else:
                for t in (args.get("lines") or args.get("texts") or []):
                    if isinstance(t, dict):
                        lines.append((str(t.get("text", "")).strip(), t.get("voice") or None, _norm_emotion(t.get("emotion"))))
                    else:
                        lines.append((str(t).strip(), None, None))
            lines = [(t, v, e) for t, v, e in lines if t][:200]
            if not lines:
                return {"ok": False, "error": "缺少 script（用户剧本原文）或 texts（旁白句数组）"}
            # 断点接续：默认沿用已配过的句子（文本身份匹配，兼容历史任务只存前缀）；force=true 时全部重新生成
            force = bool(args.get("force"))
            done_map: dict[str, str] = {}
            prefix_entries: list[tuple[str, str]] = []
            if not force:
                for j in job_manager.list(jtype="tts", status="completed", limit=5000):
                    t = str(j.get("params", {}).get("text", "")).strip()
                    if t and j.get("output"):
                        nt = _norm_tts_text(t)
                        done_map.setdefault(nt, j["id"])
                        prefix_entries.append((nt, j["id"]))
            reused: list[str] = []
            fresh: list[tuple[str, str | None, str | None]] = []
            for item in lines:
                nt = _norm_tts_text(item[0])
                jid = done_map.get(nt)
                if not jid and nt:
                    need = min(len(nt), 100)
                    for pt, pid in prefix_entries:
                        if len(pt) >= need and nt.startswith(pt):
                            jid = pid
                            break
                if jid:
                    reused.append(jid)
                else:
                    fresh.append(item)
            self.voice_reused.extend(reused)
            lines = fresh
            d_provider, d_model, d_voice = _default_tts_setup()
            model = args.get("model") or d_model
            if not model:
                return {"ok": False, "error": "未配置语音模型：请先在设置里添加自定义语音模型，或添加小米 MiMo API Key"}
            provider = _tts_provider_for_model(model)
            default_voice = d_voice if provider == d_provider else ("冰糖" if provider == "xiaomi" else "female-shaonv")
            voice = args.get("voice") or default_voice
            job_ids = []

            async def _paced_tts(i: int, job_id: str, t: str, v: str | None, e: str | None):
                # 错峰提交：每条间隔 3 秒起步，避开 TTS 接口的 RPM 限流
                await asyncio.sleep(i * 3.0)
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        await voice_service.run_voice_job(
                            job_id=job_id, provider=provider, model=model,
                            credential_id=None, text=t, voice=v or voice, speed=1.0, emotion=e or "neutral",
                        )
                        return
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if "rate limit" in str(exc).lower() and attempt < 2:
                            await asyncio.sleep(30)  # 撞限流：等一个窗口再重试
                            continue
                        raise
                raise last_exc  # type: ignore[misc]

            for i, (t, v, e) in enumerate(lines):
                job = job_manager.create("tts", provider, model, {"text": t[:800], "voice": v or voice, "emotion": e or "neutral"})
                job_manager.attach(job["id"], _paced_tts(i, job["id"], t, v, e))
                self.jobs.append({"job_id": job["id"], "type": "tts"})
                job_ids.append(job["id"])
            return {
                "ok": True,
                "count": len(job_ids),
                "skipped": len(reused),
                "job_ids": job_ids,
                "reused_job_ids": reused,
                "lines_detail": [
                    {"job_id": jid, "text": t, "voice": v or voice, "emotion": e or "neutral"}
                    for jid, (t, v, e) in zip(job_ids, lines)
                ],
                "note": (
                    f"新提交 {len(job_ids)} 条后台错峰执行（无需 wait_job）；"
                    f"另有 {len(reused)} 条历史上已配过，自动沿用旧结果（断点接续）。"
                    "用户需要逐句时长/音频地址时，调用 get_voice_lines 查询（任务完成后会返回精确 duration_seconds 和音频 URL）。"
                    "finish 时告知用户：新配了几条、跳过几条，界面会给出本批打包链接（含沿用部分）。"
                ),
            }
        if tool == "get_voice_lines":
            raw = str(args.get("job_ids") or "").strip()
            if raw:
                ids = [i.strip() for i in raw.split(",") if i.strip()]
            else:
                ids = [j["job_id"] for j in self.jobs if j.get("type") == "tts"] + list(self.voice_reused)
            if not ids:
                return {"ok": False, "error": "本会话没有配音任务；请传 job_ids 或先生成配音"}
            rows = []
            for jid in ids:
                j = job_manager.get(jid)
                if not j:
                    continue
                rows.append({
                    "job_id": jid,
                    "status": j.get("status"),
                    "text": j.get("params", {}).get("text", ""),
                    "voice": j.get("params", {}).get("voice", ""),
                    "duration_seconds": j.get("duration_seconds"),
                    "audio_url": j.get("output"),
                })
            done = sum(1 for r in rows if r["status"] == "completed")
            total = sum(r["duration_seconds"] or 0 for r in rows if r["status"] == "completed")
            return {
                "ok": True,
                "count": len(rows),
                "completed": done,
                "total_duration_seconds": round(total, 2),
                "lines": rows,
                "note": "duration_seconds 在任务 completed 后为精确时长；未完成的稍后再查。",
            }
        if tool == "generate_video":
            prompt = self._scene("prompt")
            if not prompt:
                return {"ok": False, "error": "当前没有视频 Prompt，请先 set_prompt"}
            duration = min(max(int(args.get("duration") or 5), 1), 15)
            aspect = args.get("aspect_ratio") or self.context.get("aspect_ratio") or "16:9"
            with_image = bool(args.get("with_image"))
            # 场景图优先取项目最新值：context 是运行开始时的快照，generate_scene_image
            # 生成的图写进项目文件后快照里看不到（复现中模型已出图仍报"不支持文生视频"）
            img = self.context.get("image")
            if with_image and not img and self.project_id:
                project = self._load_project() or {}
                scenes = project.get("scenes", [])
                if scenes and scenes[0].get("image"):
                    img = scenes[0]["image"]
            images = [img] if (with_image and img) else []
            vmodel = settings.VIDEO_MODELS[0]
            mode = "image" if images else "text"
            contract_error = video_service.autodl_contract_error(
                vmodel["id"], mode, images, [], [], prompt=prompt
            )
            if contract_error:
                hint = (
                    "。请先为场景生成图片（generate_scene_image 或用户上传），再用 with_image=true 重试"
                    if not images
                    else ""
                )
                return {"ok": False, "error": f"当前视频工作流「{vmodel['label']}」：{contract_error}{hint}"}
            job = job_manager.create("video", vmodel["provider"], vmodel["id"], {
                "mode": mode,
                "prompt": str(prompt)[:300],
                "duration": duration,
                "aspect_ratio": aspect,
                "model": vmodel["id"],
            })
            job_manager.attach(
                job["id"],
                video_service.run_video_job(
                    job_id=job["id"], provider=vmodel["provider"], model=vmodel["id"],
                    credential_id=None, mode=mode, prompt=str(prompt),
                    duration=duration, aspect_ratio=aspect, resolution=None,
                    images=images, last_images=[], videos=[], audios=[],
                    options={"prompt_optimizer": True},
                ),
            )
            self.jobs.append({"job_id": job["id"], "type": "video", "duration": duration,
                              "project_id": self.project_id})
            return {"ok": True, "job_id": job["id"]}
        return {"ok": False, "error": f"未知工具 {tool}"}

    async def wait_job(self, args: dict) -> dict:
        """等待指定任务到终态（异步，最多 seconds 秒）。execute_tool 是同步的，这里单独走异步分发。"""
        job_id = args.get("job_id") or ""
        try:
            seconds = min(max(int(args.get("seconds") or 20), 1), 25)
        except (TypeError, ValueError):
            seconds = 20
        waited = 0.0
        while waited < seconds:
            job = job_manager.get(job_id)
            if not job:
                return {"ok": False, "error": f"任务 {job_id} 不存在"}
            if job["status"] in ("completed", "failed"):
                return {
                    "ok": True,
                    "status": job["status"],
                    "output": job.get("output"),
                    "error": job.get("error"),
                }
            await asyncio.sleep(2)
            waited += 2
        job = job_manager.get(job_id) or {}
        return {"ok": True, "status": job.get("status"), "note": "仍在后台进行中"}


_voice_discovery_cache: dict[str, tuple[float, list[dict]]] = {}
_VOICE_CACHE_TTL = 600  # 秒；get_voice 是慢接口，结果短期缓存


async def _capability_snapshot() -> dict:
    """收集当前真实可用的能力注入系统提示词，防止 Agent 谎报没有能力。

    TTS 模型按协议可用性收集：自定义模型 + MiniMax 域凭据上发现的 TTS 系模型
    + 小米 MiMo 内置模型（配了小米 Key 即可用，无需自定义模型）。
    全部网络发现走线程池执行——这些是同步 httpx 调用，直接 await 会卡死事件循环，
    冻结整个 Studio 的所有接口。
    """
    custom = model_store.list_custom()
    tts_models = [m["id"] for m in custom.get("tts", [])]
    if _has_xiaomi_key():
        tts_models += [m for m in ("mimo-v2.5-tts", "mimo-v2.5-tts-voicedesign") if m not in tts_models]
    voices = [v["id"] for v in custom.get("voice", [])]

    # 复用凭据 /models 发现（带缓存），只为 MiniMax 域凭据补充 TTS 模型
    try:
        from ..config import key_manager
        from ..providers.registry import list_models_for_credential

        for pub in key_manager.list_keys():
            base = (pub.get("base_url") or "").lower()
            if pub["provider"] != "minimax" and "minimax" not in base:
                continue
            try:
                cred = key_manager.get_key(pub["id"])
            except Exception:  # noqa: BLE001
                continue
            ids = await asyncio.to_thread(list_models_for_credential, cred)
            for mid in ids:
                if "tts" in mid.lower() and mid not in tts_models:
                    tts_models.append(mid)
    except Exception:  # noqa: BLE001 - 发现失败不影响能力声明
        pass

    # 音色：MiniMax 域 get_voice 发现（同步、慢 → 线程池 + TTL 缓存）
    try:
        import time

        from ..api.settings import _discover_voices
        from ..config import key_manager as km

        for pub in km.list_keys():
            base = (pub.get("base_url") or "").lower()
            if pub["provider"] != "minimax" and "minimax" not in base:
                continue
            try:
                cred = km.get_key(pub["id"])
            except Exception:  # noqa: BLE001
                continue
            now = time.time()
            hit = _voice_discovery_cache.get(pub["id"])
            if hit and now - hit[0] < _VOICE_CACHE_TTL:
                found = hit[1]
            else:
                found, _err = await asyncio.to_thread(_discover_voices, cred)
                _voice_discovery_cache[pub["id"]] = (now, found)
            voices.extend(v["id"] for v in found if v["id"] not in voices)
            break
    except Exception:  # noqa: BLE001
        pass

    # 小米 MiMo：预置音色为静态列表，配了 Key 即全部可用（无需网络发现）
    if _has_xiaomi_key():
        try:
            from ..providers.tts.xiaomi_tts import XIAOMI_PRESET_VOICES

            voices.extend(v["id"] for v in XIAOMI_PRESET_VOICES if v["id"] not in voices)
        except Exception:  # noqa: BLE001
            pass

    return {
        "tts_models": ", ".join(tts_models[:20]) or "（未配置）",
        "voices": ", ".join(voices[:15]) or "（未配置，可在 args 里指定 voice ID）",
        "fixed_voices": ", ".join(
            f"{a['name']}（ID={a['id']}）" for a in voice_assets.list_assets() if a["kind"] == "clone"
        ) or "（无，可用 save_voice 从 voicedesign 结果固化）",
        # 只列厂商已配凭据的生图模型（含自定义），避免 Agent 误以为模型不可用
        "image_models": _available_image_models() or "（未配置生图凭据）",
        "video_models": ", ".join(m["id"] for m in settings.VIDEO_MODELS[:6]),
    }


def _available_image_models() -> list[str]:
    from ..config.key_manager import list_keys

    providers = {k.get("provider") for k in list_keys()}
    models = settings.IMAGE_MODELS + model_store.list_custom().get("image", [])
    return [m["id"] for m in models if m.get("provider") in providers]


def _calibrate_scene_duration(audio_seconds: float, old_duration: int) -> tuple[int, str | None]:
    """按旁白实际时长校准分镜时长。返回 (新时长, 需要Agent跟进的提示)。

    - 向上取整并留约 1s 呼吸感（台词说完立即切镜很难看）；
    - 与旧时长差 >=1s 时提示重写 Action Timeline（时间轴脱节）；
    - 超过视频生成上限 15s 时提示拆镜/精简。
    """
    new_dur = min(max(int(audio_seconds + 0.999), 1), 60)
    note = None
    if abs(new_dur - old_duration) >= 1:
        note = f"该镜时长已从 {old_duration}s 校准为 {new_dur}s（旁白实际 {audio_seconds:.1f}s）——原 Prompt 的 Action Timeline 已与新时长脱节，必须用 set_scene_prompt 重写时间轴（动作内容不变，仅重排为 0-{new_dur}s）"
    if new_dur > 15:
        note = ((note + "；") if note else "") + f"旁白 {audio_seconds:.1f}s 超过视频生成上限 15s，视频会被截断——请把该镜拆成两镜（各自生成旁白与视频）或精简台词"
    return new_dur, note


def _normalize_route(force_route) -> str | None:
    """已废弃：双模式路由已拆除。保留函数仅为旧调用方兼容，恒返回 None。"""
    return None


def _heuristic_route(goal: str, project: dict | None) -> str | None:
    """确定性预判：素材齐备的编排请求直走流水线（不需要 LLM 判断）。

    命中条件：有分镜挂了 Prompt+用户素材，且请求措辞是"生成/继续"而非"创作/改写/提问"。
    返回 None 表示预判不了，交给 LLM 分类。
    """
    scenes = [s for s in ((project or {}).get("scenes") or []) if isinstance(s, dict)]
    if not scenes:
        return None
    mounted = any(
        s.get("prompt") and (s.get("ref_images") or s.get("ref_videos")
                             or s.get("ref_audios") or s.get("image"))
        for s in scenes
    )
    if not mounted:
        return None
    text = (goal or "").strip()
    if not text or len(text) > 600:
        return None  # 长文本多半是新剧本/新分镜稿，交给分类器
    if text.endswith(("？", "?")) or text.startswith(("为什么", "怎么", "如何", "什么", "帮我看看", "分析")):
        return None  # 疑似提问，交给分类器
    authoring_kw = ("新剧本", "重新拆", "重拆", "改写", "重写", "拆分镜", "写分镜", "新创意",
                    "换剧本", "换风格", "帮我写", "创作")
    if any(k in text for k in authoring_kw):
        return "agent"
    generate_kw = ("生成", "视频", "继续", "直出", "出片", "提交", "跑一")
    if any(k in text for k in generate_kw):
        return "pipeline"
    return None


def _goal_wants_generation(goal: str) -> bool:
    """创作路径交接判定：用户要的是成片（而不是只要文案/分镜表）。"""
    return any(k in (goal or "") for k in ("视频", "成片", "出片", "分镜生成", "生成"))


def _goal_wants_video(goal: str) -> bool:
    """兜底网判定：用户明确要视频成片。比 _goal_wants_generation 窄——
    「重新生成配音」这类含「生成」的纯配音任务不许触发视频兜底。"""
    return any(k in (goal or "") for k in ("视频", "成片", "出片", "video"))


async def run_agent(
    goal: str,
    context: dict,
    credential_id: str | None,
    model: str | None,
    history: list[dict] | None = None,
    on_step=None,
    force_route: str | None = None,  # 已废弃：双模式路由已拆除，仅为旧前端兼容保留（忽略）
) -> dict:
    agent = _Agent(context or {}, goal=goal)
    llm, model = resolve_llm_call(credential_id, provider_hint="minimax", model=model)

    # ---- 零 LLM 快路径：消息是纯执行指令且逐镜预检零缺口 → 直接出片，不烧一次模型调用。
    # 有任何缺口（缺 Prompt/缺配音/契约不满足）都进助手循环——由模型消化用户给的
    # 任何内容（聊天里贴的分镜稿、上传的参考图/配音说明），写好分镜后统一交给流水线。
    # 没有模式选择：一条链路，用户不管怎么给素材都能走到出片。
    project = agent._load_project()
    if _heuristic_route(goal, project) == "pipeline":
        from . import orchestrator  # 函数内导入避免循环依赖
        if not orchestrator.preflight(project)["gaps"]:
            return await orchestrator.run_pipeline(agent, goal, on_step=on_step)

    skills = "\n".join(f"- {s['id']}：{s['description']}" for s in skill_service.list_skills()) or "（无）"
    capability = await _capability_snapshot()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            skills=skills,
            context=json.dumps(context or {}, ensure_ascii=False, default=str),
            **capability,
        )},
        *(history or []),
        {"role": "user", "content": goal},
    ]

    def _record(step: dict) -> None:
        agent.steps.append(step)
        if on_step:
            try:
                on_step(step)
            except Exception:  # noqa: BLE001 - 进度回调失败不影响主流程
                pass

    def _refeed(raw_text: str, note: str) -> None:
        """把模型的畸形输出连同纠正提示回喂，要求重新输出合法动作 JSON。"""
        messages.append({"role": "assistant", "content": raw_text})
        messages.append({"role": "user", "content": json.dumps({"note": note}, ensure_ascii=False)})

    last_error_sig: list = ["", 0]
    empty_summary_retried = False
    bad_output_streak = 0
    cancel_flag = _CancelFlag()
    _ACTIVE_CANCEL_FLAGS.add(cancel_flag)
    try:
        for step in range(MAX_STEPS):
            if cancel_flag.flag:
                raise ProviderError("已手动停止：本次运行被用户终止，已提交的后台任务不受影响")
            raw = await llm.generate(messages, model or "MiniMax-M2", temperature=0.3, max_tokens=8000)
            data, strict, whole_parse = _extract_action(raw)
            action = data.get("action")

            # ---- 畸形输出：纯文本、没有任何可解析的动作 JSON ----
            # 曾导致"模型只宣布一句计划就被当成最终回复直接结束"（用户看到开场白后再无动静）。
            # 处理：回喂纠错要求补上 JSON；连续多次仍失败才把文本降级为回复收尾。
            if not strict:
                bad_output_streak += 1
                if bad_output_streak < MAX_BAD_OUTPUT_STREAK:
                    _refeed(raw, (
                        "你上一条输出是纯文字，没有包含任何可执行的动作 JSON。不要只宣布计划，"
                        "必须直接输出一个 JSON 对象："
                        "调用工具用 {\"action\":\"tool\",\"tool\":\"工具名\",\"args\":{...}}；"
                        "任务完成或回答用户时用 {\"action\":\"finish\",\"summary\":\"<完整回答，不能为空>\"}。"
                        "除此之外不要输出任何文字。"
                    ))
                    continue
                _record({"step": step + 1, "type": "reply", "text": (
                    "⚠️ 模型连续多次未输出可执行动作，任务未完成。以下为模型最后一条原始输出"
                    "（多为草稿或计划文本，不是交付内容）：\n\n" + (raw.strip() or _fallback_summary(agent))
                )})
                break

            bad_output_streak = 0

            # ---- 明确的结束动作 ----
            # 模型经常把 finish 误格式化成工具调用（{"action":"tool","tool":"finish"}），
            # 旧实现返回"未知工具 finish"把收尾打回，运行被迫烧到步数上限——一并兼容
            fin_tool = data.get("tool") if isinstance(data.get("tool"), str) else ""
            if action in ("finish", "reply") or fin_tool in ("finish", "reply"):
                fin_args = data.get("args") if isinstance(data.get("args"), dict) else {}
                text = str(data.get("summary") or data.get("text")
                           or fin_args.get("summary") or fin_args.get("text") or "")
                if not text.strip():
                    # finish/reply 没带总结：回喂一次让模型补全，而不是吃掉回复落进兜底
                    if not empty_summary_retried:
                        empty_summary_retried = True
                        _refeed(raw, "你上一次 finish/reply 的 summary 为空。summary 是给用户看的最终回复，不能为空。请重新输出 {\"action\":\"finish\",\"summary\":\"<完整回答内容>\"}。")
                        continue
                    text = _fallback_summary(agent)
                # 最终回复保留全文（reply 字段取自这条 step），截断会让长 Prompt 半途而废
                _record({"step": step + 1, "type": "finish", "text": text})
                break

            # ---- 混排输出（说明文字 + 内嵌工具 JSON）：全部按序执行 ----
            # 旧逻辑只执行第一个、且正则要求键序严格匹配，键序变化时会漏执行并误判为结束
            embedded = _embedded_tool_calls(raw)
            if embedded and not whole_parse:
                results = []
                for etool, eargs in embedded:
                    er = await _safe_exec(agent, etool, eargs)
                    results.append(er)
                    _record({"step": step + 1, "type": "tool", "tool": etool, "result": er})
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "note": "你上一条输出把说明文字与工具 JSON 混在了一起，这些工具调用均已按序执行。请从现在起每次只输出一个纯 JSON 对象，不要附加说明文字。",
                            "tool_results": results,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                })
                continue

            # ---- 标准工具调用 ----
            # 工具名兼容多种写法：顶层 "tool"、action 嵌套 {"action":{"type":"tool","tool":"..."}}
            tool = data.get("tool")
            if not tool and isinstance(action, dict):
                tool = action.get("tool") or action.get("name")
            if tool:
                args = data.get("args")
                result = await _safe_exec(agent, tool, args if isinstance(args, dict) else {})
                _record({"step": step + 1, "type": "tool", "tool": tool, "result": result})
                messages.append({"role": "assistant", "content": raw})
                payload: dict = {"tool_result": result}
                err_sig = (result.get("error") or "")[:80] if isinstance(result, dict) else ""
                if err_sig and err_sig == last_error_sig[0]:
                    last_error_sig[1] += 1
                    if last_error_sig[1] == 2:
                        payload["note"] = "该工具已连续失败且错误相同，属于配置/资源缺失，重试不会成功。请立即停止重试：继续执行其余可行步骤，或 finish 并明确告知用户需要补充什么配置。"
                    elif last_error_sig[1] >= MAX_SAME_TOOL_ERRORS:
                        # 熔断：同一工具同一错误反复失败时强制收尾，避免烧光步数预算
                        _record({
                            "step": step + 1,
                            "type": "finish",
                            "text": (
                                f"工具 {tool} 连续 {last_error_sig[1]} 次以相同错误失败，已中止本次运行避免无效重试。"
                                f"最后错误：{err_sig}。已提交的后台任务不受影响；请排查配置后重试。"
                            ),
                        })
                        break
                else:
                    last_error_sig[0], last_error_sig[1] = err_sig, 1
                messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)})
                continue

            # ---- JSON 合法但缺 action/tool 字段：要求重新输出（同样有熔断上限） ----
            bad_output_streak += 1
            if bad_output_streak < MAX_BAD_OUTPUT_STREAK:
                _refeed(raw, "你上一条输出是 JSON 但缺少 tool 字段（工具名）。请重新输出完整格式：{\"action\":\"tool\",\"tool\":\"工具名\",\"args\":{...}}，或 {\"action\":\"finish\",\"summary\":\"<完整回答>\"}，不要省略字段。")
                continue
            _record({"step": step + 1, "type": "reply", "text": raw.strip() or _fallback_summary(agent)})
            break
        else:
            _record({"step": MAX_STEPS + 1, "type": "limit", "text": (
                "已达步数上限" + ("，已提交的任务在后台继续" if agent.jobs
                                  else "；本次未提交任何生成任务，目标未完成——请重试或更换模型")
            )})
    finally:
        _ACTIVE_CANCEL_FLAGS.discard(cancel_flag)
    summary = next(
        (s.get("text") for s in reversed(agent.steps) if s.get("type") in ("reply", "finish", "limit") and s.get("text")),
        "任务已执行",
    )
    # ---- 自动出片（代码触发而非模型决策）：
    # 1) 交接：本次写入了分镜且用户要的是成片 → 流水线接管生成（旁白代为合成）；
    # 2) 兜底网：用户明确要视频、但模型一个视频任务都没提交就收了尾（只回文字）→
    #    流水线照样出片。"用户不管怎么传都要能生成视频"的最后一道保证。
    async def _handoff() -> dict:
        from . import orchestrator
        pipe = await orchestrator.run_pipeline(
            agent, goal, on_step=on_step, _start_step=len(agent.steps),
            synthesize_missing_voice=True,  # 旁白是 Agent 本次写的，代为合成属既定流程
        )
        return {
            "reply": f"{summary}\n\n【系统流水线】{pipe.get('reply', '')}",
            "steps": agent.steps + pipe.get("steps", []),
            "updates": agent.updates,
            "jobs": agent.jobs,
            "exports": agent.exports,
            "voice_reused": agent.voice_reused,
            "project_changed": agent.project_changed or bool(pipe.get("project_changed")),
        }

    if agent.prompt_touched and _goal_wants_generation(goal):
        return await _handoff()
    if _goal_wants_video(goal) and not any(j.get("type") == "video" for j in agent.jobs):
        proj = agent._load_project() or {}
        has_work = any(
            isinstance(s, dict) and (s.get("prompt") or s.get("ref_images") or s.get("ref_audios"))
            for s in proj.get("scenes") or []
        )
        if has_work:
            return await _handoff()
    return {
        "reply": summary,
        "steps": agent.steps,
        "updates": agent.updates,
        "jobs": agent.jobs,
        "exports": agent.exports,
        "voice_reused": agent.voice_reused,
        "project_changed": agent.project_changed,
    }
