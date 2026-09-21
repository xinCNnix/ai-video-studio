"use client";

/** 素材缩略图：左上角上传序号角标（对号 <Image N> 的依据）+ 点击放大预览。
 * 素材文件名多为哈希/UUID，对用户不可读——缩略图与序号才是核对绑定的依据。 */
import { useState } from "react";
import { fileUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

export function MaterialThumb({
  src,
  badge,
  title,
  className,
}: {
  src: string;
  badge?: string;
  title?: string;
  className?: string;
}) {
  const [zoom, setZoom] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setZoom(true)}
        title={title}
        className={cn(
          "relative block size-14 shrink-0 cursor-zoom-in overflow-hidden rounded-lg border border-border bg-[#0b0c10]",
          className,
        )}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={fileUrl(src)} alt={title ?? ""} loading="lazy" className="size-full object-cover" />
        {badge && (
          <span className="absolute left-0.5 top-0.5 rounded bg-black/70 px-1 text-[9px] font-semibold leading-4 text-white">
            {badge}
          </span>
        )}
      </button>
      {zoom && (
        <div
          className="fixed inset-0 z-50 flex cursor-zoom-out items-center justify-center bg-black/80 p-6"
          onClick={() => setZoom(false)}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={fileUrl(src)} alt={title ?? ""} className="max-h-[85vh] max-w-[85vw] rounded-lg object-contain shadow-2xl" />
        </div>
      )}
    </>
  );
}
