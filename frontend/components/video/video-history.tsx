"use client";

import { Download, Film, RefreshCw, Trash2 } from "lucide-react";
import { fileUrl } from "@/lib/api";
import type { Job } from "@/lib/types";
import { cn, fmtDate, saveFile } from "@/lib/utils";

export function VideoHistory({
  jobs,
  selectedId,
  onSelect,
  onDelete,
  onRefresh,
}: {
  jobs: Job[];
  selectedId: string | null;
  onSelect: (job: Job) => void;
  onDelete: (id: string) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="flex h-full w-[260px] shrink-0 flex-col border-l border-border bg-card/40">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-sm font-semibold">History</span>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground cursor-pointer"
          title="刷新"
        >
          <RefreshCw className="size-3.5" />
        </button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {jobs.length === 0 && (
          <div className="px-2 py-8 text-center text-xs text-muted-foreground">暂无生成记录</div>
        )}
        {jobs.map((job) => {
          const aspect = (job.params?.aspect_ratio as string) || "16:9";
          const duration = (job.params?.duration as number) || 5;
          const credential = (job.params?.credential as string) || "";
          const seed = job.params?.seed as number | undefined;
          const selected = job.id === selectedId;
          return (
            <button
              key={job.id}
              type="button"
              onClick={() => onSelect(job)}
              className={cn(
                "group flex w-full cursor-pointer items-center gap-3 rounded-xl border p-2 text-left transition-colors",
                selected
                  ? "border-primary/50 bg-primary/[0.08]"
                  : "border-border bg-secondary/40 hover:border-white/20"
              )}
            >
              <div className="flex size-11 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-black/40">
                {job.output ? (
                  <video
                    src={`${fileUrl(job.output)}#t=1`}
                    muted
                    className="size-full object-cover"
                    preload="metadata"
                  />
                ) : (
                  <Film className="size-4 text-muted-foreground" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium" title={String(job.params?.prompt ?? "")}>
                  {String(job.params?.prompt ?? "").trim().slice(0, 60) || "（无 Prompt）"}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                  {aspect} <span>·</span> {duration}s
                  <span>·</span>
                  <span className="truncate">{job.model}</span>
                  {credential && (
                    <>
                      <span>·</span>
                      <span className="shrink-0 text-emerald-300/70" title={`本次使用的 Key：${credential}`}>
                        🔑{credential}
                      </span>
                    </>
                  )}
                  {seed != null && (
                    <>
                      <span>·</span>
                      <span className="shrink-0" title="本次使用的随机种子：相同 seed + 相同参数可复现同样结果">
                        🎲{seed}
                      </span>
                    </>
                  )}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                  <span>{fmtDate(job.created_at)}</span>
                  {job.status === "failed" && <span className="text-red-400">失败</span>}
                  {["queued", "submitted", "generating", "downloading"].includes(job.status) && (
                    <span className="text-primary">生成中…</span>
                  )}
                </div>
              </div>
              {job.output && (
                <span
                  role="button"
                  tabIndex={0}
                  title="下载"
                  onClick={(e) => {
                    e.stopPropagation();
                    saveFile(fileUrl(job.output));
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.stopPropagation();
                      saveFile(fileUrl(job.output));
                    }
                  }}
                  className="rounded-md p-1 text-muted-foreground hover:text-foreground cursor-pointer"
                >
                  <Download className="size-3.5" />
                </span>
              )}
              <span
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(job.id);
                }}
                onKeyDown={(e) => e.key === "Enter" && onDelete(job.id)}
                className="hidden rounded-md p-1 text-muted-foreground hover:text-red-400 group-hover:block cursor-pointer"
              >
                <Trash2 className="size-3.5" />
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
