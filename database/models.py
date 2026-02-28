"""
database/models.py — SQLAlchemy ORM 모델 (v2 — Evidence-based)

테이블:
    job_queue          — 분산 작업 큐 (SKIP LOCKED 패턴)
    artists            — 솔로 아티스트 마스터 (증거 기반 필드 추적)
    groups             — 그룹/밴드 마스터 (증거 기반 필드 추적)
    member_of          — 아티스트 ↔ 그룹 활동 이력 (유닛/메인 그룹 포함)
    artist_educations  — 아티스트 학력 이력 (1:N)
    artist_sns         — 아티스트 SNS 계정 (플랫폼별 분리)
    group_sns          — 그룹 SNS 계정 (플랫폼별 분리)
    data_update_logs   — 기사 → 엔티티 필드 업데이트 감사 로그 (The Core)
    articles           — 수집·정제된 아티클 (다국어)
    article_images     — 아티클 첨부 이미지 1:N
    entity_mappings    — 아티클 ↔ 아티스트/그룹/이벤트 연결
    system_logs        — 스크래핑·AI 처리 이력 (append-only)
    glossary           — 한↔영 번역 용어 사전

설계 원칙:
    ─ 증거 기반(Evidence-based) ───────────────────────────────────
      · 모든 갱신 가능한 프로필 필드에 *_source_article_id FK 추가.
        "어떤 기사에서 이 값이 왔는지" 필드 단위로 추적.
      · DataUpdateLog: 하나의 기사가 N개 엔티티 × M개 필드를
        동시에 업데이트한 이력을 완전히 기록 (The Core).

    ─ 입체적 인물 구조 ────────────────────────────────────────────
      · Artist (솔로) + Group (그룹/밴드/유닛) 테이블 완전 분리.
      · MemberOf 로 솔로 활동 이력(시작일·종료일·역할) 추적.

    ─ Deep Metadata ───────────────────────────────────────────────
      · MBTI, 혈액형, 국적, 신장·체중 (Artist)
      · 팬덤명, 소속사, 활동 상태 (Group)
      · 학력 이력 1:N (ArtistEducation)
      · SNS 플랫폼별 계정 1:N (ArtistSNS / GroupSNS)

    ─ 이중 언어 ───────────────────────────────────────────────────
      · 모든 표시 텍스트 필드 _ko / _en 쌍으로 저장.

    ─ PostgreSQL 전용 ──────────────────────────────────────────────
      · ENUM, JSONB, ARRAY, TSVECTOR, GIN/Trigram 인덱스
      · 공통: created_at / updated_at (TIMESTAMPTZ, 트리거 자동 갱신)
      · append-only 테이블(system_logs, data_update_logs)은 created_at 만

Alembic autogenerate 기준 파일 — 모델 변경 후 alembic revision --autogenerate
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

# PostgreSQL TIMESTAMP WITH TIME ZONE 편의 별칭
TIMESTAMPTZ = DateTime(timezone=True)

from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


# ═════════════════════════════════════════════════════════════
# Python Enum 정의 (PostgreSQL ENUM 과 1:1 대응)
# ═════════════════════════════════════════════════════════════

# ── 기존 Enum (변경 없음) ──────────────────────────────────────

class ProcessStatus(str, enum.Enum):
    """아티클 처리 상태 — articles.process_status"""
    PENDING       = "PENDING"        # 수집 대기
    SCRAPED       = "SCRAPED"        # HTML 수집 완료, AI 처리 대기
    PROCESSED     = "PROCESSED"      # Gemini AI 정제 완료
    VERIFIED      = "VERIFIED"       # [Phase 4-B] 신뢰도 ≥ 0.95 자동 승인 (운영자 확인 불필요)
    ERROR         = "ERROR"          # 처리 실패 (system_logs 참조)
    MANUAL_REVIEW = "MANUAL_REVIEW"  # AI 신뢰도 낮음 — 사람이 검토 필요


class EntityType(str, enum.Enum):
    """엔티티 유형 — entity_mappings.entity_type / data_update_logs.entity_type"""
    ARTIST = "ARTIST"  # 솔로 아티스트 → artists.id
    GROUP  = "GROUP"   # 그룹/밴드 → groups.id
    EVENT  = "EVENT"   # 공연/시상식 (미래 events 테이블 예정)


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


# ── 신규 Enum ──────────────────────────────────────────────────

class ArtistGender(str, enum.Enum):
    """
    성별 — artists.gender, groups.gender
    그룹의 경우 MIXED(혼성)를 사용합니다.
    """
    MALE    = "MALE"
    FEMALE  = "FEMALE"
    MIXED   = "MIXED"    # 혼성 그룹
    UNKNOWN = "UNKNOWN"


class MemberRole(str, enum.Enum):
    """
    아티스트 역할 — member_of.roles 배열 내 값.
    한 멤버는 복수 역할 보유 가능 (예: VOCALIST + RAPPER).
    """
    VOCALIST = "VOCALIST"  # 보컬
    RAPPER   = "RAPPER"    # 래퍼
    DANCER   = "DANCER"    # 댄서
    LEADER   = "LEADER"    # 리더
    VISUAL   = "VISUAL"    # 비주얼
    MAKNAE   = "MAKNAE"    # 막내
    CENTER   = "CENTER"    # 센터
    ACTOR    = "ACTOR"     # 배우 겸업
    MC       = "MC"        # MC 겸업
    OTHER    = "OTHER"     # 기타


class ActivityStatus(str, enum.Enum):
    """
    그룹 활동 상태 — groups.activity_status.
    솔로 아티스트의 그룹 활동 종료는 member_of.ended_on 으로 표현합니다.
    """
    ACTIVE    = "ACTIVE"     # 현재 활동 중
    HIATUS    = "HIATUS"     # 활동 중단 (공백기)
    DISBANDED = "DISBANDED"  # 해체
    SOLO_ONLY = "SOLO_ONLY"  # 솔로 활동만 (그룹 공식 해체 아님)


class SNSPlatform(str, enum.Enum):
    """SNS 플랫폼 — artist_sns.platform, group_sns.platform"""
    INSTAGRAM = "INSTAGRAM"
    TWITTER_X = "TWITTER_X"  # Twitter (현 X)
    YOUTUBE   = "YOUTUBE"
    TIKTOK    = "TIKTOK"
    WEVERSE   = "WEVERSE"    # Weverse
    VLIVE     = "VLIVE"      # V LIVE (서비스 종료, 레거시 데이터)
    FACEBOOK  = "FACEBOOK"
    THREADS   = "THREADS"
    BLUESKY   = "BLUESKY"
    WEIBO     = "WEIBO"      # 웨이보 (중국)
    OTHER     = "OTHER"


class EducationLevel(str, enum.Enum):
    """학력 — artist_educations.education_level"""
    MIDDLE_SCHOOL = "MIDDLE_SCHOOL"  # 중학교
    HIGH_SCHOOL   = "HIGH_SCHOOL"    # 고등학교
    UNIVERSITY    = "UNIVERSITY"     # 대학교
    GRADUATE      = "GRADUATE"       # 대학원
    DROPOUT       = "DROPOUT"        # 중퇴


class ResolutionType(str, enum.Enum):
    """
    AI 자율 결정 유형 — auto_resolution_logs.resolution_type

    FILL      : DB 에 비어있던 필드를 기사에서 추출한 값으로 보충
    RECONCILE : DB 기존 값과 기사 추출 값의 모순을 Gemini 판단으로 해결
    ENROLL    : glossary 에 신규 용어를 Auto-Provisioned 상태로 등록
    """
    FILL      = "FILL"      # 빈 필드 자동 보충
    RECONCILE = "RECONCILE" # 모순 자동 해결 (Gemini 2차 판단)
    ENROLL    = "ENROLL"    # 신규 용어 Glossary 자동 등록


class ConflictStatus(str, enum.Enum):
    """
    모순 플래그 처리 상태 — conflict_flags.status

    OPEN      : 미해결 (운영자 검토 필요)
    RESOLVED  : 운영자 또는 시스템이 해결 완료
    DISMISSED : 무시(사소한 오류 또는 중복)로 판정하여 닫음
    """
    OPEN      = "OPEN"      # 미해결
    RESOLVED  = "RESOLVED"  # 해결 완료
    DISMISSED = "DISMISSED" # 무시 처리


# ═════════════════════════════════════════════════════════════
# JobQueue (변경 없음)
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
        Index(
            "idx_jq_pending",
            "status", "priority", "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<JobQueue id={self.id} type={self.job_type!r} status={self.status!r}>"


# ═════════════════════════════════════════════════════════════
# Artist — 솔로 아티스트
# ═════════════════════════════════════════════════════════════

class Artist(Base):
    """
    솔로 아티스트 마스터 데이터 (v1 Artist 에서 GROUP 타입 분리).

    증거 기반(Evidence-based) 필드 패턴:
        각 갱신 가능한 프로필 필드에 *_source_article_id FK 를 추가.
        "어떤 기사에서 이 값이 확인/수정되었는지" 필드 단위로 추적합니다.

        birth_date               ← birth_date_source_article_id
        nationality_ko / _en     ← nationality_source_article_id
        mbti                     ← mbti_source_article_id
        blood_type               ← blood_type_source_article_id
        height_cm / weight_kg    ← body_source_article_id  (체형은 동일 기사)
        bio_ko                   ← bio_ko_source_article_id
        bio_en                   ← bio_en_source_article_id

    전체 수정 이력은 DataUpdateLog 테이블에서 조회합니다.

    GIN Trigram 인덱스 (마이그레이션에서 op.execute()로 생성):
        idx_artists_trgm_name_ko  — 오타·부분 일치 이름 검색
        idx_artists_trgm_name_en  — 영어명 부분 일치 검색
        idx_artists_trgm_bio_ko   — 소개글 검색
    """
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── 이름 (다국어) ──────────────────────────────────────────
    name_ko:       Mapped[str]           = mapped_column(
        String(200), nullable=False, comment="활동명 또는 본명 (한국어)"
    )
    name_en:       Mapped[Optional[str]] = mapped_column(
        String(200), comment="활동명 또는 본명 (영어)"
    )
    stage_name_ko: Mapped[Optional[str]] = mapped_column(
        String(200), comment="무대 활동명 (본명과 다를 때만 입력)"
    )
    stage_name_en: Mapped[Optional[str]] = mapped_column(
        String(200), comment="무대 활동명 (영어)"
    )

    # ── 기본 프로필 (증거 기반) ─────────────────────────────────
    gender: Mapped[Optional[ArtistGender]] = mapped_column(
        SAEnum(ArtistGender, name="artist_gender_enum", create_type=False),
    )

    birth_date:                   Mapped[Optional[date]] = mapped_column(Date, comment="생년월일")
    birth_date_source_article_id: Mapped[Optional[int]]  = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="birth_date 출처 기사",
    )

    nationality_ko:                   Mapped[Optional[str]] = mapped_column(String(100), comment="국적 (한국어, 예: 한국)")
    nationality_en:                   Mapped[Optional[str]] = mapped_column(String(100), comment="국적 (영어, 예: South Korea)")
    nationality_source_article_id:    Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="nationality 출처 기사",
    )

    # ── Deep Metadata (증거 기반) ──────────────────────────────
    mbti:                   Mapped[Optional[str]] = mapped_column(String(4),  comment="MBTI 유형 (예: INFP)")
    mbti_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="mbti 출처 기사",
    )

    blood_type:                   Mapped[Optional[str]] = mapped_column(String(3), comment="혈액형 (예: AB)")
    blood_type_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="blood_type 출처 기사",
    )

    height_cm:               Mapped[Optional[float]] = mapped_column(Float, comment="신장 (cm)")
    weight_kg:               Mapped[Optional[float]] = mapped_column(Float, comment="체중 (kg)")
    body_source_article_id:  Mapped[Optional[int]]   = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="height_cm / weight_kg 출처 기사 (체형 정보는 주로 동일 기사에서 수집)",
    )

    # ── 소개글 (다국어, 증거 기반) ──────────────────────────────
    bio_ko:                   Mapped[Optional[str]] = mapped_column(Text, comment="아티스트 소개글 (한국어)")
    bio_ko_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="bio_ko 출처 기사",
    )
    bio_en:                   Mapped[Optional[str]] = mapped_column(Text, comment="아티스트 소개글 (영어)")
    bio_en_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="bio_en 출처 기사",
    )

    # ── 번역 우선순위 & 검증 ────────────────────────────────────
    is_verified:     Mapped[bool]          = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="공식 확인된 아티스트 여부",
    )
    global_priority: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="번역 우선순위: 1=전체번역, 2=요약만, 3=번역제외, NULL=미분류",
    )

    # ── [Phase 2-D] Self-Healing 메타 ──────────────────────────
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True,
        comment="[Phase 2-D] AI 시스템이 이 아티스트의 데이터를 마지막으로 재검증한 시점.",
    )
    data_reliability_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "[Phase 2-D] 이 아티스트 데이터의 누적 신뢰도 점수 (0.0~1.0). "
            "높을수록 여러 고신뢰도 기사에서 검증된 데이터."
        ),
    )

    # ── Gemini 보강 추적 ────────────────────────────────────────
    enriched_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True,
        comment="Gemini 프로필 보강 완료 시각. NULL=미보강(보강 대상), NOT NULL=보강 완료(스킵)",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    member_of:       Mapped[list["MemberOf"]]        = relationship(
        back_populates="artist", cascade="all, delete-orphan",
    )
    sns_accounts:    Mapped[list["ArtistSNS"]]        = relationship(
        back_populates="artist", cascade="all, delete-orphan",
    )
    educations:      Mapped[list["ArtistEducation"]]  = relationship(
        back_populates="artist", cascade="all, delete-orphan",
    )
    entity_mappings: Mapped[list["EntityMapping"]]    = relationship(
        back_populates="artist",
        foreign_keys="EntityMapping.artist_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "global_priority IS NULL OR global_priority IN (1, 2, 3)",
            name="ck_artists_global_priority",
        ),
        CheckConstraint(
            "mbti IS NULL OR (length(mbti) = 4 AND mbti ~ '^[A-Z]{4}$')",
            name="ck_artists_mbti",
        ),
        Index("idx_artists_name_ko",       "name_ko"),
        Index("idx_artists_name_en",       "name_en"),
        Index("idx_artists_is_verified",   "is_verified"),
        Index(
            "idx_artists_global_priority", "global_priority",
            postgresql_where=text("global_priority IS NOT NULL"),
        ),
        # GIN Trigram (마이그레이션에서 op.execute()로 생성)
        # idx_artists_trgm_name_ko, idx_artists_trgm_name_en, idx_artists_trgm_bio_ko
    )

    def __repr__(self) -> str:
        return f"<Artist id={self.id} name_ko={self.name_ko!r} verified={self.is_verified}>"


# ═════════════════════════════════════════════════════════════
# Group — 그룹/밴드/유닛
# ═════════════════════════════════════════════════════════════

class Group(Base):
    """
    그룹/밴드/유닛 마스터 데이터.

    v1 의 Artist(GROUP 타입)에서 분리된 독립 테이블.
    멤버 구성 및 활동 이력은 MemberOf 테이블로 관리합니다.

    증거 기반(Evidence-based) 필드 패턴:
        debut_date               ← debut_date_source_article_id
        label_ko / _en           ← label_source_article_id
        fandom_name_ko / _en     ← fandom_name_source_article_id
        activity_status          ← activity_status_source_article_id
        bio_ko                   ← bio_ko_source_article_id
        bio_en                   ← bio_en_source_article_id

    GIN Trigram 인덱스 (마이그레이션에서 op.execute()로 생성):
        idx_groups_trgm_name_ko  — 그룹명 부분 일치 검색
        idx_groups_trgm_name_en  — 그룹 영어명 부분 일치 검색
    """
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── 이름 (다국어) ──────────────────────────────────────────
    name_ko: Mapped[str]           = mapped_column(String(200), nullable=False, comment="그룹명 (한국어)")
    name_en: Mapped[Optional[str]] = mapped_column(String(200), comment="그룹명 (영어)")

    # ── 기본 프로필 ─────────────────────────────────────────────
    gender: Mapped[Optional[ArtistGender]] = mapped_column(
        SAEnum(ArtistGender, name="artist_gender_enum", create_type=False),
        comment="혼성=MIXED, 여성=FEMALE, 남성=MALE",
    )

    debut_date:                   Mapped[Optional[date]] = mapped_column(Date, comment="데뷔일")
    debut_date_source_article_id: Mapped[Optional[int]]  = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="debut_date 출처 기사",
    )

    # ── 소속사 (다국어, 증거 기반) ─────────────────────────────
    label_ko:                 Mapped[Optional[str]] = mapped_column(String(200), comment="소속사명 (한국어, 예: 하이브)")
    label_en:                 Mapped[Optional[str]] = mapped_column(String(200), comment="소속사명 (영어, 예: HYBE)")
    label_source_article_id:  Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="label 출처 기사",
    )

    # ── 팬덤명 (다국어, 증거 기반) ─────────────────────────────
    fandom_name_ko:                Mapped[Optional[str]] = mapped_column(String(100), comment="팬덤명 (한국어, 예: 아미)")
    fandom_name_en:                Mapped[Optional[str]] = mapped_column(String(100), comment="팬덤명 (영어, 예: ARMY)")
    fandom_name_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="fandom_name 출처 기사",
    )

    # ── 활동 상태 (증거 기반) ──────────────────────────────────
    activity_status: Mapped[Optional[ActivityStatus]] = mapped_column(
        SAEnum(ActivityStatus, name="activity_status_enum", create_type=False),
        comment="그룹 현재 활동 상태",
    )
    activity_status_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="activity_status 출처 기사",
    )

    # ── 소개글 (다국어, 증거 기반) ──────────────────────────────
    bio_ko:                   Mapped[Optional[str]] = mapped_column(Text, comment="그룹 소개글 (한국어)")
    bio_ko_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="bio_ko 출처 기사",
    )
    bio_en:                   Mapped[Optional[str]] = mapped_column(Text, comment="그룹 소개글 (영어)")
    bio_en_source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="bio_en 출처 기사",
    )

    # ── 번역 우선순위 & 검증 ────────────────────────────────────
    is_verified:     Mapped[bool]          = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="공식 확인된 그룹 여부",
    )
    global_priority: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="번역 우선순위: 1=전체번역, 2=요약만, 3=번역제외, NULL=미분류",
    )

    # ── [Phase 2-D] Self-Healing 메타 ──────────────────────────
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True,
        comment="[Phase 2-D] AI 시스템이 이 그룹의 데이터를 마지막으로 재검증한 시점.",
    )
    data_reliability_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "[Phase 2-D] 이 그룹 데이터의 누적 신뢰도 점수 (0.0~1.0). "
            "높을수록 여러 고신뢰도 기사에서 검증된 데이터."
        ),
    )

    # ── Gemini 보강 추적 ────────────────────────────────────────
    enriched_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True,
        comment="Gemini 프로필 보강 완료 시각. NULL=미보강(보강 대상), NOT NULL=보강 완료(스킵)",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    members:         Mapped[list["MemberOf"]]       = relationship(
        back_populates="group", cascade="all, delete-orphan",
    )
    sns_accounts:    Mapped[list["GroupSNS"]]        = relationship(
        back_populates="group", cascade="all, delete-orphan",
    )
    entity_mappings: Mapped[list["EntityMapping"]]   = relationship(
        back_populates="group",
        foreign_keys="EntityMapping.group_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "global_priority IS NULL OR global_priority IN (1, 2, 3)",
            name="ck_groups_global_priority",
        ),
        Index("idx_groups_name_ko",         "name_ko"),
        Index("idx_groups_name_en",         "name_en"),
        Index("idx_groups_is_verified",     "is_verified"),
        Index("idx_groups_activity_status", "activity_status"),
        Index(
            "idx_groups_global_priority", "global_priority",
            postgresql_where=text("global_priority IS NOT NULL"),
        ),
        # GIN Trigram (마이그레이션에서 op.execute()로 생성)
        # idx_groups_trgm_name_ko, idx_groups_trgm_name_en
    )

    def __repr__(self) -> str:
        return f"<Group id={self.id} name_ko={self.name_ko!r} status={self.activity_status!r}>"


# ═════════════════════════════════════════════════════════════
# MemberOf — 아티스트 ↔ 그룹 활동 이력
# ═════════════════════════════════════════════════════════════

class MemberOf(Base):
    """
    아티스트 ↔ 그룹 활동 이력 (정션 테이블).

    활용 시나리오:
        · 메인 그룹 소속 (is_sub_unit=False)
        · 유닛/서브유닛 활동 (is_sub_unit=True, group_id → 유닛 Group 행)
        · 탈퇴 이력 추적 (ended_on 설정)
        · 재가입 (동일 artist_id + group_id 의 복수 레코드 허용)

    ended_on 규칙:
        NULL  → 현재 활동 중
        date  → 해당 날짜에 탈퇴·활동 종료

    roles (ARRAY of MemberRole):
        ['VOCALIST', 'RAPPER'] — 복수 역할 허용
        빈 배열 []              — 역할 미상/미입력
    """
    __tablename__ = "member_of"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── FK ────────────────────────────────────────────────────
    artist_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False,
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
    )

    # ── 역할 (복수 허용) ────────────────────────────────────────
    roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
        comment="MemberRole 값 배열 (예: ['VOCALIST', 'RAPPER'])",
    )

    # ── 활동 기간 ──────────────────────────────────────────────
    started_on: Mapped[Optional[date]] = mapped_column(Date, comment="소속 시작일 (데뷔일 등). NULL=미상")
    ended_on:   Mapped[Optional[date]] = mapped_column(Date, comment="소속 종료일. NULL=현재 활동 중")

    # ── 유닛 여부 ──────────────────────────────────────────────
    is_sub_unit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="True=유닛·서브유닛 활동 (False=메인 그룹 소속)",
    )

    # ── 증거 기반 ──────────────────────────────────────────────
    source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="이 멤버십 정보의 출처 기사",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    artist:         Mapped["Artist"]            = relationship(back_populates="member_of")
    group:          Mapped["Group"]             = relationship(back_populates="members")
    source_article: Mapped[Optional["Article"]] = relationship(foreign_keys=[source_article_id])

    __table_args__ = (
        CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_mo_date_order",
        ),
        Index("idx_mo_artist_id", "artist_id"),
        Index("idx_mo_group_id",  "group_id"),
        # 현재 활동 중인 멤버만 조회 (ended_on IS NULL 부분 인덱스)
        Index(
            "idx_mo_active",
            "group_id", "artist_id",
            postgresql_where=text("ended_on IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        status = "active" if self.ended_on is None else f"ended={self.ended_on}"
        return f"<MemberOf artist={self.artist_id} → group={self.group_id} [{status}]>"


# ═════════════════════════════════════════════════════════════
# ArtistEducation — 아티스트 학력 이력
# ═════════════════════════════════════════════════════════════

class ArtistEducation(Base):
    """
    아티스트 학력 이력 (1:N).

    한 아티스트의 학력이 여러 레코드로 기록될 수 있습니다.
    (예: 고등학교 졸업 확인 → 나중에 대학 재학 추가 확인)
    모든 레코드는 source_article_id 로 출처 기사를 추적합니다.
    """
    __tablename__ = "artist_educations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── FK ────────────────────────────────────────────────────
    artist_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False,
    )

    # ── 학교 정보 ──────────────────────────────────────────────
    school_name_ko:  Mapped[str]           = mapped_column(String(300), nullable=False, comment="학교명 (한국어)")
    school_name_en:  Mapped[Optional[str]] = mapped_column(String(300), comment="학교명 (영어)")
    education_level: Mapped[EducationLevel] = mapped_column(
        SAEnum(EducationLevel, name="education_level_enum", create_type=False),
        nullable=False,
    )
    graduated_year: Mapped[Optional[int]] = mapped_column(Integer, comment="졸업 연도 (4자리)")

    # ── 증거 기반 ──────────────────────────────────────────────
    source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="이 학력 정보의 출처 기사",
    )

    # ── 시간 (append-only 성격이나 수정 가능) ─────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    artist:         Mapped["Artist"]            = relationship(back_populates="educations")
    source_article: Mapped[Optional["Article"]] = relationship(foreign_keys=[source_article_id])

    __table_args__ = (
        Index("idx_ae_artist_id",        "artist_id"),
        Index("idx_ae_education_level",  "education_level"),
    )

    def __repr__(self) -> str:
        return (
            f"<ArtistEducation artist={self.artist_id} "
            f"{self.education_level!r} {self.school_name_ko!r}>"
        )


# ═════════════════════════════════════════════════════════════
# ArtistSNS — 아티스트 SNS 계정 (플랫폼별)
# ═════════════════════════════════════════════════════════════

class ArtistSNS(Base):
    """
    아티스트 SNS 계정 (플랫폼별 분리, 1:N).

    UniqueConstraint(artist_id, platform) — 아티스트당 플랫폼 하나.
    follower_count 는 주기적으로 갱신되며 source_article_id 도 함께 업데이트합니다.
    """
    __tablename__ = "artist_sns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── FK ────────────────────────────────────────────────────
    artist_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False,
    )

    # ── 플랫폼 정보 ────────────────────────────────────────────
    platform:       Mapped[SNSPlatform]  = mapped_column(
        SAEnum(SNSPlatform, name="sns_platform_enum", create_type=False),
        nullable=False,
    )
    url:            Mapped[Optional[str]] = mapped_column(Text, comment="계정 URL")
    handle:         Mapped[Optional[str]] = mapped_column(String(200), comment="계정 핸들 (예: @BTS_twt)")
    follower_count: Mapped[Optional[int]] = mapped_column(BigInteger,  comment="팔로워 수 (최근 수집 기준)")

    # ── 증거 기반 ──────────────────────────────────────────────
    source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="이 SNS 정보의 출처 기사",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    artist:         Mapped["Artist"]            = relationship(back_populates="sns_accounts")
    source_article: Mapped[Optional["Article"]] = relationship(foreign_keys=[source_article_id])

    __table_args__ = (
        UniqueConstraint("artist_id", "platform", name="uq_artist_sns_platform"),
        Index("idx_asns_artist_id", "artist_id"),
        Index("idx_asns_platform",  "platform"),
    )

    def __repr__(self) -> str:
        return f"<ArtistSNS artist={self.artist_id} {self.platform!r} handle={self.handle!r}>"


# ═════════════════════════════════════════════════════════════
# GroupSNS — 그룹 SNS 계정 (플랫폼별)
# ═════════════════════════════════════════════════════════════

class GroupSNS(Base):
    """
    그룹 SNS 계정 (플랫폼별 분리, 1:N).
    ArtistSNS 와 동일한 구조, group_id FK 로 구분합니다.
    """
    __tablename__ = "group_sns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── FK ────────────────────────────────────────────────────
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
    )

    # ── 플랫폼 정보 ────────────────────────────────────────────
    platform:       Mapped[SNSPlatform]   = mapped_column(
        SAEnum(SNSPlatform, name="sns_platform_enum", create_type=False),
        nullable=False,
    )
    url:            Mapped[Optional[str]] = mapped_column(Text, comment="계정 URL")
    handle:         Mapped[Optional[str]] = mapped_column(String(200), comment="계정 핸들 (예: @BLACKPINK)")
    follower_count: Mapped[Optional[int]] = mapped_column(BigInteger,  comment="팔로워 수 (최근 수집 기준)")

    # ── 증거 기반 ──────────────────────────────────────────────
    source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="이 SNS 정보의 출처 기사",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    group:          Mapped["Group"]             = relationship(back_populates="sns_accounts")
    source_article: Mapped[Optional["Article"]] = relationship(foreign_keys=[source_article_id])

    __table_args__ = (
        UniqueConstraint("group_id", "platform", name="uq_group_sns_platform"),
        Index("idx_gsns_group_id", "group_id"),
        Index("idx_gsns_platform", "platform"),
    )

    def __repr__(self) -> str:
        return f"<GroupSNS group={self.group_id} {self.platform!r} handle={self.handle!r}>"


# ═════════════════════════════════════════════════════════════
# DataUpdateLog — 기사 → 엔티티 필드 업데이트 감사 로그 (The Core)
# ═════════════════════════════════════════════════════════════

class DataUpdateLog(Base):
    """
    기사 → 엔티티 필드 업데이트 감사 로그 (The Core).

    설계 의도:
        하나의 기사(article_id)에서 여러 아티스트·그룹의 여러 필드를
        동시에 업데이트할 수 있습니다. 이 테이블이 완전한 데이터 provenance 를
        기록합니다.

        "아이유의 MBTI가 INFP 라는 정보는 2026-02-25 기사(id=1042)에서
         AI 파이프라인이 추출했으며, 이전 값은 NULL 이었다."
        → {article_id=1042, entity_type=ARTIST, entity_id=7,
           field_name='mbti', old_value_json=null,
           new_value_json={"value": "INFP"}, updated_by='ai_pipeline'}

    entity_type + entity_id 조합:
        ARTIST → artists.id 참조
        GROUP  → groups.id 참조

    field_name 예시:
        아티스트: "birth_date", "mbti", "blood_type", "height_cm",
                  "weight_kg", "nationality_ko", "nationality_en",
                  "bio_ko", "bio_en"
        그룹:     "debut_date", "fandom_name_ko", "fandom_name_en",
                  "label_ko", "label_en", "activity_status",
                  "bio_ko", "bio_en"

    updated_by 값:
        "ai_pipeline" — Gemini AI 파이프라인 (기본값)
        "manual"      — 사람이 직접 수정
        "scraper"     — 스크래퍼 자동 추출

    append-only: 이 테이블의 기존 행은 수정하지 않습니다.
    """
    __tablename__ = "data_update_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ── 출처 기사 (The Core) ────────────────────────────────────
    article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"),
        comment="업데이트 출처 기사. 기사 삭제 시 NULL(이력 자체는 보존)",
    )

    # ── 대상 엔티티 ─────────────────────────────────────────────
    entity_type: Mapped[EntityType] = mapped_column(
        SAEnum(EntityType, name="entity_type_enum", create_type=False),
        nullable=False,
        comment="ARTIST=artists.id 참조, GROUP=groups.id 참조",
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="artists.id 또는 groups.id (entity_type 에 따라 결정)",
    )

    # ── 변경 내역 ──────────────────────────────────────────────
    field_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="변경된 필드명 (예: birth_date, mbti, fandom_name_ko)",
    )
    old_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, comment='변경 전 값. NULL=최초 입력. 예: {"value": "INTJ"}',
    )
    new_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, comment='변경 후 값. 예: {"value": "INFP"}',
    )

    # ── 변경 주체 ──────────────────────────────────────────────
    updated_by: Mapped[str] = mapped_column(
        String(50), nullable=False, default="ai_pipeline",
        comment="변경 주체: ai_pipeline | manual | scraper",
    )

    # ── 시간 (append-only — updated_at 없음) ──────────────────
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(),
    )

    # ── 관계 ──────────────────────────────────────────────────
    article: Mapped[Optional["Article"]] = relationship(foreign_keys=[article_id])

    __table_args__ = (
        Index(
            "idx_dul_article_id", "article_id",
            postgresql_where=text("article_id IS NOT NULL"),
        ),
        Index("idx_dul_entity",       "entity_type", "entity_id"),
        Index("idx_dul_entity_field", "entity_type", "entity_id", "field_name"),
        Index("idx_dul_created_at",   "created_at"),
        Index("idx_dul_field_name",   "field_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<DataUpdateLog id={self.id} "
            f"{self.entity_type!r}:{self.entity_id} "
            f"field={self.field_name!r} by={self.updated_by!r}>"
        )


# ═════════════════════════════════════════════════════════════
# Article (변경 없음)
# ═════════════════════════════════════════════════════════════

class Article(Base):
    """
    수집·정제된 아티클.

    수명 주기:
        PENDING → (워커 스크래핑) → SCRAPED
                → (Gemini AI 정제) → PROCESSED
                → (실패 시)        → ERROR

    GIN/Trigram 인덱스 (0001, 0003~0006 마이그레이션에서 생성):
        - to_tsvector FTS: title_ko+body_ko (simple), title_en+body_en (english)
        - gin_trgm_ops:    title_ko, title_en, body_ko, body_en, artist_name_*
        - GIN array:       hashtags_ko, hashtags_en
        - JSONB GIN:       seo_hashtags
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

    # ── 원문 (한국어 전문) ────────────────────────────────────
    content_ko: Mapped[Optional[str]] = mapped_column(Text, comment="원문(한국어) 전체 본문")

    # ── 요약 (다국어) ─────────────────────────────────────────
    summary_ko: Mapped[Optional[str]] = mapped_column(Text)
    summary_en: Mapped[Optional[str]] = mapped_column(Text)

    # ── 전문 검색 벡터 (PostgreSQL TSVECTOR) ─────────────────
    # trg_update_article_search_vector 트리거(INSERT/UPDATE)가 자동 갱신
    # 가중치: A=title, B=summary, C=content
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR, nullable=True,
        comment="다국어 FTS 벡터 (트리거 자동 갱신). GIN 인덱스: idx_articles_search_vector",
    )

    # ── 저자 및 원문 메타 ─────────────────────────────────────
    author:       Mapped[Optional[str]]      = mapped_column(String(200))
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    # ── 아티스트 (비정규화, 빠른 필터링용) ────────────────────
    artist_name_ko:  Mapped[Optional[str]] = mapped_column(String(200))
    artist_name_en:  Mapped[Optional[str]] = mapped_column(String(200))
    global_priority: Mapped[bool]          = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # ── SEO 해시태그 ─────────────────────────────────────────
    hashtags_ko: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::text[]"),
    )
    hashtags_en: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::text[]"),
    )
    seo_hashtags: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="AI 생성 영어 SEO 해시태그 (생성 모델·신뢰도·카테고리 메타데이터 포함)",
    )

    # ── 미디어 ────────────────────────────────────────────────
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text)

    # ── 감성 분류 ─────────────────────────────────────────────
    sentiment: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True,
        comment="Gemini AI 감성 분류: POSITIVE/NEGATIVE/NEUTRAL/NULL(미처리)",
    )

    # ── FK ────────────────────────────────────────────────────
    job_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_queue.id", ondelete="SET NULL"),
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    job:             Mapped[Optional["JobQueue"]]   = relationship(back_populates="articles")
    images:          Mapped[list["ArticleImage"]]   = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        order_by="ArticleImage.is_representative.desc(), ArticleImage.id.asc()",
    )
    entity_mappings: Mapped[list["EntityMapping"]]  = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )
    system_logs:     Mapped[list["SystemLog"]]      = relationship(back_populates="article")

    __table_args__ = (
        CheckConstraint("language IN ('kr','en','jp')", name="ck_articles_language"),
        Index("idx_articles_process_status",  "process_status", "created_at"),
        Index("idx_articles_status_priority", "process_status", "global_priority", "created_at"),
        Index("idx_articles_global_flag",     "global_priority", "created_at",
              postgresql_where=text("global_priority = true")),
        Index("idx_articles_language_date",   "language", "created_at"),
        Index(
            "idx_articles_manual_review", "created_at",
            postgresql_where=text("process_status = 'MANUAL_REVIEW'"),
        ),
    )

    def __repr__(self) -> str:
        title = str(self.title_ko or "")[:30]
        return f"<Article id={self.id} status={self.process_status!r} title_ko={title!r}>"


