"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { idolsApi, type EntityMappingItem } from "@/lib/api";
import { Loader2, Plus, RefreshCw, ChevronLeft, ChevronRight, Search, X, ExternalLink } from "lucide-react";

const PAGE_SIZE = 50;

interface ArticleGroup {
  article_id: number;
  article_title_ko: string | null;
  article_url: string | null;
  mappings: EntityMappingItem[];
}

export function MappingsTab() {
  const [mappings, setMappings] = useState<EntityMappingItem[]>([]);
  const [total,    setTotal]    = useState(0);
  const [loading,  setLoading]  = useState(true);
  const [deleting, setDeleting] = useState<Record<number, boolean>>({});

  // 검색 상태
  const [searchQ,         setSearchQ]         = useState("");
  const [filterArticleId, setFilterArticleId] = useState("");
  const [page,            setPage]            = useState(0);

  // 새 매핑 추가 폼
  const [newArticleId, setNewArticleId] = useState("");
  const [newArtistId,  setNewArtistId]  = useState("");
  const [newGroupId,   setNewGroupId]   = useState("");
  const [adding,       setAdding]       = useState(false);
  const [addError,     setAddError]     = useState("");

  // 이름 찾기 helper
  const [helperQuery,   setHelperQuery]   = useState("");
  const [helperResults, setHelperResults] = useState<{ id: number; name: string; type: string }[]>([]);
  const [helperLoading, setHelperLoading] = useState(false);

  const totalPages = Math.ceil(total / PAGE_SIZE);

  // 기사별로 그룹핑
  const articleGroups = useMemo<ArticleGroup[]>(() => {
    const map = new Map<number, ArticleGroup>();
    for (const m of mappings) {
      if (!map.has(m.article_id)) {
        map.set(m.article_id, {
          article_id:      m.article_id,
          article_title_ko: m.article_title_ko,
          article_url:     m.article_url,
          mappings:        [],
        });
      }
      map.get(m.article_id)!.mappings.push(m);
    }
    return Array.from(map.values());
  }, [mappings]);

  const load = useCallback(async (p = page) => {
    setLoading(true);
    try {
      const result = await idolsApi.listMappings({
        q:          searchQ   || undefined,
        article_id: filterArticleId ? Number(filterArticleId) : undefined,
        limit:      PAGE_SIZE,
        offset:     p * PAGE_SIZE,
      });
      setMappings(result.items);
      setTotal(result.total);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, [searchQ, filterArticleId, page]);

  useEffect(() => { load(0); setPage(0); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setPage(0);
    load(0);
  }

  function goToPage(p: number) {
    setPage(p);
    load(p);
  }

  async function handleDelete(id: number) {
    setDeleting((prev) => ({ ...prev, [id]: true }));
    try {
      await idolsApi.deleteMapping(id);
      // 낙관적 업데이트 + 서버에서 재조회 (일관성 보장)
      setMappings((prev) => prev.filter((m) => m.id !== id));
      setTotal((t) => Math.max(0, t - 1));
      // 서버 재조회로 확인
      const result = await idolsApi.listMappings({
        q:          searchQ || undefined,
        article_id: filterArticleId ? Number(filterArticleId) : undefined,
        limit:      PAGE_SIZE,
        offset:     page * PAGE_SIZE,
      });
      setMappings(result.items);
      setTotal(result.total);
    } catch (e) {
      alert(`삭제 실패: ${e}`);
    } finally {
      setDeleting((prev) => ({ ...prev, [id]: false }));
    }
  }

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!newArticleId || (!newArtistId && !newGroupId)) {
      setAddError("기사 ID와 아티스트 ID 또는 그룹 ID를 입력하세요.");
      return;
    }
    setAdding(true);
    setAddError("");
    try {
      await idolsApi.createMapping({
        article_id: Number(newArticleId),
        artist_id:  newArtistId ? Number(newArtistId) : undefined,
        group_id:   newGroupId  ? Number(newGroupId)  : undefined,
      });
      setNewArticleId("");
      setNewArtistId("");
      setNewGroupId("");
      setPage(0);
      load(0);
    } catch (e) {
      setAddError(`추가 실패: ${e}`);
    } finally {
      setAdding(false);
    }
  }

  async function handleHelperSearch() {
    if (!helperQuery.trim()) return;
    setHelperLoading(true);
    setHelperResults([]);
    try {
      const [artists, groups] = await Promise.all([
        idolsApi.listArtists(helperQuery),
        idolsApi.listGroups(helperQuery),
      ]);
      const results = [
        ...artists.slice(0, 8).map((a) => ({ id: a.id, name: a.name_ko + (a.stage_name_ko ? ` (${a.stage_name_ko})` : ""), type: "아티스트" as const })),
        ...groups.slice(0, 8).map((g) => ({ id: g.id, name: g.name_ko, type: "그룹" as const })),
      ];
      setHelperResults(results);
    } catch (e) {
      console.error(e);
    } finally {
      setHelperLoading(false);
    }
  }

  return (
    <div className="space-y-5">

      {/* ── 새 매핑 추가 ── */}
      <div className="rounded-xl border border-border bg-card p-4 space-y-4">
        <h2 className="font-semibold text-sm">새 매핑 추가</h2>

        {/* 이름으로 ID 찾기 helper */}
        <div className="rounded-lg bg-muted/40 p-3 space-y-2">
          <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">ID 찾기 (이름 검색)</p>
          <div className="flex gap-2">
            <input
              type="text"
              value={helperQuery}
              onChange={(e) => setHelperQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleHelperSearch()}
              placeholder="아티스트 또는 그룹 이름"
              className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <button
              type="button"
              onClick={handleHelperSearch}
              disabled={helperLoading}
              className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50"
            >
              {helperLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
              검색
            </button>
          </div>
          {helperResults.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-1">
              {helperResults.map((r) => (
                <button
                  key={`${r.type}-${r.id}`}
                  type="button"
                  onClick={() => {
                    if (r.type === "아티스트") setNewArtistId(String(r.id));
                    else setNewGroupId(String(r.id));
                  }}
                  className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-xs hover:bg-muted transition-colors"
                >
                  <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${r.type === "아티스트" ? "bg-purple-500/15 text-purple-400" : "bg-pink-500/15 text-pink-400"}`}>
                    {r.type}
                  </span>
                  <span className="font-medium">{r.name}</span>
                  <span className="text-muted-foreground font-mono">#{r.id}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <form onSubmit={handleAdd} className="flex flex-wrap gap-3 items-end">
          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground uppercase tracking-wider">기사 ID *</label>
            <input
              type="number"
              value={newArticleId}
              onChange={(e) => setNewArticleId(e.target.value)}
              placeholder="article_id"
              required
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground uppercase tracking-wider">아티스트 ID</label>
            <input
              type="number"
              value={newArtistId}
              onChange={(e) => setNewArtistId(e.target.value)}
              placeholder="artist_id"
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground uppercase tracking-wider">그룹 ID</label>
            <input
              type="number"
              value={newGroupId}
              onChange={(e) => setNewGroupId(e.target.value)}
              placeholder="group_id"
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <button
            type="submit"
            disabled={adding}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-sm font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
          >
            {adding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
            추가
          </button>
        </form>
        {addError && <p className="text-xs text-rose-500">{addError}</p>}
      </div>

      {/* ── 검색 ── */}
      <form onSubmit={handleSearch} className="flex flex-wrap items-end gap-3">
        <div className="space-y-1 flex-1 min-w-48">
          <label className="text-[10px] text-muted-foreground uppercase tracking-wider">아티스트/그룹/기사 이름 검색</label>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <input
              type="text"
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
              placeholder="이름으로 검색..."
              className="w-full rounded-md border border-border bg-background pl-8 pr-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label className="text-[10px] text-muted-foreground uppercase tracking-wider">기사 ID</label>
          <input
            type="number"
            value={filterArticleId}
            onChange={(e) => setFilterArticleId(e.target.value)}
            placeholder="article_id"
            className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <button
          type="submit"
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50 transition-colors"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          검색
        </button>
      </form>

      {/* ── 기사별 매핑 목록 ── */}
      <div className="space-y-2">
        {loading && (
          <div className="flex items-center justify-center py-10">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}
        {!loading && articleGroups.length === 0 && (
          <div className="rounded-xl border border-border px-4 py-8 text-center text-xs text-muted-foreground">
            매핑 없음
          </div>
        )}
        {!loading && articleGroups.map((group) => (
          <div key={group.article_id} className="rounded-xl border border-border bg-card px-4 py-3 flex items-start gap-3">
            {/* 기사 정보 */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-1.5 mb-2">
                <p className="text-xs font-medium line-clamp-1">
                  {group.article_title_ko ?? `기사 #${group.article_id}`}
                </p>
                {group.article_url && (
                  <a
                    href={group.article_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="shrink-0 text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
              <p className="text-[10px] text-muted-foreground font-mono mb-2">id={group.article_id}</p>

              {/* 연결된 엔티티 태그 — 각 태그 ✕로 개별 연결 해제 */}
              <div className="flex flex-wrap gap-1.5">
                {group.mappings.map((m) => {
                  const isArtist = m.entity_type === "ARTIST";
                  const label    = isArtist
                    ? (m.artist_name_ko ?? `아티스트 #${m.artist_id}`)
                    : (m.group_name_ko  ?? `그룹 #${m.group_id}`);
                  return (
                    <span
                      key={m.id}
                      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors ${
                        isArtist
                          ? "border-purple-500/30 bg-purple-500/10 text-purple-300"
                          : "border-pink-500/30 bg-pink-500/10 text-pink-300"
                      }`}
                    >
                      <span className={`text-[9px] font-semibold ${isArtist ? "text-purple-400" : "text-pink-400"}`}>
                        {isArtist ? "아티스트" : "그룹"}
                      </span>
                      <span className="font-medium">{label}</span>
                      <button
                        type="button"
                        onClick={() => handleDelete(m.id)}
                        disabled={deleting[m.id]}
                        title={`${label} 연결 해제`}
                        className="ml-0.5 rounded-full p-0.5 hover:bg-rose-500/20 hover:text-rose-400 disabled:opacity-50 transition-colors"
                      >
                        {deleting[m.id]
                          ? <Loader2 className="h-3 w-3 animate-spin" />
                          : <X className="h-3 w-3" />}
                      </button>
                    </span>
                  );
                })}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* ── 페이지네이션 ── */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          전체 <span className="font-semibold text-foreground">{total.toLocaleString()}</span>개
          {totalPages > 1 && (
            <> · 페이지 <span className="font-semibold text-foreground">{page + 1}</span> / {totalPages}</>
          )}
        </p>
        <div className="flex items-center gap-1">
          <button
            onClick={() => goToPage(page - 1)}
            disabled={page === 0 || loading}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            이전
          </button>
          <button
            onClick={() => goToPage(page + 1)}
            disabled={page >= totalPages - 1 || loading}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            다음
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

    </div>
  );
}
