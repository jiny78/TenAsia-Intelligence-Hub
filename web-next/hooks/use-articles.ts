"use client";
import useSWR from "swr";
import { articlesApi } from "@/lib/api";
import type { Article } from "@/lib/types";

export function useArticles(params?: {
  translation_pending?: boolean;
  process_status?: string;
  limit?: number;
  offset?: number;
}) {
  const key = ["articles", params];
  return useSWR<Article[]>(key, () => articlesApi.list(params), {
    refreshInterval: 30_000,
  });
}
