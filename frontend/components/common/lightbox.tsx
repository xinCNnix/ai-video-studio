"use client";

import { useEffect } from "react";
import { Download, X } from "lucide-react";

export function Lightbox({
  open,
  onClose,
  src,
  type = "image",
}: {
  open: boolean;
  onClose: () => void;
  src: string | null;
  type?: "image" | "video";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !src) return null;

  return (
    <div
      className="anim-overlay fixed inset-0 z-[60] flex items-center justify-center bg-black/85 p-8"
      onClick={onClose}
    >
      <div className="absolute right-5 top-5 flex items-center gap-2">
        <a
          href={`${src.split("#")[0]}?dl=1`}
          download
          target="_blank"
          rel="noreferrer"
          title="下载"
          onClick={(e) => e.stopPropagation()}
          className="rounded-full bg-white/10 p-2 text-white hover:bg-white/20"
        >
          <Download className="size-5" />
        </a>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full bg-white/10 p-2 text-white hover:bg-white/20 cursor-pointer"
        >
          <X className="size-5" />
        </button>
      </div>
      {type === "video" ? (
        <video
          src={src}
          controls
          autoPlay
          onClick={(e) => e.stopPropagation()}
          className="max-h-full max-w-full rounded-xl border border-border bg-black shadow-2xl"
        />
      ) : (
        /* eslint-disable-next-line @next/next/no-img-element */
        <img
          src={src}
          alt="preview"
          onClick={(e) => e.stopPropagation()}
          className="max-h-full max-w-full rounded-xl border border-border object-contain shadow-2xl"
        />
      )}
    </div>
  );
}
