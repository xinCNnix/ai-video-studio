"use client";

/** 单镜抽屉：字段编辑（防抖命令）、素材绑定管理、三项生成按钮（统一后端路径）。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Loader2, Mic, Plus, Sparkles, Trash2, Video, X } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { displayFilename, materialOrders, orderLabel } from "@/lib/material-order";
import { MaterialThumb } from "@/components/studio-v2/material-thumb";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { cn } from "@/lib/utils";

export function ShotDrawer() {
  const project = useStudioV2Store((s) => s.project);
  const currentSceneId = useStudioV2Store((s) => s.currentSceneId);
  const setCurrentScene = useStudioV2Store((s) => s.setCurrentScene);
  const patchScene = useStudioV2Store((s) => s.patchScene);
  const deleteScene = useStudioV2Store((s) => s.deleteScene);
  const generate = useStudioV2Store((s) => s.generate);
  const bindMaterial = useStudioV2Store((s) => s.bindMaterial);
  const unbindMaterial = useStudioV2Store((s) => s.unbindMaterial);

  const scene = project?.scenes.find((s) => s.id === currentSceneId) ?? null;
  const [draft, setDraft] = useState(scene ?? null);
  const [busy, setBusy] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 真正被用户编辑过、尚未保存落地的字段（防抖窗口内）。SSE 推送的服务端更新
  // 只绕开这些字段——否则改时长后服务端同步缩放的 Prompt 会被本地旧草稿顶掉
  const editingRef = useRef<Set<string>>(new Set());

  // 切换分镜/服务端更新时同步草稿（保留正在输入的未保存字段优先）
  useEffect(() => {
    if (!scene) {
      setDraft(null);
      return;
    }
    setDraft((d) => (d && d.id === scene.id ? { ...scene, ...pickEditing(d, scene, editingRef.current) } : scene));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene?.id, scene?.revision, scene?.status, scene?.assets.video, scene?.assets.keyframe, scene?.assets.voice]);

  if (!project || !scene || !draft) return null;

  const scheduleSave = (patch: Record<string, unknown>) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void patchScene(scene.id, patch).then(() => {
        Object.keys(patch).forEach((k) => editingRef.current.delete(k));
      });
    }, 600);
  };

  const edit = (field: string, value: string | number) => {
    editingRef.current.add(field);
    setDraft((d) => (d ? { ...d, [field]: value } : d));
    scheduleSave({ [field]: value });
  };

  const run = async (kind: "keyframe" | "voice" | "video") => {
    setBusy(kind);
    try {
      // 已有视频时按钮语义就是"重生成"：带 force 让后端清旧视频直接重提（配音 force 走后端去重逻辑）
      await generate(scene.id, { kind, ...(kind === "video" && scene.assets.video ? { force: true } : {}) });
    } finally {
      setBusy(null);
    }
  };

  const materials = project.materials;
  const boundImages = scene.binding.images
    .map((b) => ({ binding: b, material: materials.find((m) => m.id === b.material_id) }))
    .filter((x) => x.material && x.material.url);
  const boundAudios = scene.binding.audios
    .map((id) => materials.find((m) => m.id === id))
    .filter(Boolean);
  // 素材上传序号（与后端 bind 对齐语义）：核对"这镜挂的图是原稿 <Image N> 的哪张"
  const poolMats = useMemo(
    () => materials.filter((m) => m.kind !== "script" && m.kind !== "doc" && m.url),
    [materials],
  );
  const orderOf = useMemo(() => {
    const orders = materialOrders(materials);
    const map = new Map<string, number | null>();
    materials.forEach((m, i) => map.set(m.id, orders[i]));
    return map;
  }, [materials]);
  const isStoryboard = (project.screenplay as { script_type?: string } | null)?.script_type === "storyboard";
  const boundIds = new Set([
    ...scene.binding.images.map((b) => b.material_id),
    ...scene.binding.audios,
    ...scene.binding.videos,
  ]);
  const pickableMats = poolMats;

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
            <span className="text-muted-foreground" title="修改时长会按比例同步缩放 Prompt 里的时间轴与总时长引用">
              时长 {draft.duration}s
            </span>
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

        {/* 素材绑定：缩略图 + 上传序号 + 本镜 <Picture k> 位置——生成前核对对错图的地方 */}
        <section className="space-y-1.5">
          <div className="flex items-center gap-2">
            <div className="text-xs font-medium">参考素材</div>
            <button
              type="button"
              onClick={() => setPicking((v) => !v)}
              className="inline-flex cursor-pointer items-center gap-0.5 rounded-md border border-border bg-secondary/40 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
              title="从素材池选择素材绑定到本镜（换图/补绑）"
            >
              <Plus className="size-3" />
              {picking ? "收起素材池" : "添加/换绑"}
            </button>
            {isStoryboard && <span className="text-[10px] text-muted-foreground">图N = 原稿 &lt;Image N&gt;</span>}
          </div>
          <div className="flex flex-wrap items-start gap-2">
            {boundImages.map(({ binding, material }, k) => {
              const order = orderOf.get(material!.id);
              return (
                <div key={binding.material_id} className="group relative">
                  <MaterialThumb
                    src={material!.url ?? ""}
                    badge={orderLabel("image", order)}
                    title={`${displayFilename(material!.filename)}｜${isStoryboard ? `第${order}张上传图 = 原稿 <Image ${order}>；` : ""}本镜第 ${k + 1} 张参考位 = Prompt 里的 <Picture ${k + 1}>`}
                    className="size-14"
                  />
                  {/* 本镜参考位：与 Prompt 的 <Picture k> 编号对应，悬停可核对声明行 */}
                  <span
                    className="absolute right-0.5 top-0.5 rounded bg-primary/90 px-1 text-[9px] font-semibold leading-4 text-white"
                    title={`本镜第 ${k + 1} 张参考位（Prompt 里的 <Picture ${k + 1}>）`}
                  >
                    P{k + 1}
                  </span>
                  {binding.role === "first_frame" && (
                    <span className="absolute bottom-0.5 left-0.5 rounded bg-black/70 px-1 text-[9px] leading-4 text-white">首帧</span>
                  )}
                  <button
                    type="button"
                    onClick={() => void unbindMaterial(scene.id, "image", material!.id)}
                    className="absolute -right-1 -top-1 hidden cursor-pointer rounded-full bg-red-500/90 p-0.5 text-white group-hover:block"
                    title="解绑此图"
                  >
                    <X className="size-2.5" />
                  </button>
                </div>
              );
            })}
            {boundAudios.map((m) => (
              <span
                key={m!.id}
                className="group inline-flex items-center gap-1 rounded-lg border border-border bg-secondary/40 px-2 py-1 text-[10px]"
                title={`${displayFilename(m!.filename)}｜${isStoryboard ? `第${orderOf.get(m!.id)}段上传音频 = 原稿 <Audio ${orderOf.get(m!.id)}>` : displayFilename(m!.filename)}${m!.duration ? `（${m!.duration}s）` : ""}`}
              >
                <Mic className="size-3" />
                {orderLabel("audio", orderOf.get(m!.id)) && (
                  <span className="font-semibold text-primary">{orderLabel("audio", orderOf.get(m!.id))}</span>
                )}
                {displayFilename(m!.filename).slice(0, 12)}
                {m!.duration != null && <span className="font-semibold text-amber-300">{m!.duration}s</span>}
                <button
                  type="button"
                  onClick={() => void unbindMaterial(scene.id, "audio", m!.id)}
                  className="hidden cursor-pointer text-muted-foreground hover:text-foreground group-hover:inline"
                  title="解绑此音频"
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
            {boundImages.length === 0 && boundAudios.length === 0 && (
              <span className="text-[10px] text-muted-foreground">（无绑定素材）</span>
            )}
          </div>
          {(() => {
            // 台词时长对账：剧本看不到配音精确时长，这里可见 + 不照时说明闸门行为
            const total = boundAudios.reduce((sum, m) => sum + (m?.duration ?? 0), 0);
            if (!total) return null;
            const over = total > scene.duration + 0.5;
            return (
              <div className={cn("text-[10px] leading-relaxed", over ? "text-amber-300" : "text-muted-foreground")}>
                {over
                  ? `台词共 ${total.toFixed(1)}s 超过镜头 ${scene.duration}s：生成前会自动把镜头校准为 ${Math.ceil(total)}s（超出模型上限则拦截，建议拆镜或精简台词）`
                  : `台词共 ${total.toFixed(1)}s / 镜头 ${scene.duration}s`}
              </div>
            );
          })()}
          {picking && (
            <div className="max-h-44 space-y-1 overflow-y-auto rounded-lg border border-border bg-background/60 p-2">
              {pickableMats.length === 0 && (
                <div className="text-[10px] text-muted-foreground">素材池为空——在上方投放面板上传参考图/音频</div>
              )}
              {pickableMats.map((m) => {
                const bound = boundIds.has(m.id);
                return (
                  <button
                    key={m.id}
                    type="button"
                    disabled={bound}
                    onClick={() => void bindMaterial(scene.id, m.kind, m.id)}
                    className={cn(
                      "flex w-full cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-left text-[10px] transition-colors",
                      bound ? "cursor-not-allowed opacity-40" : "hover:bg-secondary/60",
                    )}
                    title={bound ? "已绑定到本镜" : "点击绑定到本镜"}
                  >
                    {m.kind === "image" ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={fileUrl(m.url)} alt="" className="size-8 shrink-0 rounded object-cover" />
                    ) : m.kind === "audio" ? (
                      <span className="flex size-8 shrink-0 items-center justify-center rounded bg-secondary/60"><Mic className="size-3.5" /></span>
                    ) : (
                      <span className="flex size-8 shrink-0 items-center justify-center rounded bg-secondary/60"><FileText className="size-3.5" /></span>
                    )}
                    <span className="font-semibold text-primary">{orderLabel(m.kind, orderOf.get(m.id))}</span>
                    <span className="min-w-0 flex-1 truncate text-muted-foreground">{displayFilename(m.filename)}</span>
                    {bound && <span className="shrink-0 text-muted-foreground">已绑</span>}
                  </button>
                );
              })}
            </div>
          )}
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
        {(() => {
          // 原文单独窗口：分镜稿原文不再拼进 Prompt（对视频模型无用），这里对照展示
          const sp = project.screenplay as { beats?: { source_text?: string }[] } | null;
          const idx = project.scenes.findIndex((s) => s.id === scene.id);
          const src = sp?.beats?.[idx]?.source_text;
          if (!src) return null;
          return (
            <details className="rounded-lg border border-border bg-background/60">
              <summary className="cursor-pointer select-none px-3 py-1.5 text-xs font-medium">
                原文（分镜稿第 {idx + 1} 拍 · 不进 Prompt）
              </summary>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-border px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                {src}
              </pre>
            </details>
          );
        })()}
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

/** 保留真正正在编辑（用户动过且未保存落地）的字段，避免 SSE 更新打断输入。
 * 判定基于 editingRef 而不是"草稿≠服务端"——服务端主动改写的字段（如改时长后
 * 同步缩放的 Prompt）恰好会让旧草稿失配，按值判定会把旧草稿错误地保留下来。 */
function pickEditing(
  draft: { narration: string; prompt: string; keyframe_prompt: string; title: string; duration: number },
  server: typeof draft,
  editing: Set<string>,
) {
  const keep: Record<string, unknown> = {};
  for (const k of ["title", "narration", "prompt", "keyframe_prompt", "duration"] as const) {
    if (editing.has(k) && draft[k] !== server[k]) keep[k] = draft[k];
  }
  return keep;
}
