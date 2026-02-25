"""
processor/gemini_engine.py — Phase 4: Gemini Entity Extraction Engine (v3)

역할:
    1. Structured Output (엔티티별 신뢰도 포함)
       ArticleIntelligence Pydantic 모델로 Gemini 응답 형식을 강제합니다.
       DetectedArtist 에 confidence_score / is_ambiguous / ambiguity_reason 추가.

    2. 이중 언어 추출 (v3 신규)
       한 번의 Gemini 호출로 한국어 + 영어 제목·요약을 동시에 생성합니다.
         - title_ko / topic_summary      : 한국어 (원문 기반)
         - title_en / topic_summary_en   : 영어 (K-POP 팬 친화적, 직역 금지)
       영문 필드 누락 시 → MANUAL_REVIEW 자동 라우팅.

    3. K-POP 문화 현지화 (v3 신규)
       한국어 K-엔터 고유 표현('역주행', '대세돌' 등)을 영어권 팬이 이해하는
       표현('viral comeback', 'trending it-idol' 등)으로 변환하도록 Gemini 를
       가이드합니다.

    4. Contextual Linking
       탐지된 아티스트명의 문맥(소속사, 그룹, 브랜드 등)을 분석하여
       DB artists 테이블의 아티스트 ID와 매칭합니다.

    5. 조건부 상태 전환 (_decide_status)
       ┌ VERIFIED      : [Phase 4-B] 모든 PROCESSED 조건 충족
       │                 AND overall confidence ≥ 0.95 (_AUTO_COMMIT_THRESHOLD)
       │                 → 운영자 확인 없이 즉시 반영
       ├ PROCESSED     : 모든 엔티티 confidence_score ≥ 0.80
       │                 AND 모호한 엔티티 없음 (is_ambiguous=False)
       │                 AND relevance_score ≥ 0.30
       │                 AND overall confidence ≥ 0.60
       │                 AND title_en / topic_summary_en 모두 비어있지 않음 (v3)
       ├ MANUAL_REVIEW : 위 조건 중 하나라도 미충족
       │                 → system_note 에 AI 판단 모호 이유 기록
       └ ERROR         : Gemini 호출 실패 / JSON 파싱 실패 / DB 오류

    6. [Phase 4-B] 자율형 데이터 정제 (Zero-Ops Logic)
       Cross-Validation    : Gemini 추출 값 vs DB 기존 프로필 비교
                             일치/비어있음 → 즉시 업데이트 + confidence 보정
       Auto-Reconciliation : 상충 시 2차 Gemini 호출 → 더 정확한 값으로 자동 갱신
                             (이전 값은 삭제 않고 data_update_logs 에 아카이빙)
       Smart Glossary      : 미매핑 신규 엔티티 → glossary 에 Auto-Provisioned 즉시 등록
       Auto-Commit         : (see 5. VERIFIED)

    6. 비용 분석 로그 (GeminiCallMetrics)
       Gemini API 호출마다 prompt_tokens, completion_tokens,
       total_tokens, response_time_ms 를 측정하여
       system_logs.details 에 기록합니다.

상수:
    _ENTITY_CONFIDENCE_THRESHOLD = 0.80  엔티티별 자동승인 임계값
    _MIN_RELEVANCE               = 0.30  기사 관련도 최솟값
    _MIN_CONFIDENCE              = 0.60  전체 분석 신뢰도 최솟값
    _AUTO_COMMIT_THRESHOLD       = 0.95  [Phase 4-B] VERIFIED 자동 승인 임계값

CLI 사용 예:
    python -m processor.gemini_engine                        # PENDING 10건
    python -m processor.gemini_engine --job-id 7             # 특정 job
    python -m processor.gemini_engine --model gemini-2.0-flash --batch-size 20
"""

from __future__ import annotations

import enum
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

# 용어 사전(glossary) 캐시 TTL — 10분
_GLOSSARY_CACHE_TTL: float = float(os.getenv("GLOSSARY_CACHE_TTL", "600"))

# [Phase 4-B] Threshold-based Auto-Commit
# intelligence.confidence 가 이 값 이상이면 운영자 확인 없이 VERIFIED 로 즉시 반영
_AUTO_COMMIT_THRESHOLD: float = float(os.getenv("AUTO_COMMIT_THRESHOLD", "0.95"))

# [Phase 4-B] 아티스트 필드 업데이트 화이트리스트 (SQL 인젝션 방지)
_UPDATABLE_ARTIST_FIELDS: frozenset[str] = frozenset({
    "name_en", "nationality_ko", "nationality_en",
    "mbti", "blood_type", "height_cm", "weight_kg",
})


# ─────────────────────────────────────────────────────────────
# 번역 티어 (선택적 번역 — 비용 최적화)
# ─────────────────────────────────────────────────────────────

class TranslationTier(str, enum.Enum):
    """
    artists.global_priority 기반 선택적 번역 티어.

    DB 매핑:
        priority 1 (FULL)       : 제목 + 본문 요약 전체 번역 + SEO 해시태그
                                   (글로벌 팬덤 아티스트 — BTS, BLACKPINK 등)
        priority 2 (TITLE_ONLY) : 영문 제목 + 3문장 요약만 번역 + SEO 해시태그
                                   (국내 인지도 있으나 글로벌 팬덤 제한)
        priority 3 (KO_ONLY)    : 한국어 엔티티 추출만 — 번역·해시태그 없음
                                   (신인 / 국내 소규모 아티스트)
        NULL / 미분류           → FULL (누락 방지 기본값)
    """
    FULL       = "full"        # priority 1 — 전체 번역 + 해시태그
    TITLE_ONLY = "title_only"  # priority 2 — 제목 + 3문장 요약 + 해시태그
    KO_ONLY    = "ko_only"     # priority 3 — 한국어 엔티티 추출만


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
    processor/gemini_engine.py 전용 Gemini 구조화 응답 모델 (v3).

    scraper/gemini_engine.py 의 ArticleExtracted 와 별개:
      - ArticleExtracted:    스크래핑 시 제목/본문/해시태그 추출 (Phase 3)
      - ArticleIntelligence: 저장된 기사의 엔티티/지식 추출   (Phase 4)

    v3 신규 필드:
      - title_ko         : 한국어 기사 제목 (Gemini 확인/정제)
      - title_en         : K-POP 팬 친화적 영어 제목 (직역 금지)
      - topic_summary_en : 핵심 주제 영문 요약 (3문장 이내, K-POP 팬 친화적)
    """

    # ── v3: 이중 언어 제목 ───────────────────────────────────
    title_ko: str = Field(
        "",
        max_length=300,
        description="한국어 기사 제목 (Gemini 확인/정제, 원문 기반)",
    )
    title_en: str = Field(
        "",
        max_length=300,
        description=(
            "K-POP 팬 친화적 영어 제목 (직역 금지).\n"
            "  - 아티스트 영어명 우선 사용\n"
            "  - 글로벌 SNS 에서 통용되는 K-POP 표현 활용\n"
            "  - 비어있으면 → MANUAL_REVIEW 자동 라우팅"
        ),
    )
    detected_artists: list[DetectedArtist] = Field(
        default_factory=list,
        description="기사에 등장하는 모든 아티스트/그룹/행사",
    )
    topic_summary: str = Field(
        "",
        max_length=300,
        description="핵심 주제 요약 (300자 이내, 한국어)",
    )
    # ── v3: 영문 요약 ────────────────────────────────────────
    topic_summary_en: str = Field(
        "",
        max_length=500,
        description=(
            "핵심 주제 영문 요약 (3문장 이내, K-POP 팬 친화적).\n"
            "  - 단순 직역 금지\n"
            "  - 역주행→'viral comeback', 대세돌→'trending it-idol' 등 현지화 적용\n"
            "  - KO_ONLY 티어에서는 빈 문자열 허용\n"
            "  - FULL/TITLE_ONLY 에서 비어있으면 → MANUAL_REVIEW 자동 라우팅"
        ),
    )
    # ── v3: SEO 해시태그 (FULL/TITLE_ONLY 티어만) ────────────
    seo_hashtags: list[str] = Field(
        default_factory=list,
        description=(
            "글로벌 SEO 해시태그 (Tier 1/2만 생성, 5~10개).\n"
            "  - 반드시 # 으로 시작\n"
            "  - 북미/유럽 K-POP 팬이 X/Instagram에서 자주 쓰는 태그\n"
            "  - KO_ONLY 티어에서는 빈 리스트"
        ),
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

    @field_validator("title_ko", "title_en")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return v.strip()

    @field_validator("topic_summary")
    @classmethod
    def _clean_summary(cls, v: str) -> str:
        return v.strip()

    @field_validator("topic_summary_en")
    @classmethod
    def _clean_summary_en(cls, v: str) -> str:
        return v.strip()

    @field_validator("seo_hashtags")
    @classmethod
    def _validate_hashtags(cls, v: list[str]) -> list[str]:
        """# 접두어 정규화, 공백 제거, 최대 15개 제한."""
        cleaned: list[str] = []
        for tag in v:
            tag = tag.strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = "#" + tag
            cleaned.append(tag)
        return cleaned[:15]

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
    verified:      int = 0   # [Phase 4-B] confidence ≥ 0.95 자동 승인 건수
    manual_review: int = 0
    failed:        int = 0
    total_tokens:  int = 0   # [v2] 배치 전체 토큰 합계

    def to_dict(self) -> dict:
        return {
            "total":         self.total,
            "processed":     self.processed,
            "verified":      self.verified,
            "manual_review": self.manual_review,
            "failed":        self.failed,
            "total_tokens":  self.total_tokens,
        }


