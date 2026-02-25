"""
scraper/db.py — Job Queue DB 연산

PostgreSQL job_queue 테이블을 사용하는 분산 작업 큐입니다.
SKIP LOCKED를 이용해 여러 워커가 경합 없이 작업을 가져갑니다.

테이블 스키마:
    id           SERIAL PK
    job_type     VARCHAR(50)    예: 'scrape'
    params       JSONB          입력 파라미터
    status       VARCHAR(20)    pending → running → completed | failed
    priority     INTEGER        높을수록 먼저 처리 (기본 5)
    created_at   TIMESTAMPTZ
    started_at   TIMESTAMPTZ
    completed_at TIMESTAMPTZ
    worker_id    VARCHAR(100)   처리한 EC2 인스턴스 식별자
    result       JSONB          완료 결과
    error_msg    TEXT           실패 사유
    retry_count  INTEGER        현재 재시도 횟수
    max_retries  INTEGER        최대 재시도 횟수 (기본 3)
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# ── 상태 상수 ─────────────────────────────────────────────────
STATUS_PENDING   = "pending"
STATUS_RUNNING   = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_CANCELLED = "cancelled"

# ── 테이블 DDL ────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS job_queue (
    id           SERIAL       PRIMARY KEY,
    job_type     VARCHAR(50)  NOT NULL DEFAULT 'scrape',
    params       JSONB        NOT NULL DEFAULT '{}',
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','running','completed','failed','cancelled')),
    priority     INTEGER      NOT NULL DEFAULT 5,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    worker_id    VARCHAR(100),
    result       JSONB,
    error_msg    TEXT,
    retry_count  INTEGER      NOT NULL DEFAULT 0,
    max_retries  INTEGER      NOT NULL DEFAULT 3
);

CREATE INDEX IF NOT EXISTS idx_jq_pending
    ON job_queue (status, priority DESC, created_at ASC)
    WHERE status = 'pending';
"""


def _db_url() -> str:
    from core.config import settings
    return settings.DATABASE_URL


@contextmanager
def _conn():
    """psycopg2 커넥션 컨텍스트 매니저 — 커밋/롤백 자동 처리"""
    conn = psycopg2.connect(_db_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 초기화
# ─────────────────────────────────────────────────────────────

def create_db_tables() -> None:
    """
    앱 시작 시 1회 호출. 모든 테이블이 없으면 생성합니다 (멱등).

    실행 순서:
      1. job_queue 테이블 (FK 기준)
      2. articles 테이블 + 다국어 인덱스 (job_queue 참조)
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
    logger.info("job_queue 테이블 초기화 완료")

    # articles 테이블 + GIN/Trigram 인덱스 생성
    from scraper.schema import create_article_tables
    create_article_tables()


# ─────────────────────────────────────────────────────────────
# 쓰기
# ─────────────────────────────────────────────────────────────

def create_job(
    job_type: str,
    params: dict[str, Any],
    priority: int = 5,
    max_retries: int = 3,
) -> int:
    """
    작업 큐에 새 작업을 추가합니다.

    Returns:
        생성된 job_id (int)

    Example:
        job_id = create_job("scrape", {
            "source_url": "https://tenasia.hankyung.com/...",
            "language":   "kr",
            "platforms":  ["x", "instagram"],
        })
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_queue (job_type, params, priority, max_retries)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (job_type, json.dumps(params), priority, max_retries),
            )
            job_id: int = cur.fetchone()[0]

    logger.info("작업 생성 | id=%d type=%s priority=%d", job_id, job_type, priority)
    return job_id


