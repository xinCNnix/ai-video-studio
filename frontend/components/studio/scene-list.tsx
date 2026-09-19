"use client";

import { useState } from "react";
import { Clapperboard, Copy, Plus, Trash2 } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { useStudioStore } from "@/stores/studio-store";
import { cn } from "@/lib/utils";

export function SceneList() {
  const project = useStudioStore((s) => s.project);
  const currentSceneId = useStudioStore((s) => s.currentSceneId);
  const setCurrent = useStudioStore((s) => s.setCurrent);
  const addScene = useStudioStore((s) => s.addScene);
  const duplicateScene = useStudioStore((s) => s.duplicateScene);
  const deleteScene = useStudioStore((s) => s.deleteScene);
  const reorderScene = useStudioStore((s) => s.reorderScene);

  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const scenes = project?.scenes ?? [];
  const characters = project?.characters ?? [];

  return (
    <div className="flex h-full w-[220px] shrink-0 flex-col border-r border-border bg-card/40">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-sm font-semibold">Scenes</span>
        <button
          type="button"
          onClick={addScene}
          className="accent-gradient flex size-6 cursor-pointer items-center justify-center rounded-md text-white"
          title="新增场景"
        >
          <Plus className="size-3.5" />
        </button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {/* 角色设定卡（跨镜一致性锚点） */}
        {characters.length > 0 && (
          <details className="rounded-xl border border-primary/30 bg-primary/[0.05] px-2.5 py-2">
            <summary className="cursor-pointer list-none text-[11px] font-medium text-primary [&::-webkit-details-marker]:hidden">
              🎭 角色设定卡（{characters.length}）
            </summary>
            <div className="mt-2 space-y-1.5">
              {characters.map((c) => (
                <div key={c.name} className="rounded-lg bg-secondary/50 px-2 py-1.5">
                  <div className="text-[11px] font-semibold text-foreground">{c.name}</div>
                  <div className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">{c.design}</div>
                </div>
              ))}
            </div>
          </details>
        )}
        {scenes.map((scene, index) => {
          const selected = scene.id === currentSceneId;
          return (
            <div
              key={scene.id}
              draggable
              onDragStart={() => setDragIndex(index)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => {
                if (dragIndex !== null) reorderScene(dragIndex, index);
                setDragIndex(null);
              }}
              onClick={() => setCurrent(scene.id)}
              className={cn(
                "group cursor-pointer rounded-xl border p-2 transition-colors",
                selected ? "border-primary/50 bg-primary/[0.08]" : "border-border bg-secondary/40 hover:border-white/20",
                dragIndex === index && "opacity-50"
              )}
            >
              <div className="flex h-16 items-center justify-center overflow-hidden rounded-lg bg-black/40">
                {scene.video ? (
                  <video src={fileUrl(scene.video)} muted preload="metadata" className="size-full object-cover" />
                ) : scene.image ? (
                  /* eslint-disable-next-line @next/next/no-img-element */
                  <img src={fileUrl(scene.image)} alt="" className="size-full object-cover" />
                ) : (
                  <Clapperboard className="size-5 text-muted-foreground" />
                )}
              </div>
              <div className="mt-2 flex items-center justify-between gap-1 px-0.5">
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">
                    {String(index + 1).padStart(2, "0")} {scene.title}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    <span>{scene.duration}s</span>
                    <span
                      className={
                        scene.gen_mode === "t2v"
                          ? "rounded bg-secondary px-1 text-[9px] text-muted-foreground"
                          : "rounded bg-primary/20 px-1 text-[9px] text-primary"
                      }
                      title={scene.gen_mode === "t2v" ? "文生视频" : "图生视频（先关键帧后视频）"}
                    >
                      {scene.gen_mode === "t2v" ? "t2v" : "i2v"}
                    </span>
                  </div>
                </div>
                <div className="hidden shrink-0 gap-0.5 group-hover:flex">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      duplicateScene(scene.id);
                    }}
                    className="rounded p-1 text-muted-foreground hover:text-foreground cursor-pointer"
                    title="Duplicate"
                  >
                    <Copy className="size-3" />
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteScene(scene.id);
                    }}
                    className="rounded p-1 text-muted-foreground hover:text-red-400 cursor-pointer"
                    title="Delete"
                  >
                    <Trash2 className="size-3" />
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
