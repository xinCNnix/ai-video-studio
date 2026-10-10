/** 命令式 API 客户端：所有修改带 base_version 乐观锁，409 时上层刷新重放。 */
import { API_BASE, attachmentKind, uploadAttachment } from "./api";
import type {
  MaterialKind,
  PendingBinding,
  PipelineInfo,
  ProjectSettings,
  ProjectSummaryV2,
  ProjectV2,
  SceneV2,
} from "./types-v2";

export class VersionConflictError extends Error {
  currentVersion: number;
  constructor(message: string, currentVersion: number) {
    super(message);
    this.currentVersion = currentVersion;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  if (init?.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* ignore */
    }
    if (res.status === 409) {
      const current = Number(res.headers.get("x-current-version") ?? 0);
      throw new VersionConflictError(detail || "版本冲突", current);
    }
    throw new Error(detail || `请求失败 (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const p2 = {
  listProjects: () => request<{ projects: ProjectSummaryV2[] }>("/api/projects"),
  createProject: (name: string, aspect_ratio: string) =>
    request<ProjectV2>("/api/projects", { method: "POST", body: JSON.stringify({ name, aspect_ratio }) }),
  getProject: (id: string) => request<ProjectV2>(`/api/projects/${id}`),
  patchProject: (id: string, body: { base_version?: number; name?: string; aspect_ratio?: string; settings?: Partial<ProjectSettings> }) =>
    request<ProjectV2>(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProject: (id: string) => request<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),

  patchScene: (id: string, sceneId: string, body: Record<string, unknown> & { base_version?: number }) =>
    request<ProjectV2>(`/api/projects/${id}/scenes/${sceneId}`, { method: "PATCH", body: JSON.stringify(body) }),
  addScene: (id: string, body: { base_version?: number; index?: number; title?: string }) =>
    request<ProjectV2>(`/api/projects/${id}/scenes`, { method: "POST", body: JSON.stringify(body) }),
  deleteScene: (id: string, sceneId: string, base_version?: number) =>
    request<ProjectV2>(`/api/projects/${id}/scenes/${sceneId}${base_version != null ? `?base_version=${base_version}` : ""}`, { method: "DELETE" }),
  reorderScenes: (id: string, order: string[], base_version: number) =>
    request<ProjectV2>(`/api/projects/${id}/scenes/reorder`, { method: "POST", body: JSON.stringify({ order, base_version }) }),

  bindMaterial: (id: string, body: { scene_id: string; kind: MaterialKind; material_id: string; role?: string; base_version?: number }) =>
    request<ProjectV2>(`/api/projects/${id}/bindings`, { method: "POST", body: JSON.stringify(body) }),
  unbindMaterial: (id: string, body: { scene_id: string; kind: MaterialKind; material_id: string; base_version?: number }) =>
    request<ProjectV2>(`/api/projects/${id}/bindings`, { method: "DELETE", body: JSON.stringify(body) }),
  addMaterials: (id: string, materials: { url: string; kind?: string; filename?: string; text?: string; duration?: number | null }[]) =>
    request<{ ok: boolean; materials: ProjectV2["materials"] }>(`/api/projects/${id}/materials`, { method: "POST", body: JSON.stringify({ materials }) }),
  reorderMaterials: (id: string, order: string[], base_version: number) =>
    request<ProjectV2>(`/api/projects/${id}/materials/reorder`, { method: "POST", body: JSON.stringify({ order, base_version }) }),
  removeMaterial: (id: string, materialId: string, base_version?: number) =>
    request<ProjectV2>(`/api/projects/${id}/materials/${materialId}${base_version != null ? `?base_version=${base_version}` : ""}`, { method: "DELETE" }),
  resetContent: (id: string, base_version: number) =>
    request<ProjectV2>(`/api/projects/${id}/reset`, { method: "POST", body: JSON.stringify({ base_version }) }),

  generate: (id: string, sceneId: string, body: { kind: "keyframe" | "voice" | "video"; prompt?: string; text?: string; force?: boolean; use_uploaded?: boolean; reference_scene_id?: string }) =>
    request<{ ok: boolean; job_id?: string; queued?: string; note?: string }>(`/api/projects/${id}/scenes/${sceneId}/generate`, { method: "POST", body: JSON.stringify(body) }),

  startPipeline: (id: string, body: { script_text?: string; materials?: { url: string; kind?: string; filename?: string; text?: string; duration?: number | null }[]; settings?: Partial<ProjectSettings> }) =>
    request<{ ok: boolean; run_id: string }>(`/api/projects/${id}/pipeline/start`, { method: "POST", body: JSON.stringify(body) }),
  advancePipeline: (id: string, confirmed = false) =>
    request<{ ok: boolean; run_id: string }>(`/api/projects/${id}/pipeline/advance`, { method: "POST", body: JSON.stringify({ confirmed }) }),
  confirmBindings: (id: string) =>
    request<{ ok: boolean; run_id: string }>(`/api/projects/${id}/bindings/confirm`, { method: "POST", body: JSON.stringify({ confirmed: true }) }),
  listRuns: (id: string) =>
    request<{ runs: { run_id: string; kind: string; status: string; started_at: string; goal?: string; counts?: Record<string, number> }[] }>(`/api/projects/${id}/runs`),
  runTrace: (id: string, runId: string) =>
    request<{ run_id: string; events: { seq: number; t: string; type: string }[] }>(`/api/projects/${id}/runs/${runId}/trace`),

  exportProject: (id: string) =>
    request<{ url: string; scenes_exported: number; scenes_skipped: number[] }>(`/api/projects/${id}/export`, { method: "POST" }),
};

/** SSE 订阅：返回关闭函数。事件（snapshot/project_updated）通过 onEvent 回调。 */
export function subscribeProjectEvents(
  projectId: string,
  onEvent: (event: { type: string; version?: number; project?: ProjectV2 }) => void,
  onStateChange?: (connected: boolean) => void,
): () => void {
  const es = new EventSource(`${API_BASE}/api/projects/${projectId}/events`);
  es.onopen = () => onStateChange?.(true);
  es.onerror = () => onStateChange?.(false);
  es.onmessage = (ev) => {
    try {
      onEvent(JSON.parse(ev.data));
    } catch {
      /* 忽略心跳等非 JSON 行 */
    }
  };
  return () => es.close();
}

// ---------------- Agent v2 ----------------

export interface AgentV2Event {
  type: "route" | "step" | "done";
  mode?: string;
  tool?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  result?: unknown;
  reply?: string;
}

type RawAgentEvent = {
  type?: string;
  step?: { type?: string; tool?: string; ok?: boolean; text?: string };
  result?: unknown;
  message?: string;
};

/** 后端 SSE 是旧协议（step 载荷嵌在 step 字段、终点事件叫 final），这里归一成 AgentV2Event。 */
function normalizeAgentEvent(evt: RawAgentEvent): AgentV2Event {
  if (evt.type === "step") {
    const s = evt.step ?? {};
    if (s.type === "route") return { type: "route", mode: (s.text ?? "").replace(/^路由：/, "") };
    return { type: "step", tool: s.tool, ok: typeof s.ok === "boolean" ? s.ok : undefined };
  }
  const r = (evt.result ?? {}) as { reply?: string };
  return { type: "done", result: evt.result, reply: typeof r.reply === "string" ? r.reply : undefined };
}

export async function runAgentV2Stream(
  body: { goal: string; project_id?: string | null; scene_id?: string | null; model?: string | null; credential_id?: string | null; history?: { role: string; content: string }[] },
  onEvent: (e: AgentV2Event) => void,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/agent/run/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error("agent 请求失败");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      let evt: RawAgentEvent;
      try {
        evt = JSON.parse(line.slice(6)) as RawAgentEvent;
      } catch {
        continue;
      }
      if (evt.type === "error") throw new Error(evt.message || "Agent 运行失败");
      onEvent(normalizeAgentEvent(evt));
    }
  }
}

export async function cancelAgentV2(runId: string): Promise<void> {
  await fetch(`${API_BASE}/api/agent/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId }),
  });
}

// ---------------- 素材上传（复用 v1 上传端点，取得 URL 后随 pipeline start 提交） ----------------

export async function uploadMaterial(file: File): Promise<{ url: string; kind: MaterialKind; filename: string; text?: string; text_truncated?: boolean; duration?: number | null }> {
  const kind = attachmentKind(file) ?? "doc";
  const att = await uploadAttachment(file, kind);
  return { url: att.url, kind, filename: att.name, text: att.text, text_truncated: att.text_truncated, duration: att.duration };
}

export type { PendingBinding, PipelineInfo, ProjectSummaryV2, ProjectV2, SceneV2 };
