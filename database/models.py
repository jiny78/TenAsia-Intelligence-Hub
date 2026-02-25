"""
database/models.py — SQLAlchemy ORM 모델

테이블:
    job_queue  — 분산 작업 큐 (scraper/db.py 의 raw SQL 과 동일 스키마)
    articles   — 정제된 아티클 (scraper/schema.py 의 스키마와 동일)

주의:
    - Alembic 마이그레이션은 이 파일의 모델을 기준으로 autogenerate 합니다.
    - 실제 DB 조작은 scraper/db.py (raw psycopg2) 를 병행 사용합니다.
    - SQLAlchemy ORM 은 복잡한 쿼리/리포트 용도로 사용합니다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMPTZ
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


# ─────────────────────────────────────────────────────────────
# JobQueue
# ─────────────────────────────────────────────────────────────

class JobQueue(Base):
    __tablename__ = "job_queue"

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True)
    job_type:     Mapped[str]           = mapped_column(String(50),  nullable=False, default="scrape")
    params:       Mapped[Optional[dict]]= mapped_column(JSONB,       nullable=False, default={})
    status:       Mapped[str]           = mapped_column(String(20),  nullable=False, default="pending")
    priority:     Mapped[int]           = mapped_column(Integer,     nullable=False, default=5)
    created_at:   Mapped[datetime]      = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    started_at:   Mapped[Optional[datetime]]  = mapped_column(TIMESTAMPTZ)
    completed_at: Mapped[Optional[datetime]]  = mapped_column(TIMESTAMPTZ)
    worker_id:    Mapped[Optional[str]] = mapped_column(String(100))
    result:       Mapped[Optional[dict]]= mapped_column(JSONB)
    error_msg:    Mapped[Optional[str]] = mapped_column(Text)
    retry_count:  Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    max_retries:  Mapped[int]           = mapped_column(Integer, nullable=False, default=3)

    # 관계
    articles: Mapped[list["Article"]] = relationship(back_populates="job")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_job_queue_status",
        ),
        Index(
            "idx_jq_pending",
            "status", "priority", "created_at",
            postgresql_where="status = 'pending'",
        ),
    )

    def __repr__(self) -> str:
        return f"<JobQueue id={self.id} type={self.job_type!r} status={self.status!r}>"


# ─────────────────────────────────────────────────────────────
# Article
# ─────────────────────────────────────────────────────────────

class Article(Base):
    __tablename__ = "articles"

    id:              Mapped[int]           = mapped_column(Integer, primary_key=True)
    source_url:      Mapped[str]           = mapped_column(Text,        nullable=False, unique=True)
    language:        Mapped[str]           = mapped_column(String(5),   nullable=False, default="kr")

    # 제목 (다국어)
    title_ko:        Mapped[Optional[str]] = mapped_column(Text)
    title_en:        Mapped[Optional[str]] = mapped_column(Text)

    # 본문 (다국어)
    body_ko:         Mapped[Optional[str]] = mapped_column(Text)
    body_en:         Mapped[Optional[str]] = mapped_column(Text)

    # 요약 (다국어)
    summary_ko:      Mapped[Optional[str]] = mapped_column(Text)
    summary_en:      Mapped[Optional[str]] = mapped_column(Text)

    # 아티스트
    artist_name_ko:  Mapped[Optional[str]] = mapped_column(String(200))
    artist_name_en:  Mapped[Optional[str]] = mapped_column(String(200))
    global_priority: Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)

    # SEO 해시태그 (배열)
    hashtags_ko:     Mapped[list[str]]     = mapped_column(ARRAY(Text), nullable=False, default=list)
    hashtags_en:     Mapped[list[str]]     = mapped_column(ARRAY(Text), nullable=False, default=list)

    # 미디어
    thumbnail_url:   Mapped[Optional[str]] = mapped_column(Text)

    # FK
    job_id:          Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("job_queue.id", ondelete="SET NULL")
    )

    # 시간
    published_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    created_at:      Mapped[datetime]           = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    updated_at:      Mapped[datetime]           = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now(), onupdate=func.now())

    # 관계
    job: Mapped[Optional["JobQueue"]] = relationship(back_populates="articles")

    __table_args__ = (
        CheckConstraint(
            "language IN ('kr','en','jp')",
            name="ck_articles_language",
        ),
        # 전문 검색 인덱스 (GIN)는 Alembic 버전 파일에서 op.execute()로 생성
    )

    def __repr__(self) -> str:
        return f"<Article id={self.id} title_ko={self.title_ko!r:.30}>"
