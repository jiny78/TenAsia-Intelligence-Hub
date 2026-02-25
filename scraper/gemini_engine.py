"""
scraper/gemini_engine.py — Gemini API 기반 아티클 추출 엔진

주요 기능:
  1. RPM 자동 대기 (GeminiRpmLimiter)
     - Gemini Flash 기본: 무료 15 RPM, 유료 2000 RPM
     - 환경 변수 GEMINI_RPM_LIMIT 으로 조정 가능
     - 슬라이딩 윈도우 방식으로 정확한 대기 시간 계산

  2. global_priority 기반 추출 수준 분기 (비용 절감)
     ┌───────────────────┬────────────────────────────────────┐
     │ global_priority   │ 추출 내용                          │
     ├───────────────────┼────────────────────────────────────┤
     │ True  (글로벌)    │ 전체: 한/영 제목+본문+요약+해시태그│
     │ False (국내용)    │ 최소: 한국어 제목 + 아티스트명만   │
     └───────────────────┴────────────────────────────────────┘

  3. Kill Switch 연동
     - 매 호출 전 check_gemini_kill_switch() 확인
     - 초과 시 GeminiKillSwitchError 발생 → 작업 중단

사용법:
    engine = GeminiEngine()
    result = engine.extract_article(html, global_priority=True)
    # result = {
    #     "title_ko": "...", "title_en": "...",
    #     "body_ko": "...",  "body_en": "...",
    #     "hashtags_ko": [...], "hashtags_en": [...],
    #     ...
    # }
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any

import google.generativeai as genai

from core.config import GeminiKillSwitchError, check_gemini_kill_switch, record_gemini_usage

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────────────────────

_DEFAULT_RPM         = int(os.getenv("GEMINI_RPM_LIMIT", "60"))   # 기본 60 RPM
_DEFAULT_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
_SAFETY_MARGIN_SEC   = 0.1   # 타이밍 오차 보정
_MAX_OUTPUT_TOKENS   = 2048


# ─────────────────────────────────────────────────────────────
# RPM 제한기
# ─────────────────────────────────────────────────────────────

class GeminiRpmLimiter:
    """
    슬라이딩 윈도우 방식 RPM 제한기.

    Thread-safe: 여러 스레드가 동시에 호출해도 한 개씩 직렬화.

    동작 원리:
        - 최근 60초 안의 호출 타임스탬프를 deque 에 보관
        - 새 호출 시 60초 지난 타임스탬프 제거
        - 남은 수 == rpm_limit 이면 가장 오래된 타임스탬프가
          60초를 넘을 때까지 대기
    """

    def __init__(self, rpm_limit: int = _DEFAULT_RPM) -> None:
        self.rpm_limit = rpm_limit
        self._lock       = threading.Lock()
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        """
        호출 슬롯을 확보합니다.
        RPM 초과 시 필요한 만큼 블로킹 대기합니다.
        """
        with self._lock:
            while True:
                now = time.monotonic()
                # 60초 이전 타임스탬프 제거
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.rpm_limit:
                    # 슬롯 여유 있음 → 즉시 통과
                    self._timestamps.append(now)
                    return

                # 슬롯 가득 참 → 가장 오래된 타임스탬프 기준 대기
                wait_sec = 60.0 - (now - self._timestamps[0]) + _SAFETY_MARGIN_SEC
                logger.debug(
                    "RPM 한도 도달 (%d/%d) — %.1fs 대기",
                    len(self._timestamps), self.rpm_limit, wait_sec,
                )
                # Lock 해제 후 대기 (다른 스레드 블로킹 방지)
                self._lock.release()
                time.sleep(max(wait_sec, 0.0))
                self._lock.acquire()

    @property
    def current_usage(self) -> int:
        """현재 슬라이딩 윈도우 내 호출 수."""
        with self._lock:
            now = time.monotonic()
            return sum(1 for t in self._timestamps if now - t < 60.0)


# ─────────────────────────────────────────────────────────────
# 프롬프트 정의
# ─────────────────────────────────────────────────────────────

_PROMPT_MINIMAL = """\
다음 HTML에서 아래 두 가지 정보만 추출하세요. JSON으로만 응답하세요.

HTML:
{html}

추출 형식:
{{
  "title_ko": "한국어 제목 (없으면 null)",
  "artist_name_ko": "주인공 아티스트/연예인 한국어 이름 (없으면 null)"
}}

