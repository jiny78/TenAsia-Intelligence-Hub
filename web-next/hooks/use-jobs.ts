"use client";
import useSWR from "swr";
import { scraperApi } from "@/lib/api";
import type { ScrapeJob } from "@/lib/types";

export function useJobs(limit = 30) {
  return useSWR<ScrapeJob[]>(["jobs", limit], () => scraperApi.jobs(limit), {
    refreshInterval: 5_000,
  });
}