# ─────────────────────────────────────────────────────────────
# Gemini 프롬프트 빌더 (v3 — 번역 티어별 동적 생성)
# ─────────────────────────────────────────────────────────────

# 공통: 엔티티 분석 규칙 블록 (모든 티어에 포함)
_ENTITY_RULES = textwrap.dedent("""\
    ─────────────────────────────────────────────────────────────
    ▶ 엔티티 분석 규칙 (모든 티어 공통)
    ─────────────────────────────────────────────────────────────
    detected_artists: 기사에 직접 언급된 모든 가수·그룹·배우·MC를 포함하세요.
      - context_hints: 소속사(YG, SM, HYBE 등), 그룹명, 브랜드, 드라마/앨범 제목
      - entity_type: ARTIST(솔로), GROUP(그룹/팀), EVENT(시상식/행사)

    confidence_score (0.0~1.0): 해당 아티스트 탐지의 신뢰도를 직접 평가하세요.
      - 0.9~1.0: 이름과 문맥이 명확하여 특정 아티스트임을 확신
      - 0.7~0.9: 대부분 확신하나 일부 모호함
      - 0.5~0.7: 동명이인이나 문맥 부족으로 불확실
      - 0.0~0.5: 매우 모호하거나 본문에 직접적인 증거 없음

    is_ambiguous: 동명이인이나 문맥 모호로 정확한 아티스트 특정이 어려우면 true
      예: '지수' → JISOO(블랙핑크)인지 다른 지수인지 모호하면 true
      예: '뷔' → BTS V가 명확하면 false

    ambiguity_reason: is_ambiguous=true일 때 모호한 이유 한 문장으로 설명

    sentiment: positive | negative | neutral | mixed
    relevance_score: K-팝·K-드라마 등 K-엔터 관련도 (0.0~1.0)
    confidence: 분석 전체 신뢰도 (정보 충분→높게, 부족→낮게)
    main_category: music|drama|film|fashion|entertainment|award|other
""")

# 공통: 번역 가이드 블록 (FULL / TITLE_ONLY 티어만)
_TRANSLATION_RULES = textwrap.dedent("""\
    ─────────────────────────────────────────────────────────────
    ▶ 이중 언어 번역 규칙 (CRITICAL — 반드시 준수)
    ─────────────────────────────────────────────────────────────
    title_en: 영어 제목은 단순 직역이 아닌, 글로벌 K-POP 팬이 트위터·레딧에서
      쓸 법한 자연스러운 표현으로 작성하세요. 아티스트 영어명을 우선 사용하세요.
      예: "방탄소년단, 신곡 공개" → "BTS Drops New Single"
      예: "블랙핑크 제니, 솔로 컴백 확정" → "BLACKPINK's Jennie Confirms Solo Comeback"

    topic_summary_en: 영어 요약도 단순 직역 금지. 3문장 이내, 글로벌 K-POP 팬
      커뮤니티(트위터/레딧)에서 사용하는 표현을 활용하세요.

    title_en 과 topic_summary_en 은 반드시 비어있지 않아야 합니다.
    영어 번역이 어려운 경우에도 최선의 영어 표현을 반드시 제공하세요.

    ─────────────────────────────────────────────────────────────
    ▶ 한국어 K-엔터 고유 표현 → 영어 변환 가이드 (Localization)
    ─────────────────────────────────────────────────────────────
    아래 표현이 기사에 등장하면 괄호 안의 영어 표현으로 번역하세요:
      역주행        → "viral comeback" / "reverse chart surge"
      대세돌        → "trending it-idol" / "breakout star"
      컴백          → "comeback"
      음방          → "music show performance" / "music show stage"
      초동          → "first-week sales"
      더블타이틀    → "double title track"
      완전체        → "full group lineup" / "all-member"
      선공개        → "pre-released track" / "pre-release"
      뮤뱅/뮤직뱅크 → "Music Bank"
      인기가요      → "Inkigayo"
      엠카운트다운  → "M Countdown"
      음원          → "digital single" / "streaming release"
      차트인         → "chart entry" / "charted on"
      솔로          → "solo debut" / "solo release"
      팬미팅        → "fan meeting"
      월드투어      → "world tour"
      데뷔          → "debut"
      타이틀곡      → "title track"
      수록곡        → "b-side track" / "album track"
      팬덤          → "fandom"
      스밍          → "streaming"
""")

# 공통: SEO 해시태그 규칙 (FULL / TITLE_ONLY 티어만)
_HASHTAG_RULES_FULL = textwrap.dedent("""\
    ─────────────────────────────────────────────────────────────
    ▶ 글로벌 SEO 해시태그 생성 규칙 (seo_hashtags)
    ─────────────────────────────────────────────────────────────
    북미/유럽 K-POP 팬들이 X(트위터)·Instagram에서 가장 많이 쓰는 영어 해시태그
    5~10개를 생성하세요.
      - 반드시 # 으로 시작 (예: #KPOP, #BTS)
      - 아티스트 공식 영어명 태그 포함 (예: #BTS, #BLACKPINK)
      - 장르·트렌드 태그 포함 (예: #KPOP, #KPOPTwitter, #KPOPNews)
      - 이벤트·앨범 관련 태그 포함 (예: #NewMusic, #Comeback, #MusicVideo)
      - 팬덤명 태그 포함 시 우선 (예: #ARMY, #BLINK)
      - 예시: ["#KPOP", "#BTS", "#ARMY", "#NewSingle", "#KPOPTwitter"]
""")

_HASHTAG_RULES_TITLE_ONLY = textwrap.dedent("""\
    ─────────────────────────────────────────────────────────────
    ▶ SEO 해시태그 생성 규칙 (seo_hashtags — 5~7개)
    ─────────────────────────────────────────────────────────────
    아티스트명 + 장르 + 이벤트 관련 영어 해시태그 5~7개를 생성하세요.
      - 반드시 # 으로 시작
      - 예시: ["#KPOP", "#ArtistName", "#NewMusic", "#Comeback", "#KPOPNews"]
""")


def _build_glossary_section(glossary: list[dict]) -> str:
    """
    DB glossary 데이터를 프롬프트 삽입용 문자열로 변환합니다.

    카테고리별로 그룹화하여 아티스트 → 소속사 → 공연/방송 순으로 출력합니다.
    glossary 가 비어있으면 빈 문자열을 반환합니다.
    """
    if not glossary:
        return ""

    by_cat: dict[str, list[dict]] = {}
    for entry in glossary:
        cat = entry.get("category", "OTHER")
        by_cat.setdefault(cat, []).append(entry)

    cat_labels = {
        "ARTIST": "아티스트/그룹명",
        "AGENCY": "소속사명",
        "EVENT":  "공연·방송·시상식명",
    }

    lines = [
        "─────────────────────────────────────────────────────────────",
        "▶ 필수 영문 표기 가이드 (Glossary — 반드시 준수)",
        "─────────────────────────────────────────────────────────────",
        "아래 한국어 표현은 반드시 지정된 영어 표기를 사용하세요:",
    ]
    for cat_key in ("ARTIST", "AGENCY", "EVENT"):
        entries = by_cat.get(cat_key, [])
        if not entries:
            continue
        lines.append(f"\n  [{cat_labels.get(cat_key, cat_key)}]")
        for e in entries:
            ko = e.get("term_ko", "")
            en = e.get("term_en", "")
            if ko and en:
                desc = e.get("description", "")
                suffix = f"  ({desc})" if desc else ""
                lines.append(f"    {ko} → {en}{suffix}")
    lines.append("")
    return "\n".join(lines)


