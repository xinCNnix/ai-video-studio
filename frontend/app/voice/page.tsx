"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { AudioLines, Download, Send, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, API_BASE, pollJob } from "@/lib/api";
import { loadModelPref, pushRecent, saveModelPref } from "@/lib/model-prefs";
import { useAppStore } from "@/stores/app-store";
import type { Job, VoiceAsset } from "@/lib/types";
import {
  VoiceSidebar,
  isHiggsModel,
  voiceModeOf,
  type VoiceMode,
} from "@/components/voice/voice-editor";
import { ScriptWorkbench } from "@/components/voice/script-workbench";
import { AudioPlayer } from "@/components/voice/audio-player";
import { EmptyState } from "@/components/common/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ModelSelector } from "@/components/common/model-selector";
import { Segmented } from "@/components/common/segmented";
import { GenerationButton } from "@/components/common/generation-button";
import { cn, fmtDate, fmtSeconds } from "@/lib/utils";

export default function VoicePage() {
  const router = useRouter();
  const catalog = useAppStore((s) => s.catalog);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const loadDiscovered = useAppStore((s) => s.loadDiscovered);
  const setPendingVoice = useAppStore((s) => s.setPendingVoice);

  const [modelId, setModelIdRaw] = useState("");
  const setModelId = (id: string) => {
    setModelIdRaw(id);
    saveModelPref("tts", id);
  };
  const [text, setText] = useState("");
  const [voice, setVoice] = useState("");
  const [speed, setSpeed] = useState(1.0);
  const [emotion, setEmotion] = useState("neutral");
  // 全局默认音调/音量（左栏滑杆；片段未加 [pitch:x]/[vol:x] 标记时生效）
  const [pitch, setPitch] = useState(0);
  const [vol, setVol] = useState(1);
  const [takes, setTakes] = useState(1);

  // 模式是显式用户意图：点顶栏页签设定，换模型不重算（在设计模式切到 speech 模型
  // 被弹回配音的事故源）。仅反向同步：配音模式下手动选了 voicedesign /
  // voiceclone 专属模型时，模式跟过去避免“配音界面配专用模型”。
  const [mode, setMode] = useState<VoiceMode>(() => voiceModeOf(modelId));
  useEffect(() => {
    const inherent = voiceModeOf(modelId);
    setMode((cur) => (inherent !== "dub" && cur === "dub" ? inherent : cur));
  }, [modelId]);

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
    // 记忆的模型/音色须挂载后读 localStorage：服务端首帧没有 window，
    // 放进 useState 初始化器会让 SSR 与水合的文本不一致（Hydration failed）
    setModelIdRaw(loadModelPref("tts"));
    setVoice(loadModelPref("voice"));
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

  // 模式切换只是换模型的快捷入口：目标模式有专属模型就切过去，没有就保持不动
  // （MiniMax 永久音色 / 服务端克隆走独立接口，不依赖当前合成模型）
  const switchMode = (target: VoiceMode) => {
    if (target === mode) return;
    setMode(target);
    // 克隆音色的资产 ID（voice_*）只在克隆模式有意义；切到其他模式时清掉，
    // 避免把机器 ID 当作音色描述/预置音色 ID 发给模型
    if (target !== "clone" && voice.startsWith("voice_")) {
      inputVoice("");
    }
    // higgs：配音⇄克隆自由切换（两套界面它都用得上），模型保持不动
    if (isHiggsModel(modelId) && (target === "dub" || target === "clone")) {
      return;
    }
    // 当前模型自身就是目标模式（如从 voicedesign 模型返回配音后再进设计）：只切模式
    if (target === voiceModeOf(modelId)) {
      return;
    }
    const models = catalog?.tts ?? [];
    const lower = (id: string) => id.toLowerCase();
    let next: string | undefined;
    if (target === "clone") {
      next = models.find((m) => lower(m.id).includes("voiceclone"))?.id;
    } else if (target === "design") {
      next = models.find((m) => lower(m.id).includes("voicedesign"))?.id;
    } else {
      next = models.find(
        (m) =>
          !lower(m.id).includes("voicedesign") &&
          !lower(m.id).includes("voiceclone") &&
          !lower(m.id).includes("higgs")
      )?.id;
    }
    if (!next && target !== "dub") {
      // 目录里没有该模式的专属模型（如只配了 MiniMax speech 模型）：模型保持不动
      return;
    }
    setModelId(
      next ?? (target === "clone" ? "mimo-v2.5-tts-voiceclone" : target === "design" ? "mimo-v2.5-tts-voicedesign" : "mimo-v2.5-tts")
    );
  };

  const isHiggs = isHiggsModel(modelId);
  // 设计模式下只有 voicedesign 模型能按描述直接合成；其他模型（如 MiniMax speech）
  // 直接生成会把描述当 voice_id 发出去——需先设计永久音色或选用资产
  const voiceIsAsset = assets.some((a) => a.id === voice.trim());
  const designGenBlocked =
    mode === "design" &&
    !modelId.toLowerCase().includes("voicedesign") &&
    !voiceIsAsset;

  // 顶栏音色速览：一眼确认“用哪个嗓子念”
  const voiceChip = useMemo(() => {
    if (!voice.trim()) return isHiggs ? "内置音色" : "未选音色";
    const preset = catalog?.tts_voices.find((v) => v.id === voice);
    if (preset) return preset.label === preset.id ? preset.id : preset.label;
    const asset = assets.find((a) => a.id === voice);
    if (asset) return asset.name;
    return voice;
  }, [voice, catalog, assets, isHiggs]);

  const isDesignJob = !!currentJob && String(currentJob.model).includes("voicedesign");
  // 最新产物精确时长（ffprobe 实测）：播放器自带的 mm:ss 是取整值且需加载元数据后才出现
  const latestSeconds = fmtSeconds(currentJob?.duration_seconds);

  // 克隆音色上传固化成功：刷新列表并直接选中新音色
  const onCloneCreated = async (asset: VoiceAsset) => {
    commitVoice(asset.id);
    await loadAssets();
    void loadDiscovered();
  };

  // MiniMax 永久音色设计成功：只刷新列表，不抢占音色描述输入（可继续调整重新设计）
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
    // 声音克隆（非 higgs）：必须选中带样本的克隆音色（即用户上传固化的声音）
    if (!isHiggs && mode === "clone") {
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
        pitch,
        vol,
        takes,
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

  const providerLabels = Object.fromEntries((catalog?.providers ?? []).map((p) => [p.id, p.label]));

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      {/* 左栏：音色与全局参数 */}
      <VoiceSidebar
        mode={mode}
        modelId={modelId}
        voice={voice}
        onVoiceChange={commitVoice}
        onVoiceInput={inputVoice}
        speed={speed}
        onSpeedChange={setSpeed}
        emotion={emotion}
        onEmotionChange={setEmotion}
        pitch={pitch}
        onPitchChange={setPitch}
        vol={vol}
        onVolChange={setVol}
        takes={takes}
        onTakesChange={setTakes}
        assets={assets}
        onDeleteAsset={(a) => void deleteAsset(a)}
        onUseAsset={(a) => commitVoice(a.id)}
        onCloneCreated={(a) => void onCloneCreated(a)}
        onDesignCreated={(a) => void onDesignCreated(a)}
      />

      <div className="flex min-w-0 flex-1 flex-col bg-[#0b0c10]">
        {/* 常驻操作栏：模式 + 模型 + 当前音色 + 生成。动作紧邻台词区，不随内容滚动消失 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-6 py-3">
          <Segmented
            value={mode}
            onChange={(v) => switchMode(v as VoiceMode)}
            options={[
              { value: "dub", label: "配音" },
              { value: "design", label: "声音设计" },
              { value: "clone", label: "声音克隆" },
            ]}
          />
          <div className="w-[240px]">
            {(catalog?.tts.length ?? 0) > 0 ? (
              <ModelSelector
                models={catalog?.tts ?? []}
                value={modelId}
                onChange={setModelId}
                providerLabels={providerLabels}
              />
            ) : (
              <Input
                value={modelId}
                onChange={(e) => setModelId(e.target.value)}
                placeholder="输入语音模型 ID"
                className="h-9"
              />
            )}
          </div>
          <span
            className="hidden min-w-0 max-w-[200px] items-center gap-1.5 truncate text-xs text-muted-foreground md:flex"
            title={voice || undefined}
          >
            🎙 <span className="truncate">{voiceChip}</span>
          </span>
          <div className="ml-auto">
            <GenerationButton
              size="default"
              className="w-auto px-6"
              loading={generating}
              loadingLabel="生成中…"
              onClick={() => void generate()}
              disabled={designGenBlocked}
              title={designGenBlocked ? "当前模型不能按音色描述直接合成" : undefined}
            >
              生成语音
            </GenerationButton>
          </div>
        </div>
        {designGenBlocked && (
          <div className="border-b border-amber-500/20 bg-amber-500/[0.06] px-6 py-1.5 text-[11px] leading-relaxed text-amber-300/80">
            当前模型不能按描述直接合成：先用左栏「MiniMax 永久音色」设计，再点「已保存音色」选用；或换小米 voicedesign 模型。
          </div>
        )}

        {/* 工作区：台词编辑 + 播放器 + 历史，居中限宽 */}
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6 lg:p-8">
            <ScriptWorkbench
              value={text}
              onChange={setText}
              modelId={modelId}
              voices={[
                ...(catalog?.tts_voices ?? []).map((v) => ({
                  id: v.id,
                  label: v.label === v.id ? v.id : `${v.label}（${v.id}）`,
                })),
                // catalog.tts_voices 经 discovered 接口已含全部音色资产，直接拼 assets
                // 会让同一 voice_* 出现两次 → SelectItem key 冲突。只补 catalog 还没有的。
                ...assets
                  .filter((a) => !(catalog?.tts_voices ?? []).some((v) => v.id === a.id))
                  .map((a) => ({ id: a.id, label: `${a.name}（资产）` })),
              ]}
            />

            {currentJob?.status === "completed" && currentJob.output ? (
              <>
                <AudioPlayer
                  src={currentJob.output}
                  title={`最新生成${latestSeconds ? ` · ${latestSeconds}` : ""}`}
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
                title="生成的音频会显示在这里"
                hint="生成后可试听、下载或发送到 Studio 作为场景旁白"
              />
            )}

            {history.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-xs font-medium text-muted-foreground">最近生成（{history.length}）</h3>
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
    </div>
  );
}
