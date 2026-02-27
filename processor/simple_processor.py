"""
processor/simple_processor.py — 경량 AI 후처리기 (Batch Fast Track)

핵심 설계:
  · N개 기사를 Gemini 1회 호출로 일괄 처리 (기존: 기사당 1회 → N배 빠름)
  · SCRAPED 상태 기사 처리 (기존 gemini_engine.py의 PENDING 불일치 버그 수정)
  · 기사당 ~200 토큰 (배치 20개 = 4000 토큰, 기존 단건 400 토큰 × 20 = 8000 토큰)
  · Kill Switch / 토큰 사용량 기록 연동

처리 결과:
  title_en, summary_ko, summary_en, hashtags_en 채움
  process_status: SCRAPED → PROCESSED (성공) / ERROR (실패)
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

BATCH_SIZE = 20  # 1회 Gemini 호출당 처리 기사 수

# 배치 프롬프트: N개 기사를 JSON 배열로 일괄 반환
_BATCH_PROMPT = """\
You are a K-pop news translator. Process the following {n} Korean articles.
Return ONLY a JSON array — no markdown, no explanation.

Articles:
{articles_json}

Return this JSON array (one object per article):
[
  {{
    "id": <integer article id>,
    "title_en": "English translation of the Korean title",
    "summary_ko": "2-sentence Korean summary",
    "summary_en": "2-sentence English summary",
    "hashtags_en": ["tag1", "tag2", "tag3"]
  }}
]"""

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
    logger.debug("Gemini 모델 초기화 | model=%s", settings.GEMINI_MODEL)
    return _model


def _call_gemini_batch(articles: list) -> list[dict]:
    """N개 기사를 Gemini 1회 호출로 처리하고 결과 배열을 반환합니다."""
    from core.config import check_gemini_kill_switch, record_gemini_usage

    check_gemini_kill_switch()

    articles_json = json.dumps(
        [
            {
                "id": a.id,
                "title_ko": a.title_ko or "",
                "content": (a.content_ko or "")[:400],  # 앞 400자만 전달
            }
            for a in articles
        ],
        ensure_ascii=False,
    )

    prompt = _BATCH_PROMPT.format(n=len(articles), articles_json=articles_json)
    model = _get_model()
    response = model.generate_content(prompt)

    usage = getattr(response, "usage_metadata", None)
    total_tokens = getattr(usage, "total_token_count", 0)
    if total_tokens:
        try:
            record_gemini_usage(total_tokens)
        except Exception:
            pass

    raw = response.text.strip()
    result = json.loads(raw)
    # 배열이 아니라 단일 객체로 반환되는 경우 대응
    if isinstance(result, dict):
        result = [result]
    return result


def _apply_results(article_map: dict, results: list) -> dict[int, bool]:
    """Gemini 결과를 DB에 적용합니다. {id: success} 매핑 반환."""
    from core.db import get_db
    from database.models import ProcessStatus

    status: dict[int, bool] = {}

    for r in results:
        article_id = r.get("id")
        if not article_id:
            continue
        article = article_map.get(article_id)
        if not article:
            continue

        with get_db() as session:
            art = session.get(type(article), article_id)
            if art is None or art.process_status.value != "SCRAPED":
                status[article_id] = False
                continue
            try:
                if not art.title_en:
                    art.title_en = r.get("title_en") or None
                if not art.summary_ko:
                    art.summary_ko = r.get("summary_ko") or None
                if not art.summary_en:
                    art.summary_en = r.get("summary_en") or None
                if not art.hashtags_en:
                    tags = r.get("hashtags_en") or []
                    if isinstance(tags, list):
                        art.hashtags_en = [str(t).lstrip("#").strip() for t in tags if t]
                art.process_status = ProcessStatus.PROCESSED
                session.commit()
                logger.info("✓ id=%-6d %s", art.id, (art.title_ko or "")[:50])
                status[article_id] = True
            except Exception as exc:
                session.rollback()
                logger.warning("✗ id=%-6d DB 저장 실패: %s", article_id, exc)
                try:
                    art.process_status = ProcessStatus.ERROR
                    session.commit()
                except Exception:
                    session.rollback()
                status[article_id] = False

    return status


def _mark_error(article_ids: list[int]) -> None:
    """지정한 기사들을 ERROR 상태로 표시합니다."""
    from core.db import get_db
    from database.models import Article, ProcessStatus

    for aid in article_ids:
        with get_db() as session:
            art = session.get(Article, aid)
            if art and art.process_status.value == "SCRAPED":
                try:
                    art.process_status = ProcessStatus.ERROR
                    session.commit()
                except Exception:
                    session.rollback()


def process_scraped_batch(batch_size: int = BATCH_SIZE) -> int:
    """
    SCRAPED 기사 최대 batch_size개를 Gemini 1회 호출로 일괄 처리합니다.
    완료된 기사 수를 반환합니다.

    기존 process_scraped()와 비교:
      - 기사 20개 → Gemini 1회 호출 (기존: 20회)
      - API 응답 대기 1회 (기존: 20회) → ~20배 빠름
    """
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, ProcessStatus

    # SCRAPED 기사 조회
    with get_db() as session:
        articles = list(
            session.scalars(
                select(Article)
                .where(Article.process_status == ProcessStatus.SCRAPED)
                .order_by(Article.published_at.desc().nullslast())
                .limit(batch_size)
            )
        )
        # 세션 밖에서 쓰기 위해 필요한 필드 미리 로드
        article_map = {
            a.id: type("_A", (), {
                "id": a.id,
                "title_ko": a.title_ko,
                "content_ko": a.content_ko,
            })()
            for a in articles
        }
        ids = list(article_map.keys())

    if not ids:
        logger.debug("처리할 SCRAPED 기사 없음")
        return 0

    logger.info("배치 AI 처리 시작 | %d개 → Gemini 1회 호출", len(ids))

    try:
        # title_ko 없는 기사는 Gemini 건너뜀
        valid_articles = [a for a in articles if a.title_ko]
        skip_ids = [a.id for a in articles if not a.title_ko]

        results = _call_gemini_batch(valid_articles) if valid_articles else []
        status = _apply_results({a.id: a for a in articles}, results)

        # title_ko 없는 기사는 PROCESSED로 직접 마킹
        if skip_ids:
            from core.db import get_db
            from database.models import ProcessStatus
            for aid in skip_ids:
                with get_db() as session:
                    art = session.get(Article, aid)
                    if art:
                        art.process_status = ProcessStatus.PROCESSED
                        session.commit()
                status[aid] = True

        done = sum(1 for v in status.values() if v)
        logger.info("배치 AI 처리 완료 | 성공=%d / 대상=%d", done, len(ids))
        return done

    except Exception as exc:
        logger.warning("배치 Gemini 호출 실패: %s — 기사 ERROR 처리", exc)
        _mark_error(ids)
        return 0


def process_all_scraped() -> int:
    """
    SCRAPED 기사가 없어질 때까지 배치 처리를 반복합니다.
    """
    total = 0
    while True:
        n = process_scraped_batch(batch_size=BATCH_SIZE)
        total += n
        if n == 0:
            break
    if total:
        logger.info("전체 배치 AI 처리 완료 | 총=%d", total)
    return total
