"use client";
import { useState } from "react";
import { RefreshCw, LayoutGrid } from "lucide-react";
import { ArticleCard } from "./article-card";
import { useArticles } from "@/hooks/use-articles";
import type { ProcessStatus } from "@/lib/types";

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "전체" },
  { value: "PROCESSED",     label: "완료" },
  { value: "SCRAPED",       label: "수집됨" },
  { value: "PENDING",       label: "대기 중" },
  { value: "MANUAL_REVIEW", label: "검토 필요" },
  { value: "ERROR",         label: "오류" },
];

interface ArticleGridProps {
  lang?: "KO" | "EN";
  fixedStatus?: ProcessStatus;
}

export function ArticleGrid({ lang = "KO", fixedStatus }: ArticleGridProps) {
  const [filter, setFilter] = useState(fixedStatus ?? "");
  const [limit, setLimit] = useState(40);

  const { data, isLoading, mutate } = useArticles({
    process_status: filter || undefined,
    limit,
  });

  const articles = data ?? [];

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      {!fixedStatus && (
        <div className="flex items-center gap-2 flex-wrap">
          <LayoutGrid className="h-4 w-4 text-muted-foreground shrink-0" />
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-all duration-200 ${
                filter === f.value
                  ? "bg-primary text-white shadow-[0_0_10px_-3px_hsl(267_84%_64%/0.8)]"
                  : "bg-muted text-muted-foreground hover:text-foreground"
              }`}
            >
              {f.label}
            </button>
          ))}
          <button
            onClick={() => mutate()}
            className="ml-auto rounded-lg p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? "animate-spin" : ""}`} />
          </button>
        </div>
      )}

      {/* Masonry Grid */}
      {isLoading ? (
        <div className="masonry">
          {Array.from({ length: 12 }).map((_, i) => (
            <div
              key={i}
              className="masonry-item animate-pulse rounded-xl bg-muted"
              style={{ height: `${180 + (i % 3) * 60}px` }}
            />
          ))}
        </div>
      ) : articles.length === 0 ? (
        <div className="flex flex-col items-center py-24 text-muted-foreground">
          <span className="text-5xl mb-4">&#128237;</span>
          <p className="text-sm">기사가 없습니다.</p>
        </div>
      ) : (
        <>
          <div className="masonry">
            {articles.map((article) => (
              <div key={article.id} className="masonry-item">
                <ArticleCard article={article} lang={lang} onUpdate={() => mutate()} />
              </div>
            ))}
          </div>
          {articles.length >= limit && (
            <div className="flex justify-center pt-2">
              <button
                onClick={() => setLimit((l) => l + 40)}
                className="rounded-xl border border-border px-6 py-2 text-sm text-muted-foreground hover:text-foreground hover:border-primary/40 transition-all"
              >
                더 불러오기
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
