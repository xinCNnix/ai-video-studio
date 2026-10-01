"use client";

/** 投放面板：剧本文本 + 参考素材上传 → 启动流水线（agent-first 的主入口）。
 * 素材按上传顺序编号（图N/音N）——storyboard 模式下该编号 = 原稿 <Image N>/<Audio N>，
 * 没有编号与缩略图，用户无法核对"哪张图对哪个镜"，只能等生成完才发现绑错。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, FileText, Loader2, Mic, Paperclip, Play, X } from "lucide-react";
import { toast } from "sonner";
import { uploadMaterial } from "@/lib/api-v2";
import { displayFilename, materialDupOfs, materialOrders, orderLabel } from "@/lib/material-order";
import { MaterialThumb } from "@/components/studio-v2/material-thumb";
import { useAppStore } from "@/stores/app-store";
import { useStudioV2Store } from "@/stores/studio-v2-store";

interface PendingMaterial {
  url: string;
  kind: string;
  filename: string;
  text?: string;
  duration?: number | null; // 音频：服务端 ffprobe 的精确时长（分镜时长对齐的依据）
}

export function IntakePanel() {
  const startPipeline = useStudioV2Store((s) => s.startPipeline);
  const resetContent = useStudioV2Store((s) => s.resetContent);
  const moveMaterial = useStudioV2Store((s) => s.moveMaterial);
  const removeMaterial = useStudioV2Store((s) => s.removeMaterial);
  const sceneCount = useStudioV2Store((s) => s.project?.scenes.length ?? 0);
  const materialCount = useStudioV2Store((s) => s.project?.materials.length ?? 0);
  const project = useStudioV2Store((s) => s.project);
  const [script, setScript] = useState("");
  const [materials, setMaterials] = useState<PendingMaterial[]>([]);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  // 有分镜后默认收起成一条，把竖向空间还给分镜看板；用户显式展开/收起则记住选择
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const expanded = userOpen ?? sceneCount === 0;
  const fileRef = useRef<HTMLInputElement>(null);

  // 接收提示词页发来的 Prompt 文本：并入剧本框等用户投放——拆分镜由 Studio 流水线做
  const pendingStudio = useAppStore((s) => s.pendingStudio);
  const setPendingStudio = useAppStore((s) => s.setPendingStudio);
  useEffect(() => {
    const text = pendingStudio?.text?.trim();
    if (!text) return;
    setPendingStudio(null);
    setUserOpen(true);
    setScript((s) => (s ? `${s}\n\n${text}` : text));
    toast.success("Prompt 已并入剧本框：可继续追加素材，启动流水线后自动拆分镜");
  }, [pendingStudio, setPendingStudio]);

  // storyboard 模式下上传序号直接对号原稿 <Image N>/<Audio N>，tooltip 需点明这层含义
  const isStoryboard = (project?.screenplay as { script_type?: string } | null)?.script_type === "storyboard";
  const poolMats = useMemo(
    () => (project?.materials ?? []).filter((m) => m.kind !== "script" && m.kind !== "doc"),
    [project?.materials],
  );
  const poolOrders = useMemo(
    () => materialOrders(poolMats),
    [poolMats],
  );
  const poolDupOfs = useMemo(
    () => materialDupOfs(poolMats),
    [poolMats],
  );
  // 待投素材序号：把池内素材拼在前面一起编号——已在池内（同 URL/同身份）的待投项
  // 不占新号，与后端 intake 入池判重一致
  const pendingBase = useMemo(() => [...poolMats, ...materials], [poolMats, materials]);
  const pendingOrders = useMemo(
    () => materialOrders(pendingBase).slice(poolMats.length),
    [pendingBase, poolMats.length],
  );
  const pendingDupOfs = useMemo(
    () => materialDupOfs(pendingBase).slice(poolMats.length),
    [pendingBase, poolMats.length],
  );

  const addFiles = async (files: FileList | File[]) => {
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        const m = await uploadMaterial(f);
        if (m.kind === "doc" && m.text) {
          setScript((s) => (s ? `${s}\n\n${m.text}` : (m.text ?? "")));
          toast.success(`已读取剧本「${m.filename}」`);
        } else {
          setMaterials((ms) => (ms.some((x) => x.url === m.url) ? ms : [...ms, { ...m }]));
        }
      }
    } catch (err) {
      toast.error(`上传失败：${(err as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  const submit = async () => {
    if (!script.trim() && materials.length === 0) {
      toast.error("请先粘贴剧本文本或上传素材");
      return;
    }
    setStarting(true);
    try {
      await startPipeline(script.trim(), materials);
      toast.success("流水线已启动：剧本解析 → 分镜规划 → 素材分配，完成后会等你确认再生成");
      setScript("");
      setMaterials([]);
      setUserOpen(false);
    } catch {
      /* store 已 toast */
    } finally {
      setStarting(false);
    }
  };

  const movePending = (url: string, dir: -1 | 1) => {
    setMaterials((ms) => {
      const i = ms.findIndex((x) => x.url === url);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= ms.length) return ms;
      const next = [...ms];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  const dropHandlers = {
    onDragOver: (e: React.DragEvent) => e.preventDefault(),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) void addFiles(e.dataTransfer.files);
    },
  };

  const orderTitle = (kind: string, order: number | null | undefined, dupOf: number | null | undefined, filename: string) => {
    if (order == null && dupOf != null) {
      // 同名/同 URL 副本：折叠到 dupOf 号不占号。不解释清楚会被当成"编号跳号漏了一张图"
      const twin = orderLabel(kind, dupOf);
      const tag = kind === "image" ? `<Image ${dupOf}>` : kind === "audio" ? `<Audio ${dupOf}>` : "";
      return `${filename}｜同名副本（重${dupOf}）：与 ${twin} 同文件名，视为同一素材不占号${tag ? `，${twin} = 原稿 ${tag}` : ""}。` +
        `要用这个版本就删掉 ${twin} 那张，本张会自动接上编号；两版都要用请给本张改名后重新上传`;
    }
    const label = orderLabel(kind, order);
    if (!label) return filename;
    const tag = kind === "image" ? `<Image ${order}>` : kind === "audio" ? `<Audio ${order}>` : "";
    return isStoryboard && tag
      ? `${filename}｜${label} = 原稿 ${tag}（按上传顺序对号，悬停箭头可调整顺序）`
      : `${filename}｜${label}（第 ${order} 个上传的${kind === "image" ? "图" : kind === "audio" ? "音频" : "视频"}）`;
  };

  // 音频精确时长（服务端 ffprobe）：对齐分镜时长前先看得见
  const durBadge = (d: number | null | undefined) =>
    d != null ? <span className="font-semibold text-amber-300">{d}s</span> : null;

  if (!expanded) {
    return (
      <div
        className="flex shrink-0 cursor-pointer items-center gap-2 rounded-2xl border border-dashed border-border bg-card/40 px-4 py-2 text-xs text-muted-foreground transition-colors hover:border-primary/50"
        onClick={() => setUserOpen(true)}
        title="展开投放表单（粘贴剧本 / 拖入素材）"
        {...dropHandlers}
      >
        <Paperclip className="size-3.5 text-primary" />
        投放新剧本 / 追加素材（拖到这条上即可上传）
        <ChevronDown className="ml-auto size-3.5" />
      </div>
    );
  }

  return (
    <div
      className="shrink-0 rounded-2xl border border-dashed border-border bg-card/40 p-5"
      {...dropHandlers}
    >
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Paperclip className="size-4 text-primary" />
        投放剧本与素材
        <span className="text-xs font-normal text-muted-foreground">
          粘贴任意格式剧本 / 拖入 .md .txt 剧本、参考图、参考音频 —— 拆分镜与素材分配全自动，生成前会等你确认
        </span>
        {sceneCount > 0 && (
          <button
            type="button"
            onClick={() => setUserOpen(false)}
            className="ml-auto cursor-pointer rounded-md p-1 text-muted-foreground hover:text-foreground"
            title="收起，把空间让给分镜看板"
          >
            <ChevronUp className="size-4" />
          </button>
        )}
      </div>
      <textarea
        value={script}
        onChange={(e) => setScript(e.target.value)}
        placeholder="在此粘贴剧本 / 故事 / 已有分镜稿 / 广告口播……（也可以拖入文件）"
        className="mt-3 h-32 w-full resize-none rounded-xl border border-border bg-background/60 p-3 text-sm leading-relaxed outline-none focus:border-primary/60"
      />

      {/* 已入池素材：二次投放 / 重开页面时池子是绑定的真实对象，必须可见可核对。
          悬停箭头交换同类素材顺序——修正 storyboard 上传顺序错位；已落到分镜的绑定
          是 material_id 引用不受影响，换绑在分镜抽屉里做 */}
      {poolMats.length > 0 && (
        <div className="mt-2">
          <div className="mb-1 text-[10px] text-muted-foreground">
            素材池 {poolMats.length} 项（{isStoryboard
              ? "编号 = 原稿 <Image N>/<Audio N>；传错顺序用悬停箭头交换；标「重N」的是同名副本（不占号，入池时忽略）。已绑到分镜的素材不受影响，换绑请到分镜抽屉"
              : "图/音按上传顺序编号，悬停箭头可交换顺序；标「重N」的是同名副本（不占号）"}）
          </div>
          <div className="flex max-h-36 flex-wrap items-center gap-2 overflow-y-auto pr-1">
            {poolMats.map((m, i) =>
              m.kind === "image" && m.url ? (
                <div key={m.id} className="group relative">
                  <MaterialThumb
                    src={m.url}
                    badge={orderLabel("image", poolOrders[i]) || (poolDupOfs[i] != null ? `重${poolDupOfs[i]}` : undefined)}
                    badgeTone={poolOrders[i] == null && poolDupOfs[i] != null ? "warn" : "default"}
                    title={orderTitle("image", poolOrders[i], poolDupOfs[i], displayFilename(m.filename))}
                    className="size-12"
                  />
                  <button
                    type="button"
                    className="absolute -right-1 -top-1 hidden cursor-pointer rounded-full bg-red-500/90 p-0.5 text-white group-hover:block"
                    onClick={() => void removeMaterial(m.id)}
                    title="从素材池删除（已绑定的分镜会解除引用）"
                  >
                    <X className="size-2.5" />
                  </button>
                  <div className="absolute inset-x-0.5 bottom-1 hidden justify-between group-hover:flex">
                    <button
                      type="button"
                      className="cursor-pointer rounded bg-black/70 px-1 text-[9px] leading-4 text-white hover:bg-black"
                      onClick={() => void moveMaterial(m.id, -1)}
                      title="与前一张同类素材交换（编号-1）"
                    >
                      ◀
                    </button>
                    <button
                      type="button"
                      className="cursor-pointer rounded bg-black/70 px-1 text-[9px] leading-4 text-white hover:bg-black"
                      onClick={() => void moveMaterial(m.id, 1)}
                      title="与后一张同类素材交换（编号+1）"
                    >
                      ▶
                    </button>
                  </div>
                </div>
              ) : (
                <span
                  key={m.id}
                  title={orderTitle(m.kind, poolOrders[i], poolDupOfs[i], displayFilename(m.filename))}
                  className="group inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2 py-1 text-[11px]"
                >
                  {m.kind === "audio" ? <Mic className="size-3" /> : <FileText className="size-3" />}
                  {orderLabel(m.kind, poolOrders[i]) ? (
                    <span className="font-semibold text-primary">{orderLabel(m.kind, poolOrders[i])}</span>
                  ) : poolDupOfs[i] != null ? (
                    <span className="font-semibold text-amber-400">重{poolDupOfs[i]}</span>
                  ) : null}
                  {displayFilename(m.filename)}
                  {m.kind === "audio" && durBadge(m.duration)}
                  <span className="hidden items-center gap-0.5 group-hover:inline-flex">
                    <button type="button" className="cursor-pointer px-0.5 text-muted-foreground hover:text-foreground" onClick={() => void moveMaterial(m.id, -1)} title="与前一段同类素材交换（编号-1）">◀</button>
                    <button type="button" className="cursor-pointer px-0.5 text-muted-foreground hover:text-foreground" onClick={() => void moveMaterial(m.id, 1)} title="与后一段同类素材交换（编号+1）">▶</button>
                  </span>
                  <button type="button" className="cursor-pointer text-muted-foreground hover:text-red-400" onClick={() => void removeMaterial(m.id)} title="从素材池删除（已绑定的分镜会解除引用）">
                    <X className="size-3" />
                  </button>
                </span>
              ),
            )}
          </div>
        </div>
      )}

      {materials.length > 0 && (
        <div className="mt-2">
          <div className="mb-1 text-[10px] text-muted-foreground">
            本次待投 {materials.length} 项（{isStoryboard
              ? "序号接续素材池，= 原稿 <Image N>/<Audio N>；顺序错了用悬停箭头调整；标「重N」的是同名副本（不占号，入池时忽略）"
              : "图/音按上传顺序编号，悬停箭头可调整顺序；标「重N」的是同名副本（不占号）"}）
          </div>
          <div className="flex max-h-40 flex-wrap items-start gap-2 overflow-y-auto pr-1">
            {materials.map((m, i) => {
              const order = pendingOrders[i];
              const dupOf = pendingDupOfs[i];
              if (m.kind === "image") {
                return (
                  <div key={m.url} className="group relative">
                    <MaterialThumb
                      src={m.url}
                      badge={orderLabel("image", order) || (dupOf != null ? `重${dupOf}` : undefined)}
                      badgeTone={order == null && dupOf != null ? "warn" : "default"}
                      title={orderTitle("image", order, dupOf, displayFilename(m.filename))}
                    />
                    <button
                      type="button"
                      className="absolute -right-1 -top-1 hidden cursor-pointer rounded-full bg-red-500/90 p-0.5 text-white group-hover:block"
                      onClick={() => setMaterials((ms) => ms.filter((x) => x.url !== m.url))}
                      title="移除"
                    >
                      <X className="size-2.5" />
                    </button>
                    <div className="absolute inset-x-1 bottom-1 hidden justify-between group-hover:flex">
                      <button
                        type="button"
                        className="cursor-pointer rounded bg-black/70 px-1 text-[9px] leading-4 text-white hover:bg-black"
                        onClick={() => movePending(m.url, -1)}
                        title="提前一位（编号-1）"
                      >
                        ◀
                      </button>
                      <button
                        type="button"
                        className="cursor-pointer rounded bg-black/70 px-1 text-[9px] leading-4 text-white hover:bg-black"
                        onClick={() => movePending(m.url, 1)}
                        title="移后一位（编号+1）"
                      >
                        ▶
                      </button>
                    </div>
                  </div>
                );
              }
              return (
                <span
                  key={m.url}
                  title={orderTitle(m.kind, order, dupOf, displayFilename(m.filename))}
                  className="group inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2 py-1 text-[11px]"
                >
                  {m.kind === "audio" ? <Mic className="size-3" /> : <FileText className="size-3" />}
                  {orderLabel(m.kind, order) ? (
                    <span className="font-semibold text-primary">{orderLabel(m.kind, order)}</span>
                  ) : dupOf != null ? (
                    <span className="font-semibold text-amber-400">重{dupOf}</span>
                  ) : null}
                  {displayFilename(m.filename)}
                  {m.kind === "audio" && durBadge(m.duration)}
                  <span className="hidden items-center gap-0.5 group-hover:inline-flex">
                    <button type="button" className="cursor-pointer px-0.5 text-muted-foreground hover:text-foreground" onClick={() => movePending(m.url, -1)} title="提前一位（编号-1）">◀</button>
                    <button type="button" className="cursor-pointer px-0.5 text-muted-foreground hover:text-foreground" onClick={() => movePending(m.url, 1)} title="移后一位（编号+1）">▶</button>
                  </span>
                  <button type="button" className="cursor-pointer text-muted-foreground hover:text-foreground" onClick={() => setMaterials((ms) => ms.filter((x) => x.url !== m.url))}>
                    <X className="size-3" />
                  </button>
                </span>
              );
            })}
          </div>
        </div>
      )}
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="cursor-pointer rounded-lg border border-border bg-card px-3 py-1.5 text-xs transition-colors hover:bg-secondary/60"
        >
          {uploading ? <Loader2 className="size-3.5 animate-spin" /> : "选择文件"}
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept=".md,.txt,image/*,audio/*,video/*"
          className="hidden"
          onChange={(e) => e.target.files && void addFiles(e.target.files)}
        />
        <div className="flex-1" />
        {(materialCount > 0 || sceneCount > 0) && (
          <button
            type="button"
            onClick={() => {
              const msg = `清空重投：将删除全部 ${materialCount} 项素材与 ${sceneCount} 个分镜（含生成结果与待确认项）。\n项目名、画幅、模型设置保留。确定清空？`;
              if (confirm(msg)) void resetContent();
            }}
            className="cursor-pointer rounded-lg border border-red-500/40 px-3 py-1.5 text-xs text-red-300 transition-colors hover:bg-red-500/10"
            title="清掉素材池与全部分镜，从头重新投放（解决素材重复堆积）"
          >
            清空重投
          </button>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={starting || uploading}
          className="accent-gradient inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-60"
        >
          {starting ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
          启动流水线
        </button>
      </div>
    </div>
  );
}
