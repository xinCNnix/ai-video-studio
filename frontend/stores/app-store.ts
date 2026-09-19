"use client";

import { create } from "zustand";
import { api } from "@/lib/api";
import type { Credential, ModelCatalog } from "@/lib/types";

export interface PendingVideo {
  prompt?: string;
  mode?: "text" | "image" | "reference" | "first_last";
  images?: string[];
}

interface AppState {
  catalog: ModelCatalog | null;
  credentials: Credential[];
  /** 从凭据 /models、get_voice 动态发现的模型与音色（按厂商分组） */
  discovered: Record<string, string[]>;
  discoveredTts: Record<string, string[]>;
  discoveredVoices: Record<string, { id: string; label: string; source: string }[]>;
  /** 音色发现失败的原因（如缺少 Group ID） */
  voiceErrors: { provider: string; name: string; message: string }[];
  backendOk: boolean | null;
  agentOpen: boolean;
  /** Agent 任务执行中（跨页面共享，图片页据此开启历史轮询） */
  agentBusy: boolean;
  pendingVideo: PendingVideo | null;
  pendingVoice: string | null;

  loadCatalog: () => Promise<void>;
  loadCredentials: () => Promise<void>;
  loadDiscovered: () => Promise<void>;
  setBackendOk: (ok: boolean | null) => void;
  setAgentOpen: (open: boolean) => void;
  setAgentBusy: (busy: boolean) => void;
  setPendingVideo: (v: PendingVideo | null) => void;
  setPendingVoice: (url: string | null) => void;
}

export const useAppStore = create<AppState>()((set, get) => ({
  catalog: null,
  credentials: [],
  discovered: {},
  discoveredTts: {},
  discoveredVoices: {},
  voiceErrors: [],
  backendOk: null,
  agentOpen: false,
  agentBusy: false,
  pendingVideo: null,
  pendingVoice: null,

  loadCatalog: async () => {
    try {
      const catalog = await api.getCatalog();
      set({ catalog, backendOk: true });
    } catch {
      set({ backendOk: false });
    }
  },
  loadCredentials: async () => {
    try {
      const { credentials } = await api.getCredentials();
      set({ credentials });
      if (credentials.length) void get().loadDiscovered();
    } catch {
      /* 后端未启动时静默 */
    }
  },
  loadDiscovered: async () => {
    try {
      const { discovered, discovered_tts, voices, voice_errors } = await api.getDiscoveredModels();
      set({ discovered, discoveredTts: discovered_tts ?? {}, discoveredVoices: voices, voiceErrors: voice_errors ?? [] });
      // 合并进 catalog，让所有模型/音色选择器直接拿到最新列表
      const cat = get().catalog;
      if (!cat) return;
      const mergeModels = (base: ModelInfoList, provider: string): ModelInfoList => {
        const seen = new Set(base.map((m) => `${m.provider}:${m.id}`));
        const extra = (discovered[provider] ?? []).map((id) => ({ id, label: id, provider }));
        return [...base, ...extra.filter((e) => !seen.has(`${e.provider}:${e.id}`))];
      };
      let llm = cat.llm;
      let image = cat.image;
      for (const p of cat.providers) {
        if (p.capabilities.includes("llm")) llm = mergeModels(llm, p.id);
        if (p.capabilities.includes("image")) image = mergeModels(image, p.id);
      }
      const seenVoices = new Set(cat.tts_voices.map((v) => v.id));
      const extraVoices = Object.values(voices)
        .flat()
        .filter((v) => !seenVoices.has(v.id))
        .map((v) => ({ id: v.id, label: v.label }));
      // 发现的 TTS 模型（如 MiniMax mimo-tts 系列）合并进语音模型选择器
      const seenTts = new Set(cat.tts.map((m) => `${m.provider}:${m.id}`));
      const extraTtsModels = Object.entries(discovered_tts ?? {}).flatMap(([provider, ids]) =>
        ids
          .filter((id) => !seenTts.has(`${provider}:${id}`))
          .map((id) => ({ id, label: id, provider }))
      );
      set({
        catalog: {
          ...cat,
          llm,
          image,
          tts: [...cat.tts, ...extraTtsModels],
          tts_voices: [...cat.tts_voices, ...extraVoices],
        },
      });
    } catch {
      /* 发现失败不影响使用 */
    }
  },
  setBackendOk: (ok) => set({ backendOk: ok }),
  setAgentOpen: (open) => {
    set({ agentOpen: open });
    try {
      window.localStorage.setItem("agentOpen", open ? "1" : "0");
    } catch {
      /* ignore */
    }
  },
  setAgentBusy: (busy) => set({ agentBusy: busy }),
  setPendingVideo: (v) => set({ pendingVideo: v }),
  setPendingVoice: (url) => set({ pendingVoice: url }),
}));

type ModelInfoList = ModelCatalog["llm"];
