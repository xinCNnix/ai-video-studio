# AI Video Studio —— AI 视频生成整合工作台 v1.0

按《AI 视频生成整合工作台 软件开发设计书 v1.0》实现的完整可运行项目。

> 定位：**一个拥有 AI Prompt 辅助能力的多模型视频生成工作台**。
> 用一个统一、精美、易用的前端界面整合 LLM、TTS、生图模型和视频生成模型；各模块无强依赖，可单独使用。

## 技术方案（与设计书一致）

| 层 | 技术 |
| --- | --- |
| 前端 | Next.js 15（App Router）+ TypeScript + Tailwind CSS v4 + shadcn/ui 风格组件 + Zustand |
| 后端 | Python venv + FastAPI + Pydantic + httpx + cryptography + aiofiles |
| 存储 | 无数据库，全部本地文件 `data/`（Key 使用 AES-256-GCM 整体加密） |

## 目录结构

```
video_gener_agent/
├── backend/                  # FastAPI 后端（.venv 虚拟环境在本目录内）
│   ├── .venv/
│   ├── requirements.txt
│   └── app/
│       ├── main.py           # FastAPI 入口（127.0.0.1:8000）
│       ├── api/              # prompt / image / voice / video / jobs / projects / settings / agent / files
│       ├── providers/        # 模型适配器
│       │   ├── llm/openai_compatible.py     # OpenAI 兼容 LLM（MiniMax / OpenAI / 自定义）
│       │   ├── image/openai_image.py        # OpenAI 生图（gpt-image-1 / dall-e-3）
│       │   ├── tts/minimax_tts.py           # MiniMax TTS (t2a_v2)
│       │   └── video/minimax_h3.py          # MiniMax H3 视频（submit + 轮询）
│       │       └── generic_video.py         # 通用视频 API（POST /videos 模式）
│       ├── services/         # prompt / image / voice / video / project / file
│       ├── jobs/manager.py   # JobManager：job_id + data/jobs/{id}.json，无需 Celery
│       ├── config/           # settings.py（模型目录）+ key_manager.py（AES 加密）
│       └── prompt/           # h3_prompt.py / generic_prompt.py（系统提示词）
├── frontend/                 # Next.js 前端（127.0.0.1:3000）
│   ├── app/                  # 首页 / prompt / image / voice / video / studio / settings
│   ├── components/           # layout / prompt / image / voice / video / studio / common / agent / ui
│   ├── stores/               # app-store.ts / studio-store.ts（Zustand）
│   └── lib/                  # api.ts / types.ts / utils.ts
├── data/                     # 运行时数据（自动创建）
│   ├── config/providers.enc  # 加密的 API Key
│   ├── uploads/{image,audio,video}/
│   ├── outputs/{image,audio,video}/
│   ├── jobs/                 # 每个任务一个 JSON
│   ├── projects/             # Studio 项目
│   └── presets/              # Prompt 预设
└── start.bat                 # 一键启动
```

## 启动

```bat
:: 方式一：静默启动（推荐，无命令行窗口）
:: 双击 start-hidden.vbs —— 后台启动两个服务并自动打开浏览器
:: 日志输出到 data\logs\backend.log 和 frontend.log
:: 停止：双击 stop.bat

:: 方式二：可见窗口启动（想实时看日志时用）
start.bat

:: 方式三：手动
:: 后端（首次需先创建 venv）
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

:: 前端
cd frontend
npm install
npm run dev
```

打开 http://127.0.0.1:3000 。后端接口文档：http://127.0.0.1:8000/docs

## 使用流程

1. **设置 → API Keys**：添加厂商 Key（MiniMax / OpenAI / OpenAI Compatible / Generic Video API）。
   Key 加密存于 `data/config/providers.enc`，前端只显示掩码；可添加多 Key 并指定默认，支持 Test Connection。
2. **AI 视频**（核心页）：Model / Credential / Mode（Text・Image・Reference）/ Prompt（右上 `AI` 一键改写）/
   Duration（**1-15 秒滑杆任选**）/ Aspect / Resolution → Generate Video。
   中央预览随比例自适应（9:16 竖屏 / 16:9 横屏），生成状态只显示真实阶段（Submitted → Generating → Downloading，不虚构百分比），
   右侧 History 保存全部历史，点击即切回预览。
