"use client";
import { useState } from "react";
import { AlertTriangle, CheckCircle2, XCircle, ChevronDown } from "lucide-react";
import { useAutomationConflicts } from "@/hooks/use-automation";
import { automationApi } from "@/lib/api";
import type { ConflictFlag } from "@/lib/types";

function scoreColor(score: number): string {
  if (score >= 0.8) return "text-red-400";
  if (score >= 0.5) return "text-amber-400";
  return "text-yellow-400";
}

function scoreBg(score: number): string {
  if (score >= 0.8) return "bg-red-500/10";
  if (score >= 0.5) return "bg-amber-500/10";
  return "bg-yellow-500/10";
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string")  return `"${v}"`;
  if (typeof v === "object")  return JSON.stringify(v);
  return String(v);
}

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

interface ConflictRowProps {
  conflict: ConflictFlag;
  onResolved: () => void;
}

function ConflictRow({ conflict, onResolved }: ConflictRowProps) {
  const [expanded,    setExpanded]    = useState(false);
  const [resolvedBy,  setResolvedBy]  = useState("");
  const [submitting,  setSubmitting]  = useState(false);
  const [error,       setError]       = useState<string | null>(null);

  async function handleAction(action: "RESOLVED" | "DISMISSED") {
    if (!resolvedBy.trim()) {
      setError("처리자 이름을 입력하세요.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await automationApi.resolveConflict(conflict.id, { action, resolved_by: resolvedBy.trim() });
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "처리 실패");
    } finally {
      setSubmitting(false);
    }
  }

  const score = conflict.conflict_score;

  return (
    <div className="border-b border-border/40 last:border-0">
      {/* Summary row */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-start gap-3 px-5 py-3.5 text-left hover:bg-muted/30 transition-colors"
      >
        {/* Severity icon */}
        <div className={`mt-0.5 shrink-0 rounded-full p-1.5 ${scoreBg(score)} ${scoreColor(score)}`}>
          <AlertTriangle className="h-3 w-3" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className={`text-[11px] font-bold ${scoreColor(score)}`}>
              심각도 {Math.round(score * 100)}%
            </span>
            <span className="text-xs font-medium text-foreground">{conflict.field_name}</span>
            <span className="text-[11px] text-muted-foreground">
              {conflict.entity_type} #{conflict.entity_id}
            </span>
          </div>
          <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
            <span className="line-through opacity-60">{formatValue(conflict.existing_value)}</span>
            {" vs "}
            <span className="text-foreground">{formatValue(conflict.conflicting_value)}</span>
          </p>
          {conflict.article_title_ko && (
            <p className="mt-0.5 truncate text-[10px] text-muted-foreground/60">
              {conflict.article_title_ko}
            </p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2 text-[10px] text-muted-foreground">
          <span>{formatRelative(conflict.created_at)}</span>
          <ChevronDown
            className={`h-3 w-3 transition-transform ${expanded ? "rotate-180" : ""}`}
          />
        </div>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-border/40 bg-muted/20 px-5 py-4 space-y-3">
          {conflict.conflict_reason && (
            <p className="text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">사유: </span>
              {conflict.conflict_reason}
            </p>
          )}

          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="rounded-lg bg-card border border-border/60 p-3">
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                기존 DB 값
              </p>
              <p className="font-mono text-foreground break-all">
                {formatValue(conflict.existing_value)}
              </p>
            </div>
            <div className="rounded-lg bg-card border border-amber-500/30 p-3">
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                기사 추출 값
              </p>
              <p className="font-mono text-foreground break-all">
                {formatValue(conflict.conflicting_value)}
              </p>
            </div>
          </div>

          {/* Resolve form */}
          <div className="flex items-center gap-2 pt-1">
            <input
              value={resolvedBy}
              onChange={(e) => setResolvedBy(e.target.value)}
              placeholder="처리자 이름/ID"
              className="h-8 flex-1 rounded-lg border border-border/60 bg-background px-3 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <button
              onClick={() => handleAction("RESOLVED")}
              disabled={submitting}
              className="flex h-8 items-center gap-1.5 rounded-lg bg-emerald-600 px-3 text-[11px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <CheckCircle2 className="h-3 w-3" />
              해결
            </button>
            <button
              onClick={() => handleAction("DISMISSED")}
              disabled={submitting}
              className="flex h-8 items-center gap-1.5 rounded-lg bg-muted px-3 text-[11px] font-semibold text-muted-foreground transition-colors hover:bg-muted/80 disabled:opacity-50"
            >
              <XCircle className="h-3 w-3" />
              기각
            </button>
          </div>
          {error && <p className="text-[11px] text-red-400">{error}</p>}
        </div>
      )}
    </div>
  );
}

interface ConflictListProps {
  status?: string;
}

export function ConflictList({ status = "OPEN" }: ConflictListProps) {
  const { data, isLoading, mutate } = useAutomationConflicts(status);

  return (
    <div className="rounded-xl border border-border/60 bg-card">
      <div className="flex items-center justify-between border-b border-border/60 px-5 py-4">
        <h2 className="text-sm font-semibold">
          Conflict Flags
          {data && data.length > 0 && (
            <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-bold text-amber-400">
              {data.length}
            </span>
          )}
        </h2>
        <span className="text-[11px] text-muted-foreground">심각도 높은 순 정렬</span>
      </div>

      {isLoading && (
        <div className="space-y-3 p-5 animate-pulse">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-14 rounded-xl bg-muted" />
          ))}
        </div>
      )}

      {!isLoading && (!data || data.length === 0) && (
        <div className="flex flex-col items-center gap-2 py-10 text-muted-foreground">
          <CheckCircle2 className="h-8 w-8 text-emerald-400 opacity-60" />
          <p className="text-sm">미해결 충돌이 없습니다.</p>
        </div>
      )}

      <div>
        {data?.map((conflict) => (
          <ConflictRow
            key={conflict.id}
            conflict={conflict}
            onResolved={() => mutate()}
          />
        ))}
      </div>
    </div>
  );
}
