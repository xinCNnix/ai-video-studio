"use client";

/** 分镜看板：卡片墙 + 上/下移排序 + 点击打开单镜抽屉。 */
import { ChevronDown, ChevronUp, Clapperboard, Film, Image as ImageIcon, Mic, Plus } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { SCENE_STATUS_LABELS } from "@/lib/types-v2";
import type { SceneV2 } from "@/lib/types-v2";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { cn } from "@/lib/utils";

function statusColor(status: SceneV2["status"], error?: string | null) {
  if (status === "failed" || error) return "bg-red-500/15 text-red-300";
  if (status === "done") return "bg-emerald-500/15 text-emerald-300";
  if (status === "generating") return "bg-primary/20 text-primary";
  if (status === "ready") return "bg-sky-500/15 text-sky-300";
  return "bg-secondary/60 text-muted-foreground";
}

export function ShotBoard() {
  const project = useStudioV2Store((s) => s.project);
  const setCurrentScene = useStudioV2Store((s) => s.setCurrentScene);
  const addScene = useStudioV2Store((s) => s.addScene);
  const moveScene = useStudioV2Store((s) => s.moveScene);
  if (!project) return null;
  const scenes = project.scenes;

  if (scenes.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-border text-muted-foreground">
        <Clapperboard className="size-8 opacity-50" />
        <div className="text-sm">还没有分镜 —— 在上方投放剧本，流水线会自动拆分并分配素材</div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(210px,1fr))] gap-3">
        {scenes.map((s, i) => {
          const thumb = s.assets.video || s.assets.keyframe;
          const boundImg = project.materials.find((m) => m.id === s.binding.images[0]?.material_id);
          return (
            <div
              key={s.id}
              className="group relative cursor-pointer overflow-hidden rounded-xl border border-border bg-card/60 transition-colors hover:border-primary/50"
              onClick={() => setCurrentScene(s.id)}
            >
              <div className="relative aspect-video bg-[#0b0c10]">
                {thumb ? (
                  s.assets.video ? (
                    <video src={fileUrl(s.assets.video)} className="size-full object-cover" muted preload="metadata" />
                  ) : (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={fileUrl(thumb)} alt={s.title} className="size-full object-cover" />
                  )
                ) : boundImg?.url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={fileUrl(boundImg.url)} alt={s.title} className="size-full object-cover opacity-70" />
                ) : (
                  <div className="flex size-full items-center justify-center text-muted-foreground/40">
                    <Film className="size-6" />
                  </div>
                )}
                <span className="absolute left-1.5 top-1.5 rounded-md bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                  {i + 1}
                </span>
                <span className={cn("absolute right-1.5 top-1.5 rounded-md px-1.5 py-0.5 text-[10px]", statusColor(s.status, s.error))}>
                  {s.error ? "失败" : SCENE_STATUS_LABELS[s.status]}
                </span>
                <div className="absolute bottom-1.5 right-1.5 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    type="button"
                    className="cursor-pointer rounded-md bg-black/60 p-1 text-white hover:bg-black/80"
                    onClick={(e) => {
                      e.stopPropagation();
                      void moveScene(s.id, -1);
                    }}
                  >
                    <ChevronUp className="size-3" />
                  </button>
                  <button
                    type="button"
                    className="cursor-pointer rounded-md bg-black/60 p-1 text-white hover:bg-black/80"
                    onClick={(e) => {
                      e.stopPropagation();
                      void moveScene(s.id, 1);
                    }}
                  >
                    <ChevronDown className="size-3" />
                  </button>
                </div>
              </div>
              <div className="p-2">
                <div className="truncate text-xs font-medium">{s.title}</div>
                <div className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                  <span>{s.duration}s</span>
                  <span className="rounded bg-secondary/60 px-1">{s.gen_mode === "i2v" ? "图生" : "文生"}</span>
                  {s.binding.images.length > 0 && (
                    <span className="inline-flex items-center gap-0.5">
                      <ImageIcon className="size-2.5" />
                      {s.binding.images.length}
                    </span>
                  )}
                  {s.binding.audios.length > 0 && (
                    <span className="inline-flex items-center gap-0.5">
                      <Mic className="size-2.5" />
                      {s.binding.audios.length}
                    </span>
                  )}
                  {s.assets.voice && <span title="已有配音">🔊</span>}
                </div>
                {s.narration && <div className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-muted-foreground/80">{s.narration}</div>}
                {s.error && <div className="mt-1 line-clamp-2 text-[10px] text-red-300">{s.error}</div>}
              </div>
            </div>
          );
        })}
        <button
          type="button"
          onClick={() => void addScene()}
          className="flex aspect-[16/10] cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-border text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary"
        >
          <Plus className="size-5" />
          <span className="text-[11px]">手动加镜</span>
        </button>
      </div>
    </div>
  );
}
