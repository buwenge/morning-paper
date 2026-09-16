"use client";

import { useCallback, useState } from "react";
import type { MorningPaperFeedback, MorningPaperItem, WsDownMessage } from "@/lib/types";

const EMPTY_FEEDBACK: MorningPaperFeedback = { following: [], not_interested: [] };

export function useMorningPaper() {
  const [date, setDate] = useState<string | null>(null);
  const [items, setItems] = useState<MorningPaperItem[]>([]);
  const [hasDraft, setHasDraft] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<MorningPaperFeedback>(EMPTY_FEEDBACK);
  const [loading, setLoading] = useState(false);
  // 请求过、但当天草稿确实不存在的日期集合——跟"还没请求过"区分开，
  // 前端才能把"这天没有晨报"渲染成正常空态而不是一直转圈等结果。
  const [requestedDates, setRequestedDates] = useState<Set<string>>(new Set());

  const handleWsMessage = useCallback((msg: WsDownMessage) => {
    if (msg.type !== "morning_paper") return;
    setDate(msg.date);
    setItems(msg.items);
    setHasDraft(msg.has_draft);
    setError(msg.error);
    setAvailableDates(msg.available_dates);
    setFeedback(msg.feedback ?? EMPTY_FEEDBACK);
    setRequestedDates((prev) => new Set(prev).add(msg.date));
    setLoading(false);
  }, []);

  const requestDate = useCallback((send: (msg: Record<string, unknown>) => void, forDate?: string) => {
    setLoading(true);
    send({ type: "get_morning_paper", ...(forDate ? { date: forDate } : {}) });
  }, []);

  return {
    date, items, hasDraft, error, availableDates, feedback, loading, requestedDates,
    handleWsMessage, requestDate,
  };
}
