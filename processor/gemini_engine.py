"""
processor/gemini_engine.py — Phase 4: Gemini Entity Extraction Engine

역할:
    1. Structured Output
       ArticleIntelligence Pydantic 모델로 Gemini 응답 형식을 강제합니다.
       필드: detected_artists, topic_summary, sentiment, relevance_score,
             main_category, confidence

    2. Contextual Linking
       기사 본문에서 탐지된 아티스트명의 주변 문맥(소속사, 그룹, 브랜드 등)을
       분석하여 DB artists 테이블의 특정 아티스트 ID와 매칭합니다.
       매칭 신뢰도(confidence_score)를 계산하여 entity_mappings 테이블에 저장합니다.

       예시: "제니"가 ["블랙핑크", "YG", "샤넬"] 컨텍스트와 함께 등장하면
             → agency="YG Entertainment", official_tags={"groups":["블랙핑크"]}인
                artists 레코드와 매칭 (score ≈ 0.75)

    3. Incremental Update
       process_status = 'PENDING' 기사만 SELECT FOR UPDATE SKIP LOCKED 로 원자적 클레임.
       처리 결과에 따라 상태 전환:
           PROCESSED     : relevance_score ≥ 0.30 AND confidence ≥ 0.60
           MANUAL_REVIEW : 위 임계값 미달 (검수 큐)
           ERROR         : Gemini 호출 실패 / JSON 파싱 실패 / DB 오류
       실패 시 system_logs 에 level='ERROR', category='AI_PROCESS' 로 기록.

상태 전환 흐름:
    PENDING ──(claim)──► SCRAPED(임시)
        ├── (Gemini 성공, 신뢰도 충족) ──► PROCESSED
        ├── (Gemini 성공, 신뢰도 미달) ──► MANUAL_REVIEW
        └── (예외 발생)               ──► ERROR

아키텍처 메모:
    - scraper.gemini_engine.GeminiRpmLimiter 를 동일 클래스로 재사용.
      단, 이 모듈 고유의 module-level 싱글톤(_rpm_limiter)을 생성합니다.
      스크래핑과 엔티티 추출은 파이프라인상 순차 실행이므로 RPM 충돌 가능성은 낮습니다.
    - settings.check_gemini_kill_switch() + record_gemini_usage() Kill Switch 연동.
    - psycopg2 raw SQL (scraper/db.py 패턴 동일).

CLI 사용 예:
    # PENDING 기사 10건 처리
    python -m processor.gemini_engine

    # 특정 job의 기사만 처리
    python -m processor.gemini_engine --job-id 7

    # 모델 지정 + 배치 크기 조정
    python -m processor.gemini_engine --model gemini-1.5-pro --batch-size 5
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

# Gemini 모델: Phase 4 엔티티 추출은 1.5 Pro 사용 (2.0 Flash 대비 추론력 우수)
_INTELLIGENCE_MODEL: str = os.getenv("INTELLIGENCE_MODEL", "gemini-1.5-pro")
_TEXT_MAX_CHARS: int = 6_000       # 프롬프트 내 본문 최대 글자 수
_BATCH_SIZE: int = 10              # 기본 배치 크기

# 상태 전환 임계값
_MIN_RELEVANCE: float = 0.30       # K-엔터 관련도 최솟값 (미달 시 MANUAL_REVIEW)
_MIN_CONFIDENCE: float = 0.60      # 전체 분석 신뢰도 최솟값

# 엔티티 매칭 임계값
_MIN_MATCH_SCORE: float = 0.35     # entity_mappings 저장 최소 매칭 점수


# ─────────────────────────────────────────────────────────────
# Pydantic 구조화 응답 모델
# ─────────────────────────────────────────────────────────────

class DetectedArtist(BaseModel):
    """Gemini가 탐지한 개별 아티스트/그룹 정보."""

    name_ko: str = Field(..., description="아티스트 한국어 이름")
    name_en: Optional[str] = Field(None, description="영어 이름 (없으면 null)")
    context_hints: list[str] = Field(
        default_factory=list,
        description=(
            "주변 문맥 힌트 목록 — 소속사, 그룹명, 브랜드, 드라마 제목 등 "
            "아티스트 DB 매칭 정확도를 높이는 단서"
        ),
    )
    mention_count: int = Field(1, ge=1, description="기사 내 언급 횟수")
    is_primary: bool = Field(False, description="기사의 주인공 여부 (가장 중심 인물)")
    entity_type: Literal["ARTIST", "GROUP", "EVENT"] = Field(
        "ARTIST",
        description="엔티티 유형: ARTIST(솔로), GROUP(그룹/팀), EVENT(시상식/행사)",
    )

    @field_validator("name_ko")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("context_hints")
    @classmethod
    def _limit_hints(cls, v: list[str]) -> list[str]:
        return [h.strip() for h in v if h.strip()][:10]


class ArticleIntelligence(BaseModel):
    """
    processor/gemini_engine.py 전용 Gemini 구조화 응답 모델.

    scraper/gemini_engine.py 의 ArticleExtracted 와는 별개:
    - ArticleExtracted : 스크래핑 시 제목/본문/해시태그 추출 (Phase 3)
    - ArticleIntelligence: 저장된 기사의 엔티티/지식 추출 (Phase 4)
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
        description="기사 전체 감성 (positive/negative/neutral/mixed)",
    )
    relevance_score: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="K-엔터테인먼트 관련도 (0.0 = 무관, 1.0 = 완전 관련)",
    )
    main_category: Literal[
        "music", "drama", "film", "fashion", "entertainment", "award", "other"
    ] = Field("other", description="기사 주요 카테고리")
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="분석 전체 신뢰도 (정보 부족·모호한 경우 낮게 설정)",
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
class ProcessingResult:
    """단일 기사 처리 결과."""

    article_id:      int
    status:          str                          # PROCESSED | MANUAL_REVIEW | ERROR
    intelligence:    Optional[ArticleIntelligence] = None
    linked_artists:  list[dict]                  = field(default_factory=list)
    duration_ms:     int                         = 0
    error:           Optional[str]               = None


