"""Evidence-based v2 스키마 — 입체적 인물 구조 + 증거 기반 필드 추적

변경 요약:

  ─ 신규 PostgreSQL ENUM 타입 ────────────────────────────────────
    artist_gender_enum   : MALE, FEMALE, MIXED, UNKNOWN
    activity_status_enum : ACTIVE, HIATUS, DISBANDED, SOLO_ONLY
    sns_platform_enum    : INSTAGRAM, TWITTER_X, YOUTUBE, TIKTOK, WEVERSE,
                           VLIVE, FACEBOOK, THREADS, BLUESKY, WEIBO, OTHER
    education_level_enum : MIDDLE_SCHOOL, HIGH_SCHOOL, UNIVERSITY,
                           GRADUATE, DROPOUT

  ─ 신규 테이블 ──────────────────────────────────────────────────
    groups            — 그룹/밴드 마스터 (v1 artists GROUP 타입 분리)
    member_of         — 아티스트 ↔ 그룹 활동 이력 (유닛 포함)
    artist_educations — 아티스트 학력 이력 (1:N)
    artist_sns        — 아티스트 SNS 계정 (플랫폼별, 1:N)
    group_sns         — 그룹 SNS 계정 (플랫폼별, 1:N)
    data_update_logs  — 기사 → 엔티티 필드 업데이트 감사 로그 (The Core)

  ─ artists 테이블 변경 ──────────────────────────────────────────
    ADD: stage_name_ko, stage_name_en
    ADD: gender (artist_gender_enum)
    ADD: birth_date, birth_date_source_article_id
    ADD: nationality_ko, nationality_en, nationality_source_article_id
    ADD: mbti, mbti_source_article_id
    ADD: blood_type, blood_type_source_article_id
    ADD: height_cm, weight_kg, body_source_article_id
    ADD: bio_ko_source_article_id, bio_en_source_article_id
    DROP: debut_date, agency, official_tags
    DROP INDEX: idx_artists_agency
    ADD CHECK: ck_artists_mbti
    ADD TRIGGER: set_updated_at_artists (이미 존재하면 재생성 생략)
    ADD GIN Trigram INDEX: idx_artists_trgm_bio_ko

  ─ entity_mappings 테이블 변경 ──────────────────────────────────
    ADD: artist_id (FK → artists.id ON DELETE SET NULL)
    ADD: group_id  (FK → groups.id  ON DELETE SET NULL)
    DATA: entity_type='ARTIST' 행의 entity_id → artist_id 복사
    DROP: entity_id (구 단일 FK)
    DROP INDEX: uq_entity_mapping, idx_em_entity (구 인덱스)
    ADD UNIQUE INDEX: uq_em_article_artist, uq_em_article_group (partial)
    ADD INDEX: idx_em_artist_id, idx_em_group_id (partial)
    ADD CHECK: ck_em_entity_fk_consistency

  ─ 뷰 변경 ─────────────────────────────────────────────────────
    DROP/RECREATE: v_artist_coverage  (agency → label 반영)
    CREATE NEW:    v_group_coverage   (그룹별 기사 커버리지)

  ─ 데이터 마이그레이션 주의사항 ─────────────────────────────────
    entity_mappings 에서 entity_type='GROUP' 인 행은
    artist_id=NULL, group_id=NULL 로 처리됩니다.
    (구 artists 테이블의 GROUP 행을 groups 테이블로 이관하려면
     별도의 데이터 마이그레이션 스크립트를 실행해야 합니다.)

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ══════════════════════════════════════════════════════════
    # 0. 신규 PostgreSQL ENUM 타입
    #    DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL $$
    #    → 재실행(idempotent) 보장
    # ══════════════════════════════════════════════════════════

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE artist_gender_enum AS ENUM (
                'MALE', 'FEMALE', 'MIXED', 'UNKNOWN'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE activity_status_enum AS ENUM (
                'ACTIVE', 'HIATUS', 'DISBANDED', 'SOLO_ONLY'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE sns_platform_enum AS ENUM (
                'INSTAGRAM', 'TWITTER_X', 'YOUTUBE', 'TIKTOK',
                'WEVERSE', 'VLIVE', 'FACEBOOK', 'THREADS',
                'BLUESKY', 'WEIBO', 'OTHER'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE education_level_enum AS ENUM (
                'MIDDLE_SCHOOL', 'HIGH_SCHOOL', 'UNIVERSITY',
                'GRADUATE', 'DROPOUT'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # MANUAL_REVIEW 값이 0006 마이그레이션에서 추가됐을 수 있으나,
    # 기존 DB 에 없을 경우를 위해 idempotent 하게 추가합니다.
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE process_status_enum ADD VALUE IF NOT EXISTS 'MANUAL_REVIEW';
        EXCEPTION WHEN others THEN NULL;
        END $$
    """)

    # ══════════════════════════════════════════════════════════
    # 1. groups 테이블 (신규)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "groups",
        sa.Column("id",      sa.Integer(), primary_key=True),

        # 이름 (다국어)
        sa.Column("name_ko", sa.String(200), nullable=False),
        sa.Column("name_en", sa.String(200), nullable=True),

        # 기본 프로필
        sa.Column(
            "gender",
            postgresql.ENUM(name="artist_gender_enum", create_type=False),
            nullable=True,
        ),

        # 데뷔일 (증거 기반)
        sa.Column("debut_date",                   sa.Date(),    nullable=True),
        sa.Column("debut_date_source_article_id",  sa.Integer(), nullable=True),

        # 소속사 (다국어, 증거 기반)
        sa.Column("label_ko",                 sa.String(200), nullable=True),
        sa.Column("label_en",                 sa.String(200), nullable=True),
        sa.Column("label_source_article_id",  sa.Integer(),   nullable=True),

        # 팬덤명 (다국어, 증거 기반)
        sa.Column("fandom_name_ko",                sa.String(100), nullable=True),
        sa.Column("fandom_name_en",                sa.String(100), nullable=True),
        sa.Column("fandom_name_source_article_id", sa.Integer(),   nullable=True),

        # 활동 상태 (증거 기반)
        sa.Column(
            "activity_status",
            postgresql.ENUM(name="activity_status_enum", create_type=False),
            nullable=True,
        ),
        sa.Column("activity_status_source_article_id", sa.Integer(), nullable=True),

        # 소개글 (다국어, 증거 기반)
        sa.Column("bio_ko",                   sa.Text(), nullable=True),
        sa.Column("bio_ko_source_article_id", sa.Integer(), nullable=True),
        sa.Column("bio_en",                   sa.Text(), nullable=True),
        sa.Column("bio_en_source_article_id", sa.Integer(), nullable=True),

        # 시스템 메타
        sa.Column("is_verified",     sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("global_priority", sa.Integer(), nullable=True),

        # 시간
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),

        # 제약
        sa.CheckConstraint(
            "global_priority IS NULL OR global_priority IN (1, 2, 3)",
            name="ck_groups_global_priority",
        ),
    )

    # groups FK (테이블 생성 후 ALTER TABLE 로 추가 — 순환 의존 방지)
    op.execute("""
        ALTER TABLE groups
            ADD CONSTRAINT fk_groups_debut_article
                FOREIGN KEY (debut_date_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_groups_label_article
                FOREIGN KEY (label_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_groups_fandom_article
                FOREIGN KEY (fandom_name_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_groups_activity_article
                FOREIGN KEY (activity_status_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_groups_bio_ko_article
                FOREIGN KEY (bio_ko_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_groups_bio_en_article
                FOREIGN KEY (bio_en_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL
    """)

    # groups B-tree 인덱스
    op.create_index("idx_groups_name_ko",         "groups", ["name_ko"])
    op.create_index("idx_groups_name_en",          "groups", ["name_en"])
    op.create_index("idx_groups_is_verified",      "groups", ["is_verified"])
    op.create_index("idx_groups_activity_status",  "groups", ["activity_status"])
    op.execute("""
        CREATE INDEX idx_groups_global_priority
            ON groups (global_priority)
            WHERE global_priority IS NOT NULL
    """)

    # groups GIN Trigram
    op.execute("CREATE INDEX idx_groups_trgm_name_ko ON groups USING GIN (name_ko gin_trgm_ops)")
    op.execute("CREATE INDEX idx_groups_trgm_name_en ON groups USING GIN (name_en gin_trgm_ops)")

    # groups updated_at 트리거
    op.execute("""
        CREATE TRIGGER set_updated_at_groups
            BEFORE UPDATE ON groups
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 2. artists 테이블 변경
    # ══════════════════════════════════════════════════════════

    # ── 2-A. 신규 컬럼 추가 ──────────────────────────────────

    op.execute("""
        ALTER TABLE artists
            ADD COLUMN IF NOT EXISTS stage_name_ko VARCHAR(200),
            ADD COLUMN IF NOT EXISTS stage_name_en VARCHAR(200),
            ADD COLUMN IF NOT EXISTS gender         artist_gender_enum,
            ADD COLUMN IF NOT EXISTS birth_date     DATE,
            ADD COLUMN IF NOT EXISTS birth_date_source_article_id  INTEGER,
            ADD COLUMN IF NOT EXISTS nationality_ko VARCHAR(100),
            ADD COLUMN IF NOT EXISTS nationality_en VARCHAR(100),
            ADD COLUMN IF NOT EXISTS nationality_source_article_id INTEGER,
            ADD COLUMN IF NOT EXISTS mbti            VARCHAR(4),
            ADD COLUMN IF NOT EXISTS mbti_source_article_id        INTEGER,
            ADD COLUMN IF NOT EXISTS blood_type     VARCHAR(3),
            ADD COLUMN IF NOT EXISTS blood_type_source_article_id  INTEGER,
            ADD COLUMN IF NOT EXISTS height_cm      FLOAT,
            ADD COLUMN IF NOT EXISTS weight_kg      FLOAT,
            ADD COLUMN IF NOT EXISTS body_source_article_id        INTEGER,
            ADD COLUMN IF NOT EXISTS bio_ko_source_article_id      INTEGER,
            ADD COLUMN IF NOT EXISTS bio_en_source_article_id      INTEGER
    """)

    # bio_ko, bio_en 이 이전 마이그레이션에서 없었을 경우를 위한 안전망
    op.execute("""
        ALTER TABLE artists
            ADD COLUMN IF NOT EXISTS bio_ko TEXT,
            ADD COLUMN IF NOT EXISTS bio_en TEXT
    """)

    # artists source_article FK 제약 추가
    op.execute("""
        ALTER TABLE artists
            ADD CONSTRAINT fk_artists_birth_article
                FOREIGN KEY (birth_date_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_nat_article
                FOREIGN KEY (nationality_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_mbti_article
                FOREIGN KEY (mbti_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_blood_article
                FOREIGN KEY (blood_type_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_body_article
                FOREIGN KEY (body_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_bio_ko_article
                FOREIGN KEY (bio_ko_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_artists_bio_en_article
                FOREIGN KEY (bio_en_source_article_id)
                REFERENCES articles(id) ON DELETE SET NULL
    """)

    # MBTI 값 검증 CHECK
    op.execute("""
        ALTER TABLE artists
            ADD CONSTRAINT ck_artists_mbti
                CHECK (mbti IS NULL OR (length(mbti) = 4 AND mbti ~ '^[A-Z]{4}$'))
    """)

    # bio GIN Trigram (bio_ko 검색)
    op.execute("CREATE INDEX IF NOT EXISTS idx_artists_trgm_bio_ko ON artists USING GIN (bio_ko gin_trgm_ops)")

    # ── 2-B. 구 컬럼 제거 ──────────────────────────────────
    # 주의: 기존 데이터(debut_date, agency, official_tags)가 있다면
    #       먼저 별도 백업을 수행할 것.

    # 구 인덱스 삭제 (agency 컬럼에 걸려있던 인덱스)
    op.execute("DROP INDEX IF EXISTS idx_artists_agency")

    # 구 컬럼 삭제
    op.execute("""
        ALTER TABLE artists
            DROP COLUMN IF EXISTS debut_date,
            DROP COLUMN IF EXISTS agency,
            DROP COLUMN IF EXISTS official_tags
    """)

    # ══════════════════════════════════════════════════════════
    # 3. member_of 테이블 (신규)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "member_of",
        sa.Column("id",        sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "roles",
            postgresql.ARRAY(sa.String(20)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
            comment="MemberRole 값 배열 (예: ['VOCALIST', 'RAPPER'])",
        ),
        sa.Column("started_on",  sa.Date(), nullable=True),
        sa.Column("ended_on",    sa.Date(), nullable=True),
        sa.Column(
            "is_sub_unit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "source_article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_mo_date_order",
        ),
    )

    op.create_index("idx_mo_artist_id", "member_of", ["artist_id"])
    op.create_index("idx_mo_group_id",  "member_of", ["group_id"])
    op.execute("""
        CREATE INDEX idx_mo_active
            ON member_of (group_id, artist_id)
            WHERE ended_on IS NULL
    """)

    op.execute("""
        CREATE TRIGGER set_updated_at_member_of
            BEFORE UPDATE ON member_of
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 4. artist_educations 테이블 (신규)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "artist_educations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("school_name_ko",  sa.String(300), nullable=False),
        sa.Column("school_name_en",  sa.String(300), nullable=True),
        sa.Column(
            "education_level",
            postgresql.ENUM(name="education_level_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("graduated_year", sa.Integer(), nullable=True),
        sa.Column(
            "source_article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_index("idx_ae_artist_id",       "artist_educations", ["artist_id"])
    op.create_index("idx_ae_education_level",  "artist_educations", ["education_level"])

    # ══════════════════════════════════════════════════════════
    # 5. artist_sns 테이블 (신규)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "artist_sns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "platform",
            postgresql.ENUM(name="sns_platform_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("url",            sa.Text(),       nullable=True),
        sa.Column("handle",         sa.String(200),  nullable=True),
        sa.Column("follower_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "source_article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("artist_id", "platform", name="uq_artist_sns_platform"),
    )

    op.create_index("idx_asns_artist_id", "artist_sns", ["artist_id"])
    op.create_index("idx_asns_platform",  "artist_sns", ["platform"])

    op.execute("""
        CREATE TRIGGER set_updated_at_artist_sns
            BEFORE UPDATE ON artist_sns
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 6. group_sns 테이블 (신규)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "group_sns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "platform",
            postgresql.ENUM(name="sns_platform_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("url",            sa.Text(),       nullable=True),
        sa.Column("handle",         sa.String(200),  nullable=True),
        sa.Column("follower_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "source_article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("group_id", "platform", name="uq_group_sns_platform"),
    )

    op.create_index("idx_gsns_group_id", "group_sns", ["group_id"])
    op.create_index("idx_gsns_platform", "group_sns", ["platform"])

    op.execute("""
        CREATE TRIGGER set_updated_at_group_sns
            BEFORE UPDATE ON group_sns
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()
    """)

    # ══════════════════════════════════════════════════════════
    # 7. data_update_logs 테이블 (신규 — The Core)
    # ══════════════════════════════════════════════════════════

    op.create_table(
        "data_update_logs",
        # BigInteger PK: 고빈도 로그에 안전한 범위
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
            comment="업데이트 출처 기사. 기사 삭제 시 NULL 로 처리(이력 보존)",
        ),
        sa.Column(
            "entity_type",
            postgresql.ENUM(name="entity_type_enum", create_type=False),
            nullable=False,
            comment="ARTIST=artists.id 참조, GROUP=groups.id 참조",
        ),
        sa.Column(
            "entity_id",
            sa.Integer(),
            nullable=False,
            comment="artists.id 또는 groups.id (entity_type 에 따라 결정)",
        ),
        sa.Column(
            "field_name",
            sa.String(100),
            nullable=False,
            comment="변경된 필드명 (예: birth_date, mbti, fandom_name_ko)",
        ),
        sa.Column(
            "old_value_json",
            postgresql.JSONB,
            nullable=True,
            comment='변경 전 값. NULL=최초 입력. 예: {"value": "INTJ"}',
        ),
        sa.Column(
            "new_value_json",
            postgresql.JSONB,
            nullable=True,
            comment='변경 후 값. 예: {"value": "INFP"}',
        ),
        sa.Column(
            "updated_by",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'ai_pipeline'"),
            comment="변경 주체: ai_pipeline | manual | scraper",
        ),
        # append-only — updated_at 없음
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.execute("""
        CREATE INDEX idx_dul_article_id
            ON data_update_logs (article_id)
            WHERE article_id IS NOT NULL
    """)
    op.create_index("idx_dul_entity",       "data_update_logs", ["entity_type", "entity_id"])
    op.create_index("idx_dul_entity_field", "data_update_logs", ["entity_type", "entity_id", "field_name"])
    op.create_index("idx_dul_created_at",   "data_update_logs", ["created_at"])
    op.create_index("idx_dul_field_name",   "data_update_logs", ["field_name"])

    # ══════════════════════════════════════════════════════════
    # 8. entity_mappings — 구조 변경
    #
    #    v1: entity_id (FK → artists.id) 단일 컬럼
    #    v2: artist_id (FK → artists.id) + group_id (FK → groups.id) 분리
    #
    #    데이터 마이그레이션:
    #      ARTIST 타입 행: entity_id → artist_id 복사
    #      GROUP  타입 행: group_id=NULL 유지
    #                      (구 artists 행을 groups 로 이관하는 작업은
    #                       별도 스크립트로 수행)
    # ══════════════════════════════════════════════════════════

    # 8-A. 신규 컬럼 추가
    op.add_column(
        "entity_mappings",
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="SET NULL"),
            nullable=True,
            comment="entity_type=ARTIST 일 때 설정",
        ),
    )
    op.add_column(
        "entity_mappings",
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="SET NULL"),
            nullable=True,
            comment="entity_type=GROUP 일 때 설정",
        ),
    )

    # 8-B. 데이터 마이그레이션 — ARTIST 타입 행 복사
    op.execute("""
        UPDATE entity_mappings
        SET artist_id = entity_id
        WHERE entity_type = 'ARTIST'
          AND entity_id IS NOT NULL
    """)

    # 8-C. 구 인덱스 삭제
    op.execute("DROP INDEX IF EXISTS uq_entity_mapping")
    op.execute("DROP INDEX IF EXISTS idx_em_entity")

    # 8-D. 구 entity_id 컬럼 삭제
    #      v_artist_coverage 가 entity_id 를 참조하므로 먼저 DROP
    op.execute("DROP VIEW IF EXISTS v_artist_coverage")

    #      FK 제약이 있으면 먼저 삭제
    op.execute("""
        DO $$ DECLARE
            c TEXT;
        BEGIN
            SELECT constraint_name INTO c
            FROM information_schema.table_constraints
            WHERE table_name = 'entity_mappings'
              AND constraint_type = 'FOREIGN KEY'
              AND constraint_name LIKE '%entity_id%';
            IF c IS NOT NULL THEN
                EXECUTE 'ALTER TABLE entity_mappings DROP CONSTRAINT ' || quote_ident(c);
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE entity_mappings DROP COLUMN IF EXISTS entity_id")

    # 8-E. CHECK 제약 추가 (FK 일관성)
    op.execute("""
        ALTER TABLE entity_mappings
            ADD CONSTRAINT ck_em_entity_fk_consistency CHECK (
                (entity_type = 'ARTIST' AND artist_id IS NOT NULL AND group_id IS NULL) OR
                (entity_type = 'GROUP'  AND group_id  IS NOT NULL AND artist_id IS NULL) OR
                (entity_type = 'EVENT'  AND artist_id IS NULL     AND group_id  IS NULL)
            )
    """)

    # 8-F. 신규 인덱스
    op.execute("""
        CREATE UNIQUE INDEX uq_em_article_artist
            ON entity_mappings (article_id, artist_id)
            WHERE artist_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_em_article_group
            ON entity_mappings (article_id, group_id)
            WHERE group_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX idx_em_artist_id
            ON entity_mappings (artist_id)
            WHERE artist_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX idx_em_group_id
            ON entity_mappings (group_id)
            WHERE group_id IS NOT NULL
    """)

    # ══════════════════════════════════════════════════════════
    # 9. 뷰 업데이트
    #    v_artist_coverage : agency 컬럼 제거 → label_ko 반영 (없으므로 제거)
    #    v_group_coverage  : 신규 그룹 커버리지 뷰
    # ══════════════════════════════════════════════════════════

    # 구 v_artist_coverage (agency 사용) 교체
    op.execute("DROP VIEW IF EXISTS v_artist_coverage")
    op.execute("""
        CREATE OR REPLACE VIEW v_artist_coverage AS
        SELECT
            a.id                                                     AS artist_id,
            a.name_ko,
            a.name_en,
            a.is_verified,
            a.global_priority,
            COUNT(em.id)                                             AS article_count,
            ROUND(AVG(em.confidence_score)::numeric, 3)              AS avg_confidence
        FROM artists a
        LEFT JOIN entity_mappings em
            ON em.artist_id = a.id
        GROUP BY a.id, a.name_ko, a.name_en, a.is_verified, a.global_priority
    """)

    # 신규 v_group_coverage
    op.execute("""
        CREATE OR REPLACE VIEW v_group_coverage AS
        SELECT
            g.id                                                     AS group_id,
            g.name_ko,
            g.name_en,
            g.label_ko,
            g.activity_status,
            g.is_verified,
            g.global_priority,
            COUNT(em.id)                                             AS article_count,
            ROUND(AVG(em.confidence_score)::numeric, 3)              AS avg_confidence,
            COUNT(mo.id)                                             AS member_count
        FROM groups g
        LEFT JOIN entity_mappings em ON em.group_id = g.id
        LEFT JOIN member_of mo
            ON mo.group_id = g.id AND mo.ended_on IS NULL
        GROUP BY
            g.id, g.name_ko, g.name_en, g.label_ko,
            g.activity_status, g.is_verified, g.global_priority
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # ── 뷰 ──────────────────────────────────────────────────
    op.execute("DROP VIEW IF EXISTS v_group_coverage")
    op.execute("DROP VIEW IF EXISTS v_artist_coverage")

    # ── entity_mappings 원복 ─────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_em_group_id")
    op.execute("DROP INDEX IF EXISTS idx_em_artist_id")
    op.execute("DROP INDEX IF EXISTS uq_em_article_group")
    op.execute("DROP INDEX IF EXISTS uq_em_article_artist")
    op.execute("""
        ALTER TABLE entity_mappings
            DROP CONSTRAINT IF EXISTS ck_em_entity_fk_consistency
    """)

    # entity_id 컬럼 복원 + 데이터 복구
    op.add_column(
        "entity_mappings",
        sa.Column(
            "entity_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute("""
        UPDATE entity_mappings
        SET entity_id = artist_id
        WHERE entity_type = 'ARTIST' AND artist_id IS NOT NULL
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_entity_mapping
            ON entity_mappings (article_id, entity_type, entity_id)
            WHERE entity_id IS NOT NULL
    """)
    op.create_index("idx_em_entity", "entity_mappings", ["entity_type", "entity_id"])

    op.drop_column("entity_mappings", "group_id")
    op.drop_column("entity_mappings", "artist_id")

    # ── data_update_logs ─────────────────────────────────────
    op.drop_table("data_update_logs")

    # ── group_sns ────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_group_sns ON group_sns")
    op.drop_table("group_sns")

    # ── artist_sns ───────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_artist_sns ON artist_sns")
    op.drop_table("artist_sns")

    # ── artist_educations ────────────────────────────────────
    op.drop_table("artist_educations")

    # ── member_of ────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_member_of ON member_of")
    op.drop_table("member_of")

    # ── artists 원복 ─────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_artists_trgm_bio_ko")
    op.execute("ALTER TABLE artists DROP CONSTRAINT IF EXISTS ck_artists_mbti")

    # source_article FK 제거
    op.execute("""
        ALTER TABLE artists
            DROP CONSTRAINT IF EXISTS fk_artists_birth_article,
            DROP CONSTRAINT IF EXISTS fk_artists_nat_article,
            DROP CONSTRAINT IF EXISTS fk_artists_mbti_article,
            DROP CONSTRAINT IF EXISTS fk_artists_blood_article,
            DROP CONSTRAINT IF EXISTS fk_artists_body_article,
            DROP CONSTRAINT IF EXISTS fk_artists_bio_ko_article,
            DROP CONSTRAINT IF EXISTS fk_artists_bio_en_article
    """)

    # v2 에서 추가된 컬럼 삭제
    op.execute("""
        ALTER TABLE artists
            DROP COLUMN IF EXISTS stage_name_ko,
            DROP COLUMN IF EXISTS stage_name_en,
            DROP COLUMN IF EXISTS gender,
            DROP COLUMN IF EXISTS birth_date,
            DROP COLUMN IF EXISTS birth_date_source_article_id,
            DROP COLUMN IF EXISTS nationality_ko,
            DROP COLUMN IF EXISTS nationality_en,
            DROP COLUMN IF EXISTS nationality_source_article_id,
            DROP COLUMN IF EXISTS mbti,
            DROP COLUMN IF EXISTS mbti_source_article_id,
            DROP COLUMN IF EXISTS blood_type,
            DROP COLUMN IF EXISTS blood_type_source_article_id,
            DROP COLUMN IF EXISTS height_cm,
            DROP COLUMN IF EXISTS weight_kg,
            DROP COLUMN IF EXISTS body_source_article_id,
            DROP COLUMN IF EXISTS bio_ko_source_article_id,
            DROP COLUMN IF EXISTS bio_en_source_article_id
    """)

    # v1 컬럼 복원
    op.execute("""
        ALTER TABLE artists
            ADD COLUMN IF NOT EXISTS debut_date   DATE,
            ADD COLUMN IF NOT EXISTS agency       VARCHAR(200),
            ADD COLUMN IF NOT EXISTS official_tags JSONB
                NOT NULL DEFAULT '{}'::jsonb
    """)
    op.create_index("idx_artists_agency", "artists", ["agency"])

    # ── groups ───────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS set_updated_at_groups ON groups")
    # groups 를 참조하는 FK 먼저 해제
    op.execute("""
        ALTER TABLE groups
            DROP CONSTRAINT IF EXISTS fk_groups_debut_article,
            DROP CONSTRAINT IF EXISTS fk_groups_label_article,
            DROP CONSTRAINT IF EXISTS fk_groups_fandom_article,
            DROP CONSTRAINT IF EXISTS fk_groups_activity_article,
            DROP CONSTRAINT IF EXISTS fk_groups_bio_ko_article,
            DROP CONSTRAINT IF EXISTS fk_groups_bio_en_article
    """)
    op.drop_table("groups")

    # ── v_artist_coverage 원복 ───────────────────────────────
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

    # ── ENUM 타입 삭제 ────────────────────────────────────────
    op.execute("DROP TYPE IF EXISTS education_level_enum")
    op.execute("DROP TYPE IF EXISTS sns_platform_enum")
    op.execute("DROP TYPE IF EXISTS activity_status_enum")
    op.execute("DROP TYPE IF EXISTS artist_gender_enum")