3. **AI Prompt**：输入创意 → 选择 Prompt Mode 与 Target Video Model（MiniMax H3 / Generic）→ 生成结构化 Prompt
   （Scene / Subject / Action Timeline / Camera / … / Constraints 分节展示），支持 Shorter・More Detailed・Improve Motion・
   Improve Camera・Improve Timing・Regenerate，可 Copy / Save / Send to Video。
4. **AI 图片**：Prompt + 参考图拖拽 + 比例 → 生成；结果悬浮提供 Download / Use as Reference / Send to Video。
5. **AI 语音**：文案 + 音色 / 语速 / 情感 → 大型播放器试听，Download / Send to Studio。
   声音克隆上传样本即音色：小米侧样本随身（每次请求 base64 锚定），配置 MiniMax Key 后可勾选
   **同时克隆到 MiniMax 服务端**（`/v1/files/upload` + `/v1/voice_clone` 生成永久 voice_id），
   speech 系列模型直接复用且语速/情感为结构化真实参数。
6. **Studio**：Scenes 列表（增 / 复制 / 删除 / 拖拽排序）+ 中央预览 + 右侧 Prompt・Image・Voice・Video 四个 Tab
   （场景内完成全部生成）+ 底部 Scene Strip；项目自动保存到 `data/projects/`。
7. **AI Assistant**：右下角 ✦ 悬浮助手，可理解当前页面/场景上下文，直接执行“把提示词改得更适合H3”“镜头不要移动”等修改。

## 关键设计对照

- Key 单点管理：只在「设置 → API Keys」录入/保存（AES-256-GCM 加密），生成面板不再重复选择凭据；请求自动使用对应厂商的默认 Key，多 Key 用「设为默认」切换，仅配单一厂商 Key 时全链路通用
- 模型不预制陈旧清单：LLM/生图从凭据的 `/models` 接口自动发现，音色从 MiniMax `get_voice` 发现（系统音色 + 克隆音色）；「设置 → Models」可添加任意自定义模型 ID / 音色 ID（私有部署、新模型均可），选择器记住上次选择
- 内置兜底仅厂商旗舰：LLM MiniMax-M2、TTS speech-2.5 系列、生图 gpt-image-1、视频 MiniMax H3 + Generic（v1.0 设计范围）
- 分辨率不写死：默认 **Auto（跟随模型默认，请求不携带分辨率参数）**，720p/1080p 仅为快捷项，Advanced 可输入任意值（如 768p / 2560x1440）原样传给厂商，不支持时厂商错误会显示在生成状态中
- 无数据库：配置 = 加密 JSON，任务 = `jobs/{job_id}.json`，素材 = `outputs/`，项目 = `projects/{project_id}.json`
- 不需要 Celery/Redis：FastAPI 内 JobManager + asyncio 任务，前端 3 秒轮询 `GET /api/jobs/{id}`
- 统一视频接口：`POST /api/video/generate`（provider / model / mode / prompt / duration / aspect_ratio / images），时长 1–15 秒任选
- 复杂参数默认隐藏：Advanced Settings 收起，首屏只有 Prompt・Reference・Duration・Aspect・Generate
- H3 作为标准而不是限制：内部统一 H3 结构化 Prompt，选 Generic 时输出通用视频语义

## 配置

- 前端 API 地址：环境变量 `NEXT_PUBLIC_API_BASE`（默认 `http://127.0.0.1:8000`）
- 数据目录：环境变量 `DATA_DIR`（默认项目根 `data/`）
- 新增视频厂商：在 `backend/app/providers/video/` 添加适配器并在 `registry.get_video` 注册，模型目录在 `backend/app/config/settings.py`

## 说明

- MiniMax/OpenAI 等接口路径按公开文档实现（`/v1/video_generation`、`/v1/query/video_generation`、`/v1/t2a_v2`、
  `/v1/chat/completions`、`/v1/images/generations|edits`）；如厂商版本调整，只需改对应 Adapter 内的常量。
- 视频时长支持 1–15 秒任选（滑杆），后端同步校验范围。
