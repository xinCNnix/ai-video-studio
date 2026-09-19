"use client";

import { Sparkles } from "lucide-react";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/common/segmented";
import { ModelSelector } from "@/components/common/model-selector";
import { GenerationButton } from "@/components/common/generation-button";
import { useAppStore } from "@/stores/app-store";

const EXAMPLE =
  "一个女孩去医院看白癜风，医生向她解释白斑并不是一定要大量忌口，3D动画风格，大约5秒。";

export interface PromptEditorProps {
  description: string;
  onDescriptionChange: (v: string) => void;
  mode: string;
  onModeChange: (m: string) => void;
  modelId: string;
  onModelChange: (id: string) => void;
  targetModel: string;
  onTargetModelChange: (t: string) => void;
  onGenerate: () => void;
  generating: boolean;
}

export function PromptEditor(props: PromptEditorProps) {
  const catalog = useAppStore((s) => s.catalog);
  const providerLabels = Object.fromEntries((catalog?.providers ?? []).map((p) => [p.id, p.label]));

  return (
    <div className="flex h-full w-[45%] shrink-0 flex-col gap-5 overflow-y-auto border-r border-border bg-card/40 px-6 py-6">
      <h2 className="text-sm font-semibold">Video Prompt</h2>

      <div className="space-y-2">
        <Label>描述你想生成的视频</Label>
        <Textarea
          value={props.description}
          onChange={(e) => props.onDescriptionChange(e.target.value)}
          placeholder={`描述你想生成的视频……\n\n例如：${EXAMPLE}`}
          className="min-h-[170px] resize-none leading-relaxed"
        />
      </div>

      <div className="space-y-2">
        <Label>Prompt Mode</Label>
        <Segmented
          options={[
            { value: "text_to_video", label: "Text to Video" },
            { value: "image_to_video", label: "Image to Video" },
            { value: "reference_video", label: "Reference Video" },
            { value: "first_last_frame", label: "First / Last Frame" },
          ]}
          value={props.mode}
          onChange={props.onModeChange}
        />
      </div>

      <div className="space-y-2">
        <Label>Prompt Model</Label>
        <ModelSelector
          models={catalog?.llm ?? []}
          value={props.modelId}
          onChange={props.onModelChange}
          providerLabels={providerLabels}
        />
      </div>

      <div className="space-y-2">
        <Label>Target Video Model</Label>
        <Segmented
          options={[
            { value: "minimax_h3", label: "MiniMax H3" },
            { value: "generic", label: "Generic Video" },
          ]}
          value={props.targetModel}
          onChange={props.onTargetModelChange}
        />
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {props.targetModel === "generic"
            ? "生成通用视频语义的 Prompt，可迁移到任意视频模型。"
            : "按 MiniMax H3 的结构化风格生成：主体、动作时间线、镜头、声音与限制条件。"}
        </p>
      </div>

      <div className="mt-auto pt-2">
        <GenerationButton
          loading={props.generating}
          loadingLabel="Writing Prompt..."
          onClick={props.onGenerate}
        >
          <Sparkles className="size-4" />
          Generate Prompt
        </GenerationButton>
      </div>
    </div>
  );
}
