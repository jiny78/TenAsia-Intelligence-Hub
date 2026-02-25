"""Phase 4-B: VERIFIED status + Glossary Auto-Provisioned columns

Revision ID: 0009
Revises:     0008
Create Date: 2026-02-25

변경 사항:
    1. process_status_enum 에 'VERIFIED' 값 추가
       - 신뢰도 ≥ 0.95 인 기사를 운영자 확인 없이 자동 승인하는 새 상태
    2. glossary 테이블에 Phase 4-B Smart Glossary Auto-Enroll 컬럼 추가
       - is_auto_provisioned BOOLEAN DEFAULT FALSE
       - source_article_id   INTEGER FK → articles.id (SET NULL)
       - idx_glossary_auto_provisioned 부분 인덱스 (관리자 검토 큐)
"""

from alembic import op
import sqlalchemy as sa

# Alembic 메타데이터
revision        = "0009"
down_revision   = "0008"
branch_labels   = None
depends_on      = None


# ─────────────────────────────────────────────────────────────
# Upgrade
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. process_status_enum 에 VERIFIED 추가 ────────────────
    # PostgreSQL 에서 ENUM 값을 추가할 때는 ALTER TYPE ... ADD VALUE 를 사용합니다.
    # IF NOT EXISTS 는 PostgreSQL 9.6+ 에서 지원됩니다.
    op.execute("""
        ALTER TYPE process_status_enum
        ADD VALUE IF NOT EXISTS 'VERIFIED'
        AFTER 'PROCESSED'
    """)

    # ── 2. glossary.is_auto_provisioned 컬럼 추가 ───────────────
    op.execute("""
        ALTER TABLE glossary
        ADD COLUMN IF NOT EXISTS is_auto_provisioned BOOLEAN
            NOT NULL DEFAULT FALSE
    """)

    op.execute("""
        COMMENT ON COLUMN glossary.is_auto_provisioned IS
        'True = Phase 4-B Smart Glossary Auto-Enroll 로 자동 등록된 용어. '
        '사람이 검토 후 False 로 전환.'
    """)

    # ── 3. glossary.source_article_id 컬럼 추가 ─────────────────
    op.execute("""
        ALTER TABLE glossary
        ADD COLUMN IF NOT EXISTS source_article_id INTEGER
            REFERENCES articles(id) ON DELETE SET NULL
    """)

    op.execute("""
        COMMENT ON COLUMN glossary.source_article_id IS
        '최초 등록 근거 기사 (Auto-Provisioned 시 해당 기사 ID)'
    """)

    # ── 4. Auto-Provisioned 관리자 검토 큐 인덱스 ───────────────
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_glossary_auto_provisioned
        ON glossary (created_at)
        WHERE is_auto_provisioned = true
    """)

    op.execute("""
        COMMENT ON INDEX idx_glossary_auto_provisioned IS
        'Auto-Provisioned 용어 관리자 검토 큐 조회용 (Phase 4-B)'
    """)


# ─────────────────────────────────────────────────────────────
# Downgrade
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # ── 4. 인덱스 삭제 ────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_glossary_auto_provisioned")

    # ── 3. source_article_id 컬럼 삭제 ──────────────────────
    op.execute("""
        ALTER TABLE glossary
        DROP COLUMN IF EXISTS source_article_id
    """)

    # ── 2. is_auto_provisioned 컬럼 삭제 ──────────────────────
    op.execute("""
        ALTER TABLE glossary
        DROP COLUMN IF EXISTS is_auto_provisioned
    """)

    # ── 1. VERIFIED 값 제거 ────────────────────────────────────
    # PostgreSQL 은 ENUM 값 삭제를 직접 지원하지 않습니다.
    # 다운그레이드가 필요한 경우 아래 수동 절차를 따르세요:
    #
    #   1. VERIFIED 상태 기사를 PROCESSED 로 되돌리기:
    #      UPDATE articles SET process_status = 'PROCESSED'
    #      WHERE  process_status = 'VERIFIED';
    #
    #   2. 새 ENUM 타입 생성 (VERIFIED 제외):
    #      CREATE TYPE process_status_enum_new AS ENUM
    #          ('PENDING','SCRAPED','PROCESSED','ERROR','MANUAL_REVIEW');
    #
    #   3. 컬럼 타입 교체:
    #      ALTER TABLE articles
    #          ALTER COLUMN process_status
    #          TYPE process_status_enum_new
    #          USING process_status::text::process_status_enum_new;
    #
    #   4. 구 ENUM 삭제 및 이름 변경:
    #      DROP TYPE process_status_enum;
    #      ALTER TYPE process_status_enum_new RENAME TO process_status_enum;
    #
    # 자동 롤백 대신 주석으로만 안내합니다 (데이터 안전 우선).
    pass