# ═════════════════════════════════════════════════════════════
# ArticleImage (변경 없음)
# ═════════════════════════════════════════════════════════════

class ArticleImage(Base):
    """
    아티클 첨부 이미지 (articles 와 1:N).

    S3 경로 규칙 (thumbnail_path):
        형식: {env}/{artist_name_en}/{article_id}/{image_id}_thumb.webp
        예시: prod/BTS/1042/7_thumb.webp
        NULL: S3 업로드 전(스크래핑 직후) 또는 업로드 실패 상태
    """
    __tablename__ = "article_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── FK ────────────────────────────────────────────────────
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False,
        comment="소속 아티클 (기사 삭제 시 이미지 레코드도 자동 삭제)",
    )

    # ── 이미지 경로 ────────────────────────────────────────────
    original_url: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="스크래퍼가 수집한 원본 이미지 URL (UNIQUE)",
    )
    thumbnail_path: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="S3 썸네일 저장 경로. NULL=업로드 전/실패",
    )

    # ── 대표 이미지 여부 ───────────────────────────────────────
    is_representative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="True 인 행은 기사당 1개 권장 — Article.thumbnail_url 과 동기화",
    )

    # ── 접근성 ────────────────────────────────────────────────
    alt_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="이미지 대체 텍스트 (웹 접근성 / SEO)",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    article: Mapped["Article"] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("original_url", name="uq_article_images_url"),
        Index("idx_ai_article_id", "article_id"),
        Index(
            "idx_ai_representative", "article_id",
            postgresql_where=text("is_representative = true"),
        ),
        Index(
            "idx_ai_pending_upload", "article_id", "created_at",
            postgresql_where=text("thumbnail_path IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        flag = " [REP]" if self.is_representative else ""
        return (
            f"<ArticleImage id={self.id} article={self.article_id}"
            f"{flag} url={str(self.original_url)[:50]!r}>"
        )


# ═════════════════════════════════════════════════════════════
# EntityMapping (v2 — artist_id / group_id 분리)
# ═════════════════════════════════════════════════════════════

class EntityMapping(Base):
    """
    아티클 ↔ 아티스트/그룹/이벤트 연결.

    v2 변경 사항:
        v1 의 단일 entity_id (→ artists.id 만 참조) 를
        artist_id (→ artists.id) + group_id (→ groups.id) 로 분리.
        테이블 분리 후에도 완전한 FK 참조 무결성을 유지합니다.

    entity_type 별 FK 규칙:
        ARTIST → artist_id NOT NULL, group_id IS NULL
        GROUP  → group_id NOT NULL,  artist_id IS NULL
        EVENT  → artist_id IS NULL,  group_id IS NULL  (미래 events 테이블 예정)

    confidence_score (0.0 ~ 1.0):
        1.0  : URL 또는 제목에서 명시적 언급
        0.8+ : 본문에서 여러 번 언급
        0.5~ : 간접 언급 또는 태그에서만 발견
    """
    __tablename__ = "entity_mappings"

    id:         Mapped[int]         = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int]         = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False,
    )
    entity_type: Mapped[EntityType] = mapped_column(
        SAEnum(EntityType, name="entity_type_enum", create_type=False),
        nullable=False,
    )

    # ── 분리된 FK (entity_type 에 따라 하나만 설정) ──────────
    artist_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("artists.id", ondelete="SET NULL"), nullable=True,
        comment="entity_type=ARTIST 일 때 설정. artists.id 참조",
    )
    group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True,
        comment="entity_type=GROUP 일 때 설정. groups.id 참조",
    )

    confidence_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=text("1.0"),
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    # ── 관계 ──────────────────────────────────────────────────
    article: Mapped["Article"]          = relationship(back_populates="entity_mappings")
    artist:  Mapped[Optional["Artist"]] = relationship(
        back_populates="entity_mappings", foreign_keys=[artist_id],
    )
    group:   Mapped[Optional["Group"]]  = relationship(
        back_populates="entity_mappings", foreign_keys=[group_id],
    )

    __table_args__ = (
        CheckConstraint("confidence_score BETWEEN 0.0 AND 1.0", name="ck_em_confidence"),
        # entity_type 별 FK 일관성 보장
        CheckConstraint(
            "(entity_type = 'ARTIST' AND artist_id IS NOT NULL AND group_id IS NULL) OR "
            "(entity_type = 'GROUP'  AND group_id  IS NOT NULL AND artist_id IS NULL) OR "
            "(entity_type = 'EVENT'  AND artist_id IS NULL     AND group_id  IS NULL)",
            name="ck_em_entity_fk_consistency",
        ),
        # 기사당 아티스트/그룹 중복 매핑 방지
        Index(
            "uq_em_article_artist", "article_id", "artist_id",
            unique=True,
            postgresql_where=text("artist_id IS NOT NULL"),
        ),
        Index(
            "uq_em_article_group", "article_id", "group_id",
            unique=True,
            postgresql_where=text("group_id IS NOT NULL"),
        ),
        Index("idx_em_article_id",  "article_id"),
        Index("idx_em_artist_id",   "artist_id",
              postgresql_where=text("artist_id IS NOT NULL")),
        Index("idx_em_group_id",    "group_id",
              postgresql_where=text("group_id IS NOT NULL")),
        Index("idx_em_confidence",  "confidence_score"),
    )

    def __repr__(self) -> str:
        entity = f"artist={self.artist_id}" if self.artist_id else f"group={self.group_id}"
        return (
            f"<EntityMapping article={self.article_id} "
            f"type={self.entity_type!r} {entity} "
            f"score={self.confidence_score:.2f}>"
        )


