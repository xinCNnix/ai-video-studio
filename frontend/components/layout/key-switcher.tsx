"use client";

/**
 * 顶栏 API Key 快速切换器：按厂商分组列出全部凭据，点选即把该厂商的默认 Key
 * 切换过去（等价于 设置 → API Keys → 设为默认，但一键完成）。
 * 切换立即生效于该厂商的 LLM / 生图 / 视频 / TTS 调用；
 * 若「默认文本模型」绑定的凭据属于同一厂商，会一并改绑到新 Key，保持模型不变。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronsUpDown, Check, KeyRound, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useAppStore } from "@/stores/app-store";
import type { Credential } from "@/lib/types";
import { cn } from "@/lib/utils";

export function KeySwitcher() {
  const credentials = useAppStore((s) => s.credentials);
  const catalog = useAppStore((s) => s.catalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const [open, setOpen] = useState(false);
  const [switchingId, setSwitchingId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void loadCredentials();
    void loadCatalog();
  }, [loadCredentials, loadCatalog]);

  useEffect(() => {
    if (!open) return;
    // 每次打开都强制刷新：保证显示的默认 Key 与后端 providers.enc 实时一致
    void loadCredentials();
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open, loadCredentials]);

  const providerLabel = (provider: string) =>
    catalog?.providers.find((p) => p.id === provider)?.label ?? provider;

  // 按厂商分组；多 Key 的厂商排前面（可切换），单 Key 的在后
  const groups = useMemo(() => {
    const map = new Map<string, Credential[]>();
    for (const c of credentials) {
      const list = map.get(c.provider) ?? [];
      list.push(c);
      map.set(c.provider, list);
    }
    return [...map.entries()]
      .map(([provider, keys]) => ({ provider, keys }))
      .sort((a, b) => b.keys.length - a.keys.length);
  }, [credentials]);

  // 有多个 Key 的厂商数（用于按钮角标，提示哪些厂商可切换）
  const switchableCount = groups.filter((g) => g.keys.length > 1).length;

  const pick = async (key: Credential) => {
    if (switchingId) return;
    const wasDefault = key.is_default;
    if (wasDefault) {
      setOpen(false);
      return;
    }
    setSwitchingId(key.id);
    try {
      await api.setDefaultCredential(key.id);
      // 「默认文本模型」若绑定的是同厂商旧 Key，一并改绑到新 Key（模型不变）
      const { default_llm } = await api.getDefaultLlm();
      const bound = default_llm?.credential_id;
      if (bound && bound !== key.id) {
        const boundCred = credentials.find((c) => c.id === bound);
        if (boundCred && boundCred.provider === key.provider) {
          await api.setDefaultLlm({ model: default_llm!.model, credential_id: key.id });
        }
      }
      await loadCredentials();
      toast.success(`${providerLabel(key.provider)} 默认 Key 已切换 → ${key.name}`);
      setOpen(false);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSwitchingId(null);
    }
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title="切换各厂商默认 API Key（立即生效）"
        className={cn(
          "flex h-8 cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 text-xs transition-colors",
          open
            ? "border-primary/50 bg-primary/10 text-primary"
            : "border-border bg-secondary/50 text-muted-foreground hover:text-foreground"
        )}
      >
        <KeyRound className="size-3.5" strokeWidth={1.8} />
        <span className="hidden sm:inline">API Key</span>
        {switchableCount > 0 && (
          <span className="rounded-full bg-primary/15 px-1.5 text-[10px] font-medium text-primary">
            {switchableCount}
          </span>
        )}
        <ChevronsUpDown className="size-3.5" />
      </button>

      {open && (
        <div className="anim-pop absolute right-0 z-50 mt-2 w-80 overflow-hidden rounded-lg border border-border bg-[#171923] shadow-xl">
          <div className="border-b border-border px-3 py-2 text-[11px] font-medium text-muted-foreground">
            切换厂商默认 Key · 立即生效
          </div>
          <div className="max-h-[60vh] overflow-y-auto p-1.5">
            {groups.length === 0 && (
              <div className="px-2 py-6 text-center text-xs text-muted-foreground">
                尚未配置 Key，请到 设置 → API Keys 添加
              </div>
            )}
            {groups.map((g) => (
              <div key={g.provider} className="mb-1 last:mb-0">
                <div className="flex items-center justify-between px-2 pb-0.5 pt-1.5">
                  <span className="text-[11px] font-semibold text-foreground/80">
                    {providerLabel(g.provider)}
                  </span>
                  {g.keys.length > 1 && (
                    <span className="text-[10px] text-muted-foreground">{g.keys.length} 把 Key</span>
                  )}
                </div>
                {g.keys.map((k) => (
                  <div
                    key={k.id}
                    onClick={() => void pick(k)}
                    title={k.is_default ? "当前默认" : `点击切换为默认（${k.masked_key}）`}
                    className={cn(
                      "flex cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm",
                      k.is_default ? "text-primary" : "text-foreground hover:bg-white/[0.06]"
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-[13px]">{k.name}</span>
                      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{k.masked_key}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      {switchingId === k.id ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        k.is_default && <Check className="size-3.5" />
                      )}
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
          <div className="border-t border-border px-3 py-1.5 text-[10px] text-muted-foreground">
            影响该厂商的 LLM · 生图 · 视频 · TTS 调用；默认文本模型随同厂商自动改绑
          </div>
        </div>
      )}
    </div>
  );
}
