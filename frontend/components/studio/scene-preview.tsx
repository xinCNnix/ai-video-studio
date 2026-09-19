"use client";

import { Clapperboard, Download, ExternalLink } from "lucide-react";
import { fileUrl } from "@/lib/api";
import type { Scene } from "@/lib/types";

export function ScenePreview({ scene }: { scene: Scene | null }) {
  if (!scene) return null;

  const media = scene.video || scene.image || null;

  return (
    // relative + overflow-hidden：媒体放在 absolute 盒里，盒高由定位锚定（不依赖
    // flex 百分比链），max-h-full 在任何窗口尺寸下都能可靠解析——视频随浏览器缩放，
    // 播放控制条永远在可视区内
    <div className="relative flex min-h-0 min-w-0 flex-1 items-center justify-center overflow-hidden bg-[#0b0c10] p-6">
      {media && (
        <div className="absolute right-4 top-4 z-10 flex items-center gap-1.5">
          <a
            href={`${fileUrl(media)}?dl=1`}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-card/90 px-2.5 py-1.5 text-[11px] text-foreground/90 shadow backdrop-blur transition-colors hover:bg-card"
            title={scene.video ? "下载该分镜视频" : "下载该分镜图片"}
          >
            <Download className="size-3.5" />
            下载{scene.video ? "视频" : "图片"}
          </a>
          <button
            type="button"
            onClick={() => window.open(fileUrl(media), "_blank")}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-card/90 px-2.5 py-1.5 text-[11px] text-foreground/90 shadow backdrop-blur transition-colors hover:bg-card"
            title="在新标签页打开"
          >
            <ExternalLink className="size-3.5" />
            新窗口
          </button>
        </div>
      )}
      {scene.video ? (
        <div className="absolute inset-6 flex items-center justify-center">
          <video
            key={scene.video}
            src={fileUrl(scene.video)}
            controls
            className="max-h-full max-w-full rounded-xl border border-border bg-black shadow-2xl"
          />
        </div>
      ) : scene.image ? (
        <div className="absolute inset-6 flex items-center justify-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={fileUrl(scene.image)}
            alt={scene.title}
            className="max-h-full max-w-full rounded-xl border border-border object-contain shadow-2xl"
          />
        </div>
      ) : (
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="flex size-14 items-center justify-center rounded-2xl border border-dashed border-border/70 bg-secondary/30">
            <Clapperboard className="size-6 text-muted-foreground" />
          </div>
          <div>
            <div className="text-sm font-medium text-foreground/90">Scene Preview</div>
            <div className="mt-1 text-xs text-muted-foreground">
              在右侧为场景生成图片或视频后，将在此预览
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
