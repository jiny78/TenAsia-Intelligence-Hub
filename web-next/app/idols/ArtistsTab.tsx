"use client";

import { useEffect, useRef, useState } from "react";
import { idolsApi, type PublicArtist } from "@/lib/api";
import { Camera, Loader2, RefreshCw, RotateCcw, Trash2, UserCircle2 } from "lucide-react";

export function ArtistsTab() {
  const [artists, setArtists]     = useState<PublicArtist[]>([]);
  const [loading, setLoading]     = useState(true);
  const [query, setQuery]         = useState("");
  const [resetting, setResetting] = useState<Record<number, boolean>>({});
  const [deleting, setDeleting]   = useState<Record<number, boolean>>({});
  const [uploading, setUploading] = useState<Record<number, boolean>>({});
  const [errors, setErrors]       = useState<Record<number, string>>({});
  const fileInputRefs = useRef<Record<number, HTMLInputElement | null>>({});

  async function load() {
    setLoading(true);
    try {
      const data = await idolsApi.listArtists();
      setArtists(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleDelete(artistId: number, name: string) {
    if (!confirm(`"${name}" 아티스트를 삭제하시겠습니까?\n관련 기사 매핑도 함께 삭제됩니다.`)) return;
    setDeleting((p) => ({ ...p, [artistId]: true }));
    try {
      await idolsApi.deleteArtist(artistId);
      setArtists((prev) => prev.filter((a) => a.id !== artistId));
    } catch (e) {
      alert(`삭제 실패: ${e}`);
    } finally {
      setDeleting((p) => ({ ...p, [artistId]: false }));
    }
  }

  async function handleResetEnrichment(artistId: number) {
    if (!confirm("이 아티스트의 보강 데이터를 초기화하시겠습니까? 다음 보강 실행 시 재보강됩니다.")) return;
    setResetting((p) => ({ ...p, [artistId]: true }));
    try {
      await idolsApi.resetArtistEnrichment(artistId);
      setArtists((prev) =>
        prev.map((a) =>
          a.id === artistId
            ? { ...a, name_en: null }
            : a
        )
      );
    } catch (e) {
      setErrors((p) => ({ ...p, [artistId]: String(e) }));
    } finally {
      setResetting((p) => ({ ...p, [artistId]: false }));
    }
  }

  async function handlePhotoUpload(artistId: number, file: File) {
    setUploading((p) => ({ ...p, [artistId]: true }));
    setErrors((p) => ({ ...p, [artistId]: "" }));
    try {
      const result = await idolsApi.uploadArtistPhoto(artistId, file);
      setArtists((prev) =>
        prev.map((a) => a.id === artistId ? { ...a, photo_url: result.photo_url } : a)
      );
    } catch (e) {
      setErrors((p) => ({ ...p, [artistId]: "업로드 실패" }));
    } finally {
      setUploading((p) => ({ ...p, [artistId]: false }));
    }
  }

  const filtered = artists.filter((a) =>
    !query ||
    a.name_ko.includes(query) ||
    (a.stage_name_ko ?? "").includes(query) ||
    (a.name_en ?? "").toLowerCase().includes(query.toLowerCase())
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="아티스트명 검색..."
          className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary w-56"
        />
        <button
          type="button"
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
              <th className="px-3 py-3 w-12 text-left">사진</th>
              <th className="px-4 py-3 text-left">아티스트</th>
              <th className="px-4 py-3 text-left">영어명</th>
              <th className="px-4 py-3 text-left w-20">보강</th>
              <th className="px-4 py-3 w-12"></th>
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
                  아티스트 없음
                </td>
              </tr>
            )}
            {filtered.map((artist) => (
              <tr key={artist.id} className="hover:bg-muted/30 transition-colors">
                {/* 사진 컬럼 */}
                <td className="px-3 py-2">
                  <div className="relative group w-9 h-9">
                    {artist.photo_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={artist.photo_url}
                        alt={artist.name_ko}
                        className="w-9 h-9 rounded-full object-cover border border-border"
                      />
                    ) : (
                      <div className="w-9 h-9 rounded-full bg-muted flex items-center justify-center border border-border">
                        <UserCircle2 className="h-5 w-5 text-muted-foreground" />
                      </div>
                    )}
                    {/* 카메라 오버레이 (hover) */}
                    <button
                      type="button"
                      onClick={() => fileInputRefs.current[artist.id]?.click()}
                      disabled={uploading[artist.id]}
                      title="프로필 사진 업로드"
                      className="absolute inset-0 rounded-full bg-black/50 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity disabled:cursor-not-allowed"
                    >
                      {uploading[artist.id]
                        ? <Loader2 className="h-3.5 w-3.5 text-white animate-spin" />
                        : <Camera className="h-3.5 w-3.5 text-white" />}
                    </button>
                    {/* 숨겨진 파일 input */}
                    <input
                      ref={(el) => { fileInputRefs.current[artist.id] = el; }}
                      type="file"
                      accept="image/*"
                      className="hidden"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) handlePhotoUpload(artist.id, file);
                        e.target.value = "";
                      }}
                    />
                  </div>
                  {errors[artist.id] === "업로드 실패" && (
                    <span className="text-[9px] text-rose-500 block text-center">실패</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <p className="font-semibold text-sm">{artist.name_ko}</p>
                  {artist.stage_name_ko && artist.stage_name_ko !== artist.name_ko && (
                    <p className="text-[10px] text-muted-foreground">{artist.stage_name_ko}</p>
                  )}
                </td>
                <td className="px-4 py-3 text-xs text-muted-foreground">
                  {artist.name_en ?? "—"}
                </td>
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => handleResetEnrichment(artist.id)}
                    disabled={resetting[artist.id]}
                    title="보강 데이터 초기화 (재보강 대상으로 설정)"
                    className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[10px] text-muted-foreground hover:text-rose-500 hover:border-rose-400 disabled:opacity-50 transition-colors"
                  >
                    {resetting[artist.id]
                      ? <Loader2 className="h-3 w-3 animate-spin" />
                      : <RotateCcw className="h-3 w-3" />}
                    초기화
                  </button>
                  {errors[artist.id] && errors[artist.id] !== "업로드 실패" && (
                    <span className="ml-1 text-[10px] text-rose-500">오류</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => handleDelete(artist.id, artist.stage_name_ko ?? artist.name_ko)}
                    disabled={deleting[artist.id]}
                    title="아티스트 삭제"
                    className="inline-flex items-center justify-center rounded-md border border-border p-1.5 text-muted-foreground hover:text-rose-500 hover:border-rose-400 disabled:opacity-50 transition-colors"
                  >
                    {deleting[artist.id]
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
