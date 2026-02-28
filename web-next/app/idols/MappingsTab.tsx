"use client";

import { useEffect, useState, useCallback } from "react";
import { idolsApi, type EntityMappingItem } from "@/lib/api";
import { Trash2, Loader2, Plus, RefreshCw, ChevronLeft, ChevronRight, Search } from "lucide-react";

const PAGE_SIZE = 50;

export function MappingsTab() {
  const [mappings, setMappings] = useState<EntityMappingItem[]>([]);
  const [total,    setTotal]    = useState(0);
  const [loading,  setLoading]  = useState(true);
  const [deleting, setDeleting] = useState<Record<number, boolean>>({});

  // 검색 상태
  const [searchQ,         setSearchQ]         = useState("");  // 이름 통합 검색
  const [filterArticleId, setFilterArticleId] = useState("");  // 기사 ID 정확 검색
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
      setMappings((prev) => prev.filter((m) => m.id !== id));
      setTotal((t) => t - 1);
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

      {/* ── 검색 + 새로고침 ── */}
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

      {/* ── 테이블 ── */}
      <div className="rounded-xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-xs font-semibold text-muted-foreground">
            <tr>
              <th className="px-4 py-3 text-left w-14">ID</th>
              <th className="px-4 py-3 text-left">기사</th>
              <th className="px-4 py-3 text-left">엔티티</th>
              <th className="px-4 py-3 text-left w-16">신뢰도</th>
              <th className="px-4 py-3 text-center w-14">삭제</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {loading && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center">
                  <Loader2 className="h-5 w-5 animate-spin mx-auto text-muted-foreground" />
                </td>
              </tr>
            )}
            {!loading && mappings.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-xs text-muted-foreground">
                  매핑 없음
                </td>
              </tr>
            )}
            {mappings.map((m) => (
              <tr key={m.id} className="hover:bg-muted/30 transition-colors">
                <td className="px-4 py-2.5 text-xs text-muted-foreground font-mono">{m.id}</td>
                <td className="px-4 py-2.5 max-w-xs">
                  <p className="text-xs font-medium line-clamp-1 mb-0.5">
                    {m.article_title_ko ?? `기사 #${m.article_id}`}
                  </p>
                  <p className="text-[10px] text-muted-foreground font-mono">id={m.article_id}</p>
                </td>
                <td className="px-4 py-2.5">
                  {m.entity_type === "ARTIST" ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="rounded-full bg-purple-500/15 text-purple-400 text-[10px] font-semibold px-1.5 py-0.5">아티스트</span>
                      <span className="text-xs font-medium">{m.artist_name_ko ?? `#${m.artist_id}`}</span>
                    </span>
                  ) : m.entity_type === "GROUP" ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="rounded-full bg-pink-500/15 text-pink-400 text-[10px] font-semibold px-1.5 py-0.5">그룹</span>
                      <span className="text-xs font-medium">{m.group_name_ko ?? `#${m.group_id}`}</span>
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">
                  {m.confidence_score != null ? `${(m.confidence_score * 100).toFixed(0)}%` : "—"}
                </td>
                <td className="px-4 py-2.5 text-center">
                  <button
                    onClick={() => handleDelete(m.id)}
                    disabled={deleting[m.id]}
                    className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 disabled:opacity-50 transition-colors"
                  >
                    {deleting[m.id]
                      ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      : <Trash2 className="h-3.5 w-3.5" />}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
