"use client";

import { useEffect, useRef, useState } from "react";
import { galleryApi, articlesApi } from "@/lib/api";
import type { GalleryPhoto } from "@/lib/types";
import { ImageGallery } from "@/components/gallery/image-gallery";
import { ImagePlus, Loader2, Trash2, Upload, X } from "lucide-react";

// ── 기사에 이미지 추가 탭 ──────────────────────────────────────
function ArticleImageUploadForm() {
  const [articleId, setArticleId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<{ url: string } | null>(null);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    setResult(null);
    setError("");
    if (f) {
      const url = URL.createObjectURL(f);
      setPreview(url);
    } else {
      setPreview(null);
    }
  }

  function clearFile() {
    setFile(null);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const id = parseInt(articleId, 10);
    if (!id || !file) return;
    setUploading(true);
    setError("");
    setResult(null);
    try {
      const res = await galleryApi.uploadToArticle(id, file);
      setResult({ url: res.url });
      clearFile();
      setArticleId("");
    } catch (err) {
      setError(String(err));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="max-w-lg space-y-4">
      <p className="text-sm text-muted-foreground">
        기사 ID를 입력하고 이미지를 업로드하면 해당 기사의 추가 이미지로 등록됩니다.
      </p>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-xs font-medium mb-1.5">기사 ID</label>
          <input
            type="number"
            value={articleId}
            onChange={(e) => setArticleId(e.target.value)}
            placeholder="예: 1042"
            required
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <div>
          <label className="block text-xs font-medium mb-1.5">이미지 파일</label>
          {preview ? (
            <div className="relative inline-block">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={preview} alt="preview" className="h-40 rounded-lg object-cover border border-border" />
              <button
                type="button"
                onClick={clearFile}
                className="absolute -top-2 -right-2 rounded-full bg-background border border-border p-0.5 hover:bg-muted"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="flex items-center gap-2 rounded-lg border-2 border-dashed border-border px-6 py-8 text-sm text-muted-foreground hover:border-primary hover:text-foreground transition-colors w-full justify-center"
            >
              <ImagePlus className="h-5 w-5" />
              이미지 선택
            </button>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleFileChange}
          />
        </div>
        {error && <p className="text-xs text-rose-500">{error}</p>}
        {result && (
          <p className="text-xs text-emerald-500">업로드 완료!</p>
        )}
        <button
          type="submit"
          disabled={!articleId || !file || uploading}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
        >
          {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
          업로드
        </button>
      </form>
    </div>
  );
}

// ── 독립 갤러리 관리 탭 ────────────────────────────────────────
function StandaloneGalleryManager() {
  const [photos, setPhotos] = useState<GalleryPhoto[]>([]);
  const [loading, setLoading] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [uploading, setUploading] = useState(false);
  const [deleting, setDeleting] = useState<Record<number, boolean>>({});
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function loadPhotos() {
    setLoading(true);
    try {
      const data = await galleryApi.list({ limit: 100 });
      setPhotos(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadPhotos(); }, []);

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    setError("");
    if (f) {
      const url = URL.createObjectURL(f);
      setPreview(url);
    } else {
      setPreview(null);
    }
  }

  function clearFile() {
    setFile(null);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      const photo = await galleryApi.upload(file, title || undefined);
      setPhotos((prev) => [photo, ...prev]);
      clearFile();
      setTitle("");
    } catch (err) {
      setError(String(err));
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("이 사진을 삭제하시겠습니까?")) return;
    setDeleting((p) => ({ ...p, [id]: true }));
    try {
      await galleryApi.delete(id);
      setPhotos((prev) => prev.filter((p) => p.id !== id));
    } catch (e) {
      alert(`삭제 실패: ${e}`);
    } finally {
      setDeleting((p) => ({ ...p, [id]: false }));
    }
  }

  return (
    <div className="space-y-6">
      {/* 업로드 폼 */}
      <div className="rounded-xl border border-border p-4 space-y-4 max-w-lg">
        <h3 className="text-sm font-semibold">새 사진 업로드</h3>
        <form onSubmit={handleUpload} className="space-y-3">
          <div>
            <label className="block text-xs font-medium mb-1.5">제목 (선택)</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="이미지 제목..."
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div>
            {preview ? (
              <div className="relative inline-block">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={preview} alt="preview" className="h-40 rounded-lg object-cover border border-border" />
                <button
                  type="button"
                  onClick={clearFile}
                  className="absolute -top-2 -right-2 rounded-full bg-background border border-border p-0.5 hover:bg-muted"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="flex items-center gap-2 rounded-lg border-2 border-dashed border-border px-6 py-8 text-sm text-muted-foreground hover:border-primary hover:text-foreground transition-colors w-full justify-center"
              >
                <ImagePlus className="h-5 w-5" />
                이미지 선택
              </button>
            )}
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handleFileChange}
            />
          </div>
          {error && <p className="text-xs text-rose-500">{error}</p>}
          <button
            type="submit"
            disabled={!file || uploading}
            className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
          >
            {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            업로드
          </button>
        </form>
      </div>

      {/* 갤러리 그리드 */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <h3 className="text-sm font-semibold">업로드된 사진</h3>
          <span className="text-xs text-muted-foreground">{photos.length}개</span>
        </div>
        {loading ? (
          <div className="flex justify-center py-10">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : photos.length === 0 ? (
          <p className="text-sm text-muted-foreground py-8 text-center">업로드된 사진이 없습니다.</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
            {photos.map((photo) => (
              <div key={photo.id} className="group relative aspect-square overflow-hidden rounded-xl border border-border/50">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={photo.s3_url}
                  alt={photo.title ?? "gallery"}
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-110"
                />
                {/* 호버 오버레이 */}
                <div className="absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-black/80 to-transparent p-2 opacity-0 group-hover:opacity-100 transition-opacity">
                  {photo.title && (
                    <p className="text-[10px] text-white font-medium line-clamp-2 mb-1">{photo.title}</p>
                  )}
                  {photo.article_id && (
                    <p className="text-[9px] text-white/70 mb-1">기사 #{photo.article_id}</p>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(photo.id)}
                    disabled={deleting[photo.id]}
                    className="self-end rounded-md bg-rose-500/80 p-1 hover:bg-rose-600 disabled:opacity-50 transition-colors"
                  >
                    {deleting[photo.id]
                      ? <Loader2 className="h-3 w-3 text-white animate-spin" />
                      : <Trash2 className="h-3 w-3 text-white" />}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── 메인 갤러리 페이지 ─────────────────────────────────────────
const TABS = [
  { id: "articles",       label: "기사 갤러리" },
  { id: "upload-article", label: "기사에 이미지 추가" },
  { id: "standalone",     label: "독립 갤러리" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function GalleryPage() {
  const [activeTab, setActiveTab] = useState<TabId>("articles");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-heading font-bold gradient-text">갤러리</h1>
        <p className="mt-1 text-sm text-muted-foreground">이미지 관리 및 수동 업로드</p>
      </div>

      {/* 탭 네비게이션 */}
      <div className="flex gap-1 rounded-xl bg-muted p-1 w-fit">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={`rounded-lg px-4 py-2 text-xs font-semibold transition-all ${
              activeTab === tab.id
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* 탭 컨텐츠 */}
      {activeTab === "articles" && <ImageGallery />}
      {activeTab === "upload-article" && <ArticleImageUploadForm />}
      {activeTab === "standalone" && <StandaloneGalleryManager />}
    </div>
  );
}
