"use client";
import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ArticleCard } from "./article-card";
import { useArticles } from "@/hooks/use-articles";
import type { ProcessStatus } from "@/lib/types";

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "전체" },
  { value: "PENDING", label: "대기 중" },
  { value: "SCRAPED", label: "수집됨" },
  { value: "PROCESSED", label: "완료" },
  { value: "MANUAL_REVIEW", label: "검토 필요" },
  { value: "ERROR", label: "오류" },
];

interface ArticleListProps {
  lang?: "KO" | "EN";
  pendingOnly?: boolean;
}

export function ArticleList({ lang = "KO", pendingOnly = false }: ArticleListProps) {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [limit, setLimit] = useState(30);

  const { data, isLoading, mutate } = useArticles({
    translation_pending: pendingOnly || undefined,
    process_status: statusFilter || undefined,
    limit,
  });

  const articles = data ?? [];

  return (
    <div className="space-y-4">
      {/* Filters */}
      {!pendingOnly && (
        <div className="flex flex-wrap items-center gap-2">
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setStatusFilter(opt.value)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-all duration-200 ${
                statusFilter === opt.value
                  ? "bg-primary text-primary-foreground shadow-glow-sm"
                  : "bg-muted text-muted-foreground hover:text-foreground"
              }`}
            >
              {opt.label}
            </button>
          ))}
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto h-8 w-8"
            onClick={() => mutate()}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? "animate-spin" : ""}`} />
          </Button>
        </div>
      )}

      {/* List */}
      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-[100px] w-full rounded-xl" />
          ))}
        </div>
      ) : articles.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
          <span className="text-4xl mb-3">📭</span>
          <p className="text-sm">기사가 없습니다.</p>
        </div>
      ) : (
        <>
          <div className="space-y-3">
            {articles.map((article) => (
              <ArticleCard
                key={article.id}
                article={article}
                lang={lang}
                onUpdate={() => mutate()}
              />
            ))}
          </div>
          {articles.length >= limit && (
            <div className="flex justify-center pt-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setLimit((l) => l + 30)}
              >
                더 불러오기
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
