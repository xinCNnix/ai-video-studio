"use client";

import { useEffect, useState } from "react";
import { Clapperboard, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, fileUrl } from "@/lib/api";
import { useAppStore } from "@/stores/app-store";
import { useStudioStore } from "@/stores/studio-store";
import { SceneList } from "@/components/studio/scene-list";
import { ScenePreview } from "@/components/studio/scene-preview";
import { SceneProperties } from "@/components/studio/scene-properties";
import { SceneStrip } from "@/components/studio/scene-strip";
import { SearchableSelect } from "@/components/common/searchable-select";

export default function StudioPage() {
  const ensureLoaded = useStudioStore((s) => s.ensureLoaded);
  const loaded = useStudioStore((s) => s.loaded);
  const project = useStudioStore((s) => s.project);
  const projects = useStudioStore((s) => s.projects);
  const currentSceneId = useStudioStore((s) => s.currentSceneId);
  const updateScene = useStudioStore((s) => s.updateScene);
  const saving = useStudioStore((s) => s.saving);
  const saveError = useStudioStore((s) => s.saveError);
  const loadError = useStudioStore((s) => s.loadError);
  const renameProjectById = useStudioStore((s) => s.renameProjectById);
  const openProject = useStudioStore((s) => s.openProject);
  const createProject = useStudioStore((s) => s.createProject);
  const deleteProject = useStudioStore((s) => s.deleteProject);
  const [exporting, setExporting] = useState(false);
  const pendingVoice = useAppStore((s) => s.pendingVoice);
  const setPendingVoice = useAppStore((s) => s.setPendingVoice);

  // 新建项目弹窗：名称在弹窗里输入（有名字按名字建，没名字用默认名），改名入口在下拉列表每项的铅笔图标
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [creating, setCreating] = useState(false);

  const scene = project?.scenes.find((s) => s.id === currentSceneId) ?? null;

  const submitCreate = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const finalName = await createProject(createName.trim() || undefined);
      toast.success(`已新建项目「${finalName}」`);
      setCreateOpen(false);
    } catch (err) {
      toast.error(`新建项目失败：${(err as Error).message}`);
    } finally {
      setCreating(false);
    }
  };

  useEffect(() => {
    void ensureLoaded();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 接收 Voice 页发来的旁白。跳转进来时项目往往还没加载完（scene 为 null），
  // 必须等 scene 就绪后再触发一次，否则 pendingVoice 永远不会被消费
  useEffect(() => {
    if (pendingVoice && scene) {
      updateScene(scene.id, { voice: pendingVoice });
      setPendingVoice(null);
      toast.success("旁白已放入当前 Scene");
    }
  }, [pendingVoice, scene?.id, updateScene, setPendingVoice]);

  // Agent 修改当前场景 Prompt / 描述
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (!detail?.value) return;
      if (detail.type === "update_prompt" && scene) {
        updateScene(scene.id, { prompt: detail.value });
        toast.success("AI Assistant 已更新当前 Scene 的 Prompt");
      }
    };
    window.addEventListener("agent:action", handler);
    return () => window.removeEventListener("agent:action", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene?.id]);

  if (loadError && !project) {
    return (
      <div className="flex h-[calc(100vh-3.5rem)] flex-col items-center justify-center gap-3 text-sm">
        <span className="text-red-400">{loadError}</span>
        <button
          type="button"
          onClick={() => { useStudioStore.setState({ loaded: false, loadError: null }); void ensureLoaded(); }}
          className="rounded-lg border border-border bg-secondary/40 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer"
        >
          重试
        </button>
      </div>
    );
  }

  if (!loaded || !project) {
    return (
      <div className="flex h-[calc(100vh-3.5rem)] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        正在加载项目…
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col overflow-hidden">
      {/* 项目名 + 管理 + 保存状态 */}
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex items-center gap-2">
          <SearchableSelect
            className="w-44"
            items={projects.map((p) => ({ value: p.id, label: p.name }))}
            value={project.id}
            onChange={(id) =>
              openProject(id).catch((err) => toast.error(`切换项目失败：${(err as Error).message}`))
            }
            onDelete={(id) => {
              const target = projects.find((p) => p.id === id);
              if (target && confirm(`删除项目「${target.name}」？`)) void deleteProject(id);
            }}
            onRename={(id, name) => {
              renameProjectById(id, name).catch((err) =>
                toast.error(`改名失败：${(err as Error).message}`)
              );
            }}
            placeholder="切换项目"
            searchPlaceholder="搜索项目…"
          />
          <button
            type="button"
            onClick={() => {
              setCreateName("");
              setCreateOpen(true);
            }}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground cursor-pointer"
            title="新建项目"
          >
            <Plus className="size-4" />
          </button>
          <button
            type="button"
            onClick={() => {
              if (confirm(`删除项目「${project.name}」？`)) void deleteProject(project.id);
            }}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-400 cursor-pointer"
            title="删除当前项目"
          >
            <Trash2 className="size-4" />
          </button>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={exporting}
            onClick={async () => {
              const ready = project.scenes.filter((s) => s.video).length;
              if (!ready) {
                toast.error("还没有任何分镜视频，先生成视频再导出");
                return;
              }
              if (!confirm(`按顺序拼接 ${ready} 个有视频的分镜并混入旁白，导出成片？`)) return;
              setExporting(true);
              try {
                const res = await api.exportProject(project.id);
                toast.success(`成片已导出（${res.scenes_exported} 个分镜${res.scenes_skipped.length ? `，跳过 ${res.scenes_skipped.length} 个无视频分镜` : ""}）`);
                window.open(`${fileUrl(res.url)}?dl=1`, "_blank");
              } catch (err) {
                toast.error((err as Error).message);
              } finally {
                setExporting(false);
              }
            }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-2.5 py-1 text-[11px] text-primary transition-colors hover:bg-primary/20 disabled:opacity-50 cursor-pointer"
          >
            {exporting ? <Loader2 className="size-3 animate-spin" /> : <Clapperboard className="size-3" />}
            导出成片
          </button>
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {saving ? (
            <>
              <Loader2 className="size-3 animate-spin" /> 保存中…
            </>
          ) : saveError ? (
            <span className="text-red-400">自动保存失败——修改任意内容将自动重试，请勿直接关闭页面</span>
          ) : (
            <>
              <Save className="size-3" /> 已自动保存
            </>
          )}
        </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <SceneList />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <ScenePreview scene={scene} />
        </div>
        <SceneProperties />
      </div>

      <SceneStrip />

      {/* 新建项目弹窗 */}
      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setCreateOpen(false);
          }}
        >
          <div className="w-80 rounded-xl border border-border bg-card p-4 shadow-xl">
            <div className="mb-2 text-sm font-semibold">新建项目</div>
            <input
              autoFocus
              value={createName}
              maxLength={60}
              placeholder="未命名项目"
              onChange={(e) => setCreateName(e.target.value)}
              onFocus={(e) => e.currentTarget.select()}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitCreate();
                } else if (e.key === "Escape") {
                  setCreateOpen(false);
                }
              }}
              className="w-full rounded-md border border-border bg-secondary/40 px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="rounded-lg px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
              >
                取消
              </button>
              <button
                type="button"
                disabled={creating}
                onClick={() => void submitCreate()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-3 py-1.5 text-[11px] text-primary transition-colors hover:bg-primary/20 disabled:opacity-50 cursor-pointer"
              >
                {creating ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
                新建
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
