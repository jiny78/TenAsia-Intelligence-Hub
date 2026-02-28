"""artists/groups: enriched_at 컬럼 추가

Revision ID: 0012
Revises:     0011
Create Date: 2026-02-28

변경 사항:
    1. artists 테이블에 enriched_at 컬럼 추가 (TIMESTAMPTZ)
       - Gemini 프로필 보강이 완료된 시점 기록
       - NULL = 아직 한 번도 보강 안 됨 → 보강 대상
       - NOT NULL = 이미 보강됨 → 재보강 스킵
    2. groups 테이블에 동일한 enriched_at 컬럼 추가
"""

from alembic import op

revision      = "0012"
down_revision = "0011"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE artists
        ADD COLUMN IF NOT EXISTS enriched_at TIMESTAMPTZ DEFAULT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN artists.enriched_at IS
        'Gemini 프로필 보강 완료 시각. NULL=미보강(보강 대상), NOT NULL=보강 완료(스킵)'
    """)
    op.execute("""
        ALTER TABLE groups
        ADD COLUMN IF NOT EXISTS enriched_at TIMESTAMPTZ DEFAULT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN groups.enriched_at IS
        'Gemini 프로필 보강 완료 시각. NULL=미보강(보강 대상), NOT NULL=보강 완료(스킵)'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE artists DROP COLUMN IF EXISTS enriched_at")
    op.execute("ALTER TABLE groups  DROP COLUMN IF EXISTS enriched_at")
