"use client";

/** Agent 控制台：对话 + 步骤流展示（SSE）。创作类请求由后端路由直通流水线。 */
import { useEffect, useRef, useState } from "react";
import { Bot, Loader2, Send, User, Wrench } from "lucide-react";
import { runAgentV2Stream } from "@/lib/api-v2";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "assistant";
  text: string;
  steps?: { tool?: string; ok?: boolean; note?: string }[];
}

export function AgentConsole() {
  const project = useStudioV2Store((s) => s.project);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, running]);

  const send = async (text?: string) => {
    const goal = (text ?? input).trim();
    if (!goal || running || !project) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: goal }]);
    setRunning(true);
    const steps: Msg["steps"] = [];
    let reply = "";
    try {
      await runAgentV2Stream({ goal, project_id: project.id }, (e) => {
        if (e.type === "step" && e.tool) {
          steps.push({ tool: e.tool, ok: e.ok });
          setMessages((m) => [...m]);
        }
        if (e.type === "done") {
          reply = (e.result as { reply?: string } | undefined)?.reply ?? e.reply ?? "（完成）";
        }
      });
      setMessages((m) => [...m, { role: "assistant", text: reply || "（完成）", steps: [...steps] }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", text: `执行失败：${(err as Error).message}` }]);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex h-full w-[320px] shrink-0 flex-col border-r border-border bg-card/30">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold">
        <Bot className="size-4 text-primary" />
        Agent 控制台
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {messages.length === 0 && (
          <div className="rounded-xl bg-secondary/40 p-3 text-[11px] leading-relaxed text-muted-foreground">
            直接对话即可：粘贴剧本会自动进流水线；说「继续生成」推进流水线；也可以让我改某一镜的 Prompt/时长/素材。
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={cn("space-y-1", m.role === "user" ? "text-right" : "text-left")}>
            <div
              className={cn(
                "inline-block max-w-[92%] rounded-xl px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap text-left",
                m.role === "user" ? "bg-primary/15 text-primary-foreground" : "bg-secondary/50",
              )}
            >
              {m.text}
            </div>
            {m.steps && m.steps.length > 0 && (
              <div className="space-y-0.5 text-left">
                {m.steps.map((s, j) => (
                  <div key={j} className="flex items-center gap-1 text-[10px] text-muted-foreground">
                    <Wrench className="size-2.5" />
                    {s.tool}
                    <span className={s.ok === false ? "text-red-400" : "text-emerald-400"}>{s.ok === false ? "✕" : "✓"}</span>
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
          <div className="flex flex-1 items-center gap-1.5 rounded-lg border border-border bg-background/60 px-2.5">
            <User className="size-3.5 shrink-0 text-muted-foreground" />
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && void send()}
              placeholder="输入指令或粘贴剧本…"
              className="h-9 min-w-0 flex-1 bg-transparent text-xs outline-none"
            />
          </div>
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
