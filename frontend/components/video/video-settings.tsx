"use client";

import { ArrowLeftRight, AudioLines, Image as ImageIcon, Layers, Loader2, Settings2, Type, Wand2, X } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { Segmented } from "@/components/common/segmented";
import { ModelSelector } from "@/components/common/model-selector";
import { FileDropzone } from "@/components/common/file-dropzone";
import { GenerationButton } from "@/components/common/generation-button";
import { useAppStore } from "@/stores/app-store";
import type { VideoModelInfo } from "@/lib/types";

export interface VideoSettingsProps {
  modelInfo: VideoModelInfo | null;
  modelId: string;
  onModelChange: (id: string) => void;
  mode: string;
  onModeChange: (mode: string) => void;
  prompt: string;
  onPromptChange: (p: string) => void;
  onImprovePrompt: () => void;
  improving: boolean;
  startImages: string[];
  onAddStartImages: (urls: string[]) => void;
  onRemoveStartImage: (url: string) => void;
  onSetFirstFrame: (url: string) => void;
  onClearFirstFrame: () => void;
  lastImages: string[];
  onSetLastFrame: (url: string) => void;
  onRemoveLastFrame: () => void;
  refImages: string[];
  onAddRefImages: (urls: string[]) => void;
  onRemoveRefImage: (url: string) => void;
  refVideos: string[];
  onAddRefVideos: (urls: string[]) => void;
  onRemoveRefVideo: (url: string) => void;
  refAudios: string[];
  onAddRefAudios: (urls: string[]) => void;
  onRemoveRefAudio: (url: string) => void;
  duration: number;
  onDurationChange: (d: number) => void;
  aspect: string;
  onAspectChange: (a: string) => void;
  resolution: string;
  onResolutionChange: (r: string) => void;
  resolutionCustom: string;
  onResolutionCustomChange: (r: string) => void;
  promptOptimizer: boolean;
  onPromptOptimizerChange: (b: boolean) => void;
  onGenerate: () => void;
  generating: boolean;
}

