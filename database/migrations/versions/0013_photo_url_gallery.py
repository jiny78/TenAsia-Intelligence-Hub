"""artists/groups: photo_url 컬럼 추가 + gallery_photos 테이블 신규 생성

변경 요약:
    artists 테이블:
        + photo_url TEXT NULL  — 수동 업로드된 프로필 사진 S3 URL

    groups 테이블:
        + photo_url TEXT NULL  — 수동 업로드된 그룹 썸네일 S3 URL

    신규 테이블: gallery_photos
        id          SERIAL PK
        s3_url      TEXT NOT NULL        S3 업로드된 이미지 URL
        title       TEXT NULL            선택적 제목
        article_id  INTEGER NULL FK      연결된 기사 (SET NULL on delete)
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Revision ID: 0013
Revises:     0012
Create Date: 2026-03-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 1. artists.photo_url 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.add_column(
        "artists",
        sa.Column(
            "photo_url",
            sa.Text(),
            nullable=True,
            comment="수동 업로드된 프로필 사진 S3 URL. 기사 썸네일보다 우선 표시",
        ),
    )

    # ══════════════════════════════════════════════════════════
    # 2. groups.photo_url 컬럼 추가
    # ══════════════════════════════════════════════════════════
    op.add_column(
        "groups",
        sa.Column(
            "photo_url",
            sa.Text(),
            nullable=True,
            comment="수동 업로드된 그룹 썸네일 S3 URL. 기사 썸네일보다 우선 표시",
        ),
    )

    # ══════════════════════════════════════════════════════════
    # 3. gallery_photos 테이블 생성 (독립 갤러리 이미지)
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "gallery_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "s3_url",
            sa.Text(),
            nullable=False,
            comment="S3에 업로드된 이미지 퍼블릭 URL",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=True,
            comment="선택적 이미지 제목",
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
            comment="연결된 기사 ID (선택적). 기사 삭제 시 SET NULL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # gallery_photos 인덱스
    op.create_index("idx_gallery_photos_created_at", "gallery_photos", ["created_at"])
    op.create_index("idx_gallery_photos_article_id", "gallery_photos", ["article_id"])


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    op.drop_index("idx_gallery_photos_article_id", table_name="gallery_photos")
    op.drop_index("idx_gallery_photos_created_at", table_name="gallery_photos")
    op.drop_table("gallery_photos")

    op.drop_column("groups", "photo_url")
    op.drop_column("artists", "photo_url")
