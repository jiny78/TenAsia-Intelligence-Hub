"""아티클 이미지 관리 테이블(article_images) 추가 — articles와 1:N 관계

변경 요약:
    신규 테이블: article_images
        id                SERIAL PK
        article_id        INTEGER NOT NULL  FK → articles.id  ON DELETE CASCADE
        original_url      TEXT    NOT NULL  UNIQUE (중복 이미지 처리 방지)
        thumbnail_path    TEXT    NULL      S3 썸네일 경로 (업로드 전 NULL)
        is_representative BOOLEAN NOT NULL  DEFAULT false (기사 대표 이미지 여부)
        alt_text          TEXT    NULL      웹 접근성·SEO 대체 텍스트
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()

    인덱스:
        uq_article_images_url     — original_url UNIQUE (중복 방지)
        idx_ai_article_id         — article_id B-tree (기사별 전체 이미지 조회)
        idx_ai_representative     — article_id, WHERE is_representative=true
                                    (대표 이미지 단건 조회 최적화)
        idx_ai_pending_upload     — article_id + created_at,
                                    WHERE thumbnail_path IS NULL
                                    (S3 업로드 대기 배치 처리용)

    트리거:
        set_updated_at_article_images — 기존 trg_set_updated_at() 함수 재사용

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 1. article_images 테이블 생성
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "article_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
            comment="소속 아티클 — CASCADE 삭제",
        ),
        sa.Column(
            "original_url",
            sa.Text(),
            nullable=False,
            comment="스크래퍼가 수집한 원본 이미지 URL (UNIQUE)",
        ),
        sa.Column(
            "thumbnail_path",
            sa.Text(),
            nullable=True,
            comment="S3 썸네일 경로. NULL=업로드 전/실패 (예: prod/BTS/1042/7_thumb.webp)",
        ),
        sa.Column(
            "is_representative",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="대표 이미지 여부 — 기사당 1개 권장, Article.thumbnail_url 과 동기화",
        ),
        sa.Column(
            "alt_text",
            sa.Text(),
            nullable=True,
            comment="이미지 대체 텍스트 (웹 접근성·SEO). HTML alt 속성 또는 AI 생성",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # original_url 유니크 — 같은 이미지 URL 의 중복 처리 방지
        sa.UniqueConstraint("original_url", name="uq_article_images_url"),
    )

    # ══════════════════════════════════════════════════════════
    # 2. 인덱스 생성
    # ══════════════════════════════════════════════════════════

    # 기사별 전체 이미지 조회 (기본 1:N 접근 패턴)
    op.create_index("idx_ai_article_id", "article_images", ["article_id"])

    # 대표 이미지 단건 조회 최적화
    # 활용: SELECT * FROM article_images WHERE article_id=? AND is_representative=true
    op.execute("""
        CREATE INDEX idx_ai_representative
            ON article_images (article_id)
            WHERE is_representative = true
    """)

    # S3 업로드 대기 배치 처리 — thumbnail_path IS NULL 인 행만 인덱스
    # 활용: SELECT * FROM article_images WHERE thumbnail_path IS NULL ORDER BY created_at
    op.execute("""
        CREATE INDEX idx_ai_pending_upload
            ON article_images (article_id, created_at)
            WHERE thumbnail_path IS NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 3. updated_at 자동 갱신 트리거
    #    trg_set_updated_at() 함수는 0002_phase2_schema 에서 이미 생성됨
    # ══════════════════════════════════════════════════════════
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'set_updated_at_article_images'
            ) THEN
                CREATE TRIGGER set_updated_at_article_images
                    BEFORE UPDATE ON article_images
                    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
            END IF;
        END $$;
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # 트리거 제거
    op.execute(
        "DROP TRIGGER IF EXISTS set_updated_at_article_images ON article_images"
    )

    # 인덱스 제거 (테이블 DROP 전)
    op.execute("DROP INDEX IF EXISTS idx_ai_pending_upload")
    op.execute("DROP INDEX IF EXISTS idx_ai_representative")
    op.drop_index("idx_ai_article_id", table_name="article_images")

    # 테이블 DROP (UniqueConstraint 포함 모두 제거)
    op.drop_table("article_images")
