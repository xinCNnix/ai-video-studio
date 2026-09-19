"use client";

import { useEffect, useRef, useState } from "react";
import { fileUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

/** src → 归一化峰值（0..1），同一音频在播放器/克隆列表间复用，避免重复解码 */
const peaksCache = new Map<string, number[]>();

async function decodePeaks(src: string, buckets: number): Promise<number[]> {
  const cached = peaksCache.get(src);
  if (cached) return cached;
  const res = await fetch(fileUrl(src));
  if (!res.ok) throw new Error(`fetch ${res.status}`);
  const buf = await res.arrayBuffer();
  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) throw new Error("Web Audio 不可用");
  const ctx = new Ctor();
  try {
    const audio = await ctx.decodeAudioData(buf);
    const data = audio.getChannelData(0);
    const size = Math.max(1, Math.floor(data.length / buckets));
    const peaks: number[] = [];
    for (let i = 0; i < buckets; i++) {
      let max = 0;
      const start = i * size;
      const end = Math.min(data.length, start + size);
      // 抽样取峰：长音频每桶最多看 ~256 个采样点，解码后遍历仍是 O(n) 但常数可控
      const stride = Math.max(1, Math.floor((end - start) / 256));
      for (let j = start; j < end; j += stride) {
        const v = Math.abs(data[j]);
        if (v > max) max = v;
      }
      peaks.push(max);
    }
    const top = Math.max(...peaks, 0.01);
    const norm = peaks.map((v) => Math.min(1, v / top));
    peaksCache.set(src, norm);
    return norm;
  } finally {
    void ctx.close();
  }
}

/** 解码失败时的占位波形：由 src 哈希生成的确定性形状，保持界面可用（仍可点击寻位） */
function pseudoPeaks(src: string, buckets: number): number[] {
  let h = 0;
  for (let i = 0; i < src.length; i++) h = (h * 31 + src.charCodeAt(i)) >>> 0;
  return Array.from({ length: buckets }, (_, i) => {
    const v = Math.sin(i * 0.35 + h) * 0.25 + Math.sin(i * 0.11 + (h % 97)) * 0.2 + 0.45;
    return Math.min(1, Math.max(0.12, v));
  });
}

export function Waveform({
  src,
  /** 播放进度 0..1，之前的波形高亮 */
  progress = 0,
  height = 48,
  /** 提供后支持点击/拖拽寻位 */
  onSeek,
  className,
}: {
  src: string;
  progress?: number;
  height?: number;
  onSeek?: (fraction: number) => void;
  className?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPeaks(peaksCache.get(src) ?? null);
    setFailed(false);
    decodePeaks(src, 240)
      .then((p) => {
        if (!cancelled) setPeaks(p);
      })
      .catch(() => {
        if (!cancelled) {
          setPeaks(pseudoPeaks(src, 240));
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [src]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width || !peaks?.length) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const barW = 2;
    const gap = 2;
    const bars = Math.max(1, Math.floor(width / (barW + gap)));
    const mid = height / 2;
    const playedX = progress * width;
    for (let i = 0; i < bars; i++) {
      // 波形桶 → 屏幕柱的最近邻映射；桶数固定，任意宽度下形状一致
      const p = peaks[Math.round((i / Math.max(1, bars - 1)) * (peaks.length - 1))] ?? 0;
      const barH = Math.max(2, p * (height - 4));
      const x = i * (barW + gap);
      ctx.fillStyle = x + barW <= playedX ? "#7c5cff" : "rgba(255,255,255,0.18)";
      ctx.beginPath();
      ctx.roundRect(x, mid - barH / 2, barW, barH, 1);
      ctx.fill();
    }
  }, [peaks, width, height, progress]);

  const seekFromEvent = (e: React.PointerEvent) => {
    const el = wrapRef.current;
    if (!el || !onSeek) return;
    const rect = el.getBoundingClientRect();
    onSeek(Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)));
  };

  return (
    <div
      ref={wrapRef}
      className={cn("relative w-full select-none", onSeek && "cursor-pointer", className)}
      style={{ height }}
      onPointerDown={(e) => {
        if (!onSeek) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        seekFromEvent(e);
      }}
      onPointerMove={(e) => {
        if (onSeek && e.buttons === 1) seekFromEvent(e);
      }}
    >
      {!peaks && (
        <div className="absolute inset-0 flex items-center gap-[2px] overflow-hidden opacity-40">
          {Array.from({ length: 60 }).map((_, i) => (
            <div
              key={i}
              className="w-[2px] shrink-0 animate-pulse rounded-full bg-white/25"
              style={{ height: `${25 + ((i * 37) % 55)}%` }}
            />
          ))}
        </div>
      )}
      <canvas ref={canvasRef} style={{ width: "100%", height }} className={cn(failed && "opacity-50")} />
    </div>
  );
}
