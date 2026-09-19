export interface Credential {
  id: string;
  provider: string;
  name: string;
  masked_key: string;
  base_url: string;
  group_id: string;
  is_default: boolean;
  created_at?: string;
}

export interface ProviderInfo {
  id: string;
  label: string;
  capabilities: string[];
  default_base_url: string;
  fields: string[];
}

export interface ModelInfo {
  id: string;
  label: string;
  provider: string;
}

export interface VideoModelInfo extends ModelInfo {
  durations: number[];
  aspect_ratios: string[];
  resolutions: string[];
  modes: string[];
  /** AutoDL 工作流入参契约（后端 AUTODL_WORKFLOW_SPECS 下发；其他 provider 无此字段） */
  min_images?: number;
  max_images?: number;
  accepts_audios?: boolean;
  max_audios?: number;
  accepts_videos?: boolean;
  requires_prompt?: boolean;
}

export interface ModelCatalog {
  providers: ProviderInfo[];
  llm: ModelInfo[];
  tts: ModelInfo[];
  tts_voices: { id: string; label: string }[];
  emotions: string[];
  image: ModelInfo[];
  video: VideoModelInfo[];
  prompt_modes: string[];
  refine_actions: string[];
}

/** 音色资产：voicedesign 设计后固化的音色（clone=带样本稳定复用 / design=仅描述会漂移） */
export interface VoiceAsset {
  id: string;
  name: string;
  kind: "clone" | "design";
  desc: string;
  has_sample: boolean;
  sample_url: string | null;
  /** MiniMax 服务端克隆音色 ID：有值时该资产可直接用于 speech 系列模型 */
  minimax_voice_id?: string | null;
  /** 同步克隆 MiniMax 失败时的原因（仅保存响应携带，列表接口不带） */
  minimax_error?: string | null;
  created_at?: string;
}

export type JobStatus =
  | "queued"
  | "submitted"
  | "generating"
  | "downloading"
  | "completed"
  | "failed";

export interface SelfCheck {
  ok: boolean;
  checks: { name: string; pass: boolean; detail: string }[];
}

export interface Job {
  id: string;
  type: "video" | "image" | "tts";
  provider: string;
  model: string;
  status: JobStatus;
  provider_task_id: string | null;
  params: Record<string, unknown>;
  output: string | null;
  outputs: string[];
  error: string | null;
  created_at: string;
  updated_at?: string;
  self_check?: SelfCheck | null;
}

export interface PromptSection {
  title: string;
  content: string;
}
