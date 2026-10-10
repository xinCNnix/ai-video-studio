import type { Credential, Job, ModelCatalog, VoiceAsset } from "./types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

export interface AgentRunResult {
  reply: string;
  steps: { step: number; type: string; tool?: string; text?: string; result?: unknown }[];
  updates: Record<string, string>;
  jobs: { job_id: string; type: string; duration?: number; scene_index?: number; project_id?: string | null }[];
  exports?: string[];
  project_changed?: boolean;
  voice_reused?: string[];
}

export interface AgentRunBody {
  goal: string;
  history?: { role: string; content: string }[];
  page: string;
  scene_title?: string;
  scene_description?: string;
  scene_prompt?: string;
  scene_narration?: string;
  scene_image?: string | null;
  aspect_ratio?: string;
  project_id?: string | null;
  credential_id?: string;
  model?: string;
  force_route?: string; // 已废弃：双模式路由已拆除（后端忽略），仅为兼容保留
}

export interface AgentLlmOption {
  model: string;
  credential_id: string;
  provider: string;
  credential_name: string;
  label: string;
}

export interface AgentLlmOptions {
  current: { model: string | null; credential_id: string | null; source: "default" | "auto" };
  options: AgentLlmOption[];
}

export interface UploadedAttachment {
  kind: "image" | "audio" | "video" | "doc";
  name: string;
  url: string;
  text?: string; // doc 类型：剧本正文（发送时内联进消息，模型直接读全文）
  text_truncated?: boolean; // doc 类型：正文超上限被截断（第十回 14 镜被截成 9 镜的事故信号，必须明示）
  duration?: number | null; // audio 类型：服务端 ffprobe 的精确时长（秒），分镜对齐用
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  if (init?.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...headers, ...(init?.headers as object) } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* ignore */
    }
    throw new Error(detail || `请求失败 (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function fileUrl(url?: string | null): string {
  if (!url) return "";
  // blob:=本地预览（上传前试听）；http/data=绝对地址，其余视为后端相对路径
  if (url.startsWith("http") || url.startsWith("data:") || url.startsWith("blob:")) return url;
  return `${API_BASE}${url}`;
}

export function attachmentKind(file: File): UploadedAttachment["kind"] | null {
  const n = file.name.toLowerCase();
  if (n.endsWith(".md") || n.endsWith(".txt") || file.type.startsWith("text/")) return "doc";
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("audio/")) return "audio";
  if (file.type.startsWith("video/")) return "video";
  return null;
}

export async function uploadAttachment(file: File, kind: UploadedAttachment["kind"]): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append("file", file);
  form.append("kind", kind);
  const res = await fetch(`${API_BASE}/api/uploads`, { method: "POST", body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* ignore */
    }
    throw new Error(detail || `上传失败 (${res.status})`);
  }
  const data = (await res.json()) as { url: string; name: string; text?: string; text_truncated?: boolean; duration?: number | null };
  return { kind, name: data.name || file.name, url: data.url, text: data.text, text_truncated: data.text_truncated, duration: data.duration ?? null };
}

export const api = {
  // 系统
  health: () => request<{ status: string }>("/api/health"),
  getCatalog: () => request<ModelCatalog>("/api/settings/catalog"),
  getGeneral: () =>
    request<{ app_name: string; version: string; data_dir: string; output_counts: Record<string, number> }>(
      "/api/settings/general"
    ),
  cleanupUploads: (days: number) =>
    request<{ ok: boolean; deleted: number; days: number }>("/api/settings/cleanup-uploads", {
      method: "POST",
      body: JSON.stringify({ days }),
    }),

  // 模型发现与自定义
  getDiscoveredModels: () =>
    request<{
      discovered: Record<string, string[]>;
      discovered_by_credential: Record<string, string[]>;
      discovered_tts: Record<string, string[]>;
      voices: Record<string, { id: string; label: string; source: string }[]>;
      voice_errors: { provider: string; name: string; message: string }[];
    }>("/api/settings/discovered-models"),
  testModel: (body: { provider: string; credential_id?: string; model: string }) =>
    request<{ ok: boolean; message: string; sample?: string }>("/api/settings/test-model", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getDefaultLlm: () =>
    request<{ default_llm: { model: string; credential_id: string | null } | null }>(
      "/api/settings/default-llm"
    ),
  setDefaultLlm: (body: { model: string; credential_id?: string | null }) =>
    request<{ default_llm: { model: string; credential_id: string | null } }>("/api/settings/default-llm", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateCredential: (
    id: string,
    body: { name?: string; base_url?: string; group_id?: string }
  ) => request<Credential>(`/api/settings/credentials/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  getCustomModels: () =>
    request<{ custom: Record<string, { id: string; label: string; provider: string }[]> }>(
      "/api/settings/custom-models"
    ),
  addCustomModel: (capability: string, provider: string, model_id: string) =>
    request<{ id: string; label: string; provider: string }>("/api/settings/custom-models", {
      method: "POST",
      body: JSON.stringify({ capability, provider, model_id }),
    }),
  removeCustomModel: (capability: string, provider: string, model_id: string) => {
    const q = new URLSearchParams({ capability, provider, model_id });
    return request<{ ok: boolean }>(`/api/settings/custom-models?${q.toString()}`, { method: "DELETE" });
  },

  getSkills: () =>
    request<{ skills: { id: string; name: string; description: string; source: string }[] }>(
      "/api/agent/skills"
    ),
  getComfyTemplates: () =>
    request<{ templates: { name: string; size: number }[] }>("/api/settings/comfyui-templates"),
  saveComfyTemplate: (name: string, workflow: unknown) =>
    request<{ ok: boolean; name: string }>(`/api/settings/comfyui-templates/${name}`, {
      method: "PUT",
      body: JSON.stringify(workflow),
    }),
  deleteComfyTemplate: (name: string) =>
    request<{ ok: boolean }>(`/api/settings/comfyui-templates/${name}`, { method: "DELETE" }),

  // 凭据
  getCredentials: () => request<{ credentials: Credential[] }>("/api/settings/credentials"),
  addCredential: (body: { provider: string; name: string; api_key: string; base_url?: string; group_id?: string }) =>
    request<Credential>("/api/settings/credentials", { method: "POST", body: JSON.stringify(body) }),
  deleteCredential: (id: string) => request<{ ok: boolean }>(`/api/settings/credentials/${id}`, { method: "DELETE" }),
  setDefaultCredential: (id: string) =>
    request<Credential>(`/api/settings/credentials/${id}/default`, { method: "POST" }),
  testCredential: (id: string) =>
    request<{ ok: boolean; message: string; latency_ms: number }>(`/api/settings/credentials/${id}/test`, {
      method: "POST",
    }),

  // 上传
  upload: async (file: File, kind: "image" | "audio" | "video") => {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    return request<{ url: string; name: string; size: number }>("/api/uploads", { method: "POST", body: form });
  },

  // Prompt
  generatePrompt: (body: {
    description: string;
    mode: string;
    target_model: string;
    credential_id?: string;
    model?: string;
    duration?: number;
    provider?: string;
  }) => request<{ prompt: string; sections: { title: string; content: string }[] }>("/api/prompt/generate", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  refinePrompt: (body: {
    action: string;
    prompt: string;
    description?: string;
    target_model: string;
    credential_id?: string;
    model?: string;
    provider?: string;
  }) => request<{ prompt: string; sections: { title: string; content: string }[] }>("/api/prompt/refine", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  improvePrompt: (body: {
    prompt: string;
    target_model: string;
    credential_id?: string;
    model?: string;
    provider?: string;
  }) => request<{ prompt: string }>("/api/prompt/improve", { method: "POST", body: JSON.stringify(body) }),
  improveImagePrompt: (body: {
    prompt: string;
    image_model?: string;
  }) => request<{ prompt: string }>("/api/prompt/image", { method: "POST", body: JSON.stringify(body) }),
  getPresets: () => request<{ presets: { id: string; title: string; created_at: string }[] }>("/api/prompt/presets"),
  getPreset: (id: string) => request<{ id: string; title: string; content: string }>(`/api/prompt/presets/${id}`),
  savePreset: (title: string, content: string) =>
    request<{ id: string }>("/api/prompt/presets", { method: "POST", body: JSON.stringify({ title, content }) }),

  // 生成任务
  generateImage: (body: {
    provider: string;
    model: string;
    credential_id?: string;
    prompt: string;
    aspect_ratio: string;
    size?: string;
    n?: number;
    references?: string[];
  }) => request<{ job_id: string; job: Job }>("/api/image/generate", { method: "POST", body: JSON.stringify(body) }),
  generateVoice: (body: {
    provider: string;
    model: string;
    credential_id?: string;
    text: string;
    voice: string;
    speed: number;
    emotion: string;
    pitch?: number;
    vol?: number;
    takes?: number;
  }) => request<{ job_id: string; job: Job }>("/api/voice/generate", { method: "POST", body: JSON.stringify(body) }),
  // 音色资产：设计音色的固化与复用
  listVoiceAssets: () => request<{ assets: VoiceAsset[] }>("/api/voice/assets"),
  saveVoiceAsset: (body: { name: string; kind?: string; desc?: string; sample_from?: string; ref_text?: string; sync_minimax?: boolean }) =>
    request<VoiceAsset>("/api/voice/assets", { method: "POST", body: JSON.stringify(body) }),
  retryVoiceAssetMinimax: (id: string) =>
    request<VoiceAsset>(`/api/voice/assets/${id}/clone-minimax`, { method: "POST", body: JSON.stringify({}) }),
  // MiniMax 音色设计：文字描述 → 永久 voice_id，试听音频固化为资产样本
  designMinimaxVoice: (body: { name: string; prompt: string; preview_text?: string }) =>
    request<VoiceAsset>("/api/voice/design-minimax", { method: "POST", body: JSON.stringify(body) }),
  deleteVoiceAsset: (id: string) => request<{ ok: boolean }>(`/api/voice/assets/${id}`, { method: "DELETE" }),
  generateVideo: (body: {
    provider: string;
    model: string;
    credential_id?: string;
    mode: string;
    prompt: string;
    duration?: number;
    aspect_ratio?: string;
    resolution?: string;
    images?: string[];
    last_images?: string[];
    videos?: string[];
    audios?: string[];
    options?: Record<string, unknown>;
  }) => request<{ job_id: string; job: Job }>("/api/video/generate", { method: "POST", body: JSON.stringify(body) }),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),
  listJobs: (params: { type?: string; status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params.type) q.set("type", params.type);
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    return request<{ jobs: Job[] }>(`/api/jobs?${q.toString()}`);
  },
  deleteJob: (id: string) => request<{ ok: boolean }>(`/api/jobs/${id}`, { method: "DELETE" }),

  // Agent
  agentChat: (body: {
    message: string;
    history?: { role: string; content: string }[];
    page: string;
    scene_title?: string;
    scene_description?: string;
    current_prompt?: string;
    credential_id?: string;
  }) => request<{ reply: string; action: { type: string; value?: string } }>("/api/agent/chat", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  runAgent: (body: AgentRunBody) => request<AgentRunResult>("/api/agent/run", { method: "POST", body: JSON.stringify(body) }),
  // Agent 侧栏模型选单：当前生效模型 + 全部（模型, 凭据）可选项
  getAgentLlmOptions: () => request<AgentLlmOptions>("/api/agent/llm-options"),
  // 流式执行 Agent：SSE 逐步回调工具步骤，结束后返回完整结果
  runAgentStream: async (
    body: AgentRunBody,
    onStep?: (step: AgentRunResult["steps"][number]) => void,
    onStarted?: (runId: string) => void
  ): Promise<AgentRunResult> => {
    const res = await fetch(`${API_BASE}/api/agent/run/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
      } catch {
        /* ignore */
      }
      throw new Error(detail || `请求失败 (${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let final: AgentRunResult | null = null;
    const handleEvent = (chunk: string) => {
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!line) return;
      let evt: { type: string; step?: { step: number; type: string; tool?: string; text?: string; result?: unknown }; result?: AgentRunResult; message?: string; run_id?: string };
      try {
        evt = JSON.parse(line.slice(6));
      } catch {
        return;
      }
      if (evt.type === "step") onStep?.(evt.step!);
      else if (evt.type === "started" && evt.run_id) onStarted?.(evt.run_id);
      else if (evt.type === "error") throw new Error(evt.message || "Agent 运行失败");
      else if (evt.type === "final") final = evt.result ?? null;
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleEvent(chunk);
      }
    }
    if (!final) throw new Error("Agent 连接中断，未收到最终结果（已提交的后台任务不受影响）");
    return final;
  },
  // runId 缺省时后端视为"取消全部"：仅旧客户端兜底，正常都传当前 run 精确取消（防误杀 Studio 的 agent）
  cancelAgent: (runId?: string) =>
    request<{ ok: boolean }>("/api/agent/cancel", { method: "POST", body: JSON.stringify({ run_id: runId ?? "" }) }),};

export async function pollJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  intervalMs = 3000
): Promise<Job> {
  // 容错：瞬时网络错误连续 5 次才判失败；总时长 30 分钟兜底
  let failures = 0;
  const deadline = Date.now() + 30 * 60 * 1000;
  for (;;) {
    let job: Job;
    try {
      job = await api.getJob(jobId);
      failures = 0;
    } catch (err) {
      if (++failures >= 5) throw err;
      await new Promise((r) => setTimeout(r, intervalMs));
      continue;
    }
    onUpdate(job);
    if (job.status === "completed" || job.status === "failed") return job;
    if (Date.now() > deadline) {
      return { ...job, status: "failed", error: job.error || "等待超时（30 分钟），任务可能仍在后台执行，稍后可在历史中查看结果" };
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
