"use client";

/** 投放面板：剧本文本 + 参考素材上传 → 启动流水线（agent-first 的主入口）。 */
import { useRef, useState } from "react";
import { FileText, Image as ImageIcon, Loader2, Mic, Paperclip, Play, X } from "lucide-react";
import { toast } from "sonner";
import { uploadMaterial } from "@/lib/api-v2";
import { useStudioV2Store } from "@/stores/studio-v2-store";

interface PendingMaterial {
  url: string;
  kind: string;
  filename: string;
  text?: string;
}

export function IntakePanel() {
  const startPipeline = useStudioV2Store((s) => s.startPipeline);
  const [script, setScript] = useState("");
  const [materials, setMaterials] = useState<PendingMaterial[]>([]);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const addFiles = async (files: FileList | File[]) => {
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        const m = await uploadMaterial(f);
        if (m.kind === "doc" && m.text) {
          setScript((s) => (s ? `${s}\n\n${m.text}` : (m.text ?? "")));
          toast.success(`已读取剧本「${m.filename}」`);
        } else {
          setMaterials((ms) => (ms.some((x) => x.url === m.url) ? ms : [...ms, { ...m }]));
        }
      }
    } catch (err) {
      toast.error(`上传失败：${(err as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  const submit = async () => {
    if (!script.trim() && materials.length === 0) {
      toast.error("请先粘贴剧本文本或上传素材");
      return;
    }
    setStarting(true);
    try {
      await startPipeline(script.trim(), materials);
      toast.success("流水线已启动：剧本解析 → 分镜规划 → 素材分配，完成后会等你确认再生成");
      setScript("");
      setMaterials([]);
    } catch {
      /* store 已 toast */
    } finally {
      setStarting(false);
    }
  };

  return (
    <div
      className="shrink-0 rounded-2xl border border-dashed border-border bg-card/40 p-5"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (e.dataTransfer.files.length) void addFiles(e.dataTransfer.files);
      }}
    >
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Paperclip className="size-4 text-primary" />
        投放剧本与素材
        <span className="text-xs font-normal text-muted-foreground">
          粘贴任意格式剧本 / 拖入 .md .txt 剧本、参考图、参考音频 —— 拆分镜与素材分配全自动，生成前会等你确认
        </span>
      </div>
      <textarea
        value={script}
        onChange={(e) => setScript(e.target.value)}
        placeholder="在此粘贴剧本 / 故事 / 已有分镜稿 / 广告口播……（也可以拖入文件）"
        className="mt-3 h-32 w-full resize-none rounded-xl border border-border bg-background/60 p-3 text-sm leading-relaxed outline-none focus:border-primary/60"
      />
      {materials.length > 0 && (
        <div className="mt-2">
          <div className="mb-1 text-[10px] text-muted-foreground">已选素材 {materials.length} 项（区域可滚动）</div>
          <div className="flex max-h-36 flex-wrap gap-2 overflow-y-auto pr-1">
            {materials.map((m) => (
              <span
                key={m.url}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2 py-1 text-[11px]"
              >
                {m.kind === "image" ? <ImageIcon className="size-3" /> : m.kind === "audio" ? <Mic className="size-3" /> : <FileText className="size-3" />}
                {m.filename}
                <button type="button" className="cursor-pointer text-muted-foreground hover:text-foreground" onClick={() => setMaterials((ms) => ms.filter((x) => x.url !== m.url))}>
                  <X className="size-3" />
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="cursor-pointer rounded-lg border border-border bg-card px-3 py-1.5 text-xs transition-colors hover:bg-secondary/60"
        >
          {uploading ? <Loader2 className="size-3.5 animate-spin" /> : "选择文件"}
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept=".md,.txt,image/*,audio/*,video/*"
          className="hidden"
          onChange={(e) => e.target.files && void addFiles(e.target.files)}
        />
        <div className="flex-1" />
        <button
          type="button"
          onClick={submit}
          disabled={starting || uploading}
          className="accent-gradient inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-60"
        >
          {starting ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          启动流水线
        </button>
      </div>
    </div>
  );
}
