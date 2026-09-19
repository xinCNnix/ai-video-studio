"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { api, pollJob } from "@/lib/api";
import { loadModelPref, saveModelPref } from "@/lib/model-prefs";
import { useAppStore } from "@/stores/app-store";
import type { Job } from "@/lib/types";
import { VideoSettings } from "@/components/video/video-settings";
import { VideoPreview } from "@/components/video/video-preview";
import { VideoHistory } from "@/components/video/video-history";

export default function VideoPage() {
  const catalog = useAppStore((s) => s.catalog);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const pendingVideo = useAppStore((s) => s.pendingVideo);
  const setPendingVideo = useAppStore((s) => s.setPendingVideo);

  const [modelId, setModelIdRaw] = useState(() => loadModelPref("video"));
  const setModelId = (id: string) => {
    setModelIdRaw(id);
    saveModelPref("video", id);
  };
  const [mode, setMode] = useState("text");
  const [prompt, setPrompt] = useState("");
  const [startImages, setStartImages] = useState<string[]>([]);
  const [lastImages, setLastImages] = useState<string[]>([]);
  const [refImages, setRefImages] = useState<string[]>([]);
  const [refVideos, setRefVideos] = useState<string[]>([]);
  const [refAudios, setRefAudios] = useState<string[]>([]);
  const [duration, setDuration] = useState(5);
  const [aspect, setAspect] = useState("16:9");
  const [resolution, setResolution] = useState("auto");
  const [resolutionCustom, setResolutionCustom] = useState("");
  const [promptOptimizer, setPromptOptimizer] = useState(true);

  const [improving, setImproving] = useState(false);
  const [currentJob, setCurrentJob] = useState<Job | null>(null);
  const [generating, setGenerating] = useState(false);
  const [history, setHistory] = useState<Job[]>([]);

  const modelInfo = catalog?.video.find((m) => m.id === modelId) ?? null;
  const modelLabel = modelInfo?.label ?? currentJob?.model ?? "";

  useEffect(() => {
    void loadCatalog();
    void loadCredentials();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 默认模型 / 校验记住的偏好是否仍存在
  useEffect(() => {
    if (catalog?.video.length && !catalog.video.some((m) => m.id === modelId)) {
      switchModel(catalog.video[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, modelId]);

  // 接收从 Prompt / Image 页发来的内容
  useEffect(() => {
    if (pendingVideo) {
      if (pendingVideo.prompt) setPrompt(pendingVideo.prompt);
      if (pendingVideo.mode) setMode(pendingVideo.mode);
      if (pendingVideo.mode === "image" && pendingVideo.images?.length) setStartImages(pendingVideo.images);
      if (pendingVideo.mode === "reference" && pendingVideo.images?.length) setRefImages(pendingVideo.images);
      setPendingVideo(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingVideo]);

  // Agent 修改 Prompt
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.type === "update_prompt" && typeof detail.value === "string") {
        setPrompt(detail.value);
        toast.success("AI Assistant 已更新视频 Prompt");
      }
    };
    window.addEventListener("agent:action", handler);
    return () => window.removeEventListener("agent:action", handler);
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      const { jobs } = await api.listJobs({ type: "video", limit: 40 });
      setHistory(jobs);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  const switchModel = (id: string) => {
    setModelId(id);
    const info = catalog?.video.find((m) => m.id === id);
    if (info) {
      const minD = Math.min(...info.durations);
      const maxD = Math.max(...info.durations);
      setDuration(Math.min(Math.max(duration, minD), maxD));
      if (!info.aspect_ratios.includes(aspect)) setAspect(info.aspect_ratios[0]);
      if (!info.resolutions.includes(resolution)) setResolution(info.resolutions[0]);
      if (info.modes?.length && !info.modes.includes(mode)) setMode(info.modes[0]);
    }
  };

  const improvePrompt = async () => {
    if (!prompt.trim()) return;
    setImproving(true);
    try {
      const target = modelId === "generic" ? "generic" : "minimax_h3";
      const provider = catalog?.video.find((m) => m.id === modelId)?.provider;
      const { prompt: improved } = await api.improvePrompt({
        prompt,
        target_model: target,
        provider,
      });
      setPrompt(improved);
      toast.success("Prompt 已优化");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setImproving(false);
    }
  };

  const generate = async () => {
    if (!modelInfo) return;
    // 本地校验素材，避免提交后才被后端 400
    if (mode === "text" && !prompt.trim()) {
      toast.error("请先填写 Prompt");
      return;
    }
    if (mode === "image" && startImages.length === 0) {
      toast.error("图生视频需要上传至少一张图片（作为参考图）");
      return;
    }
    if (mode === "first_last" && (startImages.length === 0 || lastImages.length === 0)) {
      toast.error("首尾帧模式需要同时上传首帧和尾帧图片");
      return;
    }
    if (mode === "reference" && refImages.length + refVideos.length + refAudios.length === 0) {
      toast.error("参考模式需要至少上传一张参考图/视频/音频");
      return;
    }
    // AutoDL 工作流契约校验（catalog 下发；非 AutoDL 模型无这些字段，自动跳过）
    if (modelInfo.provider === "autodl" && modelInfo.requires_prompt !== false && !prompt.trim()) {
      toast.error("该工作流必须填写视频 Prompt");
      return;
    }
    if (mode === "image" || mode === "reference") {
      const minImages = modelInfo.min_images ?? 0;
      const maxImages = modelInfo.max_images;
      const imageCount = mode === "image" ? startImages.length : refImages.length;
      if (imageCount < minImages) {
        toast.error(`该工作流必须上传至少 ${minImages} 张参考图（音频/视频不能替代）`);
        return;
      }
      if (maxImages !== undefined && imageCount > maxImages) {
        toast.error(
          maxImages === 0
            ? "该工作流为纯文生视频，不支持参考图"
            : `该工作流最多支持 ${maxImages} 张参考图（当前 ${imageCount} 张）`,
        );
        return;
      }
      if (mode === "reference") {
        if (refVideos.length > 0 && modelInfo.accepts_videos === false) {
          toast.error("该工作流不支持参考视频，请移除后重试");
          return;
        }
        if (refAudios.length > 0 && modelInfo.accepts_audios === false) {
          toast.error("该工作流不支持参考音频，请移除后重试");
          return;
        }
        if (modelInfo.max_audios !== undefined && refAudios.length > modelInfo.max_audios) {
          toast.error(`该工作流最多支持 ${modelInfo.max_audios} 段参考音频（当前 ${refAudios.length} 段）`);
          return;
        }
      }
    }
    const images =
      mode === "image"
        ? startImages
        : mode === "first_last"
          ? startImages.slice(0, 1)
          : mode === "reference"
            ? refImages
            : [];
    const lastImagesUsed = mode === "first_last" ? lastImages.slice(0, 1) : [];
    const videos = mode === "reference" ? refVideos : [];
    const audios = mode === "reference" ? refAudios : [];
    setGenerating(true);
    setCurrentJob(null);
    try {
      const { job } = await api.generateVideo({
        provider: modelInfo.provider,
        model: modelId,
        mode,
        prompt,
        duration,
        aspect_ratio: aspect,
        resolution: resolutionCustom.trim() || (resolution !== "auto" ? resolution : undefined),
        images,
        last_images: lastImagesUsed,
        videos,
        audios,
        options: { prompt_optimizer: promptOptimizer },
      });
      setCurrentJob(job);
      const finalJob = await pollJob(job.id, (j) => setCurrentJob({ ...j }));
      if (finalJob.status === "completed") {
        toast.success("视频生成完成");
      } else {
        toast.error(finalJob.error || "视频生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setGenerating(false);
      void refreshHistory();
    }
  };

  const deleteJob = async (id: string) => {
    try {
      await api.deleteJob(id);
      setHistory((h) => h.filter((j) => j.id !== id));
      if (currentJob?.id === id) setCurrentJob(null);
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      <VideoSettings
        modelInfo={modelInfo}
        modelId={modelId}
        onModelChange={switchModel}
        mode={mode}
        onModeChange={setMode}
        prompt={prompt}
        onPromptChange={setPrompt}
        onImprovePrompt={() => void improvePrompt()}
        improving={improving}
        startImages={startImages}
        onAddStartImages={(urls) => setStartImages((prev) => [...prev, ...urls])}
        onRemoveStartImage={(url) => setStartImages((prev) => prev.filter((u) => u !== url))}
        onSetFirstFrame={(url) => setStartImages([url])}
        onClearFirstFrame={() => setStartImages([])}
        lastImages={lastImages}
        onSetLastFrame={(url) => setLastImages([url])}
        onRemoveLastFrame={() => setLastImages([])}
        refImages={refImages}
        onAddRefImages={(urls) => setRefImages((prev) => [...prev, ...urls])}
        onRemoveRefImage={(url) => setRefImages((prev) => prev.filter((u) => u !== url))}
        refVideos={refVideos}
        onAddRefVideos={(urls) => setRefVideos((prev) => [...prev, ...urls])}
        onRemoveRefVideo={(url) => setRefVideos((prev) => prev.filter((u) => u !== url))}
        refAudios={refAudios}
        onAddRefAudios={(urls) => setRefAudios((prev) => [...prev, ...urls])}
        onRemoveRefAudio={(url) => setRefAudios((prev) => prev.filter((u) => u !== url))}
        duration={duration}
        onDurationChange={setDuration}
        aspect={aspect}
        onAspectChange={setAspect}
        resolution={resolution}
        onResolutionChange={setResolution}
        resolutionCustom={resolutionCustom}
        onResolutionCustomChange={setResolutionCustom}
        promptOptimizer={promptOptimizer}
        onPromptOptimizerChange={setPromptOptimizer}
        onGenerate={() => void generate()}
        generating={generating}
      />
      <VideoPreview job={currentJob} aspect={aspect} modelLabel={modelLabel} />
      <VideoHistory
        jobs={history}
        selectedId={currentJob?.id ?? null}
        onSelect={(job) => {
          setCurrentJob(job);
          const jAspect = (job.params?.aspect_ratio as string) || aspect;
          setAspect(jAspect);
        }}
        onDelete={(id) => void deleteJob(id)}
        onRefresh={() => void refreshHistory()}
      />
    </div>
  );
}
