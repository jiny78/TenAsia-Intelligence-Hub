"use client";
import { useState } from "react";
import { Plus, Trash2, Pencil, Search } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { glossaryApi } from "@/lib/api";
import useSWR from "swr";
import type { GlossaryEntry } from "@/lib/types";

const CAT_LABEL: Record<string, string> = {
  ARTIST: "🎤 아티스트",
  AGENCY: "🏢 소속사",
  EVENT: "🎪 이벤트",
};

export default function GlossaryPage() {
  const [search, setSearch] = useState("");
  const [cat, setCat] = useState("");
  const [editId, setEditId] = useState<number | null>(null);
  const [form, setForm] = useState({ term_ko: "", term_en: "", category: "ARTIST", description: "" });
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);

  const { data, isLoading, mutate } = useSWR<GlossaryEntry[]>(
    ["glossary", cat, search],
    () => glossaryApi.list({ category: cat || undefined, q: search || undefined }),
    { refreshInterval: 30_000 }
  );

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await glossaryApi.create(form);
      setForm({ term_ko: "", term_en: "", category: "ARTIST", description: "" });
      setAdding(false);
      mutate();
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: number) {
    await glossaryApi.delete(id);
    mutate();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-heading font-bold gradient-text">Glossary</h1>
          <p className="mt-1 text-sm text-muted-foreground">한국어-영어 고유명사 쌍을 관리합니다.</p>
        </div>
        <Button size="sm" onClick={() => setAdding((v) => !v)}>
          <Plus className="mr-1.5 h-4 w-4" />
          추가
        </Button>
      </div>

      <Separator />

      {/* Add Form */}
      {adding && (
        <Card>
          <CardHeader><CardTitle className="text-base">새 용어 추가</CardTitle></CardHeader>
          <CardContent>
            <form onSubmit={handleAdd} className="grid gap-3 sm:grid-cols-2">
              <Input placeholder="한국어 (term_ko)" value={form.term_ko} onChange={e => setForm(f => ({...f, term_ko: e.target.value}))} required />
              <Input placeholder="English (term_en)" value={form.term_en} onChange={e => setForm(f => ({...f, term_en: e.target.value}))} required />
              <select
                value={form.category}
                onChange={e => setForm(f => ({...f, category: e.target.value}))}
                className="flex h-10 rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="ARTIST">아티스트</option>
                <option value="AGENCY">소속사</option>
                <option value="EVENT">이벤트</option>
              </select>
              <Input placeholder="설명 (선택)" value={form.description} onChange={e => setForm(f => ({...f, description: e.target.value}))} />
              <div className="sm:col-span-2 flex gap-2">
                <Button type="submit" disabled={saving} className="flex-1">
                  {saving ? "저장 중…" : "저장"}
                </Button>
                <Button type="button" variant="outline" onClick={() => setAdding(false)}>취소</Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-9" placeholder="검색…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        {["", "ARTIST", "AGENCY", "EVENT"].map((c) => (
          <button
            key={c}
            onClick={() => setCat(c)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-all ${
              cat === c ? "bg-primary text-primary-foreground shadow-glow-sm" : "bg-muted text-muted-foreground hover:text-foreground"
            }`}
          >
            {c ? (CAT_LABEL[c] ?? c) : "전체"}
          </button>
        ))}
      </div>

      {/* List */}
      {isLoading ? (
        <div className="space-y-2">{Array.from({length:5}).map((_,i)=><Skeleton key={i} className="h-14 rounded-xl"/>)}</div>
      ) : !data?.length ? (
        <div className="flex flex-col items-center py-16 text-muted-foreground text-sm"><span className="text-4xl mb-2">📚</span><p>용어가 없습니다.</p></div>
      ) : (
        <div className="space-y-2">
          {data.map((entry) => (
            <Card key={entry.id} className="overflow-hidden">
              <CardContent className="flex items-center gap-4 py-3">
                <div className="flex-1 grid grid-cols-3 gap-4 min-w-0 text-sm">
                  <span className="font-medium">{entry.term_ko}</span>
                  <span className="text-muted-foreground">{entry.term_en}</span>
                  <span className="text-xs text-primary">{CAT_LABEL[entry.category] ?? entry.category}</span>
                </div>
                <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive hover:text-destructive shrink-0"
                  onClick={() => handleDelete(entry.id)}>
                  <Trash2 className="h-3.5 w-3.5"/>
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
