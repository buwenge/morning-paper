// S6：聊天气泡防刷屏——检测消息正文里的 `[MORNING_PAPER]` 段（晨报班写进
// wakeup 注入里的整份报纸原文，见生产 morning_paper.format_injection），
// 把它从普通文本里挖出来单独渲染成折叠卡片，其余文本原样正常显示。只做
// 前端展示切分，不改消息本体、不改注入内容本身。
//
// 匹配范围：从 `[MORNING_PAPER]` 开始，优先匹配到 format_injection 页脚
// 固定收尾句「…她不会等着你交作业。」为止（这是最精确的边界）；如果那句
// 因为截断/未来文案改动而没出现，退而求其次匹配到下一个形如 `\n[XXX]`
// 的注入标签开头（比如 `[NOTES]`/`[WAKEUP]`）为止；两者都没有就一直吃到
// 字符串末尾——三种情况按顺序尝试，惰性匹配保证选最短的那一种。

const MORNING_PAPER_RE =
  /\[MORNING_PAPER\][\s\S]*?(?:她不会等着你交作业。|(?=\n\[[A-Z][A-Z_]*\])|$)/g;

const BULLET_RE = /◆ /g;

export interface MorningPaperFoldSegment {
  type: "text" | "morning_paper";
  value: string;
  /** 仅 type === "morning_paper" 时有意义：这段里出现的条目数（数◆项符号计数）。 */
  count?: number;
}

export function splitMorningPaperSegments(text: string): MorningPaperFoldSegment[] {
  if (!text.includes("[MORNING_PAPER]")) {
    return [{ type: "text", value: text }];
  }
  const segments: MorningPaperFoldSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(MORNING_PAPER_RE)) {
    const idx = match.index ?? 0;
    if (idx > last) segments.push({ type: "text", value: text.slice(last, idx) });
    const block = match[0];
    const count = (block.match(BULLET_RE) || []).length;
    segments.push({ type: "morning_paper", value: block, count });
    last = idx + block.length;
  }
  if (last < text.length) segments.push({ type: "text", value: text.slice(last) });
  return segments.length > 0 ? segments : [{ type: "text", value: text }];
}
