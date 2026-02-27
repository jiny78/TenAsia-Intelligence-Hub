"use client";
import { Newspaper, CheckCircle2, Clock, Users2 } from "lucide-react";
import { useStats, useHealth } from "@/hooks/use-stats";

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  sub?: string;
  iconBg: string;
  iconColor: string;
}

function StatCard({ icon, label, value, sub, iconBg, iconColor }: StatCardProps) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-border/60 bg-card p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium text-muted-foreground">{label}</p>
          <p className="mt-1.5 text-3xl font-heading font-bold">{value}</p>
          {sub && <p className="mt-1 text-[11px] text-muted-foreground">{sub}</p>}
        </div>
        <div className={`rounded-xl p-2.5 ${iconBg} ${iconColor}`}>{icon}</div>
      </div>
      {/* Subtle gradient bg */}
      <div className="pointer-events-none absolute -bottom-4 -right-4 h-20 w-20 rounded-full opacity-[0.06] blur-2xl"
           style={{ background: "hsl(267 84% 64%)" }} />
    </div>
  );
}

export function StatsCards() {
  const { data: stats } = useStats();
  const { data: health } = useHealth();

  const healthColor =
    health?.status === "healthy" ? "text-emerald-400"
    : health?.status === "degraded" ? "text-amber-400"
    : "text-red-400";

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <StatCard
          icon={<Newspaper className="h-5 w-5" />}
          label="전체 기사"
          value={stats?.articles?.total ?? "—"}
          sub={`오늘 +${stats?.articles?.today ?? 0}건`}
          iconBg="bg-purple-500/10"
          iconColor="text-purple-400"
        />
        <StatCard
          icon={<CheckCircle2 className="h-5 w-5" />}
          label="처리 완료"
          value={stats?.articles?.PROCESSED ?? "—"}
          sub={`검토 필요: ${stats?.articles?.MANUAL_REVIEW ?? 0}`}
          iconBg="bg-emerald-500/10"
          iconColor="text-emerald-400"
        />
        <StatCard
          icon={<Clock className="h-5 w-5" />}
          label="처리 대기"
          value={(stats?.articles?.PENDING ?? 0) + (stats?.articles?.SCRAPED ?? 0)}
          sub={`오류: ${stats?.articles?.ERROR ?? 0}`}
          iconBg="bg-amber-500/10"
          iconColor="text-amber-400"
        />
        <StatCard
          icon={<Users2 className="h-5 w-5" />}
          label="아티스트"
          value={stats?.artists?.total ?? "—"}
          sub={`검증됨: ${stats?.artists?.verified ?? 0}`}
          iconBg="bg-blue-500/10"
          iconColor="text-blue-400"
        />
      </div>

      {/* Health bar */}
      {health && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-xl border border-border/60 bg-card px-5 py-3 text-xs">
          <span className="font-semibold uppercase tracking-wide text-muted-foreground">System</span>
          <span className={`font-bold ${healthColor}`}>{health.status.toUpperCase()}</span>
          <span className="text-muted-foreground">DB: <span className="text-foreground">{health.db}</span></span>
          <span className="text-muted-foreground">Gemini: <span className="text-foreground">{health.gemini}</span></span>
          <span className="text-muted-foreground">Disk: <span className="text-foreground">{health.disk}</span></span>
        </div>
      )}
    </div>
  );
}
