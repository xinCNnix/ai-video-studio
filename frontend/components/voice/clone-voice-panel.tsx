"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Mic, Pause, Play, RefreshCw, Trash2, UploadCloud } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl } from "@/lib/api";
import type { VoiceAsset } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAppStore } from "@/stores/app-store";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Waveform } from "./waveform";

const SAMPLE_EXTS = [".mp3", ".wav"];
const MAX_SAMPLE_MB = 20;

function fmtMB(bytes: number) {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 克隆音色列表里的一条：试听（波形+进度）+ 选用 + 补克隆 MiniMax + 删除 */
function CloneSampleRow({
  asset,
  selected,
  playingId,
  onPlayingChange,
  onSelect,
  onRetryMinimax,
  retrying,
  onDelete,
}: {
  asset: VoiceAsset;
  selected: boolean;
  playingId: string | null;
  onPlayingChange: (id: string | null) => void;
  onSelect: () => void;
  /** 未克隆到 MiniMax 时显示的重试入口（未配置 MiniMax Key 时不传） */
  onRetryMinimax?: () => void;
  retrying?: boolean;
  onDelete: () => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  // sample_url 是后端相对路径（/api/...）：必须经 fileUrl 拼后端地址，否则请求
  // 打到前端端口 404，试听样本点不出声（audio 报 MEDIA_ERR_SRC_NOT_SUPPORTED）
  const src = fileUrl(asset.sample_url);

  // 面板内同一时间只播一条：别的是当前播放者时暂停自己
  useEffect(() => {
    if (playingId !== asset.id && playing) {
      audioRef.current?.pause();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playingId]);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
    } else {
      onPlayingChange(asset.id);
      void audio.play();
    }
  };

  const seek = (f: number) => {
    const audio = audioRef.current;
    if (!audio || !duration) return;
    audio.currentTime = f * duration;
    setTime(audio.currentTime);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => e.key === "Enter" && onSelect()}
      className={cn(
        "cursor-pointer rounded-lg border px-2.5 py-2 transition-colors",
        selected
          ? "border-primary/50 bg-primary/[0.08]"
          : "border-border bg-secondary/40 hover:border-white/20"
      )}
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={toggle}
          title={playing ? "暂停试听" : "试听样本"}
          className={cn(
            "flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full transition-transform hover:scale-105",
            selected ? "accent-gradient text-white" : "bg-white/[0.08] text-foreground"
          )}
        >
          {playing ? <Pause className="size-3.5" /> : <Play className="ml-0.5 size-3.5" />}
        </button>
        <div className="min-w-0 flex-1">
          <Waveform
            src={src}
            progress={duration > 0 ? time / duration : 0}
            height={30}
            onSeek={seek}
          />
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {selected && (
            <span className="flex items-center gap-1 rounded-full bg-primary/15 px-2 py-0.5 text-[10px] text-primary">
              <Check className="size-3" /> 使用中
            </span>
          )}
          {onRetryMinimax && !asset.minimax_voice_id && (
            <button
              type="button"
              title="补克隆到 MiniMax 服务端（speech 系列模型可用）"
              onClick={(e) => {
                e.stopPropagation();
                onRetryMinimax();
              }}
              disabled={retrying}
              className="flex size-6 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-primary/10 hover:text-primary disabled:cursor-wait disabled:opacity-60"
            >
              {retrying ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            </button>
          )}
          <button
            type="button"
            title="删除该克隆音色"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="flex size-6 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-400"
          >
            <Trash2 className="size-3.5" />
          </button>
        </div>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span className="flex min-w-0 items-center gap-1.5" title={asset.desc || asset.name}>
          <span className="truncate">{asset.name}</span>
          {asset.minimax_voice_id && (
            <span
              title="已克隆到 MiniMax 服务端，speech 系列模型可直接使用"
              className="shrink-0 rounded-full bg-secondary px-1.5 py-0.5 text-[9px] text-muted-foreground"
            >
              MiniMax
            </span>
          )}
        </span>
        <span className="shrink-0 tabular-nums">
          {time > 0 ? `${Math.floor(time)}s / ` : ""}
          {duration > 0 ? `${Math.floor(duration)}s` : ""}
        </span>
      </div>
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => {
          setPlaying(false);
          onPlayingChange(null);
        }}
        onEnded={() => {
          setPlaying(false);
          onPlayingChange(null);
          setTime(0);
        }}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        className="hidden"
      />
    </div>
  );
}

/**
 * 声音克隆专属面板：上传自己的声音样本 → 固化为克隆音色 → 选用生成。
 * 与配音（预置音色）/声音设计（文字描述）界面完全分离。
 */
