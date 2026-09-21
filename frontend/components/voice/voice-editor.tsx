"use client";

import { useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ModelSelector } from "@/components/common/model-selector";
import { SearchableSelect } from "@/components/common/searchable-select";
import { Segmented } from "@/components/common/segmented";
import { GenerationButton } from "@/components/common/generation-button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getRecent, pushRecent, saveModelPref } from "@/lib/model-prefs";
import { api } from "@/lib/api";
import { useAppStore } from "@/stores/app-store";
import type { VoiceAsset } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CloneVoicePanel } from "./clone-voice-panel";

const EMOTION_LABELS: Record<string, string> = {
  neutral: "平静（Neutral）",
  happy: "开心（Happy）",
  sad: "悲伤（Sad）",
  angry: "愤怒（Angry）",
  fearful: "恐惧（Fearful）",
  surprised: "惊讶（Surprised）",
  calm: "沉着（Calm）",
};

/** 三种语音模式由模型 ID 推导，模式切换只是换模型的快捷入口 */
export type VoiceMode = "dub" | "design" | "clone";

export function voiceModeOf(modelId: string): VoiceMode {
  const m = (modelId || "").toLowerCase();
  if (m.includes("voiceclone")) return "clone";
  if (m.includes("voicedesign")) return "design";
  return "dub";
}

/** higgs 本地模型双栖：配音（零样本/资产音色）与声音克隆（样本+逐字稿）都支持 */
export function isHiggsModel(modelId: string): boolean {
  return (modelId || "").toLowerCase().includes("higgs");
}

export interface VoiceEditorProps {
  modelId: string;
  onModelChange: (id: string) => void;
  text: string;
  onTextChange: (t: string) => void;
  voice: string;
  onVoiceChange: (v: string) => void;
  /** 自由打字路径（音色描述/手动 ID）：只更新值与偏好，不写入“最近使用” */
  onVoiceInput: (v: string) => void;
  speed: number;
  onSpeedChange: (s: number) => void;
  emotion: string;
  onEmotionChange: (e: string) => void;
  onGenerate: () => void;
  generating: boolean;
  assets: VoiceAsset[];
  onUseAsset: (a: VoiceAsset) => void;
  onDeleteAsset: (a: VoiceAsset) => void;
  /** 克隆音色上传固化成功：刷新列表并选用 */
  onCloneCreated: (a: VoiceAsset) => void;
  /** MiniMax 永久音色设计成功：刷新列表（不抢占 Voice 描述输入） */
  onDesignCreated: (a: VoiceAsset) => void;
}