def _build_prompt(
    title: str,
    content: str,
    tier: TranslationTier,
    glossary: Optional[list[dict]] = None,
) -> str:
    """
    번역 티어 + 용어 사전을 기반으로 Gemini 프롬프트를 동적으로 생성합니다.

    Tier별 차이:
        FULL       : 전체 이중 언어 분석 + 용어 사전 + SEO 해시태그 (5~10개)
        TITLE_ONLY : 영문 제목 + 3문장 요약 + 용어 사전 + SEO 해시태그 (5~7개)
        KO_ONLY    : 한국어 엔티티 추출만 (번역·사전·해시태그 없음)
    """
    glossary = glossary or []

    # ── 공통: 시스템 역할 선언 ─────────────────────────────
    if tier == TranslationTier.KO_ONLY:
        role_line = (
            "당신은 K-엔터테인먼트 전문 AI 분석가입니다.\n"
            "아래 기사에서 한국어 엔티티만 추출하세요. 영어 번역은 불필요합니다."
        )
    else:
        role_line = (
            "당신은 K-엔터테인먼트 전문 AI 분석가이자 글로벌 K-POP 콘텐츠 번역가입니다.\n"
            "아래 기사를 분석하고 이중 언어 데이터를 생성하세요."
        )

    # ── 공통: 기사 본문 ───────────────────────────────────
    article_block = (
        f"=== 기사 ===\n"
        f"제목: {title}\n"
        f"본문:\n{content}\n"
        f"=== 끝 ==="
    )

    # ── 티어별 JSON 응답 형식 ─────────────────────────────
    artist_schema = textwrap.dedent("""\
        {
          "name_ko": "한국어 아티스트명",
          "name_en": "English name or null",
          "context_hints": ["소속사", "그룹명", "브랜드"],
          "mention_count": 3,
          "is_primary": true,
          "entity_type": "ARTIST",
          "confidence_score": 0.95,
          "is_ambiguous": false,
          "ambiguity_reason": null
        }""")

    if tier == TranslationTier.KO_ONLY:
        json_schema = textwrap.dedent(f"""\
            응답 JSON 형식 (한국어 엔티티 추출 — 영어 필드 제외):
            {{{{
              "detected_artists": [
                {artist_schema}
              ],
              "topic_summary": "핵심 주제 요약 (2문장 이내, 한국어)",
              "sentiment": "positive",
              "relevance_score": 0.95,
              "main_category": "music",
              "confidence": 0.88
            }}}}""")
    elif tier == TranslationTier.TITLE_ONLY:
        json_schema = textwrap.dedent(f"""\
            응답 JSON 형식 (영문 제목 + 3문장 요약 번역):
            {{{{
              "title_ko": "한국어 기사 제목 (50자 이내)",
              "title_en": "K-POP Fan-Friendly English Title (max 100 chars)",
              "detected_artists": [
                {artist_schema}
              ],
              "topic_summary": "핵심 주제 요약 (2문장 이내, 한국어)",
              "topic_summary_en": "Key summary in English (max 3 sentences, K-POP fan-friendly)",
              "seo_hashtags": ["#KPOP", "#ArtistName", "#NewMusic"],
              "sentiment": "positive",
              "relevance_score": 0.95,
              "main_category": "music",
              "confidence": 0.88
            }}}}""")
    else:  # FULL
        json_schema = textwrap.dedent(f"""\
            응답 JSON 형식 (전체 이중 언어 번역):
            {{{{
              "title_ko": "한국어 기사 제목 (50자 이내)",
              "title_en": "K-POP Fan-Friendly English Title (NOT a literal translation, max 100 chars)",
              "detected_artists": [
                {artist_schema}
              ],
              "topic_summary": "핵심 주제 요약 (3문장 이내, 한국어)",
              "topic_summary_en": "Key summary in English (max 3 sentences, K-POP fan-friendly tone)",
              "seo_hashtags": ["#KPOP", "#BTS", "#NewMusic", "#KPOPTwitter"],
              "sentiment": "positive",
              "relevance_score": 0.95,
              "main_category": "music",
              "confidence": 0.88
            }}}}""")

    # ── 부가 규칙 섹션 조합 ───────────────────────────────
    sections: list[str] = []

    # 용어 사전 주입 (FULL / TITLE_ONLY 만)
    if tier != TranslationTier.KO_ONLY:
        glossary_section = _build_glossary_section(glossary)
        if glossary_section:
            sections.append(glossary_section)

    # 번역 규칙 (FULL / TITLE_ONLY 만)
    if tier != TranslationTier.KO_ONLY:
        sections.append(_TRANSLATION_RULES)

    # SEO 해시태그 규칙
    if tier == TranslationTier.FULL:
        sections.append(_HASHTAG_RULES_FULL)
    elif tier == TranslationTier.TITLE_ONLY:
        sections.append(_HASHTAG_RULES_TITLE_ONLY)

    # 엔티티 분석 규칙 (모든 티어)
    sections.append(_ENTITY_RULES)

    prompt_parts = [
        role_line,
        "JSON 외 다른 텍스트(설명, 주석, 마크다운 코드블록 등)는 절대 포함하지 마세요.",
        "",
        article_block,
        "",
        json_schema,
        "",
        *sections,
    ]
    return "\n".join(prompt_parts)


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
    """[v2] artists 테이블 전체를 캐시용으로 조회합니다.

    v2 스키마 변경 반영: agency / official_tags 컬럼 제거.
    stage_name_ko / stage_name_en 추가 (별명 매칭 지원).
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name_ko, name_en,
                       stage_name_ko, stage_name_en,
                       global_priority, is_verified
                FROM   artists
                ORDER  BY global_priority ASC NULLS LAST, id ASC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def _update_article_status(
    article_id: int,
    status: str,
    topic_summary: Optional[str] = None,
    system_note: Optional[str] = None,         # [v2]
    title_en: Optional[str] = None,            # [v3] K-POP 팬 친화적 영문 제목
    summary_en: Optional[str] = None,          # [v3] 영문 요약
    hashtags_en: Optional[list[str]] = None,   # [v3] SEO 해시태그 단순 배열
    seo_hashtags: Optional[dict] = None,       # [v3] SEO 해시태그 JSONB (메타데이터 포함)
) -> None:
    """
    기사 process_status 를 갱신합니다.

    [v2] system_note: MANUAL_REVIEW 사유. NULL → 기존 유지, '' → 명시적 NULL 초기화.
    [v3] title_en / summary_en / hashtags_en / seo_hashtags:
         NULL 전달 시 기존 값 유지. 값 전달 시 갱신.
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
                       title_en      = CASE WHEN %s IS NOT NULL THEN %s ELSE title_en END,
                       summary_en    = CASE WHEN %s IS NOT NULL THEN %s ELSE summary_en END,
                       hashtags_en   = CASE WHEN %s IS NOT NULL THEN %s ELSE hashtags_en END,
                       seo_hashtags  = CASE WHEN %s IS NOT NULL THEN %s::jsonb ELSE seo_hashtags END,
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
                    topic_summary or None,       # summary_ko fallback
                    title_en,                    # CASE: title_en IS NOT NULL → update
                    title_en,                    # SET title_en
                    summary_en,                  # CASE: summary_en IS NOT NULL → update
                    summary_en,                  # SET summary_en
                    hashtags_en,                 # CASE: hashtags_en IS NOT NULL → update
                    hashtags_en,                 # SET hashtags_en (TEXT[])
                    json.dumps(seo_hashtags, ensure_ascii=False) if seo_hashtags else None,
                    json.dumps(seo_hashtags, ensure_ascii=False) if seo_hashtags else None,
                    system_note or "",           # CASE: empty string → NULL
                    system_note,                 # CASE: not null → update
                    system_note,                 # SET value
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


def _read_pending_articles_dry(
    limit: int = _BATCH_SIZE,
    job_id: Optional[int] = None,
) -> list[dict]:
    """
    [DRY RUN] PENDING 기사를 상태 변경 없이 읽기 전용으로 조회합니다.

    _claim_pending_articles() 와 달리 SELECT FOR UPDATE 와 status → SCRAPED 업데이트를
    수행하지 않습니다. 드라이 런에서 DB 에 아무런 흔적을 남기지 않습니다.
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
        """
        params = {"limit": limit}

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# Phase 4-B: DB 헬퍼 (증거 기반 업데이트 + Glossary 자동 등록)
# ─────────────────────────────────────────────────────────────

