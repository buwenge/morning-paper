"use client";

import { useEffect, useMemo, useState } from "react";
import type { ConnectionState, MorningPaperFeedback, MorningPaperItem, MorningPaperLink } from "@/lib/types";
import { IconBack, IconNewspaper } from "./Icons";
import { formatDateChip, formatDateHeading } from "./DateCalendar";

interface MorningPaperPageProps {
  date: string | null;
  items: MorningPaperItem[];
  hasDraft: boolean;
  error: string | null;
  availableDates: string[];
  feedback: MorningPaperFeedback;
  loading: boolean;
  connectionState: ConnectionState;
  onBack: () => void;
  onRequestDate: (date?: string) => void;
}

function groupBySection(items: MorningPaperItem[]): Array<{ section: string; items: MorningPaperItem[] }> {
  const order: string[] = [];
  const map = new Map<string, MorningPaperItem[]>();
  for (const item of items) {
    if (!map.has(item.section)) {
      map.set(item.section, []);
      order.push(item.section);
    }
    map.get(item.section)!.push(item);
  }
  return order.map((section) => ({ section, items: map.get(section)! }));
}

function formatTokens(tokens?: number): string {
  if (!tokens) return "";
  return tokens >= 1000 ? `约 ${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)} k token` : `约 ${tokens} token`;
}

/** 晨报班查到的原文链接（论文 arXiv 摘要页等）。档案馆不替小予抓这些，只把
 * 来源和体量摆出来；PDF 类一律标"很耗 token"。 */
function RelatedLinks({ links }: { links?: MorningPaperLink[] }) {
  if (!links?.length) return null;
  return (
    <ul className="mt-1.5 space-y-0.5 text-[11px] text-warm-text-secondary/80">
      {links.map((link) => (
        <li key={link.url} className="flex flex-wrap items-center gap-x-1.5">
          <a
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
            className="touch-manipulation text-warm-accent underline decoration-warm-accent/40 underline-offset-2 active:text-warm-accent/70"
            style={{ display: "inline-flex", alignItems: "center", minHeight: 32 }}
          >
            {link.note || link.url} →
          </a>
          {link.hint && <span className="opacity-75">{link.hint}</span>}
          {link.pdf_url && (
            <a
              href={link.pdf_url}
              target="_blank"
              rel="noopener noreferrer"
              className="touch-manipulation text-warm-accent/80 underline decoration-warm-accent/30 underline-offset-2 active:text-warm-accent/60"
              style={{ display: "inline-flex", alignItems: "center", minHeight: 32 }}
            >
              PDF 全文 →
            </a>
          )}
        </li>
      ))}
    </ul>
  );
}

function ArchiveToggle({ item }: { item: MorningPaperItem }) {
  const [expanded, setExpanded] = useState(false);
  if (!item.archive_text) return null;
  const sizeChip = item.archive_kind === "x_tweet"
    ? "只有这条推文的文字"
    : formatTokens(item.archive_est_tokens);
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="touch-manipulation rounded-lg px-2 -mx-2 text-[12px] font-medium text-warm-accent transition-colors active:text-warm-accent/70"
        style={{ minHeight: 44 }}
      >
        {expanded ? "收起全文 ▲" : "展开读全文 ▼"}
        {sizeChip && <span className="ml-1.5 font-normal text-warm-text-secondary/60">{sizeChip}</span>}
      </button>
      {expanded && (
        <div className="mt-1.5 max-h-[50vh] overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-warm-thinking/60 p-3 text-[13px] leading-relaxed text-warm-text-secondary">
          {item.archive_text}
        </div>
      )}
    </div>
  );
}

function MorningPaperItemCard({ item }: { item: MorningPaperItem }) {
  return (
    <article className="py-4">
      <h3 className="font-serif text-[17px] font-bold leading-snug text-warm-text">
        <span className="mr-1.5 text-warm-accent" aria-hidden="true">◆</span>
        {item.title}
      </h3>
      <p className="mt-1.5 whitespace-pre-wrap break-words text-[14px] leading-relaxed text-warm-text">
        {item.digest}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-warm-text-secondary/70">
        {item.source && <span>{item.source}</span>}
        {item.source && <span aria-hidden="true">·</span>}
        <span>{item.url_warning}</span>
        <span aria-hidden="true">·</span>
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="touch-manipulation text-warm-accent underline decoration-warm-accent/40 underline-offset-2 active:text-warm-accent/70"
          style={{ display: "inline-flex", alignItems: "center", minHeight: 32 }}
        >
          读原文 →
        </a>
      </div>
      <RelatedLinks links={item.links} />
      <ArchiveToggle item={item} />
    </article>
  );
}

/** 小予当前在追的新闻 + 最近打了 0 分（不感兴趣）的新闻——跟具体哪天的
 * 晨报无关，是跨天的反馈状态，折叠着放在日期条下面，默认收起。 */
