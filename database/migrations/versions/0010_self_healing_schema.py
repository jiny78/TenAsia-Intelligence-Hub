"""Phase 2-D: Self-Healing Schema — AutoResolutionLog + ConflictFlag

Revision ID: 0010
Revises:     0009
Create Date: 2026-02-25

변경 사항:
    1. artists / groups 테이블에 Self-Healing 메타 컬럼 추가
       - last_verified_at         TIMESTAMPTZ  — AI 마지막 재검증 시각
       - data_reliability_score   FLOAT        — 누적 데이터 신뢰도 점수 (0.0~1.0)

    2. 신규 PostgreSQL ENUM 타입 생성
       - auto_resolution_type_enum : FILL, RECONCILE, ENROLL
       - conflict_status_enum      : OPEN, RESOLVED, DISMISSED

    3. auto_resolution_logs 테이블 생성
       - AI 자율 결정(채우기·모순해결·용어등록) 감사 로그 (append-only)
       - Phase 5-B Auto-Resolution Feed 원천 데이터

    4. conflict_flags 테이블 생성
       - AI 자율 해결 불가 모순 안전망 (운영자 검토 필요)
       - Phase 5-B ConflictFlag 목록 원천 데이터
"""

from alembic import op
import sqlalchemy as sa

revision        = "0010"
down_revision   = "0009"
branch_labels   = None
depends_on      = None


# ─────────────────────────────────────────────────────────────
# Upgrade
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. artists 테이블: Self-Healing 메타 컬럼 추가 ─────────
    op.execute("""
        ALTER TABLE artists
        ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS data_reliability_score FLOAT
            CHECK (data_reliability_score IS NULL
                   OR data_reliability_score BETWEEN 0.0 AND 1.0)
    """)

    op.execute("""
        COMMENT ON COLUMN artists.last_verified_at IS
        '[Phase 2-D] AI 시스템이 이 아티스트 데이터를 마지막으로 재검증한 시점'
    """)
    op.execute("""
        COMMENT ON COLUMN artists.data_reliability_score IS
        '[Phase 2-D] 누적 데이터 신뢰도 점수 (0.0~1.0). 여러 기사에서 교차검증된 정도'
    """)

    # ── 2. groups 테이블: Self-Healing 메타 컬럼 추가 ──────────
    op.execute("""
        ALTER TABLE groups
        ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS data_reliability_score FLOAT
            CHECK (data_reliability_score IS NULL
                   OR data_reliability_score BETWEEN 0.0 AND 1.0)
    """)

    op.execute("""
        COMMENT ON COLUMN groups.last_verified_at IS
        '[Phase 2-D] AI 시스템이 이 그룹 데이터를 마지막으로 재검증한 시점'
    """)
    op.execute("""
        COMMENT ON COLUMN groups.data_reliability_score IS
        '[Phase 2-D] 누적 데이터 신뢰도 점수 (0.0~1.0)'
    """)

    # ── 3. auto_resolution_type_enum ENUM 생성 ─────────────────
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE auto_resolution_type_enum
                AS ENUM ('FILL', 'RECONCILE', 'ENROLL');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # ── 4. conflict_status_enum ENUM 생성 ──────────────────────
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE conflict_status_enum
                AS ENUM ('OPEN', 'RESOLVED', 'DISMISSED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # ── 5. auto_resolution_logs 테이블 생성 ────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS auto_resolution_logs (
            id                 BIGSERIAL    PRIMARY KEY,
            article_id         INTEGER      REFERENCES articles(id) ON DELETE SET NULL,
            entity_type        entity_type_enum          NOT NULL,
            entity_id          INTEGER                   NOT NULL,
            field_name         VARCHAR(100)              NOT NULL,
            old_value_json     JSONB,
            new_value_json     JSONB,
            resolution_type    auto_resolution_type_enum NOT NULL,
            gemini_reasoning   TEXT,
            gemini_confidence  FLOAT        CHECK (gemini_confidence IS NULL
                                                   OR gemini_confidence BETWEEN 0.0 AND 1.0),
            source_reliability FLOAT        NOT NULL DEFAULT 0.0
                               CHECK (source_reliability BETWEEN 0.0 AND 1.0),
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        COMMENT ON TABLE auto_resolution_logs IS
        '[Phase 2-D] AI 자율 결정 감사 로그 (append-only). '
        'Phase 5-B Auto-Resolution Feed 원천 데이터.'
    """)

    # auto_resolution_logs 인덱스
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_arl_article_id
        ON auto_resolution_logs (article_id)
        WHERE article_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_arl_entity
        ON auto_resolution_logs (entity_type, entity_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_arl_created_at
        ON auto_resolution_logs (created_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_arl_type_date
        ON auto_resolution_logs (resolution_type, created_at)
    """)

    # ── 6. conflict_flags 테이블 생성 ──────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS conflict_flags (
            id                     SERIAL       PRIMARY KEY,
            article_id             INTEGER      REFERENCES articles(id) ON DELETE SET NULL,
            entity_type            entity_type_enum   NOT NULL,
            entity_id              INTEGER            NOT NULL,
            field_name             VARCHAR(100)       NOT NULL,
            existing_value_json    JSONB,
            conflicting_value_json JSONB,
            conflict_reason        TEXT,
            conflict_score         FLOAT        NOT NULL DEFAULT 0.5
                                   CHECK (conflict_score BETWEEN 0.0 AND 1.0),
            status                 conflict_status_enum NOT NULL DEFAULT 'OPEN',
            resolved_by            VARCHAR(100),
            resolved_at            TIMESTAMPTZ,
            created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        COMMENT ON TABLE conflict_flags IS
        '[Phase 2-D] AI 자율 해결 불가 모순 안전망. '
        'Phase 5-B ConflictFlag 목록 원천 데이터. '
        '운영자가 RESOLVED 또는 DISMISSED 로 처리.'
    """)

    # conflict_flags 인덱스
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_cf_status_date
        ON conflict_flags (status, created_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_cf_entity
        ON conflict_flags (entity_type, entity_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_cf_article_id
        ON conflict_flags (article_id)
        WHERE article_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_cf_open
        ON conflict_flags (created_at)
        WHERE status = 'OPEN'
    """)


# ─────────────────────────────────────────────────────────────
# Downgrade
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # ── 6. conflict_flags 삭제 ─────────────────────────────────
    op.execute("DROP TABLE IF EXISTS conflict_flags")

    # ── 5. auto_resolution_logs 삭제 ──────────────────────────
    op.execute("DROP TABLE IF EXISTS auto_resolution_logs")

    # ── 4. conflict_status_enum 삭제 ──────────────────────────
    op.execute("DROP TYPE IF EXISTS conflict_status_enum")

    # ── 3. auto_resolution_type_enum 삭제 ─────────────────────
    op.execute("DROP TYPE IF EXISTS auto_resolution_type_enum")

    # ── 2. groups Self-Healing 컬럼 삭제 ──────────────────────
    op.execute("""
        ALTER TABLE groups
        DROP COLUMN IF EXISTS last_verified_at,
        DROP COLUMN IF EXISTS data_reliability_score
    """)

    # ── 1. artists Self-Healing 컬럼 삭제 ─────────────────────
    op.execute("""
        ALTER TABLE artists
        DROP COLUMN IF EXISTS last_verified_at,
        DROP COLUMN IF EXISTS data_reliability_score
    """)
