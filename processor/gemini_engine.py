"""
processor/gemini_engine.py — Phase 4: Gemini Entity Extraction Engine (v2)

역할:
    1. Structured Output (엔티티별 신뢰도 포함)
       ArticleIntelligence Pydantic 모델로 Gemini 응답 형식을 강제합니다.
       DetectedArtist 에 confidence_score / is_ambiguous / ambiguity_reason 추가.

    2. Contextual Linking
       탐지된 아티스트명의 문맥(소속사, 그룹, 브랜드 등)을 분석하여
       DB artists 테이블의 아티스트 ID와 매칭합니다.

    3. 조건부 상태 전환 (_decide_status)
       ┌ PROCESSED     : 모든 엔티티 confidence_score ≥ 0.80
       │                 AND 모호한 엔티티 없음 (is_ambiguous=False)
       │                 AND relevance_score ≥ 0.30
       │                 AND overall confidence ≥ 0.60
       ├ MANUAL_REVIEW : 위 조건 중 하나라도 미충족
       │                 → system_note 에 AI 판단 모호 이유 기록
       └ ERROR         : Gemini 호출 실패 / JSON 파싱 실패 / DB 오류

    4. 비용 분석 로그 (GeminiCallMetrics)
       Gemini API 호출마다 prompt_tokens, completion_tokens,
       total_tokens, response_time_ms 를 측정하여
       system_logs.details 에 기록합니다.

상수:
    _ENTITY_CONFIDENCE_THRESHOLD = 0.80  엔티티별 자동승인 임계값
    _MIN_RELEVANCE               = 0.30  기사 관련도 최솟값
    _MIN_CONFIDENCE              = 0.60  전체 분석 신뢰도 최솟값

CLI 사용 예:
    python -m processor.gemini_engine                        # PENDING 10건
    python -m processor.gemini_engine --job-id 7             # 특정 job
    python -m processor.gemini_engine --model gemini-2.0-flash --batch-size 20
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional

import psycopg2
import psycopg2.extras
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

_INTELLIGENCE_MODEL: str = os.getenv("INTELLIGENCE_MODEL", "gemini-1.5-pro")
_TEXT_MAX_CHARS: int = 6_000
_BATCH_SIZE: int = 10

# ── 상태 전환 임계값 ──────────────────────────────────────────

# [NEW v2] 엔티티별 신뢰도: 이 값 미만인 엔티티가 하나라도 있으면 MANUAL_REVIEW
_ENTITY_CONFIDENCE_THRESHOLD: float = float(
    os.getenv("ENTITY_CONFIDENCE_THRESHOLD", "0.80")
)
# 기사 전체 K-엔터 관련도 최솟값
_MIN_RELEVANCE: float = 0.30
# Gemini 전체 분석 신뢰도 최솟값
_MIN_CONFIDENCE: float = 0.60

# 엔티티 DB 매칭 최소 점수 (이하이면 entity_id=None 으로 저장)
_MIN_MATCH_SCORE: float = 0.35


# ─────────────────────────────────────────────────────────────
# Pydantic 구조화 응답 모델
# ─────────────────────────────────────────────────────────────

class DetectedArtist(BaseModel):
    """
    Gemini가 탐지한 개별 아티스트/그룹 정보.

    v2 추가 필드:
        confidence_score  — 이 엔티티 탐지의 신뢰도 (Gemini 자체 평가)
        is_ambiguous      — 동명이인·문맥 모호 여부
        ambiguity_reason  — 모호한 이유 (is_ambiguous=True일 때)
    """

    name_ko: str = Field(..., description="아티스트 한국어 이름")
    name_en: Optional[str] = Field(None, description="영어 이름 (없으면 null)")
    context_hints: list[str] = Field(
        default_factory=list,
        description="주변 문맥 힌트 — 소속사, 그룹명, 브랜드, 드라마 제목",
    )
    mention_count: int = Field(1, ge=1, description="기사 내 언급 횟수")
    is_primary: bool = Field(False, description="기사의 주인공 여부")
    entity_type: Literal["ARTIST", "GROUP", "EVENT"] = Field(
        "ARTIST",
        description="ARTIST(솔로), GROUP(그룹/팀), EVENT(시상식/행사)",
    )
    # ── v2: 엔티티별 신뢰도 ──────────────────────────────────
    confidence_score: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "이 아티스트 탐지의 신뢰도 (Gemini 자체 평가).\n"
            "  0.9~1.0: 이름+문맥이 명확하여 특정 아티스트 확신\n"
            "  0.7~0.9: 대부분 확신, 일부 모호\n"
            "  0.5~0.7: 동명이인이나 문맥 부족으로 불확실\n"
            "  0.0~0.5: 매우 모호하거나 증거 불충분"
        ),
    )
    is_ambiguous: bool = Field(
        False,
        description="동명이인이나 문맥 모호로 정확한 아티스트 특정이 어려우면 True",
    )
    ambiguity_reason: Optional[str] = Field(
        None,
        description=(
            "is_ambiguous=True일 때 모호한 이유.\n"
            "예: '지수'는 블랙핑크 지수(JISOO)와 다른 인물이 있어 문맥상 추정"
        ),
    )

    @field_validator("name_ko")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("context_hints")
    @classmethod
    def _limit_hints(cls, v: list[str]) -> list[str]:
        return [h.strip() for h in v if h.strip()][:10]

    @field_validator("ambiguity_reason")
    @classmethod
    def _trim_reason(cls, v: Optional[str]) -> Optional[str]:
        return v.strip()[:300] if v else None


class ArticleIntelligence(BaseModel):
    """
    processor/gemini_engine.py 전용 Gemini 구조화 응답 모델 (v2).

    scraper/gemini_engine.py 의 ArticleExtracted 와 별개:
      - ArticleExtracted:    스크래핑 시 제목/본문/해시태그 추출 (Phase 3)
      - ArticleIntelligence: 저장된 기사의 엔티티/지식 추출   (Phase 4)
    """

    detected_artists: list[DetectedArtist] = Field(
        default_factory=list,
        description="기사에 등장하는 모든 아티스트/그룹/행사",
    )
    topic_summary: str = Field(
        "",
        max_length=300,
        description="핵심 주제 요약 (300자 이내, 한국어)",
    )
    sentiment: Literal["positive", "negative", "neutral", "mixed"] = Field(
        "neutral",
        description="기사 전체 감성",
    )
    relevance_score: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="K-엔터테인먼트 관련도 (0.0=무관, 1.0=완전 관련)",
    )
    main_category: Literal[
        "music", "drama", "film", "fashion", "entertainment", "award", "other"
    ] = Field("other", description="기사 주요 카테고리")
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="전체 분석 신뢰도",
    )

    @field_validator("topic_summary")
    @classmethod
    def _clean_summary(cls, v: str) -> str:
        return v.strip()

    @field_validator("detected_artists")
    @classmethod
    def _limit_artists(cls, v: list) -> list:
        return v[:20]


# ─────────────────────────────────────────────────────────────
# 처리 결과 데이터 클래스
# ─────────────────────────────────────────────────────────────

@dataclass
class GeminiCallMetrics:
    """
    [v2] Gemini API 단일 호출의 비용·성능 지표.

    system_logs.details 에 기록하여 비용 분석에 활용합니다.

    비용 계산 참고 (Gemini 1.5 Pro, 2024 기준):
        입력 128K 이내: $3.50 / 1M tokens
        출력           : $10.50 / 1M tokens
    """

    prompt_tokens:     int = 0   # 입력 토큰 수
    completion_tokens: int = 0   # 출력 토큰 수 (candidates)
    total_tokens:      int = 0   # 합계
    response_time_ms:  int = 0   # API 응답 소요 시간 (ms)

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens":     self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens":      self.total_tokens,
            "response_time_ms":  self.response_time_ms,
        }


@dataclass
class ProcessingResult:
    """단일 기사 처리 결과."""

    article_id:      int
    status:          str                           # PROCESSED | MANUAL_REVIEW | ERROR
    intelligence:    Optional[ArticleIntelligence] = None
    linked_artists:  list[dict]                   = field(default_factory=list)
    duration_ms:     int                          = 0
    token_metrics:   Optional[GeminiCallMetrics]  = None   # [v2]
    system_note:     Optional[str]                = None   # [v2] MANUAL_REVIEW 사유
    error:           Optional[str]                = None


@dataclass
class BatchResult:
    """배치 처리 집계 결과."""

    total:         int = 0
    processed:     int = 0
    manual_review: int = 0
    failed:        int = 0
    total_tokens:  int = 0   # [v2] 배치 전체 토큰 합계

    def to_dict(self) -> dict:
        return {
            "total":         self.total,
            "processed":     self.processed,
            "manual_review": self.manual_review,
            "failed":        self.failed,
            "total_tokens":  self.total_tokens,
        }


# ─────────────────────────────────────────────────────────────
# Gemini 프롬프트 (v2 — 엔티티별 신뢰도 포함)
# ─────────────────────────────────────────────────────────────

_INTELLIGENCE_PROMPT = textwrap.dedent("""\
    당신은 K-엔터테인먼트 전문 AI 분석가입니다.
    아래 기사를 분석하고, 정확히 다음 JSON 형식으로만 응답하세요.
    JSON 외 다른 텍스트(설명, 주석, 마크다운 코드블록 등)는 절대 포함하지 마세요.

    === 기사 ===
    제목: {title}
    본문:
    {content}
    === 끝 ===

    응답 JSON 형식:
    {{
      "detected_artists": [
        {{
          "name_ko": "한국어 아티스트명",
          "name_en": "English name or null",
          "context_hints": ["소속사", "그룹명", "브랜드"],
          "mention_count": 3,
          "is_primary": true,
          "entity_type": "ARTIST",
          "confidence_score": 0.95,
          "is_ambiguous": false,
          "ambiguity_reason": null
        }}
      ],
      "topic_summary": "100자 이내 핵심 요약",
      "sentiment": "positive",
      "relevance_score": 0.95,
      "main_category": "music",
      "confidence": 0.88
    }}

    분석 규칙:
    1. detected_artists: 기사에 직접 언급된 모든 가수·그룹·배우·MC를 포함하세요.
       - context_hints: 소속사(YG, SM, HYBE 등), 그룹명, 브랜드, 드라마/앨범 제목
       - entity_type: ARTIST(솔로), GROUP(그룹/팀), EVENT(시상식/행사)

    2. confidence_score (0.0~1.0): 해당 아티스트 탐지의 신뢰도를 직접 평가하세요.
       - 0.9~1.0: 이름과 문맥이 명확하여 특정 아티스트임을 확신
       - 0.7~0.9: 대부분 확신하나 일부 모호함
       - 0.5~0.7: 동명이인이나 문맥 부족으로 불확실
       - 0.0~0.5: 매우 모호하거나 본문에 직접적인 증거 없음

    3. is_ambiguous: 동명이인이나 문맥 모호로 정확한 아티스트 특정이 어려우면 true
       예시:
         - '지수' → 블랙핑크 지수(JISOO)인지 다른 지수인지 모호하면 true
         - '뷔' → BTS 뷔(V)가 명확하면 false
         - '로제' → 블랙핑크 로제가 명확하면 false

    4. ambiguity_reason: is_ambiguous=true일 때 모호한 이유를 한 문장으로 설명

    5. sentiment: positive | negative | neutral | mixed
    6. relevance_score: K-팝·K-드라마 등 K-엔터 관련도 (0.0~1.0)
    7. confidence: 분석 전체 신뢰도 (정보 충분→높게, 부족→낮게)
    8. main_category: music|drama|film|fashion|entertainment|award|other
