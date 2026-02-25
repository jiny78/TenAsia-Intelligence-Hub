"""다국어 스키마 확장 — articles / artists 컬럼 재편 및 영어 검색 인덱스 강화

변경 요약:
    Articles:
        RENAME  body_ko    → content_ko   (원문(한국어) 전체 본문으로 명칭 명확화)
        DROP    body_en                   (영어 전체 번역 미제공, 비용 효율)
        ADD     author     VARCHAR(200)   (이미 0002에서 추가됨, 멱등 처리)

    Artists:
        ADD     bio_ko     TEXT           (아티스트 소개글 한국어)
        ADD     bio_en     TEXT           (아티스트 소개글 영어)

인덱스 변경:
    DROP & RECREATE (컬럼명 변경으로 인한 재빌드):
        idx_articles_fts_ko      → content_ko 반영 (title_ko+content_ko+summary_ko)
        idx_articles_fts_en      → body_en 제거    (title_en+summary_en 만)
        idx_articles_trgm_body_ko → idx_articles_trgm_content_ko (rename)

    DROP (컬럼 삭제):
        idx_articles_trgm_body_en

    NEW (사용자 요청 — 영어 검색 성능 확보):
        idx_articles_trgm_summary_en  ← title_en FTS 외 summary_en 부분 매칭
        idx_articles_trgm_summary_ko  ← 일관성 확보

    NEW (artists bio 검색):
        idx_artists_trgm_bio_ko
        idx_artists_trgm_bio_en

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 1. GIN 인덱스 선제 삭제
    #    body_ko / body_en 을 참조하는 인덱스는 컬럼 변경 전에 제거해야 합니다.
    # ══════════════════════════════════════════════════════════
    op.execute("DROP INDEX IF EXISTS idx_articles_fts_ko")
    op.execute("DROP INDEX IF EXISTS idx_articles_fts_en")
    op.execute("DROP INDEX IF EXISTS idx_articles_trgm_body_ko")
    op.execute("DROP INDEX IF EXISTS idx_articles_trgm_body_en")

    # ══════════════════════════════════════════════════════════
    # 2. articles — body_ko → content_ko (컬럼명 명확화)
    # ══════════════════════════════════════════════════════════
    op.execute("ALTER TABLE articles RENAME COLUMN body_ko TO content_ko")

    # ══════════════════════════════════════════════════════════
    # 3. articles — body_en 제거 (영어 전체 번역 미제공 정책)
    #
    #    영어 콘텐츠 전략:
    #      - title_en    : 영어 제목 (AI 추출, global_priority=true 시 필수)
    #      - summary_en  : 영어 요약 (SNS 캡션, 최대 500자)
    #      - hashtags_en : 영어 SEO 해시태그
    #      ※ 전체 본문 번역은 Gemini API 비용 절감을 위해 제외
    # ══════════════════════════════════════════════════════════
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS body_en")

    # ══════════════════════════════════════════════════════════
    # 4. artists — 소개글 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.add_column(
        "artists",
        sa.Column(
            "bio_ko", sa.Text(), nullable=True,
            comment="아티스트 소개글 (한국어)",
        ),
    )
    op.add_column(
        "artists",
        sa.Column(
            "bio_en", sa.Text(), nullable=True,
            comment="아티스트 소개글 (영어)",
        ),
    )

    # ══════════════════════════════════════════════════════════
    # 5. FTS 인덱스 재생성
    # ══════════════════════════════════════════════════════════

    # 한국어 FTS: 원문(content_ko) 포함
    op.execute("""
        CREATE INDEX idx_articles_fts_ko ON articles
        USING GIN (
            to_tsvector(
                'simple',
                coalesce(title_ko,   '') || ' ' ||
                coalesce(content_ko, '') || ' ' ||
                coalesce(summary_ko, '')
            )
        )
    """)

    # 영어 FTS: title_en + summary_en (body_en 제외)
    op.execute("""
        CREATE INDEX idx_articles_fts_en ON articles
        USING GIN (
            to_tsvector(
                'english',
                coalesce(title_en,   '') || ' ' ||
                coalesce(summary_en, '')
            )
        )
    """)

    # ══════════════════════════════════════════════════════════
    # 6. 트라이그램 인덱스 재생성 (content_ko) + 신규 (summary_*)
    # ══════════════════════════════════════════════════════════

    # content_ko 트라이그램 (body_ko 대체)
    op.execute("""
        CREATE INDEX idx_articles_trgm_content_ko
            ON articles USING GIN (content_ko gin_trgm_ops)
    """)

    # summary_ko 트라이그램 (신규 — 요약 부분 매칭)
    op.execute("""
        CREATE INDEX idx_articles_trgm_summary_ko
            ON articles USING GIN (summary_ko gin_trgm_ops)
    """)

    # summary_en 트라이그램 (신규 — 영어 요약 검색 성능 확보, 사용자 요청)
    op.execute("""
        CREATE INDEX idx_articles_trgm_summary_en
            ON articles USING GIN (summary_en gin_trgm_ops)
    """)

    # ══════════════════════════════════════════════════════════
    # 7. artists bio 트라이그램 인덱스 (신규)
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE INDEX idx_artists_trgm_bio_ko
            ON artists USING GIN (bio_ko gin_trgm_ops)
    """)
    op.execute("""
        CREATE INDEX idx_artists_trgm_bio_en
            ON artists USING GIN (bio_en gin_trgm_ops)
    """)

    # ══════════════════════════════════════════════════════════
    # 8. 뷰 갱신 — v_article_processing_stats
    #    body_ko 참조 없으므로 내용 변경 불필요,
    #    다만 REPLACE 로 최신 상태 명시
    # ══════════════════════════════════════════════════════════
    op.execute("""
        CREATE OR REPLACE VIEW v_article_processing_stats AS
        SELECT
            process_status,
            COUNT(*)                                            AS article_count,
            COUNT(*) FILTER (WHERE global_priority = true)     AS priority_count,
            MIN(created_at)                                     AS oldest,
            MAX(updated_at)                                     AS latest_updated
        FROM articles
        GROUP BY process_status
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # artists bio 트라이그램 제거
    op.execute("DROP INDEX IF EXISTS idx_artists_trgm_bio_en")
    op.execute("DROP INDEX IF EXISTS idx_artists_trgm_bio_ko")

    # 신규 summary 트라이그램 제거
    op.execute("DROP INDEX IF EXISTS idx_articles_trgm_summary_en")
    op.execute("DROP INDEX IF EXISTS idx_articles_trgm_summary_ko")
    op.execute("DROP INDEX IF EXISTS idx_articles_trgm_content_ko")

    # FTS 인덱스 제거 (원복 전 삭제)
    op.execute("DROP INDEX IF EXISTS idx_articles_fts_ko")
    op.execute("DROP INDEX IF EXISTS idx_articles_fts_en")

    # artists bio 컬럼 제거
    op.drop_column("artists", "bio_en")
    op.drop_column("artists", "bio_ko")

    # articles: content_ko → body_ko 복원
    op.execute("ALTER TABLE articles RENAME COLUMN content_ko TO body_ko")

    # articles: body_en 복원 (빈 컬럼, 데이터는 복원 불가)
    op.add_column(
        "articles",
        sa.Column("body_en", sa.Text(), nullable=True),
    )

    # 원래 FTS 인덱스 복원
    op.execute("""
        CREATE INDEX idx_articles_fts_ko ON articles
        USING GIN (
            to_tsvector(
                'simple',
                coalesce(title_ko,'') || ' ' ||
                coalesce(body_ko, '') || ' ' ||
                coalesce(summary_ko,'')
            )
        )
    """)
    op.execute("""
        CREATE INDEX idx_articles_fts_en ON articles
        USING GIN (
            to_tsvector(
                'english',
                coalesce(title_en,'') || ' ' ||
                coalesce(body_en, '') || ' ' ||
                coalesce(summary_en,'')
            )
        )
    """)

    # 원래 트라이그램 인덱스 복원
    op.execute("""
        CREATE INDEX idx_articles_trgm_body_ko
            ON articles USING GIN (body_ko gin_trgm_ops)
    """)
    op.execute("""
        CREATE INDEX idx_articles_trgm_body_en
            ON articles USING GIN (body_en gin_trgm_ops)
    """)
