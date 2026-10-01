# AI Video Studio —— AI 视频生成整合工作台 v2.0

> 定位：**Agent-First 的多模型视频生成工作台**。
> Studio 以 Agent/流水线为主控：投放任意格式剧本 → 自动拆分镜 → 精确分配参考图/参考音频 → 确认后逐镜生成 → 拼接成片。
> 稳定性由 **Agent Harness 工程层**保证（模型不可信，骨架兜底），不依赖 prompt 和模型自觉。

## 技术方案

| 层 | 技术 |
| --- | --- |
| 前端 | Next.js 15（App Router）+ TypeScript + Tailwind CSS v4 + shadcn/ui 风格组件 + Zustand |
| 后端 | Python venv + FastAPI + Pydantic v2 + httpx + cryptography + aiofiles |
| 存储 | 无数据库，全部本地文件 `data/`（Key 使用 AES-256-GCM 整体加密） |

## v2 架构（Studio + Agent 重构）

```
用户投放剧本+素材（IntakePanel / Agent 对话）
  ↓ POST /api/projects/{id}/pipeline/start
pipeline 状态机（services/pipeline/，跑在 harness 之上）：
  intake(纯代码) → normalize(LLM信封) → plan(LLM信封) → bind(确定性+LLM兜底)
  → review 门禁(默认暂停等确认) → generate(声明式调谐) → assemble(ffmpeg)
  ↓ 每阶段 checkpoint 持久化，崩溃可续跑；全部 LLM 输出过 schema 校验信封
generation_service（唯一生成入口：手动按钮 = agent 工具 = 流水线，规则只有一份）
  ↓ 写回只经 project_store（版本乐观锁），结果经 SSE 实时推送前端
```

### Harness 层（backend/app/harness/，稳定性核心）

| 模块 | 机制 |
| --- | --- |
| `errors.py` | 错误三分类：Retryable（自动重试）/ Fatal（干净中止，保留产物）/ NeedsUserInput（进确认面板，绝不瞎猜） |
| `state.py` | 项目 v2 Pydantic 模型 + 写入口不变量：非法状态（素材引用不存在、音频>3段、时长越界）在落盘前拒绝 |
| `llm 信封`（pipeline/llm_client.py） | 结构化调用：JSON 解析（围栏/混排/字符串换行容忍）→ schema 校验 → 错误回喂修复×2 → 网络退避重试×3 |
| `checkpoint.py` | 阶段进度持久化在项目本身；断点续跑；悬空 running 由看门狗转 paused |
| `watchdog.py` | 项目级 single-flight 运行锁 + 心跳：同项目同时只允许一个 agent/pipeline run，卡死判死 |
| `trace.py` | run 级事件溯源账本 `data/runs/*.jsonl`：每次 LLM 调用/修复/工具/阶段全在案，可回放 |
| `reconcile.py`（services/） | 声明式调谐器：每镜期望资产 vs 实际资产求差 → 幂等提交（去重键）→ 失败自动重试 ≤2 次 → 耗尽转 failed |

### 关键设计决策

- **素材先于分镜**：plan 阶段的 LLM 输入就带完整素材清单（id/类型/文件名台词/时长），分镜直接声明要哪些素材；bind 阶段确定性对号 + LLM 兜底带置信度，低置信进 `pending_bindings` 由用户确认——参考图绝不再"挂到全部分镜"
- **单一代码路径**：关键帧/配音/视频生成只有 generation_service 一份实现（mode 判定、`<Audio N>` 重编号、契约校验、时长钳制、模型选择全部单点）；视频模型取自项目设置，不再硬编码目录第一项
- **单一事实源**：前端是命令发送端 + SSE 视图（无全量 PUT、无本地编辑副本），409 冲突自动刷新重放；消灭前后端双写竞态
- **任意剧本格式**：normalize 阶段先判别类型（故事/标准剧本/已有分镜表/广告口播/仅素材）再逐类抽取，长剧本确定性分块（输入侧分块，杜绝输出被 max_tokens 截断整段丢失）
- **review 门禁**：分镜+素材分配完成后默认暂停等用户确认再生成（会消耗额度）；项目设置可开"全自动"
- **幂等与断点**：任意阶段 kill 进程可从检查点续跑；视频任务带云端任务号，服务重启自动续轮询并按 Prompt 指纹写回
- **导出修复**：无旁白分镜自动补静音轨，concat 不再因音轨不一致失败；ffmpeg 带超时

## 目录结构（主要变化）

