"use client";

import { create } from "zustand";
import { api } from "@/lib/api";
import type { Project, Scene } from "@/lib/types";

function emptyScene(): Scene {
  return {
    id: `scene_${Math.random().toString(36).slice(2, 10)}`,
    title: "新场景",
    description: "",
    prompt: "",
    narration: "",
    image: null,
    voice: null,
    video: null,
    duration: 5,
    ref_images: [],
    ref_videos: [],
    ref_audios: [],
  };
}

/** 生成不与现有项目重名的默认名：重名时追加 “ (2)”、“ (3)”… */
function uniqueName(base: string, existing: { name: string }[]): string {
  const taken = new Set(existing.map((p) => p.name));
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) {
    const candidate = `${base} (${i})`;
    if (!taken.has(candidate)) return candidate;
  }
}

interface StudioState {
  project: Project | null;
  projects: { id: string; name: string; scene_count: number; updated_at?: string }[];
  currentSceneId: string | null;
  saving: boolean;
  /** 最近一次自动保存是否失败（供头部提示，避免 UI 谎报已保存） */
  saveError: boolean;
  loaded: boolean;
  /** 项目加载失败原因（非空时页面显示错误与重试，而不是永远 loading） */
  loadError: string | null;

  loadProjects: () => Promise<void>;
  openProject: (id: string) => Promise<void>;
  createProject: (name?: string) => Promise<string>;
  deleteProject: (id: string) => Promise<void>;
  reloadProject: () => Promise<void>;
  ensureLoaded: () => Promise<void>;
  setCurrent: (id: string) => void;
  addScene: () => void;
  duplicateScene: (id: string) => void;
  deleteScene: (id: string) => void;
  reorderScene: (from: number, to: number) => void;
  updateScene: (id: string, patch: Partial<Scene>) => void;
  renameProject: (name: string) => void;
  /** 按项目 ID 改名；非当前项目时读-改-写，不切换当前打开的项目 */
  renameProjectById: (id: string, name: string) => Promise<void>;
  save: () => Promise<void>;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;

export const useStudioStore = create<StudioState>()((set, get) => {
  const mutate = (fn: (p: Project) => void) => {
    const project = get().project;
    if (!project) return;
    const copy: Project = JSON.parse(JSON.stringify(project));
    fn(copy);
    set({ project: copy });
    scheduleSave();
  };

  const scheduleSave = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => void get().save(), 800);
  };

