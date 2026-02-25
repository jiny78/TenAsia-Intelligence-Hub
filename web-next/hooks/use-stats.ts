"use client";
import useSWR from "swr";
import { dashboardApi } from "@/lib/api";
import type { DashboardStats, HealthStatus, CostReport } from "@/lib/types";

export function useStats() {
  return useSWR<DashboardStats>("stats", dashboardApi.stats, {
    refreshInterval: 15_000,
  });
}

export function useHealth() {
  return useSWR<HealthStatus>("health", dashboardApi.health, {
    refreshInterval: 30_000,
  });
}

export function useCostReport() {
  return useSWR<CostReport>("cost-report", dashboardApi.costReport, {
    refreshInterval: 60_000,
  });
}
