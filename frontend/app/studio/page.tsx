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

  const [videoModels, setVideoModels] = useState<{ id: string; label: string; resolutions?: string[] }[]>([]);

  useEffect(() => {
    void ensureLoaded();
  }, [ensureLoaded]);

  // 接收 Voice 页发来的旁白音频：先登记进素材池；已有分镜再绑定为当前分镜的参考音频
  const pendingVoice = useAppStore((s) => s.pendingVoice);
  const setPendingVoice = useAppStore((s) => s.setPendingVoice);
  useEffect(() => {
    const url = pendingVoice?.url;
    // 项目未加载完先留着，加载完成后 project?.id 变化会重跑本 effect
    if (!url || !project?.id) return;
    setPendingVoice(null);
    const projectId = project.id;
    void (async () => {
      const { p2 } = await import("@/lib/api-v2");
      // 台词随素材入库：旧格式（uuid 命名）音频靠它合成语义文件名，分镜时才能对号
      const added = await p2.addMaterials(projectId, [{ url, kind: "audio", text: pendingVoice.text || undefined }]);
      const material = (added.materials ?? []).find((m) => m.url === url);
      if (!material) {
        toast.error("旁白素材登记失败");
        return;
      }
      const fresh = await p2.getProject(projectId);
      const current = useStudioV2Store.getState().currentSceneId;
      const sceneId = fresh.scenes.some((s) => s.id === current)
        ? current
        : fresh.scenes[0]?.id;
      if (!sceneId) {
        toast.success("旁白已加入素材池（项目还没有分镜，生成分镜后在分镜抽屉绑定）");
        return;
      }
      await p2.bindMaterial(projectId, {
        scene_id: sceneId, kind: "audio", material_id: material.id, base_version: fresh.version,
      });
      toast.success("旁白已绑定为该镜参考音频（生成时会随视频传入）");
    })().catch((err) => toast.error(`旁白发送失败：${(err as Error).message}`));
  }, [pendingVoice, project?.id, setPendingVoice]);

  // 接收图片页发来的生成图：登记进素材池即可，不自动绑镜——多图对号交给 bind 阶段 / 分镜抽屉
  const pendingStudio = useAppStore((s) => s.pendingStudio);
  const setPendingStudio = useAppStore((s) => s.setPendingStudio);
  useEffect(() => {
    const images = pendingStudio?.images ?? [];
    // 项目未加载完先留着，加载完成后 project 变化会重跑本 effect
    if (images.length === 0 || !project?.id) return;
    setPendingStudio(null);
    const projectId = project.id;
    const inPool = new Set(project.materials.map((m) => m.url));
    const fresh = images.filter((u) => !inPool.has(u));
    if (fresh.length === 0) {
      toast.success("这张图已在素材池中，无需重复发送");
      return;
    }
    void (async () => {
      const { p2 } = await import("@/lib/api-v2");
      await p2.addMaterials(projectId, fresh.map((url) => ({ url, kind: "image" })));
      toast.success(
        project.scenes.length > 0
          ? `已加入素材池（${fresh.length} 张）：可在分镜抽屉挂到指定镜头，或下次投放时自动分配`
          : `已加入素材池（${fresh.length} 张）：启动流水线时会自动分配到分镜`,
      );
    })().catch((err) => toast.error(`图片发送失败：${(err as Error).message}`));
  }, [pendingStudio, project, setPendingStudio]);

  useEffect(() => {
    void fetch(`${process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000"}/api/settings/catalog`)
      .then((r) => r.json())
      .then((d: { video?: { id: string; label: string; resolutions?: string[] }[] }) => setVideoModels(d.video ?? []))
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

  // 外壳布局没有高度约束（body 需保持可滚动以兼容其他页面），这里直接锚定视口：
  // Topbar 高 3.5rem，页面占余下高度，子列才能真正内部滚动
  return (
    <div className="flex h-[calc(100dvh-3.5rem)] min-h-0 flex-col">
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
          onClick={() => {
            const name = window.prompt("新建项目名称", "未命名项目");
            if (name === null) return;
            void createProject(name).then((n) => toast.success(`已新建项目「${n}」`));
          }}
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

        {(() => {
          const current = project.settings.resolution || "auto";
          const tiers = videoModels.find((m) => m.id === project.settings.video_model)?.resolutions ?? ["auto"];
          const options = tiers.includes(current) ? tiers : [current, ...tiers];
          return (
            <select
              value={current}
              onChange={(e) => void patchSettings({ resolution: e.target.value })}
              className="h-8 cursor-pointer rounded-lg border border-border bg-card px-2 text-xs outline-none"
              title="分辨率（auto=按画幅用默认档位；对之后新生成的分镜生效，档位随所选视频模型变化）"
            >
              {options.map((r) => (
                <option key={r} value={r}>
                  分辨率：{r}
                </option>
              ))}
            </select>
          );
        })()}

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

        <label
          className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground"
          title="分镜 Prompt 润色（director pass）：开启时规划后逐镜润色到导演级写作；关闭时直接采用草稿 Prompt。分镜稿已成稿（原文即定稿 H3 Prompt）或润色破坏原文锁定的构图/机位时建议关闭，对下一次规划生效"
        >
          <input
            type="checkbox"
            checked={project.settings.prompt_polish}
            onChange={(e) => void patchSettings({ prompt_polish: e.target.checked })}
            className="accent-primary"
          />
          分镜润色
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

      {/* 主体三栏：Agent 控制台 | 中央视图（整体一根滚动条） | 单镜抽屉（占位，看板自适应换行）。
          极窄窗口下整行出横向滚动，保证任何卡片都可达 */}
      <div className="flex min-h-0 flex-1 overflow-x-auto">
        <AgentConsole />
        <div className="flex min-w-[420px] flex-1 flex-col gap-3 overflow-y-auto p-4">
          <IntakePanel />
          <PipelineBar />
          <ShotBoard />
        </div>
        {currentSceneId && <ShotDrawer />}
      </div>
    </div>
  );
}
