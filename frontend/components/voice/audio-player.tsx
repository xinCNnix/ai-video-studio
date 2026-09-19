"use client";

import { useEffect, useRef, useState } from "react";
import { Download, Pause, Play } from "lucide-react";
import { fileUrl } from "@/lib/api";
import { cn, fmtTime } from "@/lib/utils";
import { Waveform } from "./waveform";

export function AudioPlayer({
  src,
  title,
  onSendToStudio,
  className,
}: {
  src: string;
  title?: string;
  onSendToStudio?: () => void;
  className?: string;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    setPlaying(false);
    setTime(0);
  }, [src]);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
    } else {
      void audio.play();
    }
  };

  const seekFraction = (f: number) => {
    const audio = audioRef.current;
    if (!audio || !duration) return;
    audio.currentTime = f * duration;
    setTime(audio.currentTime);
  };

  const progress = duration > 0 ? time / duration : 0;

  return (
    <div className={cn("rounded-xl border border-border bg-card p-5", className)}>
      {title && <div className="mb-3 truncate text-xs text-muted-foreground">{title}</div>}
      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={toggle}
          className="accent-gradient flex size-12 shrink-0 cursor-pointer items-center justify-center rounded-full text-white shadow-[0_4px_16px_rgba(124,92,255,0.35)] transition-transform hover:scale-105"
        >
          {playing ? <Pause className="size-5" /> : <Play className="ml-0.5 size-5" />}
        </button>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <Waveform src={src} progress={progress} height={44} onSeek={seekFraction} />
          <div className="flex items-center justify-between text-[11px] tabular-nums text-muted-foreground">
            <span>{fmtTime(time)}</span>
            <span>{fmtTime(duration)}</span>
          </div>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 border-t border-border pt-3">
        <a
          href={`${fileUrl(src)}?dl=1`}
          download
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <Download className="size-3.5" />
          Download
        </a>
        {onSendToStudio && (
          <button
            type="button"
            onClick={onSendToStudio}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs text-primary transition-colors hover:bg-primary/20"
          >
            Send to Studio
          </button>
        )}
      </div>

      <audio
        ref={audioRef}
        src={fileUrl(src)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        className="hidden"
      />
    </div>
  );
}
