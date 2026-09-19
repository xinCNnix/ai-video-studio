"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { Loader2, Paperclip, Send, Sparkles, Square } from "lucide-react";
import { toast } from "sonner";
import { api, attachmentKind, fileUrl, pollJob, uploadAttachment, API_BASE, type AgentLlmOption, type UploadedAttachment } from "@/lib/api";
import { hydrateAgentChat, useAgentChatStore, type ChatArchive } from "@/lib/agent-chat-store";
import { useAppStore } from "@/stores/app-store";
import { useStudioStore } from "@/stores/studio-store";
import { Button } from "@/components/ui/button";
import { SearchableSelect, type SearchableItem } from "@/components/common/searchable-select";
import { cn } from "@/lib/utils";

const SUGGESTIONS = [
  "把提示词改得更适合 H3",
  "这个剧本有什么问题？帮我看一下",
  "把这个场景做成一条 5 秒视频：医生坐在诊室里向女孩解释白斑治疗误区",
  "把下面剧本拆成 5 个分镜并逐个生成视频：女孩发现手背有白斑→去医院→医生解释误区→细胞科普动画→拥抱结尾",
];

// 侧栏显式选择的模型（持久化；随每次 Agent 运行显式传给后端）
const MODEL_LS_KEY = "agentModelOverride";
const DEFAULT_MODEL_VALUE = "__default__";

type PickedModel = { model: string; credential_id: string };