  // 取消 pending 保存并立即落盘旧项目（切换/重载前调用，防止把旧状态写到新目标或覆盖服务端新数据）
  const flushPendingSave = async () => {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
      await get().save();
    }
  };

  return {
    project: null,
    projects: [],
    currentSceneId: null,
    saving: false,
    saveError: false,
    loaded: false,
    loadError: null,

    loadProjects: async () => {
      try {
        const { projects } = await api.listProjects();
        set({ projects });
      } catch {
        set({ projects: [] });
      }
    },

    openProject: async (id) => {
      // 先把当前项目的未落盘编辑保存，再切换（否则防抖 timer 会把 A 的编辑写进 B）
      const old = get().project;
      if (old && old.id !== id) await flushPendingSave();
      // 加载失败时向上抛：已有项目则由调用方提示并留在原项目，没有项目则进入错误页
      const project = await api.getProject(id);
      if (project.scenes.length === 0) project.scenes = [emptyScene()];
      set({ project, currentSceneId: project.scenes[0].id, loaded: true, loadError: null });
    },

    createProject: async (name) => {
      // 与 openProject 一样先落盘旧项目的防抖编辑，否则 timer 会在切换后保存新项目、丢掉旧项目的修改
      await flushPendingSave();
      const finalName = uniqueName(name?.trim() || "未命名项目", get().projects);
      const project = await api.createProject(finalName);
      project.scenes = [emptyScene()];
      set({ project, currentSceneId: project.scenes[0].id, loaded: true, loadError: null });
      // 立即落盘初始场景并刷新列表（save 内部会 loadProjects），否则列表的 scene_count 一直是 0
      await get().save();
      // 返回最终名称（可能与传入名不同：重名时会被 uniqueName 追加序号），供 UI 明确提示新建结果
      return project.name;
    },

    deleteProject: async (id) => {
      // 趁项目文件还在先落盘未保存的编辑；若删除后才 flush，PUT 会把已删项目在服务端“复活”
      await flushPendingSave();
      try {
        await api.deleteProject(id);
      } catch {
        /* 项目可能已被删除 */
      }
      await get().loadProjects();
      if (get().project?.id !== id) return;
      const next = get().projects.find((p) => p.id !== id);
      if (next) {
        try {
          await get().openProject(next.id);
        } catch (err) {
          set({ project: null, currentSceneId: null, loaded: true, loadError: `项目加载失败：${(err as Error).message}` });
        }
      } else {
        set({ project: null, currentSceneId: null, loaded: false });
        await get().ensureLoaded();
      }
    },

    reloadProject: async () => {
      const current = get().project;
      if (!current) return;
      // 关键：丢弃 pending 的防抖保存——它持有的是服务端写回（Agent 生成结果）之前的旧副本，
      // 若不取消，save() 会用旧 scenes 全量 PUT 把 Agent 刚写入的图/视频抹掉
      if (saveTimer) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      try {
        const fresh = await api.getProject(current.id);
        const keepId = fresh.scenes.some((s) => s.id === get().currentSceneId)
          ? get().currentSceneId
          : fresh.scenes[0]?.id ?? null;
        set({ project: fresh, currentSceneId: keepId });
      } catch {
        /* 后端不可达时保留本地状态 */
      }
    },

    ensureLoaded: async () => {
      if (get().project) return;
      await get().loadProjects();
      const first = get().projects[0];
      if (first) {
        try {
          await get().openProject(first.id);
        } catch (err) {
          set({ loaded: true, loadError: `项目加载失败：${(err as Error).message}` });
        }
      } else {
        try {
          await get().createProject();
        } catch (err) {
          set({ loaded: true, loadError: `无法创建初始项目：${(err as Error).message}` });
        }
      }
    },

    setCurrent: (id) => set({ currentSceneId: id }),

    addScene: () =>
      mutate((p) => {
        const scene = emptyScene();
        p.scenes.push(scene);
        set({ currentSceneId: scene.id });
      }),

    duplicateScene: (id) =>
      mutate((p) => {
        const idx = p.scenes.findIndex((s) => s.id === id);
        if (idx < 0) return;
        const copy = JSON.parse(JSON.stringify(p.scenes[idx])) as Scene;
        copy.id = `scene_${Math.random().toString(36).slice(2, 10)}`;
        copy.title = `${p.scenes[idx].title} 副本`;
        p.scenes.splice(idx + 1, 0, copy);
        set({ currentSceneId: copy.id });
      }),

    deleteScene: (id) =>
      mutate((p) => {
        if (p.scenes.length <= 1) return;
        const idx = p.scenes.findIndex((s) => s.id === id);
        if (idx < 0) return;
        p.scenes.splice(idx, 1);
        const next = p.scenes[Math.min(idx, p.scenes.length - 1)];
        set({ currentSceneId: next.id });
      }),

    reorderScene: (from, to) =>
      mutate((p) => {
        if (from === to || from < 0 || to < 0 || from >= p.scenes.length || to >= p.scenes.length) return;
        const [scene] = p.scenes.splice(from, 1);
        p.scenes.splice(to, 0, scene);
      }),

    updateScene: (id, patch) =>
      mutate((p) => {
        const scene = p.scenes.find((s) => s.id === id);
        if (scene) Object.assign(scene, patch);
      }),

    renameProject: (name) => mutate((p) => void (p.name = name)),

    renameProjectById: async (id, name) => {
      const trimmed = name.trim();
      if (!trimmed) return;
      if (get().project?.id === id) {
        get().renameProject(trimmed);
        return;
      }
      // 非当前项目：读取→改名→保存，不切换当前打开的项目
      const p = await api.getProject(id);
      p.name = trimmed;
      await api.saveProject(id, { name: p.name, aspect_ratio: p.aspect_ratio, scenes: p.scenes });
      await get().loadProjects();
    },

    save: async () => {
      const project = get().project;
      if (!project) return;
      set({ saving: true });
      try {
        await api.saveProject(project.id, {
          name: project.name,
          aspect_ratio: project.aspect_ratio,
          scenes: project.scenes,
        });
        set({ saveError: false });
        await get().loadProjects();
      } catch {
        // 标记失败供 UI 提示；下次任意编辑会自动重试
        set({ saveError: true });
      } finally {
        set({ saving: false });
      }
    },
  };
});