""")


# ─────────────────────────────────────────────────────────────
# RPM 리미터 (모듈 레벨 싱글톤)
# ─────────────────────────────────────────────────────────────

def _build_rpm_limiter():
    try:
        from scraper.gemini_engine import GeminiRpmLimiter  # type: ignore[import]
        rpm = int(os.getenv("GEMINI_RPM_LIMIT", "60"))
        return GeminiRpmLimiter(rpm)
    except Exception as exc:
        log.warning("GeminiRpmLimiter 초기화 실패 (RPM 제어 비활성화) | err=%r", exc)
        return None


_rpm_limiter = _build_rpm_limiter()


# ─────────────────────────────────────────────────────────────
# DB 헬퍼 (psycopg2 raw SQL)
# ─────────────────────────────────────────────────────────────

@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    """psycopg2 연결 컨텍스트 매니저."""
    from core.config import settings
    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_pending_articles(
    limit: int = _BATCH_SIZE,
    job_id: Optional[int] = None,
) -> list[dict]:
    """
    PENDING 기사를 원자적으로 클레임합니다.

    SELECT FOR UPDATE SKIP LOCKED → UPDATE process_status = 'SCRAPED' (in-progress 마커)
    """
    if job_id is not None:
        sql = """
            SELECT id, title_ko, content_ko, summary_ko,
                   artist_name_ko, global_priority, language, source_url, job_id
            FROM   articles
            WHERE  process_status = 'PENDING'
              AND  job_id = %(job_id)s
            ORDER  BY created_at ASC
            LIMIT  %(limit)s
            FOR UPDATE SKIP LOCKED
        """
        params: dict = {"job_id": job_id, "limit": limit}
    else:
        sql = """
            SELECT id, title_ko, content_ko, summary_ko,
                   artist_name_ko, global_priority, language, source_url, job_id
            FROM   articles
            WHERE  process_status = 'PENDING'
            ORDER  BY created_at ASC
            LIMIT  %(limit)s
            FOR UPDATE SKIP LOCKED
        """
        params = {"limit": limit}

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

            if not rows:
                return []

            ids = [r["id"] for r in rows]
            cur.execute(
                "UPDATE articles SET process_status = 'SCRAPED', updated_at = NOW() "
                "WHERE id = ANY(%s)",
                (ids,),
            )

    return [dict(r) for r in rows]


def _get_all_artists() -> list[dict]:
    """artists 테이블 전체를 캐시용으로 조회합니다."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name_ko, name_en, agency, official_tags, global_priority
                FROM   artists
                ORDER  BY global_priority DESC, id ASC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def _update_article_status(
    article_id: int,
    status: str,
    topic_summary: Optional[str] = None,
    system_note: Optional[str] = None,    # [v2]
) -> None:
    """
    기사 process_status 를 갱신합니다.

    [v2] system_note: MANUAL_REVIEW 사유. 기존 노트를 덮어씁니다.
         NULL 을 전달하면 system_note 는 변경하지 않습니다 (COALESCE 방식으로 보존).
         빈 문자열을 전달하면 명시적으로 NULL 로 초기화합니다.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE articles
                SET    process_status = %s,
                       summary_ko    = COALESCE(
                                           NULLIF(trim(coalesce(summary_ko, '')), ''),
                                           %s
                                       ),
                       system_note   = CASE
                                           WHEN %s = '' THEN NULL
                                           WHEN %s IS NOT NULL THEN %s
                                           ELSE system_note
                                       END,
                       updated_at    = NOW()
                WHERE  id = %s
                """,
                (
                    status,
                    topic_summary or None,    # summary_ko fallback
                    system_note or "",        # CASE: empty string → NULL
                    system_note,              # CASE: not null → update
                    system_note,              # SET value
                    article_id,
                ),
            )