# ═════════════════════════════════════════════════════════════
# SystemLog (변경 없음)
# ═════════════════════════════════════════════════════════════

class SystemLog(Base):
    """
    스크래핑·AI 처리 이력 (append-only).

    event 값 예시:
        "scrape_start", "scrape_success", "scrape_error"
        "ai_extract_start", "ai_extract_success", "ai_extract_error"
        "db_upsert_success", "s3_upload_success"

    details (JSONB) 예시:
        {"url": "...", "html_bytes": 42300, "tokens_used": 1250, "model": "gemini-2.0-flash"}
    """
    __tablename__ = "system_logs"

    id:          Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    level:       Mapped[LogLevel]       = mapped_column(
        SAEnum(LogLevel, name="log_level_enum", create_type=False),
        nullable=False, default=LogLevel.INFO, server_default="INFO",
    )
    category:    Mapped[LogCategory]    = mapped_column(
        SAEnum(LogCategory, name="log_category_enum", create_type=False),
        nullable=False,
    )
    event:       Mapped[str]            = mapped_column(String(100), nullable=False)
    message:     Mapped[str]            = mapped_column(Text, nullable=False)
    details:     Mapped[Optional[dict]] = mapped_column(JSONB)
    duration_ms: Mapped[Optional[int]]  = mapped_column(Integer, comment="처리 소요 시간 (밀리초)")

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
    article: Mapped[Optional["Article"]]  = relationship(back_populates="system_logs")
    job:     Mapped[Optional["JobQueue"]] = relationship(back_populates="system_logs")

    __table_args__ = (
        Index("idx_sl_created_at", "created_at"),
        Index("idx_sl_level_date", "level",    "created_at"),
        Index("idx_sl_category",   "category", "created_at"),
        Index(
            "idx_sl_article_id", "article_id",
            postgresql_where=text("article_id IS NOT NULL"),
        ),
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
# Glossary (변경 없음)
# ═════════════════════════════════════════════════════════════

class Glossary(Base):
    """
    한↔영 번역 용어 사전.

    AI 번역 프롬프트에 삽입되어 고유명사 표기 일관성을 확보합니다.
    예: "방탄소년단은 항상 BTS로 번역할 것"

    GIN Trigram 인덱스 (마이그레이션에서 op.execute()로 생성):
        idx_glossary_trgm_ko — term_ko 부분 매칭 검색
        idx_glossary_trgm_en — term_en 역방향 검색
    """
    __tablename__ = "glossary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    term_ko: Mapped[str] = mapped_column(
        String(300), nullable=False,
        comment="한국어 원어 (예: 방탄소년단, 하이브, 뮤직뱅크)",
    )
    term_en: Mapped[Optional[str]] = mapped_column(
        String(300), nullable=True,
        comment="영어 공식 표기 (예: BTS, HYBE, Music Bank)",
    )
    category: Mapped[GlossaryCategory] = mapped_column(
        SAEnum(GlossaryCategory, name="glossary_category_enum", create_type=False),
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="추가 설명 (예: '7인조 보이그룹, 2013년 데뷔', 동명이인 구분용)",
    )

    # ── [Phase 4-B] Auto-Provisioned 상태 ─────────────────────
    is_auto_provisioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="True = Phase 4-B Smart Glossary Auto-Enroll 로 자동 등록된 용어. "
                "사람이 검토 후 False 로 전환.",
    )
    source_article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True,
        comment="최초 등록 근거 기사 (Auto-Provisioned 시 해당 기사 ID)",
    )

    # ── 시간 ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("term_ko", "category", name="uq_glossary_term_category"),
        Index("idx_glossary_category",          "category"),
        Index("idx_glossary_term_ko",           "term_ko"),
        Index(
            "idx_glossary_auto_provisioned", "created_at",
            postgresql_where=text("is_auto_provisioned = true"),
        ),
        # GIN Trigram (마이그레이션에서 op.execute()로 생성)
        # idx_glossary_trgm_ko, idx_glossary_trgm_en
    )

    def __repr__(self) -> str:
        return (
            f"<Glossary id={self.id} "
            f"{self.term_ko!r} → {self.term_en!r} "
            f"[{self.category!r}]>"
        )


