"use client";

import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function GenerationButton({
  loading = false,
  loadingLabel = "Generating...",
  children,
  className,
  variant = "accent",
  size = "lg",
  ...props
}: React.ComponentProps<typeof Button> & { loading?: boolean; loadingLabel?: string }) {
  return (
    <Button
      variant={variant}
      size={size}
      disabled={loading || props.disabled}
      className={cn("w-full font-medium", className)}
      {...props}
    >
      {loading && <Loader2 className="size-4 animate-spin" />}
      {loading ? loadingLabel : children}
    </Button>
  );
}
