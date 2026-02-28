"use client";

import { useEffect, useState } from "react";
import { idolsApi, type EntityMappingItem } from "@/lib/api";
import { Trash2, Loader2, Plus, RefreshCw } from "lucide-react";

export function MappingsTab() {
  const [mappings, setMappings] = useState<EntityMappingItem[]>([]);
  const [loading, setLoading]   = useState(true);
  const [deleting, setDeleting] = useState<Record<number, boolean>>({});

  // filter state
  const [filterArtistId,  setFilterArtistId]  = useState("");
  const [filterGroupId,   setFilterGroupId]   = useState("");
  const [filterArticleId, setFilterArticleId] = useState("");

  // new mapping form
  const [newArticleId, setNewArticleId] = useState("");
  const [newArtistId,  setNewArtistId]  = useState("");
  const [newGroupId,   setNewGroupId]   = useState("");
  const [adding,       setAdding]       = useState(false);
  const [addError,     setAddError]     = useState("");

  async function load() {
    setLoading(true);
    try {
      const data = await idolsApi.listMappings({
        artist_id:  filterArtistId  ? Number(filterArtistId)  : undefined,
        group_id:   filterGroupId   ? Number(filterGroupId)   : undefined,
        article_id: filterArticleId ? Number(filterArticleId) : undefined,
      });
      setMappings(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleDelete(id: number) {
    setDeleting((p) => ({ ...p, [id]: true }));
    try {
      await idolsApi.deleteMapping(id);
      setMappings((prev) => prev.filter((m) => m.id !== id));
    } catch (e) {
      alert(`삭제 실패: ${e}`);
    } finally {
      setDeleting((p) => ({ ...p, [id]: false }));
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
      await load();
    } catch (e) {
      setAddError(`추가 실패: ${e}`);
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="space-y-5">
      {/* Add new mapping */}
      <div className="rounded-xl border border-border bg-card p-4 space-y-3">
        <h2 className="font-semibold text-sm">새 매핑 추가</h2>
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

      {/* Filter + refresh */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <label className="text-[10px] text-muted-foreground uppercase tracking-wider">아티스트 ID 필터</label>
          <input
            type="number"
            value={filterArtistId}
            onChange={(e) => setFilterArtistId(e.target.value)}
            placeholder="artist_id"
            className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] text-muted-foreground uppercase tracking-wider">그룹 ID 필터</label>
          <input
            type="number"
            value={filterGroupId}
            onChange={(e) => setFilterGroupId(e.target.value)}
            placeholder="group_id"
            className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] text-muted-foreground uppercase tracking-wider">기사 ID 필터</label>
          <input
            type="number"
            value={filterArticleId}
            onChange={(e) => setFilterArticleId(e.target.value)}
            placeholder="article_id"
            className="rounded-md border border-border bg-background px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50 transition-colors"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          검색
        </button>
        <span className="text-xs text-muted-foreground pb-1.5">{mappings.length}개</span>
      </div>

      {/* Table */}
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
                      <span className="text-xs">{m.artist_name_ko ?? `#${m.artist_id}`}</span>
                    </span>
                  ) : m.entity_type === "GROUP" ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="rounded-full bg-pink-500/15 text-pink-400 text-[10px] font-semibold px-1.5 py-0.5">그룹</span>
                      <span className="text-xs">{m.group_name_ko ?? `#${m.group_id}`}</span>
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
    </div>
  );
}
