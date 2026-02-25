export type ProcessStatus =
  | "PENDING"
  | "SCRAPED"
  | "PROCESSED"
  | "VERIFIED"
  | "MANUAL_REVIEW"
  | "ERROR";

export interface Article {
  id: number;
  source_url: string | null;
  language: string | null;
  process_status: ProcessStatus | null;
  title_ko: string | null;
  title_en: string | null;
  summary_ko: string | null;
  summary_en: string | null;
  author: string | null;
  artist_name_ko: string | null;
  artist_name_en: string | null;
  hashtags_ko: string[];
  hashtags_en: string[];
  thumbnail_url: string | null;
  thumbnail_s3_url: string | null;
  thumbnail_local_url: string | null;
  published_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Artist {
  id: number;
  name_ko: string;
  name_en: string | null;
  stage_name_ko: string | null;
  stage_name_en: string | null;
  global_priority: 1 | 2 | 3 | null;
  is_verified: boolean;
  last_verified_at: string | null;
  data_reliability_score: number | null;
}

export interface GlossaryEntry {
  id: number;
  term_ko: string;
  term_en: string;
  category: "ARTIST" | "AGENCY" | "EVENT" | string;
  description: string | null;
  is_auto_provisioned: boolean;
  source_article_id: number | null;
  created_at: string | null;
}

export interface ScrapeJob {
  id: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  params: {
    source_url?: string;
    language?: string;
    dry_run?: boolean;
    start_date?: string;
    end_date?: string;
  } | null;
  priority: number;
  retry_count: number;
  max_retries: number;
  worker_id: string | null;
  error_msg: string | null;
  result: Record<string, unknown> | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface HealthStatus {
  status: "healthy" | "degraded" | "unhealthy";
  db: string;
  gemini: string;
  disk: string;
}

export interface DashboardStats {
  articles: {
    PENDING?: number;
    SCRAPED?: number;
    PROCESSED?: number;
    MANUAL_REVIEW?: number;
    ERROR?: number;
    total: number;
    today: number;
  };
  artists: {
    total: number;
    verified: number;
  };
}

export interface CostReport {
  api_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  avg_latency_ms: number;
  input_cost_usd: number;
  output_cost_usd: number;
  total_cost_usd: number;
  skipped_articles: number;
  estimated_savings_usd: number;
  savings_pct: number;
}

export interface ScrapeRangeRequest {
  start_date: string;
  end_date: string;
  batch_size?: number;
  dry_run?: boolean;
}

export interface ArticlePatch {
  title_en?: string;
  summary_en?: string;
}

// ── [Phase 5-B] Automation Monitor ─────────────────────────────────────────

export type ResolutionType = "FILL" | "RECONCILE" | "ENROLL";
export type ConflictStatus = "OPEN" | "RESOLVED" | "DISMISSED";

export interface AutoResolutionLog {
  id: number;
  article_id: number | null;
  article_title_ko: string | null;
  entity_type: string;
  entity_id: number;
  field_name: string;
  old_value: unknown;
  new_value: unknown;
  resolution_type: ResolutionType;
  gemini_reasoning: string | null;
  gemini_confidence: number | null;
  source_reliability: number;
  created_at: string | null;
}

export interface ConflictFlag {
  id: number;
  article_id: number | null;
  article_title_ko: string | null;
  entity_type: string;
  entity_id: number;
  field_name: string;
  existing_value: unknown;
  conflicting_value: unknown;
  conflict_reason: string | null;
  conflict_score: number;
  status: ConflictStatus;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string | null;
}

export interface AutomationSummary {
  period: "24h";
  total_decisions: number;
  fill_count: number;
  reconcile_count: number;
  enroll_count: number;
  conflicts_resolved_24h: number;
  open_conflicts: number;
  avg_reliability: number;
}

export interface ConflictResolveRequest {
  action: "RESOLVED" | "DISMISSED";
  resolved_by: string;
}
