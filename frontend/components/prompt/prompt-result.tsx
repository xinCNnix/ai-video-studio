"use client";

import { useEffect, useState } from "react";
import { Check, Clapperboard, Copy, FileText, Loader2, Pencil, RefreshCw, Save, Send, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { PromptSection } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/common/empty-state";
import { SearchableSelect } from "@/components/common/searchable-select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

export interface PromptResultProps {
  sections: PromptSection[];
  raw: string;
  loading: boolean;
  refining: string | null;
  onRefine: (action: string) => void;
  onRegenerate: () => void;
  onSendToVideo: () => void;
  onSendToStudio: () => void;
  onApplyRaw: (text: string) => void;
}

const REFINEMENTS = [
  { action: "shorter", label: "Shorter" },
  { action: "detailed", label: "More Detailed" },
  { action: "motion", label: "Improve Motion" },
  { action: "camera", label: "Improve Camera" },
  { action: "timing", label: "Improve Timing" },
];

export function PromptResult(props: PromptResultProps) {
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveTitle, setSaveTitle] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [presets, setPresets] = useState<{ id: string; title: string }[]>([]);

  const refreshPresets = () => {
    api.getPresets().then(({ presets }) => setPresets(presets)).catch(() => undefined);
  };
  useEffect(() => refreshPresets(), []);

  const loadPreset = async (id: string) => {
    try {
      const p = await api.getPreset(id);
      props.onApplyRaw(p.content);
      toast.success(`已加载预设「${p.title}」`);
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const startEdit = () => {
    setDraft(props.raw);
    setEditing(true);
  };

  const applyEdit = () => {
    props.onApplyRaw(draft);
    setEditing(false);
    toast.success("已应用修改");
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(props.raw);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选择复制");
    }
  };

  const save = async () => {
    try {
      await api.savePreset(saveTitle || "未命名 Prompt", props.raw);
      setSaveOpen(false);
      setSaveTitle("");
      toast.success("已保存为 Prompt 预设");
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      {/* 头部操作 */}
      <div className="flex items-center justify-between border-b border-border px-6 py-3.5">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold">
            H3 Prompt
            <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-normal text-muted-foreground">
              {props.sections.length} sections
            </span>
          </div>
          {presets.length > 0 && (
            <SearchableSelect
              className="w-44"
              items={presets.map((p) => ({ value: p.id, label: p.title }))}
              value=""
              onChange={(id) => void loadPreset(id)}
              placeholder="加载预设…"
              searchPlaceholder="搜索预设…"
              emptyText="无匹配预设"
            />
          )}
        </div>
        <div className="flex items-center gap-2">
          {!editing && (
            <Button size="sm" variant="ghost" onClick={startEdit} disabled={!props.raw}>
              <Pencil className="size-3.5" />
              编辑
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => void copy()} disabled={!props.raw}>
            <Copy className="size-3.5" />
            Copy
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSaveOpen(true)} disabled={!props.raw}>
            <Save className="size-3.5" />
            Save
          </Button>
          <Button size="sm" variant="accent" onClick={props.onSendToVideo} disabled={!props.raw}>
            <Send className="size-3.5" />
            Send to Video
          </Button>
          <Button size="sm" variant="outline" onClick={props.onSendToStudio} disabled={!props.raw}>
            <Clapperboard className="size-3.5" />
            Send to Studio
          </Button>
        </div>
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto px-6 py-5">
        {props.loading ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
            <Loader2 className="size-6 animate-spin text-primary" />
            <div className="text-xs">AI 正在撰写 Prompt…</div>
          </div>
        ) : editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="h-full min-h-full w-full resize-none rounded-lg border border-border bg-secondary/40 p-4 font-mono text-sm leading-relaxed text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          />
        ) : props.sections.length === 0 ? (
          <EmptyState
            icon={FileText}
            title="Prompt Result"
            hint="输入创意描述并点击 Generate Prompt，这里会展示结构化的专业视频 Prompt"
            className="h-full"
          />
        ) : (
          <div className="mx-auto max-w-2xl space-y-6">
            {props.sections.map((section, i) => (
              <section key={i}>
                <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-widest text-primary/90">
                  {section.title}
                </div>
                <div className="whitespace-pre-wrap border-l-2 border-secondary pl-4 text-sm leading-relaxed text-foreground/90">
                  {section.content}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>

      {/* 底部操作栏：编辑态显示 应用/取消 */}
      {editing ? (
        <div className="flex items-center gap-2 border-t border-border px-6 py-3">
          <Button size="sm" variant="accent" onClick={applyEdit}>
            <Check className="size-3.5" />
            应用修改
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            取消
          </Button>
          <span className="ml-auto text-[11px] text-muted-foreground">可直接修改文本，应用后重新分节</span>
        </div>
      ) : (
        props.sections.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 border-t border-border px-6 py-3">
            {REFINEMENTS.map((r) => (
              <Button
                key={r.action}
                size="sm"
                variant="outline"
                disabled={!!props.refining}
                onClick={() => props.onRefine(r.action)}
              >
                {props.refining === r.action && <Loader2 className="size-3 animate-spin" />}
                {r.label}
              </Button>
            ))}
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              disabled={!!props.refining}
              onClick={props.onRegenerate}
            >
              {props.refining === "regenerate" ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <RefreshCw className="size-3.5" />
              )}
              Regenerate
            </Button>
          </div>
        )
      )}

      {/* 保存对话框 */}
      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>保存 Prompt 预设</DialogTitle>
            <DialogDescription>保存后可以在下次创作时快速复用</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Input
              value={saveTitle}
              onChange={(e) => setSaveTitle(e.target.value)}
              placeholder="预设名称"
              autoFocus
            />
            <Button variant="accent" className="w-full" onClick={() => void save()}>
              <Wand2 className="size-4" />
              保存
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
