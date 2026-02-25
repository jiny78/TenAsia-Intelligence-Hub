"use client";
import { useState } from "react";
import { StatsCards } from "@/components/dashboard/stats-cards";
import { CostReport } from "@/components/dashboard/cost-report";
import { ArticleGrid } from "@/components/articles/article-grid";

export default function DashboardPage() {
  const [lang, setLang] = useState<"KO" | "EN">("KO");

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-heading font-bold gradient-text">Dashboard</h1>
          <p className="mt-1 text-sm text-muted-foreground">K-Entertainment 기사 수집 현황</p>
        </div>
        {/* Lang toggle */}
        <div className="flex items-center gap-1 rounded-lg bg-muted p-1">
          {(["KO", "EN"] as const).map((l) => (
            <button
              key={l}
              onClick={() => setLang(l)}
              className={`rounded-md px-3 py-1.5 text-xs font-semibold transition-all ${
                lang === l ? "bg-background text-primary shadow-sm" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {l}
            </button>
          ))}
        </div>
      </div>

      <StatsCards />
      <CostReport />

      {/* Article Feed */}
      <div>
        <h2 className="mb-4 text-lg font-heading font-semibold">Article Feed</h2>
        <ArticleGrid lang={lang} />
      </div>
    </div>
  );
}
