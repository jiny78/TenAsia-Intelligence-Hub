"""
scraper/schema.py — Articles 테이블 DDL 및 다국어 인덱스

다국어 전략:
    - 원문(한국어)  : content_ko (전체 본문)
    - 번역(영어)    : title_en, summary_en 위주
                     (전체 본문 번역은 API 비용 효율상 미제공)

지원 인덱스:
  ┌──────────────────────────────────────────────────────────────────┐
  │ 컬럼                        인덱스 타입   용도                    │
  ├──────────────────────────────────────────────────────────────────┤
  │ title_ko+content_ko+sum_ko  GIN (FTS)   한국어 전문 검색          │
  │ title_en+summary_en         GIN (FTS)   영어 전문 검색            │
  │ title_ko                    GIN trgm    한국어 제목 부분 매칭      │
  │ title_en                    GIN trgm    영어 제목 부분 매칭        │
  │ content_ko                  GIN trgm    한국어 원문 부분 매칭      │
  │ summary_ko                  GIN trgm    한국어 요약 부분 매칭      │
  │ summary_en                  GIN trgm    영어 요약 부분 매칭 ★NEW  │
  │ artist_name_ko              GIN trgm    한국어 아티스트명 검색      │
  │ artist_name_en              GIN trgm    영어 아티스트명 검색        │
  │ hashtags_ko                 GIN array   한국어 해시태그 포함 검색  │
  │ hashtags_en                 GIN array   영어 SEO 해시태그 검색     │
  │ global_priority             B-tree      글로벌 아티스트 필터링     │
  │ language                    B-tree      언어 필터링               │
  └──────────────────────────────────────────────────────────────────┘

사용법:
    from scraper.schema import create_article_tables
    create_article_tables()   # 앱 시작 시 1회 호출 (멱등)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Extensions
# ─────────────────────────────────────────────────────────────

_EXTENSIONS_DDL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
"""

# ─────────────────────────────────────────────────────────────
# 테이블
# ─────────────────────────────────────────────────────────────

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    id              SERIAL          PRIMARY KEY,
    source_url      TEXT            NOT NULL UNIQUE,

    language        VARCHAR(5)      NOT NULL DEFAULT 'kr'
                                    CHECK (language IN ('kr', 'en', 'jp')),

    -- ── 제목 (다국어) ─────────────────────────────────────────
    title_ko        TEXT,
    title_en        TEXT,

    -- ── 원문 (한국어 전문 — 영어 전체 번역은 비용 효율상 미제공) ──
    content_ko      TEXT,

    -- ── 요약 (SNS 캡션용 — 다국어) ────────────────────────────
    summary_ko      TEXT,
    summary_en      TEXT,

    -- ── 아티스트 정보 ──────────────────────────────────────────
    artist_name_ko  VARCHAR(200),
    artist_name_en  VARCHAR(200),

    -- global_priority: 해외 팬덤 있는 아티스트 여부
    --   true  → 영어 제목 + 요약 + 해시태그 전체 추출 (AI 비용 높음)
    --   false → 한국어 최소 추출만 (비용 절감)
    global_priority BOOLEAN         NOT NULL DEFAULT false,

    -- ── SEO 해시태그 (배열) ───────────────────────────────────
    hashtags_ko     TEXT[]          NOT NULL DEFAULT '{}',
    hashtags_en     TEXT[]          NOT NULL DEFAULT '{}',

    -- ── 미디어 ───────────────────────────────────────────────
    thumbnail_url   TEXT,

    -- ── 처리 상태 ─────────────────────────────────────────────
    author          VARCHAR(200),
    process_status  VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
                                    CHECK (process_status IN
                                        ('PENDING','SCRAPED','PROCESSED','ERROR')),

    -- ── 연결 ──────────────────────────────────────────────────
    job_id          INTEGER         REFERENCES job_queue(id) ON DELETE SET NULL,

    -- ── 시간 ──────────────────────────────────────────────────
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'set_updated_at_articles_v2'
    ) THEN
        CREATE TRIGGER set_updated_at_articles_v2
            BEFORE UPDATE ON articles
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
    END IF;
END;
$$;
"""

# ─────────────────────────────────────────────────────────────
# 인덱스
# ─────────────────────────────────────────────────────────────

_INDEXES_DDL = """
-- ============================================================
-- 1. 전문 검색 (FTS) — GIN
--    한국어: 'simple'  (언어 독립적 토큰화)
--    영어  : 'english' (어간 추출 — run/runs/running → run)
-- ============================================================

-- 한국어 FTS: 원문(content_ko) + 제목 + 요약
CREATE INDEX IF NOT EXISTS idx_articles_fts_ko
    ON articles
    USING GIN (
        to_tsvector(
            'simple',
            coalesce(title_ko,   '') || ' ' ||
            coalesce(content_ko, '') || ' ' ||
            coalesce(summary_ko, '')
        )
    );

-- 영어 FTS: 제목 + 요약 (전체 본문 번역 없음, 비용 절감)
CREATE INDEX IF NOT EXISTS idx_articles_fts_en
    ON articles
    USING GIN (
        to_tsvector(
            'english',
            coalesce(title_en,   '') || ' ' ||
            coalesce(summary_en, '')
        )
    );

