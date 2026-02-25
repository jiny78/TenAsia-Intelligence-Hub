"use client";
import { Trash2 } from "lucide-react";
import { scraperApi } from "@/lib/api";
import { useJobs } from "@/hooks/use-jobs";
import { Progress } from "@/components/ui/progress";
import { formatDatetime } from "@/lib/utils";

const STATUS_BADGE: Record<string, string> = {
  pending:   "bg-zinc-500/10 text-zinc-400 border-zinc-500/20",
  running:   "bg-blue-500/10 text-blue-400 border-blue-500/20",
  completed: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  failed:    "bg-red-500/10 text-red-400 border-red-500/20",
  cancelled: "bg-zinc-500/10 text-zinc-500 border-zinc-500/10",
};

const STATUS_ICON: Record<string, string> = {
  pending: "🕐", running: "🔄", completed: "✅", failed: "❌", cancelled: "🚫",
};

export function JobList() {
  const { data: jobs, isLoading, mutate } = useJobs(20);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="h-16 animate-pulse rounded-xl bg-muted" />
        ))}
      </div>
    );
  }

  if (!jobs?.length) {
    return (
      <div className="flex flex-col items-center py-16 text-center text-muted-foreground">
        <span className="text-3xl mb-3">&#128237;</span>
        <p className="text-sm">작업 없음</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {jobs.map((job) => {
        const isRunning = job.status === "running";
        return (
          <div
            key={job.id}
            className={`rounded-xl border bg-card p-3 transition-colors ${
              isRunning ? "border-blue-500/30" : "border-border/60 hover:border-border"
            }`}
          >
            <div className="flex items-start gap-3">
              <span className="mt-0.5 text-base">{STATUS_ICON[job.status] ?? "❓"}</span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  <span
                    className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                      STATUS_BADGE[job.status] ?? ""
                    }`}
                  >
                    {job.status}
                  </span>
                  <span className="text-[10px] text-muted-foreground">#{job.id}</span>
                  {job.params?.dry_run && (
                    <span className="text-[10px] font-medium text-amber-400">dry-run</span>
                  )}
                </div>

                {isRunning && (
                  <Progress value={50} variant="gradient" size="sm" className="mb-2" />
                )}

                <p className="truncate text-[10px] text-muted-foreground">
                  {job.params?.source_url ?? job.params?.start_date ?? "—"}
                </p>
                <p className="mt-0.5 text-[9px] text-muted-foreground/60">
                  {formatDatetime(job.created_at)}
                  {job.completed_at && ` → ${formatDatetime(job.completed_at)}`}
                </p>

                {job.error_msg && (
                  <p className="mt-1 text-[10px] text-red-400 line-clamp-2">{job.error_msg}</p>
                )}
              </div>

              {job.status === "pending" && (
                <button
                  onClick={async () => { await scraperApi.cancelJob(job.id); mutate(); }}
                  className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive transition-colors shrink-0"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
