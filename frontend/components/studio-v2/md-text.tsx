"use client";

/** 极简 Markdown 渲染：agent 回复实际用到的子集（标题/加粗/行内代码/列表/表格/分隔线）。 */
import type { ReactNode } from "react";

function renderInline(text: string, key: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("`")) {
      nodes.push(
        <code key={`${key}-c${i}`} className="rounded bg-background/70 px-1 py-0.5 font-mono text-[10px]">
          {tok.slice(1, -1)}
        </code>,
      );
    } else {
      nodes.push(<strong key={`${key}-b${i}`} className="font-semibold">{tok.slice(2, -2)}</strong>);
    }
    last = m.index + tok.length;
    i += 1;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function isTableSep(line: string): boolean {
  return /^\|?[\s:|-]+\|?$/.test(line) && line.includes("-");
}

function splitRow(line: string): string[] {
  return line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

export function MdText({ text }: { text: string }) {
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (/^#{1,6}\s/.test(line)) {
      out.push(
        <div key={key++} className="mb-0.5 mt-1.5 font-semibold first:mt-0">
          {renderInline(line.replace(/^#{1,6}\s+/, ""), `h${key}`)}
        </div>,
      );
      i += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      out.push(<hr key={key++} className="my-1.5 border-border" />);
      i += 1;
      continue;
    }

    if (line.trimStart().startsWith("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trimStart().startsWith("|")) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      out.push(
        <table key={key++} className="my-1 w-full border-collapse text-left">
          <thead>
            <tr>{head.map((c, j) => <th key={j} className="border border-border bg-secondary/40 px-1.5 py-0.5 font-semibold">{renderInline(c, `th${key}-${j}`)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>{r.map((c, ci) => <td key={ci} className="border border-border px-1.5 py-0.5">{renderInline(c, `td${key}-${ri}-${ci}`)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }

    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
        i += 1;
      }
      const List = ordered ? "ol" : "ul";
      out.push(
        <List key={key++} className={`my-1 space-y-0.5 pl-4 ${ordered ? "list-decimal" : "list-disc"}`}>
          {items.map((it, j) => <li key={j}>{renderInline(it, `li${key}-${j}`)}</li>)}
        </List>,
      );
      continue;
    }

    // 段落：空行分隔；段内单个换行保留为 <br>
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|[-*]{3,}$|\s*[-*]\s|\s*\d+\.\s|\|)/.test(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    if (para.length) {
      out.push(
        <p key={key++} className="my-0.5 first:mt-0">
          {para.map((l, j) => (
            <span key={j}>
              {j > 0 && <br />}
              {renderInline(l, `p${key}-${j}`)}
            </span>
          ))}
        </p>,
      );
    } else {
      // 兜底：未识别的行原样输出，避免丢内容
      out.push(<p key={key++} className="my-0.5">{renderInline(line, `f${key}`)}</p>);
      i += 1;
    }
  }

  return <div className="space-y-0.5">{out}</div>;
}
