"use client";

import { create } from "zustand";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** 一次「清空对话」产生的存档：仅供回看，绝不作为 Agent 上下文喂回。 */
export interface ChatArchive {
  id: string;
  archived_at: string; // ISO 时间
  messages: ChatMessage[];
}

const STORAGE_KEY = "agent_chat_v1";
const ARCHIVE_KEY = "agent_chat_archives_v1";
const MAX_PERSIST = 200;
const MAX_ARCHIVES = 20;

function load(): ChatMessage[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const data = JSON.parse(raw) as { messages?: ChatMessage[] };
    return Array.isArray(data.messages) ? data.messages.slice(-MAX_PERSIST) : [];
  } catch {
    return [];
  }
}

function save(messages: ChatMessage[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ messages: messages.slice(-MAX_PERSIST) }));
  } catch {
    /* 存储满等异常时静默放弃，不影响聊天 */
  }
}

function loadArchives(): ChatArchive[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(ARCHIVE_KEY);
    if (!raw) return [];
    const data = JSON.parse(raw) as { archives?: ChatArchive[] };
    return Array.isArray(data.archives) ? data.archives.slice(0, MAX_ARCHIVES) : [];
  } catch {
    return [];
  }
}

function saveArchives(archives: ChatArchive[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ARCHIVE_KEY, JSON.stringify({ archives: archives.slice(0, MAX_ARCHIVES) }));
  } catch {
    /* 存储满时放弃最旧的存档也值得记录，但聊天功能不受影响 */
  }
}

interface AgentChatState {
  messages: ChatMessage[];
  archives: ChatArchive[];
  addMessage: (m: ChatMessage) => void;
  clear: () => void;
  /** 清空对话 = /reset：当前对话存档（可在存档面板回看），Agent 记忆彻底归零。 */
  archiveAndClear: () => ChatArchive | null;
  deleteArchive: (id: string) => void;
}

/**
 * Agent 聊天记录全局 store：写入 localStorage，
 * 刷新页面后对话仍在，「清空对话」= 先存档再清空（Agent 上下文随之归零，
 * 因为每次运行只携带前端传的 history，没有服务端会话状态）。
 */
export const useAgentChatStore = create<AgentChatState>()((set, get) => ({
  messages: [],
  archives: [],
  addMessage: (m) => {
    const messages = [...get().messages, m];
    save(messages);
    set({ messages });
  },
  clear: () => {
    save([]);
    set({ messages: [] });
  },
  archiveAndClear: () => {
    const messages = get().messages;
    let archive: ChatArchive | null = null;
    if (messages.length > 0) {
      archive = {
        id: `arch_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
        archived_at: new Date().toISOString(),
        messages: messages.slice(-MAX_PERSIST),
      };
      const archives = [archive, ...loadArchives()].slice(0, MAX_ARCHIVES);
      saveArchives(archives);
      set({ archives });
    }
    save([]);
    set({ messages: [] });
    return archive;
  },
  deleteArchive: (id) => {
    const archives = loadArchives().filter((a) => a.id !== id);
    saveArchives(archives);
    set({ archives });
  },
}));

/** 仅在客户端执行一次：把 localStorage 里的历史与存档灌进 store。 */
export function hydrateAgentChat() {
  if (useAgentChatStore.getState().messages.length === 0) {
    const messages = load();
    if (messages.length > 0) useAgentChatStore.setState({ messages });
  }
  if (useAgentChatStore.getState().archives.length === 0) {
    const archives = loadArchives();
    if (archives.length > 0) useAgentChatStore.setState({ archives });
  }
}
