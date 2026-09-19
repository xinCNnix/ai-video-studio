"use client";

/** 单镜抽屉：字段编辑（防抖命令）、素材绑定管理、三项生成按钮（统一后端路径）。 */
import { useEffect, useRef, useState } from "react";
import { Image as ImageIcon, Loader2, Mic, Sparkles, Trash2, Video, X } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { cn } from "@/lib/utils";

export function ShotDrawer() {
  const project = useStudioV2Store((s) => s.project);
  const currentSceneId = useStudioV2Store((s) => s.currentSceneId);
  const setCurrentScene = useStudioV2Store((s) => s.setCurrentScene);
  const patchScene = useStudioV2Store((s) => s.patchScene);
  const deleteScene = useStudioV2Store((s) => s.deleteScene);
  const generate = useStudioV2Store((s) => s.generate);

  const scene = project?.scenes.find((s) => s.id === currentSceneId) ?? null;
  const [draft, setDraft] = useState(scene ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 切换分镜/服务端更新时同步草稿（保留正在输入的未保存字段优先）
  useEffect(() => {
    if (!scene) {
      setDraft(null);
      return;
    }
    setDraft((d) => (d && d.id === scene.id ? { ...scene, ...pickEditing(d, scene) } : scene));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene?.id, scene?.revision, scene?.status, scene?.assets.video, scene?.assets.keyframe, scene?.assets.voice]);

  if (!project || !scene || !draft) return null;

  const scheduleSave = (patch: Record<string, unknown>) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => void patchScene(scene.id, patch), 600);
  };

  const edit = (field: string, value: string | number) => {
    setDraft((d) => (d ? { ...d, [field]: value } : d));
    scheduleSave({ [field]: value });
  };

  const run = async (kind: "keyframe" | "voice" | "video") => {
    setBusy(kind);
    try {
      await generate(scene.id, { kind });
    } finally {
      setBusy(null);
    }
  };

  const materials = project.materials;
  const boundImages = scene.binding.images
    .map((b) => ({ binding: b, material: materials.find((m) => m.id === b.material_id) }))
    .filter((x) => x.material);
  const boundAudios = scene.binding.audios
    .map((id) => materials.find((m) => m.id === id))
    .filter(Boolean);

  return (
    <div className="flex h-full w-[380px] shrink-0 flex-col overflow-y-auto border-l border-border bg-card/30">
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card/80 px-4 py-3 backdrop-blur">
        <input
          value={draft.title}
          onChange={(e) => edit("title", e.target.value)}
          className="min-w-0 flex-1 bg-transparent text-sm font-semibold outline-none"
        />
        <button
          type="button"
          onClick={() => void deleteScene(scene.id).then(() => setCurrentScene(null))}
          className="cursor-pointer rounded-md p-1 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"
          title="删除分镜"
        >
          <Trash2 className="size-4" />
        </button>
        <button type="button" onClick={() => setCurrentScene(null)} className="cursor-pointer rounded-md p-1 text-muted-foreground hover:text-foreground">
          <X className="size-4" />
        </button>
      </div>

      <div className="space-y-4 p-4">
        {/* 概要 */}
        <div className="grid grid-cols-2 gap-2 text-xs">
          <label className="space-y-1">
            <span className="text-muted-foreground">时长 {draft.duration}s</span>
            <input
              type="range"
              min={1}
              max={30}
              value={draft.duration}
              onChange={(e) => edit("duration", Number(e.target.value))}
              className="w-full accent-primary"
            />
          </label>
          <div className="flex items-end gap-1.5 pb-1">
            {(["i2v", "t2v"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => edit("gen_mode", m)}
                className={cn(
                  "cursor-pointer rounded-md px-2.5 py-1 text-[11px]",
                  draft.gen_mode === m ? "bg-primary/20 text-primary" : "bg-secondary/60 text-muted-foreground",
                )}
              >
                {m === "i2v" ? "图生视频" : "文生视频"}
              </button>
            ))}
          </div>
        </div>

        {/* 生成动作（统一走后端 generation_service） */}
        <div className="grid grid-cols-3 gap-2">
          <button
            type="button"
            onClick={() => void run("keyframe")}
            disabled={busy !== null || scene.status === "generating"}
            className="inline-flex cursor-pointer items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 py-2 text-[11px] hover:bg-secondary/60 disabled:opacity-50"
          >
            {busy === "keyframe" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
            {scene.assets.keyframe ? "重生成首帧" : "生成首帧"}
          </button>
          <button
            type="button"
            onClick={() => void run("voice")}
            disabled={busy !== null || scene.status === "generating"}
            className="inline-flex cursor-pointer items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 py-2 text-[11px] hover:bg-secondary/60 disabled:opacity-50"
          >
            {busy === "voice" ? <Loader2 className="size-3.5 animate-spin" /> : <Mic className="size-3.5" />}
            {scene.assets.voice ? "重配旁白" : "生成旁白"}
          </button>
          <button
            type="button"
            onClick={() => void run("video")}
            disabled={busy !== null || scene.status === "generating"}
            className="accent-gradient inline-flex cursor-pointer items-center justify-center gap-1 rounded-lg px-2 py-2 text-[11px] font-semibold text-white disabled:opacity-50"
          >
            {busy === "video" ? <Loader2 className="size-3.5 animate-spin" /> : <Video className="size-3.5" />}
            {scene.assets.video ? "重生成视频" : "生成视频"}
          </button>
        </div>
        {scene.error && <div className="rounded-lg bg-red-500/10 px-3 py-2 text-[11px] text-red-300">{scene.error}</div>}

        {/* 素材绑定 */}
        <section className="space-y-1.5">
          <div className="text-xs font-medium">参考素材</div>
          <div className="flex flex-wrap gap-1.5">
            {boundImages.map(({ binding, material }) => (
              <span key={binding.material_id} className="inline-flex items-center gap-1 rounded-lg border border-border bg-secondary/40 px-2 py-1 text-[10px]">
                <ImageIcon className="size-3" />
                {material!.filename.slice(0, 14)}
                {binding.role === "first_frame" && <span className="text-primary">首帧</span>}
              </span>
            ))}
            {boundAudios.map((m) => (
              <span key={m!.id} className="inline-flex items-center gap-1 rounded-lg border border-border bg-secondary/40 px-2 py-1 text-[10px]">
                <Mic className="size-3" />
                {m!.filename.slice(0, 14)}
              </span>
            ))}
            {boundImages.length === 0 && boundAudios.length === 0 && (
              <span className="text-[10px] text-muted-foreground">（无绑定素材）</span>
            )}
          </div>
        </section>

        {/* 产物预览 */}
        {(scene.assets.keyframe || scene.assets.video || scene.assets.voice) && (
          <section className="space-y-1.5">
            <div className="text-xs font-medium">产物</div>
            {scene.assets.video && (
              <video src={fileUrl(scene.assets.video)} controls className="w-full rounded-lg border border-border" />
            )}
            {!scene.assets.video && scene.assets.keyframe && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={fileUrl(scene.assets.keyframe)} alt="关键帧" className="w-full rounded-lg border border-border" />
            )}
            {scene.assets.voice && (
              <audio src={fileUrl(scene.assets.voice)} controls className="w-full" />
            )}
          </section>
        )}

        {/* 文本字段 */}
        <label className="block space-y-1">
          <span className="text-xs font-medium">旁白</span>
          <textarea
            value={draft.narration}
            onChange={(e) => edit("narration", e.target.value)}
            className="h-16 w-full resize-none rounded-lg border border-border bg-background/60 p-2 text-xs outline-none focus:border-primary/60"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium">视频 Prompt</span>
          <textarea
            value={draft.prompt}
            onChange={(e) => edit("prompt", e.target.value)}
            className="h-40 w-full resize-none rounded-lg border border-border bg-background/60 p-2 font-mono text-[11px] leading-relaxed outline-none focus:border-primary/60"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium">关键帧描述</span>
          <textarea
            value={draft.keyframe_prompt}
            onChange={(e) => edit("keyframe_prompt", e.target.value)}
            className="h-16 w-full resize-none rounded-lg border border-border bg-background/60 p-2 text-xs outline-none focus:border-primary/60"
          />
        </label>
      </div>
    </div>
  );
}

/** 保留正在编辑（草稿与服务端不同）的字段，避免 SSE 更新打断输入 */
function pickEditing(draft: { narration: string; prompt: string; keyframe_prompt: string; title: string; duration: number }, server: typeof draft) {
  const keep: Record<string, unknown> = {};
  for (const k of ["title", "narration", "prompt", "keyframe_prompt", "duration"] as const) {
    if (draft[k] !== server[k]) keep[k] = draft[k];
  }
  return keep;
}
