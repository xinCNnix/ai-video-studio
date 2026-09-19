"use client";

/**
 * 可搜索下拉选择：搜索过滤 + 字母排序 + 最近使用置顶 + 键盘导航。
 * 用于音色（300+）和模型（发现列表可能很长）等大列表场景。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronsUpDown, Pencil, Search, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SearchableItem {
  value: string;
  label: string;
  hint?: string;
}

export function SearchableSelect({
  items,
  value,
  onChange,
  onDelete,
  onRename,
  placeholder = "请选择",
  searchPlaceholder = "搜索…",
  recentValues,
  className,
  emptyText = "无匹配项",
  allowCustom = false,
  customLabelPrefix = "使用自定义描述",
}: {
  items: SearchableItem[];
  value: string;
  onChange: (v: string) => void;
  /** 可选：每项右侧显示删除按钮（下拉保持打开，方便连续删除多项） */
  onDelete?: (value: string) => void;
  /** 可选：每项右侧显示改名按钮，点击后行内编辑（回车/失焦提交，Esc 取消） */
  onRename?: (value: string, name: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  recentValues?: string[];
  className?: string;
  emptyText?: string;
  /** 允许把搜索词直接作为值提交（voicedesign 的音色描述等自由输入场景） */
  allowCustom?: boolean;
  /** 自定义项的前缀文案（如音色 ID 场景用“使用自定义音色 ID”） */
  customLabelPrefix?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [renaming, setRenaming] = useState<{ value: string; draft: string } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const recentSet = useMemo(() => new Set(recentValues ?? []), [recentValues]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const base = q
      ? items.filter((i) => i.label.toLowerCase().includes(q) || i.value.toLowerCase().includes(q))
      : items;
    return [...base].sort((a, b) => a.label.localeCompare(b.label, "zh-Hans-CN", { numeric: true }));
  }, [items, query]);
  const recentFiltered = useMemo(
    () =>
      (recentValues ?? [])
        .map((v) => filtered.find((i) => i.value === v))
        .filter(Boolean) as SearchableItem[],
    [filtered, recentValues]
  );
  const restFiltered = useMemo(
    () => (recentValues?.length ? filtered.filter((i) => !recentSet.has(i.value)) : filtered),
    [filtered, recentValues, recentSet]
  );
  const custom = useMemo(() => {
    const q = query.trim();
    if (!allowCustom || !q || items.some((i) => i.value === q)) return null;
    return {
      value: q,
      label: `${customLabelPrefix}：${q.length > 20 ? q.slice(0, 20) + "…" : q}`,
      hint: "回车确认",
    };
  }, [allowCustom, customLabelPrefix, query, items]);
  const flat = useMemo(
    () => [...(custom ? [custom] : []), ...recentFiltered, ...restFiltered],
    [custom, recentFiltered, restFiltered]
  );

  useEffect(() => setActive(0), [query, open]);

  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [open, active, query]);

  const select = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  const commitRename = () => {
    if (!renaming) return;
    const name = renaming.draft.trim();
    const item = items.find((i) => i.value === renaming.value);
    if (name && name !== item?.label) onRename?.(renaming.value, name);
    setRenaming(null);
  };

  const onInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, Math.max(0, flat.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const it = flat[active];
      if (it) select(it.value);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  const renderItem = (item: SearchableItem, idx: number) => {
    if (renaming?.value === item.value) {
      return (
        <div
          key={item.value}
          data-idx={idx}
          data-active={idx === active}
          className="flex w-full items-center rounded-md bg-white/[0.08] px-1.5 py-1"
        >
          <input
            autoFocus
            value={renaming.draft}
            maxLength={60}
            title="新名称（回车确认，Esc 取消）"
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => setRenaming({ value: item.value, draft: e.target.value })}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitRename();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setRenaming(null);
              }
            }}
            onBlur={commitRename}
            className="w-full rounded border border-primary/50 bg-secondary/60 px-1.5 py-0.5 text-sm text-foreground focus:outline-none"
          />
        </div>
      );
    }
    return (
      <div
        key={item.value}
        data-idx={idx}
        data-active={idx === active}
        title={item.hint ? `${item.label}（${item.hint}）` : item.label}
        onMouseEnter={() => setActive(idx)}
        onClick={() => select(item.value)}
        className={cn(
          "group flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm",
          idx === active ? "bg-white/[0.08]" : "",
          value === item.value ? "text-primary" : "text-foreground"
        )}
      >
        <span className="truncate">{item.label}</span>
        <span className="flex shrink-0 items-center gap-1.5">
          {item.hint && (
            <span className="rounded-full border border-border bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {item.hint}
            </span>
          )}
          {onRename && (
            <button
              type="button"
              title="改名"
              onClick={(e) => {
                e.stopPropagation();
                setRenaming({ value: item.value, draft: item.label });
              }}
              className="rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:bg-white/10 hover:text-foreground group-hover:opacity-100 cursor-pointer"
            >
              <Pencil className="size-3.5" />
            </button>
          )}
          {onDelete && (
            <button
              type="button"
              title="删除"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(item.value);
              }}
              className="rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:bg-red-500/10 hover:text-red-400 group-hover:opacity-100 cursor-pointer"
            >
              <Trash2 className="size-3.5" />
            </button>
          )}
          {value === item.value && <Check className="size-3.5" />}
        </span>
      </div>
    );
  };

  const selected = items.find((i) => i.value === value);

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={selected ? (selected.hint ? `${selected.label}（${selected.hint}）` : selected.label) : placeholder}
        className="flex h-9 w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-border bg-secondary/50 px-3 py-2 text-sm shadow-sm transition-colors focus:outline-none focus:ring-2 focus:ring-ring"
      >
        <span className={cn("truncate", !value && "text-muted-foreground/60")}>
          {selected ? selected.label : value ? value : placeholder}
        </span>
        <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
      </button>

      {open && (
        <div className="anim-pop absolute z-50 mt-1 w-full overflow-hidden rounded-lg border border-border bg-[#171923] shadow-xl">
          <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
            <Search className="size-3.5 shrink-0 text-muted-foreground" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onInputKeyDown}
              placeholder={searchPlaceholder}
              className="w-full bg-transparent text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none"
            />
          </div>
          <div ref={listRef} className="max-h-64 overflow-y-auto p-1">
            {flat.length === 0 && (
              <div className="px-2 py-6 text-center text-xs text-muted-foreground">{emptyText}</div>
            )}
            {recentFiltered.length > 0 && (
              <>
                <div className="px-2 pb-1 pt-1.5 text-[10px] font-medium text-muted-foreground">最近使用</div>
                {recentFiltered.map((it, i) => renderItem(it, i))}
                <div className="mx-1 my-1 border-t border-border" />
                <div className="px-2 pb-1 pt-1 text-[10px] font-medium text-muted-foreground">全部</div>
              </>
            )}
            {restFiltered.map((it, i) => renderItem(it, recentFiltered.length + i))}
          </div>
          <div className="border-t border-border px-2.5 py-1.5 text-[10px] text-muted-foreground">
            {flat.length} / {items.length} 项
          </div>
        </div>
      )}
    </div>
  );
}
