"use client";

/** Studio v2 store：服务端状态的本地视图 + 命令发送端。
 *
 * 与 v1 的本质区别：本地不再持有待保存的编辑副本（没有 save()）。
 * - 项目快照只来自 GET 与 SSE 推送（按 version 去重）
 * - 一切修改 = 发命令（带 base_version）；409 = 有并发写 → 刷新后重放一次
 * - 生成结果由后端写回 + SSE 推送，前端不轮询不回写
 */
import { create } from "zustand";
import { toast } from "sonner";
import { p2, subscribeProjectEvents, VersionConflictError } from "@/lib/api-v2";
import type { MaterialKind, ProjectSummaryV2, ProjectV2 } from "@/lib/types-v2";

interface StudioV2State {
  project: ProjectV2 | null;
  projects: ProjectSummaryV2[];
  currentSceneId: string | null;
  loaded: boolean;
  loadError: string | null;
  sseConnected: boolean;
  exporting: boolean;

  loadProjects: () => Promise<void>;
  openProject: (id: string) => Promise<void>;
  createProject: (name?: string) => Promise<string>;
  deleteProject: (id: string) => Promise<void>;
  reloadProject: () => Promise<void>;
  ensureLoaded: () => Promise<void>;
  setCurrentScene: (id: string | null) => void;

  /** 命令发送：409 时自动刷新并重放一次；SSE 会同步推送结果快照 */
  command: <T>(fn: (project: ProjectV2) => Promise<T>, replay?: ((fresh: ProjectV2) => Promise<T> | null) | null) => Promise<T | null>;

  patchScene: (sceneId: string, patch: Record<string, unknown>) => Promise<void>;
  addScene: (index?: number) => Promise<void>;
  deleteScene: (sceneId: string) => Promise<void>;
  moveScene: (sceneId: string, dir: -1 | 1) => Promise<void>;
  bindMaterial: (sceneId: string, kind: MaterialKind, materialId: string) => Promise<void>;
  unbindMaterial: (sceneId: string, kind: MaterialKind, materialId: string) => Promise<void>;
  /** 素材池同类内移位（图N↔图N±1），修正 storyboard 的 <Image N> 上传顺序对齐 */
  moveMaterial: (materialId: string, dir: -1 | 1) => Promise<void>;
  /** 删除单个素材：出池并解除全部分镜对它的绑定（绑定中的素材删除前先确认） */
  removeMaterial: (materialId: string) => Promise<void>;
  patchSettings: (settings: Record<string, unknown>) => Promise<void>;
  renameProject: (name: string) => Promise<void>;
  setAspect: (aspect: string) => Promise<void>;

  startPipeline: (scriptText: string, materials: { url: string; kind?: string; filename?: string; text?: string; duration?: number | null }[]) => Promise<void>;
  advancePipeline: (confirmed: boolean) => Promise<void>;
  confirmBindings: () => Promise<void>;
  resetContent: () => Promise<void>;
  generate: (sceneId: string, body: Parameters<typeof p2.generate>[2]) => Promise<void>;
  exportProject: () => Promise<void>;
}

let unsubscribe: (() => void) | null = null;