def update_job_status(
    job_id: int,
    status: str,
    result: Optional[dict] = None,
    error_msg: Optional[str] = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_queue
                SET status       = %s,
                    completed_at = CASE WHEN %s IN ('completed','failed','cancelled')
                                        THEN NOW() ELSE completed_at END,
                    result       = COALESCE(%s::jsonb, result),
                    error_msg    = COALESCE(%s, error_msg)
                WHERE id = %s
                """,
                (
                    status, status,
                    json.dumps(result) if result else None,
                    error_msg,
                    job_id,
                ),
            )
    logger.debug("작업 상태 변경 | id=%d → %s", job_id, status)


def increment_retry(job_id: int) -> int:
    """
    재시도 횟수를 증가시킵니다.
    retry_count >= max_retries 면 status를 'failed'로 변경합니다.

    Returns:
        갱신된 retry_count
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_queue
                SET retry_count = retry_count + 1,
                    status      = CASE WHEN retry_count + 1 >= max_retries
                                       THEN 'failed' ELSE 'pending' END,
                    error_msg   = NULL,
                    started_at  = NULL,
                    worker_id   = NULL
                WHERE id = %s
                RETURNING retry_count
                """,
                (job_id,),
            )
            return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────
# 읽기 (워커용)
# ─────────────────────────────────────────────────────────────

def get_pending_job(worker_id: str) -> Optional[dict]:
    """
    SKIP LOCKED로 pending 작업을 원자적으로 가져와 running 으로 전환합니다.
    여러 EC2 워커가 동시에 실행돼도 중복 처리되지 않습니다.

    Returns:
        작업 딕셔너리 또는 None (큐 비어있을 때)
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE job_queue
                SET status     = 'running',
                    started_at = NOW(),
                    worker_id  = %s
                WHERE id = (
                    SELECT id FROM job_queue
                    WHERE  status = 'pending'
                    ORDER  BY priority DESC, created_at ASC
                    LIMIT  1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (worker_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# 읽기 (UI용)
# ─────────────────────────────────────────────────────────────

def get_job_by_id(job_id: int) -> Optional[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM job_queue WHERE id = %s", (job_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_recent_jobs(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, job_type, status, priority, params,
                       created_at, started_at, completed_at,
                       worker_id, error_msg, retry_count, max_retries
                FROM   job_queue
                ORDER  BY created_at DESC
                LIMIT  %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_queue_stats() -> dict[str, int]:
    """상태별 작업 수를 반환합니다 (대시보드용)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM job_queue GROUP BY status"
            )
            rows = cur.fetchall()
    base = {s: 0 for s in (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED)}
    base.update({row[0]: row[1] for row in rows})
    return base


def cancel_job(job_id: int) -> bool:
    """pending 상태인 작업을 취소합니다. 성공 시 True 반환."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_queue SET status = 'cancelled'
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (job_id,),
            )
            return cur.fetchone() is not None


# ─────────────────────────────────────────────────────────────
# Articles CRUD
# ─────────────────────────────────────────────────────────────

def upsert_article(
    source_url: str,
    data: dict[str, Any],
    job_id: Optional[int] = None,
) -> int:
    """
    아티클을 삽입하거나 갱신합니다 (source_url 기준 UPSERT).

    data 키 (모두 Optional):
        title_ko, title_en,
        content_ko,                  ← 원문(한국어) 전체 본문
        summary_ko, summary_en,      ← 요약 (영어 전체 번역 미제공)
        author,
        artist_name_ko, artist_name_en,
        global_priority, hashtags_ko, hashtags_en,
        seo_hashtags,                ← AI SEO 해시태그 (JSONB, 메타데이터 포함)
        thumbnail_url, published_at, language,
        process_status

    Returns:
        articles.id (int)
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO articles (
                    source_url,     language,
                    title_ko,       title_en,
                    content_ko,
                    summary_ko,     summary_en,
                    author,
                    artist_name_ko, artist_name_en,
                    global_priority,
                    hashtags_ko,    hashtags_en,
                    seo_hashtags,
                    thumbnail_url,
                    process_status,
                    job_id,         published_at
                ) VALUES (
                    %s, %s,
                    %s, %s,
                    %s,
                    %s, %s,
                    %s,
                    %s, %s,
                    %s,
                    %s, %s,
                    %s::jsonb,
                    %s,
                    %s,
                    %s, %s
                )
                ON CONFLICT (source_url) DO UPDATE SET
                    language        = EXCLUDED.language,
                    title_ko        = COALESCE(EXCLUDED.title_ko,        articles.title_ko),
                    title_en        = COALESCE(EXCLUDED.title_en,        articles.title_en),
                    content_ko      = COALESCE(EXCLUDED.content_ko,      articles.content_ko),
                    summary_ko      = COALESCE(EXCLUDED.summary_ko,      articles.summary_ko),
                    summary_en      = COALESCE(EXCLUDED.summary_en,      articles.summary_en),
                    author          = COALESCE(EXCLUDED.author,          articles.author),
                    artist_name_ko  = COALESCE(EXCLUDED.artist_name_ko,  articles.artist_name_ko),
                    artist_name_en  = COALESCE(EXCLUDED.artist_name_en,  articles.artist_name_en),
                    global_priority = EXCLUDED.global_priority,
                    hashtags_ko     = EXCLUDED.hashtags_ko,
                    hashtags_en     = EXCLUDED.hashtags_en,
                    seo_hashtags    = COALESCE(EXCLUDED.seo_hashtags,    articles.seo_hashtags),
                    thumbnail_url   = COALESCE(EXCLUDED.thumbnail_url,   articles.thumbnail_url),
                    process_status  = EXCLUDED.process_status,
                    updated_at      = NOW()
                RETURNING id
                """,
                (
                    source_url,
                    data.get("language", "kr"),
                    data.get("title_ko"),
                    data.get("title_en"),
                    data.get("content_ko"),
                    data.get("summary_ko"),
                    data.get("summary_en"),
                    data.get("author"),
                    data.get("artist_name_ko"),
                    data.get("artist_name_en"),
                    data.get("global_priority", False),
                    data.get("hashtags_ko") or [],
                    data.get("hashtags_en") or [],
                    json.dumps(data.get("seo_hashtags")) if data.get("seo_hashtags") else None,
                    data.get("thumbnail_url"),
                    data.get("process_status", "PROCESSED"),
                    job_id,
                    data.get("published_at"),
                ),
            )
            article_id: int = cur.fetchone()[0]

    logger.info("아티클 upsert | id=%d url=%s", article_id, source_url)
    return article_id


def get_article_by_url(source_url: str) -> Optional[dict]:
    """source_url 로 아티클을 조회합니다."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM articles WHERE source_url = %s",
                (source_url,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def get_recent_articles(
    limit: int = 20,
    language: Optional[str] = None,
    global_only: bool = False,
) -> list[dict]:
    """최근 아티클 목록 (language / global_priority 필터 지원)."""
    conditions = []
    params: list[Any] = []
    if language:
        conditions.append("language = %s")
        params.append(language)
    if global_only:
        conditions.append("global_priority = true")

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT id, source_url, language,
                       title_ko, title_en,
                       artist_name_ko, artist_name_en,
                       global_priority,
                       hashtags_ko, hashtags_en,
                       thumbnail_url,
                       created_at, updated_at
                FROM   articles
                {where_clause}
                ORDER  BY created_at DESC
                LIMIT  %s
                """,
                [*params, limit],
            )
            return [dict(r) for r in cur.fetchall()]


def get_latest_published_at() -> "Optional[datetime]":
    """
    articles 테이블에서 가장 최근 published_at 을 반환합니다.
    check_latest() 에서 새 기사 감지 기준선으로 사용됩니다.

    Returns:
        timezone-aware datetime 또는 None (테이블이 비어있을 때)
    """
    from datetime import datetime as _dt, timezone as _tz

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(published_at) FROM articles WHERE published_at IS NOT NULL"
            )
            row = cur.fetchone()

    value = row[0] if row else None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=_tz.utc)
    return value


