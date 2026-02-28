"use client";

import { useEffect, useState } from "react";
import { idolsApi, type PublicGroup } from "@/lib/api";
import { Check, Loader2, RefreshCw, RotateCcw } from "lucide-react";

const STATUS_OPTIONS = [
  { value: "ACTIVE",    label: "활동 중",    color: "text-emerald-500" },
  { value: "HIATUS",    label: "활동 중단",  color: "text-amber-500" },
  { value: "DISBANDED", label: "해체",       color: "text-rose-500" },
  { value: "SOLO_ONLY", label: "솔로 활동",  color: "text-blue-400" },
];

export function GroupsTab() {
  const [groups, setGroups]   = useState<PublicGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery]     = useState("");
  const [saving, setSaving]       = useState<Record<number, boolean>>({});
  const [saved, setSaved]         = useState<Record<number, boolean>>({});
  const [errors, setErrors]       = useState<Record<number, string>>({});
  const [resetting, setResetting] = useState<Record<number, boolean>>({});

  async function load() {
    setLoading(true);
    try {
      const data = await idolsApi.listGroups();
      setGroups(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleStatusChange(groupId: number, status: string) {
    setSaving((p) => ({ ...p, [groupId]: true }));
    setSaved((p)  => ({ ...p, [groupId]: false }));
    setErrors((p) => ({ ...p, [groupId]: "" }));
    try {
      await idolsApi.updateGroup(groupId, { activity_status: status });
      setGroups((prev) =>
        prev.map((g) => (g.id === groupId ? { ...g, activity_status: status } : g))
      );
      setSaved((p) => ({ ...p, [groupId]: true }));
      setTimeout(() => setSaved((p) => ({ ...p, [groupId]: false })), 2000);
    } catch (e) {
      setErrors((p) => ({ ...p, [groupId]: String(e) }));
    } finally {
      setSaving((p) => ({ ...p, [groupId]: false }));
    }
  }

  async function handleResetEnrichment(groupId: number) {
    if (!confirm("이 그룹의 보강 데이터를 초기화하시겠습니까? 다음 보강 실행 시 재보강됩니다.")) return;
    setResetting((p) => ({ ...p, [groupId]: true }));
    try {
      await idolsApi.resetGroupEnrichment(groupId);
      // Clear enricher-filled fields locally
      setGroups((prev) =>
        prev.map((g) =>
          g.id === groupId
            ? { ...g, name_en: null, debut_date: null, label_ko: null, fandom_name_ko: null }
            : g
        )
      );
    } catch (e) {
      setErrors((p) => ({ ...p, [groupId]: String(e) }));
    } finally {
      setResetting((p) => ({ ...p, [groupId]: false }));
    }
  }

  const filtered = groups.filter((g) =>
    !query || g.name_ko.includes(query) || (g.name_en ?? "").toLowerCase().includes(query.toLowerCase())
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="그룹명 검색..."
          className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary w-56"
        />
        <button
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50 transition-colors"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          새로고침
        </button>
        <span className="text-xs text-muted-foreground">{filtered.length}개</span>
      </div>

      <div className="rounded-xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-xs font-semibold text-muted-foreground">
            <tr>
              <th className="px-4 py-3 text-left">그룹</th>
              <th className="px-4 py-3 text-left">데뷔</th>
              <th className="px-4 py-3 text-left">소속사</th>
              <th className="px-4 py-3 text-left w-48">활동 상태</th>
              <th className="px-4 py-3 text-left w-20">보강</th>
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
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-xs text-muted-foreground">
                  그룹 없음
                </td>
              </tr>
            )}
            {filtered.map((group) => {
              const currentOpt = STATUS_OPTIONS.find((s) => s.value === group.activity_status);
              return (
                <tr key={group.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">
                    <p className="font-semibold text-sm">{group.name_ko}</p>
                    {group.name_en && (
                      <p className="text-[10px] text-muted-foreground">{group.name_en}</p>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">
                    {group.debut_date ? group.debut_date.slice(0, 7) : "—"}
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">
                    {group.label_ko ?? "—"}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <select
                        value={group.activity_status ?? ""}
                        onChange={(e) => handleStatusChange(group.id, e.target.value)}
                        disabled={saving[group.id]}
                        className={`rounded-md border border-border bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-50 ${currentOpt?.color ?? ""}`}
                      >
                        <option value="">— 미설정 —</option>
                        {STATUS_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                      {saving[group.id] && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
                      {saved[group.id]  && <Check className="h-3.5 w-3.5 text-emerald-500" />}
                      {errors[group.id] && <span className="text-[10px] text-rose-500">오류</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => handleResetEnrichment(group.id)}
                      disabled={resetting[group.id]}
                      title="보강 데이터 초기화 (재보강 대상으로 설정)"
                      className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[10px] text-muted-foreground hover:text-rose-500 hover:border-rose-400 disabled:opacity-50 transition-colors"
                    >
                      {resetting[group.id]
                        ? <Loader2 className="h-3 w-3 animate-spin" />
                        : <RotateCcw className="h-3 w-3" />}
                      초기화
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