@dataclass
class BatchResult:
    """배치 처리 집계 결과."""

    total:         int = 0
    processed:     int = 0
    manual_review: int = 0
    failed:        int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total":         self.total,
            "processed":     self.processed,
            "manual_review": self.manual_review,
            "failed":        self.failed,
        }


# ─────────────────────────────────────────────────────────────
# Gemini 프롬프트
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
          "context_hints": ["소속사", "그룹명", "브랜드", "드라마제목"],
          "mention_count": 3,
          "is_primary": true,
          "entity_type": "ARTIST"
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
       - context_hints에는 소속사(예: YG, SM, HYBE), 그룹명(예: 블랙핑크),
         관련 브랜드(예: 샤넬), 활동명, 드라마/앨범 제목을 포함하세요.
       - 동명이인이 있는 경우(예: '제니') context_hints로 구분 정보를 반드시 제공하세요.
       - entity_type: ARTIST(솔로 아티스트), GROUP(그룹/팀), EVENT(시상식/행사/콘서트)
    2. sentiment: 기사의 전반적 어조 (positive/negative/neutral/mixed)
    3. relevance_score: K-팝·K-드라마·K-필름 등 K-엔터테인먼트 관련도 (0.0~1.0)
    4. confidence: 위 분석 전체의 신뢰도 (정보가 충분하면 높게, 부족하면 낮게)
    5. main_category: music|drama|film|fashion|entertainment|award|other 중 택일
