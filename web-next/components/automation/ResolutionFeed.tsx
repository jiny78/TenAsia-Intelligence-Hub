"use client";
import { useState } from "react";
import { Pencil, RefreshCw, BookPlus } from "lucide-react";
import { useAutomationFeed } from "@/hooks/use-automation";
import type { ResolutionType } from "@/lib/types";

const TYPE_CONFIG: Record<
  ResolutionType,
  { label: string; icon: React.ReactNode; bg: string; text: string }
> = {
  FILL: {
    label: "빈 필드 보충",
    icon: <Pencil className="h-3 w-3" />,
    bg: "bg-blue-500/10",
    text: "text-blue-400",
  },
  RECONCILE: {
    label: "모순 해결",
    icon: <RefreshCw className="h-3 w-3" />,
    bg: "bg-emerald-500/10",
    text: "text-emerald-400",
  },
  ENROLL: {
    label: "신규 등록",
    icon: <BookPlus className="h-3 w-3" />,
    bg: "bg-cyan-500/10",
    text: "text-cyan-400",
  },
};

const FILTER_OPTIONS = [
  { label: "전체", value: undefined },
  { label: "FILL",      value: "FILL"      },
  { label: "RECONCILE", value: "RECONCILE" },
  { label: "ENROLL",    value: "ENROLL"    },
] as const;

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1)  return "방금 전";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string")  return v;
  if (typeof v === "object")  return JSON.stringify(v);
  return String(v);
}

export function ResolutionFeed() {
  const [filterType, setFilterType] = useState<string | undefined>(undefined);
  const { data, isLoading } = useAutomationFeed({ limit: 50, resolution_type: filterType });

  return (
    <div className="rounded-xl border border-border/60 bg-card">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/60 px-5 py-4">
        <h2 className="text-sm font-semibold">Auto-Resolution Feed</h2>
        {/* Type filter */}
        <div className="flex items-center gap-1 rounded-lg bg-muted p-1">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={String(opt.value)}
              onClick={() => setFilterType(opt.value)}
              className={`rounded-md px-2.5 py-1 text-[11px] font-semibold transition-all ${
                filterType === opt.value
                  ? "bg-background text-primary shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Feed */}
      <div className="divide-y divide-border/40">
        {isLoading && (
          <div className="space-y-3 p-5">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex gap-3 animate-pulse">
                <div className="mt-1 h-6 w-6 shrink-0 rounded-full bg-muted" />
                <div className="flex-1 space-y-1.5">
                  <div className="h-3 w-3/4 rounded bg-muted" />
                  <div className="h-3 w-1/2 rounded bg-muted" />
                </div>
              </div>
            ))}
          </div>
        )}

        {!isLoading && (!data || data.length === 0) && (
          <p className="p-8 text-center text-sm text-muted-foreground">
            자율 결정 내역이 없습니다.
          </p>
        )}

        {data?.map((log) => {
          const cfg = TYPE_CONFIG[log.resolution_type];
          return (
            <div key={log.id} className="flex items-start gap-3 px-5 py-3.5 hover:bg-muted/30 transition-colors">
              {/* Type badge */}
              <div className={`mt-0.5 flex shrink-0 items-center justify-center rounded-full p-1.5 ${cfg.bg} ${cfg.text}`}>
                {cfg.icon}
              </div>

              {/* Content */}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className={`text-[11px] font-semibold ${cfg.text}`}>{cfg.label}</span>
                  <span className="text-xs font-medium text-foreground">{log.field_name}</span>
                  <span className="text-[11px] text-muted-foreground">
                    {log.entity_type} #{log.entity_id}
                  </span>
                </div>

                {/* Old → New values */}
                {(log.resolution_type === "FILL" || log.resolution_type === "RECONCILE") && (
                  <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                    {log.old_value !== null && (
                      <span className="line-through opacity-60">{formatValue(log.old_value)}</span>
                    )}
                    {log.old_value !== null && " → "}
                    <span className="text-foreground">{formatValue(log.new_value)}</span>
                  </p>
                )}

                {/* Enroll: show term */}
                {log.resolution_type === "ENROLL" && log.new_value && (
                  <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                    <span className="text-foreground">{formatValue(log.new_value)}</span>
                  </p>
                )}

                {/* Reasoning */}
                {log.gemini_reasoning && (
                  <p className="mt-0.5 truncate text-[11px] text-muted-foreground italic">
                    "{log.gemini_reasoning}"
                  </p>
                )}

                {/* Footer: source + time */}
                <div className="mt-1 flex flex-wrap items-center gap-x-3 text-[10px] text-muted-foreground/70">
                  {log.article_title_ko && (
                    <span className="truncate max-w-[200px]" title={log.article_title_ko}>
                      {log.article_title_ko}
                    </span>
                  )}
                  {log.gemini_confidence !== null && (
                    <span>신뢰도 {Math.round((log.gemini_confidence ?? 0) * 100)}%</span>
                  )}
                  <span className="ml-auto shrink-0">{formatRelative(log.created_at)}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
