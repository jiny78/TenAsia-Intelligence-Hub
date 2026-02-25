"""
scraper/schema.py — Articles 테이블 DDL 및 다국어 인덱스

지원 인덱스:
  ┌──────────────────────────────────────────────────────────┐
  │ 컬럼               인덱스 타입   용도                     │
  ├──────────────────────────────────────────────────────────┤
  │ title_ko + body_ko GIN (FTS)   한국어 전문 검색           │
  │ title_en + body_en GIN (FTS)   영어 전문 검색             │
  │ title_ko           GIN trgm    한국어 제목 부분 매칭       │
  │ title_en           GIN trgm    영어 제목 부분 매칭         │
  │ body_ko            GIN trgm    한국어 본문 부분 매칭       │
  │ body_en            GIN trgm    영어 본문 부분 매칭         │
  │ artist_name_ko     GIN trgm    한국어 아티스트명 검색      │
  │ artist_name_en     GIN trgm    영어 아티스트명 검색        │
  │ hashtags_ko        GIN array   한국어 해시태그 포함 검색   │
  │ hashtags_en        GIN array   영어 SEO 해시태그 검색      │
  │ global_priority    B-tree      글로벌 아티스트 필터링      │
  │ language           B-tree      언어 필터링                 │
  └──────────────────────────────────────────────────────────┘

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
-- 트라이그램 검색 확장 (LIKE/부분 매칭 최적화)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- unaccent (영어 발음 부호 무시 검색)
CREATE EXTENSION IF NOT EXISTS unaccent;
"""

# ─────────────────────────────────────────────────────────────
# 테이블
# ─────────────────────────────────────────────────────────────

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    id              SERIAL          PRIMARY KEY,
    source_url      TEXT            NOT NULL UNIQUE,

    -- 언어 코드 ('kr' | 'en' | 'jp')
    language        VARCHAR(5)      NOT NULL DEFAULT 'kr'
                                    CHECK (language IN ('kr', 'en', 'jp')),

    -- ── 제목 (다국어) ────────────────────────────────────────
    title_ko        TEXT,
    title_en        TEXT,

    -- ── 본문 요약 (AI 생성, 다국어) ──────────────────────────
    body_ko         TEXT,
    body_en         TEXT,

    -- ── 짧은 요약 (SNS 캡션용) ───────────────────────────────
    summary_ko      TEXT,
    summary_en      TEXT,

    -- ── 아티스트 정보 ─────────────────────────────────────────
    artist_name_ko  VARCHAR(200),
    artist_name_en  VARCHAR(200),

    -- global_priority: 해외 팬덤 있는 아티스트 여부
    --   true  → 영어 + 해시태그 전체 추출 (AI 비용 높음)
    --   false → 한국어 최소 추출만 (비용 절감)
    global_priority BOOLEAN         NOT NULL DEFAULT false,

    -- ── SEO 해시태그 (배열) ──────────────────────────────────
    hashtags_ko     TEXT[]          NOT NULL DEFAULT '{}',
    hashtags_en     TEXT[]          NOT NULL DEFAULT '{}',  -- 영어 SEO 해시태그

    -- ── 미디어 ───────────────────────────────────────────────
    thumbnail_url   TEXT,           -- S3 퍼블릭 URL (성공 시)

    -- ── 연결 ─────────────────────────────────────────────────
    job_id          INTEGER         REFERENCES job_queue(id) ON DELETE SET NULL,

    -- ── 시간 ─────────────────────────────────────────────────
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- updated_at 자동 갱신 트리거 함수
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- 트리거 연결 (이미 존재하면 무시)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_updated_at_articles'
    ) THEN
        CREATE TRIGGER set_updated_at_articles
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
--    to_tsvector: 토큰화 + 어간 추출
--    'simple'  : 언어 독립적 (한국어 등 비영어)
--    'english' : 영어 어간 추출 (run/runs/running → run)
-- ============================================================

-- 한국어 전문 검색 (제목 + 본문 + 요약)
CREATE INDEX IF NOT EXISTS idx_articles_fts_ko
    ON articles
    USING GIN (
        to_tsvector(
            'simple',
            coalesce(title_ko, '')   || ' ' ||
            coalesce(body_ko,  '')   || ' ' ||
            coalesce(summary_ko, '')
        )
    );

-- 영어 전문 검색 (제목 + 본문 + 요약)
CREATE INDEX IF NOT EXISTS idx_articles_fts_en
    ON articles
    USING GIN (
        to_tsvector(
            'english',
            coalesce(title_en,   '')   || ' ' ||
            coalesce(body_en,    '')   || ' ' ||
            coalesce(summary_en, '')
        )
    );

