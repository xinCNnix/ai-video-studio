"use client";

import { useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { SearchableSelect } from "@/components/common/searchable-select";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getRecent, pushRecent, saveModelPref } from "@/lib/model-prefs";
import { api } from "@/lib/api";
import { EMOTION_LABELS } from "@/lib/voice-script";
import { useAppStore } from "@/stores/app-store";
import type { VoiceAsset } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CloneVoicePanel } from "./clone-voice-panel";

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

export interface VoiceSidebarProps {
  mode: VoiceMode;
  modelId: string;
  voice: string;
  onVoiceChange: (v: string) => void;
  /** 自由打字路径（音色描述/手动 ID）：只更新值与偏好，不写入“最近使用” */
  onVoiceInput: (v: string) => void;
  /** 全局默认语速（0.5~2.0）；片段 [speed:x] 标记优先 */
  speed: number;
  onSpeedChange: (s: number) => void;
  /** 全局默认音调（MiniMax 半音 -12~12）；片段 [pitch:x] 标记优先 */
  pitch: number;
  onPitchChange: (p: number) => void;
  /** 全局默认音量（1~10，1 为常规）；片段 [vol:x] 标记优先 */
  vol: number;
  onVolChange: (v: number) => void;
  /** 多版本挑优：生成 N 版按韵律评分取最优（对抗 MiniMax 合成语调随机性） */
  takes: number;
  onTakesChange: (n: number) => void;
  emotion: string;
  onEmotionChange: (e: string) => void;
  assets: VoiceAsset[];
  onUseAsset: (a: VoiceAsset) => void;
  onDeleteAsset: (a: VoiceAsset) => void;
  /** 克隆音色上传固化成功：刷新列表并选用 */
  onCloneCreated: (a: VoiceAsset) => void;
  /** MiniMax 永久音色设计成功：刷新列表（不抢占 Voice 描述输入） */
  onDesignCreated: (a: VoiceAsset) => void;
}

/**
 * 左栏：音色与全局参数。模式/模型/生成按钮在页面顶部的常驻操作栏里，
 * 这里只管「用哪个声音、按什么默认参数念」。
 */
