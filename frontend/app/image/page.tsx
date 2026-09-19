"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { api, fileUrl, pollJob } from "@/lib/api";
import { loadModelPref, saveModelPref } from "@/lib/model-prefs";
import { useAppStore } from "@/stores/app-store";
import type { Job } from "@/lib/types";
import { ImageGenerator } from "@/components/image/image-generator";
import { ImageGrid, type ImageTile } from "@/components/image/image-grid";
import { Lightbox } from "@/components/common/lightbox";

const ASPECT_SIZES: Record<string, string> = {
  "1:1": "1024x1024",
  "3:4": "1024x1536",
  "4:3": "1536x1024",
  "9:16": "1024x1536",
  "16:9": "1536x1024",
};

export default function ImagePage() {
  const router = useRouter();
  const catalog = useAppStore((s) => s.catalog);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const setPendingVideo = useAppStore((s) => s.setPendingVideo);

  const [modelId, setModelIdRaw] = useState(() => loadModelPref("image"));
  const setModelId = (id: string) => {
    setModelIdRaw(id);
    saveModelPref("image", id);
  };
  const [prompt, setPrompt] = useState("");
  const [references, setReferences] = useState<string[]>([]);
  const [aspect, setAspect] = useState("1:1");
  const [sizeCustom, setSizeCustom] = useState("");
  const [n, setN] = useState(1);

  const [tiles, setTiles] = useState<ImageTile[]>([]);
  const [pending, setPending] = useState(false);
  const [lightbox, setLightbox] = useState<string | null>(null);

  const deleteJob = async (jobId: string) => {
    try {
      await api.deleteJob(jobId);
      setTiles((prev) => prev.filter((t) => t.jobId !== jobId));
      toast.success("已删除该任务的生成记录");
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  useEffect(() => {
    void loadCatalog();
    void loadCredentials();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (catalog?.image.length && !catalog.image.some((m) => m.id === modelId)) {
      setModelId(catalog.image[0].id);
    }
  }, [catalog, modelId]);

  const loadHistory = useCallback(async () => {
    try {
      const { jobs } = await api.listJobs({ type: "image", status: "completed", limit: 24 });
      setTiles(
        jobs.flatMap((job: Job) =>
          (job.outputs ?? []).map((url) => ({
            url,
            jobId: job.id,
            credential: (job.params?.credential as string) || undefined,
          }))
        )
      );
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // 本页生成中或 Agent 任务执行中时轮询刷新历史（其余时间不请求）
  const agentBusy = useAppStore((s) => s.agentBusy);
  useEffect(() => {
    if (!pending && !agentBusy) return;
    const t = setInterval(() => void loadHistory(), 8000);
    return () => clearInterval(t);
  }, [pending, agentBusy, loadHistory]);

  const generate = async () => {
    const model = catalog?.image.find((m) => m.id === modelId);
    if (!model) return;
    const custom = sizeCustom.trim();
    if (custom && !/^\d{2,5}x\d{2,5}$/.test(custom)) {
      toast.error("自定义分辨率格式应为 宽x高，例如 1024x1536");
      return;
    }
    setPending(true);
    try {
      const { job } = await api.generateImage({
        provider: model.provider,
        model: modelId,
        prompt,
        aspect_ratio: aspect,
        size: custom || ASPECT_SIZES[aspect],
        n,
        references,
      });
      const finalJob = await pollJob(job.id, () => undefined);
      if (finalJob.status === "completed") {
        setTiles((prev) => [
          ...(finalJob.outputs ?? []).map((url) => ({
            url,
            jobId: finalJob.id,
            credential: (finalJob.params?.credential as string) || undefined,
          })),
          ...prev,
        ]);
        toast.success("图片生成完成");
      } else {
        toast.error(finalJob.error || "图片生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      <ImageGenerator
        modelId={modelId}
        onModelChange={setModelId}
        prompt={prompt}
        onPromptChange={setPrompt}
        references={references}
        onAddReferences={(urls) => setReferences((prev) => [...prev, ...urls])}
        onRemoveReference={(url) => setReferences((prev) => prev.filter((u) => u !== url))}
        aspect={aspect}
        onAspectChange={setAspect}
        sizeCustom={sizeCustom}
        onSizeCustomChange={setSizeCustom}
        n={n}
        onNChange={setN}
        onGenerate={() => void generate()}
        generating={pending}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <ImageGrid
          tiles={tiles}
          pending={pending}
          onUseAsReference={(url) => {
            setReferences((prev) => (prev.includes(url) ? prev : [...prev, url]));
            toast.success("已加入参考图");
          }}
          onSendToVideo={(url) => {
            setPendingVideo({ mode: "image", images: [url] });
            router.push("/video");
          }}
          onOpenImage={(url) => setLightbox(url)}
          onDeleteJob={(jobId) => void deleteJob(jobId)}
        />
      </div>
      <Lightbox open={!!lightbox} onClose={() => setLightbox(null)} src={lightbox ? fileUrl(lightbox) : null} />
    </div>
  );
}