-- ============================================================
-- 2. 트라이그램 (pg_trgm) — GIN (gin_trgm_ops)
--    LIKE '%검색어%', ILIKE, similarity() 최적화
--    한국어/영어 제목·본문·아티스트명 각각 생성
-- ============================================================

-- 한국어 제목 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_title_ko
    ON articles USING GIN (title_ko gin_trgm_ops);

-- 영어 제목 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_title_en
    ON articles USING GIN (title_en gin_trgm_ops);

-- 한국어 본문 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_body_ko
    ON articles USING GIN (body_ko gin_trgm_ops);

-- 영어 본문 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_body_en
    ON articles USING GIN (body_en gin_trgm_ops);

-- 한국어 아티스트명 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_artist_ko
    ON articles USING GIN (artist_name_ko gin_trgm_ops);

-- 영어 아티스트명 트라이그램
CREATE INDEX IF NOT EXISTS idx_articles_trgm_artist_en
    ON articles USING GIN (artist_name_en gin_trgm_ops);

-- ============================================================
-- 3. SEO 해시태그 — GIN (배열 포함 검색)
--    WHERE hashtags_en @> ARRAY['bts'] 등
-- ============================================================

-- 한국어 SEO 해시태그 배열
CREATE INDEX IF NOT EXISTS idx_articles_hashtags_ko
    ON articles USING GIN (hashtags_ko);

-- 영어 SEO 해시태그 배열 (글로벌 SEO 최적화 핵심)
CREATE INDEX IF NOT EXISTS idx_articles_hashtags_en
    ON articles USING GIN (hashtags_en);

-- ============================================================
-- 4. 필터/정렬 — B-tree
-- ============================================================

-- global_priority 필터 (글로벌 아티스트만 빠른 조회)
CREATE INDEX IF NOT EXISTS idx_articles_global_priority
    ON articles (global_priority, created_at DESC)
    WHERE global_priority = true;

-- 언어별 최신 아티클
CREATE INDEX IF NOT EXISTS idx_articles_language_date
    ON articles (language, created_at DESC);

-- 아티스트명(EN) + 날짜 복합 (아티스트별 히스토리)
CREATE INDEX IF NOT EXISTS idx_articles_artist_en_date
    ON articles (artist_name_en, created_at DESC)
    WHERE artist_name_en IS NOT NULL;
"""


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────

def create_article_tables() -> None:
    """
    articles 테이블과 모든 인덱스를 생성합니다 (멱등 — 이미 있으면 무시).
    job_queue 테이블이 먼저 존재해야 합니다 (FK 참조).

    호출 순서:
        from scraper.db import create_db_tables
        from scraper.schema import create_article_tables
        create_db_tables()      # job_queue 먼저
        create_article_tables() # articles 나중
    """
    from scraper.db import _conn  # 순환 import 방지용 지연 import

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_EXTENSIONS_DDL)
            cur.execute(_TABLE_DDL)
            cur.execute(_INDEXES_DDL)

    logger.info(
        "articles 테이블 초기화 완료 "
        "(FTS×2, Trigram×6, GIN-array×2, B-tree×3)"
    )


def article_search_ko(query: str, limit: int = 20) -> str:
    """
    한국어 전문 검색 쿼리 (예시).

    Usage:
        sql = article_search_ko("BTS 콘서트")
        # → SELECT ... WHERE to_tsvector('simple', ...) @@ plainto_tsquery(...)
    """
    return f"""
        SELECT id, title_ko, artist_name_ko, published_at,
               ts_rank(
                   to_tsvector('simple',
                       coalesce(title_ko,'') || ' ' ||
                       coalesce(body_ko,'')
                   ),
                   plainto_tsquery('simple', '{query}')
               ) AS rank
        FROM   articles
        WHERE  to_tsvector('simple',
                   coalesce(title_ko,'') || ' ' || coalesce(body_ko,'')
               ) @@ plainto_tsquery('simple', '{query}')
        ORDER  BY rank DESC, created_at DESC
        LIMIT  {limit};
    """


def article_search_en(query: str, limit: int = 20) -> str:
    """영어 전문 검색 쿼리 (예시)."""
    return f"""
        SELECT id, title_en, artist_name_en, published_at,
               ts_rank(
                   to_tsvector('english',
                       coalesce(title_en,'') || ' ' ||
                       coalesce(body_en,'')
                   ),
                   plainto_tsquery('english', '{query}')
               ) AS rank
        FROM   articles
        WHERE  global_priority = true
          AND  to_tsvector('english',
                   coalesce(title_en,'') || ' ' || coalesce(body_en,'')
               ) @@ plainto_tsquery('english', '{query}')
        ORDER  BY rank DESC, created_at DESC
        LIMIT  {limit};
    """
