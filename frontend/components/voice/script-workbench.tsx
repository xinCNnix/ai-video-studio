"use client";

/**
 * 配音工作台：台词区内联标记编辑器，整体一张卡片。
 *
 * 选中任意片段（不必整句）→ 工具条给片段加情绪/音调/音量/语速/音色标记；
 * 光标处插入停顿 <#x#> / MiniMax 语气词 (laughs)。标记语法见 lib/voice-script.ts，
 * 后端 voice_script.py 拆段独立合成后拼接——与 MiniMax 网页工作台「选中文字
 * 加情绪」同一实现路径（MiniMax API 无句内情绪标签，片段情绪靠拆段合成）。
 */
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  EMOTION_LABELS,
  PARALANGUAGE_TAGS,
  parseVoiceScript,
  stripVoiceMarkers,
} from "@/lib/voice-script";
import { cn } from "@/lib/utils";

export interface WorkbenchVoiceOption {
  id: string;
  label: string;
}

export interface ScriptWorkbenchProps {
  value: string;
  onChange: (v: string) => void;
  modelId: string;
  voices: WorkbenchVoiceOption[];
}

const EMOTION_BADGE: Record<string, string> = {
  happy: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  sad: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  angry: "bg-red-500/15 text-red-300 border-red-500/30",
  fearful: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  disgusted: "bg-lime-500/15 text-lime-300 border-lime-500/30",
  surprised: "bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-500/30",
  calm: "bg-teal-500/15 text-teal-300 border-teal-500/30",
};

function voiceLabelOf(id: string, voices: WorkbenchVoiceOption[]): string {
  const hit = voices.find((v) => v.id === id);
  if (hit) return hit.label.length > 8 ? hit.label.slice(0, 8) + "…" : hit.label;
  return id.length > 10 ? id.slice(0, 10) + "…" : id;
}

/** 工具条分组标签：竖线分隔，让「作用于选区」与「作用于光标」两组一眼可分 */
function GroupTag({ children }: { children: string }) {
  return (
    <span className="shrink-0 text-[10px] font-medium tracking-wide text-muted-foreground/70">
      {children}
    </span>
  );
}

