"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AudioLines, Clapperboard, Download, Film, ImagePlus, Play } from "lucide-react";
import { api, fileUrl } from "@/lib/api";
import type { Job } from "@/lib/types";
import { fmtDate } from "@/lib/utils";

export function RecentGenerations() {
  const router = useRouter();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listJobs({ status: "completed", limit: 12 })
      .then(({ jobs }) => setJobs(jobs))
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, []);

  const open = (job: Job) => {
    if (job.type === "video") router.push("/video");
    else if (job.type === "image") router.push("/image");
    else router.push("/voice");
  };

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Recent Generations</h3>
      </div>

      {loading ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="aspect-video animate-pulse rounded-xl bg-secondary/50" />
          ))}
        </div>
      ) : jobs.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border/70 bg-secondary/20 py-10 text-center">
          <p className="text-xs text-muted-foreground">
            还没有生成记录 —— 从上面的卡片开始你的第一次创作吧
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
          {jobs.map((job) => (
            <div key={job.id} className="group relative">
              <button
                type="button"
                onClick={() => open(job)}
                className="relative flex aspect-video w-full cursor-pointer items-center justify-center overflow-hidden rounded-xl border border-border bg-secondary/30 transition-all hover:border-primary/40"
              >
                {job.type === "image" && job.output && (
                  /* eslint-disable-next-line @next/next/no-img-element */
                  <img src={fileUrl(job.output)} alt="" className="absolute inset-0 size-full object-cover" />
                )}
                {job.type === "video" && job.output && (
                  <video src={fileUrl(job.output)} muted preload="metadata" className="absolute inset-0 size-full object-cover" />
                )}
                {(!job.output || job.type === "tts") && (
                  <div className="flex flex-col items-center gap-2 p-3">
                    {job.type === "tts" ? (
                      <AudioLines className="size-6 text-emerald-300/80" />
                    ) : job.type === "video" ? (
                      <Film className="size-6 text-fuchsia-300/80" />
                    ) : (
                      <ImagePlus className="size-6 text-sky-300/80" />
                    )}
                    <span className="line-clamp-2 text-[10px] text-muted-foreground">
                      {String(job.params?.prompt ?? job.params?.text ?? job.model)}
                    </span>
                  </div>
                )}
                {job.type === "video" && job.output && (
                  <div className="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 transition-opacity group-hover:opacity-100">
                    <Play className="size-8 text-white" />
                  </div>
                )}
                <div className="absolute bottom-0 left-0 right-0 flex items-center justify-between bg-gradient-to-t from-black/80 to-transparent px-2.5 pb-1.5 pt-6 text-[10px]">
                  <span className="font-medium text-white/90 capitalize">{job.type}</span>
                  <span className="text-white/60">{fmtDate(job.created_at)}</span>
                </div>
              </button>
              {job.output && (
                <a
                  href={`${fileUrl(job.output)}?dl=1`}
                  download
                  target="_blank"
                  rel="noreferrer"
                  title="下载"
                  className="absolute right-2 top-2 z-10 flex size-7 items-center justify-center rounded-lg bg-black/50 text-white/80 backdrop-blur transition-colors hover:bg-black/70 hover:text-white"
                >
                  <Download className="size-3.5" />
                </a>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="mt-6 text-center">
        <Link href="/studio" className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
          或进入 Studio，把 Prompt、图片、声音、视频整合成一个作品 →
        </Link>
      </div>
    </div>
  );
}
