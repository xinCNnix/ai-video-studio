"use client";

import { Download, ImagePlus, Loader2, Play, Star, Trash2 } from "lucide-react";
import { fileUrl } from "@/lib/api";
import type { Job } from "@/lib/types";

export interface ImageTile {
  url: string;
  jobId: string;
  /** 生成该图实际使用的凭据（如 “按量付费 · Ocy8”），来自任务记录 */
  credential?: string;
}

export function ImageGrid({
  tiles,
  pending,
  onUseAsReference,
  onSendToVideo,
  onOpenImage,
  onDeleteJob,
}: {
  tiles: ImageTile[];
  pending: boolean;
  onUseAsReference: (url: string) => void;
  onSendToVideo: (url: string) => void;
  onOpenImage: (url: string) => void;
  onDeleteJob: (jobId: string) => void;
}) {
  return (
    <div className="flex-1 overflow-y-auto p-6">
      {tiles.length === 0 && !pending ? (
        <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
          <div className="flex size-16 items-center justify-center rounded-2xl border border-dashed border-border/70 bg-secondary/30">
            <ImagePlus className="size-7 text-muted-foreground" />
          </div>
          <div>
            <div className="text-sm font-medium text-foreground/90">Your images will appear here.</div>
            <div className="mt-1 text-xs text-muted-foreground">在左侧输入描述并点击 Generate</div>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
          {pending && (
            <div className="flex aspect-square animate-pulse flex-col items-center justify-center gap-2 rounded-xl border border-border bg-secondary/30">
              <Loader2 className="size-6 animate-spin text-primary" />
              <span className="text-xs text-muted-foreground">Generating…</span>
            </div>
          )}
          {tiles.map((tile) => (
            <div
              key={`${tile.jobId}-${tile.url}`}
              className="group relative cursor-zoom-in overflow-hidden rounded-xl border border-border bg-secondary/30"
              onClick={() => onOpenImage(tile.url)}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={fileUrl(tile.url)} alt="generated" className="aspect-square w-full object-cover" />
              {tile.credential && (
                <div
                  title={`本次使用的 Key：${tile.credential}`}
                  className="pointer-events-none absolute left-2 top-2 rounded-md bg-black/55 px-1.5 py-0.5 text-[10px] text-emerald-300/90 opacity-0 backdrop-blur transition-opacity group-hover:opacity-100"
                >
                  🔑{tile.credential}
                </div>
              )}
              {/* 遮罩本身不拦截点击（点击空白处仍打开大图），仅按钮区拦截 */}
              <div className="pointer-events-none absolute inset-0 flex items-end justify-center bg-gradient-to-t from-black/80 via-black/20 to-transparent opacity-0 transition-opacity group-hover:opacity-100">
                <div className="pointer-events-auto mb-3 flex gap-2">
                  <a
                    href={`${fileUrl(tile.url)}?dl=1`}
                    title="Download"
                    onClick={(e) => e.stopPropagation()}
                    className="flex size-8 items-center justify-center rounded-lg bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/20"
                  >
                    <Download className="size-4" />
                  </a>
                  <button
                    type="button"
                    title="Use as Reference"
                    onClick={() => onUseAsReference(tile.url)}
                    className="flex size-8 cursor-pointer items-center justify-center rounded-lg bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/20"
                  >
                    <Star className="size-4" />
                  </button>
                  <button
                    type="button"
                    title="Send to Video"
                    onClick={() => onSendToVideo(tile.url)}
                    className="flex size-8 cursor-pointer items-center justify-center rounded-lg bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/20"
                  >
                    <Play className="size-4" />
                  </button>
                  <button
                    type="button"
                    title="删除该任务的全部图片"
                    onClick={() => onDeleteJob(tile.jobId)}
                    className="flex size-8 cursor-pointer items-center justify-center rounded-lg bg-white/10 text-white backdrop-blur transition-colors hover:bg-red-500/40"
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