# ═════════════════════════════════════════════════════════════
# AutoResolutionLog — AI 자율 결정 감사 로그 (Phase 2-D)
# ═════════════════════════════════════════════════════════════

class AutoResolutionLog(Base):
    """
    [Phase 2-D] AI 시스템의 자율 데이터 수정 이력 (append-only).

    Phase 4-B 엔진이 운영자 개입 없이 데이터를 수정하거나 등록할 때마다
    이 테이블에 한 행이 추가됩니다. DataUpdateLog 가 "무엇이 바뀌었는지"를
    기록한다면, AutoResolutionLog 는 "AI 가 왜 그 결정을 내렸는지"를 기록합니다.

    resolution_type 별 의미:
        FILL      : DB 에 비어있던 필드를 기사 추출 값으로 자동 보충
        RECONCILE : 기존 값과 추출 값의 모순을 Gemini 2차 판단으로 해결
        ENROLL    : 미등록 고유명사를 glossary 에 Auto-Provisioned 로 즉시 등록

    Phase 5-B Auto-Resolution Feed 의 원천 데이터입니다.
    """
    __tablename__ = "auto_resolution_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ── 출처 ──────────────────────────────────────────────────
    article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True,
        comment="결정의 근거가 된 기사",
    )

    # ── 대상 엔티티 ───────────────────────────────────────────
    entity_type: Mapped[EntityType] = mapped_column(
        SAEnum(EntityType, name="entity_type_enum", create_type=False),
        nullable=False,
        comment="수정 대상 엔티티 유형 (ARTIST / GROUP / EVENT)",
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="수정 대상 엔티티 PK (entity_type 에 따라 artists.id 또는 groups.id)",
    )
    field_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="수정된 필드명 (예: name_en, label_ko) 또는 'glossary' (ENROLL 시)",
    )

    # ── 변경 내용 ─────────────────────────────────────────────
    old_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="수정 전 값 ({\"value\": ...} 형태). FILL 이면 null.",
    )
    new_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="수정 후 값 ({\"value\": ...} 형태).",
    )

    # ── AI 결정 메타 ──────────────────────────────────────────
    resolution_type: Mapped[ResolutionType] = mapped_column(
        SAEnum(ResolutionType, name="auto_resolution_type_enum", create_type=False),
        nullable=False,
        comment="AI 결정 유형: FILL / RECONCILE / ENROLL",
    )
    gemini_reasoning: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Gemini 가 제시한 판단 근거 (Auto-Reconciliation 시 기록).",
    )
    gemini_confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="이 결정에 사용된 Gemini 전체 신뢰도 (0.0~1.0).",
    )
    source_reliability: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0.0"),
        comment=(
            "[Phase 2-D] 근거 기사의 신뢰도 가중치 (0.0~1.0). "
            "기사의 confidence 점수를 기반으로 설정."
        ),
    )

    # ── 시간 (append-only) ────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_arl_article_id",   "article_id",
              postgresql_where=text("article_id IS NOT NULL")),
        Index("idx_arl_entity",       "entity_type", "entity_id"),
        Index("idx_arl_created_at",   "created_at"),
        Index(
            "idx_arl_type_date", "resolution_type", "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AutoResolutionLog id={self.id} "
            f"type={self.resolution_type!r} "
            f"entity={self.entity_type!r}:{self.entity_id} "
            f"field={self.field_name!r}>"
        )