export function VoiceEditor(props: VoiceEditorProps) {
  const catalog = useAppStore((s) => s.catalog);
  const providerLabels = Object.fromEntries((catalog?.providers ?? []).map((p) => [p.id, p.label]));
  const [recent, setRecent] = useState<string[]>(() => getRecent("voice"));
  const isHiggs = isHiggsModel(props.modelId);
  // 模式是显式用户意图：点页签设定，换模型不重算（在设计模式切到 speech 模型
  // 被弹回配音的事故源）。仅反向同步：配音模式下手动选了 voicedesign /
  // voiceclone 专属模型时，模式跟过去避免“配音界面配专用模型”。
  const [mode, setMode] = useState<VoiceMode>(() => voiceModeOf(props.modelId));
  useEffect(() => {
    const inherent = voiceModeOf(props.modelId);
    setMode((cur) => (inherent !== "dub" && cur === "dub" ? inherent : cur));
  }, [props.modelId]);
  // 音色设计模式的“最近描述”快选：只留自由文本描述，排除预置音色 ID 与资产 ID
  const knownVoiceIds = useMemo(
    () => new Set((catalog?.tts_voices ?? []).map((v) => v.id)),
    [catalog]
  );
  const recentDescs = useMemo(
    () => recent.filter((v) => !knownVoiceIds.has(v) && !v.startsWith("voice_")),
    [recent, knownVoiceIds]
  );

  // MiniMax 永久音色设计：需要 MiniMax Key；描述取上面的 Voice 输入，无需重复填写
  const credentials = useAppStore((s) => s.credentials);
  const hasMinimaxKey = credentials.some(
    (c) => c.provider === "minimax" || (c.base_url || "").toLowerCase().includes("minimax")
  );
  const [mmName, setMmName] = useState("");
  const [mmPreview, setMmPreview] = useState("");
  const [mmDesigning, setMmDesigning] = useState(false);

  const designMinimax = async () => {
    const prompt = props.voice.trim();
    if (!prompt) {
      toast.error("请先在 Voice 里填写音色描述");
      return;
    }
    const name = mmName.trim();
    if (!name) {
      toast.error("请给这个音色起个名字（如：深夜电台男声）");
      return;
    }
    setMmDesigning(true);
    try {
      const asset = await api.designMinimaxVoice({ name, prompt, preview_text: mmPreview.trim() });
      toast.success(`永久音色「${name}」已保存：点「已保存音色」选用后即可生成`);
      setMmName("");
      setMmPreview("");
      props.onDesignCreated(asset);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setMmDesigning(false);
    }
  };

  // 设计模式下只有 voicedesign 模型能按描述直接合成；其他模型（如 MiniMax speech）
  // 直接生成会把描述当 voice_id 发出去——需先设计永久音色或选用资产
  const voiceIsAsset = props.assets.some((a) => a.id === props.voice.trim());
  const designGenBlocked =
    mode === "design" &&
    !props.modelId.toLowerCase().includes("voicedesign") &&
    !voiceIsAsset;

  const selectVoice = (v: string) => {
    props.onVoiceChange(v);
    saveModelPref("voice", v);
    pushRecent("voice", v);
    setRecent(getRecent("voice"));
  };

  const switchMode = (target: VoiceMode) => {
    if (target === mode) return;
    setMode(target);
    // 克隆音色的资产 ID（voice_*）只在克隆模式有意义；切到其他模式时清掉，
    // 避免把机器 ID 当作音色描述/预置音色 ID 发给模型
    if (target !== "clone" && props.voice.startsWith("voice_")) {
      props.onVoiceInput("");
    }
    // higgs：配音⇄克隆自由切换（两套界面它都用得上），模型保持不动
    if (isHiggs && (target === "dub" || target === "clone")) {
      return;
    }
    // 当前模型自身就是目标模式（如从 voicedesign 模型返回配音后再进设计）：只切模式
    if (target === voiceModeOf(props.modelId)) {
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
      // 目录里没有该模式的专属模型（如只配了 MiniMax speech 模型）：模型保持不动——
      // MiniMax 永久音色 / 服务端克隆走独立接口，不依赖当前合成模型
      return;
    }
    props.onModelChange(
      next ?? (target === "clone" ? "mimo-v2.5-tts-voiceclone" : target === "design" ? "mimo-v2.5-tts-voicedesign" : "mimo-v2.5-tts")
    );
  };

  return (
    <div className="w-[340px] shrink-0 space-y-5 overflow-y-auto px-5 py-6">
      <h2 className="text-sm font-semibold">Voice Settings</h2>

      {/* 模式：配音=预置音色 / 声音设计=文字描述 / 声音克隆=上传样本 */}
      <div className="space-y-2">
        <Label>Mode</Label>
        <Segmented
          value={mode}
          onChange={(v) => switchMode(v as VoiceMode)}
          options={[
            { value: "dub", label: "配音" },
            { value: "design", label: "声音设计" },
            { value: "clone", label: "声音克隆" },
          ]}
        />
      </div>

      <div className="space-y-2">
        <Label>Model</Label>
        {(catalog?.tts.length ?? 0) > 0 ? (
          <ModelSelector
            models={catalog?.tts ?? []}
            value={props.modelId}
            onChange={props.onModelChange}
            providerLabels={providerLabels}
          />
        ) : (
          <Input
            value={props.modelId}
            onChange={(e) => props.onModelChange(e.target.value)}
            placeholder="输入语音模型 ID（也可在设置里保存为自定义模型）"
          />
        )}
      </div>

      {mode === "clone" ? (
        // 声音克隆：专属界面——上传样本即音色，与配音/设计完全分离
        <CloneVoicePanel
          modelId={props.modelId}
          assets={props.assets}
          selected={props.voice}
          onSelect={props.onUseAsset}
          onDelete={props.onDeleteAsset}
          onCreated={props.onCloneCreated}
        />
      ) : (
        <>
          <div className="space-y-2">
            <Label>Voice</Label>
            {/* 音色设计：描述是核心输入，固定用自由输入框——不随音色列表的异步加载
                在 Input / 下拉之间变形，否则发现接口返回后正在输入的内容会被打断 */}
            {mode === "design" ? (
              <>
                <Input
                  value={props.voice}
                  onChange={(e) => props.onVoiceInput(e.target.value)}
                  onBlur={() => {
                    const v = props.voice.trim();
                    if (v) {
                      pushRecent("voice", v);
                      setRecent(getRecent("voice"));
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                  }}
                  placeholder="输入音色描述，如：低沉沙哑的中年男声，像深夜电台主持人"
                />
                {recentDescs.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {recentDescs.map((v) => (
                      <button
                        key={v}
                        type="button"
                        title={v}
                        onClick={() => props.onVoiceChange(v)}
                        className="max-w-full truncate rounded-full border border-border bg-secondary/40 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                      >
                        {v.length > 16 ? v.slice(0, 16) + "…" : v}
                      </button>
                    ))}
                  </div>
                )}
              </>
            ) : (catalog?.tts_voices.length ?? 0) > 0 ? (
              <SearchableSelect
                items={(catalog?.tts_voices ?? []).map((v) => ({
                  value: v.id,
                  label: v.label === v.id ? v.id : `${v.label}（${v.id}）`,
                }))}
                value={props.voice}
                onChange={selectVoice}
                searchPlaceholder="搜索或输入音色 ID，回车确认…"
                recentValues={recent}
                emptyText="无匹配音色"
                allowCustom
                customLabelPrefix="使用自定义音色 ID"
              />
            ) : (
              <Input
                value={props.voice}
                onChange={(e) => props.onVoiceInput(e.target.value)}
                placeholder={
                  isHiggs
                    ? "default = 内置音色（零样本）；或点下方已保存音色用克隆音色"
                    : "输入音色 ID（配置 MiniMax / 小米 Key 后自动发现可用音色）"
                }
              />
            )}
            {mode === "design" && (
              <>
                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  音色设计模式：Voice 填音色描述（如“20 多岁女性、低沉沙哑、慵懒”）。
                  小米 voicedesign 同一描述每次嗓音都不同；用下方 MiniMax
                  可一次设计为永久音色，之后稳定复用不再漂移。
                </p>
                <div className="space-y-2 rounded-lg border border-border bg-secondary/30 p-3">
                  <div className="text-[11px] font-medium text-foreground">MiniMax 永久音色</div>
                  {hasMinimaxKey ? (
                    <>
                      <Input
                        value={mmName}
                        onChange={(e) => setMmName(e.target.value)}
                        placeholder="音色名称，如：深夜电台男声"
                        className="h-8 text-xs"
                      />
                      <Textarea
                        value={mmPreview}
                        onChange={(e) => setMmPreview(e.target.value)}
                        placeholder="试听文本（≤500 字，按字符计费）；留空用默认文案"
                        className="min-h-[48px] resize-none text-xs"
                        rows={2}
                      />
                      <Button
                        size="sm"
                        className="h-8 w-full"
                        disabled={mmDesigning}
                        onClick={() => void designMinimax()}
                      >
                        {mmDesigning ? (
                          <>
                            <Loader2 className="size-3.5 animate-spin" /> 设计中…
                          </>
                        ) : (
                          "用上面的描述设计永久音色"
                        )}
                      </Button>
                      <p className="text-[10px] leading-relaxed text-muted-foreground/70">
                        调 MiniMax 音色设计接口生成永久 voice_id；试听音频存为样本，小米 / Higgs 也能复用。
                      </p>
                    </>
                  ) : (
                    <p className="text-[10px] leading-relaxed text-muted-foreground/70">
                      添加 MiniMax Key（设置 → API Keys）后，可把描述一次性设计为永久音色（speech 系列模型稳定可用）。
                    </p>
                  )}
                </div>
              </>
            )}
          </div>

          {props.assets.length > 0 && (
            <div className="space-y-2">
              <Label>已保存音色（{props.assets.length}）</Label>
              <div className="space-y-1">
                {props.assets.map((a) => (
                  <div
                    key={a.id}
                    className={cn(
                      "flex items-center justify-between gap-2 rounded-lg border px-2.5 py-1.5 text-xs transition-colors",
                      props.voice === a.id
                        ? "border-primary/50 bg-primary/[0.08] text-foreground"
                        : "border-border bg-secondary/40 text-muted-foreground hover:text-foreground"
                    )}
                  >
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 cursor-pointer items-center gap-1 truncate text-left"
                      title={a.desc || a.name}
                      onClick={() => props.onUseAsset(a)}
                    >
                      <span className="truncate">{a.name}</span>
                      <span className="shrink-0 text-[10px] opacity-70">
                        {a.kind === "clone" ? "固化" : "设计"}
                      </span>
                      {a.minimax_voice_id && (
                        <span
                          title="已克隆到 MiniMax 服务端，speech 系列模型可直接使用"
                          className="shrink-0 rounded-full bg-secondary px-1.5 py-0.5 text-[9px] text-muted-foreground"
                        >
                          MiniMax
                        </span>
                      )}
                    </button>
                    <button
                      type="button"
                      title="删除该音色"
                      className="flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-400"
                      onClick={() => props.onDeleteAsset(a)}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label>Speed</Label>
          <span className="text-xs text-muted-foreground">{props.speed.toFixed(1)}x</span>
        </div>
        <Slider
          value={[props.speed]}
          onValueChange={(v) => props.onSpeedChange(v[0])}
          min={0.5}
          max={2}
          step={0.1}
        />
      </div>

      <div className="space-y-2">
        <Label>Emotion</Label>
        <Select value={props.emotion} onValueChange={props.onEmotionChange}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(catalog?.emotions ?? ["neutral"]).map((e) => (
              <SelectItem key={e} value={e}>
                {EMOTION_LABELS[e] ?? e}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Higgs 本地模型：控制标签写法速查（正文可内嵌 <|类别:值|> 标签） */}
      {(props.modelId || "").toLowerCase().includes("higgs") && (
        <details className="rounded-lg border border-border bg-secondary/30 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
          <summary className="cursor-pointer select-none text-xs font-medium text-foreground">
            Higgs 本地模型指南（标签 · 音色 · 速度）
          </summary>
          <div className="mt-2 space-y-1.5">
            <p>
              <span className="text-foreground">音色</span>：本模型无预置音色库。配音页 Voice 填{" "}
              <code className="rounded bg-secondary px-1">default</code> 用内置音色（零样本），或点下方
              「已保存音色」克隆复用；到「声音克隆」页可上传新样本（需填写
              <span className="text-foreground"> 逐字稿</span>）固化为音色。
            </p>
            <p>
              <span className="text-foreground">控制标签</span>：正文里可内嵌{" "}
              <code className="rounded bg-secondary px-1">&lt;|类别:值|&gt;</code> 控制情感/语速/拟声，
              左侧 Emotion / Speed 控件会自动转成标签加在句首，两者可叠加。
            </p>
            <p>
              <span className="text-foreground">句首标签</span>（放在句子开头，作用于整句）：
              情感 21 种如{" "}
              <code className="rounded bg-secondary px-1">&lt;|emotion:elation|&gt;</code>（affection /
              amusement / anger / arousal / awe / bitterness / confusion / contemplation /
              contentment / determination / disgust / elation / enthusiasm / fear / helplessness /
              longing / pride / relief / sadness / shame / surprise）；风格{" "}
              <code className="rounded bg-secondary px-1">&lt;|style:whispering|&gt;</code>（singing /
              shouting / whispering）；语速{" "}
              <code className="rounded bg-secondary px-1">&lt;|prosody:speed_slow|&gt;</code>（speed_very_slow /
              speed_slow / speed_fast / speed_very_fast）与音高（pitch_low / pitch_high）。
            </p>
            <p>
              <span className="text-foreground">行内标签</span>（放在效果发生处）：拟声{" "}
              <code className="rounded bg-secondary px-1">&lt;|sfx:cough|&gt;咳咳，</code>{" "}
              ——标签后紧跟拟声词、不留空格（cough / laughter / crying / screaming / burping / humming /
              sigh / sniff / sneeze）；停顿{" "}
              <code className="rounded bg-secondary px-1">&lt;|prosody:pause|&gt;</code>（pause / long_pause）。
            </p>
            <p className="text-muted-foreground/80">
              示例：<code className="rounded bg-secondary px-1">&lt;|emotion:sadness|&gt;他走了。&lt;|prosody:pause|&gt;再也没有回来。</code>
            </p>
            <p className="text-muted-foreground/80">
              只认以上 43 个标签，写别的会被当作文字念出来。本地 CPU 合成较慢（约 9 秒出 1 秒音频），
              长文本请耐心等待任务完成。
            </p>
          </div>
        </details>
      )}

      <GenerationButton
        loading={props.generating}
        loadingLabel="Generating..."
        onClick={props.onGenerate}
        disabled={designGenBlocked}
        title={designGenBlocked ? "当前模型不能按音色描述直接合成" : undefined}
      >
        Generate Voice
      </GenerationButton>
      {designGenBlocked && (
        <p className="text-[10px] leading-relaxed text-amber-300/80">
          当前模型不能按描述直接合成：先用上方「MiniMax 永久音色」设计，再点「已保存音色」选用；或换小米 voicedesign 模型。
        </p>
      )}
    </div>
  );
}
