"use client";

import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon,
  title,
  hint,
  className,
  children,
}: {
  icon: LucideIcon;
  title: string;
  hint?: string;
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border/70 bg-secondary/20 px-6 py-14 text-center",
        className
      )}
    >
      <div className="flex size-12 items-center justify-center rounded-xl bg-secondary">
        <Icon className="size-6 text-muted-foreground" />
      </div>
      <div className="text-sm font-medium text-foreground/90">{title}</div>
      {hint && <div className="max-w-xs text-xs leading-relaxed text-muted-foreground">{hint}</div>}
      {children}
    </div>
  );
}