""")


# ─────────────────────────────────────────────────────────────
# RPM 리미터 (모듈 레벨 싱글톤)
# ─────────────────────────────────────────────────────────────

def _build_rpm_limiter():
    """scraper.gemini_engine.GeminiRpmLimiter 를 재사용하여 모듈 싱글톤 생성."""
    try:
        from scraper.gemini_engine import GeminiRpmLimiter  # type: ignore[import]
        rpm = int(os.getenv("GEMINI_RPM_LIMIT", "60"))
        return GeminiRpmLimiter(rpm)
    except Exception as exc:                                # scraper 미로드 시 No-op
        log.warning("GeminiRpmLimiter 초기화 실패 (RPM 제어 비활성화) | err=%r", exc)
        return None


_rpm_limiter = _build_rpm_limiter()


# ─────────────────────────────────────────────────────────────
# DB 헬퍼 (psycopg2 raw SQL)
# ─────────────────────────────────────────────────────────────

@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    """psycopg2 연결 컨텍스트 매니저 (auto commit/rollback)."""
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
    같은 트랜잭션 내에서 실행하여 동시성 안전을 보장합니다.

    Returns:
        article 딕셔너리 목록 (id, title_ko, content_ko, summary_ko, job_id, global_priority)
        빈 목록이면 처리할 PENDING 기사 없음.
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
            # SCRAPED = "in-progress" 마커 (처리 중 크래시 시 재진입 방지)
            cur.execute(
                "UPDATE articles SET process_status = 'SCRAPED', updated_at = NOW() "
                "WHERE id = ANY(%s)",
                (ids,),
            )

    return [dict(r) for r in rows]


def _get_all_artists() -> list[dict]:
    """
    artists 테이블 전체를 캐시용으로 조회합니다.

    Returns:
        [{id, name_ko, name_en, agency, official_tags, global_priority}]
        global_priority DESC (높은 우선순위 아티스트 먼저)
    """
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
) -> None:
    """
    기사 process_status 를 갱신합니다.

    topic_summary 가 주어지면 기존 summary_ko 가 비어있는 경우에 한해 덮어씁니다.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            if topic_summary:
                cur.execute(
                    """
                    UPDATE articles
                    SET    process_status = %s,
                           summary_ko    = COALESCE(NULLIF(trim(coalesce(summary_ko, '')), ''), %s),
                           updated_at    = NOW()
                    WHERE  id = %s
                    """,
                    (status, topic_summary, article_id),
                )
            else:
                cur.execute(
                    "UPDATE articles SET process_status = %s, updated_at = NOW() WHERE id = %s",
                    (status, article_id),
                )


def _replace_entity_mappings(article_id: int, records: list[dict]) -> int:
    """
    기사의 entity_mappings 를 교체합니다 (기존 삭제 후 일괄 삽입).

    Args:
        article_id: 기사 ID
        records: [{entity_name_ko, entity_id, entity_type, confidence_score, context_snippet}]

    Returns:
        삽입된 레코드 수
    """
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
    """
    system_logs 에 처리 기록을 추가합니다 (append-only).

    Args:
        level:       DEBUG | INFO | WARNING | ERROR
        event:       처리 단계 식별자 (예: "entity_extract_success")
        message:     사람이 읽을 수 있는 설명
        details:     JSONB 컨텍스트 (오류 상세, 토큰 수, 아티스트 목록 등)
        duration_ms: 처리 소요 시간 (밀리초)
    """
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
        # 로그 기록 실패는 주 처리 흐름을 막지 않습니다
        log.error(
            "system_logs 기록 실패 | article_id=%s event=%s err=%r",
            article_id, event, exc,
        )


# ─────────────────────────────────────────────────────────────
# Intelligence Engine
# ─────────────────────────────────────────────────────────────

