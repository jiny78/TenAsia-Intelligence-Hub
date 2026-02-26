"""FTS search_vector 컬럼, MANUAL_REVIEW 상태, 아티스트 트라이그램 인덱스 강화

변경 요약:

  1. ProcessStatus ENUM 확장
       ALTER TYPE process_status_enum ADD VALUE 'MANUAL_REVIEW'
       용도: AI 신뢰도가 낮은 아티클을 별도 검수 큐로 분리

  2. Articles — search_vector TSVECTOR 컬럼 추가
       다국어 가중 전문 검색 벡터 (트리거 자동 갱신)
       가중치 체계:
           A: title_ko, title_en     (제목 — 최우선)
           B: summary_ko, summary_en (요약 — 중간)
           C: content_ko             (본문 — 낮음, 노이즈 제어)
       트리거: BEFORE INSERT OR UPDATE OF 관련 컬럼
       기존 행 일괄 갱신 (UPDATE ... WHERE search_vector IS NULL)
       인덱스: idx_articles_search_vector GIN

  3. Articles — MANUAL_REVIEW 검수 큐 인덱스 추가
       idx_articles_manual_review: created_at 부분 인덱스
       WHERE process_status = 'MANUAL_REVIEW'

  4. Artists — Trigram 인덱스 명시적 확보 (IF NOT EXISTS — 멱등)
       0002 마이그레이션에서 이미 생성됐지만, 실수로 누락된 환경 대비
       idx_artists_trgm_name_ko: GIN gin_trgm_ops (오타·부분 일치)
       idx_artists_trgm_name_en: GIN gin_trgm_ops (영어명 부분 일치)

  주의:
       ALTER TYPE ADD VALUE 는 PostgreSQL 12 이상에서 트랜잭션 내 실행 가능
       PostgreSQL 9.3~11 은 autocommit 블록 필요 (이 프로젝트는 PG 14+ 가정)

Revision ID: 0006
Revises: 0005
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 1. ProcessStatus ENUM 에 MANUAL_REVIEW 추가
    #    PostgreSQL 제약: ADD VALUE로 추가한 값은 같은 트랜잭션에서 사용 불가
    #    → COMMIT/BEGIN 으로 별도 트랜잭션에서 실행 후 즉시 커밋
    # ══════════════════════════════════════════════════════════
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE process_status_enum ADD VALUE IF NOT EXISTS 'MANUAL_REVIEW'"))
    op.execute(sa.text("BEGIN"))

    # ══════════════════════════════════════════════════════════
    # 2. articles — search_vector TSVECTOR 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE articles
            ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
    """)

    # ══════════════════════════════════════════════════════════
    # 3. search_vector 자동 갱신 트리거 함수 생성
    #    BEFORE INSERT OR UPDATE OF 제목/본문/요약 컬럼
    #    → 관련 없는 컬럼 변경(updated_at 등) 시 불필요한 재계산 방지
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_update_article_search_vector()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector :=
                -- 한국어 제목 (A: 최고 가중치)
                setweight(
                    to_tsvector('simple', coalesce(NEW.title_ko, '')),
                    'A'
                ) ||
                -- 영어 제목 (A: 최고 가중치)
                setweight(
                    to_tsvector('english', coalesce(NEW.title_en, '')),
                    'A'
                ) ||
                -- 한국어 요약 (B: 중간 가중치)
                setweight(
                    to_tsvector('simple', coalesce(NEW.summary_ko, '')),
                    'B'
                ) ||
                -- 영어 요약 (B: 중간 가중치)
                setweight(
                    to_tsvector('english', coalesce(NEW.summary_en, '')),
                    'B'
                ) ||
                -- 한국어 원문 (C: 낮은 가중치 — 노이즈 제어)
                setweight(
                    to_tsvector('simple', coalesce(NEW.content_ko, '')),
                    'C'
                );
            RETURN NEW;
        END;
        $$;
    """)

    # 트리거 생성 — 제목/본문/요약 변경 시에만 발화
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'update_article_search_vector'
            ) THEN
                CREATE TRIGGER update_article_search_vector
                    BEFORE INSERT OR UPDATE OF
                        title_ko, title_en,
                        summary_ko, summary_en,
                        content_ko
                    ON articles
                    FOR EACH ROW
                    EXECUTE FUNCTION trg_update_article_search_vector();
            END IF;
        END $$;
    """)

    # ══════════════════════════════════════════════════════════
    # 4. 기존 행 search_vector 일괄 갱신
    #    마이그레이션 시점의 데이터를 즉시 검색 가능한 상태로 만듭니다.
    #    이후 INSERT / UPDATE 는 트리거가 자동 처리합니다.
    # ══════════════════════════════════════════════════════════
    op.execute("""
        UPDATE articles
        SET search_vector =
            setweight(to_tsvector('simple',  coalesce(title_ko,   '')), 'A') ||
            setweight(to_tsvector('english', coalesce(title_en,   '')), 'A') ||
            setweight(to_tsvector('simple',  coalesce(summary_ko, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(summary_en, '')), 'B') ||
            setweight(to_tsvector('simple',  coalesce(content_ko, '')), 'C')
        WHERE search_vector IS NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 5. search_vector GIN 인덱스 생성
    #    @@ 연산자, ts_rank(), ts_headline() 가속
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_search_vector
            ON articles USING GIN (search_vector)
    """)

    # ══════════════════════════════════════════════════════════
    # 6. MANUAL_REVIEW 검수 큐 부분 인덱스
    #    process_status = 'MANUAL_REVIEW' 인 행만 인덱스
    #    → 검수 큐 UI에서 created_at DESC 정렬 시 최적화
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_manual_review
            ON articles (created_at DESC)
            WHERE process_status = 'MANUAL_REVIEW'
    """)

    # ══════════════════════════════════════════════════════════
    # 7. artists Trigram 인덱스 명시적 확보 (IF NOT EXISTS — 멱등)
    #    0002 마이그레이션에서 이미 생성됐으나, 누락된 환경 대비
    #    pg_trgm 확장: 0001_initial 또는 scraper/schema.py 에서 이미 활성화
    # ══════════════════════════════════════════════════════════

    # name_ko 트라이그램 — 한국어 이름 오타·부분 일치 검색
    # 활용: WHERE name_ko LIKE '%방탄%' OR similarity(name_ko, '방탄소년단') > 0.3
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_artists_trgm_name_ko
            ON artists USING GIN (name_ko gin_trgm_ops)
    """)

    # name_en 트라이그램 — 영어 이름 오타·부분 일치 검색
    # 활용: WHERE name_en ILIKE '%bts%' OR similarity(name_en, 'BTS') > 0.3
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_artists_trgm_name_en
            ON artists USING GIN (name_en gin_trgm_ops)
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # ── 7. artist trigram 인덱스 (0002에서 생성됐다면 제거하지 않음)
    #    본 마이그레이션이 IF NOT EXISTS 로 멱등하게 확보한 인덱스이므로
    #    downgrade 시 DROP 하지 않습니다 (0002 downgrade에서 처리).
    pass

    # ── 6. MANUAL_REVIEW 검수 큐 인덱스 ─────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_articles_manual_review")

    # ── 5. search_vector GIN 인덱스 ─────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_articles_search_vector")

    # ── 4. search_vector 트리거 및 함수 ──────────────────────────
    op.execute(
        "DROP TRIGGER IF EXISTS update_article_search_vector ON articles"
    )
    op.execute("DROP FUNCTION IF EXISTS trg_update_article_search_vector()")

    # ── 3. search_vector 컬럼 ────────────────────────────────────
    op.drop_column("articles", "search_vector")

    # ── 1. MANUAL_REVIEW ENUM 값 제거 ────────────────────────────
    #    PostgreSQL 은 ENUM 값을 직접 삭제할 수 없으므로
    #    컬럼 타입을 임시 VARCHAR로 변경 후 ENUM 재생성합니다.
    #
    #    Step 1: MANUAL_REVIEW 행을 안전한 상태(ERROR)로 변경
    op.execute("""
        UPDATE articles
        SET process_status = 'ERROR'
        WHERE process_status = 'MANUAL_REVIEW'
    """)

    #    Step 2: 컬럼 타입을 VARCHAR로 임시 변경
    op.execute("""
        ALTER TABLE articles
            ALTER COLUMN process_status TYPE VARCHAR(20)
    """)

    #    Step 3: 기존 ENUM 타입 삭제
    op.execute("DROP TYPE IF EXISTS process_status_enum")

    #    Step 4: MANUAL_REVIEW 없이 ENUM 재생성
    op.execute("""
        CREATE TYPE process_status_enum
            AS ENUM ('PENDING', 'SCRAPED', 'PROCESSED', 'ERROR')
    """)

    #    Step 5: 컬럼 타입 ENUM 으로 복원
    op.execute("""
        ALTER TABLE articles
            ALTER COLUMN process_status
            TYPE process_status_enum
            USING process_status::process_status_enum
    """)
