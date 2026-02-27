// 로컬 개발: NEXT_PUBLIC_API_URL=http://localhost:8000 (.env 설정)
// 프로덕션: NEXT_PUBLIC_API_URL 미설정 → /api 프록시 경유 (next.config.ts rewrites)
const BASE = process.env.NEXT_PUBLIC_API_URL ?? "/api";

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ── Articles ───────────────────────────────────────────────────
export const articlesApi = {
  list: (params?: { translation_pending?: boolean; process_status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.translation_pending) q.set("translation_pending", "true");
    if (params?.process_status) q.set("process_status", params.process_status);
    if (params?.limit !== undefined) q.set("limit", String(params.limit));
    if (params?.offset !== undefined) q.set("offset", String(params.offset));
    return request<import("./types").Article[]>(`/articles?${q}`);
  },
  patch: (id: number, body: import("./types").ArticlePatch) =>
    request<import("./types").Article>(`/articles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
};

// ── Scraper ────────────────────────────────────────────────────
export const scraperApi = {
  scrapeRange: (body: import("./types").ScrapeRangeRequest) =>
    request<{ task_id: string }>("/scrape", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  scrapeUrl: (url: string, language?: string) =>
    request<{ job_id: number }>("/scrape/url", {
      method: "POST",
      body: JSON.stringify({ url, language: language ?? "ko" }),
    }),
  jobs: (limit = 30) =>
    request<import("./types").ScrapeJob[]>(`/jobs?limit=${limit}`),
  cancelJob: (id: number) =>
    request<void>(`/jobs/${id}`, { method: "DELETE" }),
  status: () => request<Record<string, unknown>>("/scrape/status"),
};

// ── Dashboard ──────────────────────────────────────────────────
export const dashboardApi = {
  stats: () => request<import("./types").DashboardStats>("/status"),
  health: () => request<import("./types").HealthStatus>("/health"),
  costReport: () => request<import("./types").CostReport>("/reports/cost/today"),
};

// ── Glossary ───────────────────────────────────────────────────
export const glossaryApi = {
  list: (params?: { category?: string; q?: string }) => {
    const qs = new URLSearchParams();
    if (params?.category) qs.set("category", params.category);
    if (params?.q) qs.set("q", params.q);
    return request<import("./types").GlossaryEntry[]>(`/glossary?${qs}`);
  },
  create: (body: { term_ko: string; term_en: string; category: string; description?: string }) =>
    request<import("./types").GlossaryEntry>("/glossary", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (id: number, body: Partial<{ term_ko: string; term_en: string; category: string; description: string }>) =>
    request<import("./types").GlossaryEntry>(`/glossary/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  delete: (id: number) =>
    request<void>(`/glossary/${id}`, { method: "DELETE" }),
};

// ── Artists ────────────────────────────────────────────────────
export const artistsApi = {
  list: (q?: string) =>
    request<import("./types").Artist[]>(`/artists${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  setPriority: (id: number, priority: 1 | 2 | 3 | null) =>
    request<import("./types").Artist>(`/artists/${id}/priority`, {
      method: "PATCH",
      body: JSON.stringify({ global_priority: priority }),
    }),
};

// ── [Phase 5-B] Automation ─────────────────────────────────────
export const automationApi = {
  summary: () =>
    request<import("./types").AutomationSummary>("/automation/summary"),

  feed: (params?: { limit?: number; offset?: number; resolution_type?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit    !== undefined) q.set("limit",           String(params.limit));
    if (params?.offset   !== undefined) q.set("offset",          String(params.offset));
    if (params?.resolution_type)        q.set("resolution_type", params.resolution_type);
    return request<import("./types").AutoResolutionLog[]>(`/automation/feed?${q}`);
  },

  conflicts: (params?: { status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.status !== undefined) q.set("status", params.status);
    if (params?.limit  !== undefined) q.set("limit",  String(params.limit));
    if (params?.offset !== undefined) q.set("offset", String(params.offset));
    return request<import("./types").ConflictFlag[]>(`/automation/conflicts?${q}`);
  },

  resolveConflict: (id: number, body: import("./types").ConflictResolveRequest) =>
    request<{ id: number; status: string; resolved_by: string; resolved_at: string | null }>(
      `/automation/conflicts/${id}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
};