export const useStudioV2Store = create<StudioV2State>()((set, get) => {
  const applyServerProject = (fresh: ProjectV2) => {
    set((state) => {
      if (state.project && state.project.id === fresh.id && fresh.version < state.project.version) return state;
      // 分镜没了（清空重投/切项目）就关抽屉；不自动选中第一镜——刷新/切项目时看板保持全宽
      const keepScene = fresh.scenes.some((s) => s.id === state.currentSceneId)
        ? state.currentSceneId
        : null;
      return { project: fresh, currentSceneId: keepScene };
    });
  };

  const attachSse = (projectId: string) => {
    unsubscribe?.();
    unsubscribe = subscribeProjectEvents(
      projectId,
      (event) => {
        if (event.type === "snapshot" || event.type === "project_updated") {
          if (event.project) applyServerProject(event.project);
        } else if (event.type === "project_deleted") {
          set({ project: null, currentSceneId: null });
          void get().ensureLoaded();
        }
      },
      (connected) => set({ sseConnected: connected }),
    );
  };

  return {
    project: null,
    projects: [],
    currentSceneId: null,
    loaded: false,
    loadError: null,
    sseConnected: false,
    exporting: false,

    loadProjects: async () => {
      try {
        const { projects } = await p2.listProjects();
        set({ projects });
      } catch {
        set({ projects: [] });
      }
    },

    openProject: async (id) => {
      try {
        const fresh = await p2.getProject(id);
        applyServerProject(fresh);
        set({ loaded: true, loadError: null });
        attachSse(id);
      } catch (err) {
        set({ loadError: `项目加载失败：${(err as Error).message}`, loaded: true });
        throw err;
      }
    },

    createProject: async (name) => {
      const fresh = await p2.createProject(name?.trim() || "未命名项目", "16:9");
      applyServerProject(fresh);
      set({ loaded: true, loadError: null });
      attachSse(fresh.id);
      await get().loadProjects();
      return fresh.name;
    },

    deleteProject: async (id) => {
      try {
        await p2.deleteProject(id);
      } catch {
        /* 可能已被删 */
      }
      if (get().project?.id === id) {
        unsubscribe?.();
        unsubscribe = null;
        set({ project: null, currentSceneId: null });
      }
      await get().loadProjects();
      await get().ensureLoaded();
    },

    reloadProject: async () => {
      const cur = get().project;
      if (!cur) return;
      try {
        const fresh = await p2.getProject(cur.id);
        applyServerProject(fresh);
      } catch {
        /* 网络异常保留本地视图 */
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

    setCurrentScene: (id) => set({ currentSceneId: id }),

    command: async (fn, replay) => {
      const project = get().project;
      if (!project) {
        toast.error("没有打开的项目");
        return null;
      }
      try {
        return await fn(project);
      } catch (err) {
        if (err instanceof VersionConflictError) {
          // 有并发写（通常是后端流水线/生成写回）：刷新快照后重放一次用户意图
          const fresh = await p2.getProject(project.id);
          applyServerProject(fresh);
          const replayFn = replay?.(fresh);
          if (replayFn) {
            try {
              return await replayFn;
            } catch (err2) {
              toast.error((err2 as Error).message);
              return null;
            }
          }
          toast.error("页面状态已刷新（有并发更新），请重试本次操作");
          return null;
        }
        toast.error((err as Error).message);
        return null;
      }
    },

    patchScene: async (sceneId, patch) => {
      await get().command(
        (p) => p2.patchScene(p.id, sceneId, { ...patch, base_version: p.version }),
        (fresh) => {
          if (!fresh.scenes.some((s) => s.id === sceneId)) return null;
          return p2.patchScene(fresh.id, sceneId, { ...patch, base_version: fresh.version });
        },
      );
    },

    addScene: async (index) => {
      await get().command(
        (p) => p2.addScene(p.id, { base_version: p.version, index }),
        (fresh) => p2.addScene(fresh.id, { base_version: fresh.version, index }),
      );
    },

    deleteScene: async (sceneId) => {
      await get().command(
        (p) => p2.deleteScene(p.id, sceneId, p.version),
        (fresh) => (fresh.scenes.some((s) => s.id === sceneId) ? p2.deleteScene(fresh.id, sceneId, fresh.version) : Promise.resolve(fresh)),
      );
    },

    moveScene: async (sceneId, dir) => {
      const project = get().project;
      if (!project) return;
      const ids = project.scenes.map((s) => s.id);
      const i = ids.indexOf(sceneId);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await get().command(
        (p) => p2.reorderScenes(p.id, ids, p.version),
        (fresh) => p2.reorderScenes(fresh.id, ids, fresh.version),
      );
    },

    bindMaterial: async (sceneId, kind, materialId) => {
      await get().command(
        (p) => p2.bindMaterial(p.id, { scene_id: sceneId, kind, material_id: materialId, base_version: p.version }),
        (fresh) =>
          fresh.scenes.some((s) => s.id === sceneId)
            ? p2.bindMaterial(fresh.id, { scene_id: sceneId, kind, material_id: materialId, base_version: fresh.version })
            : null,
      );
    },

    unbindMaterial: async (sceneId, kind, materialId) => {
      await get().command(
        (p) => p2.unbindMaterial(p.id, { scene_id: sceneId, kind, material_id: materialId, base_version: p.version }),
        (fresh) =>
          fresh.scenes.some((s) => s.id === sceneId)
            ? p2.unbindMaterial(fresh.id, { scene_id: sceneId, kind, material_id: materialId, base_version: fresh.version })
            : null,
      );
    },

    moveMaterial: async (materialId, dir) => {
      const project = get().project;
      if (!project) return;
      const ids = project.materials.map((m) => m.id);
      const i = ids.indexOf(materialId);
      if (i < 0) return;
      // 同类内移位：跳过中间其他类型的素材再交换——只改变同类之间的相对顺序，
      // 音频穿插在图片中间时移动图片不会打乱音频的 <Audio N> 编号
      const kind = project.materials[i].kind;
      let j = i + dir;
      while (j >= 0 && j < ids.length && project.materials[j].kind !== kind) j += dir;
      if (j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await get().command(
        (p) => p2.reorderMaterials(p.id, ids, p.version),
        (fresh) =>
          fresh.materials.length === ids.length && ids.every((id) => fresh.materials.some((m) => m.id === id))
            ? p2.reorderMaterials(fresh.id, ids, fresh.version)
            : null,
      );
    },

    removeMaterial: async (materialId) => {
      const project = get().project;
      const mat = project?.materials.find((m) => m.id === materialId);
      if (!project || !mat) return;
      const boundCount = project.scenes.filter(
        (s) =>
          s.binding.audios.includes(materialId) ||
          s.binding.videos.includes(materialId) ||
          s.binding.images.some((x) => x.material_id === materialId),
      ).length;
      if (
        boundCount > 0 &&
        !confirm(`「${mat.filename || materialId}」已绑到 ${boundCount} 个分镜，删除会同时解除这些绑定。确定删除？`)
      ) {
        return;
      }
      const r = await get().command(
        (p) => p2.removeMaterial(p.id, materialId, p.version),
        (fresh) =>
          fresh.materials.some((m) => m.id === materialId)
            ? p2.removeMaterial(fresh.id, materialId, fresh.version)
            : null,
      );
      if (r) {
        toast.success(`已删除素材「${mat.filename || materialId}」${boundCount > 0 ? `（解除 ${boundCount} 个分镜的绑定）` : ""}`);
      }
    },

    patchSettings: async (settings) => {
      await get().command(
        (p) => p2.patchProject(p.id, { base_version: p.version, settings }),
        (fresh) => p2.patchProject(fresh.id, { base_version: fresh.version, settings }),
      );
    },

    renameProject: async (name) => {
      const trimmed = name.trim();
      if (!trimmed) return;
      await get().command(
        (p) => p2.patchProject(p.id, { base_version: p.version, name: trimmed }),
        (fresh) => p2.patchProject(fresh.id, { base_version: fresh.version, name: trimmed }),
      );
      await get().loadProjects();
    },

    setAspect: async (aspect) => {
      await get().command(
        (p) => p2.patchProject(p.id, { base_version: p.version, aspect_ratio: aspect }),
        (fresh) => p2.patchProject(fresh.id, { base_version: fresh.version, aspect_ratio: aspect }),
      );
    },

    startPipeline: async (scriptText, materials) => {
      await get().command(
        (p) => p2.startPipeline(p.id, { script_text: scriptText, materials }),
        null,
      );
    },

    advancePipeline: async (confirmed) => {
      await get().command(
        (p) => p2.advancePipeline(p.id, confirmed),
        null,
      );
    },

    confirmBindings: async () => {
      await get().command((p) => p2.confirmBindings(p.id), null);
    },

    resetContent: async () => {
      const r = await get().command(
        (p) => p2.resetContent(p.id, p.version),
        (fresh) => (fresh.materials.length === 0 ? Promise.resolve(fresh) : p2.resetContent(fresh.id, fresh.version)),
      );
      if (r) {
        set({ currentSceneId: null });
        toast.success("已清空：素材与分镜全部移除，可重新投放（项目名/画幅/模型设置保留）");
      }
    },

    generate: async (sceneId, body) => {
      try {
        const r = await get().command((p) => p2.generate(p.id, sceneId, body), null);
        if (r?.ok) toast.success(r.note || "已提交生成");
      } catch {
        /* command 已 toast */
      }
    },

    exportProject: async () => {
      const project = get().project;
      if (!project) return;
      set({ exporting: true });
      try {
        const r = await p2.exportProject(project.id);
        toast.success(`成片已导出（${r.scenes_exported} 镜）`);
      } catch (err) {
        toast.error((err as Error).message);
      } finally {
        set({ exporting: false });
      }
    },
  };
});
