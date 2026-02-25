"use client";
import { useState } from "react";
import { ExternalLink, Search } from "lucide-react";
import { useArticles } from "@/hooks/use-articles";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/articles/status-badge";
import { truncate } from "@/lib/utils";

export function ImageGallery() {
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const { data: articles, isLoading } = useArticles({ limit: 60 });

  const withImages = (articles ?? []).filter(
    (a) => a.thumbnail_s3_url || a.thumbnail_url
  );

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
        {Array.from({ length: 18 }).map((_, i) => (
          <Skeleton key={i} className="aspect-square rounded-xl" />
        ))}
      </div>
    );
  }

  if (!withImages.length) {
    return (
      <div className="flex flex-col items-center py-20 text-muted-foreground">
        <span className="text-5xl mb-4">🖼️</span>
        <p>썸네일이 있는 기사가 없습니다.</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
      {withImages.map((article) => {
        const thumbUrl = article.thumbnail_s3_url || article.thumbnail_url;
        const isHovered = hoveredId === article.id;

        return (
          <div
            key={article.id}
            className="group relative aspect-square overflow-hidden rounded-xl border border-border/50 cursor-pointer"
            onMouseEnter={() => setHoveredId(article.id)}
            onMouseLeave={() => setHoveredId(null)}
          >
            {/* Thumbnail */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={thumbUrl!}
              alt={article.title_ko ?? ""}
              loading="lazy"
              className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-110"
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = "none";
              }}
            />

            {/* Hover overlay */}
            <div
              className={`absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-black/90 via-black/40 to-transparent p-2.5 transition-all duration-300 ${
                isHovered ? "opacity-100" : "opacity-0"
              }`}
            >
              <div className="mb-1.5">
                <StatusBadge status={article.process_status} />
              </div>
              <p className="text-xs font-medium text-white leading-tight line-clamp-2">
                {truncate(article.title_ko, 50)}
              </p>
              {article.artist_name_ko && (
                <p className="text-[10px] text-white/70 mt-0.5">
                  {article.artist_name_ko}
                </p>
              )}

              {/* Link button */}
              {article.source_url && (
                <a
                  href={article.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 flex items-center justify-center gap-1 rounded-md bg-white/15 backdrop-blur-sm border border-white/20 py-1 text-[10px] font-semibold text-white hover:bg-white/25 transition-colors"
                  onClick={(e) => e.stopPropagation()}
                >
                  <ExternalLink className="h-3 w-3" />
                  원문 보기
                </a>
              )}
            </div>

            {/* Status dot */}
            <div className="absolute right-2 top-2">
              {article.process_status === "PROCESSED" && (
                <div className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_6px_#34d399]" />
              )}
              {article.process_status === "ERROR" && (
                <div className="h-2 w-2 rounded-full bg-red-400 shadow-[0_0_6px_#f87171]" />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
