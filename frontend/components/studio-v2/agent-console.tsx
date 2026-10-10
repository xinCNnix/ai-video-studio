"use client";

/** Agent 控制台：对话 + 步骤流展示（SSE）。创作类请求由后端路由直通流水线。 */
import { useEffect, useRef, useState } from "react";
import { Bot, Eraser, Loader2, Paperclip, Send, User, Wrench } from "lucide-react";
import { toast } from "sonner";
import { attachmentKind } from "@/lib/api";
import { runAgentV2Stream, uploadMaterial } from "@/lib/api-v2";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { MdText } from "@/components/studio-v2/md-text";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "assistant";
  text: string;
  steps?: { tool?: string; ok?: boolean; note?: string }[];
}

/** 后端 route() 的模式 → 控制台可读标签（tools_voice 等原始值不给用户看）。 */
const ROUTE_LABELS: Record<string, string> = {
  pipeline_start: "拆分镜流水线",
  classify: "意图分流",
  pipeline_advance: "继续生成",
  export: "导出成片",
  tools: "创作助理",
  tools_voice: "配音台词师",
};

/** 超长消息（粘贴的剧本等）折叠显示，避免撑爆控制台。 */
const LONG_LINES = 8;
const LONG_CHARS = 300;

function MsgBubble({ m }: { m: Msg }) {
  const [open, setOpen] = useState(false);
  const isLong = m.text.length > LONG_CHARS || m.text.split("\n").length > LONG_LINES;
  const text =
    isLong && !open
      ? `${m.text.split("\n").slice(0, LONG_LINES).join("\n").slice(0, LONG_CHARS)}…`
      : m.text;
  return (
    <div
      className={cn(
        "inline-block max-w-[92%] rounded-xl px-3 py-2 text-left text-xs leading-relaxed",
        m.role === "user" ? "whitespace-pre-wrap bg-primary/15 text-primary-foreground" : "bg-secondary/50",
      )}
    >
      {m.role === "user" ? text : <MdText text={text} />}
      {isLong && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="mt-1 block cursor-pointer text-[10px] text-primary/80 hover:underline"
        >
          {open ? "收起" : `展开全部（${m.text.length} 字）`}
        </button>
      )}
    </div>
  );
}