export function CloneVoicePanel({
  modelId,
  assets,
  selected,
  onSelect,
  onDelete,
  onCreated,
}: {
  /** 当前语音模型 ID：本地 Higgs 克隆需要逐字稿（ref_text），小米/MiniMax 不需要 */
  modelId: string;
  assets: VoiceAsset[];
  selected: string;
  onSelect: (a: VoiceAsset) => void;
  onDelete: (a: VoiceAsset) => void;
  /** 上传固化成功后回调（父层刷新列表并选用） */
  onCreated: (a: VoiceAsset) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const previewAudioRef = useRef<HTMLAudioElement>(null);
  const [dragging, setDragging] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingUrl, setPendingUrl] = useState("");
  const [pendingName, setPendingName] = useState("");
  const [pendingRefText, setPendingRefText] = useState("");
  const [saving, setSaving] = useState(false);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [previewTime, setPreviewTime] = useState(0);
  const [previewDuration, setPreviewDuration] = useState(0);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  // 配置了 MiniMax Key 时默认双克隆：小米样本随身带 + MiniMax 服务端永久 voice_id
  const credentials = useAppStore((s) => s.credentials);
  const hasMinimaxKey = credentials.some(
    (c) => c.provider === "minimax" || (c.base_url || "").toLowerCase().includes("minimax")
  );
  const [syncMinimax, setSyncMinimax] = useState(true);
  const isHiggs = (modelId || "").toLowerCase().includes("higgs");

  // 克隆音色 = 带样本的固化资产（含此前 voicedesign 固化的与本次上传的）
  const clones = assets.filter((a) => a.kind === "clone" && a.has_sample);

  useEffect(() => {
    return () => {
      if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    };
  }, [pendingUrl]);

  const acceptFile = (file: File) => {
    const name = file.name.toLowerCase();
    if (!SAMPLE_EXTS.some((ext) => name.endsWith(ext))) {
      toast.error("样本仅支持 mp3 / wav 格式");
      return;
    }
    if (file.size > MAX_SAMPLE_MB * 1024 * 1024) {
      toast.error(`样本过大（上限 ${MAX_SAMPLE_MB}MB）：${fmtMB(file.size)}`);
      return;
    }
    if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    setPendingFile(file);
    setPendingUrl(URL.createObjectURL(file));
    setPendingName(file.name.replace(/\.[^.]+$/, ""));
    setPreviewPlaying(false);
    setPreviewTime(0);
    setPreviewDuration(0);
  };

  const saveClone = async () => {
    if (!pendingFile) return;
    const name = pendingName.trim();
    if (!name) {
      toast.error("请给这个克隆音色起个名字（如：我的声音）");
      return;
    }
    const refText = pendingRefText.trim();
    if (isHiggs && !refText) {
      toast.error("Higgs 本地克隆需要样本逐字稿：样本里念了什么就一字不差地写什么");
      return;
    }
    setSaving(true);
    try {
      const up = await api.upload(pendingFile, "audio");
      const wantMinimax = hasMinimaxKey && syncMinimax;
      const asset = await api.saveVoiceAsset({
        name,
        kind: "clone",
        sample_from: up.url,
        ref_text: refText,
        sync_minimax: wantMinimax,
      });
      if (asset.minimax_error) {
        // 资产本身已保存（小米/本地样本克隆可用），仅 MiniMax 侧失败
        toast.warning(`音色「${name}」已保存，但 MiniMax 克隆失败：${asset.minimax_error}`);
      } else if (isHiggs) {
        toast.success(`克隆音色「${name}」已保存：Higgs 本地生成时将以它为音色样本`);
      } else if (wantMinimax) {
        toast.success(`克隆音色「${name}」已保存：小米即时可用，MiniMax speech 模型同步可用`);
      } else {
        toast.success(`克隆音色「${name}」已保存，生成时将以它为音色样本`);
      }
      URL.revokeObjectURL(pendingUrl);
      setPendingFile(null);
      setPendingUrl("");
      setPendingName("");
      setPendingRefText("");
      onCreated(asset);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  // 已保存但没克隆到 MiniMax 的音色：无需重新上传样本，一键补克隆
  const retryMinimax = async (a: VoiceAsset) => {
    setRetryingId(a.id);
    try {
      const res = await api.retryVoiceAssetMinimax(a.id);
      if (res.minimax_error) {
        toast.warning(`「${a.name}」MiniMax 克隆失败：${res.minimax_error}`);
      } else {
        toast.success(`「${a.name}」已克隆到 MiniMax，speech 系列模型可用`);
      }
      onCreated(res.minimax_voice_id ? res : a);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setRetryingId(null);
    }
  };

  const togglePreview = () => {
    const audio = previewAudioRef.current;
    if (!audio) return;
    if (previewPlaying) {
      audio.pause();
    } else {
      setPlayingId(null);
      void audio.play();
    }
  };

  const seekPreview = (f: number) => {
    const audio = previewAudioRef.current;
    if (!audio || !previewDuration) return;
    audio.currentTime = f * previewDuration;
    setPreviewTime(audio.currentTime);
  };

  return (
    <div className="space-y-3">
      {/* 上传区：拖拽或点击 */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = Array.from(e.dataTransfer.files)[0];
          if (file) acceptFile(file);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center gap-1.5 rounded-xl border border-dashed border-border bg-secondary/30 px-4 py-5 text-center transition-colors hover:border-primary/50 hover:bg-primary/[0.04]",
          dragging && "border-primary/70 bg-primary/[0.07]"
        )}
      >
        <UploadCloud className="size-5 text-muted-foreground" />
        <div className="text-xs text-muted-foreground">
          上传要克隆的<span className="text-foreground">声音样本</span>（mp3 / wav）
        </div>
        <div className="text-[10px] text-muted-foreground/70">
          建议 10~30 秒清晰人声，无背景音乐，≤{MAX_SAMPLE_MB}MB
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".mp3,.wav,audio/mpeg,audio/wav"
          className="hidden"
          onChange={(e) => {
            const file = Array.from(e.target.files ?? [])[0];
            if (file) acceptFile(file);
            e.target.value = "";
          }}
        />
      </div>

      {/* 待保存样本：先试听，起名后固化 */}
      {pendingFile && (
        <div className="space-y-2 rounded-xl border border-primary/30 bg-primary/[0.05] p-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={togglePreview}
              className="accent-gradient flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full text-white transition-transform hover:scale-105"
            >
              {previewPlaying ? <Pause className="size-3.5" /> : <Play className="ml-0.5 size-3.5" />}
            </button>
            <div className="min-w-0 flex-1">
              <Waveform
                src={pendingUrl}
                progress={previewDuration > 0 ? previewTime / previewDuration : 0}
                height={30}
                onSeek={seekPreview}
              />
            </div>
          </div>
          <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span className="truncate" title={pendingFile.name}>
              {pendingFile.name}
            </span>
            <span className="shrink-0">{fmtMB(pendingFile.size)}</span>
          </div>
          <div className="flex gap-2">
            <Input
              value={pendingName}
              onChange={(e) => setPendingName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !saving && void saveClone()}
              placeholder="克隆音色名称，如：我的声音"
              className="h-8 text-xs"
              autoFocus
            />
            <Button size="sm" className="h-8" disabled={saving} onClick={() => void saveClone()}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : "保存为克隆音色"}
            </Button>
          </div>
          {isHiggs && (
            <div className="space-y-1">
              <Textarea
                value={pendingRefText}
                onChange={(e) => setPendingRefText(e.target.value)}
                placeholder="样本逐字稿（Higgs 本地克隆必填）：样本里念了什么就写什么，一字不差"
                className="min-h-[56px] resize-none text-xs"
                rows={3}
              />
              <p className="text-[10px] leading-relaxed text-muted-foreground/70">
                逐字稿与样本对得越准，克隆出的音色越像。左侧「情绪 / 语速」与正文标签仍可正常叠加使用。
              </p>
            </div>
          )}
          {saving && syncMinimax && hasMinimaxKey ? (
            <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              正在同步克隆到 MiniMax（需分析样本，约半分钟）…
            </div>
          ) : hasMinimaxKey ? (
            <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
              <input
                type="checkbox"
                checked={syncMinimax}
                disabled={saving}
                onChange={(e) => setSyncMinimax(e.target.checked)}
                className="size-3.5 shrink-0 accent-primary"
              />
              同时克隆到 MiniMax（speech 系列模型可用，语速/情感为真实参数）
            </label>
          ) : (
            <p className="text-[10px] leading-relaxed text-muted-foreground/70">
              {isHiggs
                ? "带逐字稿的样本将用于 Higgs 本地克隆。添加 MiniMax Key（设置 → API Keys）后可同步克隆到 speech 系列模型。"
                : "仅保存小米样本克隆。添加 MiniMax Key（设置 → API Keys）后可同步克隆，speech 系列模型即可以真实参数使用该音色。"}
            </p>
          )}
          <audio
            ref={previewAudioRef}
            src={pendingUrl}
            onPlay={() => setPreviewPlaying(true)}
            onPause={() => setPreviewPlaying(false)}
            onEnded={() => {
              setPreviewPlaying(false);
              setPreviewTime(0);
            }}
            onTimeUpdate={(e) => setPreviewTime(e.currentTarget.currentTime)}
            onLoadedMetadata={(e) => setPreviewDuration(e.currentTarget.duration)}
            className="hidden"
          />
        </div>
      )}

      {/* 已有克隆音色列表 */}
      <div className="space-y-1.5">
        <div className="text-xs font-medium text-muted-foreground">
          {clones.length > 0 ? `我的克隆音色（${clones.length}）` : "还没有克隆音色"}
        </div>
        {clones.length === 0 && (
          <div className="flex items-center gap-2 rounded-lg border border-dashed border-border px-3 py-2.5 text-[11px] text-muted-foreground">
            <Mic className="size-3.5 shrink-0" />
            上传一段自己的声音，保存后即可用它朗读任意文案
          </div>
        )}
        {/* 列表限高滚动：条目多时不把下方的朗读参数挤出视口 */}
        {clones.length > 0 && (
          <div className="max-h-72 space-y-1.5 overflow-y-auto pr-0.5">
            {clones.map((a) => (
              <CloneSampleRow
                key={a.id}
                asset={a}
                selected={selected === a.id}
                playingId={playingId}
                onPlayingChange={setPlayingId}
                onSelect={() => onSelect(a)}
                onRetryMinimax={hasMinimaxKey ? () => void retryMinimax(a) : undefined}
                retrying={retryingId === a.id}
                onDelete={() => onDelete(a)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