def get_articles_status_by_urls(urls: list) -> dict:
    """
    URL 목록에 대한 process_status 맵을 일괄 조회합니다.
    scrape_batch() 의 상태 기반 중복 체크에 사용됩니다.

    Returns:
        {source_url: process_status} — DB에 없는 URL은 결과에 포함되지 않음
    """
    if not urls:
        return {}

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_url, process_status
                FROM   articles
                WHERE  source_url = ANY(%s)
                """,
                (list(urls),),
            )
            rows = cur.fetchall()

    return {row[0]: row[1] for row in rows}


def upsert_article_image(
    article_id:        int,
    original_url:      str,
    thumbnail_path:    Optional[str] = None,
    is_representative: bool          = False,
    alt_text:          Optional[str] = None,
) -> int:
    """
    article_images 행을 UPSERT 합니다 (original_url 유니크 기준).

    INSERT:
        새 이미지 레코드를 생성합니다.

    ON CONFLICT (original_url) DO UPDATE:
        thumbnail_path    — 새 경로가 있으면 갱신, 없으면 기존 값 유지
        is_representative — 항상 최신값으로 덮어씁니다
        alt_text          — 새 텍스트가 있으면 갱신, 없으면 기존 값 유지
        updated_at        — 자동 갱신

    Args:
        article_id:        소속 articles.id
        original_url:      원본 이미지 URL (UNIQUE 키)
        thumbnail_path:    생성된 썸네일 로컬 경로
                           예) "static/thumbnails/42_3a8f.webp"
        is_representative: True 면 기사 대표 이미지 (og:image 등)
        alt_text:          HTML alt 속성 또는 AI 생성 대체 텍스트

    Returns:
        article_images.id (int)
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO article_images
                    (article_id, original_url, thumbnail_path,
                     is_representative, alt_text)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (original_url) DO UPDATE SET
                    thumbnail_path    = COALESCE(
                                            EXCLUDED.thumbnail_path,
                                            article_images.thumbnail_path
                                        ),
                    is_representative = EXCLUDED.is_representative,
                    alt_text          = COALESCE(
                                            EXCLUDED.alt_text,
                                            article_images.alt_text
                                        ),
                    updated_at        = NOW()
                RETURNING id
                """,
                (
                    article_id,
                    original_url,
                    thumbnail_path,
                    is_representative,
                    alt_text,
                ),
            )
            img_id: int = cur.fetchone()[0]

    logger.debug(
        "article_image upsert | id=%d article_id=%d url=%.60s",
        img_id, article_id, original_url,
    )
    return img_id
