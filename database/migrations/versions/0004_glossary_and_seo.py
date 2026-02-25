"""용어집(Glossary) 신규, Artists.global_priority, Articles.seo_hashtags 추가

변경 요약:
    신규 테이블:
        glossary
            id           SERIAL PK
            term_ko      VARCHAR(300) NOT NULL
            term_en      VARCHAR(300) NULL
            category     glossary_category_enum NOT NULL  (ARTIST/AGENCY/EVENT)
            description  TEXT NULL
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            UNIQUE (term_ko, category)

    Artists 테이블 확장:
        ADD global_priority INTEGER NULL
            CHECK (global_priority IS NULL OR global_priority IN (1, 2, 3))
            1 = 최우선 번역 (title_en + summary_en + hashtags_en 전체)
            2 = 요약만 번역 (summary_en)
            3 = 번역 제외

    Articles 테이블 확장:
        ADD seo_hashtags JSONB NULL
            AI 생성 영어 SEO 해시태그 — 생성 모델·신뢰도·카테고리 메타데이터 포함
            예: {"tags": ["BTS","KPOP"], "model": "gemini-2.0-flash", "confidence": 0.95}

신규 인덱스:
    glossary:
        idx_glossary_trgm_ko       — term_ko GIN 트라이그램 (부분 매칭)
        idx_glossary_trgm_en       — term_en GIN 트라이그램 (역방향 검색)
        idx_glossary_category      — category B-tree (0004_models.py __table_args__)
        idx_glossary_term_ko       — term_ko  B-tree (0004_models.py __table_args__)

    artists:
        idx_artists_global_priority — global_priority B-tree, WHERE IS NOT NULL

    articles:
        idx_articles_seo_hashtags   — seo_hashtags GIN (JSONB 키/값 검색)

뷰 갱신:
    v_artist_coverage — global_priority 컬럼 추가 반영

Revision ID: 0004
Revises: 0003
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 1. glossary_category_enum 타입 생성 (멱등)
    # ══════════════════════════════════════════════════════════
    op.execute("""
        DO $$
        BEGIN
            CREATE TYPE glossary_category_enum AS ENUM ('ARTIST', 'AGENCY', 'EVENT');
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END $$;
    """)

    # ══════════════════════════════════════════════════════════
    # 2. glossary 테이블 생성
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "glossary",
        sa.Column("id",          sa.Integer(),     primary_key=True),
        sa.Column("term_ko",     sa.String(300),   nullable=False,
                  comment="한국어 원어 (예: 방탄소년단, 하이브)"),
        sa.Column("term_en",     sa.String(300),   nullable=True,
                  comment="영어 공식 표기 (예: BTS, HYBE)"),
        sa.Column(
            "category",
            sa.Enum("ARTIST", "AGENCY", "EVENT",
                    name="glossary_category_enum",
                    create_type=False),
            nullable=False,
            comment="용어 분류 (ARTIST / AGENCY / EVENT)",
        ),
        sa.Column("description", sa.Text(),        nullable=True,
                  comment="추가 설명 — 동명이인 구분, 데뷔 연도 등"),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at",  sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.UniqueConstraint("term_ko", "category", name="uq_glossary_term_category"),
    )

    # ── updated_at 자동 갱신 트리거 ──────────────────────────
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'set_updated_at_glossary'
            ) THEN
                CREATE TRIGGER set_updated_at_glossary
                    BEFORE UPDATE ON glossary
                    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
            END IF;
        END $$;
    """)

    # ══════════════════════════════════════════════════════════
    # 3. glossary 인덱스 생성
    # ══════════════════════════════════════════════════════════

    # 분류별 일괄 조회 (B-tree — 프롬프트 구성 시 ARTIST 전체 로드 등)
    op.create_index("idx_glossary_category", "glossary", ["category"])

    # 한국어 원어 일치 조회 (B-tree)
    op.create_index("idx_glossary_term_ko", "glossary", ["term_ko"])

    # term_ko 트라이그램 — 부분 매칭 검색 (LIKE '%방탄%')
    op.execute("""
        CREATE INDEX idx_glossary_trgm_ko
            ON glossary USING GIN (term_ko gin_trgm_ops)
    """)

    # term_en 트라이그램 — 영어 원어 역방향 검색 (LIKE '%BTS%')
    op.execute("""
        CREATE INDEX idx_glossary_trgm_en
            ON glossary USING GIN (term_en gin_trgm_ops)
    """)

    # ══════════════════════════════════════════════════════════
    # 4. artists — global_priority 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.add_column(
        "artists",
        sa.Column(
            "global_priority",
            sa.Integer(),
            nullable=True,
            comment="번역 우선순위: 1=전체번역, 2=요약만, 3=번역제외, NULL=미분류",
        ),
    )

    op.create_check_constraint(
        "ck_artists_global_priority",
        "artists",
        "global_priority IS NULL OR global_priority IN (1, 2, 3)",
    )

    # NULL이 아닌 행만 인덱스 (번역 정책 조회용)
    op.execute("""
        CREATE INDEX idx_artists_global_priority
            ON artists (global_priority)
            WHERE global_priority IS NOT NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 5. articles — seo_hashtags 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.add_column(
        "articles",
        sa.Column(
            "seo_hashtags",
            JSONB,
            nullable=True,
            comment=(
                "AI 생성 영어 SEO 해시태그 (메타데이터 포함). "
                "예: {\"tags\":[\"BTS\",\"KPOP\"], \"model\":\"gemini-2.0-flash\", "
                "\"confidence\":0.95, \"generated_at\":\"2026-02-25T00:00:00Z\"}"
            ),
        ),
    )

    # JSONB GIN 인덱스 — @>, ?, ?& 등 JSON 연산자 최적화
    op.execute("""
        CREATE INDEX idx_articles_seo_hashtags
            ON articles USING GIN (seo_hashtags)
            WHERE seo_hashtags IS NOT NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 6. v_artist_coverage 뷰 갱신 — global_priority 반영
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE OR REPLACE VIEW v_artist_coverage AS
        SELECT
            a.id,
            a.name_ko,
            a.name_en,
            a.global_priority                                          AS translation_priority,
            a.is_verified,
            COUNT(DISTINCT ar.id)                                      AS article_count,
            COUNT(DISTINCT ar.id) FILTER (WHERE ar.global_priority)   AS global_article_count,
            MAX(ar.created_at)                                         AS latest_article_at
        FROM artists a
        LEFT JOIN entity_mappings em ON em.entity_id = a.id
        LEFT JOIN articles ar        ON ar.id = em.article_id
        GROUP BY a.id, a.name_ko, a.name_en, a.global_priority, a.is_verified
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # 뷰 원복 (global_priority 컬럼 없는 버전)
    op.execute("""
        CREATE OR REPLACE VIEW v_artist_coverage AS
        SELECT
            a.id,
            a.name_ko,
            a.name_en,
            a.is_verified,
            COUNT(DISTINCT ar.id)                                      AS article_count,
            COUNT(DISTINCT ar.id) FILTER (WHERE ar.global_priority)   AS global_article_count,
            MAX(ar.created_at)                                         AS latest_article_at
        FROM artists a
        LEFT JOIN entity_mappings em ON em.entity_id = a.id
        LEFT JOIN articles ar        ON ar.id = em.article_id
        GROUP BY a.id, a.name_ko, a.name_en, a.is_verified
    """)

    # articles.seo_hashtags 제거
    op.execute("DROP INDEX IF EXISTS idx_articles_seo_hashtags")
    op.drop_column("articles", "seo_hashtags")

    # artists.global_priority 제거
    op.execute("DROP INDEX IF EXISTS idx_artists_global_priority")
    op.drop_constraint("ck_artists_global_priority", "artists", type_="check")
    op.drop_column("artists", "global_priority")

    # glossary 인덱스 + 테이블 제거
    op.execute("DROP INDEX IF EXISTS idx_glossary_trgm_en")
    op.execute("DROP INDEX IF EXISTS idx_glossary_trgm_ko")
    op.drop_index("idx_glossary_term_ko", table_name="glossary")
    op.drop_index("idx_glossary_category", table_name="glossary")
    op.drop_table("glossary")

    # ENUM 타입 제거
    op.execute("DROP TYPE IF EXISTS glossary_category_enum")