def _replace_entity_mappings(article_id: int, records: list[dict]) -> int:
    """기사의 entity_mappings 를 교체합니다 (기존 삭제 후 일괄 삽입)."""
    if not records:
        return 0

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM entity_mappings WHERE article_id = %s",
                (article_id,),
            )
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO entity_mappings
                    (article_id, entity_type, entity_id,
                     entity_name_ko, confidence_score, context_snippet)
                VALUES %s
                """,
                [
                    (
                        article_id,
                        r.get("entity_type", "ARTIST"),
                        r.get("entity_id"),
                        r["entity_name_ko"],
                        r["confidence_score"],
                        r.get("context_snippet", ""),
                    )
                    for r in records
                ],
                template="(%s, %s::entity_type_enum, %s, %s, %s, %s)",
            )

    return len(records)


def _log_to_system(
    article_id: Optional[int],
    level: str,
    event: str,
    message: str,
    details: Optional[dict] = None,
    duration_ms: Optional[int] = None,
    job_id: Optional[int] = None,
) -> None:
    """system_logs 에 처리 기록을 추가합니다 (append-only)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system_logs
                        (article_id, job_id, level, category,
                         event, message, details, duration_ms)
                    VALUES (%s, %s,
                            %s::log_level_enum,
                            'AI_PROCESS'::log_category_enum,
                            %s, %s, %s, %s)
                    """,
                    (
                        article_id,
                        job_id,
                        level,
                        event,
                        message,
                        json.dumps(details, ensure_ascii=False, default=str)
                        if details
                        else None,
                        duration_ms,
                    ),
                )
    except Exception as exc:
        log.error(
            "system_logs 기록 실패 | article_id=%s event=%s err=%r",
            article_id, event, exc,
        )


# ─────────────────────────────────────────────────────────────
# Intelligence Engine
# ─────────────────────────────────────────────────────────────

class IntelligenceEngine:
    """
    Gemini 기반 Phase 4 지식 추출 엔진 (v2).

    v2 주요 변경:
        1. DetectedArtist 에 confidence_score / is_ambiguous / ambiguity_reason 추가
        2. _decide_status(): 엔티티별 0.80 임계값 기반 PROCESSED/MANUAL_REVIEW 결정
           - 하나라도 confidence_score < 0.80 이거나 is_ambiguous = True이면 MANUAL_REVIEW
           - system_note 에 AI가 판단한 모호 이유 기록
        3. _call_gemini(): (text, GeminiCallMetrics) 튜플 반환
           - prompt_tokens, completion_tokens, total_tokens, response_time_ms 측정
        4. system_logs.details 에 token_metrics 포함 → 비용 분석 가능

    Contextual Linking 점수 체계 (최대 1.0):
        +0.50  이름(name_ko) 완전 일치
        +0.30  이름 부분 포함
        +0.20  영어명 완전 일치  /  +0.10 부분 포함
        +0.15  context_hints ∩ agency
        +0.10  context_hints ∩ official_tags 값 (최대 3개 힌트)
    """

    _CACHE_TTL: float = 300.0

    def __init__(
        self,
        model_name: str = _INTELLIGENCE_MODEL,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size

        self._genai_model = None
        self._artists_cache: list[dict] = []
        self._cache_loaded_at: float = 0.0

        log.info(
            "IntelligenceEngine v2 초기화 | model=%s batch_size=%d "
            "entity_threshold=%.2f",
            model_name, batch_size, _ENTITY_CONFIDENCE_THRESHOLD,
        )

    # ── Gemini 클라이언트 ──────────────────────────────────

    def _ensure_model(self) -> None:
        if self._genai_model is not None:
            return
        try:
            import google.generativeai as genai  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "google-generativeai 미설치. `pip install google-generativeai`"
            ) from exc

        from core.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._genai_model = genai.GenerativeModel(
            self.model_name,
            generation_config=genai.GenerationConfig(
                temperature=0.10,
                response_mime_type="application/json",
            ),
        )
        log.debug("Gemini 모델 준비 완료 | model=%s", self.model_name)

    def _call_gemini(self, prompt: str) -> tuple[str, GeminiCallMetrics]:
        """
        [v2] Gemini API 를 호출하고 (응답 텍스트, GeminiCallMetrics) 를 반환합니다.

        측정 항목:
            - response_time_ms: API 호출 시작 ~ 응답 수신 시간
            - prompt_tokens:    입력 토큰 수 (usage_metadata.prompt_token_count)
            - completion_tokens: 출력 토큰 수 (usage_metadata.candidates_token_count)
            - total_tokens:     합계 (usage_metadata.total_token_count)
        """
        from core.config import settings

        settings.check_gemini_kill_switch()

        if _rpm_limiter is not None:
            _rpm_limiter.acquire()

        self._ensure_model()

        # ── 응답 시간 측정 시작 ──────────────────────────
        t_api = time.monotonic()
        response = self._genai_model.generate_content(prompt)
        response_time_ms = int((time.monotonic() - t_api) * 1000)

        # ── 토큰 수집 ────────────────────────────────────
        usage = getattr(response, "usage_metadata", None)
        metrics = GeminiCallMetrics(
            prompt_tokens     = getattr(usage, "prompt_token_count",     0),
            completion_tokens = getattr(usage, "candidates_token_count", 0),
            total_tokens      = getattr(usage, "total_token_count",      0),
            response_time_ms  = response_time_ms,
        )

        if metrics.total_tokens:
            try:
                settings.record_gemini_usage(metrics.total_tokens)
            except Exception:
                pass

        log.debug(
            "Gemini 호출 완료 | tokens(p=%d c=%d t=%d) time=%dms",
            metrics.prompt_tokens,
            metrics.completion_tokens,
            metrics.total_tokens,
            metrics.response_time_ms,
        )
        return response.text, metrics

    @staticmethod
    def _parse_json(raw_text: str) -> dict[str, Any]:
        """마크다운 코드블록 제거 후 JSON 파싱."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        return json.loads(text)

    # ── 아티스트 캐시 ──────────────────────────────────────

    def _get_artists(self, force: bool = False) -> list[dict]:
        """아티스트 목록을 메모리 캐시에서 반환 (TTL: 5분)."""
        now = time.monotonic()
        if (
            force
            or not self._artists_cache
            or (now - self._cache_loaded_at) > self._CACHE_TTL
        ):
            self._artists_cache = _get_all_artists()
            self._cache_loaded_at = now
            log.debug("아티스트 캐시 갱신 | count=%d", len(self._artists_cache))
        return self._artists_cache

    # ── 매칭 점수 계산 ─────────────────────────────────────

    def _score_artist_match(
        self,
        detected: DetectedArtist,
        candidate: dict,
    ) -> float:
        """
        탐지된 아티스트와 DB 후보 사이의 매칭 신뢰도 점수를 계산합니다.

        이 점수는 DB 매칭용(entity_mappings.confidence_score)이며,
        DetectedArtist.confidence_score(Gemini 자체 평가)와 별개입니다.
        """
        score = 0.0

        name_ko = detected.name_ko.strip()
        cand_ko = (candidate.get("name_ko") or "").strip()

        if name_ko and cand_ko:
            if name_ko == cand_ko:
                score += 0.50
            elif name_ko in cand_ko or cand_ko in name_ko:
                score += 0.30

        name_en = (detected.name_en or "").strip().lower()
        cand_en = (candidate.get("name_en") or "").strip().lower()
        if name_en and cand_en:
            if name_en == cand_en:
                score += 0.20
            elif name_en in cand_en or cand_en in name_en:
                score += 0.10

        hints  = [h.lower() for h in detected.context_hints if h.strip()]
        agency = (candidate.get("agency") or "").lower()
        if agency:
            for hint in hints:
                if hint and (hint in agency or agency in hint):
                    score += 0.15
                    break

        tags_raw  = candidate.get("official_tags") or {}
        tag_words: set[str] = set()
        if isinstance(tags_raw, dict):
            for v in tags_raw.values():
                if isinstance(v, list):
                    tag_words.update(str(x).lower() for x in v)
                elif isinstance(v, str):
                    tag_words.add(v.lower())

        if tag_words:
            hint_bonus = 0
            for hint in hints:
                if not hint:
                    continue
                for tw in tag_words:
                    if hint in tw or tw in hint:
                        score += 0.10
                        hint_bonus += 1
                        break
                if hint_bonus >= 3:
                    break

        return min(score, 1.0)

    # ── 컨텍스트 링킹 ──────────────────────────────────────

    def _contextual_link(
        self,
        detected_artists: list[DetectedArtist],
    ) -> list[dict]:
        """탐지된 아티스트 목록을 DB artists 테이블과 매칭합니다."""
        artists = self._get_artists()
        if not artists:
            log.warning("artists 캐시 비어있음 — 컨텍스트 링킹 불가")

        results: list[dict] = []

        for detected in detected_artists:
            best_score:     float = 0.0
            best_candidate: Optional[dict] = None

            for candidate in artists:
                s = self._score_artist_match(detected, candidate)
                if s > best_score:
                    best_score     = s
                    best_candidate = candidate

            linked    = best_score >= _MIN_MATCH_SCORE and best_candidate is not None
            entity_id = best_candidate["id"]      if linked else None
            entity_name = best_candidate["name_ko"] if linked else detected.name_ko

            results.append({
                "detected_name_ko":  detected.name_ko,
                "entity_id":         entity_id,
                "entity_name_ko":    entity_name,
                "entity_type":       detected.entity_type,
                "confidence_score":  round(best_score, 4),
                "context_snippet":   ", ".join(detected.context_hints[:5]),
                "mention_count":     detected.mention_count,
                "is_primary":        detected.is_primary,
                # [v2] Gemini 자체 신뢰도 함께 전달 (로깅용)
                "gemini_confidence": detected.confidence_score,
                "is_ambiguous":      detected.is_ambiguous,
                "ambiguity_reason":  detected.ambiguity_reason,
            })

            if linked:
                log.debug(
                    "링킹 성공 | %s → id=%d score=%.2f gem_conf=%.2f ambig=%s",
                    detected.name_ko, entity_id, best_score,
                    detected.confidence_score, detected.is_ambiguous,
                )
            else:
                log.debug(
                    "링킹 실패 (score=%.2f) | detected=%s gem_conf=%.2f",
                    best_score, detected.name_ko, detected.confidence_score,
                )

        return results

    # ── [v2] 조건부 상태 전환 ──────────────────────────────

    def _decide_status(
        self,
        intelligence: ArticleIntelligence,
        linked: list[dict],
    ) -> tuple[str, Optional[str]]:
        """
        [v2] 처리 결과를 기반으로 최종 상태와 system_note 를 결정합니다.

        PROCESSED 조건 (모두 충족):
            1. 모든 DetectedArtist.confidence_score ≥ _ENTITY_CONFIDENCE_THRESHOLD(0.80)
            2. 모든 DetectedArtist.is_ambiguous == False
            3. relevance_score ≥ _MIN_RELEVANCE (0.30)
            4. overall confidence ≥ _MIN_CONFIDENCE (0.60)

        MANUAL_REVIEW 조건 (하나라도 해당):
            - 엔티티 confidence_score < 0.80
            - is_ambiguous = True (동명이인/문맥 모호)
            - relevance_score 또는 overall confidence 임계값 미달

        Returns:
            (status, system_note)
            system_note 는 MANUAL_REVIEW 시 AI 판단 사유 문자열, PROCESSED 시 None
        """
        reasons: list[str] = []

        # ── 1. 엔티티별 신뢰도 검사 ──────────────────────
        for artist in intelligence.detected_artists:
            name = artist.name_ko

            if artist.confidence_score < _ENTITY_CONFIDENCE_THRESHOLD:
                reasons.append(
                    f"'{name}' 탐지 신뢰도 낮음 "
                    f"({artist.confidence_score:.2f} < {_ENTITY_CONFIDENCE_THRESHOLD:.2f})"
                )

            if artist.is_ambiguous:
                reason_text = artist.ambiguity_reason or "맥락 모호"
                reasons.append(f"'{name}' 동명이인/모호: {reason_text}")

        # ── 2. 기사 전체 지표 검사 ────────────────────────
        if intelligence.relevance_score < _MIN_RELEVANCE:
            reasons.append(
                f"K-엔터 관련도 낮음 "
                f"({intelligence.relevance_score:.2f} < {_MIN_RELEVANCE:.2f})"
            )
        if intelligence.confidence < _MIN_CONFIDENCE:
            reasons.append(
                f"전체 분석 신뢰도 낮음 "
                f"({intelligence.confidence:.2f} < {_MIN_CONFIDENCE:.2f})"
            )

        if reasons:
            note = "MANUAL_REVIEW 사유: " + "; ".join(reasons)
            log.info(
                "MANUAL_REVIEW 결정 | %d개 사유: %s",
                len(reasons), " / ".join(reasons[:3]),
            )
            return "MANUAL_REVIEW", note

        return "PROCESSED", None

    # ── Gemini 지식 추출 ───────────────────────────────────

    def _extract_intelligence(
        self,
        title_ko:   Optional[str],
        content_ko: Optional[str],
    ) -> tuple[ArticleIntelligence, GeminiCallMetrics]:
        """
        [v2] Gemini API 를 호출하여 기사의 엔티티/지식을 추출합니다.

        Returns:
            (ArticleIntelligence, GeminiCallMetrics)
        """
        title   = (title_ko   or "").strip() or "제목 없음"
        content = (content_ko or "").strip()

        if len(content) > _TEXT_MAX_CHARS:
            content = content[:_TEXT_MAX_CHARS] + "\n...(이하 생략)"
        if not content:
            log.warning("content_ko 없음 — 제목만으로 분석 (신뢰도 낮을 수 있음)")

        prompt = _INTELLIGENCE_PROMPT.format(title=title, content=content)
        raw, metrics = self._call_gemini(prompt)
        data   = self._parse_json(raw)
        result = ArticleIntelligence.model_validate(data)

        # 엔티티별 신뢰도 요약 로그
        if result.detected_artists:
            conf_list = [
                f"{a.name_ko}:{a.confidence_score:.2f}"
                + ("⚠" if a.is_ambiguous else "")
                for a in result.detected_artists
            ]
            log.info(
                "Gemini 추출 완료 | artists=[%s] sentiment=%s "
                "relevance=%.2f confidence=%.2f tokens=%d time=%dms",
                ", ".join(conf_list),
                result.sentiment,
                result.relevance_score,
                result.confidence,
                metrics.total_tokens,
                metrics.response_time_ms,
            )
        else:
            log.info(
                "Gemini 추출 완료 (아티스트 미탐지) | sentiment=%s "
                "relevance=%.2f tokens=%d time=%dms",
                result.sentiment,
                result.relevance_score,
                metrics.total_tokens,
                metrics.response_time_ms,
            )

        return result, metrics

    # ── 단일 기사 처리 ─────────────────────────────────────

    def process_article(self, article: dict) -> ProcessingResult:
        """
        [v2] 단일 기사를 처리합니다.

        처리 순서:
            1. Gemini 추출 → (ArticleIntelligence, GeminiCallMetrics)
            2. 컨텍스트 링킹 → entity_id 매칭
            3. entity_mappings 교체
            4. _decide_status() → PROCESSED / MANUAL_REVIEW (+ system_note)
            5. DB 업데이트 (process_status, summary_ko, system_note)
            6. system_logs 기록 (토큰·응답시간 포함)
        """
        article_id = article["id"]
        job_id     = article.get("job_id")
        t_start    = time.monotonic()

        try:
            # ── 1. Gemini 추출 ───────────────────────────
            intelligence, metrics = self._extract_intelligence(
                title_ko   = article.get("title_ko"),
                content_ko = article.get("content_ko"),
            )

            # ── 2. 컨텍스트 링킹 ────────────────────────
            linked = self._contextual_link(intelligence.detected_artists)

            # ── 3. entity_mappings 저장 ──────────────────
            entity_records = [
                {
                    "entity_name_ko":  m["entity_name_ko"],
                    "entity_id":       m["entity_id"],
                    "entity_type":     m["entity_type"],
                    "confidence_score": m["confidence_score"],
                    "context_snippet": m["context_snippet"],
                }
                for m in linked
            ]
            if entity_records:
                saved = _replace_entity_mappings(article_id, entity_records)
                log.debug(
                    "entity_mappings 저장 | article_id=%d count=%d", article_id, saved
                )

            # ── 4. 조건부 상태 결정 ──────────────────────
            final_status, system_note = self._decide_status(intelligence, linked)

            # ── 5. DB 업데이트 ───────────────────────────
            _update_article_status(
                article_id,
                final_status,
                topic_summary = intelligence.topic_summary or None,
                system_note   = system_note,
            )

            duration_ms = int((time.monotonic() - t_start) * 1000)

            # ── 6. 성공 로그 (토큰 포함) ──────────────────
            ambiguous_names = [
                m["detected_name_ko"]
                for m in linked
                if m.get("is_ambiguous")
            ]
            low_conf_entities = [
                f"{m['detected_name_ko']}({m['gemini_confidence']:.2f})"
                for m in linked
                if m.get("gemini_confidence", 1.0) < _ENTITY_CONFIDENCE_THRESHOLD
            ]

            _log_to_system(
                article_id  = article_id,
                level       = "INFO" if final_status == "PROCESSED" else "WARNING",
                event       = f"entity_extract_{final_status.lower()}",
                message     = (
                    f"엔티티 추출 완료 ({final_status}) | "
                    f"artists={len(linked)} "
                    f"tokens={metrics.total_tokens} "
                    f"time={metrics.response_time_ms}ms"
                ),
                details     = {
                    "status":            final_status,
                    "system_note":       system_note,
                    "sentiment":         intelligence.sentiment,
                    "relevance_score":   intelligence.relevance_score,
                    "confidence":        intelligence.confidence,
                    "main_category":     intelligence.main_category,
                    # 엔티티 신뢰도 요약
                    "entity_scores":     {
                        m["detected_name_ko"]: m.get("gemini_confidence", 1.0)
                        for m in linked
                    },
                    "ambiguous_entities":  ambiguous_names,
                    "low_conf_entities":   low_conf_entities,
                    "linked_artist_ids":   [
                        m["entity_id"] for m in linked if m["entity_id"] is not None
                    ],
                    # [v2] 비용 분석 데이터
                    "token_metrics":    metrics.to_dict(),
                },
                duration_ms = duration_ms,
                job_id      = job_id,
            )

            return ProcessingResult(
                article_id     = article_id,
                status         = final_status,
                intelligence   = intelligence,
                linked_artists = linked,
                duration_ms    = duration_ms,
                token_metrics  = metrics,
                system_note    = system_note,
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            error_msg   = f"{type(exc).__name__}: {exc}"
            log.exception(
                "기사 처리 실패 | article_id=%d error=%s", article_id, error_msg
            )

            try:
                _update_article_status(article_id, "ERROR")
            except Exception as db_exc:
                log.error(
                    "ERROR 상태 업데이트 실패 | article_id=%d err=%r",
                    article_id, db_exc,
                )

            _log_to_system(
                article_id  = article_id,
                level       = "ERROR",
                event       = "entity_extract_failed",
                message     = f"엔티티 추출 실패: {error_msg}",
                details     = {
                    "error_type":   type(exc).__name__,
                    "error_detail": str(exc),
                    "title_ko":     article.get("title_ko", ""),
                    "source_url":   article.get("source_url", ""),
                },
                duration_ms = duration_ms,
                job_id      = job_id,
            )

            return ProcessingResult(
                article_id  = article_id,
                status      = "ERROR",
                duration_ms = duration_ms,
                error       = error_msg,
            )

    # ── 배치 처리 ──────────────────────────────────────────

    def process_pending(
        self,
        batch_size: Optional[int] = None,
        job_id: Optional[int] = None,
    ) -> BatchResult:
        """
        PENDING 기사를 배치로 처리합니다.

        [v2] BatchResult 에 total_tokens 합계를 포함합니다.
        """
        limit  = batch_size if batch_size is not None else self.batch_size
        result = BatchResult()

        articles = _claim_pending_articles(limit=limit, job_id=job_id)
        result.total = len(articles)

        if not articles:
            log.info(
                "처리할 PENDING 기사 없음 | job_id=%s",
                job_id if job_id is not None else "전체",
            )
            return result

        log.info(
            "배치 처리 시작 | count=%d job_id=%s model=%s threshold=%.2f",
            len(articles), job_id, self.model_name, _ENTITY_CONFIDENCE_THRESHOLD,
        )

        for i, article in enumerate(articles, start=1):
            ar = self.process_article(article)

            # 토큰 합산
            if ar.token_metrics:
                result.total_tokens += ar.token_metrics.total_tokens

            log.info(
                "[%d/%d] article_id=%d → %s | tokens=%d time=%dms%s",
                i, len(articles),
                ar.article_id,
                ar.status,
                ar.token_metrics.total_tokens if ar.token_metrics else 0,
                ar.duration_ms,
                f" | note: {ar.system_note[:60]}..." if ar.system_note else "",
            )

            if ar.status == "PROCESSED":
                result.processed += 1
            elif ar.status == "MANUAL_REVIEW":
                result.manual_review += 1
            else:
                result.failed += 1

        log.info(
            "배치 처리 완료 | total=%d processed=%d manual_review=%d failed=%d "
            "total_tokens=%d",
            result.total, result.processed, result.manual_review, result.failed,
            result.total_tokens,
        )
        return result


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt = "%Y-%m-%dT%H:%M:%S",
    )


