"use client";
import { Pencil, RefreshCw, BookPlus, AlertTriangle, Activity } from "lucide-react";
import { useAutomationSummary } from "@/hooks/use-automation";

interface MetricTileProps {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  iconBg: string;
  iconColor: string;
}

function MetricTile({ icon, label, value, iconBg, iconColor }: MetricTileProps) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/60 bg-card px-4 py-3">
      <div className={`rounded-lg p-2 ${iconBg} ${iconColor}`}>{icon}</div>
      <div>
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="text-2xl font-heading font-bold">{value}</p>
      </div>
    </div>
  );
}

export function AutomationStatusCard() {
  const { data, isLoading } = useAutomationSummary();

  if (isLoading) {
    return (
      <div className="rounded-xl border border-border/60 bg-card p-5 animate-pulse">
        <div className="h-4 w-48 rounded bg-muted mb-4" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-16 rounded-xl bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const reliabilityPct = Math.round(data.avg_reliability * 100);

  return (
    <div className="rounded-xl border border-border/60 bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-purple-400" />
          <h2 className="text-sm font-semibold">Automation Status</h2>
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
            last 24h
          </span>
        </div>
        <p className="text-xs text-muted-foreground">
          avg reliability{" "}
          <span className="font-semibold text-foreground">{reliabilityPct}%</span>
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <MetricTile
          icon={<Activity className="h-4 w-4" />}
          label="총 자율 결정"
          value={data.total_decisions}
          iconBg="bg-purple-500/10"
          iconColor="text-purple-400"
        />
        <MetricTile
          icon={<Pencil className="h-4 w-4" />}
          label="빈 필드 보충"
          value={data.fill_count}
          iconBg="bg-blue-500/10"
          iconColor="text-blue-400"
        />
        <MetricTile
          icon={<RefreshCw className="h-4 w-4" />}
          label="모순 자동 해결"
          value={data.reconcile_count}
          iconBg="bg-emerald-500/10"
          iconColor="text-emerald-400"
        />
        <MetricTile
          icon={<BookPlus className="h-4 w-4" />}
          label="신규 용어 등록"
          value={data.enroll_count}
          iconBg="bg-cyan-500/10"
          iconColor="text-cyan-400"
        />
        <MetricTile
          icon={<AlertTriangle className="h-4 w-4" />}
          label="미해결 충돌"
          value={data.open_conflicts}
          iconBg={data.open_conflicts > 0 ? "bg-amber-500/10" : "bg-muted"}
          iconColor={data.open_conflicts > 0 ? "text-amber-400" : "text-muted-foreground"}
        />
        <MetricTile
          icon={<RefreshCw className="h-4 w-4" />}
          label="충돌 처리 완료"
          value={data.conflicts_resolved_24h}
          iconBg="bg-green-500/10"
          iconColor="text-green-400"
        />
      </div>
    </div>
  );
}