function loadPickedModel(): PickedModel | null {
  try {
    const raw = window.localStorage.getItem(MODEL_LS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (p?.model && p?.credential_id) return { model: String(p.model), credential_id: String(p.credential_id) };
  } catch {
    /* ignore */
  }
  return null;
}

/**
 * 全局 AI 助手侧栏：xl 以上推挤页面内容，小屏以浮层覆盖。
 * 聊天记录持久化在 localStorage，刷新页面不清空，点「清空对话」才清除。
 */
export function AgentSidebar() {
  const pathname = usePathname();
  const open = useAppStore((s) => s.agentOpen);
  const messages = useAgentChatStore((s) => s.messages);
  const addMessage = useAgentChatStore((s) => s.addMessage);
  const archives = useAgentChatStore((s) => s.archives);
  const archiveAndClear = useAgentChatStore((s) => s.archiveAndClear);
  const deleteArchive = useAgentChatStore((s) => s.deleteArchive);
  const project = useStudioStore((s) => s.project);
  const currentSceneId = useStudioStore((s) => s.currentSceneId);
  const updateScene = useStudioStore((s) => s.updateScene);

  const [input, setInput] = useState("");
  // busy 同步进全局 store：图片页据此开启历史轮询
  const busy = useAppStore((s) => s.agentBusy);
  const setBusy = useAppStore((s) => s.setAgentBusy);
  const [stopping, setStopping] = useState(false);
  const [progress, setProgress] = useState<{ done: number; failed: number; total: number } | null>(null);
  // 流式步骤日志：Agent 运行时实时滚动显示，不再只转圈
  const [liveSteps, setLiveSteps] = useState<string[]>([]);
  // 模型选单：当前生效模型 + 全部可选项；picked 为侧栏显式覆盖（localStorage 持久化）
  const [llmOptions, setLlmOptions] = useState<AgentLlmOption[]>([]);
  const [effective, setEffective] = useState<{ model: string | null; credential_id: string | null } | null>(null);
  const [picked, setPicked] = useState<PickedModel | null>(null);
  // 存档面板：回看历史对话（只读，不作为 Agent 记忆）
  const [showArchives, setShowArchives] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 附件：助手侧栏独立上传处——参考图/配音直接给 URL，md/txt 剧本发送时内联全文
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<UploadedAttachment[]>([]);
  const [uploading, setUploading] = useState(false);

  const scene = project?.scenes.find((s) => s.id === currentSceneId) ?? null;
  const isStudio = pathname.startsWith("/studio");

  useEffect(() => {
    // 恢复持久化的聊天记录与开合状态（仅客户端执行一次）
    hydrateAgentChat();
    setPicked(loadPickedModel());
    // 清掉旧版「执行模式」开关的残留存储（双模式已拆除）
    try {
      window.localStorage.removeItem("agentRouteMode");
    } catch {
      /* ignore */
    }
    // 模型选单：当前生效模型 + 各凭据端点发现的模型（后端有 10 分钟缓存）
    api
      .getAgentLlmOptions()
      .then(({ current, options }) => {
        setEffective({ model: current.model, credential_id: current.credential_id });
        setLlmOptions(options);
      })
      .catch(() => undefined);
    try {
      if (window.localStorage.getItem("agentOpen") === "1") {
        useAppStore.getState().setAgentOpen(true);
      }
    } catch {
      /* ignore */
    }
  }, []);

  const modelValue = picked ? `${picked.model}||${picked.credential_id}` : DEFAULT_MODEL_VALUE;
  const modelItems: SearchableItem[] = useMemo(() => {
    const items: SearchableItem[] = [
      { value: DEFAULT_MODEL_VALUE, label: `跟随默认（${effective?.model || "未配置"}）`, hint: "设置 → Models" },
      ...llmOptions.map((o) => ({ value: `${o.model}||${o.credential_id}`, label: o.label, hint: o.provider })),
    ];
    // 显式选择的模型不在发现列表里（如端点临时拉取失败）时补一项，保证名称照常显示
    if (picked && !items.some((i) => i.value === modelValue)) {
      items.push({ value: modelValue, label: picked.model, hint: "手动" });
    }
    return items;
  }, [llmOptions, effective, picked, modelValue]);

  const changeModel = (v: string) => {
    if (v === DEFAULT_MODEL_VALUE) {
      setPicked(null);
      try {
        window.localStorage.removeItem(MODEL_LS_KEY);
      } catch {
        /* ignore */
      }
      return;
    }
    const [model, credential_id] = v.split("||");
    if (!model || !credential_id) return;
    setPicked({ model, credential_id });
    try {
      window.localStorage.setItem(MODEL_LS_KEY, JSON.stringify({ model, credential_id }));
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, liveSteps, showArchives, open]);

  // 输入框随内容自动增高：单行放不下长文案，编辑时看不全；超过 max-h 后在框内滚动
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  const applyJobToScene = async (jobId: string, type: string, sceneIndex?: number, projectId?: string | null) => {
    try {
      const job = await pollJob(jobId, () => undefined);
      if (job.status !== "completed" || !job.output) {
        toast.error(`Agent 任务 ${jobId} 失败：${job.error || "未知错误"}`);
        return;
      }
      // 分镜编排任务：按场景序号写回，且必须校验项目归属——任务属于哪个项目就写哪个项目，
      // 用户中途切换项目时绝不写入「当前打开的」项目（防止 A 项目的视频串进 B 项目的分镜）。
      // 后端写回另有 Prompt 指纹校验兜底，这里是前端第一道防线。
      if (typeof sceneIndex === "number") {
        const liveProject = useStudioStore.getState().project;
        if (projectId && liveProject && liveProject.id !== projectId) {
          toast.success(`分镜${type === "video" ? "视频" : type === "image" ? "图片" : "语音"}已完成（属于原项目，已由后台写回，不影响当前项目）`);
          return;
        }
        const target = liveProject?.scenes[sceneIndex];
        if (target) {
          if (type === "video") updateScene(target.id, { video: job.output, duration: (job.params?.duration as number) || target.duration });
          else if (type === "image") updateScene(target.id, { image: job.output });
          else if (type === "tts") updateScene(target.id, { voice: job.output });
          toast.success(`分镜 ${sceneIndex + 1} 的${type === "video" ? "视频" : type === "image" ? "图片" : "语音"}已生成并写入`);
          return;
        }
      }
      if (isStudio && scene) {
        if (type === "video") updateScene(scene.id, { video: job.output });
        else if (type === "image") updateScene(scene.id, { image: job.output });
        else if (type === "tts") updateScene(scene.id, { voice: job.output });
        toast.success(`Agent 生成的${type === "video" ? "视频" : type === "image" ? "图片" : "语音"}已写入当前 Scene`);
      } else {
        toast.success(`Agent 任务完成：${job.output}`);
      }
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const runTask = async (goal: string, history: { role: string; content: string }[] = [], display?: string) => {
    addMessage({ role: "user", content: display ?? goal });
    setBusy(true);
    setProgress(null);
    setLiveSteps([]);
    try {
      // 流式执行：每个工具步骤实时上屏，不再等整个长请求结束才看到进展
      const res = await api.runAgentStream(
        {
          goal,
          history,
          page: pathname,
          scene_title: scene?.title ?? "",
          scene_description: scene?.description ?? "",
          scene_prompt: scene?.prompt ?? "",
          scene_narration: scene?.narration ?? "",
          scene_image: scene?.image ?? null,
          aspect_ratio: project?.aspect_ratio ?? "16:9",
          project_id: project?.id ?? null,
          // 侧栏显式选择的模型：随请求传给后端，覆盖设置里的默认文本模型
          ...(picked ? { model: picked.model, credential_id: picked.credential_id } : {}),
        },
        (step) => {
          if (step.type === "tool") {
            setLiveSteps((prev) => [...prev, `▸ ${step.tool}：${ok(step.result)}`]);
          }
        }
      );
      // 先应用文本更新
      if (res.updates?.prompt && scene) updateScene(scene.id, { prompt: res.updates.prompt });
      if (res.updates?.description && scene) updateScene(scene.id, { description: res.updates.description });
      if (res.updates?.narration && scene) updateScene(scene.id, { narration: res.updates.narration });
      // 分镜被改写：从后端重新加载项目
      if (res.project_changed) {
        await useStudioStore.getState().reloadProject();
        await useStudioStore.getState().loadProjects();
      }
      // 步骤日志
      // 只展示工具步骤；finish/reply 步骤的文本与 res.reply 相同，拼上会重复
      const stepsText = res.steps
        .filter((s) => s.type === "tool")
        .map((s) => `▸ ${s.tool}：${ok(s.result)}`)
        .join("\n");
      let content = cleanLLMText(`${res.reply}\n\n${stepsText}`);
      if (res.exports?.length) {
        content += `\n\n🎬 成片已导出：${fileUrl(res.exports[res.exports.length - 1])}?dl=1`;
        toast.success("成片导出完成");
      }
      addMessage({ role: "assistant", content });
      // 轮询生成的任务并写回场景（带 scene_index 的按序号回写），同时统计完成进度
      const ttsJobs = res.jobs.filter((j) => j.type === "tts");
      if (ttsJobs.length > 0) {
        setProgress({ done: 0, failed: 0, total: ttsJobs.length });
        ttsJobs.forEach((j) =>
          pollJob(j.job_id, () => undefined)
            .then((job) =>
              setProgress((p) =>
                p ? { ...p, done: p.done + (job.status === "completed" ? 1 : 0), failed: p.failed + (job.status === "failed" ? 1 : 0) } : p
              )
            )
            .catch(() => setProgress((p) => (p ? { ...p, failed: p.failed + 1 } : p)))
        );
        Promise.allSettled(ttsJobs.map((j) => pollJob(j.job_id, () => undefined))).then(async () => {
          const finals = await Promise.all(ttsJobs.map((j) => api.getJob(j.job_id).catch(() => null)));
          const okJobs = finals.filter((j) => j?.status === "completed");
          const failJobs = finals.filter((j) => j?.status === "failed");
          const reused = res.voice_reused ?? [];
          const allIds = [...reused, ...okJobs.map((j) => j!.id)].join(",");
          const zipUrl = allIds ? `${API_BASE}/api/jobs/batch-download/zip?ids=${allIds}` : "";
          let content = `🔊 配音结束：成功 ${okJobs.length}/${ttsJobs.length}`;
          if (reused.length) content += `（另有 ${reused.length} 条之前已配过，自动沿用，未重复生成）`;
          if (failJobs.length) {
            content += `
❌ 失败 ${failJobs.length} 条（对我说「把失败的句子重新配音」即可重试）：
${failJobs
              .map((j) => `- ${String(j?.params?.text ?? j?.id).slice(0, 30)}`)
              .join("\n")}`;
          }
          if (zipUrl) content += `
📦 本批打包下载（${okJobs.length} 条）：${zipUrl}`;
          addMessage({ role: "assistant", content });
        });
      }
      res.jobs.forEach((j) => void applyJobToScene(j.job_id, j.type, j.scene_index, j.project_id));
    } catch (err) {
      addMessage({ role: "assistant", content: `出错了：${(err as Error).message}` });
    } finally {
      setBusy(false);
      setLiveSteps([]);
    }
  };

  const stop = async () => {
    if (!busy) return;
    setStopping(true);
    try {
      await api.cancelAgent();
      toast.info("已请求停止：Agent 将在当前步骤后中止（已提交的后台任务不受影响）");
    } catch {
      toast.error("停止请求失败，请重试");
    } finally {
      setStopping(false);
    }
  };

  /** 清空对话 = /reset：当前对话先存档（可回看），再彻底清空 Agent 记忆。 */
  const resetConversation = () => {
    if (busy) return;
    const archive = archiveAndClear();
    setShowArchives(false);
    if (archive) {
      toast.success("已存档并清空：Agent 记忆已重置，旧对话不会影响后续任务（存档面板可回看）");
    } else {
      toast.success("对话已清空：Agent 记忆已重置");
    }
  };

  const pickFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const kind = attachmentKind(file);
        if (!kind) {
          toast.error(`不支持的文件类型：${file.name}（支持图片/音频/视频/md/txt）`);
          continue;
        }
        const att = await uploadAttachment(file, kind);
        setAttachments((prev) => [...prev, att]);
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const send = (text: string) => {
    const message = text.trim();
    if ((!message && !attachments.length) || busy) return;
    // 附件拼进任务正文：素材给 URL（助手用 attach_scene_refs 挂载/分发），剧本正文整段内联
    let goal = message;
    if (attachments.length) {
      const parts = attachments.map((a) =>
        a.kind === "doc" && a.text
          ? `- 剧本文档 ${a.name}（${a.url}）全文：\n${a.text}`
          : a.kind === "doc"
            ? `- 剧本文档 ${a.name}：${a.url}（用 read_doc 读取全文）`
            : `- ${a.kind === "image" ? "参考图" : a.kind === "audio" ? "配音" : "参考视频"}：${a.url}`
      );
      goal = `${message || "请按以下素材处理："}\n\n【本次附件】\n${parts.join("\n")}`;
    }
    // 聊天里只展示文本 + 附件名，不把剧本全文刷屏
    const display = attachments.length
      ? `${message}\n${attachments.map((a) => `📎 ${a.name}`).join("\n")}`.trim()
      : message;
    // 携带最近对话历史（不含刚追加的这条），让 Agent 有上下文记忆
    const history = messages
      .slice(-12)
      .filter((m) => m.content.trim())
      .map((m) => ({ role: m.role, content: m.content.slice(0, 3000) }));
    setInput("");
    setAttachments([]);
    setShowArchives(false);
    void runTask(goal, history, display);
  };

  if (!open) return null;

  return (
    <aside
      className={cn(
        "flex w-[380px] shrink-0 flex-col overflow-hidden border-border bg-card shadow-2xl",
        // 小屏（<xl）：浮层覆盖在页面右侧，不挤压内容
        "fixed bottom-0 right-0 top-14 z-40 border-l",
        // xl 及以上：占位的常驻侧栏，推挤页面内容
        "xl:static xl:z-auto xl:h-[calc(100vh-3.5rem)] xl:shadow-none"
      )}
    >
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Sparkles className="size-4 text-primary" />
            AI Assistant
          </div>
        </div>
        {/* 模型选单：显式标出当前使用的模型，可随时切换（对下一次运行生效） */}
        <div className={cn("mt-2", busy && "pointer-events-none opacity-60")}>
          <SearchableSelect
            items={modelItems}
            value={modelValue}
            onChange={changeModel}
            placeholder="选择模型"
            searchPlaceholder="搜索模型…"
            emptyText="未发现可用模型"
          />
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[11px] text-muted-foreground">
          <span className="truncate">
            {`提问、改文案、生成图片/配音/视频都可以，直接说需求${isStudio && scene ? ` · Scene ${scene.title}` : ""}`}
          </span>
          <span className="flex shrink-0 items-center gap-2">
            {archives.length > 0 && (
              <button
                type="button"
                onClick={() => setShowArchives((v) => !v)}
                className="hover:text-foreground cursor-pointer"
                title="查看历史对话存档（只读，Agent 不会记得存档内容）"
              >
                存档 {archives.length}
              </button>
            )}
            {messages.length > 0 && (
              <button
                type="button"
                onClick={resetConversation}
                className="shrink-0 hover:text-foreground cursor-pointer"
                title="先存档当前对话，再彻底清空：Agent 记忆归零（/reset）"
              >
                清空对话
              </button>
            )}
          </span>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {showArchives ? (
          <div className="space-y-3">
            <button
              type="button"
              onClick={() => setShowArchives(false)}
              className="cursor-pointer text-xs text-muted-foreground hover:text-foreground"
            >
              ← 返回当前对话
            </button>
            <ArchiveList archives={archives} onDelete={deleteArchive} />
          </div>
        ) : (
          <>
            {messages.length === 0 && (
              <div className="space-y-2">
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {"我是你的创作助手：提问、改 Prompt、拆分镜、生成配音/图片/视频都行。试试："}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => setInput(s)}
                      className="rounded-full border border-border bg-secondary/60 px-2.5 py-1 text-left text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) => (
              <Bubble key={i} role={m.role} content={m.content} />
            ))}
            {busy && (
              <div className="space-y-1.5 text-xs text-muted-foreground">
                <div className="flex items-center gap-2">
                  <Loader2 className="size-3.5 animate-spin" />
                  {`Agent 执行中 · ${(picked?.model ?? effective?.model) || "模型未配置"}（步骤实时显示，可点右侧按钮停止）`}
                </div>
                {liveSteps.length > 0 && (
                  <div className="max-h-44 space-y-0.5 overflow-y-auto rounded-lg bg-secondary/60 p-2 font-mono text-[11px] leading-relaxed">
                    {liveSteps.map((s, i) => (
                      <div key={i} className="break-all">
                        {s}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div className="border-t border-border p-3">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          accept="image/*,audio/*,video/*,.md,.txt"
          onChange={(e) => {
            void pickFiles(e.target.files);
            e.currentTarget.value = "";
          }}
        />
        {attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {attachments.map((a, i) => (
              <span
                key={`${a.url}_${i}`}
                className="inline-flex max-w-full items-center gap-1 rounded-full border border-border bg-secondary/60 px-2 py-0.5 text-[10px] text-foreground"
                title={a.name}
              >
                <span>{a.kind === "doc" ? "📄" : a.kind === "image" ? "🖼" : a.kind === "audio" ? "🎙" : "🎬"}</span>
                <span className="max-w-36 truncate">{a.name}</span>
                <button
                  type="button"
                  className="cursor-pointer text-muted-foreground hover:text-foreground"
                  title="移除附件"
                  onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            rows={1}
            placeholder="提问或描述任务；📎 可上传参考图/配音/md 剧本"
            className="max-h-48 flex-1 resize-none overflow-y-auto rounded-lg border border-border bg-secondary/50 px-3 py-2 text-xs text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <Button
            size="icon"
            variant="ghost"
            className="shrink-0"
            title="上传附件：参考图 / 配音 / 视频 / md·txt 剧本（随下一条消息一起发送）"
            disabled={busy || uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? <Loader2 className="size-4 animate-spin" /> : <Paperclip className="size-4" />}
          </Button>
          {busy ? (
            <Button size="icon" variant="outline" title="手动停止" onClick={() => void stop()} disabled={stopping}>
              {stopping ? <Loader2 className="size-4 animate-spin" /> : <Square className="size-4" />}
            </Button>
          ) : (
            <Button size="icon" variant="accent" disabled={!input.trim() && !attachments.length} onClick={() => send(input)}>
              <Send className="size-4" />
            </Button>
          )}
        </div>
      </div>
    </aside>
  );
}


/** 单条聊天气泡：当前对话与存档面板共用。 */
function Bubble({ role, content }: { role: "user" | "assistant"; content: string }) {
  return (
    <div
      className={cn(
        "max-w-[88%] whitespace-pre-wrap rounded-xl px-3 py-2 text-xs leading-relaxed",
        role === "user" ? "accent-gradient ml-auto text-white" : "bg-secondary text-foreground"
      )}
    >
      {linkify(content)}
    </div>
  );
}

/** 历史对话存档列表：只读回看，删除后不可恢复；内容绝不作为 Agent 记忆喂回。 */
function ArchiveList({ archives, onDelete }: { archives: ChatArchive[]; onDelete: (id: string) => void }) {
  if (archives.length === 0) {
    return (
      <p className="text-xs leading-relaxed text-muted-foreground">
        还没有存档。点「清空对话」时，当前对话会先存档到这里，再彻底清空 Agent 记忆（相当于 /reset）。
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        共 {archives.length} 份存档（最多保留 20 份）。存档仅供回看，Agent 不会记得里面的内容。
      </p>
      {archives.map((a) => {
        const first = a.messages.find((m) => m.role === "user")?.content ?? a.messages[0]?.content ?? "";
        return (
          <details key={a.id} className="rounded-lg border border-border bg-secondary/40 px-3 py-2">
            <summary className="cursor-pointer text-xs text-foreground">
              {new Date(a.archived_at).toLocaleString()} · {a.messages.length} 条
              {first && <span className="text-muted-foreground"> · {first.slice(0, 24)}</span>}
            </summary>
            <div className="mt-2 space-y-2">
              {a.messages.map((m, i) => (
                <Bubble key={i} role={m.role} content={m.content} />
              ))}
            </div>
            <button
              type="button"
              onClick={() => onDelete(a.id)}
              className="mt-2 cursor-pointer text-[11px] text-muted-foreground hover:text-destructive"
            >
              删除此存档
            </button>
          </details>
        );
      })}
    </div>
  );
}


/** 把消息文本里的 URL 渲染成可点击链接（用于 Agent 给出的下载地址等）。 */
function linkify(text: string) {
  const parts = text.split(/(https?:\/\/[^\s）)]+)/g);
  return parts.map((p, i) =>
    /^https?:\/\//.test(p) ? (
      <a key={i} href={p} target="_blank" rel="noreferrer" className="break-all text-primary underline underline-offset-2">
        {p}
      </a>
    ) : (
      <span key={i}>{p}</span>
    )
  );
}

function ok(result: unknown): string {
  try {
    const r = result as { ok?: boolean; job_id?: string; error?: string; status?: string };
    if (r?.job_id) return `已提交任务 ${r.job_id}`;
    if (r?.ok) return "完成";
    return r?.error || "完成";
  } catch {
    return "";
  }
}

/** 清洗 LLM 展示文本：剥离思考块、把字面 \n 归一为真实换行、压缩多余空行。 */
function cleanLLMText(t: string): string {
  return t
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/\\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