def main(argv: Optional[list[str]] = None) -> None:
    """
    CLI 진입점.

    사용 예:
        python -m processor.gemini_engine
        python -m processor.gemini_engine --batch-size 5
        python -m processor.gemini_engine --job-id 42
        python -m processor.gemini_engine --model gemini-2.0-flash
        python -m processor.gemini_engine --threshold 0.90  # 엔티티 신뢰도 임계값 조정
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="TIH Gemini Intelligence Engine v2 — Phase 4 Entity Extraction",
    )
    parser.add_argument(
        "--batch-size", type=int, default=_BATCH_SIZE, metavar="N",
        help=f"처리할 기사 수 (기본: {_BATCH_SIZE})",
    )
    parser.add_argument(
        "--job-id", type=int, default=None, metavar="ID",
        help="특정 job_id 의 기사만 처리",
    )
    parser.add_argument(
        "--model", default=_INTELLIGENCE_MODEL, metavar="MODEL",
        help=f"Gemini 모델명 (기본: {_INTELLIGENCE_MODEL})",
    )
    parser.add_argument(
        "--threshold", type=float, default=None, metavar="FLOAT",
        help=f"엔티티 신뢰도 임계값 (기본: {_ENTITY_CONFIDENCE_THRESHOLD}). "
             "이 값 미만 엔티티가 있으면 MANUAL_REVIEW",
    )
    args = parser.parse_args(argv)

    _setup_logging()

    # CLI 에서 임계값 오버라이드
    if args.threshold is not None:
        global _ENTITY_CONFIDENCE_THRESHOLD
        _ENTITY_CONFIDENCE_THRESHOLD = args.threshold
        log.info("엔티티 신뢰도 임계값 오버라이드: %.2f", _ENTITY_CONFIDENCE_THRESHOLD)

    engine = IntelligenceEngine(
        model_name = args.model,
        batch_size = args.batch_size,
    )

    result = engine.process_pending(
        batch_size = args.batch_size,
        job_id     = args.job_id,
    )

    print(
        f"\n처리 완료: total={result.total} "
        f"processed={result.processed} "
        f"manual_review={result.manual_review} "
        f"failed={result.failed} "
        f"total_tokens={result.total_tokens}"
    )


if __name__ == "__main__":
    main()