def _get_artist_profile_v2(artist_id: int) -> Optional[dict]:
    """
    [Phase 4-B] artists 테이블에서 단일 아티스트 프로필을 조회합니다.

    Cross-Validation 에서 Gemini 추출 값과 비교하는 데 사용합니다.
    조회 실패 시 None 을 반환하여 처리를 중단하지 않습니다.
    """
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name_ko, name_en, stage_name_ko, stage_name_en,
                           nationality_ko, nationality_en, mbti, blood_type,
                           height_cm, weight_kg, is_verified, global_priority
                    FROM   artists
                    WHERE  id = %s
                    """,
                    (artist_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        log.warning(
            "아티스트 프로필 조회 실패 | artist_id=%d err=%r", artist_id, exc
        )
        return None


def _update_artist_field_v2(
    artist_id:  int,
    field:      str,
    new_value:  Any,
    old_value:  Any,
    article_id: int,
    updated_by: str = "ai_pipeline",
) -> bool:
    """
    [Phase 4-B] artists 테이블의 단일 필드를 갱신하고 data_update_logs 에 기록합니다.

    `field` 는 _UPDATABLE_ARTIST_FIELDS 화이트리스트에 있어야 합니다.
    SQL 인젝션 방지를 위해 화이트리스트 체크를 통과한 필드명만 f-string 으로 사용합니다.

    Returns:
        True — 업데이트 및 로그 기록 성공
        False — 화이트리스트 위반 또는 DB 오류
    """
    if field not in _UPDATABLE_ARTIST_FIELDS:
        log.error(
            "허용되지 않은 아티스트 필드 업데이트 시도 | field=%r (허용: %s)",
            field, ", ".join(sorted(_UPDATABLE_ARTIST_FIELDS)),
        )
        return False

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # 1. 필드 갱신
                cur.execute(
                    f"UPDATE artists SET {field} = %s, updated_at = NOW() WHERE id = %s",
                    (new_value, artist_id),
                )
                # 2. DataUpdateLog 기록 (The Core — 증거 기반 감사 로그)
                cur.execute(
                    """
                    INSERT INTO data_update_logs
                        (article_id, entity_type, entity_id, field_name,
                         old_value_json, new_value_json, updated_by)
                    VALUES (%s, 'ARTIST'::entity_type_enum, %s, %s,
                            %s::jsonb, %s::jsonb, %s)
                    """,
                    (
                        article_id,
                        artist_id,
                        field,
                        json.dumps({"value": old_value}, ensure_ascii=False, default=str),
                        json.dumps({"value": new_value}, ensure_ascii=False, default=str),
                        updated_by,
                    ),
                )
        log.info(
            "[Phase4B] 아티스트 필드 자동 업데이트 | artist_id=%d field=%s %r → %r",
            artist_id, field, old_value, new_value,
        )
        return True
    except Exception as exc:
        log.error(
            "아티스트 필드 업데이트 실패 | artist_id=%d field=%s err=%r",
            artist_id, field, exc,
        )
        return False


def _glossary_enroll_auto(
    term_ko:    str,
    term_en:    str,
    category:   str,
    article_id: Optional[int],
) -> bool:
    """
    [Phase 4-B] Smart Glossary Auto-Enroll.

    glossary 테이블에 Auto-Provisioned 용어를 등록합니다.
    ON CONFLICT (term_ko, category) DO NOTHING — 이미 존재하면 False 반환.

    Returns:
        True  — 신규 등록 성공
        False — 이미 존재하거나 DB 오류
    """
    if not term_ko.strip() or not term_en.strip():
        return False

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO glossary
                        (term_ko, term_en, category, description,
                         is_auto_provisioned, source_article_id)
                    VALUES (%s, %s, %s::glossary_category_enum,
                            %s, TRUE, %s)
                    ON CONFLICT (term_ko, category) DO NOTHING
                    """,
                    (
                        term_ko.strip(),
                        term_en.strip(),
                        category,
                        f"Auto-Provisioned (article #{article_id})",
                        article_id,
                    ),
                )
                enrolled = cur.rowcount > 0
        if enrolled:
            log.info(
                "[Phase4B] Glossary 자동 등록 | term_ko=%r term_en=%r "
                "category=%s article_id=%s",
                term_ko, term_en, category, article_id,
            )
        return enrolled
    except Exception as exc:
        log.warning(
            "Glossary 자동 등록 실패 | term_ko=%r err=%r", term_ko, exc
        )
        return False


def _log_auto_resolution(
    article_id:         Optional[int],
    entity_type:        str,
    entity_id:          int,
    field_name:         str,
    old_value:          Any,
    new_value:          Any,
    resolution_type:    str,
    gemini_reasoning:   Optional[str] = None,
    gemini_confidence:  Optional[float] = None,
    source_reliability: float = 0.0,
) -> None:
    """
    [Phase 2-D] auto_resolution_logs 에 AI 자율 결정 이력을 기록합니다.

    resolution_type: "FILL" | "RECONCILE" | "ENROLL"
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO auto_resolution_logs
                        (article_id, entity_type, entity_id, field_name,
                         old_value_json, new_value_json, resolution_type,
                         gemini_reasoning, gemini_confidence, source_reliability)
                    VALUES (%s, %s::entity_type_enum, %s, %s,
                            %s::jsonb, %s::jsonb,
                            %s::auto_resolution_type_enum,
                            %s, %s, %s)
                    """,
                    (
                        article_id,
                        entity_type,
                        entity_id,
                        field_name,
                        json.dumps({"value": old_value},  ensure_ascii=False, default=str),
                        json.dumps({"value": new_value},  ensure_ascii=False, default=str),
                        resolution_type,
                        gemini_reasoning,
                        gemini_confidence,
                        max(0.0, min(1.0, source_reliability)),
                    ),
                )
    except Exception as exc:
        log.warning(
            "auto_resolution_logs 기록 실패 | entity=%s:%d field=%s type=%s err=%r",
            entity_type, entity_id, field_name, resolution_type, exc,
        )


def _log_conflict_flag(
    article_id:        Optional[int],
    entity_type:       str,
    entity_id:         int,
    field_name:        str,
    existing_value:    Any,
    conflicting_value: Any,
    conflict_reason:   str,
    conflict_score:    float = 0.5,
) -> None:
    """
    [Phase 2-D] conflict_flags 에 자율 해결 불가 모순을 기록합니다.

    conflict_score 가이드:
        0.0 ~ 0.3 : 사소한 차이
        0.3 ~ 0.7 : 중간 모순
        0.7 ~ 1.0 : 심각한 모순 (이름이 완전히 다름 등)
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conflict_flags
                        (article_id, entity_type, entity_id, field_name,
                         existing_value_json, conflicting_value_json,
                         conflict_reason, conflict_score)
                    VALUES (%s, %s::entity_type_enum, %s, %s,
                            %s::jsonb, %s::jsonb, %s, %s)
                    """,
                    (
                        article_id,
                        entity_type,
                        entity_id,
                        field_name,
                        json.dumps({"value": existing_value},    ensure_ascii=False, default=str),
                        json.dumps({"value": conflicting_value}, ensure_ascii=False, default=str),
                        conflict_reason,
                        max(0.0, min(1.0, conflict_score)),
                    ),
                )
        log.warning(
            "[Phase2D] ConflictFlag 기록 | entity=%s:%d field=%s score=%.2f | "
            "existing=%r conflicting=%r",
            entity_type, entity_id, field_name, conflict_score,
            existing_value, conflicting_value,
        )
    except Exception as exc:
        log.warning(
            "conflict_flags 기록 실패 | entity=%s:%d field=%s err=%r",
            entity_type, entity_id, field_name, exc,
        )


