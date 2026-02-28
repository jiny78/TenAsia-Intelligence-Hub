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
  scrapeRss: (params?: { language?: string; start_date?: string; end_date?: string }) =>
    request<{ task_id: string }>("/scrape/rss", {
      method: "POST",
      body: JSON.stringify({ language: "kr", ...params }),
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
  stats: async () => {
    const res = await request<{ db: import("./types").DashboardStats }>("/status");
    return res.db;
  },
  health: () => request<import("./types").HealthStatus>("/health"),
  costReport: async () => {
    const res = await request<{
      usage: { api_calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number; avg_latency_ms: number };
      cost:  { actual_input_usd: number; actual_output_usd: number; actual_total_usd: number };
      savings: { skipped_articles: number; saved_cost_usd_est: number; total_if_no_priority_usd: number };
    }>("/reports/cost/today");
    const totalIfNo = res.savings?.total_if_no_priority_usd ?? 0;
    const saved     = res.savings?.saved_cost_usd_est ?? 0;
    return {
      api_calls:            res.usage?.api_calls ?? 0,
      prompt_tokens:        res.usage?.prompt_tokens ?? 0,
      completion_tokens:    res.usage?.completion_tokens ?? 0,
      total_tokens:         res.usage?.total_tokens ?? 0,
      avg_latency_ms:       res.usage?.avg_latency_ms ?? 0,
      input_cost_usd:       res.cost?.actual_input_usd ?? 0,
      output_cost_usd:      res.cost?.actual_output_usd ?? 0,
      total_cost_usd:       res.cost?.actual_total_usd ?? 0,
      skipped_articles:     res.savings?.skipped_articles ?? 0,
      estimated_savings_usd: saved,
      savings_pct:          totalIfNo > 0 ? Math.round((saved / totalIfNo) * 100) : 0,
    } as import("./types").CostReport;
  },
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

// ── Idols (공개 API /public/*) ─────────────────────────────────
export interface PublicGroup {
  id: number;
  name_ko: string;
  name_en: string | null;
  activity_status: string | null;
  debut_date: string | null;
  label_ko: string | null;
  fandom_name_ko: string | null;
  is_verified: boolean;
  photo_url: string | null;
}

export interface PublicArtist {
  id: number;
  name_ko: string;
  name_en: string | null;
  stage_name_ko: string | null;
  is_verified: boolean;
  photo_url: string | null;
}

export interface EntityMappingItem {
  id: number;
  article_id: number;
  article_title_ko: string | null;
  article_url: string | null;
  entity_type: string | null;
  artist_id: number | null;
  artist_name_ko: string | null;
  group_id: number | null;
  group_name_ko: string | null;
  confidence_score: number | null;
}

export const idolsApi = {
  listGroups: (q?: string) =>
    request<PublicGroup[]>(`/public/groups${q ? `?q=${encodeURIComponent(q)}&limit=200` : "?limit=200"}`),

  updateGroup: (id: number, body: { activity_status?: string; bio_ko?: string; bio_en?: string }) =>
    request<PublicGroup>(`/public/groups/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  listArtists: (q?: string) =>
    request<PublicArtist[]>(`/public/artists${q ? `?q=${encodeURIComponent(q)}&limit=200` : "?limit=200"}`),

  listMappings: (params?: { artist_id?: number; group_id?: number; article_id?: number; q?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams({ limit: String(params?.limit ?? 50) });
    if (params?.artist_id  !== undefined) qs.set("artist_id",  String(params.artist_id));
    if (params?.group_id   !== undefined) qs.set("group_id",   String(params.group_id));
    if (params?.article_id !== undefined) qs.set("article_id", String(params.article_id));
    if (params?.q)                        qs.set("q",          params.q);
    if (params?.offset !== undefined)     qs.set("offset",     String(params.offset));
    return request<{ items: EntityMappingItem[]; total: number }>(`/public/entity-mappings?${qs}`);
  },

  deleteMapping: (id: number) =>
    request<{ deleted: number }>(`/public/entity-mappings/${id}`, { method: "DELETE" }),

  createMapping: (body: { article_id: number; artist_id?: number; group_id?: number }) =>
    request<{ created: number }>(`/public/entity-mappings`, {
      method: "POST",
      body: JSON.stringify({ confidence_score: 1.0, ...body }),
    }),

  enrichProfiles: (target: "all" | "artists" | "groups" = "all", batchSize = 10) =>
    request<{ enriched_artists: number; enriched_groups: number; total: number }>(
      `/public/enrich-profiles`,
      {
        method: "POST",
        body: JSON.stringify({ target, batch_size: batchSize }),
      }
    ),

  resetGroupEnrichment: (groupId: number) =>
    request<{ group_id: number; cleared_fields: string[]; enriched_at_reset: boolean }>(
      `/public/groups/${groupId}/reset-enrichment`,
      { method: "POST", body: JSON.stringify({}) }
    ),

  resetArtistEnrichment: (artistId: number) =>
    request<{ artist_id: number; cleared_fields: string[]; enriched_at_reset: boolean }>(
      `/public/artists/${artistId}/reset-enrichment`,
      { method: "POST", body: JSON.stringify({}) }
    ),

  resetAllEnrichment: () =>
    request<{ reset_groups: number; reset_artists: number; message: string }>(
      `/admin/reset-all-enrichment`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  enrichAll: () =>
    request<{ status: string; message: string }>(
      `/admin/enrich-all`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  enrichStatus: () =>
    request<{ running: boolean }>(`/admin/enrich-status`),

  backfillThumbnails: (limit = 30, days = 20) =>
    request<{ message: string; status: string }>(
      `/admin/backfill-thumbnails?limit=${limit}&days=${days}`,
      { method: "POST", body: JSON.stringify({}) },
    ),
};