export function AgentConsole() {
  const project = useStudioV2Store((s) => s.project);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, running]);

  // 切换项目即清空对话：messages 会作为 history 随请求发给 agent，
  // 跨项目残留会把上一个项目的上下文串进新项目的运行
  const projectId = project?.id;
  useEffect(() => {
    setMessages([]);
  }, [projectId]);

  const send = async (text?: string) => {
    const goal = (text ?? input).trim();
    if (!goal || running || !project) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    // 携带最近对话历史（不含本条，追加前先取）：后端工具循环据此维持上下文记忆
    const history = messages
      .slice(-12)
      .filter((m) => m.text.trim())
      .map((m) => ({ role: m.role, content: m.text.slice(0, 3000) }));
    setMessages((m) => [...m, { role: "user", text: goal }]);
    setRunning(true);
    const steps: Msg["steps"] = [];
    let reply = "";
    try {
      await runAgentV2Stream({ goal, project_id: project.id, history }, (e) => {
        if (e.type === "route") {
          steps.push({ note: `路由：${ROUTE_LABELS[e.mode ?? ""] ?? e.mode ?? ""}` });
          setMessages((m) => [...m]);
        }
        if (e.type === "step" && e.tool) {
          // 同一工具后端发两次：执行前（无 ok）与执行后（带 ok），合并为一行
          const last = steps[steps.length - 1];
          if (last && last.tool === e.tool && last.ok === undefined) last.ok = e.ok ?? true;
          else steps.push({ tool: e.tool, ok: e.ok });
          setMessages((m) => [...m]);
        }
        if (e.type === "done") {
          reply = (e.result as { reply?: string } | undefined)?.reply ?? e.reply ?? "";
        }
      });
      setMessages((m) => [...m, { role: "assistant", text: reply || "（完成）", steps: [...steps] }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", text: `执行失败：${(err as Error).message}` }]);
    } finally {
      setRunning(false);
    }
  };

  /** 拖入 / 粘贴 / 选择文件：剧本（.md/.txt）上传后内联全文直接提交——与粘贴剧本同一行为，
   * 长文本会被后端路由直通流水线；图/音/视上传后以 URL 随指令提交，由 Agent 工具处理。 */
  const addFiles = async (files: FileList | File[]) => {
    if (running || uploading) return;
    const list = Array.from(files);
    if (!list.length) return;
    if (!project) {
      toast.error("请先创建或打开项目，再投放剧本");
      return;
    }
    for (const f of list) {
      if (!attachmentKind(f)) {
        toast.error(`不支持的文件类型：${f.name}（支持 .md/.txt 剧本与图片/音频/视频）`);
        return;
      }
    }
    setUploading(true);
    try {
      const docParts: string[] = [];
      const mediaParts: string[] = [];
      for (const f of list) {
        const m = await uploadMaterial(f);
        if (m.kind === "doc") {
          if (m.text_truncated) toast.error(`剧本「${m.filename}」超长被截断：分镜会缺尾，请拆分成多个文件分段发送`);
          docParts.push(m.text ? `剧本文档 ${m.filename} 全文：\n${m.text}` : `剧本文档 ${m.filename}：${m.url}（用 read_doc 读取全文）`);
        } else {
          mediaParts.push(`- ${m.kind === "image" ? "参考图" : m.kind === "audio" ? "配音" : "参考视频"}：${m.url}`);
        }
      }
      const goal = docParts.length
        ? [...docParts, ...mediaParts].join("\n\n")
        : `请按以下素材处理：\n\n${mediaParts.join("\n")}`;
      await send(goal);
    } catch (err) {
      toast.error(`上传失败：${(err as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div
      className={cn(
        "relative flex h-full w-[320px] shrink-0 flex-col border-r border-border bg-card/30",
        dragOver && "bg-primary/5 ring-2 ring-inset ring-primary/50",
      )}
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        e.preventDefault();
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragOver(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer.files.length) void addFiles(e.dataTransfer.files);
      }}
    >
      <div className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold">
        <Bot className="size-4 text-primary" />
        Agent 控制台
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setMessages([])}
          disabled={messages.length === 0 || running}
          title="清除当前对话（只清空界面显示，不影响项目与流水线状态）"
          className="cursor-pointer rounded-md border border-border bg-card p-1.5 text-muted-foreground hover:bg-secondary/60 hover:text-foreground disabled:opacity-40"
        >
          <Eraser className="size-3.5" />
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {messages.length === 0 && (
          <div className="rounded-xl bg-secondary/40 p-3 text-[11px] leading-relaxed text-muted-foreground">
            直接对话即可：粘贴或拖入 .md/.txt 剧本会自动进流水线；说「继续生成」推进流水线；也可以让我改某一镜的 Prompt/时长/素材。
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={cn("space-y-1", m.role === "user" ? "text-right" : "text-left")}>
            <MsgBubble m={m} />
            {m.steps && m.steps.length > 0 && (
              <div className="space-y-0.5 text-left">
                {m.steps.map((s, j) => (
                  <div key={j} className="flex items-center gap-1 text-[10px] text-muted-foreground">
                    <Wrench className="size-2.5" />
                    {s.tool ?? s.note}
                    {s.tool && (
                      <span className={s.ok === false ? "text-red-400" : "text-emerald-400"}>
                        {s.ok === false ? "✕" : "✓"}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {running && (
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <Loader2 className="size-3 animate-spin" /> 执行中…
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <div className="border-t border-border p-2.5">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          accept=".md,.txt,image/*,audio/*,video/*"
          onChange={(e) => {
            if (e.target.files?.length) void addFiles(e.target.files);
            e.currentTarget.value = "";
          }}
        />
        {dragOver && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-primary/10 text-xs font-semibold text-primary">
            松手投放剧本 / 素材
          </div>
        )}
        <div className="mb-2 flex gap-1.5">
          <button
            type="button"
            onClick={() => void send("继续生成")}
            disabled={running}
            className="cursor-pointer rounded-md border border-border bg-card px-2.5 py-1 text-[10px] hover:bg-secondary/60 disabled:opacity-50"
          >
            继续生成
          </button>
          <button
            type="button"
            onClick={() => void send("导出成片")}
            disabled={running}
            className="cursor-pointer rounded-md border border-border bg-card px-2.5 py-1 text-[10px] hover:bg-secondary/60 disabled:opacity-50"
          >
            导出成片
          </button>
        </div>
        <div className="flex gap-1.5">
          <div className="flex flex-1 items-start gap-1.5 rounded-lg border border-border bg-background/60 px-2.5">
            <User className="mt-2.5 size-3.5 shrink-0 text-muted-foreground" />
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                const el = e.target;
                el.style.height = "auto";
                el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
              }}
              onPaste={(e) => {
                // 资源管理器里复制的文件（非文本片段）走上传，纯文本粘贴不受影响
                if (e.clipboardData?.files?.length) {
                  e.preventDefault();
                  void addFiles(e.clipboardData.files);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={1}
              placeholder="输入指令或粘贴/拖入剧本…（Shift+Enter 换行）"
              className="max-h-28 min-h-9 w-full resize-none bg-transparent py-2 text-xs outline-none"
            />
          </div>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={running || uploading}
            title="选择剧本（.md/.txt）或参考图/配音/视频上传——剧本全文随消息直接提交"
            className="cursor-pointer rounded-lg border border-border bg-card px-2 text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground disabled:opacity-50"
          >
            {uploading ? <Loader2 className="size-3.5 animate-spin" /> : <Paperclip className="size-3.5" />}
          </button>
          <button
            type="button"
            onClick={() => void send()}
            disabled={running || !input.trim()}
            className="accent-gradient cursor-pointer rounded-lg px-3 text-white disabled:opacity-50"
          >
            <Send className="size-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
