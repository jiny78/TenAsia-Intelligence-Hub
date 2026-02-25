"use client";
import { useState, useEffect } from "react";
import { format } from "date-fns";
import { Play, FlaskConical, CalendarDays } from "lucide-react";
import type { DateRange } from "react-day-picker";
import { DateRangePicker } from "@/components/ui/date-range-picker";
import { Switch } from "@/components/ui/switch";
import { Progress } from "@/components/ui/progress";
import { scraperApi } from "@/lib/api";
import { useJobs } from "@/hooks/use-jobs";

export function ScrapeForm() {
  const [range, setRange] = useState<DateRange | undefined>();
  const [batchSize, setBatchSize] = useState(10);
  const [dryRun, setDryRun] = useState(false);
  const [loading, setLoading] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);

  const { data: jobs } = useJobs(5);

  // Compute overall progress from recent jobs
  useEffect(() => {
    if (!jobs?.length) { setProgress(0); return; }
    const done  = jobs.filter((j) => j.status === "completed").length;
    const total = jobs.slice(0, 10).length;
    setProgress(total > 0 ? Math.round((done / total) * 100) : 0);
  }, [jobs]);

  const hasRunning = jobs?.some((j) => j.status === "running") ?? false;
  const recentDone = jobs?.filter((j) => j.status === "completed").slice(0, 5) ?? [];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!range?.from) return;
    setLoading(true);
    setError(null);
    setTaskId(null);
    try {
      const res = await scraperApi.scrapeRange({
        start_date: format(range.from, "yyyy-MM-dd"),
        end_date:   format(range.to ?? range.from, "yyyy-MM-dd"),
        batch_size: batchSize,
        dry_run:    dryRun,
      });
      setTaskId(res.task_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-6">
      {/* Date range */}
      <div className="space-y-2">
        <label className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          <CalendarDays className="h-3.5 w-3.5" />
          날짜 범위
        </label>
        <DateRangePicker value={range} onChange={setRange} placeholder="기간 선택" />
      </div>

      {/* Batch size */}
      <div className="space-y-2">
        <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          배치 크기
        </label>
        <div className="flex items-center gap-2">
          {[5, 10, 20, 50].map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setBatchSize(n)}
              className={`flex-1 rounded-lg py-2 text-sm font-medium transition-all ${
                batchSize === n
                  ? "bg-primary text-white shadow-[0_0_10px_-3px_hsl(267_84%_64%/0.7)]"
                  : "bg-muted text-muted-foreground hover:text-foreground"
              }`}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      {/* Dry run toggle */}
      <div className="flex items-center justify-between rounded-xl border border-border/60 bg-card px-4 py-3">
        <div>
          <p className="text-sm font-medium">Dry Run</p>
          <p className="text-[11px] text-muted-foreground">파싱만 하고 DB 저장 안 함</p>
        </div>
        <Switch checked={dryRun} onCheckedChange={setDryRun} />
      </div>

      {dryRun && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2.5 text-xs text-amber-400">
          <FlaskConical className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          드라이 런 — DB에 저장되지 않습니다.
        </div>
      )}

      {/* Submit */}
      <button
        type="submit"
        disabled={!range?.from || loading}
        className="w-full rounded-xl bg-gradient-to-r from-violet-500 to-pink-500 py-3 text-sm font-semibold text-white shadow-lg transition-all duration-200 hover:opacity-90 hover:shadow-[0_8px_20px_-6px_hsl(267_84%_64%/0.7)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
      >
        <Play className="h-4 w-4" />
        {loading ? "시작 중..." : "스크래핑 시작"}
      </button>

      {/* Feedback */}
      {taskId && (
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
          작업 시작됨 — ID: <span className="font-mono font-bold">{taskId}</span>
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-400">
          {error}
        </div>
      )}

      {/* Live progress */}
      {hasRunning && (
        <div className="space-y-2 rounded-xl border border-border/60 bg-card p-4">
          <div className="flex items-center justify-between text-xs">
            <span className="flex items-center gap-1.5 font-medium">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-400" />
              스크래핑 진행 중...
            </span>
            <span className="text-muted-foreground">{recentDone.length}개 완료</span>
          </div>
          <Progress value={progress} variant="gradient" size="md" showValue />
        </div>
      )}
    </form>
  );
}
