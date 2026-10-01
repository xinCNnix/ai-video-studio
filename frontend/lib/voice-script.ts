/**
 * 配音工作台标记语法（与后端 app/services/voice_script.py 同语义，改一处必改另一处）。
 *
 * 片段标记（选中文字后由工具条插入，后端拆段独立合成再拼接）：
 *   [happy]文字[/happy]          片段情绪
 *   [pitch:3]文字[/pitch]        音调 -12~12（MiniMax 半音）
 *   [vol:2]文字[/vol]            音量 1~10
 *   [speed:1.2]文字[/speed]      片段语速 0.5~2.0
 *   [voice:ID]文字[/voice]       片段音色（多角色对话）
 *   <#0.5#>                      停顿（秒，MiniMax 原生写法，全厂商转静音插入）
 * 语气词 (laughs) 与注音 (he2) 是 MiniMax 专属内联标签，其他厂商后端自动剥离。
 */

export const EMOTION_LABELS: Record<string, string> = {
  neutral: "平静",
  happy: "开心",
  sad: "悲伤",
  angry: "愤怒",
  fearful: "恐惧",
  disgusted: "厌恶",
  surprised: "惊讶",
  calm: "沉着",
};

/** MiniMax 语气词标签（仅 speech-2.8-hd/turbo 支持，后端按模型保留/剥离） */
export const PARALANGUAGE_TAGS: { tag: string; label: string }[] = [
  { tag: "laughs", label: "笑" },
  { tag: "chuckle", label: "轻笑" },
  { tag: "sighs", label: "叹气" },
  { tag: "gasps", label: "倒吸气" },
  { tag: "breath", label: "换气" },
  { tag: "pant", label: "喘气" },
  { tag: "inhale", label: "吸气" },
  { tag: "exhale", label: "呼气" },
  { tag: "coughs", label: "咳嗽" },
  { tag: "clear-throat", label: "清嗓子" },
  { tag: "sniffs", label: "吸鼻子" },
  { tag: "sneezes", label: "喷嚏" },
  { tag: "groans", label: "呻吟" },
  { tag: "snorts", label: "喷鼻息" },
  { tag: "burps", label: "打嗝" },
  { tag: "lip-smacking", label: "咂嘴" },
  { tag: "humming", label: "哼唱" },
  { tag: "hissing", label: "嘶声" },
  { tag: "emm", label: "嗯…" },
];

const EMOTIONS = new Set(Object.keys(EMOTION_LABELS));
const PARAM_KEYS = new Set(["pitch", "vol", "speed", "voice"]);
// 纯标点/空白（无可朗读字符）：不单独成段，并入相邻段（与后端 merge 同语义）。
// 注意 JS 的 \W 不含中文（中文属 \p{L}），必须用 Unicode 属性转义。
const PUNCT_ONLY = /^[\s\p{P}\p{S}]+$/u;

const TOKEN_RE = /\[(\/?)([a-zA-Z_-]+)(?::([^\]]+))?\]|<#(\d+(?:\.\d{1,2})?)#>/g;

export type ScriptOverrides = Partial<{
  emotion: string;
  pitch: number;
  vol: number;
  speed: number;
  voice: string;
}>;

export type ScriptPart =
  | { type: "speech"; text: string; overrides: ScriptOverrides }
  | { type: "pause"; seconds: number };

function parseValue(key: string, raw: string | undefined): unknown | null {
  if (key === "voice") return (raw ?? "").trim() || null;
  const v = Number.parseFloat(raw ?? "");
  if (Number.isNaN(v)) return null;
  if (key === "pitch") return Math.max(-12, Math.min(12, Math.round(v)));
  if (key === "vol") return Math.max(1, Math.min(10, Math.round(v)));
  if (key === "speed") return Math.max(0.5, Math.min(2.0, v));
  return null;
}

function sameOverrides(a: ScriptOverrides, b: ScriptOverrides): boolean {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => a[k as keyof ScriptOverrides] === b[k as keyof ScriptOverrides]);
}

/** 标记文本 → 片段序列（相邻同参数段合并；非法 [xxx] 当正文保留） */
export function parseVoiceScript(text: string): ScriptPart[] {
  const parts: ScriptPart[] = [];
  const stack: [string, unknown][] = [];
  let pos = 0;

  const flush = (end: number) => {
    if (end > pos) {
      parts.push({ type: "speech", text: text.slice(pos, end), overrides: Object.fromEntries(stack) as ScriptOverrides });
    }
    pos = end;
  };

  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(text)) !== null) {
    const [, closing, key, value, pause] = m;
    if (pause === undefined && !PARAM_KEYS.has(key) && !EMOTIONS.has(key)) continue; // markdown 链接等原样保留
    flush(m.index);
    pos = m.index + m[0].length;
    if (pause !== undefined) {
      parts.push({ type: "pause", seconds: Number.parseFloat(pause) });
    } else if (PARAM_KEYS.has(key)) {
      if (closing) {
        for (let i = stack.length - 1; i >= 0; i--) {
          if (stack[i][0] === key) {
            stack.splice(i, 1);
            break;
          }
        }
      } else {
        const parsed = parseValue(key, value);
        if (parsed !== null) stack.push([key, parsed]);
      }
    } else if (closing) {
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i][0] === "emotion") {
          stack.splice(i, 1);
          break;
        }
      }
    } else {
      stack.push(["emotion", key]);
    }
  }
  flush(text.length);

  // merge：相邻同参数合并、纯空白丢弃、首部停顿丢弃、纯标点并入相邻段
  const out: ScriptPart[] = [];
  let leadingPunct = "";
  for (const p of parts) {
    if (p.type === "pause") {
      const last = out[out.length - 1];
      if (last?.type === "pause") last.seconds += p.seconds;
      else if (out.length > 0) out.push({ ...p });
      continue;
    }
    if (!p.text.trim()) continue;
    if (PUNCT_ONLY.test(p.text)) {
      const last = out[out.length - 1];
      if (last?.type === "speech") last.text += p.text;
      else leadingPunct += p.text;
      continue;
    }
    const piece =
      leadingPunct !== "" ? { ...p, text: leadingPunct + p.text } : p;
    leadingPunct = "";
    const last = out[out.length - 1];
    if (last?.type === "speech" && sameOverrides(last.overrides, piece.overrides)) last.text += piece.text;
    else out.push(piece);
  }
  return out;
}

/** 去掉全部标记 → 纯朗读文本 */
export function stripVoiceMarkers(text: string): string {
  return parseVoiceScript(text)
    .filter((p): p is Extract<ScriptPart, { type: "speech" }> => p.type === "speech")
    .map((p) => p.text)
    .join("");
}

/** 是否含工作台标记（决定后端走分段合成） */
export function hasVoiceMarkers(text: string): boolean {
  const parts = parseVoiceScript(text);
  if (parts.length !== 1) return true;
  const only = parts[0];
  return only.type === "speech" && Object.keys(only.overrides).length > 0;
}
