/** 素材上传序号：与后端 bind 阶段 images_in_order / audios_in_order 的编号语义严格同源。
 *
 * storyboard 分镜稿的 <Image N>/<Audio N> 全局编号按上传顺序对齐——序号是用户核对
 * "哪张图对应原稿哪个编号"的唯一锚点，前端展示必须复刻同一套去重规则
 * （同 URL 跳过、同语义文件名折叠），否则显示的编号会和实际绑定错位。
 */

export function semanticFileKey(nameOrUrl?: string | null): string {
  let name = (nameOrUrl ?? "").split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  if (dot > 0) name = name.slice(0, dot);
  return name.replace(/^[0-9a-f]{8}_/, "").replace(/\s+/g, "").toLowerCase();
}

export interface OrderableMaterial {
  kind: string;
  filename?: string | null;
  url?: string | null;
}

/** 给素材列表按 kind 分别编上传序号（1 起）；重复拷贝（同 URL 或同语义文件名）返回
 * null 不占号——与后端 intake 入池判重一致。列表通常传 [池内素材, ...本次待投]。 */
export function materialOrders<T extends OrderableMaterial>(list: T[]): (number | null)[] {
  const seenUrls = new Set<string>();
  const seenKeys = new Set<string>();
  const counters: Record<string, number> = {};
  return list.map((m) => {
    if (m.kind === "script" || m.kind === "doc") return null;
    if (m.url) {
      if (seenUrls.has(m.url)) return null;
      seenUrls.add(m.url);
    }
    const fk = semanticFileKey(m.filename || m.url || "");
    const key = `${m.kind}\u0000${fk}`;
    if (fk && seenKeys.has(key)) return null;
    seenKeys.add(key);
    counters[m.kind] = (counters[m.kind] ?? 0) + 1;
    return counters[m.kind];
  });
}

const KIND_LABELS: Record<string, string> = { image: "图", audio: "音", video: "视" };

export function orderLabel(kind: string, order: number | null | undefined): string {
  if (!order || !KIND_LABELS[kind]) return "";
  return `${KIND_LABELS[kind]}${order}`;
}

/** 展示用文件名：剥上传服务加的 8 位哈希前缀（对用户只是噪音）。 */
export function displayFilename(name?: string | null): string {
  return (name ?? "").replace(/^[0-9a-f]{8}_/, "");
}
