"use client";

/** 分镜看板：镜号跳转条 + 状态过滤 + 卡片墙 + 上/下移排序 + 点击打开单镜抽屉。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Clapperboard, Film, Mic, Plus } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { materialOrders, orderLabel } from "@/lib/material-order";
import { SCENE_STATUS_LABELS } from "@/lib/types-v2";
import type { SceneV2 } from "@/lib/types-v2";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { cn } from "@/lib/utils";

type StatusFilter = "all" | "ungenerated" | "failed";
const FILTERS: { key: StatusFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "ungenerated", label: "未生成" },
  { key: "failed", label: "失败" },
];

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
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [visiblePos, setVisiblePos] = useState<Set<number>>(new Set());
  const gridRef = useRef<HTMLDivElement>(null);
  const sceneCount = project?.scenes.length ?? 0;
  // 素材上传序号映射（图N/音N）：与后端 bind 的编号语义同源，小图 tooltip 用它代替 UUID 文件名
  const orderOf = useMemo(() => {
    const mats = project?.materials ?? [];
    const orders = materialOrders(mats);
    const map = new Map<string, number | null>();
    mats.forEach((m, i) => map.set(m.id, orders[i]));
    return map;
  }, [project?.materials]);

  // 镜号跳转条的高亮：跟踪中间滚动列的视口，标出当前可见的镜
  useEffect(() => {
    const grid = gridRef.current;
    const scroller = grid?.closest("div.overflow-y-auto") as HTMLElement | null;
    if (!grid || !scroller) return;
    const update = () => {
      const sr = scroller.getBoundingClientRect();
      const set = new Set<number>();
      grid.querySelectorAll<HTMLElement>("[data-scene-pos]").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.bottom > sr.top && r.top < sr.bottom) set.add(Number(el.dataset.scenePos));
      });
      setVisiblePos((prev) => (prev.size === set.size && [...set].every((x) => prev.has(x)) ? prev : set));
    };
    update();
    scroller.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      scroller.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [sceneCount, filter]);

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

  const jumpTo = (pos: number) => {
    const card = gridRef.current?.querySelector<HTMLElement>(`[data-scene-pos="${pos}"]`);
    card?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const matchFilter = (s: SceneV2) =>
    filter === "all"
    || (filter === "ungenerated" && !s.assets.video)
    || (filter === "failed" && (s.status === "failed" || s.error));

  return (
    <div>
      {/* 镜号跳转条（横滚）+ 状态过滤：大项目下快速定位。
          过滤放左侧：右侧会被单镜抽屉/助手浮层盖住，数字条本身可横滚不受影响 */}
      <div className="mb-2 flex items-center gap-2">
        <div className="flex shrink-0 gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              className={cn(
                "cursor-pointer rounded-md px-2 py-0.5 text-[10px]",
                filter === f.key ? "bg-primary/20 text-primary" : "bg-secondary/50 text-muted-foreground hover:text-foreground",
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto pb-0.5">
          {scenes.map((s, i) => (
            <button
              key={s.id}
              type="button"
              onClick={() => jumpTo(i)}
              title={`${i + 1}. ${s.title}`}
              className={cn(
                "flex h-5 min-w-5 shrink-0 cursor-pointer items-center justify-center rounded px-1 text-[10px] transition-colors",
                s.status === "failed" || s.error
                  ? "bg-red-500/15 text-red-300"
                  : s.assets.video
                    ? "bg-emerald-500/15 text-emerald-300"
                    : "bg-secondary/60 text-muted-foreground hover:text-foreground",
                visiblePos.has(i) && "ring-1 ring-primary/70 font-semibold text-primary",
              )}
            >
              {i + 1}
            </button>
          ))}
        </div>
      </div>
      <div ref={gridRef} className="grid grid-cols-[repeat(auto-fill,minmax(210px,1fr))] gap-3 pb-1">
        {scenes.map((s, i) => {
          const thumb = s.assets.video || s.assets.keyframe;
          const boundImgs = s.binding.images
            .map((b) => project.materials.find((m) => m.id === b.material_id))
            .filter((m): m is NonNullable<typeof m> => Boolean(m?.url))
            .slice(0, 3);
          // 台词总时长（绑定的上传音频）：与镜头时长不照 → 卡片直接亮黄提示
          const audioTotal = s.binding.audios.reduce(
            (sum, id) => sum + (project.materials.find((m) => m.id === id)?.duration ?? 0),
            0,
          );
          const audioOver = audioTotal > s.duration + 0.5;
          return (
            <div
              key={s.id}
              data-scene-pos={i}
              className={cn(
                "group relative cursor-pointer overflow-hidden rounded-xl border border-border bg-card/60 transition-colors hover:border-primary/50",
                !matchFilter(s) && "hidden",
              )}
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
                ) : boundImgs[0]?.url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={fileUrl(boundImgs[0].url)} alt={s.title} className="size-full object-cover opacity-70" />
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
                  <span className="rounded bg-secondary/60 px-1">{s.gen_mode === "i2v" ? "图生" : s.gen_mode === "t2v" ? "文生" : "首尾帧"}</span>
                  {boundImgs.length > 0 && (
                    // 绑定参考图小缩略图：看板上一眼核对每镜挂的图，不用点开抽屉
                    <span className="inline-flex items-center gap-0.5">
                      {boundImgs.map((m) => (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          key={m.id}
                          src={fileUrl(m.url)}
                          alt={m.filename}
                          title={`${orderLabel("image", orderOf.get(m.id)) || "图"}｜${m.filename}`}
                          className="size-3.5 rounded-[3px] border border-border object-cover"
                        />
                      ))}
                      {s.binding.images.length > boundImgs.length && `+${s.binding.images.length - boundImgs.length}`}
                    </span>
                  )}
                  {s.binding.audios.length > 0 && (
                    <span
                      className={cn("inline-flex items-center gap-0.5", audioOver && "text-amber-300")}
                      title={audioOver
                        ? `绑定台词共 ${audioTotal.toFixed(1)}s 超过镜头 ${s.duration}s：生成前会自动校准镜头时长（超出模型上限则拦截）`
                        : `绑定台词 ${s.binding.audios.length} 段${audioTotal ? `，共 ${audioTotal.toFixed(1)}s` : ""}`}
                    >
                      <Mic className="size-2.5" />
                      {s.binding.audios.length}
                      {audioOver ? `｜${audioTotal.toFixed(1)}s>${s.duration}s` : ""}
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