class IntelligenceEngine:
    """
    Gemini 기반 Phase 4 지식 추출 엔진.

    Contextual Linking 알고리즘 (DetectedArtist → Artist DB):
        점수 체계 (최대 1.0):
          +0.50  이름 완전 일치 (name_ko)
          +0.30  이름 부분 포함 (name_ko ⊂ 후보 or 후보 ⊂ name_ko)
          +0.20  영어명 완전 일치 (name_en)
          +0.10  영어명 부분 포함
          +0.15  context_hints ∩ agency (소속사 매칭)
          +0.10  context_hints ∩ official_tags 값 (최대 3개 힌트, 태그당 1회)

        _MIN_MATCH_SCORE(0.35) 이상인 후보만 entity_mappings 에 저장.

    Usage:
        engine = IntelligenceEngine()
        result = engine.process_pending(batch_size=10)
    """

    _CACHE_TTL: float = 300.0    # 아티스트 캐시 유효 시간 (초)

    def __init__(
        self,
        model_name: str = _INTELLIGENCE_MODEL,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size

        self._genai_model = None                # lazy init
        self._artists_cache: list[dict] = []
        self._cache_loaded_at: float = 0.0

        log.info(
            "IntelligenceEngine 초기화 | model=%s batch_size=%d",
            model_name, batch_size,
        )

    # ── Gemini 클라이언트 ──────────────────────────────────

    def _ensure_model(self) -> None:
        """google-generativeai 모델 lazy 초기화."""
        if self._genai_model is not None:
            return

        try:
            import google.generativeai as genai  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "google-generativeai 미설치. `pip install google-generativeai` 를 실행하세요."
            ) from exc

        from core.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._genai_model = genai.GenerativeModel(
            self.model_name,
            generation_config=genai.GenerationConfig(
                temperature=0.10,                 # 엔티티 추출: 낮은 temperature
                response_mime_type="application/json",
            ),
        )
        log.debug("Gemini 모델 준비 완료 | model=%s", self.model_name)

    def _call_gemini(self, prompt: str) -> str:
        """
        Gemini API 를 호출합니다.

        Kill Switch 확인 → RPM acquire → API 호출 → 토큰 기록 순서로 실행합니다.
        """
        from core.config import settings

        # Kill Switch 확인 (월별 토큰 한도 초과 시 GeminiKillSwitchError 발생)
        settings.check_gemini_kill_switch()

        # RPM 제어 (없으면 스킵)
        if _rpm_limiter is not None:
            _rpm_limiter.acquire()

        self._ensure_model()

        response = self._genai_model.generate_content(prompt)
        token_count: int = getattr(
            getattr(response, "usage_metadata", None), "total_token_count", 0
        )
        if token_count:
            try:
                settings.record_gemini_usage(token_count)
            except Exception:
                pass   # 토큰 기록 실패는 무시

        return response.text

    @staticmethod
    def _parse_json(raw_text: str) -> dict[str, Any]:
        """Gemini 응답에서 마크다운 코드블록을 제거하고 JSON 파싱합니다."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        return json.loads(text)

    # ── 아티스트 캐시 ──────────────────────────────────────

    def _get_artists(self, force: bool = False) -> list[dict]:
        """
        아티스트 목록을 메모리 캐시에서 반환합니다 (TTL: 5분).

        force=True 이면 캐시를 무시하고 DB를 다시 조회합니다.
        """
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

        점수가 높을수록 같은 아티스트일 가능성이 높습니다.

        예시:
            detected = DetectedArtist(
                name_ko="제니",
                context_hints=["블랙핑크", "YG", "샤넬"],
            )
            candidate = {
                "name_ko": "제니 (JENNIE)",
                "agency": "YG Entertainment",
                "official_tags": {"groups": ["블랙핑크"], "brands": ["샤넬"]},
            }
            → score ≈ 0.80 (부분이름 +0.30, 소속사 +0.15, tags×2 +0.20)
        """
        score = 0.0

        # ── 이름 매칭 ────────────────────────────────────
        name_ko = detected.name_ko.strip()
        cand_ko = (candidate.get("name_ko") or "").strip()

        if name_ko and cand_ko:
            if name_ko == cand_ko:
                score += 0.50
            elif name_ko in cand_ko or cand_ko in name_ko:
                score += 0.30

        # ── 영어명 매칭 ──────────────────────────────────
        name_en = (detected.name_en or "").strip().lower()
        cand_en = (candidate.get("name_en") or "").strip().lower()

        if name_en and cand_en:
            if name_en == cand_en:
                score += 0.20
            elif name_en in cand_en or cand_en in name_en:
                score += 0.10

        # ── 컨텍스트 힌트 vs. agency ─────────────────────
        hints = [h.lower() for h in detected.context_hints if h.strip()]
        agency = (candidate.get("agency") or "").lower()

        if agency:
            for hint in hints:
                if hint and (hint in agency or agency in hint):
                    score += 0.15
                    break

        # ── 컨텍스트 힌트 vs. official_tags ──────────────
        tags_raw = candidate.get("official_tags") or {}
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
                if hint_bonus >= 3:   # 힌트 보너스 최대 3개
                    break

        return min(score, 1.0)

    # ── 컨텍스트 링킹 ──────────────────────────────────────

    def _contextual_link(
        self,
        detected_artists: list[DetectedArtist],
    ) -> list[dict]:
        """
        탐지된 아티스트 목록을 DB artists 테이블과 매칭합니다.

        각 DetectedArtist 에 대해 모든 DB 후보와 점수를 계산하고,
        _MIN_MATCH_SCORE(0.35) 이상인 최고 점수 후보를 매칭으로 결정합니다.

        Args:
            detected_artists: Gemini가 탐지한 아티스트 목록

        Returns:
            매칭 결과 목록:
            [
              {
                "detected_name_ko": "제니",
                "entity_id":        42,          # None = 미매칭
                "entity_name_ko":   "제니 (JENNIE)",
                "entity_type":      "ARTIST",
                "confidence_score": 0.85,
                "context_snippet":  "블랙핑크, YG",
                "mention_count":    3,
                "is_primary":       True,
              },
              ...
            ]
        """
        artists = self._get_artists()
        if not artists:
            log.warning("artists 캐시 비어있음 — 컨텍스트 링킹 불가 (entity_id=None 으로 저장)")

        results: list[dict] = []

        for detected in detected_artists:
            best_score: float = 0.0
            best_candidate: Optional[dict] = None

            for candidate in artists:
                score = self._score_artist_match(detected, candidate)
                if score > best_score:
                    best_score = score
                    best_candidate = candidate

            # 최소 점수 미달 시 entity_id = None (미매칭)으로 저장
            linked = best_score >= _MIN_MATCH_SCORE and best_candidate is not None
            entity_id = best_candidate["id"] if linked else None
            entity_name = best_candidate["name_ko"] if linked else detected.name_ko

            context_snippet = ", ".join(detected.context_hints[:5])

            results.append({
                "detected_name_ko": detected.name_ko,
                "entity_id":        entity_id,
                "entity_name_ko":   entity_name,
                "entity_type":      detected.entity_type,
                "confidence_score": round(best_score, 4),
                "context_snippet":  context_snippet,
                "mention_count":    detected.mention_count,
                "is_primary":       detected.is_primary,
            })

            if linked:
                log.debug(
                    "엔티티 링킹 성공 | %s → id=%d name=%s score=%.2f",
                    detected.name_ko, entity_id, entity_name, best_score,
                )
            else:
                log.debug(
                    "엔티티 링킹 실패 (score=%.2f < %.2f) | detected=%s",
                    best_score, _MIN_MATCH_SCORE, detected.name_ko,
                )

        return results

    # ── 핵심: Gemini 지식 추출 ─────────────────────────────

    def _extract_intelligence(
        self,
        title_ko: Optional[str],
        content_ko: Optional[str],
    ) -> ArticleIntelligence:
        """
        Gemini API 를 호출하여 기사의 엔티티/지식을 추출합니다.

        프롬프트에 제목과 본문(최대 _TEXT_MAX_CHARS 자)을 삽입하여
        ArticleIntelligence 구조화 응답을 생성합니다.
        """
        title   = (title_ko   or "").strip() or "제목 없음"
        content = (content_ko or "").strip()

        if len(content) > _TEXT_MAX_CHARS:
            content = content[:_TEXT_MAX_CHARS] + "\n...(이하 생략)"

        if not content:
            log.warning("content_ko 없음 — 제목만으로 분석 (신뢰도 낮을 수 있음)")

        prompt  = _INTELLIGENCE_PROMPT.format(title=title, content=content)
        raw     = self._call_gemini(prompt)
        data    = self._parse_json(raw)
        result  = ArticleIntelligence.model_validate(data)

        log.info(
            "Gemini 추출 완료 | artists=%d sentiment=%s "
            "relevance=%.2f confidence=%.2f category=%s",
            len(result.detected_artists),
            result.sentiment,
            result.relevance_score,
            result.confidence,
            result.main_category,
        )
        return result

    # ── 단일 기사 처리 ─────────────────────────────────────

    def process_article(self, article: dict) -> ProcessingResult:
        """
        단일 기사를 처리합니다.

        처리 순서:
            1. Gemini로 ArticleIntelligence 추출
            2. 컨텍스트 링킹: detected_artists → entity_id 매칭
            3. entity_mappings 교체 (DELETE + INSERT)
            4. process_status 결정 (PROCESSED / MANUAL_REVIEW)
            5. DB 상태 업데이트
            6. system_logs 성공 기록

        실패 시:
            - process_status = ERROR
            - system_logs 에 ERROR 레벨 기록

        Args:
            article: _claim_pending_articles() 반환 딕셔너리

        Returns:
            ProcessingResult
        """
        article_id = article["id"]
        job_id     = article.get("job_id")
        t_start    = time.monotonic()

        try:
            # ── 1. Gemini 추출 ───────────────────────────
            intelligence = self._extract_intelligence(
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
                log.debug("entity_mappings 저장 | article_id=%d count=%d", article_id, saved)

            # ── 4. 상태 결정 ─────────────────────────────
            ok_relevance  = intelligence.relevance_score >= _MIN_RELEVANCE
            ok_confidence = intelligence.confidence >= _MIN_CONFIDENCE
            final_status  = "PROCESSED" if (ok_relevance and ok_confidence) else "MANUAL_REVIEW"

            if final_status == "MANUAL_REVIEW":
                log.info(
                    "신뢰도 미달 → MANUAL_REVIEW | article_id=%d "
                    "relevance=%.2f(≥%.2f?) confidence=%.2f(≥%.2f?)",
                    article_id,
                    intelligence.relevance_score, _MIN_RELEVANCE,
                    intelligence.confidence,       _MIN_CONFIDENCE,
                )

            # ── 5. DB 업데이트 ───────────────────────────
            _update_article_status(
                article_id,
                final_status,
                topic_summary = intelligence.topic_summary or None,
            )

            duration_ms = int((time.monotonic() - t_start) * 1000)

            # ── 6. 성공 로그 ──────────────────────────────
            _log_to_system(
                article_id  = article_id,
                level       = "INFO",
                event       = "entity_extract_success",
                message     = f"엔티티 추출 완료 ({final_status}) | "
                              f"artists={len(linked)} "
                              f"relevance={intelligence.relevance_score:.2f} "
                              f"confidence={intelligence.confidence:.2f}",
                details     = {
                    "status":           final_status,
                    "sentiment":        intelligence.sentiment,
                    "relevance_score":  intelligence.relevance_score,
                    "confidence":       intelligence.confidence,
                    "main_category":    intelligence.main_category,
                    "detected_artists": [m["detected_name_ko"] for m in linked],
                    "linked_artist_ids": [
                        m["entity_id"] for m in linked if m["entity_id"] is not None
                    ],
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
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            error_msg   = f"{type(exc).__name__}: {exc}"
            log.exception(
                "기사 처리 실패 | article_id=%d error=%s", article_id, error_msg
            )

            # ── ERROR 상태 전환 ──────────────────────────
            try:
                _update_article_status(article_id, "ERROR")
            except Exception as db_exc:
                log.error(
                    "ERROR 상태 업데이트 실패 | article_id=%d err=%r", article_id, db_exc
                )

            # ── 에러 로그 기록 ───────────────────────────
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

        SELECT FOR UPDATE SKIP LOCKED 로 기사를 원자적으로 클레임하고
        하나씩 순차 처리합니다. SIGTERM/SIGINT 를 받아도 현재 기사 완료 후 종료됩니다.

        Args:
            batch_size: 처리할 최대 기사 수 (None = self.batch_size)
            job_id:     특정 job_id 의 기사만 처리 (None = 전체)

        Returns:
            BatchResult (total, processed, manual_review, failed)
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
            "배치 처리 시작 | count=%d job_id=%s model=%s",
            len(articles), job_id, self.model_name,
        )

        for i, article in enumerate(articles, start=1):
            article_result = self.process_article(article)
            log.info(
                "[%d/%d] article_id=%d → %s (%dms)",
                i, len(articles),
                article_result.article_id,
                article_result.status,
                article_result.duration_ms,
            )

            if article_result.status == "PROCESSED":
                result.processed += 1
            elif article_result.status == "MANUAL_REVIEW":
                result.manual_review += 1
            else:
                result.failed += 1

        log.info(
            "배치 처리 완료 | total=%d processed=%d manual_review=%d failed=%d",
            result.total, result.processed, result.manual_review, result.failed,
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
        python -m processor.gemini_engine --model gemini-2.0-flash --batch-size 20
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="TIH Gemini Intelligence Engine — Phase 4 Entity Extraction",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE,
        metavar="N",
        help=f"처리할 기사 수 (기본: {_BATCH_SIZE})",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        metavar="ID",
        help="특정 job_id 의 기사만 처리 (기본: 전체)",
    )
    parser.add_argument(
        "--model",
        default=_INTELLIGENCE_MODEL,
        metavar="MODEL",
        help=f"Gemini 모델명 (기본: {_INTELLIGENCE_MODEL})",
    )
    args = parser.parse_args(argv)

    _setup_logging()

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
        f"failed={result.failed}"
    )


if __name__ == "__main__":
    main()
