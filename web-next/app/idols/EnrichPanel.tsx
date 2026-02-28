"use client";

import { useState } from "react";
import { idolsApi } from "@/lib/api";
import { Sparkles, Loader2, Users, User } from "lucide-react";

type Target = "all" | "artists" | "groups";

const TARGET_OPTIONS: { id: Target; label: string; desc: string }[] = [
  { id: "all",     label: "전체",         desc: "아티스트 + 그룹 모두 보강" },
  { id: "artists", label: "아티스트만",   desc: "솔로 아티스트 프로필만" },
  { id: "groups",  label: "그룹만",       desc: "그룹/밴드 프로필만" },
];

interface EnrichResult {
  enriched_artists: number;
  enriched_groups: number;
  total: number;
}

export function EnrichPanel() {
  const [target,    setTarget]    = useState<Target>("all");
  const [batchSize, setBatchSize] = useState(10);
  const [loading,   setLoading]   = useState(false);
  const [result,    setResult]    = useState<EnrichResult | null>(null);
  const [error,     setError]     = useState("");

  async function handleEnrich() {
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const res = await idolsApi.enrichProfiles(target, batchSize);
      setResult(res);
    } catch (e) {
      setError(`오류: ${e}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-border bg-card p-5 space-y-5">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-1.5">
            <Sparkles className="h-4 w-4 text-yellow-400" />
            Gemini 프로필 자동 보강
          </h2>
          <p className="mt-1 text-xs text-muted-foreground leading-relaxed">
            비어있는 프로필 필드(생년월일, 국적, MBTI, 소속사, 데뷔일 등)를
            Gemini의 K-pop 지식으로 자동으로 채웁니다.
            이미 값이 있는 필드는 덮어쓰지 않습니다.
          </p>
        </div>

        {/* Target 선택 */}
        <div className="space-y-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">대상</p>
          <div className="flex gap-2 flex-wrap">
            {TARGET_OPTIONS.map((opt) => (
              <button
                key={opt.id}
                onClick={() => setTarget(opt.id)}
                className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                  target === opt.id
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-muted/30 text-muted-foreground hover:text-foreground hover:border-border/70"
                }`}
              >
                <p className="text-xs font-semibold">{opt.label}</p>
                <p className="text-[10px] mt-0.5">{opt.desc}</p>
              </button>
            ))}
          </div>
        </div>

        {/* Batch size */}
        <div className="space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            한 번에 처리할 수 (배치 크기)
          </p>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1}
              max={30}
              value={batchSize}
              onChange={(e) => setBatchSize(Number(e.target.value))}
              className="w-40 accent-primary"
            />
            <span className="text-sm font-mono font-semibold w-6">{batchSize}</span>
            <span className="text-[10px] text-muted-foreground">
              (크게 할수록 1회 Gemini 호출로 더 많이 처리)
            </span>
          </div>
        </div>

        {/* 실행 버튼 */}
        <button
          onClick={handleEnrich}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
        >
          {loading
            ? <Loader2 className="h-4 w-4 animate-spin" />
            : <Sparkles className="h-4 w-4" />}
          {loading ? "Gemini로 보강 중..." : "프로필 보강 실행"}
        </button>

        {/* 결과 */}
        {result && (
          <div className="rounded-lg bg-emerald-500/10 border border-emerald-500/20 p-4 space-y-2">
            <p className="text-sm font-semibold text-emerald-400">보강 완료</p>
            <div className="flex gap-6">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <User className="h-3.5 w-3.5" />
                아티스트 <span className="font-bold text-foreground ml-1">{result.enriched_artists}명</span>
              </span>
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Users className="h-3.5 w-3.5" />
                그룹 <span className="font-bold text-foreground ml-1">{result.enriched_groups}개</span>
              </span>
            </div>
            {result.total === 0 && (
              <p className="text-xs text-muted-foreground">
                보강할 항목이 없습니다 (이미 모두 채워져 있거나 Gemini가 해당 이름을 모름).
              </p>
            )}
          </div>
        )}

        {error && (
          <p className="text-xs text-rose-500 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3">
            {error}
          </p>
        )}
      </div>

      {/* 안내 */}
      <div className="rounded-xl border border-border/60 bg-card/50 p-4 space-y-2">
        <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">보강 가능 필드</p>
        <div className="grid grid-cols-2 gap-x-8 gap-y-1">
          <div>
            <p className="text-xs font-medium mb-1">아티스트</p>
            <ul className="text-[11px] text-muted-foreground space-y-0.5">
              <li>· 생년월일, 국적</li>
              <li>· MBTI, 혈액형</li>
              <li>· 신장, 체중</li>
              <li>· 영문명, 활동명</li>
              <li>· 소개글 (한/영)</li>
            </ul>
          </div>
          <div>
            <p className="text-xs font-medium mb-1">그룹</p>
            <ul className="text-[11px] text-muted-foreground space-y-0.5">
              <li>· 데뷔일</li>
              <li>· 소속사 (한/영)</li>
              <li>· 팬덤명 (한/영)</li>
              <li>· 영문명</li>
              <li>· 소개글 (한/영)</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