export function VoiceSidebar(props: VoiceSidebarProps) {
  const catalog = useAppStore((s) => s.catalog);
  const [recent, setRecent] = useState<string[]>([]);
  // 最近描述存于 localStorage：挂载后读，保证 SSR 与水合首帧一致（服务端无 window）
  useEffect(() => {
    setRecent(getRecent("voice"));
  }, []);
  const isHiggs = isHiggsModel(props.modelId);

  // 音色设计模式的“最近描述”快选：只留自由文本描述，排除预置音色 ID 与资产 ID
  const knownVoiceIds = useMemo(
    () => new Set((catalog?.tts_voices ?? []).map((v) => v.id)),
    [catalog]
  );
  const recentDescs = useMemo(
    () => recent.filter((v) => !knownVoiceIds.has(v) && !v.startsWith("voice_")),
    [recent, knownVoiceIds]
  );

  // MiniMax 永久音色设计：需要 MiniMax Key；描述取音色输入，无需重复填写
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
      toast.error("请先在音色里填写描述");
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

  const selectVoice = (v: string) => {
    props.onVoiceChange(v);
    saveModelPref("voice", v);
    pushRecent("voice", v);
    setRecent(getRecent("voice"));
  };

  return (
    <div className="flex w-[320px] shrink-0 flex-col overflow-y-auto border-r border-border px-5 py-6">
      <h2 className="text-sm font-semibold">音色与参数</h2>

      {/* ── 音色 ── */}
      <div className="mt-4 space-y-3">
        <div className="text-[11px] font-medium text-muted-foreground">音色</div>
        {props.mode === "clone" ? (
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
              {/* 音色设计：描述是核心输入，固定用自由输入框——不随音色列表的异步加载
                  在 Input / 下拉之间变形，否则发现接口返回后正在输入的内容会被打断 */}
              {props.mode === "design" ? (
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
              {props.mode === "design" && (
                <>
                  <p className="text-[11px] leading-relaxed text-muted-foreground">
                    声音设计模式：填音色描述（如“20 多岁女性、低沉沙哑、慵懒”）。
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
              <div className="space-y-1.5">
                <Label>已保存音色（{props.assets.length}）</Label>
                {/* 资产多了会把参数挤出视口：列表自身限高滚动 */}
                <div className="max-h-56 space-y-1 overflow-y-auto pr-0.5">
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
      </div>

      {/* ── 朗读参数（全局默认）：2×2 紧凑网格 ── */}
      <div className="mt-5 space-y-3 border-t border-border pt-4">
        <div className="text-[11px] font-medium text-muted-foreground">朗读参数（全局默认）</div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3">
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <Label className="text-xs">语速</Label>
              <span className="text-xs tabular-nums text-muted-foreground">
                {props.speed.toFixed(1)}x
              </span>
            </div>
            <Slider
              value={[props.speed]}
              onValueChange={(v) => props.onSpeedChange(v[0])}
              min={0.5}
              max={2}
              step={0.1}
            />
          </div>
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <Label className="text-xs">音调</Label>
              <span className="text-xs tabular-nums text-muted-foreground">
                {props.pitch > 0 ? `+${props.pitch}` : props.pitch}
              </span>
            </div>
            <Slider
              value={[props.pitch]}
              onValueChange={(v) => props.onPitchChange(Math.round(v[0]))}
              min={-12}
              max={12}
              step={1}
            />
          </div>
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <Label className="text-xs">音量</Label>
              <span className="text-xs tabular-nums text-muted-foreground">{props.vol}</span>
            </div>
            <Slider
              value={[props.vol]}
              onValueChange={(v) => props.onVolChange(Math.round(v[0]))}
              min={1}
              max={10}
              step={1}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">情绪</Label>
            <Select value={props.emotion} onValueChange={props.onEmotionChange}>
              <SelectTrigger className="h-8 text-xs">
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
          <div className="space-y-1">
            <Label
              className="text-xs"
              title="生成 N 版后自动按韵律评分挑最好的一版：问句看句尾升幅，陈述句看语调起伏——对抗 MiniMax 同参合成的语调随机性"
            >
              挑优版本
            </Label>
            <Select
              value={String(props.takes)}
              onValueChange={(v) => props.onTakesChange(Number(v))}
            >
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {[1, 2, 3, 4, 5, 6].map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n === 1 ? "1 版（关）" : `${n} 版`}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <p className="text-[10px] leading-relaxed text-muted-foreground/70">
          以上为全局默认：MiniMax 精确生效，小米 / Higgs 自动就近映射。选中片段要单独设置时，用台词工具条加标记。
          挑优版本 &gt; 1 时生成多版自动评分留最优，耗时相应变长。
        </p>
      </div>

      {/* Higgs 本地模型：控制标签写法速查（正文可内嵌 <|类别:值|> 标签） */}
      {isHiggsModel(props.modelId) && (
        <details className="mt-5 rounded-lg border border-border bg-secondary/30 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
          <summary className="cursor-pointer select-none text-xs font-medium text-foreground">
            Higgs 本地模型指南（标签 · 音色 · 速度）
          </summary>
          <div className="mt-2 space-y-1.5">
            <p>
              <span className="text-foreground">音色</span>：本模型无预置音色库。音色填{" "}
              <code className="rounded bg-secondary px-1">default</code> 用内置音色（零样本），或点
              「已保存音色」克隆复用；到「声音克隆」模式可上传新样本（需填写
              <span className="text-foreground"> 逐字稿</span>）固化为音色。
            </p>
            <p>
              <span className="text-foreground">控制标签</span>：正文里可内嵌{" "}
              <code className="rounded bg-secondary px-1">&lt;|类别:值|&gt;</code> 控制情感/语速/拟声，
              顶部「情绪 / 语速」控件会自动转成标签加在句首，两者可叠加。
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
    </div>
  );
}