export function ScriptWorkbench(props: ScriptWorkbenchProps) {
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const [emotion, setEmotion] = useState("happy");
  const [pitch, setPitch] = useState(3);
  const [vol, setVol] = useState(2);
  const [speed, setSpeed] = useState(1.2);
  const [voiceId, setVoiceId] = useState("");
  const [pauseSec, setPauseSec] = useState(0.5);
  const [paraTag, setParaTag] = useState("laughs");

  const isMinimaxSpeech = (props.modelId || "").toLowerCase().startsWith("speech");
  const supportsParatags = /2\.8/.test(props.modelId || "");

  const parts = useMemo(() => parseVoiceScript(props.value), [props.value]);

  /** 在光标处插入片段（保持焦点与光标位置） */
  const insertAtCursor = (snippet: string) => {
    const ta = taRef.current;
    const pos = ta ? (ta.selectionStart ?? props.value.length) : props.value.length;
    const end = ta ? (ta.selectionEnd ?? pos) : pos;
    const next = props.value.slice(0, pos) + snippet + props.value.slice(end);
    props.onChange(next);
    requestAnimationFrame(() => {
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(pos + snippet.length, pos + snippet.length);
    });
  };

  /** 选中文字包裹片段标记（选区恢复为原文字，便于连续叠加多个标记） */
  const applyToSelection = (open: string, close: string) => {
    const ta = taRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    if (start === end) {
      toast.error("请先选中要设置的文字（可只选半句甚至几个字）");
      return;
    }
    const sel = props.value.slice(start, end);
    const next = props.value.slice(0, start) + open + sel + close + props.value.slice(end);
    props.onChange(next);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + open.length, end + open.length);
    });
  };

  /** 选区内清除全部标记（保留文字）；未选中则清全文 */
  const clearMarkers = () => {
    const ta = taRef.current;
    const start = ta?.selectionStart ?? 0;
    const end = ta?.selectionEnd ?? props.value.length;
    if (start >= end) {
      props.onChange(stripVoiceMarkers(props.value));
      return;
    }
    const cleaned = stripVoiceMarkers(props.value.slice(start, end));
    const next = props.value.slice(0, start) + cleaned + props.value.slice(end);
    props.onChange(next);
    requestAnimationFrame(() => {
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(start, start + cleaned.length);
    });
  };

  const clampNum = (v: number, lo: number, hi: number, fallback: number) =>
    Number.isFinite(v) ? Math.max(lo, Math.min(hi, v)) : fallback;

  return (
    <div className="overflow-hidden rounded-2xl border border-border bg-card">
      {/* 卡片头 */}
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold">台词文案</h2>
        <span className="hidden truncate text-[11px] text-muted-foreground sm:inline">
          选中片段加情绪 / 音调 / 音量 / 语速 / 音色标记，光标处插入停顿与语气词
        </span>
      </div>

      {/* 工具条：一组作用于选区，一组作用于光标 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-secondary/20 px-4 py-2.5">
        <GroupTag>选中标记</GroupTag>

        <div className="flex items-center gap-1.5">
          <Select value={emotion} onValueChange={setEmotion}>
            <SelectTrigger className="h-8 w-[96px] text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.keys(EMOTION_LABELS)
                .filter((e) => e !== "neutral")
                .map((e) => (
                  <SelectItem key={e} value={e} className="text-xs">
                    {EMOTION_LABELS[e]}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs"
            title="给选中文字设置片段情绪（后端拆段独立合成）"
            onClick={() => applyToSelection(`[${emotion}]`, `[/${emotion}]`)}
          >
            情绪
          </Button>
        </div>

        <div className="flex items-center gap-1">
          <Label className="text-[11px] text-muted-foreground">音调</Label>
          <Input
            type="number"
            min={-12}
            max={12}
            step={1}
            value={pitch}
            onChange={(e) => setPitch(Number(e.target.value))}
            className="h-8 w-14 text-xs"
          />
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs"
            title="MiniMax 半音（-12~12）；小米近似描述，Higgs 转高低档标签"
            onClick={() => {
              const p = clampNum(pitch, -12, 12, 0);
              if (p === 0) {
                toast.error("音调 0 即默认，请设非 0 值");
                return;
              }
              applyToSelection(`[pitch:${p}]`, "[/pitch]");
            }}
          >
            应用
          </Button>
        </div>

        <div className="flex items-center gap-1">
          <Label className="text-[11px] text-muted-foreground">音量</Label>
          <Input
            type="number"
            min={1}
            max={10}
            step={1}
            value={vol}
            onChange={(e) => setVol(Number(e.target.value))}
            className="h-8 w-14 text-xs"
          />
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs"
            title="MiniMax 音量 1~10（1 为常规）；其他厂商尽力映射"
            onClick={() => {
              const v = clampNum(vol, 1, 10, 2);
              if (v === 1) {
                toast.error("音量 1 即常规音量，请设 2~10");
                return;
              }
              applyToSelection(`[vol:${v}]`, "[/vol]");
            }}
          >
            应用
          </Button>
        </div>

        <div className="flex items-center gap-1">
          <Label className="text-[11px] text-muted-foreground">语速</Label>
          <Input
            type="number"
            min={0.5}
            max={2}
            step={0.1}
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
            className="h-8 w-14 text-xs"
          />
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs"
            title="片段语速倍率 0.5~2.0"
            onClick={() => {
              const s = clampNum(speed, 0.5, 2, 1.2);
              if (Math.abs(s - 1) < 0.05) {
                toast.error("语速 1.0 即默认，请设其他值");
                return;
              }
              applyToSelection(`[speed:${s.toFixed(1)}]`, "[/speed]");
            }}
          >
            应用
          </Button>
        </div>

        {props.voices.length > 0 && (
          <div className="flex items-center gap-1.5">
            <Select value={voiceId || undefined} onValueChange={setVoiceId}>
              <SelectTrigger className="h-8 w-[140px] text-xs">
                <SelectValue placeholder="选音色…" />
              </SelectTrigger>
              <SelectContent className="max-h-64">
                {props.voices.map((v) => (
                  <SelectItem key={v.id} value={v.id} className="text-xs">
                    {v.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              variant="outline"
              className="h-8 px-2 text-xs"
              title="给选中文字换音色（多角色对话：不同角色选不同音色）"
              onClick={() => {
                if (!voiceId) {
                  toast.error("先选择要用的音色");
                  return;
                }
                applyToSelection(`[voice:${voiceId}]`, "[/voice]");
              }}
            >
              音色
            </Button>
          </div>
        )}

        <span className="h-3.5 w-px shrink-0 bg-border" />
        <GroupTag>插入</GroupTag>

        <div className="flex items-center gap-1">
          <Label className="text-[11px] text-muted-foreground">停顿</Label>
          <Input
            type="number"
            min={0.1}
            max={10}
            step={0.1}
            value={pauseSec}
            onChange={(e) => setPauseSec(Number(e.target.value))}
            className="h-8 w-14 text-xs"
          />
          <span className="text-[11px] text-muted-foreground">秒</span>
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs"
            title="在光标处插入停顿（所有厂商生效，拼接时插入静音）"
            onClick={() => {
              const s = clampNum(pauseSec, 0.1, 10, 0.5);
              setPauseSec(s);
              insertAtCursor(`<#${s.toFixed(1)}#>`);
            }}
          >
            插入
          </Button>
        </div>

        {isMinimaxSpeech && (
          <div className="flex items-center gap-1.5">
            <Label className="text-[11px] text-muted-foreground">语气词</Label>
            <Select value={paraTag} onValueChange={setParaTag}>
              <SelectTrigger className="h-8 w-[96px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="max-h-64">
                {PARALANGUAGE_TAGS.map((t) => (
                  <SelectItem key={t.tag} value={t.tag} className="text-xs">
                    {t.label}（{t.tag}）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              variant="outline"
              className="h-8 px-2 text-xs"
              title={`在光标处插入语气词标签${supportsParatags ? "" : "（当前模型非 2.8 系，合成时会自动剥离）"}`}
              onClick={() => insertAtCursor(`(${paraTag})`)}
            >
              插入
            </Button>
            {!supportsParatags && (
              <span className="text-[10px] leading-tight text-muted-foreground/70">
                仅 speech-2.8 系支持
              </span>
            )}
          </div>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            className="h-8 px-2 text-xs text-muted-foreground"
            title="清除选中范围内的标记（保留文字）；未选中则清除全部"
            onClick={clearMarkers}
          >
            清除标记
          </Button>
        </div>
      </div>

      <textarea
        ref={taRef}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        placeholder={"输入需要转成语音的旁白文案……\n\n选中任意片段后用上方工具条加情绪/音调/音量/语速/音色，光标处可插停顿；例如：\n[sad]他走了。[/sad]<#0.8#>[pitch:2]可我还没来得及说再见。[/pitch]"}
        className="min-h-[240px] w-full resize-y bg-transparent px-4 py-3.5 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/60 focus:outline-none"
      />

      {/* 结构预览：按片段参数拆分的合成计划（与后端解析同语义） */}
      {props.value.trim() && parts.length > 0 && (
        <div className="space-y-1.5 border-t border-border bg-secondary/20 px-4 py-2.5">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-medium text-foreground">合成结构</span>
            <span className="text-[10px] text-muted-foreground">
              {parts.filter((p) => p.type === "speech").length} 个语音段 ·{" "}
              {parts.filter((p) => p.type === "pause").length} 处停顿 · 不同参数的段会分别合成再拼接
            </span>
          </div>
          <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
            {parts.map((p, i) =>
              p.type === "pause" ? (
                <span
                  key={i}
                  className="inline-flex items-center rounded-full border border-border bg-background px-2 py-0.5 text-[10px] tabular-nums text-muted-foreground"
                >
                  ⏸ {p.seconds.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}s 静音
                </span>
              ) : (
                <span
                  key={i}
                  className={cn(
                    "inline-flex max-w-full items-center gap-1 rounded-md border px-2 py-0.5 text-[11px]",
                    Object.keys(p.overrides).length === 0
                      ? "border-border bg-background text-muted-foreground"
                      : "border-primary/30 bg-primary/[0.06] text-foreground",
                  )}
                >
                  <span className="truncate">{p.text.trim().slice(0, 18) || "（空）"}</span>
                  {p.overrides.emotion && (
                    <span className={cn("rounded-full border px-1.5 py-px text-[9px]", EMOTION_BADGE[p.overrides.emotion] ?? "border-border text-muted-foreground")}>
                      {EMOTION_LABELS[p.overrides.emotion] ?? p.overrides.emotion}
                    </span>
                  )}
                  {p.overrides.pitch !== undefined && (
                    <span className="rounded-full bg-secondary px-1.5 py-px text-[9px] tabular-nums text-muted-foreground">
                      {p.overrides.pitch > 0 ? `↑${p.overrides.pitch}` : `↓${-p.overrides.pitch}`}
                    </span>
                  )}
                  {p.overrides.vol !== undefined && (
                    <span className="rounded-full bg-secondary px-1.5 py-px text-[9px] tabular-nums text-muted-foreground">
                      🔊{p.overrides.vol}
                    </span>
                  )}
                  {p.overrides.speed !== undefined && (
                    <span className="rounded-full bg-secondary px-1.5 py-px text-[9px] tabular-nums text-muted-foreground">
                      {p.overrides.speed}x
                    </span>
                  )}
                  {p.overrides.voice && (
                    <span className="rounded-full bg-secondary px-1.5 py-px text-[9px] text-muted-foreground">
                      🎙{voiceLabelOf(p.overrides.voice, props.voices)}
                    </span>
                  )}
                </span>
              ),
            )}
          </div>
        </div>
      )}
    </div>
  );
}
