"""
processor/models.py — Pydantic v2 데이터 모델

스크래퍼 → 프로세서 → DB 전달 구조:

  RawArticle        : 스크래퍼가 수집한 원시 데이터
  ArticleExtracted  : Gemini 추출 결과 (유효성 검증 포함)
  ArticleRecord     : DB 저장 완료 후 반환 레코드
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)


# ─────────────────────────────────────────────────────────────
# 1. 스크래퍼 입력
# ─────────────────────────────────────────────────────────────

class RawArticle(BaseModel):
    """스크래퍼가 수집한 원시 데이터."""

    source_url:      AnyHttpUrl
    html:            str           = Field(..., min_length=100, description="원시 HTML")
    language:        str           = Field("kr", pattern=r"^(kr|en|jp)$")
    global_priority: bool          = False
    fetched_at:      datetime      = Field(default_factory=datetime.utcnow)

    model_config = {"str_strip_whitespace": True}


# ─────────────────────────────────────────────────────────────
# 2. Gemini 추출 결과
# ─────────────────────────────────────────────────────────────

class ArticleExtracted(BaseModel):
    """
    Gemini API 추출 결과 + Pydantic 유효성 검증.

    유효성 규칙:
      - title_ko 는 필수 (None 불허)
      - hashtags 는 '#' 없이 저장 (자동 제거)
      - global_priority=True 이면 title_en 권장 (경고만, 오류 아님)
      - 각 텍스트 필드 앞뒤 공백 자동 제거
    """

    # 제목 (다국어)
    title_ko:        str            = Field(..., min_length=1, description="한국어 제목 (필수)")
    title_en:        Optional[str]  = None

    # 원문 (한국어만 저장 — 영어 전체 번역은 비용 효율상 미제공)
    content_ko:      Optional[str]  = None

    # 요약 / SNS 캡션 (다국어 — 영어 요약은 global_priority=True 일 때 생성)
    summary_ko:      Optional[str]  = Field(None, max_length=500)
    summary_en:      Optional[str]  = Field(None, max_length=500)

    # 아티스트
    artist_name_ko:  Optional[str]  = None
    artist_name_en:  Optional[str]  = None

    # 글로벌 아티스트 여부
    global_priority: bool           = False

    # SEO 해시태그 (# 없이)
    hashtags_ko:     list[str]      = Field(default_factory=list)
    hashtags_en:     list[str]      = Field(default_factory=list)

    # 썸네일 (process_thumbnail 이후 S3 URL)
    thumbnail_url:   Optional[str]  = None

    model_config = {"str_strip_whitespace": True}

    # ── 유효성 검증 ──────────────────────────────────────────

    @field_validator("hashtags_ko", "hashtags_en", mode="before")
    @classmethod
    def strip_hash_prefix(cls, tags: list) -> list[str]:
        """해시태그에서 '#' 제거 및 빈 값 필터링."""
        if not isinstance(tags, list):
            return []
        return [
            str(tag).lstrip("#").strip()
            for tag in tags
            if tag and str(tag).strip()
        ]

    @field_validator("content_ko", "summary_ko", "summary_en", mode="before")
    @classmethod
    def empty_to_none(cls, v: object) -> object:
        """빈 문자열을 None으로 변환."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("hashtags_ko", "hashtags_en")
    @classmethod
    def limit_hashtags(cls, tags: list[str]) -> list[str]:
        """최대 15개 해시태그만 유지."""
        return tags[:15]

    @model_validator(mode="after")
    def warn_missing_english(self) -> "ArticleExtracted":
        """global_priority=True 인데 영어 필드가 없으면 경고 로그."""
        if self.global_priority:
            import structlog
            log = structlog.get_logger(__name__)
            if not self.title_en:
                log.warning("global_priority=True 이지만 title_en 없음",
                            title_ko=self.title_ko)
            if not self.summary_en:
                log.warning("global_priority=True 이지만 summary_en 없음",
                            title_ko=self.title_ko)
        return self


# ─────────────────────────────────────────────────────────────
# 3. DB 저장 후 반환 레코드
# ─────────────────────────────────────────────────────────────

class ArticleRecord(ArticleExtracted):
    """DB 저장 완료 후 반환되는 레코드 (id, 시간 포함)."""

    id:           int
    source_url:   str
    language:     str
    job_id:       Optional[int]  = None
    published_at: Optional[datetime] = None
    created_at:   datetime
    updated_at:   datetime

    model_config = {"from_attributes": True}   # SQLAlchemy ORM → Pydantic 변환


# ─────────────────────────────────────────────────────────────
# 4. 작업 큐 입력 스키마
# ─────────────────────────────────────────────────────────────

class ScrapeJobParams(BaseModel):
    """job_queue.params JSONB 에 저장되는 작업 파라미터."""

    source_url:      AnyHttpUrl
    language:        str        = Field("kr", pattern=r"^(kr|en|jp)$")
    platforms:       list[str]  = Field(default_factory=list)
    global_priority: bool       = False

    model_config = {"str_strip_whitespace": True}

    @field_validator("platforms", mode="before")
    @classmethod
    def validate_platforms(cls, v: list) -> list[str]:
        allowed = {"x", "instagram", "facebook", "threads", "naver_blog"}
        return [p for p in v if p in allowed]
