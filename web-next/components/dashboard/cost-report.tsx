"use client";
import { DollarSign, Cpu, TrendingDown, Zap } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCostReport } from "@/hooks/use-stats";

export function CostReport() {
  const { data, isLoading } = useCostReport();

  if (isLoading) {
    return <Skeleton className="h-[200px] w-full rounded-xl" />;
  }
  if (!data) return null;

  const metrics = [
    {
      icon: <Cpu className="h-4 w-4" />,
      label: "API 호출",
      value: data.api_calls.toLocaleString(),
      color: "text-blue-400",
    },
    {
      icon: <Zap className="h-4 w-4" />,
      label: "총 토큰",
      value: (data.total_tokens / 1000).toFixed(1) + "K",
      color: "text-primary",
    },
    {
      icon: <DollarSign className="h-4 w-4" />,
      label: "오늘 비용",
      value: "$" + data.total_cost_usd.toFixed(4),
      color: "text-amber-400",
    },
    {
      icon: <TrendingDown className="h-4 w-4" />,
      label: "절감액 (추정)",
      value: "$" + data.estimated_savings_usd.toFixed(4),
      sub: `${data.savings_pct.toFixed(0)}% 절감`,
      color: "text-emerald-400",
    },
  ];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">💰 오늘 비용 리포트</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {metrics.map((m) => (
            <div key={m.label} className="space-y-1">
              <div className={`flex items-center gap-1.5 ${m.color}`}>
                {m.icon}
                <span className="text-xs text-muted-foreground">{m.label}</span>
              </div>
              <p className={`text-2xl font-heading font-bold ${m.color}`}>
                {m.value}
              </p>
              {m.sub && (
                <p className="text-xs text-muted-foreground">{m.sub}</p>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