function FeedbackStatusPanel({ feedback }: { feedback: MorningPaperFeedback }) {
  const [expanded, setExpanded] = useState(false);
  const { following, not_interested: notInterested } = feedback;
  if (following.length === 0 && notInterested.length === 0) return null;

  return (
    <div className="mx-4 mb-2 rounded-xl bg-warm-thinking/50">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="touch-manipulation flex w-full items-center justify-between gap-2 px-3 text-[12px] font-medium text-warm-text-secondary active:text-warm-text"
        style={{ minHeight: 44 }}
      >
        <span>
          {following.length > 0 && `在追 ${following.length} 条`}
          {following.length > 0 && notInterested.length > 0 && " · "}
          {notInterested.length > 0 && `不感兴趣 ${notInterested.length} 条`}
        </span>
        <span aria-hidden="true">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="space-y-3 px-3 pb-3 text-[12px] leading-relaxed">
          {following.length > 0 && (
            <div>
              <div className="mb-1 font-medium text-warm-text-secondary/80">在追的</div>
              <ul className="space-y-1">
                {following.map((f) => (
                  <li key={f.id} className="flex flex-wrap items-baseline gap-x-1.5 text-warm-text">
                    <a
                      href={f.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="touch-manipulation underline decoration-warm-accent/40 underline-offset-2 active:text-warm-accent/70"
                      style={{ display: "inline-flex", alignItems: "center", minHeight: 32 }}
                    >
                      {f.title}
                    </a>
                    <span className="text-warm-text-secondary/60">（{f.issue_date}）</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {notInterested.length > 0 && (
            <div>
              <div className="mb-1 font-medium text-warm-text-secondary/80">不感兴趣（近 30 天打过 0 分）</div>
              <ul className="space-y-1">
                {notInterested.map((item, i) => (
                  <li key={`${item.issue_date}-${i}-${item.title}`} className="text-warm-text-secondary/80">
                    {item.title}
                    <span className="text-warm-text-secondary/50">
                      {" "}（{item.section}・{item.issue_date}）
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function MorningPaperPage({
  date, items, hasDraft, error, availableDates, feedback, loading, connectionState,
  onBack, onRequestDate,
}: MorningPaperPageProps) {
  useEffect(() => {
    onRequestDate();
    // 只在页面挂载时请求一次当天（不带 date 参数，后端默认今天）；
    // 历史日期切换由下面的日期条按钮触发，不放进这个依赖数组里，
    // 避免每次 onRequestDate 引用变化时重复发请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sections = useMemo(() => groupBySection(items), [items]);
  const heading = date ? formatDateHeading(date) : "";

  return (
    <div className="h-full flex flex-col bg-warm-bg">
      {/* Header */}
      <div className="bar-blend px-4 pt-[calc(0.75rem_+_var(--sat))] pb-3 flex items-center gap-3">
        <button
          onClick={onBack}
          className="touch-manipulation p-2 -ml-2 rounded-xl hover:bg-warm-bg/50 active:bg-warm-bg/70 md:hidden"
          style={{ minWidth: 44, minHeight: 44 }}
        >
          <IconBack className="w-5 h-5" />
        </button>
        <h1 className="text-base font-medium text-warm-text">小予晨报</h1>
        <div className="flex-1" />
      </div>

      {/* 日期条：横向可滚动，触摸区≥44px */}
      {availableDates.length > 0 && (
        <div className="flex gap-2 overflow-x-auto px-4 pb-2 pt-1" style={{ scrollbarWidth: "none" }}>
          {availableDates.map((d) => (
            <button
              key={d}
              onClick={() => onRequestDate(d)}
              className={`shrink-0 touch-manipulation rounded-full px-3.5 text-[12px] font-medium transition-all active:scale-95 ${
                d === date
                  ? "bg-warm-accent text-white"
                  : "bg-warm-thinking text-warm-text-secondary hover:text-warm-text"
              }`}
              style={{ minHeight: 36 }}
            >
              {formatDateChip(d)}
            </button>
          ))}
        </div>
      )}

      <FeedbackStatusPanel feedback={feedback} />

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 pb-24">
        {connectionState !== "online" ? (
          <div className="flex flex-col items-center justify-center gap-2 h-40 text-center text-warm-text-secondary/70">
            <div className="w-5 h-5 border-2 border-warm-accent/30 border-t-warm-accent rounded-full animate-spin" />
            <span className="text-[13px]">{connectionState === "offline" ? "正在重新连接…" : "正在同步…"}</span>
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center h-40">
            <div className="w-5 h-5 border-2 border-warm-accent/30 border-t-warm-accent rounded-full animate-spin" />
          </div>
        ) : !hasDraft ? (
          <div className="flex flex-col items-center justify-center gap-2 py-16 text-center text-warm-text-secondary/70">
            <IconNewspaper className="w-8 h-8 opacity-40" />
            <span className="text-[14px]">这天没有晨报</span>
            <span className="max-w-[240px] text-[11px] leading-relaxed opacity-70">
              {error && error.includes("缺席")
                ? "晨报班那天没交稿，或者这天还没到——不是故障。"
                : "宁缺毋滥，偶尔一天没有很正常。"}
            </span>
          </div>
        ) : (
          <>
            {/* 报头 */}
            <div className="pt-2 pb-3 text-center">
              <div className="font-serif text-[26px] font-bold tracking-wide text-warm-text">《小予晨报》</div>
              <div className="mt-1 text-[12px] text-warm-text-secondary/75">
                {heading}　共 {items.length} 条
              </div>
              <div className="mt-3 border-t-2 border-double border-warm-text-secondary/30" />
            </div>

            {sections.map(({ section, items: sectionItems }) => (
              <section key={section} className="mb-1">
                <h2 className="mt-3 border-b border-warm-text-secondary/25 pb-1.5 font-serif text-[15px] font-bold tracking-wide text-warm-text">
                  {section}
                </h2>
                <div className="divide-y divide-warm-text-secondary/10">
                  {sectionItems.map((item, i) => (
                    <MorningPaperItemCard key={`${section}-${i}-${item.title}`} item={item} />
                  ))}
                </div>
              </section>
            ))}

            <div className="mt-4 pt-3 text-center text-[11px] leading-relaxed text-warm-text-secondary/55">
              这份报纸是熟食，不是任务——读不读、读几条都随你。
            </div>
          </>
        )}
      </div>
    </div>
  );
}