규칙:
- 반드시 JSON만 응답 (마크다운 코드블록 없이)
- 확실하지 않으면 null
"""

_PROMPT_FULL = """\
다음 HTML 기사를 분석하여 아래 JSON 형식으로 정보를 추출하세요.
JSON만 응답하세요 (마크다운 코드블록 없이).

HTML:
{html}

추출 형식:
{{
  "title_ko":        "한국어 제목",
  "title_en":        "English title (번역 또는 null)",
  "body_ko":         "200자 내외 한국어 본문 요약",
  "body_en":         "English body summary (200 chars max, or null)",
  "summary_ko":      "50자 내외 SNS 캡션용 한 줄 요약",
  "summary_en":      "One-line English SNS caption (50 chars max, or null)",
  "artist_name_ko":  "주인공 아티스트 한국어 이름",
  "artist_name_en":  "Artist English name (null if unknown)",
  "global_priority": true or false,
  "hashtags_ko":     ["한국어해시태그1", "한국어해시태그2", ...],
  "hashtags_en":     ["EnglishHashtag1", "EnglishHashtag2", ...]
}}

규칙:
- global_priority: 해외 팬덤이 있는 글로벌 아티스트(BTS, BLACKPINK 등)면 true
- hashtags_ko: 5-10개, '#' 없이, 콘텐츠 관련 태그
- hashtags_en: 5-10개, '#' 없이, 영어 SEO에 최적화된 태그
- 확실하지 않은 값은 null (빈 문자열 사용 금지)
- 반드시 JSON만 응답
"""


# ─────────────────────────────────────────────────────────────
# Gemini 엔진
# ─────────────────────────────────────────────────────────────

class GeminiEngine:
    """
    Gemini API 추출 엔진.

    Args:
        model_name:  사용할 Gemini 모델 (기본: gemini-2.0-flash)
        rpm_limit:   분당 최대 호출 수 (기본: 60)

    Example:
        engine = GeminiEngine()
        data = engine.extract_article(html_text, global_priority=True)
    """

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        rpm_limit:  int = _DEFAULT_RPM,
    ) -> None:
        from core.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)

        self._model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=genai.GenerationConfig(
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                temperature=0.2,          # 사실 추출 — 낮은 창의성
                response_mime_type="application/json",
            ),
        )
        self._limiter    = GeminiRpmLimiter(rpm_limit)
        self._model_name = model_name
        logger.info(
            "GeminiEngine 초기화 | model=%s rpm_limit=%d",
            model_name, rpm_limit,
        )

    # ── 내부 호출 ──────────────────────────────────────────────

    def _call(self, prompt: str) -> tuple[str, int]:
        """
        Gemini API 호출 (단일 책임).

        Kill Switch 확인 → RPM 대기 → 실제 호출 → 토큰 기록

        Returns:
            (응답 텍스트, 사용 토큰 수)

        Raises:
            GeminiKillSwitchError: Kill Switch 활성화
            google.api_core.exceptions.GoogleAPIError: API 오류
        """
        # Kill Switch 확인
        check_gemini_kill_switch()

        # RPM 슬롯 확보 (필요 시 블로킹 대기)
        self._limiter.acquire()

        logger.debug(
            "Gemini 호출 | model=%s rpm_usage=%d/%d",
            self._model_name,
            self._limiter.current_usage,
            self._limiter.rpm_limit,
        )

        response = self._model.generate_content(prompt)

        # 토큰 사용량 추출 및 Kill Switch 카운터 업데이트
        usage = getattr(response, "usage_metadata", None)
        total_tokens = 0
        if usage:
            total_tokens = (
                getattr(usage, "total_token_count", 0) or
                getattr(usage, "prompt_token_count", 0) +
                getattr(usage, "candidates_token_count", 0)
            )
        if total_tokens:
            record_gemini_usage(total_tokens)

        return response.text, total_tokens

    # ── 파싱 ──────────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """
        Gemini 응답에서 JSON을 파싱합니다.
        마크다운 코드블록이 포함돼도 처리합니다.
        """
        text = text.strip()
        # ```json ... ``` 블록 제거
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("JSON 파싱 실패 | error=%s text=%r", exc, text[:200])
            return {}

    # ── HTML 전처리 ───────────────────────────────────────────

    @staticmethod
    def _trim_html(html: str, max_chars: int = 8_000) -> str:
        """
        프롬프트 토큰 절감을 위해 HTML을 잘라냅니다.
        script/style 태그 내용을 먼저 제거합니다.
        """
        import re
        # script, style 제거
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # 과도한 공백 정리
        html = re.sub(r"\s{3,}", "\n", html)
        return html[:max_chars]

    # ── 공개 API ──────────────────────────────────────────────

    def extract_minimal(self, html: str) -> dict[str, Any]:
        """
        최소 추출 (global_priority=False 아티스트용).

        비용 절감 목적: 제목·아티스트명만 추출.

        Returns:
            {"title_ko": str|None, "artist_name_ko": str|None}
        """
        trimmed = self._trim_html(html, max_chars=4_000)   # 더 짧게 자름
        prompt  = _PROMPT_MINIMAL.format(html=trimmed)

        try:
            raw, tokens = self._call(prompt)
            logger.debug("최소 추출 완료 | tokens=%d", tokens)
            data = self._parse_json(raw)
            return {
                "title_ko":       data.get("title_ko"),
                "artist_name_ko": data.get("artist_name_ko"),
                # 최소 추출에서는 나머지 필드 명시적으로 None
                "title_en":       None,
                "body_ko":        None,
                "body_en":        None,
                "summary_ko":     None,
                "summary_en":     None,
                "artist_name_en": None,
                "global_priority": False,
                "hashtags_ko":    [],
                "hashtags_en":    [],
            }
        except GeminiKillSwitchError:
            raise
        except Exception as exc:
            logger.error("최소 추출 실패 | error=%s", exc)
            return _empty_result(global_priority=False)

    def extract_full(self, html: str) -> dict[str, Any]:
        """
        전체 추출 (global_priority=True 아티스트용).

        한/영 이중 언어 + SEO 해시태그 전체 추출.

        Returns:
            전체 필드 딕셔너리
        """
        trimmed = self._trim_html(html, max_chars=8_000)
        prompt  = _PROMPT_FULL.format(html=trimmed)

        try:
            raw, tokens = self._call(prompt)
            logger.debug("전체 추출 완료 | tokens=%d", tokens)
            data = self._parse_json(raw)

            return {
                "title_ko":       data.get("title_ko"),
                "title_en":       data.get("title_en"),
                "body_ko":        data.get("body_ko"),
                "body_en":        data.get("body_en"),
                "summary_ko":     data.get("summary_ko"),
                "summary_en":     data.get("summary_en"),
                "artist_name_ko": data.get("artist_name_ko"),
                "artist_name_en": data.get("artist_name_en"),
                "global_priority": bool(data.get("global_priority", True)),
                "hashtags_ko":    data.get("hashtags_ko") or [],
                "hashtags_en":    data.get("hashtags_en") or [],
            }
        except GeminiKillSwitchError:
            raise
        except Exception as exc:
            logger.error("전체 추출 실패 | error=%s", exc)
            return _empty_result(global_priority=True)

    def extract_article(
        self,
        html: str,
        global_priority: bool,
    ) -> dict[str, Any]:
        """
        global_priority 에 따라 추출 수준을 자동 분기합니다.

        Args:
            html:             기사 HTML 원문
            global_priority:  True → 전체 추출, False → 최소 추출

        Returns:
            추출 결과 딕셔너리 (articles 테이블 컬럼과 1:1 대응)

        Raises:
            GeminiKillSwitchError: 월 토큰 한도 초과
        """
        if global_priority:
            logger.info("전체 추출 모드 | global_priority=True")
            return self.extract_full(html)
        else:
            logger.info("최소 추출 모드 | global_priority=False (비용 절감)")
            return self.extract_minimal(html)


# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────

def _empty_result(global_priority: bool) -> dict[str, Any]:
    """추출 실패 시 반환할 빈 결과 딕셔너리."""
    return {
        "title_ko":       None,
        "title_en":       None,
        "body_ko":        None,
        "body_en":        None,
        "summary_ko":     None,
        "summary_en":     None,
        "artist_name_ko": None,
        "artist_name_en": None,
        "global_priority": global_priority,
        "hashtags_ko":    [],
        "hashtags_en":    [],
    }