# ═════════════════════════════════════════════════════════════
# ConflictFlag — 자동 해결 불가 모순 안전망 (Phase 2-D)
# ═════════════════════════════════════════════════════════════

class ConflictFlag(Base):
    """
    [Phase 2-D] AI 가 스스로 해결하기엔 모순이 너무 큰 경우의 최소 안전망.

    Phase 4-B 엔진이 Auto-Reconciliation 시도 중 판단 불가 또는
    conflict_score ≥ 임계값인 경우에만 이 테이블에 기록합니다.

    운영자가 Phase 5-B 대시보드에서 이 목록을 검토하고
    RESOLVED 또는 DISMISSED 로 상태를 업데이트합니다.

    conflict_score 가이드:
        0.0 ~ 0.3 : 사소한 차이 (예: 대소문자, 약어 차이)
        0.3 ~ 0.7 : 중간 모순 (예: 영문명 표기 차이)
        0.7 ~ 1.0 : 심각한 모순 (예: 완전히 다른 이름, 상반된 소속사)
    """
    __tablename__ = "conflict_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── 출처 ──────────────────────────────────────────────────
    article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True,
        comment="모순을 유발한 기사",
    )

    # ── 대상 엔티티 ───────────────────────────────────────────
    entity_type: Mapped[EntityType] = mapped_column(
        SAEnum(EntityType, name="entity_type_enum", create_type=False),
        nullable=False,
    )
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # ── 충돌 내용 ─────────────────────────────────────────────
    existing_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, comment="DB 기존 값",
    )
    conflicting_value_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, comment="기사 추출 값 (기존 값과 충돌)",
    )
    conflict_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="시스템이 자율 해결을 포기한 이유 (Gemini 판단 불가 등)",
    )
    conflict_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, server_default=text("0.5"),
        comment="모순 심각도 (0.0=사소함, 1.0=매우 심각)",
    )

    # ── 처리 상태 ─────────────────────────────────────────────
    status: Mapped[ConflictStatus] = mapped_column(
        SAEnum(ConflictStatus, name="conflict_status_enum", create_type=False),
        nullable=False, default=ConflictStatus.OPEN, server_default="OPEN",
        comment="모순 처리 상태: OPEN / RESOLVED / DISMISSED",
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="해결한 주체 (운영자 ID 또는 'auto')",
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True,
        comment="해결 완료 시각",
    )

    # ── 시간 (append-only) ────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "conflict_score BETWEEN 0.0 AND 1.0",
            name="ck_cf_conflict_score",
        ),
        Index("idx_cf_status_date",  "status",    "created_at"),
        Index("idx_cf_entity",       "entity_type", "entity_id"),
        Index("idx_cf_article_id",   "article_id",
              postgresql_where=text("article_id IS NOT NULL")),
        Index(
            "idx_cf_open", "created_at",
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ConflictFlag id={self.id} "
            f"entity={self.entity_type!r}:{self.entity_id} "
            f"field={self.field_name!r} "
            f"score={self.conflict_score:.2f} status={self.status!r}>"
        )