```
backend/app/
├── harness/                  # ★ 稳定性工程层（errors/bus/trace/checkpoint/watchdog/state）
├── services/
│   ├── pipeline/             # ★ 流水线：llm_client(信封) / schemas / stages / 状态机
│   ├── project_store.py      # ★ v2 唯一写入口（乐观锁+不变量+事件）
│   ├── generation_service.py # ★ 统一生成入口
│   ├── reconcile.py          # ★ 声明式调谐器（generate 阶段引擎）
│   ├── agent_v2.py           # ★ 瘦工具循环（路由直通 + 熔断 + 取消）
│   └── material_match.py     # ★ 素材↔分镜确定性匹配（同台词去重/台词对号/标签重编号）
├── api/                      # projects(命令式+SSE) / agent_v2 / 其余工具页不动
├── providers/ config/ jobs/  # 适配器/加密/任务（未变更）
frontend/
├── app/studio/               # ★ agent-first 新布局
├── components/studio-v2/     # ★ intake/pipeline-bar/shot-board/shot-drawer/agent-console
├── stores/studio-v2-store.ts # ★ SSE 订阅 + 命令发送（无 save()）
└── lib/api-v2.ts types-v2.ts # ★ v2 客户端与类型
```

数据目录新增：`data/runs/`（run trace 账本）、`data/projects_archive/`（v1 项目自动归档，只读保留）。

## 启动

```bat
:: 方式一：静默启动（推荐）—— 双击 start-hidden.vbs；停止：stop.bat
:: 方式二：可见窗口 —— start.bat
```

打开 http://127.0.0.1:3000 ，后端文档 http://127.0.0.1:8000/docs 。

## 使用流程（Studio 为核心）

1. **设置 → API Keys**：添加厂商 Key（同 v1，AES 加密存储，模型自动发现）。
2. **Studio**：投放剧本/素材（粘贴文本或拖入 md/txt/图/音）→ 启动流水线 →
   看板实时显示阶段进度（SSE）→ review 面板确认分镜与素材分配（歧义素材逐项指派）→
   确认后逐镜生成（关键帧→配音→视频，失败自动重试）→ 导出成片。
3. **Agent 控制台**（Studio 左栏）：粘贴剧本自动进流水线；"继续生成"推进；也可对话改单镜 Prompt/时长/素材。
4. **独立工具页**（Prompt/图片/语音/视频）：保持 v1 用法不变。

## API 摘要（v2）

- `POST/GET /api/projects`、`PATCH /api/projects/{id}`（base_version 乐观锁，409=并发写）
- `PATCH /api/projects/{id}/scenes/{scene_id}`、`POST .../scenes`、`DELETE`、`POST .../scenes/reorder`（字段级命令，无全量 PUT）
- `POST /api/projects/{id}/materials`、`POST/DELETE .../bindings`、`POST .../bindings/confirm`
- `POST /api/projects/{id}/pipeline/start|advance`、`GET .../runs`、`GET .../runs/{run_id}/trace`
- `POST /api/projects/{id}/scenes/{scene_id}/generate`（keyframe/voice/video，手动与 agent 同路径）
- `GET /api/projects/{id}/events`（SSE 项目快照流，按 version 去重）
- `POST /api/agent/run|run/stream|chat|cancel`

## 自检（不联网、不烧额度，FakeLLM 驱动）

```bash
cd backend
.venv/Scripts/python.exe tests/store_selfcheck.py      # 状态层：乐观锁/不变量/归档（14 项）
.venv/Scripts/python.exe tests/reconcile_selfcheck.py  # 调谐器：顺序/幂等/重试/取消（21 项）
.venv/Scripts/python.exe tests/pipeline_v2_selfcheck.py # 流水线：信封故障注入矩阵+H3六段式闸门+端到端（36 项）
.venv/Scripts/python.exe tests/agent_v2_selfcheck.py   # Agent：路由/熔断/取消/single-flight（14 项）
.venv/Scripts/python.exe tests/voice_segment_selfcheck.py # 配音工作台：片段标记解析/分段合成/静音拼接（23 项）
.venv/Scripts/python.exe tests/storyboard_fidelity_replay.py # storyboard 保真回归：真实事故项目重放（23 项）
.venv/Scripts/python.exe tests/audio_fit_test.py       # 台词时长闸门：上传即显示精确时长/超镜自动校准/超模型上限拦截（19 项）
```

## 配置

- 前端 API 地址：`NEXT_PUBLIC_API_BASE`（默认 `http://127.0.0.1:8000`）；数据目录：`DATA_DIR`（默认 `data/`）
- 新增视频厂商：`backend/app/providers/video/` 添加适配器并在 registry 注册；工作流入参契约见 `config/settings.py` 的 `AUTODL_WORKFLOW_SPECS`

## 迁移说明（v1 → v2）

- v1 项目（`data/projects/` 旧 JSON）已整体归档至 `data/projects_archive/`，只读保留、不迁移
- v1 的全量 PUT 保存、前端轮询回写、双路径生成规则已随重构删除
- 历史回归场景（素材铁律、音频标签编号、截断防护等）已移植进 v2 自检矩阵
