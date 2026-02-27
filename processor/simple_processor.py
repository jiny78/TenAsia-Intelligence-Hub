"""
processor/simple_processor.py — 경량 AI 후처리기 (Fast Track)

기존 gemini_engine.py 대비 차이점:
  · SCRAPED 상태 기사 처리 (기존 PENDING → 상태 불일치 버그 수정)
  · 기사당 Gemini 1회 호출 (기존: 최대 2회)
  · ~400 토큰 입력 (기존: 2000+ 토큰)
  · 엔티티 매핑/DB 교차검증 없음 → 3-5배 빠른 처리

처리 결과:
  title_en, summary_ko, summary_en, hashtags_en 채움
  process_status: SCRAPED → PROCESSED (성공) / ERROR (실패)

사용 예시:
  from processor.simple_processor import process_scraped
  done = process_scraped(batch_size=10)
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Article
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

_PROMPT = """\
You are a K-pop news assistant. Translate and summarize the following Korean article.
Return ONLY valid JSON — no markdown, no extra text.

Korean title: {title_ko}
Korean content (excerpt): {content_snippet}

JSON format:
{{
  "title_en": "English translation of the Korean title",
  "summary_ko": "3-sentence Korean summary of the article",
  "summary_en": "3-sentence English summary of the article",
  "hashtags_en": ["kpop", "tag2", "tag3", "tag4", "tag5"]
}}"""

# 모듈-레벨 모델 캐시 (워커 프로세스 수명 동안 재사용)
_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model

    try:
        import google.generativeai as genai  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "google-generativeai 미설치. pip install google-generativeai"
        ) from exc

    from core.config import settings

    genai.configure(api_key=settings.GEMINI_API_KEY)
    _model = genai.GenerativeModel(
        settings.GEMINI_MODEL,
        generation_config=genai.GenerationConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    logger.debug("Gemini 모델 초기화 완료 | model=%s", settings.GEMINI_MODEL)
    return _model


def _call_gemini(prompt: str) -> dict:
    """Gemini API 호출 후 JSON dict 반환. Kill Switch + 토큰 사용량 기록 포함."""
    from core.config import check_gemini_kill_switch, record_gemini_usage

    check_gemini_kill_switch()

    model = _get_model()
    response = model.generate_content(prompt)

    # 토큰 사용량 기록 (SSM kill switch 연동)
    usage = getattr(response, "usage_metadata", None)
    total_tokens = getattr(usage, "total_token_count", 0)
    if total_tokens:
        try:
            record_gemini_usage(total_tokens)
        except Exception:
            pass

    return json.loads(response.text.strip())


def _process_one(article: "Article", session: "Session") -> bool:
    """
    기사 1개를 처리합니다.
    기존 필드가 있으면 덮어쓰지 않습니다 (멱등성).
    성공 시 True, 실패 시 False 반환.
    """
    from database.models import ProcessStatus

    # 한국어 제목이 없으면 처리 불가 → PROCESSED로 건너뜀
    if not article.title_ko:
        logger.warning("title_ko 없음, 스킵 | id=%d", article.id)
        article.process_status = ProcessStatus.PROCESSED
        session.commit()
        return True

    try:
        content_snippet = (article.content_ko or "")[:800]
        prompt = _PROMPT.format(
            title_ko=article.title_ko,
            content_snippet=content_snippet,
        )

        result = _call_gemini(prompt)

        # 기존 값이 없는 필드만 채움 (재처리 시 덮어쓰지 않음)
        if not article.title_en:
            article.title_en = result.get("title_en") or None
        if not article.summary_ko:
            article.summary_ko = result.get("summary_ko") or None
        if not article.summary_en:
            article.summary_en = result.get("summary_en") or None
        if not article.hashtags_en:
            tags = result.get("hashtags_en") or []
            if isinstance(tags, list):
                article.hashtags_en = [str(t).lstrip("#").strip() for t in tags if t]

        article.process_status = ProcessStatus.PROCESSED
        session.commit()
        logger.info("✓ id=%-6d %s", article.id, (article.title_ko or "")[:50])
        return True

    except Exception as exc:
        session.rollback()
        logger.warning("✗ id=%-6d %s: %s", article.id, type(exc).__name__, exc)
        try:
            article.process_status = ProcessStatus.ERROR
            session.commit()
        except Exception:
            session.rollback()
        return False


def process_scraped(batch_size: int = BATCH_SIZE) -> int:
    """
    SCRAPED 상태 기사를 최대 batch_size 개 처리합니다.
    처리 완료(PROCESSED + ERROR)된 기사 수를 반환합니다.

    scraper/worker.py 의 process_job() 성공 후 호출됩니다.
    """
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, ProcessStatus

    # 처리할 ID 목록 스냅샷 조회 (세션 즉시 반환)
    with get_db() as session:
        ids = list(
            session.scalars(
                select(Article.id)
                .where(Article.process_status == ProcessStatus.SCRAPED)
                .order_by(Article.published_at.desc().nullslast())
                .limit(batch_size)
            )
        )

    if not ids:
        logger.debug("처리할 SCRAPED 기사 없음")
        return 0

    logger.info("SCRAPED → AI 처리 시작 | %d개", len(ids))
    done = 0

    for article_id in ids:
        with get_db() as session:
            article = session.get(Article, article_id)
            # 이미 다른 프로세스가 처리했거나 없으면 스킵
            if article is None or article.process_status != ProcessStatus.SCRAPED:
                continue
            if _process_one(article, session):
                done += 1
        time.sleep(0.3)  # Gemini API 속도 제한 방지

    logger.info("AI 처리 완료 | 성공=%d / 대상=%d", done, len(ids))
    return done


def process_all_scraped() -> int:
    """
    SCRAPED 기사가 없어질 때까지 반복 처리합니다.
    scrape_range 처럼 대량 스크래핑 후 사용합니다.
    """
    total = 0
    while True:
        n = process_scraped(batch_size=BATCH_SIZE)
        total += n
        if n == 0:
            break
    if total:
        logger.info("전체 AI 처리 완료 | 총=%d", total)
    return total
