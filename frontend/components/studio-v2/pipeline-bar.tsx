"use client";

/** 流水线状态条：阶段芯片 + 暂停/失败提示 + 确认门禁动作（review/bindings）。 */
import { AlertTriangle, Check, ChevronRight, Loader2, PauseCircle, XCircle } from "lucide-react";
import { toast } from "sonner";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { PIPELINE_STAGE_LABELS } from "@/lib/types-v2";
import { cn } from "@/lib/utils";

const STAGES = ["intake", "normalize", "plan", "bind", "review", "generate", "assemble"] as const;

export function PipelineBar() {
  const project = useStudioV2Store((s) => s.project);
  const advancePipeline = useStudioV2Store((s) => s.advancePipeline);
  const confirmBindings = useStudioV2Store((s) => s.confirmBindings);
  if (!project) return null;
  const { pipeline } = project;
  const doneSet = new Set(pipeline.history.filter((h) => h.status === "done").map((h) => h.stage));
  const currentIndex = STAGES.indexOf(pipeline.stage as (typeof STAGES)[number]);

  return (
    <div className="shrink-0 rounded-2xl border border-border bg-card/40 p-3">
      <div className="flex flex-wrap items-center gap-1">
        {pipeline.state === "running" && <Loader2 className="mr-1 size-4 animate-spin text-primary" />}
        {pipeline.state === "paused" && <PauseCircle className="mr-1 size-4 text-amber-400" />}
        {pipeline.state === "failed" && <XCircle className="mr-1 size-4 text-red-400" />}
        {pipeline.state === "done" && <Check className="mr-1 size-4 text-emerald-400" />}
        {STAGES.map((s, i) => (
          <span key={s} className="flex items-center">
            {i > 0 && <ChevronRight className="mx-0.5 size-3 text-muted-foreground/50" />}
            <span
              className={cn(
                "rounded-full px-2.5 py-0.5 text-[11px]",
                doneSet.has(s) && "bg-emerald-500/15 text-emerald-300",
                i === currentIndex && pipeline.state === "running" && "bg-primary/20 text-primary",
                i === currentIndex && pipeline.state === "paused" && "bg-amber-500/15 text-amber-300",
                i === currentIndex && pipeline.state === "failed" && "bg-red-500/15 text-red-300",
                !doneSet.has(s) && i !== currentIndex && "bg-secondary/50 text-muted-foreground",
              )}
            >
              {PIPELINE_STAGE_LABELS[s]}
            </span>
          </span>
        ))}
        <div className="flex-1" />
        {pipeline.state === "paused" && pipeline.awaiting && (
          <button
            type="button"
            onClick={async () => {
              if (pipeline.awaiting === "bindings" && project.pending_bindings.length > 0) {
                await confirmBindings();
                toast.success("已确认素材分配并继续");
              } else {
                await advancePipeline(true);
                toast.success("已确认，开始生成");
              }
            }}
            className="accent-gradient cursor-pointer rounded-lg px-4 py-1.5 text-xs font-semibold text-white"
          >
            确认并开始生成
          </button>
        )}
        {pipeline.state === "failed" && (
          <button
            type="button"
            onClick={() => void advancePipeline(false)}
            className="cursor-pointer rounded-lg border border-border bg-card px-3 py-1.5 text-xs hover:bg-secondary/60"
          >
            从失败处重试
          </button>
        )}
      </div>

      {pipeline.state === "paused" && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-300">
          <PauseCircle className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {pipeline.awaiting === "review" && project.pending_bindings.length > 0
              ? `有 ${project.pending_bindings.length} 项素材归属待确认（见下方待确认列表，可逐项指派或直接确认保留现状）`
              : pipeline.awaiting === "review"
                ? "分镜与素材已就绪，确认后开始生成（会消耗额度）"
                : pipeline.awaiting === "resume"
                  ? "上次运行中断，可从检查点续跑（点击「确认并开始生成」继续）"
                  : "等待补充输入（剧本或素材）"}
          </span>
        </div>
      )}
      {pipeline.state === "failed" && pipeline.error && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-red-300">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{pipeline.error}</span>
        </div>
      )}

      {project.pending_bindings.length > 0 && (
        <div className="mt-2 max-h-36 space-y-1.5 overflow-y-auto pr-1">
          {project.pending_bindings.map((pb) => {
            const material = project.materials.find((m) => m.id === pb.material_id);
            const suggested = project.scenes.find((s) => s.id === pb.suggested_scene_id);
            return (
              <div key={pb.material_id} className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-2.5 py-1.5 text-[11px]">
                <span className="font-medium">{material?.filename ?? pb.material_id}</span>
                <span className="text-muted-foreground">{pb.note}</span>
                {suggested && (
                  <button
                    type="button"
                    onClick={() =>
                      void useStudioV2Store.getState().command(async (p) => {
                        const { p2 } = await import("@/lib/api-v2");
                        return p2.bindMaterial(p.id, {
                          scene_id: suggested.id,
                          kind: material?.kind === "audio" ? "audio" : "image",
                          material_id: pb.material_id,
                          base_version: p.version,
                        });
                      })
                    }
                    className="cursor-pointer rounded-md border border-border bg-card px-2 py-0.5 hover:bg-secondary/60"
                  >
                    挂到「{suggested.title}」
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
