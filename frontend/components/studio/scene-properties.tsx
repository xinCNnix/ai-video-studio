"use client";

import { useEffect, useState } from "react";
import { AudioLines, FileVideo, Loader2, Sparkles, Trash2, Wand2, X } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl, pollJob } from "@/lib/api";
import { getRecent, loadModelPref, pushRecent, saveModelPref } from "@/lib/model-prefs";
import { SearchableSelect } from "@/components/common/searchable-select";
import { FileDropzone } from "@/components/common/file-dropzone";
import { useAppStore } from "@/stores/app-store";
import { useStudioStore } from "@/stores/studio-store";
import type { Scene } from "@/lib/types";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { GenerationButton } from "@/components/common/generation-button";
import { AudioPlayer } from "@/components/voice/audio-player";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function SceneProperties() {
  const catalog = useAppStore((s) => s.catalog);
  const project = useStudioStore((s) => s.project);
  const currentSceneId = useStudioStore((s) => s.currentSceneId);
  const updateScene = useStudioStore((s) => s.updateScene);

  const scene: Scene | undefined = project?.scenes.find((s) => s.id === currentSceneId);

  // 各 tab 本地状态
  const [promptBusy, setPromptBusy] = useState(false);
  const [imgUploadBusy, setImgUploadBusy] = useState(false);
  const [imgBusy, setImgBusy] = useState(false);
  const [imgPrompt, setImgPrompt] = useState("");
  const [imgRefs, setImgRefs] = useState<string[]>([]);
  const [voiceModel, setVoiceModel] = useState("");
  const [voiceId, setVoiceId] = useState(() => loadModelPref("voice"));
  const [recentVoices, setRecentVoices] = useState<string[]>(() => getRecent("voice"));
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [videoModel, setVideoModel] = useState("");
  const [videoDuration, setVideoDuration] = useState(5);
  const [videoBusy, setVideoBusy] = useState(false);
  const [refUploadBusy, setRefUploadBusy] = useState(false);

  // 切换场景 / 旁白校准改时长时同步滑块，避免用旧值覆盖 scene.duration
  useEffect(() => {
    if (scene?.duration) setVideoDuration(scene.duration);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene?.id, scene?.duration]);

  if (!scene) return null;

  const ttsModel = voiceModel;
  const voices = catalog?.tts_voices ?? [];

  const selectVoice = (v: string) => {
    setVoiceId(v);
    saveModelPref("voice", v);
    pushRecent("voice", v);
    setRecentVoices(getRecent("voice"));
  };
  const vidModel = videoModel || catalog?.video[0]?.id || "MiniMax-H3";
  const vidInfo = catalog?.video.find((m) => m.id === vidModel);
  const refImages = (scene.ref_images ?? []).filter((u) => u !== scene.image);
  const refVideos = scene.ref_videos ?? [];
  const refAudios = scene.ref_audios ?? [];
  const hasRefs = refImages.length + refVideos.length + refAudios.length > 0;

  // 删除首帧只删关键帧本身（参考图不受影响）；点击参考图可显式设为首帧
  const removeKeyframe = () => updateScene(scene.id, { image: null });
  const promoteRef = (url: string) => {
    const idx = refImages.indexOf(url);
    if (idx < 0 || !scene.image) return;
    const next = [...refImages];
    next[idx] = scene.image;
    updateScene(scene.id, { image: url, ref_images: next });
  };

  // 上传本地图片作为当前场景的参考图（可多张）：只作参考，不占用首帧；
  // 首帧仅由 Generate Image 生成的关键帧（scene.image）决定
  const uploadSceneImage = async (files: File[]) => {
    const valid = files.filter((f) => f.type.startsWith("image/"));
    if (valid.length !== files.length) toast.error("存在非图片文件，已跳过");
    if (!valid.length) return;
    setImgUploadBusy(true);
    try {
      const urls: string[] = [];
      for (const file of valid) urls.push((await api.upload(file, "image")).url);
      updateScene(scene.id, { ref_images: [...(scene.ref_images ?? []), ...urls] });
      toast.success(urls.length > 1 ? `已上传 ${urls.length} 张参考图` : "参考图已上传");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setImgUploadBusy(false);
    }
  };

  // 上传参考视频/音频：随场景保存，生成视频时一并作为参考素材（Ref2VA）
  const uploadSceneRefs = async (files: File[], kind: "video" | "audio") => {
    const valid = files.filter((f) => f.type.startsWith(`${kind}/`));
    if (valid.length !== files.length) toast.error(`存在非${kind === "video" ? "视频" : "音频"}文件，已跳过`);
    if (!valid.length) return;
    setRefUploadBusy(true);
    try {
      const urls: string[] = [];
      for (const file of valid) urls.push((await api.upload(file, kind)).url);
      if (kind === "video") {
        updateScene(scene.id, { ref_videos: [...(scene.ref_videos ?? []), ...urls] });
        toast.success("参考视频已上传，生成视频时会随场景图片一并传给模型");
      } else {
        updateScene(scene.id, { ref_audios: [...(scene.ref_audios ?? []), ...urls] });
        toast.success("参考音频已上传，生成视频时会随场景图片一并传给模型");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setRefUploadBusy(false);
    }
  };

  const removeSceneRef = (kind: "video" | "audio", url: string) => {
    if (kind === "video") updateScene(scene.id, { ref_videos: (scene.ref_videos ?? []).filter((u) => u !== url) });
    else updateScene(scene.id, { ref_audios: (scene.ref_audios ?? []).filter((u) => u !== url) });
  };

  const generatePrompt = async () => {
    if (!scene.description.trim()) {
      toast.error("请先填写 Scene Description");
      return;
    }
    setPromptBusy(true);
    try {
      const res = await api.generatePrompt({
        description: scene.description,
        mode: "text_to_video",
        target_model: "minimax_h3",
      });
      updateScene(scene.id, { prompt: res.prompt });
      toast.success("已生成 H3 Prompt");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setPromptBusy(false);
    }
  };

  const generateImage = async () => {
    const model = catalog?.image[0];
    if (!model || !imgPrompt.trim()) {
      toast.error("请填写图片 Prompt");
      return;
    }
    setImgBusy(true);
    try {
      const { job } = await api.generateImage({
        provider: model.provider,
        model: model.id,
        prompt: imgPrompt,
        aspect_ratio: project?.aspect_ratio ?? "16:9",
      });
      const finalJob = await pollJob(job.id, () => undefined);
      if (finalJob.status === "completed" && finalJob.output) {
        updateScene(scene.id, { image: finalJob.output });
        toast.success("场景图片已更新");
      } else {
        toast.error(finalJob.error || "图片生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setImgBusy(false);
    }
  };

  const generateVoice = async () => {
    if (!scene.narration.trim()) {
      toast.error("请填写旁白文案");
      return;
    }
    if (!voiceId) {
      toast.error("请先选择或输入音色（可在设置中添加 MiniMax Key 自动发现）");
      return;
    }
    if (!ttsModel) {
      toast.error("请选择或输入语音模型 ID（可在设置里保存为自定义模型）");
      return;
    }
    setVoiceBusy(true);
    try {
      const { job } = await api.generateVoice({
        provider: "minimax",
        model: ttsModel,
        text: scene.narration,
        voice: voiceId,
        speed: 1.0,
        emotion: "neutral",
      });
      const finalJob = await pollJob(job.id, () => undefined);
      if (finalJob.status === "completed" && finalJob.output) {
        // 按旁白实际时长校准分镜时长（与 Agent 路径一致）
        const audioSec = (finalJob as { duration_seconds?: number }).duration_seconds;
        const newDur = audioSec ? Math.min(60, Math.ceil(audioSec)) : null;
        if (newDur && newDur !== scene.duration) {
          updateScene(scene.id, { voice: finalJob.output, duration: newDur });
          toast.success(`旁白 ${audioSec!.toFixed(1)}s，分镜时长已从 ${scene.duration}s 校准为 ${newDur}s${newDur > 15 ? "（超过视频上限 15s，建议拆镜）" : ""}`);
        } else {
          updateScene(scene.id, { voice: finalJob.output });
          toast.success("场景旁白已更新");
        }
      } else {
        toast.error(finalJob.error || "语音生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setVoiceBusy(false);
    }
  };

  const generateVideo = async () => {
    if (!vidInfo) return;
    if (!scene.prompt.trim()) {
      toast.error("请先生成或填写 Prompt");
      return;
    }
    const supportsRef = !vidInfo.modes?.length || vidInfo.modes.includes("reference");
    if (hasRefs && !supportsRef) {
      toast.error(`模型「${vidInfo.label}」不支持参考模式，请换用支持参考的模型或删除参考素材`);
      return;
    }
    // MiniMax 官方 V2 接口禁止首帧与参考素材混传：带参考素材时整镜走参考模式，
    // 关键帧也作为参考图之一；仅有关键帧时才是纯 i2v
    const mode =
      scene.image
        ? hasRefs
          ? "reference"
          : "image"
        : hasRefs
          ? "reference"
          : "text";
    setVideoBusy(true);
    try {
      const { job } = await api.generateVideo({
        provider: vidInfo.provider,
        model: vidModel,
        mode,
        prompt: scene.prompt,
        duration: videoDuration,
        aspect_ratio: project?.aspect_ratio ?? "16:9",
        images: [...(scene.image ? [scene.image] : []), ...refImages],
        videos: refVideos,
        audios: refAudios,
        options: {},
      });
      const finalJob = await pollJob(job.id, () => undefined, 3000);
      if (finalJob.status === "completed" && finalJob.output) {
        updateScene(scene.id, { video: finalJob.output, duration: videoDuration });
        toast.success("场景视频已更新");
      } else {
        toast.error(finalJob.error || "视频生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setVideoBusy(false);
    }
  };

  return (
    <div className="flex h-full w-[320px] shrink-0 flex-col border-l border-border bg-card/40">
      <div className="flex-1 overflow-y-auto px-4 py-4">
        <Tabs defaultValue="prompt">
          <TabsList>
            <TabsTrigger value="prompt">Prompt</TabsTrigger>
            <TabsTrigger value="image">Image</TabsTrigger>
            <TabsTrigger value="voice">Voice</TabsTrigger>
            <TabsTrigger value="video">Video</TabsTrigger>
          </TabsList>

          {/* Prompt Tab */}
          <TabsContent value="prompt" className="space-y-3">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>Scene Description</Label>
                {scene.description && (
                  <button
                    type="button"
                    onClick={() => updateScene(scene.id, { description: "" })}
                    className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    title="清空描述"
                  >
                    <Trash2 className="size-3" />
                    清空
                  </button>
                )}
              </div>
              <Textarea
                value={scene.description}
                onChange={(e) => updateScene(scene.id, { description: e.target.value })}
                placeholder="例如：医生坐在诊室中向女孩解释白斑治疗误区。"
                className="min-h-[90px] resize-none"
              />
            </div>
            <GenerationButton loading={promptBusy} loadingLabel="Writing..." size="sm" onClick={() => void generatePrompt()}>
              <Sparkles className="size-3.5" />
              Generate Prompt
            </GenerationButton>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>H3 Prompt</Label>
                {scene.prompt && (
                  <button
                    type="button"
                    onClick={() => updateScene(scene.id, { prompt: "" })}
                    className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    title="清空提示词"
                  >
                    <Trash2 className="size-3" />
                    清空
                  </button>
                )}
              </div>
              <Textarea
                value={scene.prompt}
                onChange={(e) => updateScene(scene.id, { prompt: e.target.value })}
                placeholder="生成的结构化 Prompt 会出现在这里，可手动编辑"
                className="min-h-[180px] resize-none text-xs leading-relaxed"
              />
            </div>
          </TabsContent>

          {/* Image Tab */}
          <TabsContent value="image" className="space-y-3">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>参考图（可多张）</Label>
                {(scene.image || refImages.length > 0) && (
                  <span className="text-[10px] text-muted-foreground">
                    共 {(scene.image ? 1 : 0) + refImages.length} 张
                  </span>
                )}
              </div>
              {(scene.image || refImages.length > 0) && (
                <div className="flex flex-wrap gap-2">
                  {scene.image && (
                    <div className="group relative size-16 overflow-hidden rounded-lg border border-primary/60 bg-black/40">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={fileUrl(scene.image)} alt="" className="size-full object-cover" />
                      <span
                        className="accent-gradient absolute left-0.5 top-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-bold text-white"
                        title="首帧"
                      >
                        首
                      </span>
                      <button
                        type="button"
                        onClick={removeKeyframe}
                        className="absolute right-0.5 top-0.5 hidden rounded-full bg-black/70 p-0.5 text-white group-hover:block cursor-pointer"
                        title="移除首帧（若有参考图则顺延补位）"
                      >
                        <X className="size-3" />
                      </button>
                    </div>
                  )}
                  {refImages.map((u, i) => (
                    <div
                      key={u}
                      role="button"
                      tabIndex={0}
                      onClick={() => promoteRef(u)}
                      onKeyDown={(e) => e.key === "Enter" && promoteRef(u)}
                      title="点击设为首帧"
                      className="group relative size-16 cursor-pointer overflow-hidden rounded-lg border border-border bg-black/40"
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={fileUrl(u)} alt="" className="size-full object-cover" />
                      <span className="absolute left-0.5 top-0.5 flex size-4 items-center justify-center rounded-full bg-black/70 text-[9px] font-bold text-white">
                        {i + 1}
                      </span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          updateScene(scene.id, { ref_images: refImages.filter((x) => x !== u) });
                        }}
                        className="absolute right-0.5 top-0.5 hidden rounded-full bg-black/70 p-0.5 text-white group-hover:block cursor-pointer"
                        title="移除参考图"
                      >
                        <X className="size-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <FileDropzone
                accept="image/*"
                multiple
                hint="把参考图拖到这里，可一次多张，或"
                className={imgUploadBusy ? "pointer-events-none opacity-60" : "py-4"}
                onFiles={(files) => void uploadSceneImage(files)}
              />
              <p className="text-[10px] leading-relaxed text-muted-foreground">
                参考图只作风格/主体参考（可多张），不会占用首帧；首帧由 Generate Image 生成或已有的场景关键帧决定，带「首」角标。
                官方接口不允许首帧与参考素材混传：同时有关键帧和参考素材时整镜按参考模式生成，关键帧也作参考图之一。
              </p>
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>Prompt</Label>
                {imgPrompt && (
                  <button
                    type="button"
                    onClick={() => setImgPrompt("")}
                    className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    title="清空提示词"
                  >
                    <Trash2 className="size-3" />
                    清空
                  </button>
                )}
              </div>
              <Textarea
                value={imgPrompt}
                onChange={(e) => setImgPrompt(e.target.value)}
                placeholder="Describe the scene image…"
                className="min-h-[90px] resize-none"
              />
            </div>
            <GenerationButton loading={imgBusy} loadingLabel="Generating..." size="sm" onClick={() => void generateImage()}>
              Generate Image
            </GenerationButton>
            <p className="text-[10px] leading-relaxed text-muted-foreground">
              生成的图片会作为该场景首帧，出现在上方参考图列表中。
            </p>
          </TabsContent>

          {/* Voice Tab */}
          <TabsContent value="voice" className="space-y-3">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>Narration</Label>
                {scene.narration && (
                  <button
                    type="button"
                    onClick={() => updateScene(scene.id, { narration: "" })}
                    className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    title="清空旁白"
                  >
                    <Trash2 className="size-3" />
                    清空
                  </button>
                )}
              </div>
              <Textarea
                value={scene.narration}
                onChange={(e) => updateScene(scene.id, { narration: e.target.value })}
                placeholder="输入这一幕的旁白文案……"
                className="min-h-[90px] resize-none"
              />
            </div>
            <div className="space-y-1.5">
              <Label>Voice</Label>
              {/* 音色设计模型：Voice 是自由描述输入，固定输入框，不随音色列表加载变形 */}
              {voiceModel.includes("voicedesign") ? (
                <input
                  value={voiceId}
                  onChange={(e) => setVoiceId(e.target.value)}
                  placeholder="输入音色描述，如：低沉沙哑的中年男声，像深夜电台主持人"
                  className="h-9 w-full rounded-lg border border-border bg-secondary/50 px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              ) : voices.length > 0 ? (
                <SearchableSelect
                  items={voices.map((v) => ({
                    value: v.id,
                    label: v.label === v.id ? v.id : `${v.label}（${v.id}）`,
                  }))}
                  value={voiceId}
                  onChange={selectVoice}
                  searchPlaceholder="搜索或输入音色 ID，回车确认…"
                  recentValues={recentVoices}
                  emptyText="无匹配音色"
                  allowCustom
                  customLabelPrefix="使用自定义音色 ID"
                />
              ) : (
                <input
                  value={voiceId}
                  onChange={(e) => setVoiceId(e.target.value)}
                  placeholder="输入音色 ID"
                  className="h-9 w-full rounded-lg border border-border bg-secondary/50 px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              )}
            </div>
            <div className="space-y-1.5">
              <Label>Voice Model</Label>
              {(catalog?.tts.length ?? 0) > 0 ? (
                <Select value={voiceModel || undefined} onValueChange={setVoiceModel}>
                  <SelectTrigger>
                    <SelectValue placeholder="选择语音模型" />
                  </SelectTrigger>
                  <SelectContent>
                    {(catalog?.tts ?? []).map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <input
                  value={voiceModel}
                  onChange={(e) => setVoiceModel(e.target.value)}
                  placeholder="输入语音模型 ID"
                  className="h-9 w-full rounded-lg border border-border bg-secondary/50 px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              )}
            </div>
            <GenerationButton loading={voiceBusy} loadingLabel="Generating..." size="sm" onClick={() => void generateVoice()}>
              Generate
            </GenerationButton>
            {scene.voice && <AudioPlayer src={scene.voice} />}
          </TabsContent>

          {/* Video Tab */}
          <TabsContent value="video" className="space-y-3">
            <div className="space-y-1.5">
              <Label>Model</Label>
              <Select value={vidModel} onValueChange={setVideoModel}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(catalog?.video ?? []).map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>Prompt</Label>
                {scene.prompt ? (
                  <button
                    type="button"
                    onClick={() => updateScene(scene.id, { prompt: "" })}
                    className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                    title="清空提示词（与 Prompt Tab 是同一份内容）"
                  >
                    <Trash2 className="size-3" />
                    清空
                  </button>
                ) : (
                  <span className="inline-flex items-center gap-1 text-[10px] text-primary">
                    <Wand2 className="size-3" /> 来自 Prompt Tab
                  </span>
                )}
              </div>
              <div className="max-h-32 overflow-y-auto rounded-lg border border-border bg-secondary/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                {scene.prompt || "尚未生成 Prompt，请先到 Prompt Tab 生成"}
              </div>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-border bg-secondary/40 px-3 py-2">
              <span className="text-xs text-muted-foreground">
                {scene.image && hasRefs
                  ? "参考模式：关键帧与参考素材都作参考（官方不支持首帧+参考混传）"
                  : refImages.length > 0
                    ? `${refImages.length} 张参考图（纯参考模式，不作为首帧）`
                    : hasRefs
                      ? "参考模式：图/音/视频素材作参考"
                      : scene.image
                        ? "使用场景图片作为首帧（i2v）"
                        : "纯文生视频"}
              </span>
              <input
                type="checkbox"
                checked={!!scene.image}
                disabled
                className="size-4 accent-[#7c5cff]"
              />
            </div>

            {/* 参考视频 / 参考音频：上传后随场景图片一并传给模型（参考模式） */}
            <div className="space-y-1.5">
              <Label className="flex items-center gap-1.5">
                <FileVideo className="size-3.5 text-sky-300" />
                参考视频（可多个）
              </Label>
              {(scene.ref_videos ?? []).length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {(scene.ref_videos ?? []).map((u, i) => (
                    <RefThumb key={u} url={u} index={i + 1} onRemove={() => removeSceneRef("video", u)} />
                  ))}
                </div>
              )}
              <FileDropzone
                accept="video/*"
                multiple
                hint="把参考视频拖到这里，或"
                className={refUploadBusy ? "pointer-events-none opacity-60" : "py-4"}
                onFiles={(files) => void uploadSceneRefs(files, "video")}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="flex items-center gap-1.5">
                <AudioLines className="size-3.5 text-emerald-300" />
                参考音频（可多个）
              </Label>
              <div className="space-y-1.5">
                {(scene.ref_audios ?? []).map((u, i) => (
                  <div
                    key={u}
                    className="flex items-center justify-between rounded-lg border border-border bg-secondary/40 px-3 py-2 text-xs"
                  >
                    <span className="flex min-w-0 items-center gap-2 text-foreground/80">
                      <span className="accent-gradient flex size-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold text-white">
                        {i + 1}
                      </span>
                      <span className="truncate">{decodeURIComponent(u.split("/").pop() || "参考音频")}</span>
                    </span>
                    <button
                      type="button"
                      onClick={() => removeSceneRef("audio", u)}
                      className="shrink-0 text-muted-foreground hover:text-red-400 cursor-pointer"
                    >
                      <X className="size-3.5" />
                    </button>
                  </div>
                ))}
              </div>
              <FileDropzone
                accept="audio/*"
                multiple
                hint="把参考音频拖到这里，或"
                className={refUploadBusy ? "pointer-events-none opacity-60" : "py-4"}
                onFiles={(files) => void uploadSceneRefs(files, "audio")}
              />
              <p className="text-[10px] leading-relaxed text-muted-foreground">
                上传参考视频/音频后，Generate Video 会自动切换为参考模式：场景图片与这些素材一并发给模型（需选用支持
                Reference 的模型）。
              </p>
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label>
                  Duration（
                  {vidInfo?.durations.length ? Math.min(...vidInfo.durations) : 1}-
                  {vidInfo?.durations.length ? Math.max(...vidInfo.durations) : 15}s）
                </Label>
                <span className="rounded-md bg-secondary px-2 py-0.5 text-xs font-medium text-foreground">
                  {videoDuration}s
                </span>
              </div>
              <Slider
                value={[videoDuration]}
                onValueChange={(v) => setVideoDuration(v[0])}
                min={vidInfo?.durations.length ? Math.min(...vidInfo.durations) : 1}
                max={vidInfo?.durations.length ? Math.max(...vidInfo.durations) : 15}
                step={1}
              />
            </div>
            <GenerationButton loading={videoBusy} loadingLabel="Generating..." size="sm" onClick={() => void generateVideo()}>
              Generate Video
            </GenerationButton>
            {videoBusy && (
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                <Loader2 className="size-3 animate-spin" /> 提交模型后自动轮询，一般需要 1-5 分钟
              </div>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

/** 参考视频缩略图：序号标识 + 悬停删除 */
function RefThumb({ url, index, onRemove }: { url: string; index: number; onRemove: () => void }) {
  return (
    <div className="group relative size-16 overflow-hidden rounded-lg border border-border bg-black/40">
      <video src={fileUrl(url)} muted preload="metadata" className="size-full object-cover" />
      <span className="accent-gradient absolute left-0.5 top-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-bold text-white">
        {index}
      </span>
      <button
        type="button"
        onClick={onRemove}
        className="absolute right-0.5 top-0.5 hidden rounded-full bg-black/70 p-0.5 text-white group-hover:block cursor-pointer"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}