-- ============================================================
-- 2. 트라이그램 (pg_trgm) — GIN
--    LIKE '%검색어%', ILIKE, similarity() 최적화
-- ============================================================

-- 한국어 제목
CREATE INDEX IF NOT EXISTS idx_articles_trgm_title_ko
    ON articles USING GIN (title_ko gin_trgm_ops);

-- 영어 제목
CREATE INDEX IF NOT EXISTS idx_articles_trgm_title_en
    ON articles USING GIN (title_en gin_trgm_ops);

-- 한국어 원문
CREATE INDEX IF NOT EXISTS idx_articles_trgm_content_ko
    ON articles USING GIN (content_ko gin_trgm_ops);

-- 한국어 요약
CREATE INDEX IF NOT EXISTS idx_articles_trgm_summary_ko
    ON articles USING GIN (summary_ko gin_trgm_ops);

-- 영어 요약 (글로벌 검색 핵심 — summary_en 위주 영어 서비스)
CREATE INDEX IF NOT EXISTS idx_articles_trgm_summary_en
    ON articles USING GIN (summary_en gin_trgm_ops);

-- 한국어 아티스트명
CREATE INDEX IF NOT EXISTS idx_articles_trgm_artist_ko
    ON articles USING GIN (artist_name_ko gin_trgm_ops);

-- 영어 아티스트명
CREATE INDEX IF NOT EXISTS idx_articles_trgm_artist_en
    ON articles USING GIN (artist_name_en gin_trgm_ops);

-- ============================================================
-- 3. SEO 해시태그 — GIN (배열 포함 검색)
--    WHERE hashtags_en @> ARRAY['bts']
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_articles_hashtags_ko
    ON articles USING GIN (hashtags_ko);

CREATE INDEX IF NOT EXISTS idx_articles_hashtags_en
    ON articles USING GIN (hashtags_en);

-- ============================================================
-- 4. 필터/정렬 — B-tree
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_articles_global_priority
    ON articles (global_priority, created_at DESC)
    WHERE global_priority = true;

CREATE INDEX IF NOT EXISTS idx_articles_language_date
    ON articles (language, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_articles_artist_en_date
    ON articles (artist_name_en, created_at DESC)
    WHERE artist_name_en IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_articles_process_status
    ON articles (process_status, created_at DESC);
"""


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────

def create_article_tables() -> None:
    """
    articles 테이블과 모든 인덱스를 생성합니다 (멱등 — 이미 있으면 무시).
    job_queue 테이블이 먼저 존재해야 합니다 (FK 참조).
    """
    from scraper.db import _conn

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_EXTENSIONS_DDL)
            cur.execute(_TABLE_DDL)
            cur.execute(_INDEXES_DDL)

    logger.info(
        "articles 테이블 초기화 완료 "
        "(FTS×2, Trigram×7, GIN-array×2, B-tree×4)"
    )


def article_search_ko(query: str, limit: int = 20) -> str:
    """
    한국어 전문 검색 쿼리 반환.

    검색 대상: title_ko + content_ko (원문) + summary_ko
    인덱스 활용: idx_articles_fts_ko (GIN, 'simple' tsvector)

    Usage:
        sql = article_search_ko("BTS 콘서트")
        rows = cur.execute(sql)
    """
    return f"""
        SELECT id, title_ko, artist_name_ko, published_at,
               ts_rank(
                   to_tsvector('simple',
                       coalesce(title_ko,'')   || ' ' ||
                       coalesce(content_ko,'') || ' ' ||
                       coalesce(summary_ko,'')
                   ),
                   plainto_tsquery('simple', '{query}')
               ) AS rank
        FROM   articles
        WHERE  to_tsvector('simple',
                   coalesce(title_ko,'')   || ' ' ||
                   coalesce(content_ko,'') || ' ' ||
                   coalesce(summary_ko,'')
               ) @@ plainto_tsquery('simple', '{query}')
        ORDER  BY rank DESC, created_at DESC
        LIMIT  {limit};
    """


def article_search_en(query: str, limit: int = 20) -> str:
    """
    영어 전문 검색 쿼리 반환.

    검색 대상: title_en + summary_en
    인덱스 활용: idx_articles_fts_en (GIN, 'english' tsvector)
    필터: global_priority=true (영어 콘텐츠가 있는 아티클만)

    Usage:
        sql = article_search_en("BTS concert")
        rows = cur.execute(sql)
    """
    return f"""
        SELECT id, title_en, artist_name_en, published_at,
               ts_rank(
                   to_tsvector('english',
                       coalesce(title_en,'')   || ' ' ||
                       coalesce(summary_en,'')
                   ),
                   plainto_tsquery('english', '{query}')
               ) AS rank
        FROM   articles
        WHERE  global_priority = true
          AND  to_tsvector('english',
                   coalesce(title_en,'')   || ' ' ||
                   coalesce(summary_en,'')
               ) @@ plainto_tsquery('english', '{query}')
        ORDER  BY rank DESC, created_at DESC
        LIMIT  {limit};
    """
