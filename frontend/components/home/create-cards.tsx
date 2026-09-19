"use client";

import { useRouter } from "next/navigation";
import { AudioLines, Clapperboard, Image as ImageIcon, Sparkles } from "lucide-react";

const CARDS = [
  {
    href: "/prompt",
    icon: Sparkles,
    title: "Prompt",
    desc: "编写视频提示词",
    tint: "from-violet-500/25 to-violet-500/5 text-violet-300",
  },
  {
    href: "/image",
    icon: ImageIcon,
    title: "Image",
    desc: "生成参考图片",
    tint: "from-sky-500/25 to-sky-500/5 text-sky-300",
  },
  {
    href: "/voice",
    icon: AudioLines,
    title: "Voice",
    desc: "生成旁白",
    tint: "from-emerald-500/25 to-emerald-500/5 text-emerald-300",
  },
  {
    href: "/video",
    icon: Clapperboard,
    title: "Video",
    desc: "生成视频",
    tint: "from-fuchsia-500/25 to-fuchsia-500/5 text-fuchsia-300",
  },
];

export function CreateCards() {
  const router = useRouter();
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {CARDS.map((card) => {
        const Icon = card.icon;
        return (
          <button
            key={card.href}
            type="button"
            onClick={() => router.push(card.href)}
            className="group flex cursor-pointer flex-col gap-8 rounded-2xl border border-border bg-card p-5 text-left transition-all hover:-translate-y-1 hover:border-primary/40 hover:shadow-[0_12px_40px_rgba(124,92,255,0.15)]"
          >
            <div
              className={`flex size-11 items-center justify-center rounded-xl bg-gradient-to-br ${card.tint}`}
            >
              <Icon className="size-5" />
            </div>
            <div>
              <div className="text-sm font-semibold transition-colors group-hover:text-white">{card.title}</div>
              <div className="mt-1 text-xs text-muted-foreground">{card.desc}</div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
