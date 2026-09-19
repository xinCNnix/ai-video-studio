"use client";

/**
 * 记住每个能力域上一次选择的模型，页面无需每次重新选择。
 * 存储键：asm:<capability> -> model id
 */
export function loadModelPref(capability: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(`asm:${capability}`) ?? "";
  } catch {
    return "";
  }
}

export function saveModelPref(capability: string, modelId: string) {
  try {
    window.localStorage.setItem(`asm:${capability}`, modelId);
  } catch {
    /* ignore */
  }
}

/** 最近使用列表（最多 5 条），用于下拉置顶。 */
export function getRecent(key: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(`asm:recent:${key}`);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((v) => typeof v === "string") : [];
  } catch {
    return [];
  }
}

export function pushRecent(key: string, value: string) {
  const list = getRecent(key).filter((v) => v !== value);
  list.unshift(value);
  try {
    window.localStorage.setItem(`asm:recent:${key}`, JSON.stringify(list.slice(0, 5)));
  } catch {
    /* ignore */
  }
}
