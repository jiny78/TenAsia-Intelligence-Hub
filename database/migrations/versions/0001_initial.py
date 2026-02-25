"""초기 스키마 — job_queue + articles (다국어 인덱스 포함)

Revision ID: 0001
Revises:
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # ── job_queue ─────────────────────────────────────────────
    op.create_table(
        "job_queue",
        sa.Column("id",           sa.Integer(),     primary_key=True),
        sa.Column("job_type",     sa.String(50),    nullable=False, server_default="scrape"),
        sa.Column("params",       postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status",       sa.String(20),    nullable=False, server_default="pending"),
        sa.Column("priority",     sa.Integer(),     nullable=False, server_default="5"),
        sa.Column("created_at",   sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("started_at",   sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("worker_id",    sa.String(100)),
        sa.Column("result",       postgresql.JSONB),
        sa.Column("error_msg",    sa.Text()),
        sa.Column("retry_count",  sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries",  sa.Integer(), nullable=False, server_default="3"),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_job_queue_status",
        ),
    )

    # job_queue 인덱스
    op.create_index(
        "idx_jq_pending",
        "job_queue",
        ["status", sa.text("priority DESC"), sa.text("created_at ASC")],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # ── articles ──────────────────────────────────────────────
    op.create_table(
        "articles",
        sa.Column("id",             sa.Integer(),   primary_key=True),
        sa.Column("source_url",     sa.Text(),      nullable=False, unique=True),
        sa.Column("language",       sa.String(5),   nullable=False, server_default="kr"),

        # 제목 (다국어)
        sa.Column("title_ko",       sa.Text()),
        sa.Column("title_en",       sa.Text()),

        # 본문 요약
        sa.Column("body_ko",        sa.Text()),
        sa.Column("body_en",        sa.Text()),

        # SNS 캡션
        sa.Column("summary_ko",     sa.Text()),
        sa.Column("summary_en",     sa.Text()),

        # 아티스트
        sa.Column("artist_name_ko", sa.String(200)),
        sa.Column("artist_name_en", sa.String(200)),
        sa.Column("global_priority",sa.Boolean(), nullable=False, server_default="false"),

        # SEO 해시태그
        sa.Column("hashtags_ko",    postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("hashtags_en",    postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),

        # 미디어
        sa.Column("thumbnail_url",  sa.Text()),

        # FK
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("job_queue.id", ondelete="SET NULL"),
        ),

        # 시간
        sa.Column("published_at",   sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at",     sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at",     sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),

        sa.CheckConstraint(
            "language IN ('kr','en','jp')",
            name="ck_articles_language",
        ),
    )

    # updated_at 자동 갱신 트리거
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_set_updated_at()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER set_updated_at_articles
            BEFORE UPDATE ON articles
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ── articles 인덱스 ───────────────────────────────────────

    # 1. 전문 검색 (GIN FTS) — _ko / _en 각각
    op.execute("""
        CREATE INDEX idx_articles_fts_ko ON articles
        USING GIN (
            to_tsvector('simple',
                coalesce(title_ko,'') || ' ' ||
                coalesce(body_ko,'')  || ' ' ||
                coalesce(summary_ko,'')
            )
        )
    """)
    op.execute("""
        CREATE INDEX idx_articles_fts_en ON articles
        USING GIN (
            to_tsvector('english',
                coalesce(title_en,'')   || ' ' ||
                coalesce(body_en,'')    || ' ' ||
                coalesce(summary_en,'')
            )
        )
    """)

    # 2. 트라이그램 (GIN trgm) — _ko / _en 각 컬럼
    for col in ("title_ko", "title_en", "body_ko", "body_en",
                "artist_name_ko", "artist_name_en"):
        op.execute(
            f"CREATE INDEX idx_articles_trgm_{col} "
            f"ON articles USING GIN ({col} gin_trgm_ops)"
        )

    # 3. SEO 해시태그 배열 (GIN) — _ko / _en 각각
    op.execute("CREATE INDEX idx_articles_hashtags_ko ON articles USING GIN (hashtags_ko)")
    op.execute("CREATE INDEX idx_articles_hashtags_en ON articles USING GIN (hashtags_en)")

    # 4. B-tree 필터 인덱스
    op.execute("""
        CREATE INDEX idx_articles_global_priority ON articles (global_priority, created_at DESC)
        WHERE global_priority = true
    """)
    op.execute("CREATE INDEX idx_articles_language_date ON articles (language, created_at DESC)")
    op.execute("""
        CREATE INDEX idx_articles_artist_en_date ON articles (artist_name_en, created_at DESC)
        WHERE artist_name_en IS NOT NULL
    """)


def downgrade() -> None:
    # 인덱스 및 트리거는 테이블 DROP 시 자동 삭제
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_articles ON articles")
    op.execute("DROP FUNCTION IF EXISTS trg_set_updated_at")
    op.drop_table("articles")
    op.drop_table("job_queue")
