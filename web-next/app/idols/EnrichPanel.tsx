"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { idolsApi } from "@/lib/api";
import { Sparkles, Loader2, Users, User, RotateCcw, AlertTriangle, RefreshCw } from "lucide-react";

type Target = "all" | "artists" | "groups";

const TARGET_OPTIONS: { id: Target; label: string; desc: string }[] = [
  { id: "all",     label: "전체",       desc: "아티스트 + 그룹 모두 보강" },
  { id: "artists", label: "아티스트만", desc: "솔로 아티스트 프로필만" },
  { id: "groups",  label: "그룹만",     desc: "그룹/밴드 프로필만" },
];

interface EnrichResult {
  enriched_artists: number;
  enriched_groups: number;
  total: number;
}

export function EnrichPanel() {
  // ── 배치 보강 ──────────────────────────────────────────────
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

  // ── 누락 프로필 재보강 ─────────────────────────────────────
  const [sparseLoading, setSparseLoading] = useState(false);
  const [sparseMsg,     setSparseMsg]     = useState("");

  async function handleReEnrichSparse() {
    setSparseLoading(true);
    setSparseMsg("");
    try {
      const res = await idolsApi.reEnrichSparse(200);
      setSparseMsg(res.message ?? "재보강 시작됨");
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(pollStatus, 4000);
      setBgRunning(true);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("409")) {
        setSparseMsg("이미 보강 작업이 실행 중입니다.");
        pollRef.current = setInterval(pollStatus, 4000);
      } else {
        setSparseMsg(`오류: ${e}`);
      }
    } finally {
      setSparseLoading(false);
    }
  }

  // ── 전체 재보강 ────────────────────────────────────────────
  const [resetLoading,  setResetLoading]  = useState(false);
  const [resetResult,   setResetResult]   = useState<{ reset_groups: number; reset_artists: number } | null>(null);
  const [enrichAllMsg,  setEnrichAllMsg]  = useState("");
  const [enrichAllErr,  setEnrichAllErr]  = useState("");
  const [bgRunning,     setBgRunning]     = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 폴링: 백그라운드 보강 상태 확인
  const pollStatus = useCallback(async () => {
    try {
      const s = await idolsApi.enrichStatus();
      setBgRunning(s.running);
      if (!s.running && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        setEnrichAllMsg("전체 보강 완료!");
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    // 마운트 시 현재 실행 상태 확인
    idolsApi.enrichStatus().then((s) => {
      setBgRunning(s.running);
      if (s.running) {
        setEnrichAllMsg("백그라운드에서 전체 보강 진행 중...");
        pollRef.current = setInterval(pollStatus, 4000);
      }
    }).catch(() => {});
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [pollStatus]);

  async function handleResetAll() {
    if (!confirm(
      "⚠️ 전체 보강 데이터 초기화\n\n모든 아티스트/그룹의 이름(영문), 데뷔일, 소속사, 팬덤명, 성별, 활동상태, 소개글이 삭제됩니다.\n\n계속하시겠습니까?"
    )) return;
    setResetLoading(true);
    setResetResult(null);
    setEnrichAllErr("");
    try {
      const res = await idolsApi.resetAllEnrichment();
      setResetResult(res);
    } catch (e) {
      setEnrichAllErr(`초기화 오류: ${e}`);
    } finally {
      setResetLoading(false);
    }
  }

  async function handleEnrichAll() {
    setEnrichAllErr("");
    setEnrichAllMsg("");
    setBgRunning(true);
    try {
      const res = await idolsApi.enrichAll();
      setEnrichAllMsg(res.message ?? "전체 보강 시작됨");
      // 폴링 시작
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(pollStatus, 4000);
    } catch (e: unknown) {
      setBgRunning(false);
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("409")) {
        setEnrichAllMsg("이미 전체 보강 작업이 실행 중입니다.");
        pollRef.current = setInterval(pollStatus, 4000);
      } else {
        setEnrichAllErr(`보강 시작 오류: ${e}`);
      }
    }
  }

  return (
    <div className="space-y-5">

      {/* ── 누락 프로필 재보강 ── */}
      <div className="rounded-xl border border-primary/30 bg-primary/5 p-5 space-y-3">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-1.5">
            <RefreshCw className="h-4 w-4 text-primary" />
            누락 프로필 재보강
          </h2>
          <p className="mt-1 text-xs text-muted-foreground leading-relaxed">
            소개글(bio)이 비어 있는 아티스트/그룹을 Wikipedia에서 다시 찾아 채웁니다.
            이미 값이 있는 필드(데뷔일, 소속사 등)는 유지됩니다.
            Wikipedia 텍스트 한도: 3,000자 / stage_name_ko 우선 검색.
          </p>
        </div>
        <button
          onClick={handleReEnrichSparse}
          disabled={sparseLoading || bgRunning}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
        >
          {sparseLoading || bgRunning
            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
            : <RefreshCw className="h-3.5 w-3.5" />}
          누락 프로필 재보강 실행
        </button>
        {sparseMsg && (
          <p className={`text-xs rounded-lg border p-3 ${
            sparseMsg.includes("오류")
              ? "bg-rose-500/10 border-rose-500/20 text-rose-400"
              : "bg-primary/10 border-primary/20 text-primary"
          }`}>
            {bgRunning && <Loader2 className="inline h-3 w-3 animate-spin mr-1.5" />}
            {sparseMsg}
          </p>
        )}
      </div>

      {/* ── 전체 재보강 (맨 위) ── */}
      <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-5 space-y-4">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-1.5">
            <RotateCcw className="h-4 w-4 text-amber-400" />
            전체 재보강
          </h2>
          <p className="mt-1 text-xs text-muted-foreground leading-relaxed">
            기존 보강 데이터를 전부 초기화하고 Wikipedia + Gemini로 다시 채웁니다.
            초기화 후 <strong>전체 보강 실행</strong>을 눌러 백그라운드에서 처리합니다.
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          {/* 1단계: 전체 초기화 */}
          <button
            onClick={handleResetAll}
            disabled={resetLoading || bgRunning}
            className="inline-flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs font-semibold text-amber-400 hover:bg-amber-500/20 disabled:opacity-50 transition-colors"
          >
            {resetLoading
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <AlertTriangle className="h-3.5 w-3.5" />}
            ① 전체 초기화
          </button>

          {/* 2단계: 백그라운드 보강 실행 */}
          <button
            onClick={handleEnrichAll}
            disabled={bgRunning}
            className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
          >
            {bgRunning
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <Sparkles className="h-3.5 w-3.5" />}
            ② 전체 보강 실행
          </button>
        </div>

        {/* 초기화 결과 */}
        {resetResult && (
          <div className="rounded-lg bg-muted/50 border border-border p-3 text-xs text-muted-foreground">
            초기화 완료 — 그룹 <span className="font-bold text-foreground">{resetResult.reset_groups}개</span>
            {" "}· 아티스트 <span className="font-bold text-foreground">{resetResult.reset_artists}명</span>
            <span className="ml-2 text-emerald-400">→ 이제 ② 전체 보강 실행을 누르세요</span>
          </div>
        )}

        {/* 보강 진행 상태 */}
        {enrichAllMsg && (
          <div className={`rounded-lg border p-3 text-xs font-medium ${
            enrichAllMsg.includes("완료")
              ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
              : "bg-primary/10 border-primary/20 text-primary"
          }`}>
            {bgRunning && <Loader2 className="inline h-3 w-3 animate-spin mr-1.5" />}
            {enrichAllMsg}
          </div>
        )}

        {enrichAllErr && (
          <p className="text-xs text-rose-500 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3">
            {enrichAllErr}
          </p>
        )}
      </div>

      {/* ── 배치 보강 (기존) ── */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-5">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-1.5">
            <Sparkles className="h-4 w-4 text-yellow-400" />
            Gemini 프로필 자동 보강 (배치)
          </h2>
          <p className="mt-1 text-xs text-muted-foreground leading-relaxed">
            미보강 항목 중 일부를 지금 즉시 보강합니다.
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
            배치 크기
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
          </div>
        </div>

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
                보강할 항목이 없습니다 (이미 모두 채워져 있거나 새 항목 없음).
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
              <li>· 활동 상태 (ACTIVE/HIATUS/DISBANDED)</li>
              <li>· 소개글 (한/영)</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
