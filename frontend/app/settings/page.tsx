"use client";

import { useEffect, useState } from "react";
import { Check, KeyRound, Loader2, Pencil, Plus, RefreshCw, Star, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { SearchableSelect } from "@/components/common/searchable-select";
import { useAppStore } from "@/stores/app-store";
import type { Credential } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface GeneralInfo {
  app_name: string;
  version: string;
  data_dir: string;
  output_counts: Record<string, number>;
}

export default function SettingsPage() {
  const catalog = useAppStore((s) => s.catalog);
  const credentials = useAppStore((s) => s.credentials);
  const loadCatalog = useAppStore((s) => s.loadCatalog);
  const loadCredentials = useAppStore((s) => s.loadCredentials);

  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState({ provider: "minimax", name: "Main", api_key: "", base_url: "", group_id: "" });
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [general, setGeneral] = useState<GeneralInfo | null>(null);

  // 自定义模型
  const discovered = useAppStore((s) => s.discovered);
  const discoveredTts = useAppStore((s) => s.discoveredTts);
  const discoveredVoices = useAppStore((s) => s.discoveredVoices);
  const voiceErrors = useAppStore((s) => s.voiceErrors);
  const loadDiscovered = useAppStore((s) => s.loadDiscovered);
  const [custom, setCustom] = useState<Record<string, { id: string; label: string; provider: string }[]>>({});
  const [cmForm, setCmForm] = useState({ capability: "llm", provider: "minimax", model_id: "" });
  const [cmSaving, setCmSaving] = useState(false);
  const [editing, setEditing] = useState<Credential | null>(null);
  const [skills, setSkills] = useState<{ id: string; name: string; description: string; source: string }[]>([]);
  const [templates, setTemplates] = useState<{ name: string; size: number }[]>([]);
  const [modelTests, setModelTests] = useState<Record<string, { state: "testing" | "ok" | "fail"; message: string }>>({});
  const [defLlm, setDefLlm] = useState<{ model: string; credential_id: string }>({ model: "", credential_id: "" });
  const [defSaving, setDefSaving] = useState(false);
  const [discByCred, setDiscByCred] = useState<Record<string, string[]>>({});

  // 模型选项按所选凭据过滤：不同 Key 的端点模型不通用（mimo 凭据选 MiniMax 模型必 400），
  // 该凭据的发现列表优先；未选凭据或无发现数据时退回全局列表。搜索词可回车直接作为模型 ID（allowCustom）
  const defCred = credentials.find((c) => c.id === defLlm.credential_id);
  const credModelIds = defCred ? discByCred[defCred.id] ?? [] : [];
  const llmOptions = (
    credModelIds.length
      ? credModelIds.map((id) => ({ id, label: id, provider: defCred!.provider }))
      : [
          ...(catalog?.llm ?? []).filter((m) => !defCred || m.provider === defCred.provider),
          ...Object.entries(discovered).flatMap(([provider, ids]) =>
            !defCred || provider === defCred.provider ? ids.map((id) => ({ id, label: id, provider })) : []
          ),
        ]
  )
    .filter((m, i, arr) => arr.findIndex((x) => x.id === m.id && x.provider === m.provider) === i)
    .map((m) => ({ value: m.id, label: `${m.label}（${catalog?.providers.find((p) => p.id === m.provider)?.label ?? m.provider}）` }));

  const changeDefCredential = (credId: string) => {
    setDefLlm((f) => {
      const ids = discByCred[credId] ?? [];
      const keep = f.model && ids.includes(f.model);
      return { credential_id: credId, model: keep ? f.model : ids[0] ?? "" };
    });
  };

  const saveDefaultLlm = async () => {
    setDefSaving(true);
    try {
      await api.setDefaultLlm({ model: defLlm.model, credential_id: defLlm.credential_id });
      toast.success("默认文本模型已保存，AI 助手与 Prompt 链路即刻生效");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setDefSaving(false);
    }
  };

  const refreshTemplates = () => {
    api.getComfyTemplates().then(({ templates }) => setTemplates(templates)).catch(() => undefined);
  };

  const CAPABILITY_LABELS: Record<string, string> = {
    llm: "文本模型",
    tts: "语音模型",
    image: "生图模型",
    video: "视频模型",
    voice: "音色",
  };

  const refreshCustom = () => {
    api
      .getCustomModels()
      .then(({ custom }) => setCustom(custom))
      .catch(() => undefined);
  };

  useEffect(() => {
    void loadCatalog();
    void loadCredentials();
    void loadDiscovered();
    api
      .getDiscoveredModels()
      .then(({ discovered_by_credential }) => setDiscByCred(discovered_by_credential ?? {}))
      .catch(() => undefined);
    refreshCustom();
    refreshTemplates();
    api.getSkills().then(({ skills }) => setSkills(skills)).catch(() => undefined);
    api.getGeneral().then(setGeneral).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 凭据加载后：若从未设置默认模型，预填默认凭据 + 该凭据端点上发现的第一个模型
  useEffect(() => {
    if (!credentials.length || defLlm.credential_id) return;
    api
      .getDefaultLlm()
      .then(({ default_llm }) => {
        if (default_llm) {
          setDefLlm({ model: default_llm.model, credential_id: default_llm.credential_id ?? "" });
          return;
        }
        const cred = credentials.find((c) => c.is_default) ?? credentials[0];
        setDefLlm((f) => (f.credential_id ? f : { ...f, credential_id: cred.id }));
        api
          .getDiscoveredModels()
          .then(({ discovered_by_credential }) => {
            const ids = discovered_by_credential?.[cred.id] ?? [];
            if (ids.length) setDefLlm((f) => (f.model ? f : { ...f, model: ids[0] }));
          })
          .catch(() => undefined);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentials]);

  const providersForCapability = (capability: string) =>
    (catalog?.providers ?? []).filter((p) =>
      capability === "voice" ? p.capabilities.includes("tts") : p.capabilities.includes(capability)
    );

  const addCustomModel = async () => {
    if (!cmForm.model_id.trim()) {
      toast.error("请填写模型 ID / 音色 ID");
      return;
    }
    setCmSaving(true);
    try {
      await api.addCustomModel(cmForm.capability, cmForm.provider, cmForm.model_id.trim());
      refreshCustom();
      await loadCatalog();
      setCmForm((f) => ({ ...f, model_id: "" }));
      toast.success("已添加，模型选择器中立即可见");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setCmSaving(false);
    }
  };

  const removeCustomModel = async (capability: string, provider: string, modelId: string) => {
    try {
      await api.removeCustomModel(capability, provider, modelId);
      refreshCustom();
      await loadCatalog();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const needsGroup = (editing ? editing.provider : form.provider) in { minimax: 1, openai_compatible: 1 };

  const openAdd = (providerId: string, existingCount: number) => {
    setEditing(null);
    setForm((f) => ({ ...f, provider: providerId, name: existingCount ? `Key ${existingCount + 1}` : "Main", api_key: "", base_url: "", group_id: "" }));
    setAddOpen(true);
  };

  const openEdit = (key: Credential) => {
    setEditing(key);
    setForm((f) => ({ ...f, provider: key.provider, name: key.name, api_key: "", base_url: key.base_url ?? "", group_id: key.group_id ?? "" }));
    setAddOpen(true);
  };

  const saveKey = async () => {
    if (editing) {
      try {
        await api.updateCredential(editing.id, {
          name: form.name || undefined,
          base_url: form.base_url.trim(),
          group_id: form.group_id.trim(),
        });
        toast.success("凭据已更新，正在重新发现模型与音色…");
        setAddOpen(false);
        setEditing(null);
        await loadCredentials();
        await loadDiscovered();
      } catch (err) {
        toast.error((err as Error).message);
      }
      return;
    }
    if (!form.api_key.trim()) {
      toast.error("请填写 API Key");
      return;
    }
    setSaving(true);
    try {
      await api.addCredential({
        provider: form.provider,
        name: form.name || "Main",
        api_key: form.api_key.trim(),
        base_url: form.base_url.trim() || undefined,
        group_id: form.group_id.trim() || undefined,
      });
      toast.success("API Key 已加密保存");
      setAddOpen(false);
      setForm({ provider: "minimax", name: "Main", api_key: "", base_url: "", group_id: "" });
      await loadCredentials();
      await loadDiscovered();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const testKey = async (id: string) => {
    setTesting(id);
    try {
      const res = await api.testCredential(id);
      if (res.ok) toast.success(`连接成功（${res.latency_ms}ms）`);
      else toast.error(`连接失败：${res.message}`);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setTesting(null);
    }
  };

  const deleteKey = async (id: string) => {
    try {
      await api.deleteCredential(id);
      await loadCredentials();
      toast.success("已删除");
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const setDefault = async (id: string) => {
    try {
      await api.setDefaultCredential(id);
      await loadCredentials();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const testModelChip = async (provider: string, modelId: string) => {
    const key = `${provider}:${modelId}`;
    setModelTests((m) => ({ ...m, [key]: { state: "testing", message: "" } }));
    try {
      const cred = credentials.find((c) => c.provider === provider);
      const res = await api.testModel({ provider, credential_id: cred?.id, model: modelId });
      setModelTests((m) => ({ ...m, [key]: { state: res.ok ? "ok" : "fail", message: res.message } }));
    } catch (err) {
      setModelTests((m) => ({ ...m, [key]: { state: "fail", message: (err as Error).message } }));
    }
  };

  const providers = catalog?.providers ?? [];
  const byProvider = (pid: string): Credential[] => credentials.filter((c) => c.provider === pid);

  return (
    <div className="mx-auto h-[calc(100vh-3.5rem)] max-w-4xl overflow-y-auto px-8 py-8">
      <h1 className="mb-6 text-lg font-semibold">Settings</h1>

      <Tabs defaultValue="keys">
        <TabsList className="w-fit">
          <TabsTrigger value="keys">API Keys</TabsTrigger>
          <TabsTrigger value="models">Models</TabsTrigger>
          <TabsTrigger value="general">General</TabsTrigger>
        </TabsList>

        {/* ---------------- API Keys ---------------- */}
        <TabsContent value="keys" className="space-y-5">
          {providers.map((provider) => {
            const keys = byProvider(provider.id);
            return (
              <div key={provider.id} className="rounded-xl border border-border bg-card p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <div className="flex size-8 items-center justify-center rounded-lg bg-secondary">
                      <KeyRound className="size-4 text-muted-foreground" />
                    </div>
                    <div>
                      <div className="text-sm font-semibold">{provider.label}</div>
                      <div className="text-[11px] text-muted-foreground">
                        支持 {provider.capabilities.map((c) => c.toUpperCase()).join(" / ")}
                      </div>
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => openAdd(provider.id, keys.length)}
                  >
                    <Plus className="size-3.5" />
                    Add API Key
                  </Button>
                </div>

                {keys.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-border/70 px-4 py-5 text-center text-xs text-muted-foreground">
                    尚未配置 Key
                  </div>
                ) : (
                  <div className="space-y-3">
                    {keys.map((key) => (
                      <div key={key.id} className="rounded-lg border border-border bg-secondary/40 px-4 py-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-medium">{key.name}</span>
                              {key.is_default && (
                                <Badge className="border-primary/40 bg-primary/10 text-primary">Default</Badge>
                              )}
                            </div>
                            <div className="mt-1 font-mono text-xs tracking-wider text-muted-foreground">
                              {key.masked_key}
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            {testing === key.id ? (
                              <Loader2 className="size-4 animate-spin text-primary" />
                            ) : (
                              <Button size="sm" variant="ghost" onClick={() => void testKey(key.id)}>
                                <Check className="size-3.5" />
                                Test Connection
                              </Button>
                            )}
                            <Button size="sm" variant="ghost" onClick={() => openEdit(key)}>
                              <Pencil className="size-3.5" />
                              编辑
                            </Button>
                            {!key.is_default && (
                              <Button size="sm" variant="ghost" onClick={() => void setDefault(key.id)}>
                                <Star className="size-3.5" />
                                设为默认
                              </Button>
                            )}
                            <Button size="sm" variant="destructive" onClick={() => void deleteKey(key.id)}>
                              <Trash2 className="size-3.5" />
                            </Button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            所有 Key 使用 AES-256-GCM 整体加密后保存于 data/config/providers.enc，前端仅显示掩码。
            Key 只在这里管理：生成请求会自动使用对应厂商设为「默认」的 Key，工具页面无需重复选择。
          </p>
        </TabsContent>

        {/* ---------------- Models ---------------- */}
        <TabsContent value="models" className="space-y-5">
          {/* 自定义模型 / 音色 */}
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-1 text-sm font-semibold">自定义模型与音色</div>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
              模型与音色不再内置陈旧清单：配置凭据后会自动从厂商接口发现（/models、get_voice）；
              也可以在此手动添加任意模型 ID（私有部署、新模型均可），添加后所有选择器立即可用。
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Select value={cmForm.capability} onValueChange={(v) => setCmForm((f) => ({ ...f, capability: v, provider: providersForCapability(v)[0]?.id ?? "" }))}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(CAPABILITY_LABELS).map(([k, label]) => (
                    <SelectItem key={k} value={k}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={cmForm.provider || undefined} onValueChange={(v) => setCmForm((f) => ({ ...f, provider: v }))}>
                <SelectTrigger className="w-44">
                  <SelectValue placeholder="厂商" />
                </SelectTrigger>
                <SelectContent>
                  {providersForCapability(cmForm.capability).map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Input
                className="w-56"
                value={cmForm.model_id}
                onChange={(e) => setCmForm((f) => ({ ...f, model_id: e.target.value }))}
                placeholder={cmForm.capability === "voice" ? "音色 ID" : "模型 ID"}
              />
              <Button size="sm" variant="accent" onClick={() => void addCustomModel()} disabled={cmSaving}>
                {cmSaving ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
                添加
              </Button>
            </div>

            {Object.entries(CAPABILITY_LABELS).map(([cap, label]) =>
              (custom[cap] ?? []).length === 0 ? null : (
                <div key={cap} className="mt-4">
                  <div className="mb-2 text-[11px] font-medium text-muted-foreground">{label}</div>
                  <div className="flex flex-wrap gap-2">
                    {(custom[cap] ?? []).map((m) => (
                      <span
                        key={`${m.provider}:${m.id}`}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/40 px-2.5 py-1 text-xs"
                      >
                        <Badge>{catalog?.providers.find((p) => p.id === m.provider)?.label ?? m.provider}</Badge>
                        {m.label !== m.id ? `${m.label}（${m.id}）` : m.id}
                        <button
                          type="button"
                          onClick={() => void removeCustomModel(cap, m.provider, m.id)}
                          className="text-muted-foreground hover:text-red-400 cursor-pointer"
                        >
                          <Trash2 className="size-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              )
            )}
          </div>

          {/* 自动发现 */}
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-1 flex items-center justify-between">
              <div className="text-sm font-semibold">自动发现</div>
              <Button size="sm" variant="ghost" onClick={() => void loadDiscovered()}>
                <RefreshCw className="size-3.5" />
                刷新
              </Button>
            </div>
            <p className="mb-3 text-[11px] text-muted-foreground">
              来自已配置凭据的厂商接口，随账号可用模型实时更新。<span className="text-foreground/80">点击模型名可测试是否真的可用</span>
              （厂商列表与实际可用性可能不一致）。
            </p>
            {voiceErrors.length > 0 && (
              <div className="mb-3 space-y-1.5">
                {voiceErrors.map((e, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2 text-xs leading-relaxed text-amber-300/90"
                  >
                    {catalog?.providers.find((p) => p.id === e.provider)?.label ?? e.provider} · {e.name}：{e.message}
                  </div>
                ))}
              </div>
            )}
            {Object.keys(discovered).length === 0 &&
            Object.keys(discoveredTts).length === 0 &&
            Object.keys(discoveredVoices).length === 0 ? (
              <div className="rounded-lg border border-dashed border-border/70 px-4 py-5 text-center text-xs text-muted-foreground">
                暂无发现 —— 添加 API Key 后自动拉取
              </div>
            ) : (
              <div className="space-y-3">
                {Object.entries(discovered).map(([provider, ids]) => (
                  <div key={provider}>
                    <div className="mb-2 text-[11px] font-medium text-muted-foreground">
                      {catalog?.providers.find((p) => p.id === provider)?.label ?? provider} · 文本模型（{ids.length}）
                    </div>
                    <div className="flex max-h-36 flex-wrap gap-1.5 overflow-y-auto">
                      {ids.map((id) => {
                        const key = `${provider}:${id}`;
                        const t = modelTests[key];
                        return (
                          <button
                            key={id}
                            type="button"
                            title={t ? t.message : "点击测试该模型"}
                            onClick={() => void testModelChip(provider, id)}
                            className={cn(
                              "flex cursor-pointer items-center gap-1 rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
                              !t && "border-transparent bg-secondary/60 text-foreground/80 hover:border-primary/50",
                              t?.state === "testing" && "border-border bg-secondary/60 text-muted-foreground",
                              t?.state === "ok" && "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
                              t?.state === "fail" && "border-red-500/40 bg-red-500/10 text-red-300 line-through"
                            )}
                          >
                            {t?.state === "testing" && <Loader2 className="size-3 animate-spin" />}
                            {t?.state === "ok" && <Check className="size-3" />}
                            {t?.state === "fail" && <X className="size-3" />}
                            {id}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
                {Object.entries(discoveredTts).map(([provider, ids]) => (
                  <div key={provider}>
                    <div className="mb-2 text-[11px] font-medium text-muted-foreground">
                      {catalog?.providers.find((p) => p.id === provider)?.label ?? provider} · 语音模型（{ids.length}，已并入语音页选择器）
                    </div>
                    <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
                      {ids.map((id) => (
                        <span key={id} className="rounded-md bg-secondary/60 px-2 py-1 font-mono text-[11px] text-foreground/80">
                          {id}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
                {Object.entries(discoveredVoices).map(([provider, list]) => (
                  <div key={provider}>
                    <div className="mb-2 text-[11px] font-medium text-muted-foreground">
                      {catalog?.providers.find((p) => p.id === provider)?.label ?? provider} · 音色（{list.length}）
                    </div>
                    <div className="flex max-h-36 flex-wrap gap-1.5 overflow-y-auto">
                      {list.map((v) => (
                        <span key={v.id} className="rounded-md bg-secondary/60 px-2 py-1 text-[11px] text-foreground/80">
                          {v.label === v.id ? v.id : `${v.label}（${v.id}）`}
                          {v.source === "clone" && <span className="ml-1 text-primary/80">克隆</span>}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 视频模型（内置） */}
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-3 text-sm font-semibold">视频模型</div>
            <div className="flex flex-wrap gap-2">
              {(catalog?.video ?? []).map((m) => (
                <div key={m.id} className="flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-1.5 text-xs">
                  <span className="font-medium">{m.label}</span>
                  <Badge>{catalog?.providers.find((p) => p.id === m.provider)?.label ?? m.provider}</Badge>
                  <span className="text-[10px] text-muted-foreground">
                    {m.durations[0]}-{m.durations[m.durations.length - 1]}s
                  </span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">
              按 v1.0 设计：MiniMax H3 + Generic 先行，Seedance / Wan 已接入；其他厂商通过 Adapter 扩展（backend/app/providers/video/）。
            </p>
          </div>

          {/* 默认文本模型 */}
          <div className="rounded-xl border border-primary/30 bg-card p-5">
            <div className="mb-1 text-sm font-semibold">默认文本模型（Agent / Prompt 链路使用）</div>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
              Agent / 流水线（剧本解析、分镜规划、素材绑定）、AI 助手等所有文本链路都使用这里的选择。
              选凭据后列表只显示该凭据端点上的模型；列表没有的模型可直接搜索后回车手动指定（例如 mimo-v2.6-pro）。
              改完记得点「保存」，保存后对新任务即刻生效。
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <SearchableSelect
                className="w-60"
                items={llmOptions}
                value={defLlm.model}
                onChange={(v) => setDefLlm((f) => ({ ...f, model: v }))}
                placeholder="选择文本模型"
                searchPlaceholder="搜索模型（可手动输入 ID 回车）…"
                allowCustom
                customLabelPrefix="手动指定模型 ID"
              />
              <SearchableSelect
                className="w-60"
                items={credentials
                  .filter((c) => providersForCapability("llm").some((p) => p.id === c.provider))
                  .map((c) => ({ value: c.id, label: `${catalog?.providers.find((p) => p.id === c.provider)?.label ?? c.provider} · ${c.name}` }))}
                value={defLlm.credential_id ?? ""}
                onChange={changeDefCredential}
                placeholder="选择凭据"
              />
              <Button size="sm" variant="accent" onClick={() => void saveDefaultLlm()} disabled={!defLlm.model || !defLlm.credential_id}>
                {defSaving ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
                保存
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!defLlm.model || !defLlm.credential_id}
                onClick={async () => {
                  try {
                    const cred = credentials.find((c) => c.id === defLlm.credential_id);
                    const res = await api.testModel({ provider: cred?.provider ?? "minimax", credential_id: defLlm.credential_id, model: defLlm.model });
                    if (res.ok) toast.success(`默认模型可用：${res.message}`);
                    else toast.error(res.message);
                  } catch (err) {
                    toast.error((err as Error).message);
                  }
                }}
              >
                测试
              </Button>
            </div>
          </div>

          {/* Agent 技能 */}
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-1 text-sm font-semibold">Agent 技能</div>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
              Agent 在「执行」模式写 Prompt 前会自动加载对应技能知识（如 H3 提示词写法、镜头语言）。
              扩展方式：把带 frontmatter（name / description）的 .md 文件放入 data/skills/ 即可，无需重启。
            </p>
            <div className="space-y-1.5">
              {skills.map((s) => (
                <div key={s.id} className="rounded-lg border border-border bg-secondary/40 px-3 py-2 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-foreground">{s.name}</span>
                    <Badge>{s.id}</Badge>
                    <Badge className={s.source === "user" ? "border-primary/40 text-primary" : ""}>{s.source}</Badge>
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">{s.description}</div>
                </div>
              ))}
              {skills.length === 0 && (
                <div className="text-xs text-muted-foreground">加载中…</div>
              )}
            </div>
          </div>
          {/* ComfyUI 本地工作流 */}
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-1 text-sm font-semibold">ComfyUI 本地工作流</div>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
              添加 ComfyUI (本地) 类型的 Key 后，可用本地工作流出图 / 视频 / TTS / 音乐。上传
              ComfyUI「Save (API Format)」导出的 JSON 作为模板，模板名 = 模型 ID
              （内置 comfyui-image / comfyui-video，TTS 可自建 comfyui-tts 等任意名）。
              模板约定：正向提示词写 <span className="font-mono">{"{{prompt}}"}</span>（TTS 写
              <span className="font-mono"> {"{{text}}"} </span>、参考图写
              <span className="font-mono"> {"{{image_name}}"} </span>），
              seed 自动随机，width/height 数值字段按请求自动覆盖。
            </p>
            <div className="flex items-center gap-2">
              <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-3 py-1.5 text-xs hover:border-primary/50">
                <Plus className="size-3.5" />
                上传模板 JSON
                <input
                  type="file"
                  accept=".json"
                  multiple
                  className="hidden"
                  onChange={async (e) => {
                    const files = Array.from(e.target.files ?? []);
                    for (const file of files) {
                      try {
                        const workflow = JSON.parse(await file.text());
                        await api.saveComfyTemplate(file.name.replace(/\.json$/i, ""), workflow);
                      } catch (err) {
                        toast.error(`${file.name}：${(err as Error).message}`);
                      }
                    }
                    e.target.value = "";
                    refreshTemplates();
                    toast.success("模板已上传");
                  }}
                />
              </label>
              <Button size="sm" variant="ghost" onClick={refreshTemplates}>
                <RefreshCw className="size-3.5" />
                刷新
              </Button>
            </div>
            <div className="mt-3 space-y-1.5">
              {templates.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border/70 px-4 py-4 text-center text-xs text-muted-foreground">
                  暂无模板 —— 上传后即可在模型选择器里使用
                </div>
              ) : (
                templates.map((t) => (
                  <div key={t.name} className="flex items-center justify-between rounded-lg border border-border bg-secondary/40 px-3 py-2 text-xs">
                    <span className="font-mono">{t.name}.json</span>
                    <span className="flex items-center gap-2 text-muted-foreground">
                      {(t.size / 1024).toFixed(1)} KB
                      <button
                        type="button"
                        onClick={async () => {
                          try {
                            await api.deleteComfyTemplate(t.name);
                            refreshTemplates();
                          } catch (err) {
                            toast.error((err as Error).message);
                          }
                        }}
                        className="hover:text-red-400 cursor-pointer"
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </TabsContent>
        <TabsContent value="general" className="space-y-5">
          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-3 text-sm font-semibold">General</div>
            <div className="space-y-2 text-xs">
              <div className="flex justify-between border-b border-border/60 py-2">
                <span className="text-muted-foreground">应用版本</span>
                <span>{general?.version ?? "-"}</span>
              </div>
              <div className="flex justify-between border-b border-border/60 py-2">
                <span className="text-muted-foreground">数据目录（无数据库，全部本地文件）</span>
                <span className="font-mono">{general?.data_dir ?? "-"}</span>
              </div>
              <div className="flex justify-between border-b border-border/60 py-2">
                <span className="text-muted-foreground">已生成文件</span>
                <span>
                  图片 {general?.output_counts?.image ?? 0} · 音频 {general?.output_counts?.audio ?? 0} · 视频{" "}
                  {general?.output_counts?.video ?? 0}
                </span>
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card p-5">
            <div className="mb-1 text-sm font-semibold">清理上传素材</div>
            <p className="mb-3 text-[11px] text-muted-foreground">
              删除 data/uploads 中指定天数之前上传的参考素材（生成结果不受影响）。
            </p>
            <div className="flex items-center gap-2">
              {[3, 7, 30].map((d) => (
                <Button
                  key={d}
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    try {
                      const res = await api.cleanupUploads(d);
                      toast.success(`已清理 ${res.deleted} 个 ${d} 天前的上传素材`);
                    } catch (err) {
                      toast.error((err as Error).message);
                    }
                  }}
                >
                  清理 {d} 天前
                </Button>
              ))}
            </div>
          </div>
        </TabsContent>
      </Tabs>

      {/* 新增 / 编辑 Key 对话框 */}
      <Dialog open={addOpen} onOpenChange={(o) => { setAddOpen(o); if (!o) setEditing(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "编辑 API Key" : "新增 API Key"}</DialogTitle>
            <DialogDescription>
              {editing ? "修改后自动重新发现模型与音色" : "Key 将使用 AES-256-GCM 加密存储在本机"}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label>Provider</Label>
              <Select value={editing ? editing.provider : form.provider} onValueChange={(v) => setForm((f) => ({ ...f, provider: v }))} disabled={!!editing}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {providers.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Name</Label>
              <Input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="Main" />
            </div>
            {!editing && (
              <div className="space-y-1.5">
                <Label>API Key</Label>
                <Input
                  type="password"
                  value={form.api_key}
                  onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
                  placeholder={form.provider === "ollama" ? "本地无需真实 Key，填任意字符即可" : "sk-..."}
                />
                {form.provider === "ollama" && (
                  <p className="text-[11px] text-muted-foreground">
                    前提：本机已运行 <span className="font-mono">ollama serve</span>；默认地址
                    <span className="font-mono"> http://localhost:11434/v1</span>，模型列表会自动发现（如 qwen3:8b）。
                  </p>
                )}
              </div>
            )}
            <div className="space-y-1.5">
              <Label>Base URL（可选，留空使用默认）</Label>
              <Input
                value={form.base_url}
                onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                placeholder={providers.find((p) => p.id === (editing ? editing.provider : form.provider))?.default_base_url || "https://..."}
              />
            </div>
            {needsGroup && (
              <div className="space-y-1.5">
                <Label>Group ID（MiniMax 音色 / TTS / 视频接口需要）</Label>
                <Input
                  value={form.group_id}
                  onChange={(e) => setForm((f) => ({ ...f, group_id: e.target.value }))}
                  placeholder="在 MiniMax 控制台「账户管理 → 基本信息」复制"
                />
              </div>
            )}
            <Button variant="accent" className="w-full" onClick={() => void saveKey()} disabled={saving}>
              {saving && <Loader2 className="size-4 animate-spin" />}
              {editing ? "保存修改" : "保存"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
