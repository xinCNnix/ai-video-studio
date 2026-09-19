"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AudioLines,
  Clapperboard,
  Home,
  Image as ImageIcon,
  LayoutGrid,
  Settings,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Home", icon: Home },
  { href: "/prompt", label: "Prompt", icon: Sparkles },
  { href: "/image", label: "Image", icon: ImageIcon },
  { href: "/voice", label: "Voice", icon: AudioLines },
  { href: "/video", label: "Video", icon: Clapperboard },
  { href: "/studio", label: "Studio", icon: LayoutGrid },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside
      className={cn(
        "group/sidebar fixed inset-y-0 left-0 z-40 flex w-[72px] flex-col border-r border-border bg-[#0d0e13]/95 backdrop-blur transition-[width] duration-200 ease-out hover:w-[190px] hover:shadow-[8px_0_32px_rgba(0,0,0,0.45)]"
      )}
    >
      {/* Logo */}
      <div className="flex h-14 shrink-0 items-center gap-2.5 overflow-hidden px-[22px]">
        <div className="accent-gradient flex size-7 shrink-0 items-center justify-center rounded-lg text-sm font-bold text-white shadow-[0_2px_12px_rgba(124,92,255,0.4)]">
          ✦
        </div>
        <span className="whitespace-nowrap text-[13px] font-semibold tracking-wide opacity-0 transition-opacity duration-200 group-hover/sidebar:opacity-100">
          AI Video Studio
        </span>
      </div>

      {/* 导航 */}
      <nav className="mt-2 flex flex-1 flex-col gap-1 overflow-hidden px-3">
        {NAV_ITEMS.map((item) => {
          const active = pathname === item.href;
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex h-10 items-center gap-3 overflow-hidden rounded-lg px-3 text-[13px] font-medium transition-colors",
                active
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:bg-white/[0.05] hover:text-foreground"
              )}
            >
              <Icon
                className={cn("size-5 shrink-0", active && "text-primary")}
                strokeWidth={active ? 2.2 : 1.8}
              />
              <span className="whitespace-nowrap opacity-0 transition-opacity duration-200 group-hover/sidebar:opacity-100">
                {item.label}
              </span>
            </Link>
          );
        })}
      </nav>

      {/* 底部设置 */}
      <div className="shrink-0 px-3 pb-4">
        <Link
          href="/settings"
          className={cn(
            "flex h-10 items-center gap-3 overflow-hidden rounded-lg px-3 text-[13px] font-medium transition-colors",
            pathname === "/settings"
              ? "bg-secondary text-foreground"
              : "text-muted-foreground hover:bg-white/[0.05] hover:text-foreground"
          )}
        >
          <Settings className="size-5 shrink-0" strokeWidth={1.8} />
          <span className="whitespace-nowrap opacity-0 transition-opacity duration-200 group-hover/sidebar:opacity-100">
            Settings
          </span>
        </Link>
      </div>
    </aside>
  );
}
