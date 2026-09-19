"use client";

import { useMemo, useState } from "react";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
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
}

export function VoiceEditor(props: VoiceEditorProps) {
  const catalog = useAppStore((s) => s.catalog);
  const providerLabels = Object.fromEntries((catalog?.providers ?? []).map((p) => [p.id, p.label]));
  const [recent, setRecent] = useState<string[]>(() => getRecent("voice"));
  const mode = voiceModeOf(props.modelId);
  // 音色设计模式的“最近描述”快选：只留自由文本描述，排除预置音色 ID 与资产 ID
  const knownVoiceIds = useMemo(
    () => new Set((catalog?.tts_voices ?? []).map((v) => v.id)),
    [catalog]
  );
  const recentDescs = useMemo(
    () => recent.filter((v) => !knownVoiceIds.has(v) && !v.startsWith("voice_")),
    [recent, knownVoiceIds]
  );

  const selectVoice = (v: string) => {
    props.onVoiceChange(v);
    saveModelPref("voice", v);
    pushRecent("voice", v);
    setRecent(getRecent("voice"));
  };

  const switchMode = (target: VoiceMode) => {
    if (target === mode) return;
    const models = catalog?.tts ?? [];
    const lower = (id: string) => id.toLowerCase();
    let next: string;
    if (target === "clone") {
      next = models.find((m) => lower(m.id).includes("voiceclone"))?.id ?? "mimo-v2.5-tts-voiceclone";
    } else if (target === "design") {
      next = models.find((m) => lower(m.id).includes("voicedesign"))?.id ?? "mimo-v2.5-tts-voicedesign";
    } else {
      next =
        models.find((m) => !lower(m.id).includes("voicedesign") && !lower(m.id).includes("voiceclone"))
          ?.id ?? "mimo-v2.5-tts";
    }
    props.onModelChange(next);
    // 克隆音色的资产 ID（voice_*）只在克隆模式有意义；切到其他模式时清掉，
    // 避免把机器 ID 当作音色描述/预置音色 ID 发给模型
    if (target !== "clone" && props.voice.startsWith("voice_")) {
      props.onVoiceInput("");
    }
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
                placeholder="输入音色 ID（配置 MiniMax / 小米 Key 后自动发现可用音色）"
              />
            )}
            {mode === "design" && (
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                音色设计模式：Voice 填音色描述（如“20 多岁女性、低沉沙哑、慵懒”）。
                同一描述每次生成的嗓音都不同，满意后点「保存为音色」固化复用。
              </p>
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

      <GenerationButton loading={props.generating} loadingLabel="Generating..." onClick={props.onGenerate}>
        Generate Voice
      </GenerationButton>
    </div>
  );
}
