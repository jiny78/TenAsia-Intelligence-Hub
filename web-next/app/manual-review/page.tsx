"use client";
import { useState } from "react";
import { ArticleGrid } from "@/components/articles/article-grid";

export default function ManualReviewPage() {
  const [lang, setLang] = useState<"KO" | "EN">("KO");

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-heading font-bold gradient-text">Manual Review</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            AI가 검토를 요청한 기사를 확인하고 영문 번역을 수정합니다.
          </p>
        </div>
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

      <ArticleGrid lang={lang} fixedStatus="MANUAL_REVIEW" />
    </div>
  );
}
