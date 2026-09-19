"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { Settings, Sparkles } from "lucide-react";
import Link from "next/link";
import { useAppStore } from "@/stores/app-store";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { KeySwitcher } from "@/components/layout/key-switcher";

const TITLES: Record<string, string> = {
  "/": "Home",
  "/prompt": "AI Prompt",
  "/image": "AI Image",
  "/voice": "AI Voice",
  "/video": "AI Video",
  "/studio": "Studio",
  "/settings": "Settings",
};

export function Topbar() {
  const pathname = usePathname();
  const backendOk = useAppStore((s) => s.backendOk);
  const setBackendOk = useAppStore((s) => s.setBackendOk);
  const agentOpen = useAppStore((s) => s.agentOpen);
  const setAgentOpen = useAppStore((s) => s.setAgentOpen);

  useEffect(() => {
    const check = () =>
      api
        .health()
        .then(() => setBackendOk(true))
        .catch(() => setBackendOk(false));
    check();
    const timer = setInterval(check, 30000);
    return () => clearInterval(timer);
  }, [setBackendOk]);

  return (
    <header className="fixed left-[72px] right-0 top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-[#0b0c10]/80 px-6 backdrop-blur">
      <div className="text-sm font-semibold tracking-wide">{TITLES[pathname] ?? "AI Video Studio"}</div>
      <div className="flex items-center gap-3">
        <KeySwitcher />
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span
            className={cn(
              "size-2 rounded-full",
              backendOk === true && "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]",
              backendOk === false && "bg-red-400",
              backendOk === null && "bg-zinc-500 animate-pulse"
            )}
          />
          {backendOk === false ? "后端未连接" : "API"}
        </div>
        <button
          type="button"
          onClick={() => setAgentOpen(!agentOpen)}
          title={agentOpen ? "收起 AI 助手" : "展开 AI 助手"}
          className={cn(
            "flex cursor-pointer items-center gap-1.5 rounded-md p-1.5 transition-colors hover:bg-white/[0.06]",
            agentOpen ? "text-primary" : "text-muted-foreground hover:text-foreground"
          )}
        >
          <Sparkles className="size-4.5" strokeWidth={1.8} />
          <span className="hidden text-xs md:inline">AI 助手</span>
        </button>
        <Link
          href="/settings"
          className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground"
        >
          <Settings className="size-4.5" strokeWidth={1.8} />
        </Link>
      </div>
    </header>
  );
}
