import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const ACCENT_GRADIENT = "accent-gradient text-white";

export function fmtTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return "00:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** 音频精确时长（后端 ffprobe 实测，保留实际精度不补零）；无效/缺失返回 null，调用方按需隐藏。 */
export function fmtSeconds(seconds?: number | null): string | null {
  if (seconds == null || !isFinite(seconds) || seconds <= 0) return null;
  return `${Number(seconds.toFixed(2))}s`;
}

export function fmtDate(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

/** 触发浏览器下载：append ?dl=1 让后端返回 Content-Disposition 附件头。url 需已是完整地址（fileUrl() 之后）。 */
export function saveFile(url: string): void {
  const a = document.createElement("a");
  a.href = `${url}?dl=1`;
  a.target = "_blank";
  a.rel = "noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** 与后端 parse_sections 一致：把 "## Title" 分节文本解析为 [{title, content}]。 */
export function parsePromptSections(text: string): { title: string; content: string }[] {
  const sections: { title: string; content: string }[] = [];
  let title: string | null = null;
  let lines: string[] = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^#{1,3}\s+(.+?)\s*$/);
    if (m) {
      if (title !== null) {
        const content = lines.join("\n").trim();
        if (content) sections.push({ title, content });
      }
      title = m[1].replace(/[—-]+$/, "").trim();
      lines = [];
    } else if (title !== null) {
      lines.push(line);
    }
  }
  if (title !== null) {
    const content = lines.join("\n").trim();
    if (content) sections.push({ title, content });
  }
  if (!sections.length && text.trim()) sections.push({ title: "Prompt", content: text.trim() });
  return sections;
}
