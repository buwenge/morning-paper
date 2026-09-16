"use client";

import { useState } from "react";
import { IconChevron, IconNewspaper } from "./Icons";

interface MorningPaperFoldCardProps {
  text: string;
  count?: number;
  onOpenMorningPaper?: () => void;
}

export function MorningPaperFoldCard({ text, count, onOpenMorningPaper }: MorningPaperFoldCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="my-1 w-full overflow-hidden rounded-xl border border-amber-400/25 bg-amber-400/[0.06] text-amber-700/85 dark:text-amber-200/80 animate-fade-in">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex min-h-11 w-full touch-manipulation items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-white/20 active:bg-black/[0.035] dark:hover:bg-white/[0.035]"
      >
        <IconNewspaper className="h-4 w-4 shrink-0" />
        <span className="min-w-0 flex-1 text-[12px] font-medium">
          📰 今日晨报已送达{count ? ` · ${count} 条` : ""}
        </span>
        {onOpenMorningPaper && (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); onOpenMorningPaper(); }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.stopPropagation(); onOpenMorningPaper(); }
            }}
            className="shrink-0 touch-manipulation rounded-lg px-1.5 text-[11px] font-medium underline decoration-current/40 underline-offset-2"
            style={{ minHeight: 32, display: "inline-flex", alignItems: "center" }}
          >
            去晨报页
          </span>
        )}
        <IconChevron className={`h-3 w-3 shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`} />
      </button>
      {expanded && (
        <div className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap break-words border-t border-current/10 px-3 py-2.5 text-[12px] leading-relaxed opacity-90">
          {text}
        </div>
      )}
    </div>
  );
}
