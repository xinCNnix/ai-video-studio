"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { useStudioStore } from "@/stores/studio-store";
import { cn } from "@/lib/utils";

export function SceneStrip() {
  const project = useStudioStore((s) => s.project);
  const currentSceneId = useStudioStore((s) => s.currentSceneId);
  const setCurrent = useStudioStore((s) => s.setCurrent);
  const addScene = useStudioStore((s) => s.addScene);
  const reorderScene = useStudioStore((s) => s.reorderScene);
  const updateScene = useStudioStore((s) => s.updateScene);

  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const scenes = project?.scenes ?? [];

  const commitDuration = () => {
    if (editId) {
      // 与当前模型家族的最大上限一致（Seedance 2.5 / Wan 3.0 为 30s，MiniMax H3 为 15s）；
      // 实际生成时滑块会按所选模型的具体 durations 限制
      const n = Math.max(1, Math.min(30, Math.round(Number(draft) || 0)));
      updateScene(editId, { duration: n });
      if (Number(draft) > 30) toast("时长已限制为 30s（当前模型家族最大值）；MiniMax H3 上限 15s");
    }
    setEditId(null);
  };

  return (
    <div className="flex h-[88px] shrink-0 items-center gap-2 overflow-x-auto border-t border-border bg-card/40 px-4">
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
              "flex h-16 w-24 shrink-0 cursor-pointer flex-col items-center justify-center rounded-lg border text-xs transition-colors",
              selected
                ? "border-primary/60 bg-primary/[0.1] text-foreground"
                : "border-border bg-secondary/40 text-muted-foreground hover:border-white/20",
              dragIndex === index && "opacity-50"
            )}
          >
            <span className="font-mono text-[10px] text-primary/80">{String(index + 1).padStart(2, "0")}</span>
            <span className="max-w-full truncate px-1 text-[11px]">{scene.title}</span>
            {editId === scene.id ? (
              <input
                autoFocus
                type="number"
                min={1}
                max={60}
                value={draft}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitDuration}
                onKeyDown={(e) => e.key === "Enter" && commitDuration()}
                className="w-9 rounded border border-primary/50 bg-secondary px-0.5 text-center text-[10px] focus:outline-none"
              />
            ) : (
              <span
                className="text-[10px] text-muted-foreground hover:text-foreground cursor-text"
                title="双击修改时长"
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  setDraft(String(scene.duration));
                  setEditId(scene.id);
                }}
              >
                {scene.duration}s
              </span>
            )}
          </div>
        );
      })}
      <button
        type="button"
        onClick={addScene}
        className="flex h-16 w-12 shrink-0 cursor-pointer items-center justify-center rounded-lg border border-dashed border-border text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
        title="新增场景"
      >
        <Plus className="size-4" />
      </button>
    </div>
  );
}
