"""articles: sentiment 컬럼 추가

Revision ID: 0011
Revises:     0010
Create Date: 2026-02-28

변경 사항:
    1. articles 테이블에 sentiment 컬럼 추가
       - 값: POSITIVE / NEGATIVE / NEUTRAL / NULL(미처리)
       - Gemini AI가 기사 처리 시 자동 분류
"""

from alembic import op

revision      = "0011"
down_revision = "0010"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE articles
        ADD COLUMN IF NOT EXISTS sentiment VARCHAR(10)
            CHECK (sentiment IS NULL
                   OR sentiment IN ('POSITIVE', 'NEGATIVE', 'NEUTRAL'))
    """)
    op.execute("""
        COMMENT ON COLUMN articles.sentiment IS
        'Gemini AI 감성 분류: POSITIVE(긍정)/NEGATIVE(부정)/NEUTRAL(중립)/NULL(미처리)'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS sentiment")
