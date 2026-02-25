"use client";
import useSWR from "swr";
import { automationApi } from "@/lib/api";
import type { AutomationSummary, AutoResolutionLog, ConflictFlag } from "@/lib/types";

export function useAutomationSummary() {
  return useSWR<AutomationSummary>("automation-summary", automationApi.summary, {
    refreshInterval: 30_000,
  });
}

export function useAutomationFeed(params?: { limit?: number; resolution_type?: string }) {
  const key = `automation-feed-${params?.resolution_type ?? "all"}-${params?.limit ?? 50}`;
  return useSWR<AutoResolutionLog[]>(key, () => automationApi.feed(params), {
    refreshInterval: 30_000,
  });
}

export function useAutomationConflicts(status: string = "OPEN") {
  return useSWR<ConflictFlag[]>(`automation-conflicts-${status}`, () =>
    automationApi.conflicts({ status }),
  {
    refreshInterval: 30_000,
  });
}