function Thumb({ url, onRemove, index }: { url: string; onRemove: () => void; index?: number }) {
  const isVideo = /\.(mp4|mov|webm)$/i.test(url);
  return (
    <div className="group relative size-16 overflow-hidden rounded-lg border border-border bg-black/40">
      {isVideo ? (
        <video src={fileUrl(url)} muted preload="metadata" className="size-full object-cover" />
      ) : (
        /* eslint-disable-next-line @next/next/no-img-element */
        <img src={fileUrl(url)} alt="reference" className="size-full object-cover" />
      )}
      {index !== undefined && (
        <span className="accent-gradient absolute left-0.5 top-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-bold text-white">
          {index}
        </span>
      )}
      <button
        type="button"
        onClick={onRemove}
        className="absolute right-0.5 top-0.5 hidden rounded-full bg-black/70 p-0.5 text-white group-hover:block cursor-pointer"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

export function VideoSettings(props: VideoSettingsProps) {
  const catalog = useAppStore((s) => s.catalog);
  const providerLabels = Object.fromEntries((catalog?.providers ?? []).map((p) => [p.id, p.label]));

  const upload = async (files: File[], kind: "image" | "video" | "audio", apply: (url: string) => void) => {
    for (const file of files) {
      try {
        const res = await api.upload(file, kind);
        apply(res.url);
      } catch (err) {
        toast.error(`上传失败：${(err as Error).message}`);
      }
    }
  };

  return (
    <div className="flex h-full w-[340px] shrink-0 flex-col border-r border-border bg-card/40">
      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-5">
        <h2 className="text-sm font-semibold">Generate Video</h2>

        {/* Model */}
        <div className="space-y-2">
          <Label>Model</Label>
          <ModelSelector
            models={catalog?.video ?? []}
            value={props.modelId}
            onChange={props.onModelChange}
            showProvider={false}
          />
        </div>

        {/* Mode：只展示当前模型支持的模式 */}
        <div className="space-y-2">
          <Label>Mode</Label>
          <Segmented
            options={[
              { value: "text", label: "Text", icon: <Type className="size-3.5" /> },
              { value: "image", label: "Image", icon: <ImageIcon className="size-3.5" /> },
              { value: "reference", label: "Reference", icon: <Layers className="size-3.5" /> },
              { value: "first_last", label: "首尾帧", icon: <ArrowLeftRight className="size-3.5" /> },
            ].filter((o) => !props.modelInfo?.modes?.length || props.modelInfo.modes.includes(o.value))}
            value={props.mode}
            onChange={props.onModeChange}
          />
        </div>

        {/* Prompt + AI 改写 */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>Prompt</Label>
            <button
              type="button"
              onClick={props.onImprovePrompt}
              disabled={props.improving || !props.prompt.trim()}
              className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary transition-colors hover:bg-primary/20 disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
              title="Improve Prompt"
            >
              {props.improving ? <Loader2 className="size-3 animate-spin" /> : <Wand2 className="size-3" />}
              AI
            </button>
          </div>
          <Textarea
            value={props.prompt}
            onChange={(e) => props.onPromptChange(e.target.value)}
            placeholder="描述你想要的视频画面、动作与镜头……"
            className="min-h-[120px] resize-none"
          />
        </div>

        {/* 按模式变化的素材区（图片均为参考图，仅首尾帧模式存在"首帧"语义） */}
        {props.mode === "image" && (
          <div className="space-y-2">
            <Label>
              Images（可多张
              {props.modelInfo?.min_images ? `，至少 ${props.modelInfo.min_images} 张` : ""}
              {props.modelInfo?.max_images ? `，最多 ${props.modelInfo.max_images} 张` : ""}
              ）
            </Label>
            {props.startImages.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {props.startImages.map((u, i) => (
                  <Thumb key={u} url={u} index={i + 1} onRemove={() => props.onRemoveStartImage(u)} />
                ))}
              </div>
            )}
            <FileDropzone
              accept="image/*"
              multiple
              hint="Drop start image here or"
              onFiles={(files) => void upload(files, "image", (url) => props.onAddStartImages([url]))}
            />
          </div>
        )}

        {/* 首尾帧模式 */}
        {props.mode === "first_last" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>First Frame（首帧）</Label>
              {props.startImages[0] && (
                <Thumb url={props.startImages[0]} onRemove={props.onClearFirstFrame} />
              )}
              <FileDropzone
                accept="image/*"
                hint="Drop first frame here or"
                onFiles={(files) => void upload(files.slice(0, 1), "image", props.onSetFirstFrame)}
              />
            </div>
            <div className="space-y-2">
              <Label>Last Frame（尾帧）</Label>
              {props.lastImages[0] && (
                <Thumb url={props.lastImages[0]} onRemove={props.onRemoveLastFrame} />
              )}
              <FileDropzone
                accept="image/*"
                hint="Drop last frame here or"
                onFiles={(files) => void upload(files.slice(0, 1), "image", props.onSetLastFrame)}
              />
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              模型将从首帧过渡生成到尾帧，建议两张图主体一致、构图相近。
            </p>
          </div>
        )}

        {props.mode === "reference" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>
                Images（可多张
                {props.modelInfo?.min_images ? `，至少 ${props.modelInfo.min_images} 张` : ""}
                {props.modelInfo?.max_images ? `，最多 ${props.modelInfo.max_images} 张` : ""}
                ）
              </Label>
              {props.refImages.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {props.refImages.map((u, i) => (
                    <Thumb key={u} url={u} index={i + 1} onRemove={() => props.onRemoveRefImage(u)} />
                  ))}
                </div>
              )}
              <FileDropzone
                accept="image/*"
                multiple
                hint="Drop reference images or"
                onFiles={(files) => void upload(files, "image", (url) => props.onAddRefImages([url]))}
              />
            </div>
            {props.modelInfo?.accepts_videos !== false && (
              <div className="space-y-2">
                <Label>Videos（可多个）</Label>
                {props.refVideos.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {props.refVideos.map((u, i) => (
                      <Thumb key={u} url={u} index={i + 1} onRemove={() => props.onRemoveRefVideo(u)} />
                    ))}
                  </div>
                )}
                <FileDropzone
                  accept="video/*"
                  multiple
                  hint="Drop reference videos or"
                  onFiles={(files) => void upload(files, "video", (url) => props.onAddRefVideos([url]))}
                />
              </div>
            )}
            {props.modelInfo?.accepts_audios !== false && (
              <div className="space-y-2">
                <Label>
                  Audio（可多个
                  {props.modelInfo?.max_audios ? `，最多 ${props.modelInfo.max_audios} 段` : ""}
                  ）
                </Label>
              <div className="space-y-1.5">
                {props.refAudios.map((u, i) => (
                  <div
                    key={u}
                    className="group flex items-center justify-between rounded-lg border border-border bg-secondary/40 px-3 py-2 text-xs"
                  >
                    <span className="flex items-center gap-2 truncate text-foreground/80">
                      <span className="accent-gradient flex size-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold text-white">
                        {i + 1}
                      </span>
                      <AudioLines className="size-3.5 shrink-0 text-emerald-300" />
                      已上传音频
                    </span>
                    <button
                      type="button"
                      onClick={() => props.onRemoveRefAudio(u)}
                      className="text-muted-foreground hover:text-red-400 cursor-pointer"
                    >
                      <X className="size-3.5" />
                    </button>
                  </div>
                ))}
              </div>
              <FileDropzone
                accept="audio/*"
                multiple
                hint="Drop reference audio or"
                onFiles={(files) => void upload(files, "audio", (url) => props.onAddRefAudios([url]))}
              />
              </div>
            )}
          </div>
        )}

        {/* 核心参数 */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>
              Duration（
              {props.modelInfo?.durations.length ? Math.min(...props.modelInfo.durations) : 1}-
              {props.modelInfo?.durations.length ? Math.max(...props.modelInfo.durations) : 15}s）
            </Label>
            <span className="rounded-md bg-secondary px-2 py-0.5 text-xs font-medium text-foreground">
              {props.duration}s
            </span>
          </div>
          <Slider
            value={[props.duration]}
            onValueChange={(v) => props.onDurationChange(v[0])}
            min={props.modelInfo?.durations.length ? Math.min(...props.modelInfo.durations) : 1}
            max={props.modelInfo?.durations.length ? Math.max(...props.modelInfo.durations) : 15}
            step={1}
          />
        </div>

        <div className="space-y-2">
          <Label>Aspect Ratio</Label>
          <Segmented
            options={(props.modelInfo?.aspect_ratios ?? ["16:9"]).map((a) => ({ value: a, label: a }))}
            value={props.aspect}
            onChange={props.onAspectChange}
          />
        </div>

        <div className="space-y-2">
          <Label>Resolution</Label>
          <Segmented
            options={(props.modelInfo?.resolutions ?? ["auto"]).map((r) => ({
              value: r,
              label: r === "auto" ? "Auto" : r,
            }))}
            value={props.resolution}
            onChange={props.onResolutionChange}
          />
        </div>

        {/* Advanced */}
        <details className="group rounded-lg border border-border bg-secondary/30">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-xs text-muted-foreground [&::-webkit-details-marker]:hidden">
            <Settings2 className="size-3.5" />
            Advanced Settings
            <span className="ml-auto transition-transform group-open:rotate-90">›</span>
          </summary>
          <div className="space-y-3 border-t border-border px-3 py-3">
            <label className="flex cursor-pointer items-center justify-between text-xs">
              <span className="text-muted-foreground">Prompt Optimizer（模型自动优化提示词）</span>
              <input
                type="checkbox"
                checked={props.promptOptimizer}
                onChange={(e) => props.onPromptOptimizerChange(e.target.checked)}
                className="size-4 accent-[#7c5cff] cursor-pointer"
              />
            </label>
            <div className="space-y-1.5">
              <Label>自定义分辨率（可选，覆盖上方选择）</Label>
              <input
                value={props.resolutionCustom}
                onChange={(e) => props.onResolutionCustomChange(e.target.value)}
                placeholder="原样传给厂商，如 768p / 4K / 2560x1440；不支持时厂商会返回错误"
                className="h-8 w-full rounded-md border border-border bg-secondary/50 px-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
          </div>
        </details>
      </div>

      {/* 底部固定 Generate */}
      <div className="shrink-0 border-t border-border p-4">
        <GenerationButton
          loading={props.generating}
          loadingLabel="Generating..."
          onClick={props.onGenerate}
          disabled={!props.modelId}
        >
          Generate Video
        </GenerationButton>
      </div>
    </div>
  );
}