def _update_entity_verified_at(entity_type: str, entity_id: int) -> None:
    """
    [Phase 2-D] artists 또는 groups 의 last_verified_at 을 현재 시각으로 갱신합니다.

    Cross-Validation 수행 후 호출하여 "마지막 재검증 시점"을 기록합니다.
    """
    # [보안] f-string 금지 — 테이블명을 화이트리스트 dict 로 결정하여
    # SQL Injection 원천 차단. 매핑에 없는 entity_type 은 조기 반환.
    _TABLE_MAP: dict[str, str] = {"ARTIST": "artists", "GROUP": "groups"}
    table = _TABLE_MAP.get(entity_type)
    if table is None:
        log.warning(
            "last_verified_at 갱신 스킵 — 허용되지 않은 entity_type | entity_type=%r",
            entity_type,
        )
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if table == "artists":
                    cur.execute(
                        "UPDATE artists SET last_verified_at = NOW() WHERE id = %s",
                        (entity_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE groups SET last_verified_at = NOW() WHERE id = %s",
                        (entity_id,),
                    )
    except Exception as exc:
        log.warning(
            "last_verified_at 갱신 실패 | entity=%s:%d err=%r",
            entity_type, entity_id, exc,
        )


def _get_glossary_from_db() -> list[dict]:
    """
    glossary 테이블에서 용어 사전을 조회합니다.

    term_ko / term_en / category / description 을 반환합니다.
    테이블이 없거나 오류 발생 시 빈 리스트를 반환하여 정상 진행을 보장합니다.
    """
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT term_ko, term_en, category, description
                    FROM   glossary
                    WHERE  term_en IS NOT NULL
                    ORDER  BY
                        CASE category
                            WHEN 'ARTIST' THEN 1
                            WHEN 'AGENCY' THEN 2
                            WHEN 'EVENT'  THEN 3
                            ELSE 4
                        END,
                        term_ko
                    LIMIT 300
                """)
                rows = cur.fetchall()
        result = [dict(r) for r in rows]
        log.debug("glossary 로드 | count=%d", len(result))
        return result
    except Exception as exc:
        log.warning(
            "glossary 테이블 조회 실패 — 용어 사전 없이 진행 | err=%r", exc
        )
        return []


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
    Gemini 기반 Phase 4 지식 추출 엔진 (v3).

    v2 주요 변경:
        1. DetectedArtist 에 confidence_score / is_ambiguous / ambiguity_reason 추가
        2. _decide_status(): 엔티티별 0.80 임계값 기반 PROCESSED/MANUAL_REVIEW 결정
           - 하나라도 confidence_score < 0.80 이거나 is_ambiguous = True이면 MANUAL_REVIEW
           - system_note 에 AI가 판단한 모호 이유 기록
        3. _call_gemini(): (text, GeminiCallMetrics) 튜플 반환
           - prompt_tokens, completion_tokens, total_tokens, response_time_ms 측정
        4. system_logs.details 에 token_metrics 포함 → 비용 분석 가능

    v3 주요 변경:
        5. 이중 언어 추출: 한 번의 Gemini 호출로 title_en + topic_summary_en 생성
           - K-POP 팬 친화적 영문 제목/요약 (직역 금지)
           - 한국어 고유 표현(역주행, 대세돌 등) → 영어 현지화 가이드 프롬프트
        6. _decide_status() v3: title_en / topic_summary_en 누락 시 MANUAL_REVIEW
        7. _update_article_status() v3: title_en / summary_en DB 저장 (articles 테이블)

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

        # [v3] 용어 사전 캐시 (TTL: _GLOSSARY_CACHE_TTL)
        self._glossary_cache: list[dict] = []
        self._glossary_loaded_at: float = 0.0

        log.info(
            "IntelligenceEngine v3 초기화 | model=%s batch_size=%d "
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

    def _get_glossary(self, force: bool = False) -> list[dict]:
        """
        [v3] 용어 사전을 메모리 캐시에서 반환 (TTL: _GLOSSARY_CACHE_TTL, 기본 10분).

        glossary 테이블 조회 실패 시 빈 리스트를 반환합니다.
        """
        now = time.monotonic()
        if (
            force
            or not self._glossary_cache
            or (now - self._glossary_loaded_at) > _GLOSSARY_CACHE_TTL
        ):
            self._glossary_cache = _get_glossary_from_db()
            self._glossary_loaded_at = now
            log.debug("용어 사전 캐시 갱신 | count=%d", len(self._glossary_cache))
        return self._glossary_cache

    def _get_translation_tier(self, article: dict) -> TranslationTier:
        """
        [v3] 기사의 아티스트 global_priority 를 조회하여 번역 티어를 결정합니다.

        article["artist_name_ko"] 를 artists 캐시에서 검색하여 best priority(최솟값)를
        찾습니다. 여러 아티스트가 매칭되면 가장 높은 우선순위(가장 작은 숫자)를 사용합니다.

        매핑:
            priority 1   → TranslationTier.FULL        (전체 번역)
            priority 2   → TranslationTier.TITLE_ONLY  (제목+요약만)
            priority 3   → TranslationTier.KO_ONLY     (한국어만)
            NULL / 미발견 → TranslationTier.FULL        (기본값, 누락 방지)
        """
        artist_name = (article.get("artist_name_ko") or "").strip()
        if not artist_name:
            log.debug("artist_name_ko 없음 — FULL 티어 기본값 적용")
            return TranslationTier.FULL

        artists = self._get_artists()
        best_priority: Optional[int] = None

        for artist in artists:
            cand_ko = (artist.get("name_ko") or "").strip()
            if not cand_ko:
                continue
            if (
                artist_name == cand_ko
                or artist_name in cand_ko
                or cand_ko in artist_name
            ):
                prio = artist.get("global_priority")
                if prio is not None:
                    if best_priority is None or prio < best_priority:
                        best_priority = prio

        if best_priority is None:
            log.debug(
                "artist_name_ko=%r — priority 미분류(NULL/미발견) → FULL 티어",
                artist_name,
            )
            return TranslationTier.FULL
        elif best_priority <= 1:
            log.debug("artist_name_ko=%r — priority=1 → FULL 티어", artist_name)
            return TranslationTier.FULL
        elif best_priority <= 2:
            log.debug("artist_name_ko=%r — priority=2 → TITLE_ONLY 티어", artist_name)
            return TranslationTier.TITLE_ONLY
        else:
            log.debug(
                "artist_name_ko=%r — priority=%d → KO_ONLY 티어",
                artist_name, best_priority,
            )
            return TranslationTier.KO_ONLY

    # ── 매칭 점수 계산 ─────────────────────────────────────

    def _score_artist_match(
        self,
        detected: DetectedArtist,
        candidate: dict,
    ) -> float:
        """
        [v2] 탐지된 아티스트와 DB 후보 사이의 매칭 신뢰도 점수를 계산합니다.

        v2 스키마 변경 반영: agency / official_tags 제거.
        무대명(stage_name_ko / stage_name_en) 매칭 추가.

        이 점수는 DB 매칭용(entity_mappings.confidence_score)이며,
        DetectedArtist.confidence_score(Gemini 자체 평가)와 별개입니다.

        점수 체계 (최대 1.0):
            +0.50  name_ko 완전 일치
            +0.30  name_ko 부분 포함
            +0.50  stage_name_ko 완전 일치   (name_ko 와 중복 적용 가능, max 1.0)
            +0.25  stage_name_ko 부분 포함
            +0.20  name_en 완전 일치
            +0.10  name_en 부분 포함
            +0.20  stage_name_en 완전 일치
            +0.10  stage_name_en 부분 포함
        """
        score = 0.0

        name_ko   = detected.name_ko.strip()
        cand_ko   = (candidate.get("name_ko")       or "").strip()
        stage_ko  = (candidate.get("stage_name_ko") or "").strip()

        # ── 한국어 이름 매칭 ─────────────────────────────────
        if name_ko and cand_ko:
            if name_ko == cand_ko:
                score += 0.50
            elif name_ko in cand_ko or cand_ko in name_ko:
                score += 0.30

        # ── 한국어 무대명 매칭 (본명과 다를 때 별도 가점) ────
        if name_ko and stage_ko and stage_ko != cand_ko:
            if name_ko == stage_ko:
                score += 0.50
            elif name_ko in stage_ko or stage_ko in name_ko:
                score += 0.25

        # ── 영어 이름 매칭 ───────────────────────────────────
        name_en  = (detected.name_en or "").strip().lower()
        cand_en  = (candidate.get("name_en")       or "").strip().lower()
        stage_en = (candidate.get("stage_name_en") or "").strip().lower()

        if name_en and cand_en:
            if name_en == cand_en:
                score += 0.20
            elif name_en in cand_en or cand_en in name_en:
                score += 0.10

        if name_en and stage_en and stage_en != cand_en:
            if name_en == stage_en:
                score += 0.20
            elif name_en in stage_en or stage_en in name_en:
                score += 0.10

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
        tier: TranslationTier = TranslationTier.FULL,    # [v3]
    ) -> tuple[str, Optional[str]]:
        """
        [v3] 처리 결과를 기반으로 최종 상태와 system_note 를 결정합니다.

        PROCESSED 조건 (모두 충족):
            1. 모든 DetectedArtist.confidence_score ≥ _ENTITY_CONFIDENCE_THRESHOLD(0.80)
            2. 모든 DetectedArtist.is_ambiguous == False
            3. relevance_score ≥ _MIN_RELEVANCE (0.30)
            4. overall confidence ≥ _MIN_CONFIDENCE (0.60)
            5. [v3 FULL/TITLE_ONLY] title_en + topic_summary_en 모두 비어있지 않음

        MANUAL_REVIEW 조건 (하나라도 해당):
            - 엔티티 confidence_score < 0.80
            - is_ambiguous = True (동명이인/문맥 모호)
            - relevance_score 또는 overall confidence 임계값 미달
            - [v3 FULL/TITLE_ONLY] 영문 번역 필드 누락

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

        # ── 3. [v3] 영문 번역 검증 (FULL / TITLE_ONLY 티어만) ──
        if tier != TranslationTier.KO_ONLY:
            if not (intelligence.title_en or "").strip():
                reasons.append(
                    f"영문 제목(title_en) 누락 — Gemini 번역 미생성 (tier={tier.value})"
                )
            if not (intelligence.topic_summary_en or "").strip():
                reasons.append(
                    f"영문 요약(topic_summary_en) 누락 — Gemini 번역 미생성 (tier={tier.value})"
                )

        if reasons:
            note = "MANUAL_REVIEW 사유: " + "; ".join(reasons)
            log.info(
                "MANUAL_REVIEW 결정 | %d개 사유: %s",
                len(reasons), " / ".join(reasons[:3]),
            )
            return "MANUAL_REVIEW", note

        # ── [Phase 4-B] Threshold-based Auto-Commit ───────────
        # 모든 PROCESSED 조건 충족 + confidence ≥ 0.95 → VERIFIED (운영자 확인 불필요)
        if intelligence.confidence >= _AUTO_COMMIT_THRESHOLD:
            note = (
                f"Auto-Commit: confidence={intelligence.confidence:.4f} "
                f"≥ {_AUTO_COMMIT_THRESHOLD} threshold"
            )
            log.info(
                "VERIFIED 자동 승인 | confidence=%.4f threshold=%.2f",
                intelligence.confidence, _AUTO_COMMIT_THRESHOLD,
            )
            return "VERIFIED", note

        # confidence < 0.95 — PROCESSED 로 처리하되 예외 로그 기록
        log.info(
            "PROCESSED (신뢰도 임계값 미달) | confidence=%.4f < %.2f — 로그만 기록",
            intelligence.confidence, _AUTO_COMMIT_THRESHOLD,
        )
        return "PROCESSED", None

    # ── [Phase 4-B] 자율형 데이터 정제 ────────────────────────

    def _cross_validate_and_update(
        self,
        linked:       list[dict],
        intelligence: "ArticleIntelligence",
        article_id:   int,
    ) -> list[dict]:
        """
        [Phase 4-B] Cross-Validation (상호 검증).

        DB에 매핑된 각 아티스트의 현재 프로필을 조회하고,
        Gemini 가 추출한 값과 비교합니다:

            1. DB 값이 비어있고 Gemini 값이 있으면
               → 즉시 업데이트 (Fill) + DataUpdateLog + confidence +0.05
            2. DB 값과 Gemini 값이 일치하면
               → confidence +0.05 (기존 데이터 신뢰도 강화)
            3. DB 값과 Gemini 값이 상충하면
               → _auto_reconcile() 로 Gemini 2차 판단 후 처리

        현재 검증 대상 필드:
            - name_en : DetectedArtist.name_en vs artists.name_en

        Returns:
            confidence_score 가 보정된 linked 리스트 (원본 변경 없음)
        """
        if not linked:
            return linked

        updated: list[dict] = []
        # DetectedArtist 를 이름으로 빠르게 조회
        detected_map: dict[str, "DetectedArtist"] = {
            da.name_ko: da for da in intelligence.detected_artists
        }

        for m in linked:
            entity_id = m.get("entity_id")

            # 미링크 / GROUP / EVENT 엔티티는 Cross-Validation 대상 아님
            if entity_id is None or m.get("entity_type") != "ARTIST":
                updated.append(m)
                continue

            profile = _get_artist_profile_v2(entity_id)
            if not profile:
                updated.append(m)
                continue

            detected_obj = detected_map.get(m.get("detected_name_ko", ""))
            if detected_obj is None:
                updated.append(m)
                continue

            boost = 0.0

            # ── name_en 교차검증 ──────────────────────────────
            detected_en = (detected_obj.name_en or "").strip()
            db_en       = (profile.get("name_en") or "").strip()

            # [Phase 2-D] Cross-Validation 수행 후 last_verified_at 갱신
            _update_entity_verified_at("ARTIST", entity_id)

            if detected_en:
                if not db_en:
                    # DB 비어있음 → 즉시 보충 (FILL)
                    ok = _update_artist_field_v2(
                        artist_id  = entity_id,
                        field      = "name_en",
                        new_value  = detected_en,
                        old_value  = None,
                        article_id = article_id,
                    )
                    if ok:
                        boost += 0.05
                        # [Phase 2-D] AutoResolutionLog 기록
                        _log_auto_resolution(
                            article_id        = article_id,
                            entity_type       = "ARTIST",
                            entity_id         = entity_id,
                            field_name        = "name_en",
                            old_value         = None,
                            new_value         = detected_en,
                            resolution_type   = "FILL",
                            gemini_confidence = detected_obj.confidence_score,
                            source_reliability = intelligence.confidence,
                        )
                        log.info(
                            "[Phase4B] name_en 자동 보충 | "
                            "artist_id=%d name_en=%r",
                            entity_id, detected_en,
                        )
                elif detected_en.lower() == db_en.lower():
                    # 일치 → 신뢰도 강화
                    boost += 0.05
                    log.debug(
                        "[Phase4B] name_en 일치 확인 | "
                        "artist_id=%d name_en=%r boost=+0.05",
                        entity_id, db_en,
                    )
                else:
                    # 상충 → Auto-Reconciliation
                    winner, reasoning = self._auto_reconcile(
                        artist_id       = entity_id,
                        article_id      = article_id,
                        field           = "name_en",
                        db_val          = db_en,
                        detected_val    = detected_en,
                        article_context = (intelligence.title_ko or ""),
                    )
                    if winner == "article":
                        _update_artist_field_v2(
                            artist_id  = entity_id,
                            field      = "name_en",
                            new_value  = detected_en,
                            old_value  = db_en,
                            article_id = article_id,
                        )
                        # [Phase 2-D] AutoResolutionLog 기록
                        _log_auto_resolution(
                            article_id        = article_id,
                            entity_type       = "ARTIST",
                            entity_id         = entity_id,
                            field_name        = "name_en",
                            old_value         = db_en,
                            new_value         = detected_en,
                            resolution_type   = "RECONCILE",
                            gemini_reasoning  = reasoning,
                            gemini_confidence = detected_obj.confidence_score,
                            source_reliability = intelligence.confidence,
                        )
                    elif winner is None:
                        # 판단 불가 → ConflictFlag 기록
                        # 이름이 완전히 다르면 심각(high score), 일부 다르면 중간
                        similarity = len(
                            set(detected_en.lower()) & set(db_en.lower())
                        ) / max(len(detected_en), len(db_en), 1)
                        c_score = round(1.0 - similarity, 2)
                        _log_conflict_flag(
                            article_id        = article_id,
                            entity_type       = "ARTIST",
                            entity_id         = entity_id,
                            field_name        = "name_en",
                            existing_value    = db_en,
                            conflicting_value = detected_en,
                            conflict_reason   = "Auto-Reconcile 판단 불가: Gemini 응답 없음/형식 오류",
                            conflict_score    = c_score,
                        )

            # confidence_score 보정 (최대 1.0)
            new_score = min(m["confidence_score"] + boost, 1.0)
            updated.append({**m, "confidence_score": round(new_score, 4)})

        return updated

    def _auto_reconcile(
        self,
        artist_id:       int,
        article_id:      int,
        field:           str,
        db_val:          str,
        detected_val:    str,
        article_context: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        [Phase 4-B] Auto-Reconciliation (자율 모순 해결).

        DB 기존 값과 Gemini 추출 값이 상충할 때,
        Gemini 에게 어느 것이 더 최신이며 공신력이 있는지 물어봅니다.

        2차 Gemini 호출을 수행합니다 (RPM 리미터 적용).

        Returns:
            (winner, reason) tuple:
            winner: "article" — 현재 기사 값이 더 정확함 → DB 갱신 필요
                    "db"      — 기존 DB 값이 더 정확함 → 변경 없음
                    None      — 판단 불가 또는 Gemini 호출 실패
            reason: Gemini 가 제공한 판단 이유 (None 이면 판단 불가)
        """
        prompt = (
            "K-엔터테인먼트 데이터베이스에서 두 출처 간 정보 충돌이 발생했습니다.\n"
            "어떤 값이 더 최신이며 공신력이 있는지 판단하고 JSON 으로만 응답하세요.\n\n"
            f"필드명 : {field}\n"
            f"DB 기존 값    : \"{db_val}\"\n"
            f"현재 기사 추출: \"{detected_val}\"\n"
            f"기사 제목/문맥: \"{article_context[:200]}\"\n\n"
            "응답 형식 (JSON only, 다른 텍스트 금지):\n"
            "{\"winner\": \"article\" | \"db\", "
            "\"reason\": \"판단 이유 (30자 이내)\"}"
        )

        try:
            raw, _ = self._call_gemini(prompt)
            data   = self._parse_json(raw)
            winner = data.get("winner")
            reason = data.get("reason", "")

            if winner not in ("article", "db"):
                log.warning(
                    "[Phase4B] Auto-Reconcile 판단 불가 | "
                    "artist_id=%d field=%s winner=%r",
                    artist_id, field, winner,
                )
                return (None, None)

            log.info(
                "[Phase4B] Auto-Reconcile 결정 | artist_id=%d field=%s "
                "winner=%s reason=%r | db=%r article=%r",
                artist_id, field, winner, reason, db_val, detected_val,
            )
            _log_to_system(
                article_id = article_id,
                level      = "INFO",
                event      = "auto_reconcile",
                message    = (
                    f"[Phase4B] 모순 해결: field={field} winner={winner}"
                ),
                details    = {
                    "artist_id":    artist_id,
                    "field":        field,
                    "db_val":       db_val,
                    "detected_val": detected_val,
                    "winner":       winner,
                    "reason":       reason,
                },
            )
            return (winner, reason)

        except Exception as exc:
            log.warning(
                "[Phase4B] Auto-Reconcile Gemini 호출 실패 | "
                "artist_id=%d field=%s err=%r",
                artist_id, field, exc,
            )
            return (None, None)

    def _auto_enroll_new_entities(
        self,
        detected_artists: list["DetectedArtist"],
        linked:           list[dict],
        article_id:       int,
    ) -> int:
        """
        [Phase 4-B] Smart Glossary Auto-Enroll.

        DB에 매핑되지 않은 신규 엔티티(entity_id=None)를
        glossary 테이블에 Auto-Provisioned 상태로 즉시 등록합니다.

        Gemini 가 이미 name_en 을 추론했으므로 추가 API 호출 없이 등록합니다.
        등록 후 glossary 캐시를 무효화하여 다음 배치부터 반영됩니다.

        Returns:
            등록된 신규 용어 수
        """
        # DB에 이미 매핑된 이름 집합
        linked_names: set[str] = {
            m.get("detected_name_ko", "")
            for m in linked
            if m.get("entity_id") is not None
        }

        # entity_type → glossary category 매핑
        category_map = {
            "ARTIST": "ARTIST",
            "GROUP":  "ARTIST",  # 그룹도 ARTIST 카테고리로 통합 관리
            "EVENT":  "EVENT",
        }

        enrolled = 0
        for da in detected_artists:
            if da.name_ko in linked_names:
                continue  # 이미 DB 에 매핑됨 → 등록 불필요

            name_en = (da.name_en or "").strip()
            if not name_en:
                log.debug(
                    "[Phase4B] 영문명 없음 — 자동 등록 스킵 | name_ko=%r",
                    da.name_ko,
                )
                continue

            category = category_map.get(da.entity_type, "ARTIST")
            ok = _glossary_enroll_auto(
                term_ko    = da.name_ko,
                term_en    = name_en,
                category   = category,
                article_id = article_id,
            )
            if ok:
                enrolled += 1
                # [Phase 2-D] AutoResolutionLog: 신규 용어 자동 등록 기록
                _log_auto_resolution(
                    article_id        = article_id,
                    entity_type       = da.entity_type,
                    entity_id         = 0,           # 신규 용어: DB ID 미확정
                    field_name        = "glossary_term",
                    old_value         = None,
                    new_value         = {"term_ko": da.name_ko, "term_en": name_en},
                    resolution_type   = "ENROLL",
                    gemini_reasoning  = f"Auto-Provisioned: {da.name_ko} → {name_en}",
                    gemini_confidence = da.confidence_score,
                    source_reliability = 0.0,
                )

        if enrolled:
            # 용어 사전 캐시 무효화 — 다음 호출 시 DB 에서 재로드
            self._glossary_loaded_at = 0.0
            log.info(
                "[Phase4B] 신규 용어 자동 등록 완료 | article_id=%d enrolled=%d",
                article_id, enrolled,
            )

        return enrolled

    # ── Gemini 지식 추출 ───────────────────────────────────

    def _extract_intelligence(
        self,
        title_ko:   Optional[str],
        content_ko: Optional[str],
        tier: TranslationTier = TranslationTier.FULL,       # [v3]
        glossary: Optional[list[dict]] = None,              # [v3]
    ) -> tuple[ArticleIntelligence, GeminiCallMetrics]:
        """
        [v3] Gemini API 를 호출하여 기사의 엔티티/지식을 추출합니다.

        tier 에 따라 동적으로 프롬프트를 생성하고 용어 사전을 주입합니다.

        Returns:
            (ArticleIntelligence, GeminiCallMetrics)
        """
        title   = (title_ko   or "").strip() or "제목 없음"
        content = (content_ko or "").strip()

        if len(content) > _TEXT_MAX_CHARS:
            content = content[:_TEXT_MAX_CHARS] + "\n...(이하 생략)"
        if not content:
            log.warning("content_ko 없음 — 제목만으로 분석 (신뢰도 낮을 수 있음)")

        prompt = _build_prompt(
            title=title,
            content=content,
            tier=tier,
            glossary=glossary,
        )
        log.debug(
            "Gemini 프롬프트 생성 | tier=%s glossary=%d chars=%d",
            tier.value, len(glossary or []), len(prompt),
        )
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

    def process_article(self, article: dict, dry_run: bool = False) -> ProcessingResult:
        """
        [v2] 단일 기사를 처리합니다.

        처리 순서:
            1. Gemini 추출 → (ArticleIntelligence, GeminiCallMetrics)       [항상 실행]
            2. 컨텍스트 링킹 → entity_id 매칭                               [항상 실행]
            2a. [Phase 4-B] Cross-Validation + Auto-Reconciliation           [dry_run=False 만]
                - DB 프로필과 Gemini 추출 값 비교 → Fill / Boost / Reconcile
            2b. [Phase 4-B] Smart Glossary Auto-Enroll                       [dry_run=False 만]
                - 미매핑 엔티티 glossary 자동 등록 (Auto-Provisioned)
            3. entity_mappings 교체                                          [dry_run=False 만]
            4. _decide_status() → VERIFIED / PROCESSED / MANUAL_REVIEW      [항상 실행]
                - confidence ≥ 0.95 → VERIFIED (운영자 확인 불필요)
            5. DB 업데이트 (process_status, summary_ko, system_note)        [dry_run=False 만]
            6. system_logs 기록 / [DRY RUN] JSON 미리보기 출력

        Args:
            dry_run: True 면 Gemini 호출·매핑 계산은 수행하되 DB 에 반영하지 않음.
                     예상 매핑 결과를 JSON 으로 stdout 에 출력합니다.
        """
        article_id = article["id"]
        job_id     = article.get("job_id")
        t_start    = time.monotonic()

        try:
            # ── 0. [v3] 번역 티어 결정 + 용어 사전 로드 ──
            tier     = self._get_translation_tier(article)
            glossary = self._get_glossary() if tier != TranslationTier.KO_ONLY else []
            log.info(
                "번역 티어 결정 | article_id=%d tier=%s artist=%r glossary=%d",
                article_id, tier.value,
                (article.get("artist_name_ko") or "")[:30],
                len(glossary),
            )

            # ── 1. Gemini 추출 ───────────────────────────
            intelligence, metrics = self._extract_intelligence(
                title_ko   = article.get("title_ko"),
                content_ko = article.get("content_ko"),
                tier       = tier,       # [v3]
                glossary   = glossary,   # [v3]
            )

            # ── 2. 컨텍스트 링킹 ────────────────────────
            linked = self._contextual_link(intelligence.detected_artists)

            # ── 2a. [Phase 4-B] Cross-Validation + Auto-Reconciliation ──
            if not dry_run:
                linked = self._cross_validate_and_update(
                    linked       = linked,
                    intelligence = intelligence,
                    article_id   = article_id,
                )

            # ── 2b. [Phase 4-B] Smart Glossary Auto-Enroll ──────────────
            if not dry_run:
                self._auto_enroll_new_entities(
                    detected_artists = intelligence.detected_artists,
                    linked           = linked,
                    article_id       = article_id,
                )

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
            if not dry_run and entity_records:
                saved = _replace_entity_mappings(article_id, entity_records)
                log.debug(
                    "entity_mappings 저장 | article_id=%d count=%d", article_id, saved
                )

            # ── 4. 조건부 상태 결정 ──────────────────────
            final_status, system_note = self._decide_status(intelligence, linked, tier)

            # ── 4b. [v3] SEO 해시태그 JSONB 구성 ────────
            seo_hashtags_dict: Optional[dict] = None
            if intelligence.seo_hashtags and tier != TranslationTier.KO_ONLY:
                seo_hashtags_dict = {
                    "tags":         intelligence.seo_hashtags,
                    "model":        self.model_name,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "confidence":   round(intelligence.confidence, 4),
                    "tier":         tier.value,
                }

            # ── 5. DB 업데이트 ───────────────────────────
            if not dry_run:
                _update_article_status(
                    article_id,
                    final_status,
                    topic_summary = intelligence.topic_summary or None,
                    system_note   = system_note,
                    title_en      = intelligence.title_en or None,          # [v3]
                    summary_en    = intelligence.topic_summary_en or None,  # [v3]
                    hashtags_en   = intelligence.seo_hashtags or None,      # [v3]
                    seo_hashtags  = seo_hashtags_dict,                      # [v3]
                )

            duration_ms = int((time.monotonic() - t_start) * 1000)

            if dry_run:
                # ── 6. [DRY RUN] JSON 미리보기 출력 ──────
                preview = {
                    "article_id":      article_id,
                    "title_ko":        (article.get("title_ko") or "")[:80],
                    "translation_tier": tier.value,                          # [v3]
                    "status_would_be": final_status,
                    "system_note":     system_note,
                    "intelligence": {
                        "title_ko":          intelligence.title_ko,          # [v3]
                        "title_en":          intelligence.title_en,          # [v3]
                        "topic_summary":     intelligence.topic_summary,
                        "topic_summary_en":  intelligence.topic_summary_en,  # [v3]
                        "seo_hashtags":      intelligence.seo_hashtags,      # [v3]
                        "sentiment":         intelligence.sentiment,
                        "relevance_score":   intelligence.relevance_score,
                        "confidence":        intelligence.confidence,
                        "main_category":     intelligence.main_category,
                        "detected_artists": [
                            a.model_dump() for a in intelligence.detected_artists
                        ],
                    },
                    "linked_artists":    linked,
                    "entity_mappings":   entity_records,
                    "seo_hashtags_dict": seo_hashtags_dict,                  # [v3]
                    "token_metrics":     metrics.to_dict(),
                }
                log.info(
                    "[DRY RUN] article_id=%d → status_would_be=%s | "
                    "artists=%d tokens=%d time=%dms",
                    article_id, final_status, len(linked),
                    metrics.total_tokens, metrics.response_time_ms,
                )
                print(f"\n[DRY RUN] article_id={article_id}")
                print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))
            else:
                # ── 6. 성공 로그 (토큰 포함) ──────────────
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
                        "status":             final_status,
                        "system_note":        system_note,
                        "translation_tier":   tier.value,                    # [v3]
                        "title_en":           intelligence.title_en,          # [v3]
                        "topic_summary_en":   intelligence.topic_summary_en,  # [v3]
                        "seo_hashtags":       intelligence.seo_hashtags,      # [v3]
                        "sentiment":          intelligence.sentiment,
                        "relevance_score":    intelligence.relevance_score,
                        "confidence":         intelligence.confidence,
                        "main_category":      intelligence.main_category,
                        "entity_scores":      {
                            m["detected_name_ko"]: m.get("gemini_confidence", 1.0)
                            for m in linked
                        },
                        "ambiguous_entities":  ambiguous_names,
                        "low_conf_entities":   low_conf_entities,
                        "linked_artist_ids":   [
                            m["entity_id"] for m in linked if m["entity_id"] is not None
                        ],
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
                "기사 처리 실패 | article_id=%d dry_run=%s error=%s",
                article_id, dry_run, error_msg,
            )

            if not dry_run:
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
            else:
                log.info(
                    "[DRY RUN] 처리 실패 (DB 기록 없음) | article_id=%d error=%s",
                    article_id, error_msg,
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
        dry_run: bool = False,
    ) -> BatchResult:
        """
        PENDING 기사를 배치로 처리합니다.

        [v2] BatchResult 에 total_tokens 합계를 포함합니다.

        Args:
            dry_run: True 면 기사 상태를 SCRAPED(in-progress)로 변경하지 않고
                     읽기 전용으로 조회한 뒤, Gemini 호출·매핑 계산 결과를
                     JSON 미리보기로 출력합니다. DB 에 아무런 쓰기를 하지 않습니다.
        """
        limit  = batch_size if batch_size is not None else self.batch_size
        result = BatchResult()

        if dry_run:
            articles = _read_pending_articles_dry(limit=limit, job_id=job_id)
        else:
            articles = _claim_pending_articles(limit=limit, job_id=job_id)
        result.total = len(articles)

        if not articles:
            log.info(
                "처리할 PENDING 기사 없음 | job_id=%s dry_run=%s",
                job_id if job_id is not None else "전체", dry_run,
            )
            return result

        log.info(
            "배치 처리 시작 | count=%d job_id=%s model=%s threshold=%.2f dry_run=%s",
            len(articles), job_id, self.model_name, _ENTITY_CONFIDENCE_THRESHOLD,
            dry_run,
        )

        for i, article in enumerate(articles, start=1):
            ar = self.process_article(article, dry_run=dry_run)

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

            if ar.status == "VERIFIED":
                result.verified += 1
            elif ar.status == "PROCESSED":
                result.processed += 1
            elif ar.status == "MANUAL_REVIEW":
                result.manual_review += 1
            else:
                result.failed += 1

        log.info(
            "배치 처리 완료 | total=%d verified=%d processed=%d "
            "manual_review=%d failed=%d total_tokens=%d",
            result.total, result.verified, result.processed,
            result.manual_review, result.failed, result.total_tokens,
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
        description="TIH Gemini Intelligence Engine v3 — Phase 4 Bilingual Entity Extraction",
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
    parser.add_argument(
        "--auto-commit-threshold", type=float, default=None, metavar="FLOAT",
        help=f"[Phase 4-B] Auto-Commit 임계값 (기본: {_AUTO_COMMIT_THRESHOLD}). "
             "전체 신뢰도가 이 값 이상이면 VERIFIED 로 즉시 반영",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Gemini API 호출은 수행하되 DB 에 반영하지 않음. "
             "예상 매핑 결과를 JSON 으로 출력합니다 (테스트 모드).",
    )
    args = parser.parse_args(argv)

    _setup_logging()

    # CLI 에서 임계값 오버라이드
    if args.threshold is not None:
        global _ENTITY_CONFIDENCE_THRESHOLD
        _ENTITY_CONFIDENCE_THRESHOLD = args.threshold
        log.info("엔티티 신뢰도 임계값 오버라이드: %.2f", _ENTITY_CONFIDENCE_THRESHOLD)

    if args.auto_commit_threshold is not None:
        global _AUTO_COMMIT_THRESHOLD
        _AUTO_COMMIT_THRESHOLD = args.auto_commit_threshold
        log.info(
            "[Phase4B] Auto-Commit 임계값 오버라이드: %.2f", _AUTO_COMMIT_THRESHOLD
        )

    engine = IntelligenceEngine(
        model_name = args.model,
        batch_size = args.batch_size,
    )

    result = engine.process_pending(
        batch_size = args.batch_size,
        job_id     = args.job_id,
        dry_run    = args.dry_run,
    )

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(
        f"\n{prefix}처리 완료: total={result.total} "
        f"verified={result.verified} "
        f"processed={result.processed} "
        f"manual_review={result.manual_review} "
        f"failed={result.failed} "
        f"total_tokens={result.total_tokens}"
    )


if __name__ == "__main__":
    main()
