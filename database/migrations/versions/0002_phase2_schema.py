"""Phase 2 스키마 — artists, entity_mappings, system_logs 추가 및 articles 컬럼 보강

신규 테이블:
    artists         — 아티스트/그룹 마스터
    entity_mappings — 아티클 ↔ 아티스트 연결 (신뢰도 점수)
    system_logs     — 처리 이력 (append-only, BigInteger PK)

articles 변경:
    + author          VARCHAR(200)        저자
    + process_status  process_status_enum 처리 상태 (기본: PENDING)
    + updated_at      TIMESTAMPTZ         자동 갱신 트리거

신규 PostgreSQL ENUM 타입:
    process_status_enum : PENDING, SCRAPED, PROCESSED, ERROR
    entity_type_enum    : ARTIST, GROUP, EVENT
    log_level_enum      : DEBUG, INFO, WARNING, ERROR
    log_category_enum   : SCRAPE, AI_PROCESS, DB_WRITE, S3_UPLOAD, API_CALL

신규 인덱스:
    artists         : B-tree(name_ko, name_en, agency, is_verified)
                      GIN trgm(name_ko, name_en)
    articles        : B-tree(process_status, global_priority)
    entity_mappings : Partial unique(article_id, entity_type, entity_id)
                      B-tree(article_id, entity_type+entity_id, confidence_score)
    system_logs     : B-tree(created_at, level, category)
                      Partial(article_id IS NOT NULL, job_id IS NOT NULL)

트리거:
    trg_set_updated_at() 함수는 0001_initial 에서 이미 생성됨 → 재사용
    set_updated_at_job_queue    → job_queue.updated_at
    set_updated_at_artists      → artists.updated_at
    set_updated_at_articles_v2  → articles.updated_at (0001 트리거 교체)
    set_updated_at_entity_map   → entity_mappings.updated_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 0. PostgreSQL ENUM 타입 생성
    #    - CREATE TYPE ... IF NOT EXISTS (PG 9.1+ 미지원 → 조건부 실행)
    # ══════════════════════════════════════════════════════════
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE process_status_enum AS ENUM (
                'PENDING', 'SCRAPED', 'PROCESSED', 'ERROR'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE entity_type_enum AS ENUM (
                'ARTIST', 'GROUP', 'EVENT'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE log_level_enum AS ENUM (
                'DEBUG', 'INFO', 'WARNING', 'ERROR'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE log_category_enum AS ENUM (
                'SCRAPE', 'AI_PROCESS', 'DB_WRITE', 'S3_UPLOAD', 'API_CALL'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # ══════════════════════════════════════════════════════════
    # 1. articles — 컬럼 추가
    # ══════════════════════════════════════════════════════════

    # 저자
    op.add_column(
        "articles",
        sa.Column("author", sa.String(200), nullable=True),
    )

    # 처리 상태 (기존 행은 모두 SCRAPED 로 초기화 — 이미 수집된 데이터)
    op.add_column(
        "articles",
        sa.Column(
            "process_status",
            postgresql.ENUM(name="process_status_enum", create_type=False),
            nullable=False,
            server_default="SCRAPED",     # 기존 행: 수집 완료 상태
        ),
    )
    # 이후 server_default 는 PENDING 으로 재설정 (신규 행용)
    op.alter_column(
        "articles",
        "process_status",
        server_default="PENDING",
    )

    # updated_at — 0001 에서 누락된 트리거 대상 컬럼 추가
    # (이미 컬럼이 있으면 SKIP — idempotent 보장)
    op.execute("""
        ALTER TABLE articles
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                NOT NULL DEFAULT NOW()
    """)

    # articles 처리 상태 인덱스
    op.create_index(
        "idx_articles_process_status",
        "articles",
        ["process_status", "created_at"],
    )
    op.create_index(
        "idx_articles_status_priority",
        "articles",
        ["process_status", "global_priority", "created_at"],
    )

    # articles updated_at 트리거 재설정 (0001 에서 생성한 트리거 교체)
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_articles ON articles")
    op.execute("""
        CREATE TRIGGER set_updated_at_articles_v2
            BEFORE UPDATE ON articles
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 2. job_queue — updated_at 컬럼 + 트리거 추가
    # ══════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE job_queue
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                NOT NULL DEFAULT NOW()
    """)
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_job_queue ON job_queue")
    op.execute("""
        CREATE TRIGGER set_updated_at_job_queue
            BEFORE UPDATE ON job_queue
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 3. artists 테이블
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "artists",
        sa.Column("id",            sa.Integer(),   primary_key=True),
        sa.Column("name_ko",       sa.String(200), nullable=False),
        sa.Column("name_en",       sa.String(200), nullable=True),
        sa.Column("debut_date",    sa.Date(),      nullable=True),
        sa.Column("agency",        sa.String(200), nullable=True),
        sa.Column(
            "official_tags",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="팬덤명, SNS 계정, 장르 등 반구조화 메타데이터",
        ),
        sa.Column(
            "is_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="공식 확인된 아티스트 여부",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # artists B-tree 인덱스
    op.create_index("idx_artists_name_ko",     "artists", ["name_ko"])
    op.create_index("idx_artists_name_en",     "artists", ["name_en"])
    op.create_index("idx_artists_agency",      "artists", ["agency"])
    op.create_index("idx_artists_is_verified", "artists", ["is_verified"])

    # artists GIN Trigram 인덱스 (LIKE '%검색어%' 최적화)
    op.execute(
        "CREATE INDEX idx_artists_trgm_name_ko "
        "ON artists USING GIN (name_ko gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_artists_trgm_name_en "
        "ON artists USING GIN (name_en gin_trgm_ops)"
    )

    # artists updated_at 트리거
    op.execute("""
        CREATE TRIGGER set_updated_at_artists
            BEFORE UPDATE ON artists
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 4. entity_mappings 테이블
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "entity_mappings",
        sa.Column("id",         sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entity_type",
            postgresql.ENUM(name="entity_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="SET NULL"),
            nullable=True,
            comment="ARTIST/GROUP → artists.id | EVENT → NULL",
        ),
        sa.Column(
            "confidence_score",
            sa.Float(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "confidence_score BETWEEN 0.0 AND 1.0",
            name="ck_em_confidence",
        ),
    )

    # entity_mappings 인덱스
    op.create_index("idx_em_article_id", "entity_mappings", ["article_id"])
    op.create_index("idx_em_entity",     "entity_mappings", ["entity_type", "entity_id"])
    op.create_index("idx_em_confidence", "entity_mappings", ["confidence_score"])

    # Partial Unique: entity_id NOT NULL 행만 중복 방지
    # (EVENT 타입의 entity_id=NULL 은 중복 허용)
    op.execute("""
        CREATE UNIQUE INDEX uq_entity_mapping
            ON entity_mappings (article_id, entity_type, entity_id)
            WHERE entity_id IS NOT NULL
    """)

    # entity_mappings updated_at 트리거
    op.execute("""
        CREATE TRIGGER set_updated_at_entity_map
            BEFORE UPDATE ON entity_mappings
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 5. system_logs 테이블 (append-only)
    # ══════════════════════════════════════════════════════════
    op.create_table(
        "system_logs",
        # BigInteger PK: 고빈도 로그에 안전한 범위 (최대 9.2 × 10¹⁸)
        sa.Column("id",       sa.BigInteger(), primary_key=True),
        sa.Column(
            "level",
            postgresql.ENUM(name="log_level_enum", create_type=False),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column(
            "category",
            postgresql.ENUM(name="log_category_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "event",
            sa.String(100),
            nullable=False,
            comment="처리 단계 이벤트명 (scrape_start, ai_extract_success 등)",
        ),
        sa.Column("message",     sa.Text(),    nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=True,
            comment="URL, 토큰 수, 모델명, 재시도 횟수 등 컨텍스트",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="처리 소요 시간 (밀리초)",
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("job_queue.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # updated_at 없음 — append-only 설계
    )

    # system_logs 인덱스
    op.create_index("idx_sl_created_at", "system_logs", ["created_at"])
    op.create_index("idx_sl_level_date", "system_logs", ["level",    "created_at"])
    op.create_index("idx_sl_category",   "system_logs", ["category", "created_at"])

    # Partial 인덱스 (nullable FK 최적화)
    op.execute("""
        CREATE INDEX idx_sl_article_id
            ON system_logs (article_id)
            WHERE article_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX idx_sl_job_id
            ON system_logs (job_id)
            WHERE job_id IS NOT NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 6. 통계/집계용 뷰 (선택적 — 운영 편의)
    #
    #    v_article_processing_stats:
    #        process_status별 아티클 수 + 평균 처리 시간
    #    v_artist_coverage:
    #        아티스트별 매핑된 아티클 수 + 평균 신뢰도
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

    op.execute("""
        CREATE OR REPLACE VIEW v_artist_coverage AS
        SELECT
            a.id                       AS artist_id,
            a.name_ko,
            a.name_en,
            a.agency,
            a.is_verified,
            COUNT(em.id)               AS article_count,
            ROUND(AVG(em.confidence_score)::numeric, 3) AS avg_confidence
        FROM artists a
        LEFT JOIN entity_mappings em ON em.entity_id = a.id
        GROUP BY a.id, a.name_ko, a.name_en, a.agency, a.is_verified
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:
    # ── 뷰 ──────────────────────────────────────────────────
    op.execute("DROP VIEW IF EXISTS v_artist_coverage")
    op.execute("DROP VIEW IF EXISTS v_article_processing_stats")

    # ── system_logs ──────────────────────────────────────────
    op.drop_table("system_logs")

    # ── entity_mappings ──────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_entity_map ON entity_mappings")
    op.drop_table("entity_mappings")

    # ── artists ───────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_artists ON artists")
    op.drop_table("artists")

    # ── articles 원복 ─────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_articles_v2 ON articles")
    # 0001 트리거 복원
    op.execute("""
        CREATE TRIGGER set_updated_at_articles
            BEFORE UPDATE ON articles
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)
    op.drop_index("idx_articles_status_priority", table_name="articles")
    op.drop_index("idx_articles_process_status",  table_name="articles")
    op.drop_column("articles", "process_status")
    op.drop_column("articles", "author")

    # job_queue updated_at 원복
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_job_queue ON job_queue")
    op.execute("ALTER TABLE job_queue DROP COLUMN IF EXISTS updated_at")

    # ── ENUM 타입 삭제 ────────────────────────────────────────
    op.execute("DROP TYPE IF EXISTS log_category_enum")
    op.execute("DROP TYPE IF EXISTS log_level_enum")
    op.execute("DROP TYPE IF EXISTS entity_type_enum")
    op.execute("DROP TYPE IF EXISTS process_status_enum")
