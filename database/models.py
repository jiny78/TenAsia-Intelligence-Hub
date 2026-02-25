"""
database/models.py — SQLAlchemy ORM 모델

테이블:
    job_queue       — 분산 작업 큐 (SKIP LOCKED 패턴)
    artists         — 아티스트/그룹 마스터 데이터
    articles        — 수집·정제된 아티클 (다국어)
    entity_mappings — 아티클 ↔ 아티스트/그룹/이벤트 연결 (신뢰도 점수 포함)
    system_logs     — 스크래핑·AI 처리 이력 (append-only)
    glossary        — 한↔영 번역 용어 사전 (AI 번역 일관성 확보)

설계 원칙:
    - 모든 테이블에 created_at / updated_at (TIMESTAMPTZ)
    - updated_at 은 PostgreSQL 트리거(trg_set_updated_at)로 자동 갱신
      → system_logs 는 append-only 이므로 updated_at 없음
    - ENUM 타입은 PostgreSQL 네이티브 ENUM (마이그레이션에서 CREATE TYPE)
    - JSONB: 반구조화 데이터 (official_tags, details)
    - GIN/Trigram 인덱스: 0001_initial / 0002_phase2_schema 마이그레이션에서 op.execute()

Alembic autogenerate 기준 파일 — 여기서 모델 변경 → alembic revision --autogenerate
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# PostgreSQL TIMESTAMP WITH TIME ZONE 편의 별칭
TIMESTAMPTZ = DateTime(timezone=True)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


# ═════════════════════════════════════════════════════════════
# Python Enum 정의 (PostgreSQL ENUM 과 1:1 대응)
# ═════════════════════════════════════════════════════════════

class ProcessStatus(str, enum.Enum):
    """아티클 처리 상태 — articles.process_status"""
    PENDING   = "PENDING"    # 수집 대기
    SCRAPED   = "SCRAPED"    # HTML 수집 완료, AI 처리 대기
    PROCESSED = "PROCESSED"  # Gemini AI 정제 완료
    ERROR     = "ERROR"      # 처리 실패 (system_logs 참조)


class EntityType(str, enum.Enum):
    """엔티티 유형 — entity_mappings.entity_type"""
    ARTIST = "ARTIST"  # 솔로 아티스트
    GROUP  = "GROUP"   # 그룹/밴드 (artists 테이블 공유)
    EVENT  = "EVENT"   # 공연/시상식 (entity_id = NULL 허용, 미래 events 테이블 연결 예정)


class LogLevel(str, enum.Enum):
    """로그 심각도 — system_logs.level"""
    DEBUG   = "DEBUG"
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"


class LogCategory(str, enum.Enum):
    """처리 단계 분류 — system_logs.category"""
    SCRAPE     = "SCRAPE"      # HTTP 수집
    AI_PROCESS = "AI_PROCESS"  # Gemini API 호출
    DB_WRITE   = "DB_WRITE"    # DB UPSERT
    S3_UPLOAD  = "S3_UPLOAD"   # 이미지 S3 업로드
    API_CALL   = "API_CALL"    # 외부 API 호출 (일반)


class GlossaryCategory(str, enum.Enum):
    """용어 분류 — glossary.category"""
    ARTIST = "ARTIST"  # 아티스트/그룹명    (예: 방탄소년단 → BTS)
    AGENCY = "AGENCY"  # 소속사명          (예: 하이브 → HYBE)
    EVENT  = "EVENT"   # 공연·방송·시상식명 (예: 뮤직뱅크 → Music Bank)


# ═════════════════════════════════════════════════════════════
# JobQueue
# ═════════════════════════════════════════════════════════════

class JobQueue(Base):
    """
    분산 작업 큐.

    워커(EC2)는 FOR UPDATE SKIP LOCKED 로 pending 행을 원자적으로 가져갑니다.
    raw SQL 조작: scraper/db.py
    """
    __tablename__ = "job_queue"

    id:           Mapped[int]                = mapped_column(Integer,     primary_key=True)
    job_type:     Mapped[str]                = mapped_column(String(50),  nullable=False, default="scrape")
    params:       Mapped[Optional[dict]]     = mapped_column(JSONB,       nullable=False, default=dict)
    status:       Mapped[str]                = mapped_column(String(20),  nullable=False, default="pending")
    priority:     Mapped[int]                = mapped_column(Integer,     nullable=False, default=5)
    created_at:   Mapped[datetime]           = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at:   Mapped[datetime]           = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    started_at:   Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    worker_id:    Mapped[Optional[str]]      = mapped_column(String(100))
    result:       Mapped[Optional[dict]]     = mapped_column(JSONB)
    error_msg:    Mapped[Optional[str]]      = mapped_column(Text)
    retry_count:  Mapped[int]                = mapped_column(Integer, nullable=False, default=0)
    max_retries:  Mapped[int]                = mapped_column(Integer, nullable=False, default=3)

    # ── 관계 ──────────────────────────────────────────────────
    articles:    Mapped[list["Article"]]   = relationship(back_populates="job")
    system_logs: Mapped[list["SystemLog"]] = relationship(back_populates="job")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_job_queue_status",
        ),
        # 워커가 pending 작업을 우선순위·시간 순으로 조회하는 부분 인덱스
        Index(
            "idx_jq_pending",
            "status", "priority", "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<JobQueue id={self.id} type={self.job_type!r} status={self.status!r}>"


# ═════════════════════════════════════════════════════════════
# Artist
# ═════════════════════════════════════════════════════════════

class Artist(Base):
    """
    아티스트 / 그룹 마스터 데이터.

    ARTIST (솔로)와 GROUP (그룹/밴드) 모두 이 테이블에 저장합니다.
    entity_mappings.entity_type 으로 구분합니다.

    official_tags (JSONB) 예시:
        {
            "fandom": "BLINK",
            "social": {"twitter": "@BLACKPINK", "instagram": "@blackpinkofficial"},
            "genres": ["K-Pop", "Dance"],
            "debut_album": "SQUARE ONE"
        }
    """
    __tablename__ = "artists"

    id:            Mapped[int]            = mapped_column(Integer, primary_key=True)
    name_ko:       Mapped[str]            = mapped_column(String(200), nullable=False)
    name_en:       Mapped[Optional[str]]  = mapped_column(String(200))
    debut_date:    Mapped[Optional[date]] = mapped_column(Date)
    agency:        Mapped[Optional[str]]  = mapped_column(String(200))
    official_tags: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="팬덤명, SNS 계정, 장르, 데뷔 앨범 등 반구조화 메타데이터",
    )
    is_verified:   Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="공식 확인된 아티스트 여부",
    )

    # ── 소개글 (다국어) ───────────────────────────────────────
    bio_ko: Mapped[Optional[str]] = mapped_column(
        Text, comment="아티스트 소개글 (한국어)"
    )
    bio_en: Mapped[Optional[str]] = mapped_column(
        Text, comment="아티스트 소개글 (영어)"
    )

    # ── 번역 우선순위 ───────────────────────────────────────────
    # AI 번역 비용 통제 — 아티스트별 번역 정책 결정
    #   1 : 최우선 — title_en + summary_en + hashtags_en 전체 번역 (글로벌 팬덤 아티스트)
    #   2 : 요약만  — summary_en 만 번역 (국내 인지도 있으나 글로벌 팬덤 제한)
    #   3 : 번역 제외 — 한국어 최소 추출만 (국내 아티스트 / 신인)
    #   NULL: 미분류 (신규 아티스트 등록 시 초기 상태)
    global_priority: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="번역 우선순위: 1=전체번역, 2=요약만, 3=번역제외, NULL=미분류",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    entity_mappings: Mapped[list["EntityMapping"]] = relationship(
        back_populates="artist",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "global_priority IS NULL OR global_priority IN (1, 2, 3)",
            name="ck_artists_global_priority",
        ),
        # 이름 검색용 B-tree (트라이그램 GIN 은 마이그레이션에서 op.execute()로 생성)
        Index("idx_artists_name_ko",     "name_ko"),
        Index("idx_artists_name_en",     "name_en"),
        Index("idx_artists_agency",      "agency"),
        Index("idx_artists_is_verified", "is_verified"),
        # 번역 우선순위 필터 — NULL 제외 (NOT NULL 행만 인덱스)
        Index(
            "idx_artists_global_priority", "global_priority",
            postgresql_where=text("global_priority IS NOT NULL"),
        ),
        # GIN Trigram 인덱스 (0002/0003 마이그레이션에서 op.execute()로 생성)
        # idx_artists_trgm_name_ko, idx_artists_trgm_name_en
        # idx_artists_trgm_bio_ko,  idx_artists_trgm_bio_en
    )

    def __repr__(self) -> str:
        return f"<Artist id={self.id} name_ko={self.name_ko!r} verified={self.is_verified}>"


# ═════════════════════════════════════════════════════════════
# Article
# ═════════════════════════════════════════════════════════════

class Article(Base):
    """
    수집·정제된 아티클.

    수명 주기:
        PENDING → (워커 스크래핑) → SCRAPED
                → (Gemini AI 정제) → PROCESSED
                → (실패 시)        → ERROR

    GIN/Trigram 인덱스 (0001_initial 마이그레이션에서 생성):
        - to_tsvector FTS: title_ko+body_ko (simple), title_en+body_en (english)
        - gin_trgm_ops:    title_ko, title_en, body_ko, body_en, artist_name_*
        - GIN array:       hashtags_ko, hashtags_en
    """
    __tablename__ = "articles"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    language:   Mapped[str] = mapped_column(String(5), nullable=False, default="kr")

    # ── 처리 상태 ─────────────────────────────────────────────
    process_status: Mapped[ProcessStatus] = mapped_column(
        SAEnum(ProcessStatus, name="process_status_enum", create_type=False),
        nullable=False,
        default=ProcessStatus.PENDING,
        server_default="PENDING",
    )

    # ── 제목 (다국어) ─────────────────────────────────────────
    title_ko: Mapped[Optional[str]] = mapped_column(Text)
    title_en: Mapped[Optional[str]] = mapped_column(Text)

    # ── 원문 (한국어 전문, 비용 효율상 영어 전체 번역 미제공) ──
    content_ko: Mapped[Optional[str]] = mapped_column(
        Text, comment="원문(한국어) 전체 본문"
    )

    # ── 요약 / SNS 캡션 (다국어) ──────────────────────────────
    summary_ko: Mapped[Optional[str]] = mapped_column(Text)
    summary_en: Mapped[Optional[str]] = mapped_column(Text)

    # ── 저자 및 원문 메타 ─────────────────────────────────────
    author:       Mapped[Optional[str]] = mapped_column(String(200))
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    # ── 아티스트 (비정규화, 빠른 필터링용) ────────────────────
    artist_name_ko:  Mapped[Optional[str]] = mapped_column(String(200))
    artist_name_en:  Mapped[Optional[str]] = mapped_column(String(200))
    global_priority: Mapped[bool]          = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # ── SEO 해시태그 (배열) ───────────────────────────────────
    hashtags_ko: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::text[]"),
    )
    hashtags_en: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::text[]"),
    )

    # ── AI 생성 SEO 해시태그 (JSONB — 메타데이터 포함) ─────────
    # 단순 배열(hashtags_en)과의 차이:
    #   hashtags_en  : 기본 해시태그 배열 (SNS 게시 등 단순 활용)
    #   seo_hashtags : 생성 모델·신뢰도·카테고리 등 메타데이터 포함 (SEO 전략 고도화)
    # 구조 예시:
    #   {
    #     "tags":         ["BTS", "방탄소년단", "KPOP", "NewAlbum"],
    #     "model":        "gemini-2.0-flash",
    #     "generated_at": "2026-02-25T09:00:00Z",
    #     "confidence":   0.95,
    #     "categories":   {"brand": ["BTS"], "genre": ["KPOP"], "event": ["NewAlbum"]}
    #   }
    seo_hashtags: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="AI 생성 영어 SEO 해시태그 (생성 모델·신뢰도·카테고리 메타데이터 포함)",
    )

    # ── 미디어 ────────────────────────────────────────────────
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text)

    # ── FK ────────────────────────────────────────────────────
    job_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_queue.id", ondelete="SET NULL"),
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    job:             Mapped[Optional["JobQueue"]]   = relationship(back_populates="articles")
    entity_mappings: Mapped[list["EntityMapping"]]  = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )
    system_logs:     Mapped[list["SystemLog"]]      = relationship(back_populates="article")

    __table_args__ = (
        CheckConstraint("language IN ('kr','en','jp')", name="ck_articles_language"),
        # 처리 상태별 작업 조회 인덱스
        Index("idx_articles_process_status",      "process_status", "created_at"),
        Index("idx_articles_status_priority",     "process_status", "global_priority", "created_at"),
        # 아티스트명 B-tree (트라이그램 GIN 은 0001_initial 마이그레이션에서 생성)
        Index("idx_articles_global_flag",         "global_priority", "created_at",
              postgresql_where=text("global_priority = true")),
        Index("idx_articles_language_date",       "language", "created_at"),
    )

    # ── GIN/Trigram 인덱스 현황 (0001, 0003, 0004 마이그레이션에서 op.execute()로 생성) ──
    # FTS  GIN : title_ko + content_ko + summary_ko  (simple, 한국어)
    # FTS  GIN : title_en + summary_en               (english, 영어)
    # Trgm GIN : title_ko, title_en, content_ko, summary_ko, summary_en
    #            artist_name_ko, artist_name_en
    # Array GIN: hashtags_ko, hashtags_en
    # JSONB GIN: seo_hashtags  (0004 마이그레이션에서 생성)

    def __repr__(self) -> str:
        title = str(self.title_ko or "")[:30]
        return f"<Article id={self.id} status={self.process_status!r} title_ko={title!r}>"


# ═════════════════════════════════════════════════════════════
# EntityMapping
# ═════════════════════════════════════════════════════════════

class EntityMapping(Base):
    """
    아티클 ↔ 아티스트/그룹/이벤트 연결.

    confidence_score (0.0 ~ 1.0):
        - Gemini AI 가 아티클에서 해당 엔티티를 감지한 신뢰도
        - 1.0  : URL 또는 제목에서 명시적으로 언급됨
        - 0.8+ : 본문에서 여러 번 언급됨
        - 0.5~ : 간접 언급 또는 태그에서만 발견됨

    entity_id 규칙:
        - ARTIST / GROUP : artists.id 참조
        - EVENT          : NULL 허용 (미래 events 테이블 추가 시 FK 마이그레이션 예정)

    유니크 제약:
        (article_id, entity_type, entity_id) — entity_id IS NOT NULL 에만 적용
        → 동일 아티클에 같은 아티스트가 중복 매핑되지 않도록 방지
    """
    __tablename__ = "entity_mappings"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int]           = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False,
    )
    entity_type: Mapped[EntityType]   = mapped_column(
        SAEnum(EntityType, name="entity_type_enum", create_type=False),
        nullable=False,
    )
    entity_id: Mapped[Optional[int]]  = mapped_column(
        Integer,
        ForeignKey("artists.id", ondelete="SET NULL"),
        nullable=True,
        comment="ARTIST/GROUP → artists.id | EVENT → NULL (미래 events.id 예정)",
    )
    confidence_score: Mapped[float]   = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default=text("1.0"),
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    article: Mapped["Article"]          = relationship(back_populates="entity_mappings")
    artist:  Mapped[Optional["Artist"]] = relationship(back_populates="entity_mappings")

    __table_args__ = (
        CheckConstraint("confidence_score BETWEEN 0.0 AND 1.0", name="ck_em_confidence"),
        # entity_id NOT NULL 행에만 유니크 적용 (EVENT NULL 중복 허용)
        Index(
            "uq_entity_mapping",
            "article_id", "entity_type", "entity_id",
            unique=True,
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        Index("idx_em_article_id",  "article_id"),
        Index("idx_em_entity",      "entity_type", "entity_id"),
        Index("idx_em_confidence",  "confidence_score"),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityMapping article={self.article_id} "
            f"type={self.entity_type!r} entity={self.entity_id} "
            f"score={self.confidence_score:.2f}>"
        )


# ═════════════════════════════════════════════════════════════
# SystemLog
# ═════════════════════════════════════════════════════════════

class SystemLog(Base):
    """
    스크래핑·AI 처리 이력 (append-only).

    설계 특징:
        - BigInteger PK: 고빈도 로그에 안전한 범위
        - updated_at 없음: 로그는 수정하지 않는다
        - article_id / job_id: 둘 다 nullable (시스템 이벤트는 연결 없이도 기록)
        - details (JSONB): 단계별 컨텍스트 저장

    event 값 예시:
        "scrape_start", "scrape_success", "scrape_error"
        "ai_extract_start", "ai_extract_success", "ai_extract_error"
        "db_upsert_success", "s3_upload_success", "kill_switch_activated"

    details (JSONB) 예시:
        {
            "url": "https://tenasia.hankyung.com/...",
            "html_bytes": 42300,
            "tokens_used": 1250,
            "model": "gemini-2.0-flash",
            "retry": 1
        }
    """
    __tablename__ = "system_logs"

    id:          Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    level:       Mapped[LogLevel]       = mapped_column(
        SAEnum(LogLevel, name="log_level_enum", create_type=False),
        nullable=False,
        default=LogLevel.INFO,
        server_default="INFO",
    )
    category:    Mapped[LogCategory]    = mapped_column(
        SAEnum(LogCategory, name="log_category_enum", create_type=False),
        nullable=False,
    )
    event:       Mapped[str]            = mapped_column(
        String(100), nullable=False,
        comment="처리 단계 이벤트명 (예: scrape_start, ai_extract_success)",
    )
    message:     Mapped[str]            = mapped_column(Text, nullable=False)
    details:     Mapped[Optional[dict]] = mapped_column(
        JSONB,
        comment="단계별 컨텍스트: URL, 토큰 수, 모델명, 재시도 횟수 등",
    )
    duration_ms: Mapped[Optional[int]]  = mapped_column(
        Integer,
        comment="처리 소요 시간 (밀리초)",
    )

    # ── FK (nullable) ─────────────────────────────────────────
    article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
    )
    job_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_queue.id", ondelete="SET NULL"),
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(100))

    # ── 시간 (append-only — updated_at 없음) ──────────────────
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(),
    )

    # ── 관계 ──────────────────────────────────────────────────
    article: Mapped[Optional["Article"]]   = relationship(back_populates="system_logs")
    job:     Mapped[Optional["JobQueue"]]  = relationship(back_populates="system_logs")

    __table_args__ = (
        # 최신 로그 조회 (기본 정렬)
        Index("idx_sl_created_at",  "created_at"),
        # 심각도 + 시간 필터
        Index("idx_sl_level_date",  "level",    "created_at"),
        # 카테고리별 통계
        Index("idx_sl_category",    "category", "created_at"),
        # 특정 아티클의 처리 이력 조회
        Index(
            "idx_sl_article_id", "article_id",
            postgresql_where=text("article_id IS NOT NULL"),
        ),
        # 특정 작업의 처리 이력 조회
        Index(
            "idx_sl_job_id", "job_id",
            postgresql_where=text("job_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SystemLog id={self.id} [{self.level!r}] "
            f"{self.category!r}:{self.event!r}>"
        )


# ═════════════════════════════════════════════════════════════
# Glossary
# ═════════════════════════════════════════════════════════════

class Glossary(Base):
    """
    한↔영 번역 용어 사전.

    AI 번역 프롬프트에 삽입되어 고유명사 표기 일관성을 확보합니다.
    예: "방탄소년단은 항상 BTS로 번역할 것"

    category 별 활용:
        ARTIST : 아티스트/그룹명 — AI 프롬프트 삽입으로 번역 통일
                 예) 방탄소년단 → BTS, 블랙핑크 → BLACKPINK
        AGENCY : 소속사명 — 공식 영문 표기 강제
                 예) 하이브 → HYBE, SM엔터테인먼트 → SM Entertainment
        EVENT  : 공연·방송·시상식명 — 현지화 vs 음역 결정
                 예) 뮤직뱅크 → Music Bank, 멜론뮤직어워드 → Melon Music Awards

    유니크 제약:
        (term_ko, category) — 같은 카테고리 내 한국어 원어 중복 방지
        → 동명이인 아티스트는 description 으로 구분

    인덱스 (0004 마이그레이션에서 생성):
        idx_glossary_trgm_ko  : term_ko 트라이그램 (부분 매칭 검색)
        idx_glossary_trgm_en  : term_en 트라이그램 (영어 원어 역방향 검색)
        idx_glossary_category : category B-tree (분류별 일괄 조회)
    """
    __tablename__ = "glossary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    term_ko: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
        comment="한국어 원어 (예: 방탄소년단, 하이브, 뮤직뱅크)",
    )
    term_en: Mapped[Optional[str]] = mapped_column(
        String(300),
        nullable=True,
        comment="영어 공식 표기 (예: BTS, HYBE, Music Bank)",
    )
    category: Mapped[GlossaryCategory] = mapped_column(
        SAEnum(GlossaryCategory, name="glossary_category_enum", create_type=False),
        nullable=False,
        comment="용어 분류 (ARTIST / AGENCY / EVENT)",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="추가 설명 (예: '7인조 보이그룹, 2013년 데뷔', 동명이인 구분용)",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # 같은 카테고리 내 한국어 원어 중복 방지
        UniqueConstraint("term_ko", "category", name="uq_glossary_term_category"),
        # 분류별 일괄 조회 (프롬프트 구성 시 category='ARTIST' 전체 로드 등)
        Index("idx_glossary_category", "category"),
        # term_ko B-tree (일치 조회용 — 트라이그램은 마이그레이션에서 op.execute())
        Index("idx_glossary_term_ko",  "term_ko"),
        # GIN Trigram 인덱스 (0004 마이그레이션에서 op.execute()로 생성)
        # idx_glossary_trgm_ko : term_ko 부분 매칭 검색
        # idx_glossary_trgm_en : term_en 역방향 검색
    )

    def __repr__(self) -> str:
        return (
            f"<Glossary id={self.id} "
            f"{self.term_ko!r} → {self.term_en!r} "
            f"[{self.category!r}]>"
        )
