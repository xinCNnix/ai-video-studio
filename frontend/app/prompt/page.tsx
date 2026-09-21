"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { loadModelPref, saveModelPref } from "@/lib/model-prefs";
import { parsePromptSections } from "@/lib/utils";
import { useAppStore } from "@/stores/app-store";
import type { PromptSection } from "@/lib/types";
import { PromptEditor } from "@/components/prompt/prompt-editor";
import { PromptResult } from "@/components/prompt/prompt-result";

export default function PromptPage() {
  const router = useRouter();
  const catalog = useAppStore((s) => s.catalog);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);
  const setPendingVideo = useAppStore((s) => s.setPendingVideo);
  const setPendingStudio = useAppStore((s) => s.setPendingStudio);

  const [description, setDescription] = useState("");
  const [mode, setMode] = useState("text_to_video");
  const [modelId, setModelIdRaw] = useState(() => loadModelPref("llm"));
  const setModelId = (id: string) => {
    setModelIdRaw(id);
    saveModelPref("llm", id);
  };
  const [targetModel, setTargetModel] = useState("minimax_h3");

  const [sections, setSections] = useState<PromptSection[]>([]);
  const [raw, setRaw] = useState("");
  const [generating, setGenerating] = useState(false);
  const [refining, setRefining] = useState<string | null>(null);

  useEffect(() => {
    void loadCatalog();
    void loadCredentials();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (catalog?.llm.length && !catalog.llm.some((m) => m.id === modelId)) {
      setModelId(catalog.llm[0].id);
    }
  }, [catalog, modelId]);

  const run = async (fn: () => Promise<{ prompt: string; sections: PromptSection[] }>) => {
    const result = await fn();
    setRaw(result.prompt);
    setSections(result.sections);
  };

  const modelProvider = catalog?.llm.find((m) => m.id === modelId)?.provider;

  const generate = async () => {
    setGenerating(true);
    try {
      await run(() =>
        api.generatePrompt({
          description,
          mode,
          target_model: targetModel,
          model: modelId || undefined,
          provider: modelProvider,
        })
      );
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setGenerating(false);
    }
  };

  const refine = async (action: string) => {
    setRefining(action);
    try {
      await run(() =>
        api.refinePrompt({
          action,
          prompt: raw,
          description,
          target_model: targetModel,
          model: modelId || undefined,
          provider: modelProvider,
        })
      );
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setRefining(null);
    }
  };

  const sendToVideo = () => {
    const modeMap: Record<string, "text" | "image" | "reference" | "first_last"> = {
      text_to_video: "text",
      image_to_video: "image",
      reference_video: "reference",
      first_last_frame: "first_last",
    };
    setPendingVideo({ prompt: raw, mode: modeMap[mode] ?? "text" });
    router.push("/video");
  };

  // 提示词发到 Studio 的投放剧本框：多次发送会追加，由 Studio 流水线拆分镜
  const sendToStudio = () => {
    setPendingStudio({ text: raw });
    router.push("/studio");
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      <PromptEditor
        description={description}
        onDescriptionChange={setDescription}
        mode={mode}
        onModeChange={setMode}
        modelId={modelId}
        onModelChange={setModelId}
        targetModel={targetModel}
        onTargetModelChange={setTargetModel}
        onGenerate={() => void generate()}
        generating={generating}
      />
      <PromptResult
        sections={sections}
        raw={raw}
        loading={generating}
        refining={refining}
        onRefine={(action) => void refine(action)}
        onRegenerate={() => void refine("regenerate")}
        onSendToVideo={sendToVideo}
        onSendToStudio={sendToStudio}
        onApplyRaw={(text) => {
          setRaw(text);
          setSections(parsePromptSections(text));
        }}
      />
    </div>
  );
}
