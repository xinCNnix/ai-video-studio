"use client";

import { useEffect, useState } from "react";
import { Clapperboard, Download, Film, Loader2, Plus, Radio, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { fileUrl } from "@/lib/api";
import { useAppStore } from "@/stores/app-store";
import { useStudioV2Store } from "@/stores/studio-v2-store";
import { AgentConsole } from "@/components/studio-v2/agent-console";
import { IntakePanel } from "@/components/studio-v2/intake-panel";
import { PipelineBar } from "@/components/studio-v2/pipeline-bar";
import { ShotBoard } from "@/components/studio-v2/shot-board";
import { ShotDrawer } from "@/components/studio-v2/shot-drawer";
import { cn } from "@/lib/utils";

const ASPECTS = ["16:9", "9:16", "1:1"];

export default function StudioPage() {
  const ensureLoaded = useStudioV2Store((s) => s.ensureLoaded);
  const loaded = useStudioV2Store((s) => s.loaded);
  const loadError = useStudioV2Store((s) => s.loadError);
  const project = useStudioV2Store((s) => s.project);
  const projects = useStudioV2Store((s) => s.projects);
  const currentSceneId = useStudioV2Store((s) => s.currentSceneId);
  const sseConnected = useStudioV2Store((s) => s.sseConnected);
  const openProject = useStudioV2Store((s) => s.openProject);
  const createProject = useStudioV2Store((s) => s.createProject);
  const deleteProject = useStudioV2Store((s) => s.deleteProject);
  const setAspect = useStudioV2Store((s) => s.setAspect);
  const patchSettings = useStudioV2Store((s) => s.patchSettings);
  const exportProject = useStudioV2Store((s) => s.exportProject);
  const exporting = useStudioV2Store((s) => s.exporting);

  const [videoModels, setVideoModels] = useState<{ id: string; label: string }[]>([]);

  useEffect(() => {
    void ensureLoaded();
  }, [ensureLoaded]);

  // 接收 Voice 页发来的旁白音频：登记为素材并绑定为当前分镜的参考音频
  const pendingVoice = useAppStore((s) => s.pendingVoice);
  const setPendingVoice = useAppStore((s) => s.setPendingVoice);
  useEffect(() => {
    const url = pendingVoice;
    if (!url || !project?.scenes.length) return;
    setPendingVoice(null);
    const sceneId = useStudioV2Store.getState().currentSceneId ?? project.scenes[0].id;
    void (async () => {
      const { p2 } = await import("@/lib/api-v2");
      const added = await p2.addMaterials(project.id, [{ url, kind: "audio" }]);
      const material = (added.materials ?? []).find((m) => m.url === url);
      if (!material) {
        toast.error("旁白素材登记失败");
        return;
      }
      const fresh = await p2.getProject(project.id);
      await p2.bindMaterial(project.id, {
        scene_id: sceneId, kind: "audio", material_id: material.id, base_version: fresh.version,
      });
      toast.success("旁白已绑定为该镜参考音频（生成时会随视频传入）");
    })().catch((err) => toast.error(`旁白绑定失败：${(err as Error).message}`));
  }, [pendingVoice, project?.id, project?.scenes.length, setPendingVoice]);

  useEffect(() => {
    void fetch(`${process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000"}/api/settings/catalog`)
      .then((r) => r.json())
      .then((d: { video?: { id: string; label: string }[] }) => setVideoModels(d.video ?? []))
      .catch(() => undefined);
  }, []);

  if (loadError && !project) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
        <Clapperboard className="size-8" />
        <div className="text-sm">{loadError}</div>
        <button
          type="button"
          onClick={() => void ensureLoaded()}
          className="cursor-pointer rounded-lg border border-border bg-card px-4 py-1.5 text-xs hover:bg-secondary/60"
        >
          重试
        </button>
      </div>
    );
  }

  if (!loaded || !project) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <Loader2 className="size-6 animate-spin" />
      </div>
    );
  }

  const doneCount = project.scenes.filter((s) => s.assets.video).length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 顶栏：项目 / 画幅 / 视频模型 / 全自动 / 导出 */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <select
          value={project.id}
          onChange={(e) => void openProject(e.target.value)}
          className="h-8 max-w-52 cursor-pointer rounded-lg border border-border bg-card px-2 text-xs outline-none"
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.pipeline_state === "running" ? " ⏵" : ""}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => void createProject().then((n) => toast.success(`已新建项目「${n}」`))}
          className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs hover:bg-secondary/60"
        >
          <Plus className="size-3.5" /> 新建
        </button>
        <button
          type="button"
          onClick={() => {
            if (confirm(`删除项目「${project.name}」？此操作不可恢复。`)) void deleteProject(project.id);
          }}
          className="cursor-pointer rounded-lg border border-border bg-card p-1.5 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"
          title="删除项目"
        >
          <Trash2 className="size-3.5" />
        </button>

        <div className="mx-1 h-5 w-px bg-border" />

        {ASPECTS.map((a) => (
          <button
            key={a}
            type="button"
            onClick={() => void setAspect(a)}
            className={cn(
              "cursor-pointer rounded-md px-2 py-1 text-[11px]",
              project.aspect_ratio === a ? "bg-primary/20 text-primary" : "bg-secondary/50 text-muted-foreground hover:text-foreground",
            )}
          >
            {a}
          </button>
        ))}

        <select
          value={project.settings.video_model ?? ""}
          onChange={(e) => void patchSettings({ video_model: e.target.value || null })}
          className="h-8 max-w-64 cursor-pointer rounded-lg border border-border bg-card px-2 text-xs outline-none"
          title="视频模型（流水线与手动生成都用它）"
        >
          <option value="">视频模型：默认（目录第一项）</option>
          {videoModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>

        <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground" title="开启后跳过确认门禁，直接生成">
          <input
            type="checkbox"
            checked={project.settings.auto_generate}
            onChange={(e) => void patchSettings({ auto_generate: e.target.checked })}
            className="accent-primary"
          />
          全自动
        </label>

        <div className="flex-1" />

        <span className={cn("inline-flex items-center gap-1 text-[10px]", sseConnected ? "text-emerald-400" : "text-muted-foreground")}>
          <Radio className="size-3" />
          {sseConnected ? "实时同步" : "同步断开"}
        </span>
        {project.final_video && (
          <a
            href={fileUrl(project.final_video)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1.5 text-xs text-emerald-300"
          >
            <Film className="size-3.5" /> 查看成片
          </a>
        )}
        <button
          type="button"
          onClick={() => void exportProject()}
          disabled={exporting || doneCount === 0}
          className="accent-gradient inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
        >
          {exporting ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
          导出成片{doneCount > 0 ? `（${doneCount}/${project.scenes.length}）` : ""}
        </button>
      </div>

      {/* 主体三栏：Agent 控制台 | 中央视图 | 单镜抽屉 */}
      <div className="flex min-h-0 flex-1">
        <AgentConsole />
        <div className="flex min-w-0 flex-1 flex-col gap-3 overflow-hidden p-4">
          <IntakePanel />
          <PipelineBar />
          <ShotBoard />
        </div>
        {currentSceneId && <ShotDrawer />}
      </div>
    </div>
  );
}
