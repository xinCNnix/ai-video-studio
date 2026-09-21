"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AudioLines, Download, Send, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl, pollJob, API_BASE } from "@/lib/api";
import { loadModelPref, pushRecent, saveModelPref } from "@/lib/model-prefs";
import { useAppStore } from "@/stores/app-store";
import type { Job, VoiceAsset } from "@/lib/types";
import { VoiceEditor, voiceModeOf } from "@/components/voice/voice-editor";
import { AudioPlayer } from "@/components/voice/audio-player";
import { EmptyState } from "@/components/common/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn, fmtDate, fmtSeconds } from "@/lib/utils";

export default function VoicePage() {
  const router = useRouter();
  const catalog = useAppStore((s) => s.catalog);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const loadDiscovered = useAppStore((s) => s.loadDiscovered);
  const setPendingVoice = useAppStore((s) => s.setPendingVoice);

  const [modelId, setModelIdRaw] = useState(() => loadModelPref("tts"));
  const setModelId = (id: string) => {
    setModelIdRaw(id);
    saveModelPref("tts", id);
  };
  const [text, setText] = useState("");
  const [voice, setVoice] = useState(() => loadModelPref("voice"));
  const [speed, setSpeed] = useState(1.0);
  const [emotion, setEmotion] = useState("neutral");

  const [currentJob, setCurrentJob] = useState<Job | null>(null);
  const [generating, setGenerating] = useState(false);
  const [history, setHistory] = useState<Job[]>([]);

  // 音色资产（固化音色）：设计满意后保存，之后按名稳定复用
  const [assets, setAssets] = useState<VoiceAsset[]>([]);
  const [assetName, setAssetName] = useState("");
  const [savingAsset, setSavingAsset] = useState(false);

  const loadAssets = useCallback(async () => {
    try {
      const { assets } = await api.listVoiceAssets();
      setAssets(assets);
    } catch {
      /* 后端未启动时静默 */
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    void loadCredentials();
    void loadAssets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (catalog?.tts.length && !catalog.tts.some((m) => m.id === modelId)) {
      setModelId(catalog.tts[0].id);
    }
    if (!voice && catalog?.tts_voices.length) setVoice(catalog.tts_voices[0].id);
  }, [catalog, modelId, voice]);

  const loadHistory = useCallback(async () => {
    try {
      const { jobs } = await api.listJobs({ type: "tts", status: "completed", limit: 50 });
      setHistory(jobs);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // 提交语义（下拉选中 / 点资产 / 点最近描述）：写入“最近使用”
  const commitVoice = (v: string) => {
    setVoice(v);
    saveModelPref("voice", v);
    pushRecent("voice", v);
  };
  // 打字语义（音色描述/手动 ID 逐字输入）：只更新值与偏好，避免每个字符都进“最近使用”
  const inputVoice = (v: string) => {
    setVoice(v);
    saveModelPref("voice", v);
  };

  const isDesignJob = !!currentJob && String(currentJob.model).includes("voicedesign");
  // 最新产物精确时长（ffprobe 实测）：播放器自带的 mm:ss 是取整值且需加载元数据后才出现
  const latestSeconds = fmtSeconds(currentJob?.duration_seconds);

  // 克隆音色上传固化成功：刷新列表并直接选中新音色
  const onCloneCreated = async (asset: VoiceAsset) => {
    commitVoice(asset.id);
    await loadAssets();
    void loadDiscovered();
  };

  // MiniMax 永久音色设计成功：只刷新列表，不抢占 Voice 描述输入（可继续调整重新设计）
  const onDesignCreated = async (_asset: VoiceAsset) => {
    await loadAssets();
    void loadDiscovered();
  };

  const saveAsAsset = async () => {
    if (!currentJob?.output) return;
    const name = assetName.trim();
    if (!name) {
      toast.error("请先给音色起个名字（如：深夜电台男声）");
      return;
    }
    setSavingAsset(true);
    try {
      await api.saveVoiceAsset({
        name,
        kind: "clone",
        // 描述留档：取该次任务实际使用的 voice（voicedesign 时即音色描述）
        desc: String(currentJob.params?.voice ?? ""),
        sample_from: currentJob.output,
      });
      toast.success(`音色「${name}」已固化，之后在音色列表按名选用即可稳定复用`);
      setAssetName("");
      await loadAssets();
      void loadDiscovered();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSavingAsset(false);
    }
  };

  const deleteAsset = async (asset: VoiceAsset) => {
    try {
      await api.deleteVoiceAsset(asset.id);
      toast.success(`音色「${asset.name}」已删除`);
      await loadAssets();
      void loadDiscovered();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const generate = async () => {
    if (!modelId.trim()) {
      toast.error("请选择或输入语音模型 ID（可在设置里保存为自定义模型）");
      return;
    }
    const isHiggs = modelId.toLowerCase().includes("higgs");
    // 声音克隆（非 higgs）：必须选中带样本的克隆音色（即用户上传固化的声音）
    if (!isHiggs && voiceModeOf(modelId) === "clone") {
      const asset = assets.find((a) => a.id === voice);
      if (!asset || !asset.has_sample) {
        toast.error("声音克隆需要先上传声音样本：保存为克隆音色并选中后，再用它生成语音");
        return;
      }
    }
    // Higgs：音色命中资产时走克隆（样本+逐字稿），缺逐字稿会在推理时报错，提前拦下
    if (isHiggs) {
      const asset = assets.find((a) => a.id === voice);
      if (asset && !(asset.ref_text || "").trim()) {
        toast.error("该音色缺少样本逐字稿（Higgs 克隆必填）：请删除后重新上传，并填写样本念的文字");
        return;
      }
    }
    const model = catalog?.tts.find((m) => m.id === modelId);
    // 目录未命中时按模型 ID 前缀兜底（mimo-* 是小米私有协议）
    const provider =
      model?.provider ?? (modelId.toLowerCase().startsWith("mimo-") ? "xiaomi" : "minimax");
    setGenerating(true);
    try {
      const { job } = await api.generateVoice({
        provider,
        model: modelId,
        text,
        // Higgs 音色留空 = 零样本内置音色；其余模型必须显式音色
        voice: isHiggs && !voice.trim() ? "default" : voice,
        speed,
        emotion,
      });
      const finalJob = await pollJob(job.id, (j) => setCurrentJob({ ...j }));
      if (finalJob.status === "completed") {
        setCurrentJob(finalJob);
        toast.success("语音生成完成");
      } else {
        toast.error(finalJob.error || "语音生成失败");
      }
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setGenerating(false);
      void loadHistory();
    }
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      <VoiceEditor
        modelId={modelId}
        onModelChange={setModelId}
        text={text}
        onTextChange={setText}
        voice={voice}
        onVoiceChange={commitVoice}
        onVoiceInput={inputVoice}
        speed={speed}
        onSpeedChange={setSpeed}
        emotion={emotion}
        onEmotionChange={setEmotion}
        onGenerate={() => void generate()}
        generating={generating}
        assets={assets}
        onDeleteAsset={(a) => void deleteAsset(a)}
        onUseAsset={(a) => commitVoice(a.id)}
        onCloneCreated={(a) => void onCloneCreated(a)}
        onDesignCreated={(a) => void onDesignCreated(a)}
      />

      {/* 中央：文案编辑 + 大型播放器，整体居中 */}
      <div className="flex min-w-0 flex-1 items-start justify-center overflow-y-auto bg-[#0b0c10] p-8">
        <div className="flex w-full max-w-2xl flex-col gap-6">
          <div className="space-y-2">
            <h2 className="text-sm font-semibold">Text</h2>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="输入需要转成语音的旁白文案……"
              className="min-h-[220px] w-full rounded-xl border border-border bg-card/60 p-4 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {currentJob?.status === "completed" && currentJob.output ? (
            <>
              <AudioPlayer
                src={currentJob.output}
                title={`Latest generation${latestSeconds ? ` · ${latestSeconds}` : ""}`}
                onSendToStudio={() => {
                  setPendingVoice({ url: currentJob.output ?? "", text: String(currentJob.params?.text ?? "") });
                  router.push("/studio");
                }}
              />
              {isDesignJob && (
                <div className="rounded-xl border border-border bg-secondary/30 p-4">
                  <p className="mb-2 text-xs text-muted-foreground">
                    这条是音色设计（voicedesign）的结果——同一段描述再生成会得到不同嗓音。
                    满意就把它固化保存，之后按名复用不会再变。
                  </p>
                  <div className="flex gap-2">
                    <Input
                      value={assetName}
                      onChange={(e) => setAssetName(e.target.value)}
                      placeholder="音色名称，如：深夜电台男声"
                      className="h-9"
                    />
                    <Button size="sm" disabled={savingAsset} onClick={() => void saveAsAsset()}>
                      {savingAsset ? "保存中…" : "保存为音色"}
                    </Button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <EmptyState
              icon={AudioLines}
              title="Your audio will appear here."
              hint="生成后可试听、下载或发送到 Studio 作为场景旁白"
            />
          )}

          {history.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-xs font-medium text-muted-foreground">Recent Generations</h3>
              {/* flex 而非 grid：grid 自动列会取最宽行的整行内容宽（长文案不换行），
                  把日期/图标挤出视口；flex-col + 行内 min-w-0 才能让文案截断、图标常驻可见 */}
              <div className="flex max-h-72 flex-col gap-2 overflow-y-auto pr-1">
                {history.map((job) => (
                  <div
                    key={job.id}
                    className={cn(
                      "group flex items-center justify-between rounded-lg border px-3 py-2 text-left text-xs transition-colors",
                      currentJob?.id === job.id
                        ? "border-primary/50 bg-primary/[0.08] text-foreground"
                        : "border-border bg-secondary/40 text-muted-foreground hover:text-foreground"
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => setCurrentJob(job)}
                      className="flex min-w-0 flex-1 cursor-pointer items-center justify-between gap-3"
                    >
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="truncate">{String(job.params?.text ?? "")}</span>
                        {job.duration_seconds ? (
                          <span
                            className="shrink-0 font-semibold tabular-nums text-amber-300"
                            title="该条配音的精确时长（ffprobe 实测）"
                          >
                            {fmtSeconds(job.duration_seconds)}
                          </span>
                        ) : null}
                        {job.params?.credential ? (
                          <span
                            className="shrink-0 text-[10px] text-emerald-300/70"
                            title={`本次使用的 Key：${String(job.params.credential)}`}
                          >
                            🔑{String(job.params.credential)}
                          </span>
                        ) : null}
                      </span>
                      <span className="shrink-0">{fmtDate(job.created_at)}</span>
                    </button>
                    {job.output && (
                      <a
                        href={`${API_BASE}/api/jobs/${job.id}/download`}
                        download
                        target="_blank"
                        rel="noreferrer"
                        title="下载"
                        onClick={(e) => e.stopPropagation()}
                        className="ml-2 flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground cursor-pointer"
                      >
                        <Download className="size-3.5" />
                      </a>
                    )}
                    {job.output && (
                      <button
                        type="button"
                        title="发送到 Studio 作为场景旁白"
                        onClick={(e) => {
                          e.stopPropagation();
                          setPendingVoice({ url: job.output ?? "", text: String(job.params?.text ?? "") });
                          router.push("/studio");
                        }}
                        className="ml-1 flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-primary/10 hover:text-primary"
                      >
                        <Send className="size-3.5" />
                      </button>
                    )}
                    <button
                      type="button"
                      title="删除该记录"
                      onClick={async () => {
                        try {
                          await api.deleteJob(job.id);
                          setHistory((h) => h.filter((x) => x.id !== job.id));
                          if (currentJob?.id === job.id) setCurrentJob(null);
                        } catch (err) {
                          toast.error((err as Error).message);
                        }
                      }}
                      className="ml-1 flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-400 cursor-pointer"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
