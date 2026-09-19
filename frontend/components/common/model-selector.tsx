"use client";

import { ModelInfo, VideoModelInfo } from "@/lib/types";
import { SearchableSelect } from "@/components/common/searchable-select";

export function ModelSelector({
  models,
  value,
  onChange,
  placeholder = "选择模型",
  showProvider = true,
  providerLabels = {},
}: {
  models: (ModelInfo | VideoModelInfo)[];
  value: string;
  onChange: (id: string) => void;
  placeholder?: string;
  showProvider?: boolean;
  providerLabels?: Record<string, string>;
}) {
  const items = models.map((m) => ({
    value: m.id,
    label: m.label,
    hint: showProvider ? providerLabels[m.provider] : undefined,
  }));

  return (
    <SearchableSelect
      items={items}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      searchPlaceholder="搜索模型…"
      emptyText="无匹配模型"
    />
  );
}
