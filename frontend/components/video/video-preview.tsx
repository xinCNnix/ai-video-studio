"use client";

import { Check, Clapperboard, Download, ShieldCheck } from "lucide-react";
import { fileUrl } from "@/lib/api";
import type { Job } from "@/lib/types";
import { cn } from "@/lib/utils";

const STEPS = [
  { key: "submitted", label: "Submitted" },
  { key: "generating", label: "Generating" },
  { key: "downloading", label: "Downloading" },
];

function stepState(job: Job, key: string): "done" | "active" | "pending" {
  const order = ["queued", "submitted", "generating", "downloading", "completed"];
  const current = order.indexOf(job.status);
  const target = order.indexOf(key);
  if (job.status === "completed") return "done";
  if (current > target) return "done";
  if (current === target) return "active";
  return "pending";
}

export function VideoPreview({ job, aspect, modelLabel }: { job: Job | null; aspect: string; modelLabel: string }) {
  const running = job && ["queued", "submitted", "generating", "downloading"].includes(job.status);
  const portrait = aspect === "9:16" || aspect === "3:4";

  return (
    <div className="relative flex min-w-0 flex-1 items-center justify-center overflow-hidden bg-[#0b0c10] p-6">
      {/* 生成中状态 */}
      {running && job && (
        <div className="flex w-full max-w-sm flex-col items-center gap-6 rounded-2xl border border-border bg-card/60 px-8 py-10">
          <div className="relative">
            <div className="size-14 animate-spin rounded-full border-2 border-white/10 border-t-primary" />
            <Clapperboard className="absolute inset-0 m-auto size-6 text-muted-foreground" />
          </div>
          <div className="text-center">
            <div className="text-sm font-medium">Generating with {modelLabel || job.model}</div>
            <div className="mt-1 text-xs text-muted-foreground">Waiting for model…</div>
          </div>
          {/* 不虚构百分比，仅显示阶段 */}
          <div className="w-full space-y-2.5">
            {STEPS.map((step, i) => {
              const state = stepState(job, step.key);
              return (
                <div key={step.key} className="flex items-center gap-3">
                  <div
                    className={cn(
                      "flex size-5 items-center justify-center rounded-full border text-[10px]",
                      state === "done" && "border-transparent bg-primary text-white",
                      state === "active" && "border-primary text-primary",
                      state === "pending" && "border-border text-muted-foreground"
                    )}
                  >
                    {state === "done" ? <Check className="size-3" /> : i + 1}
                  </div>
                  <span
                    className={cn(
                      "text-xs",
                      state === "pending" ? "text-muted-foreground/60" : "text-foreground"
                    )}
                  >
                    {step.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 视频预览 */}
      {!running && job?.status === "completed" && job.output && (
        <div className="flex h-full w-full items-center justify-center">
          <div
            className="relative overflow-hidden rounded-xl border border-border bg-black shadow-2xl"
            style={{
              aspectRatio: aspect.replace(":", " / "),
              height: portrait ? "100%" : "auto",
              width: portrait ? "auto" : "100%",
              maxWidth: "100%",
              maxHeight: "100%",
            }}
          >
            <video
              key={job.output}
              src={fileUrl(job.output)}
              controls
              autoPlay
              loop
              className="size-full"
            />
          </div>
        </div>
      )}

      {/* 失败状态 */}
      {!running && job?.status === "failed" && (
        <div className="flex max-w-sm flex-col items-center gap-3 rounded-2xl border border-red-500/20 bg-red-500/[0.04] px-8 py-10 text-center">
          <div className="text-sm font-medium text-red-400">生成失败</div>
          <div className="text-xs leading-relaxed text-muted-foreground">{job.error}</div>
        </div>
      )}

      {/* 空状态 */}
      {!job && (
        <div className="flex flex-col items-center gap-4 text-center">
          <div className="flex size-16 items-center justify-center rounded-2xl border border-dashed border-border/70 bg-secondary/30">
            <Clapperboard className="size-7 text-muted-foreground" />
          </div>
          <div>
            <div className="text-sm font-medium text-foreground/90">Your video will appear here</div>
            <div className="mt-1 text-xs text-muted-foreground">
              在左侧配置参数后点击 Generate Video，选择不同比例预览会自动适配
            </div>
          </div>
        </div>
      )}

      {/* 下载悬浮 */}
      {!running && job?.status === "completed" && job.output && (
        <div className="absolute right-6 top-6 flex items-center gap-2">
          {job.self_check && (
            <span
              title={job.self_check.checks.map((c) => `${c.name}：${c.pass ? "通过" : c.detail}`).join("\n")}
              className={cn(
                "inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs backdrop-blur",
                job.self_check.ok
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-300"
              )}
            >
              <ShieldCheck className="size-3.5" />
              {job.self_check.ok ? "自检通过" : "自检警告"}
            </span>
          )}
          <a
            href={`${fileUrl(job.output)}?dl=1`}
            download
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card/90 px-3 py-1.5 text-xs text-muted-foreground backdrop-blur transition-colors hover:text-foreground"
          >
            <Download className="size-3.5" />
            Download
          </a>
        </div>
      )}
    </div>
  );
}
