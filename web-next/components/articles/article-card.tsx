"use client";
import { useState } from "react";
import { ExternalLink, Pencil, Save, X } from "lucide-react";
import { StatusBadge } from "./status-badge";
import { articlesApi } from "@/lib/api";
import { formatDate, truncate } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { Article } from "@/lib/types";

interface ArticleCardProps {
  article: Article;
  lang?: "KO" | "EN";
  onUpdate?: () => void;
  compact?: boolean;
}

export function ArticleCard({ article, lang = "KO", onUpdate, compact = false }: ArticleCardProps) {
  const [editing, setEditing] = useState(false);
  const [titleEn, setTitleEn] = useState(article.title_en ?? "");
  const [summaryEn, setSummaryEn] = useState(article.summary_en ?? "");
  const [saving, setSaving] = useState(false);

  const isEn = lang === "EN";
  const title   = isEn ? (article.title_en   || article.title_ko)   : article.title_ko;
  const summary = isEn ? (article.summary_en || article.summary_ko) : article.summary_ko;
  const artist  = isEn ? (article.artist_name_en || article.artist_name_ko) : article.artist_name_ko;
  const tags    = (isEn ? article.hashtags_en : article.hashtags_ko) ?? [];
  const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const localThumb = article.thumbnail_local_url ? `${BASE}${article.thumbnail_local_url}` : null;
  const thumbUrl = article.thumbnail_s3_url || localThumb || article.thumbnail_url;
  const missingEn = isEn && !article.title_en;

  async function save() {
    setSaving(true);
    try {
      await articlesApi.patch(article.id, { title_en: titleEn, summary_en: summaryEn });
      setEditing(false);
      onUpdate?.();
    } finally { setSaving(false); }
  }

  return (
    <div className={cn(
      "group relative flex flex-col overflow-hidden rounded-xl border border-border/60 bg-card transition-all duration-300",
      "hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-[0_8px_30px_-8px_hsl(267_84%_64%/0.3)]"
    )}>
      {/* Thumbnail */}
      {thumbUrl && !compact && (
        <div className="relative aspect-video overflow-hidden bg-muted">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={thumbUrl}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-105"
            onError={(e) => { (e.target as HTMLImageElement).parentElement!.style.display = "none"; }}
          />
          {/* Status dot overlay */}
          <div className="absolute right-2 top-2">
            <StatusBadge status={article.process_status} />
          </div>
          {/* Link overlay */}
          {article.source_url && (
            <a
              href={article.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity duration-200 group-hover:opacity-100"
            >
              <div className="rounded-full bg-white/15 p-2.5 backdrop-blur-sm border border-white/20">
                <ExternalLink className="h-4 w-4 text-white" />
              </div>
            </a>
          )}
        </div>
      )}

      {/* Content */}
      <div className="flex flex-1 flex-col p-4 gap-3">
        {/* Artist tag */}
        {artist && (
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-0.5 text-[10px] font-semibold text-primary">
              {artist}
            </span>
            {compact && <StatusBadge status={article.process_status} />}
            {missingEn && (
              <span className="text-[10px] font-medium text-amber-400">&#9888; 번역 없음</span>
            )}
          </div>
        )}

        {/* Title */}
        <div>
          <p className={cn("font-heading font-semibold leading-snug", compact ? "text-sm" : "text-base")}>
            {truncate(title, 90) || "제목 없음"}
          </p>
          {summary && !compact && (
            <p className="mt-1.5 text-xs text-muted-foreground line-clamp-3 leading-relaxed">
              {summary}
            </p>
          )}
        </div>

        {/* Tags */}
        {tags.length > 0 && !compact && (
          <div className="flex flex-wrap gap-1">
            {tags.slice(0, 5).map((t) => (
              <span key={t} className="rounded-full bg-muted px-2 py-0.5 text-[9px] text-muted-foreground">
                #{t}
              </span>
            ))}
          </div>
        )}

        {/* Footer */}
        <div className="mt-auto flex items-center justify-between text-[10px] text-muted-foreground">
          <span>{formatDate(article.published_at ?? article.created_at)}</span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setEditing((v) => !v)}
              className="rounded p-1 hover:bg-muted hover:text-foreground transition-colors"
              title="번역 수정"
            >
              <Pencil className="h-3 w-3" />
            </button>
            {article.source_url && (
              <a
                href={article.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded p-1 hover:bg-muted hover:text-foreground transition-colors"
              >
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        </div>

        {/* Inline edit */}
        {editing && (
          <div className="space-y-2 rounded-lg bg-muted/60 p-3 text-xs">
            <p className="font-semibold text-primary">영문 수정</p>
            <input
              value={titleEn}
              onChange={(e) => setTitleEn(e.target.value)}
              placeholder="English title..."
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <textarea
              value={summaryEn}
              onChange={(e) => setSummaryEn(e.target.value)}
              placeholder="English summary..."
              rows={2}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring resize-none"
            />
            <div className="flex gap-1.5">
              <button
                onClick={save}
                disabled={saving}
                className="flex flex-1 items-center justify-center gap-1 rounded-md bg-primary px-3 py-1.5 text-[10px] font-semibold text-white hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                <Save className="h-3 w-3" />
                {saving ? "저장 중..." : "저장"}
              </button>
              <button
                onClick={() => setEditing(false)}
                className="rounded-md bg-muted px-3 py-1.5 text-[10px] hover:bg-muted/80 transition-colors"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
