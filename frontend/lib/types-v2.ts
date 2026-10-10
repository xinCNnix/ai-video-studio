/** 项目 v2 状态模型（与 backend/app/harness/state.py 对齐）。 */

export type MaterialKind = "image" | "audio" | "video" | "doc" | "script";
export type ImageRole = "reference" | "first_frame" | "last_frame";
/** first_last：按镜覆盖的首尾帧模式（首帧=关键帧/首帧绑定，尾帧=尾帧绑定，不传参考素材） */
export type GenMode = "i2v" | "t2v" | "first_last";
export type SceneStatus = "planned" | "ready" | "generating" | "done" | "failed";
export type PipelineStage =
  | "empty" | "intake" | "normalize" | "plan" | "bind" | "review" | "generate" | "assemble";
export type PipelineState = "idle" | "running" | "paused" | "failed" | "done";

export interface ProjectSettings {
  video_model: string | null;
  image_model: string | null;
  voice_model: string | null;
  voice_id: string | null;
  resolution: string;
  auto_generate: boolean;
  /** plan 导演润色遍开关（关闭=直接采用草稿 Prompt） */
  prompt_polish: boolean;
}

export interface Material {
  id: string;
  kind: MaterialKind;
  url: string | null;
  filename: string;
  text: string | null;
  duration: number | null;
  added_at: string;
}

export interface ImageBinding {
  material_id: string;
  role: ImageRole;
}

export interface SceneBinding {
  images: ImageBinding[];
  audios: string[];
  videos: string[];
}

export interface SceneAssets {
  keyframe: string | null;
  voice: string | null;
  video: string | null;
  keyframe_job: string | null;
  voice_job: string | null;
  video_job: string | null;
}

export interface SceneV2 {
  id: string;
  title: string;
  description: string;
  prompt: string;
  keyframe_prompt: string;
  narration: string;
  duration: number;
  gen_mode: GenMode;
  binding: SceneBinding;
  assets: SceneAssets;
  status: SceneStatus;
  error: string | null;
  revision: number;
  attempts: Record<string, number>;
}

export interface PendingBinding {
  material_id: string;
  note: string;
  suggested_scene_id: string | null;
}

export interface PipelineInfo {
  stage: PipelineStage;
  state: PipelineState;
  run_id: string | null;
  awaiting: string | null;
  error: string | null;
  history: { stage: string; status: string; at: string; note?: string; run_id?: string }[];
}

export interface CharacterV2 {
  name: string;
  design: string;
}

export interface ProjectV2 {
  schema_version: number;
  id: string;
  name: string;
  aspect_ratio: string;
  version: number;
  settings: ProjectSettings;
  materials: Material[];
  characters: CharacterV2[];
  screenplay: Record<string, unknown> | null;
  scenes: SceneV2[];
  pipeline: PipelineInfo;
  pending_bindings: PendingBinding[];
  final_video: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectSummaryV2 {
  id: string;
  name: string;
  aspect_ratio: string;
  scene_count: number;
  pipeline_stage: PipelineStage | null;
  pipeline_state: PipelineState | null;
  version: number;
  updated_at?: string;
}

export const PIPELINE_STAGE_LABELS: Record<string, string> = {
  intake: "素材登记",
  normalize: "剧本解析",
  plan: "分镜规划",
  bind: "素材分配",
  review: "确认门禁",
  generate: "逐镜生成",
  assemble: "拼接成片",
};

export const SCENE_STATUS_LABELS: Record<SceneStatus, string> = {
  planned: "已规划",
  ready: "就绪",
  generating: "生成中",
  done: "完成",
  failed: "失败",
};
