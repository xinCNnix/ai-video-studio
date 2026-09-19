"use client";

import { useState } from "react";
import { ImagePlus, Loader2, Wand2, X } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/common/segmented";
import { ModelSelector } from "@/components/common/model-selector";
import { FileDropzone } from "@/components/common/file-dropzone";
import { GenerationButton } from "@/components/common/generation-button";
import { useAppStore } from "@/stores/app-store";

export interface ImageGeneratorProps {
  modelId: string;
  onModelChange: (id: string) => void;
  prompt: string;
  onPromptChange: (p: string) => void;
  references: string[];
  onAddReferences: (urls: string[]) => void;
  onRemoveReference: (url: string) => void;
  aspect: string;
  onAspectChange: (a: string) => void;
  sizeCustom: string;
  onSizeCustomChange: (v: string) => void;
  n: number;
  onNChange: (n: number) => void;
  onGenerate: () => void;
  generating: boolean;
}

export function ImageGenerator(props: ImageGeneratorProps) {
  const catalog = useAppStore((s) => s.catalog);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [polishing, setPolishing] = useState(false);

  // AI 润色：按 image-prompt-writing 技能把描述改写成结构化生图提示词
  const polish = async () => {
    setPolishing(true);
    try {
      const { prompt } = await api.improveImagePrompt({ prompt: props.prompt, image_model: props.modelId });
      props.onPromptChange(prompt);
      toast.success("提示词已润色");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPolishing(false);
    }
  };

  const upload = async (files: File[]) => {
    const urls: string[] = [];
    for (const file of files) {
      try {
        const res = await api.upload(file, "image");
        urls.push(res.url);
      } catch (err) {
        toast.error(`上传失败：${(err as Error).message}`);
      }
    }
    if (urls.length) props.onAddReferences(urls);
  };

  return (
    <div className="flex h-full w-[360px] shrink-0 flex-col border-r border-border bg-card/40">
      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-5">
        <h2 className="text-sm font-semibold">Image Generation</h2>

        <div className="space-y-2">
          <Label>Model</Label>
          <ModelSelector
            models={catalog?.image ?? []}
            value={props.modelId}
            onChange={props.onModelChange}
            showProvider={false}
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>Prompt</Label>
            <button
              type="button"
              disabled={polishing || !props.prompt.trim()}
              onClick={() => void polish()}
              className="flex items-center gap-1 rounded-md border border-border bg-secondary/40 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50 cursor-pointer"
            >
              {polishing ? <Loader2 className="size-3 animate-spin" /> : <Wand2 className="size-3" />}
              AI 润色
            </button>
          </div>
          <Textarea
            value={props.prompt}
            onChange={(e) => props.onPromptChange(e.target.value)}
            placeholder="Describe your image"
            className="min-h-[130px] resize-none"
          />
        </div>

        <div className="space-y-2">
          <Label>Reference Images</Label>
          {props.references.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {props.references.map((url, i) => (
                <div key={url} className="group relative size-16 overflow-hidden rounded-lg border border-border">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={fileUrl(url)} alt="reference" className="size-full object-cover" />
                  <span className="accent-gradient absolute left-0.5 top-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-bold text-white">
                    {i + 1}
                  </span>
                  <button
                    type="button"
                    onClick={() => props.onRemoveReference(url)}
                    className="absolute right-0.5 top-0.5 hidden rounded-full bg-black/70 p-0.5 text-white group-hover:block cursor-pointer"
                  >
                    <X className="size-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <FileDropzone
            accept="image/*"
            multiple
            hint="Drop image here or"
            onFiles={(files) => void upload(files)}
          />
        </div>

        <div className="space-y-2">
          <Label>Aspect Ratio</Label>
          <Segmented
            options={["1:1", "3:4", "4:3", "9:16", "16:9"].map((a) => ({ value: a, label: a }))}
            value={props.aspect}
            onChange={props.onAspectChange}
          />
        </div>

        {/* Advanced 默认收起 */}
        <button
          type="button"
          onClick={() => setAdvancedOpen(!advancedOpen)}
          className="flex w-full items-center gap-2 rounded-lg border border-border bg-secondary/30 px-3 py-2.5 text-xs text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
        >
          Advanced Settings
          <span className="ml-auto transition-transform" style={{ transform: advancedOpen ? "rotate(90deg)" : "none" }}>
            ›
          </span>
        </button>
        {advancedOpen && (
          <div className="space-y-3 rounded-lg border border-border bg-secondary/30 px-3 py-3">
            <div className="space-y-1.5">
              <Label>自定义分辨率（宽x高，可选，覆盖比例选择）</Label>
              <input
                value={props.sizeCustom}
                onChange={(e) => props.onSizeCustomChange(e.target.value)}
                placeholder="原样传给厂商，如 1024x1536 / 2048x1152"
                className="h-8 w-full rounded-md border border-border bg-secondary/50 px-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <div className="space-y-1.5">
              <Label>Number of images（1-4）</Label>
              <input
                type="number"
                min={1}
                max={4}
                value={props.n}
                onChange={(e) => props.onNChange(Math.max(1, Math.min(4, Number(e.target.value) || 1)))}
                className="h-8 w-full rounded-md border border-border bg-secondary/50 px-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-border p-4">
        <GenerationButton
          loading={props.generating}
          loadingLabel="Generating..."
          onClick={props.onGenerate}
        >
          <ImagePlus className="size-4" />
          Generate
        </GenerationButton>
      </div>
    </div>
  );
}
