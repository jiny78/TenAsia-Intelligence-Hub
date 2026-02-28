"""
processor/simple_processor.py — 경량 AI 후처리기 (Batch Fast Track)

핵심 설계:
  · N개 기사를 Gemini 1회 호출로 일괄 처리 (기존: 기사당 1회 → N배 빠름)
  · SCRAPED 상태 기사 처리 (기존 gemini_engine.py의 PENDING 불일치 버그 수정)
  · 기사당 ~200 토큰 (배치 20개 = 4000 토큰, 기존 단건 400 토큰 × 20 = 8000 토큰)
  · Kill Switch / 토큰 사용량 기록 연동
  · 엔티티 추출: PROCESSED 기사에서 아티스트/그룹명 추출 → artists/groups/entity_mappings 저장

처리 결과:
  title_en, summary_ko, summary_en, hashtags_en 채움
  process_status: SCRAPED → PROCESSED (성공) / ERROR (실패)
  엔티티: artists, groups 테이블 UPSERT + entity_mappings 생성
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

BATCH_SIZE = 20        # 번역 1회 Gemini 호출당 처리 기사 수
ENTITY_BATCH_SIZE = 10  # 엔티티 추출 1회 Gemini 호출당 처리 기사 수

# 배치 프롬프트: N개 기사를 JSON 배열로 일괄 반환
_BATCH_PROMPT = """\
You are a K-pop news translator and sentiment analyzer. Process the following {n} Korean articles.
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
    "hashtags_en": ["tag1", "tag2", "tag3"],
    "sentiment": "POSITIVE" | "NEGATIVE" | "NEUTRAL"
  }}
]
Sentiment rules:
- POSITIVE: award wins, chart success, comeback, milestone, fan events, collaboration, praise
- NEGATIVE: controversy, scandal, criticism, member departure, conflict, health crisis, allegations
- NEUTRAL: general news, interviews, schedules, announcements, no strong positive/negative tone"""

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
                # 감성 분류 저장 (POSITIVE/NEGATIVE/NEUTRAL)
                sentiment = r.get("sentiment")
                if sentiment in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
                    art.sentiment = sentiment
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
        logger.exception("배치 Gemini 호출 실패 — 기사 ERROR 처리: %s", exc)
        _mark_error(ids)
        return 0


def reset_error_to_scraped(limit: int = 200) -> int:
    """
    ERROR 상태 기사를 SCRAPED으로 되돌려 재처리 큐에 진입시킵니다.
    최대 limit개를 처리하며, 리셋된 기사 수를 반환합니다.
    """
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, ProcessStatus

    with get_db() as session:
        articles = list(
            session.scalars(
                select(Article)
                .where(Article.process_status == ProcessStatus.ERROR)
                .order_by(Article.published_at.desc().nullslast())
                .limit(limit)
            )
        )
        ids = [a.id for a in articles]

    if not ids:
        return 0

    count = 0
    for aid in ids:
        with get_db() as session:
            art = session.get(Article, aid)
            if art and art.process_status == ProcessStatus.ERROR:
                art.process_status = ProcessStatus.SCRAPED
                session.commit()
                count += 1

    if count:
        logger.info("ERROR → SCRAPED 리셋 | %d개", count)
    return count


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


def process_all_with_retry() -> int:
    """
    ERROR 기사를 SCRAPED으로 리셋한 뒤 전체 처리 + 엔티티 추출을 실행합니다.
    Worker 루프에서 호출합니다.
    """
    reset_error_to_scraped()
    n = process_all_scraped()
    # 번역 완료 후 엔티티 추출 (실패해도 번역 결과는 유지)
    try:
        process_entity_extraction()
    except Exception as exc:
        logger.warning("엔티티 추출 실패 (번역 결과는 정상): %s", exc)
    return n


# ─────────────────────────────────────────────────────────────
# 엔티티 추출 (아티스트 / 그룹 → DB UPSERT + EntityMapping)
# ─────────────────────────────────────────────────────────────

# 엔티티 추출 프롬프트 (엄격한 조건)
_ENTITY_PROMPT = """\
You are a K-pop expert. Extract K-pop idol artists and groups from the following Korean articles.
Return ONLY a JSON array — no markdown, no explanation.

Articles:
{articles_json}

Return this JSON array (one object per article):
[
  {{
    "id": <integer article id>,
    "entities": [
      {{
        "name_ko": "Korean name (required)",
        "name_en": "English/romanized name or null",
        "type": "ARTIST" or "GROUP",
        "in_title": true if this entity's name appears in the article title,
        "subject_count": <integer: number of times this entity is the main subject (주어) in the body>,
        "has_activity_content": true if the article discusses this entity's activities/profile/career,
        "activity_summary_ko": "1-2 sentence Korean summary of the activity content, or null",
        "confidence": 0.7-1.0
      }}
    ],
    "primary_artist_ko": "primary artist or group name in Korean or null",
    "primary_artist_en": "primary artist or group name in English or null"
  }}
]

Rules:
- Only include K-pop idols and groups (not actors, presenters, companies, or other celebrities)
- in_title: true only if the entity name literally appears in the title string
- subject_count: count sentences where this entity is the grammatical subject/topic (주어로 등장)
- has_activity_content: true if article discusses comebacks, albums, concerts, awards, profile, group activities
- activity_summary_ko: brief summary of their activity/profile content if has_activity_content is true
- Strict confidence: 0.9+ for main subject, 0.7+ for clearly mentioned, below 0.7 = skip
- If no qualifying K-pop entities found, return empty entities array"""


def _call_gemini_entity_batch(articles: list[dict]) -> list[dict]:
    """N개 기사에서 아티스트/그룹 엔티티를 Gemini 1회 호출로 추출합니다."""
    from core.config import check_gemini_kill_switch, record_gemini_usage

    check_gemini_kill_switch()

    articles_json = json.dumps(
        [
            {
                "id": a["id"],
                "title": a["title_ko"] or "",
                "content": (a["content_ko"] or "")[:800],  # 주어 횟수 카운트를 위해 더 많이 전달
            }
            for a in articles
        ],
        ensure_ascii=False,
    )

    prompt = _ENTITY_PROMPT.format(articles_json=articles_json)
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
    if isinstance(result, dict):
        result = [result]
    return result


def _save_entity_results(results: list[dict]) -> int:
    """Gemini 엔티티 추출 결과를 artist/group/entity_mapping 테이블에 저장합니다."""
    from sqlalchemy import select

    from core.db import get_db
    from database.models import (
        ActivityStatus,
        Article,
        Artist,
        EntityMapping,
        EntityType,
        Group,
    )

    count = 0
    for r in results:
        article_id = r.get("id")
        entities = r.get("entities") or []
        primary_ko = (r.get("primary_artist_ko") or "").strip() or None
        primary_en = (r.get("primary_artist_en") or "").strip() or None

        if not article_id:
            continue

        for ent in entities:
            name_ko = (ent.get("name_ko") or "").strip()
            name_en = (ent.get("name_en") or "").strip() or None
            etype = (ent.get("type") or "ARTIST").upper()
            confidence = float(ent.get("confidence", 0.5))
            in_title = bool(ent.get("in_title", False))
            subject_count = int(ent.get("subject_count", 0))
            has_activity_content = bool(ent.get("has_activity_content", False))
            activity_summary_ko = (ent.get("activity_summary_ko") or "").strip() or None

            if not name_ko or confidence < 0.7:
                continue

            # 엄격한 연관성 조건: 제목 포함 또는 주어로 4회 이상 언급
            if not in_title and subject_count < 4:
                logger.debug(
                    "엔티티 필터링 | article_id=%d name_ko=%s in_title=%s subject_count=%d",
                    article_id, name_ko, in_title, subject_count,
                )
                continue

            try:
                if etype == "GROUP":
                    with get_db() as session:
                        group = session.scalars(
                            select(Group).where(Group.name_ko == name_ko)
                        ).first()
                        if group is None:
                            group = Group(
                                name_ko=name_ko,
                                name_en=name_en,
                                activity_status=ActivityStatus.ACTIVE,
                            )
                            session.add(group)
                            session.flush()
                        elif name_en and not group.name_en:
                            group.name_en = name_en
                        # 활동 내용 있으면 bio_ko 보완
                        if has_activity_content and activity_summary_ko and not group.bio_ko:
                            group.bio_ko = activity_summary_ko
                        entity_id = group.id
                        session.commit()

                    with get_db() as session:
                        existing = session.scalars(
                            select(EntityMapping)
                            .where(EntityMapping.article_id == article_id)
                            .where(EntityMapping.group_id == entity_id)
                        ).first()
                        if existing is None:
                            session.add(EntityMapping(
                                article_id=article_id,
                                entity_type=EntityType.GROUP,
                                group_id=entity_id,
                                confidence_score=min(confidence, 1.0),
                            ))
                            session.commit()

                else:  # ARTIST
                    with get_db() as session:
                        artist = session.scalars(
                            select(Artist).where(Artist.name_ko == name_ko)
                        ).first()
                        if artist is None:
                            artist = Artist(name_ko=name_ko, name_en=name_en)
                            session.add(artist)
                            session.flush()
                        elif name_en and not artist.name_en:
                            artist.name_en = name_en
                        # 활동 내용 있으면 bio_ko 보완
                        if has_activity_content and activity_summary_ko and not artist.bio_ko:
                            artist.bio_ko = activity_summary_ko
                        entity_id = artist.id
                        session.commit()

                    with get_db() as session:
                        existing = session.scalars(
                            select(EntityMapping)
                            .where(EntityMapping.article_id == article_id)
                            .where(EntityMapping.artist_id == entity_id)
                        ).first()
                        if existing is None:
                            session.add(EntityMapping(
                                article_id=article_id,
                                entity_type=EntityType.ARTIST,
                                artist_id=entity_id,
                                confidence_score=min(confidence, 1.0),
                            ))
                            session.commit()

            except Exception as exc:
                logger.warning(
                    "엔티티 저장 실패 | article_id=%d name_ko=%s: %s",
                    article_id, name_ko, exc,
                )

        # 대표 아티스트 이름 업데이트
        if primary_ko:
            try:
                with get_db() as session:
                    art = session.get(Article, article_id)
                    if art and not art.artist_name_ko:
                        art.artist_name_ko = primary_ko
                        if primary_en:
                            art.artist_name_en = primary_en
                        session.commit()
            except Exception as exc:
                logger.warning(
                    "artist_name 업데이트 실패 | article_id=%d: %s", article_id, exc
                )

        # 엔티티가 없는 기사는 sentinel EntityMapping(EVENT, confidence=0)을 생성해
        # 다음 추출 사이클에서 재처리되지 않도록 표시합니다.
        try:
            with get_db() as session:
                has_any = session.scalars(
                    select(EntityMapping).where(EntityMapping.article_id == article_id)
                ).first()
                if has_any is None:
                    session.add(EntityMapping(
                        article_id=article_id,
                        entity_type=EntityType.EVENT,
                        confidence_score=0.0,
                    ))
                    session.commit()
        except Exception as exc:
            logger.warning(
                "sentinel EntityMapping 생성 실패 | article_id=%d: %s", article_id, exc
            )

        count += 1

    return count


def process_entity_extraction(batch_size: int = ENTITY_BATCH_SIZE) -> int:
    """
    EntityMapping이 없는 PROCESSED 기사에서 K-pop 아티스트/그룹 엔티티를 추출합니다.

    - Gemini 1회 호출로 batch_size개 기사 처리
    - artists / groups 테이블 UPSERT (name_ko 기준)
    - entity_mappings 레코드 생성
    - articles.artist_name_ko/en 업데이트 (대표 아티스트)
    - process_status는 변경하지 않음 (PROCESSED 유지)
    """
    from sqlalchemy import exists as sa_exists
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, EntityMapping, ProcessStatus

    with get_db() as session:
        has_mapping = sa_exists().where(EntityMapping.article_id == Article.id)
        rows = list(
            session.scalars(
                select(Article)
                .where(Article.process_status == ProcessStatus.PROCESSED)
                .where(~has_mapping)
                .where(Article.title_ko.isnot(None))
                .order_by(Article.published_at.desc().nullslast())
                .limit(batch_size)
            )
        )
        article_data = [
            {
                "id": a.id,
                "title_ko": a.title_ko or "",
                "content_ko": (a.content_ko or "")[:800],
            }
            for a in rows
        ]

    if not article_data:
        logger.debug("엔티티 추출할 PROCESSED 기사 없음")
        return 0

    logger.info("엔티티 추출 시작 | %d개 기사 → Gemini 1회 호출", len(article_data))

    try:
        results = _call_gemini_entity_batch(article_data)
        count = _save_entity_results(results)
        logger.info("엔티티 추출 완료 | 처리=%d / 대상=%d", count, len(article_data))
        return count
    except Exception as exc:
        logger.exception("엔티티 추출 실패: %s", exc)
        return 0


def process_all_entity_extraction() -> int:
    """EntityMapping이 없는 PROCESSED 기사 전체에 엔티티 추출을 실행합니다."""
    total = 0
    while True:
        n = process_entity_extraction(batch_size=ENTITY_BATCH_SIZE)
        total += n
        if n == 0:
            break
    if total:
        logger.info("전체 엔티티 추출 완료 | 총=%d", total)
    return total


# ─────────────────────────────────────────────────────────────
# 감성 분류 (기존 PROCESSED 기사 소급 처리)
# ─────────────────────────────────────────────────────────────

_SENTIMENT_PROMPT = """\
You are a K-pop news sentiment analyzer. Classify each article as POSITIVE, NEGATIVE, or NEUTRAL.
Return ONLY a JSON array — no markdown, no explanation.

Articles:
{articles_json}

Return this JSON array:
[
  {{
    "id": <article id>,
    "sentiment": "POSITIVE" | "NEGATIVE" | "NEUTRAL"
  }}
]
Rules:
- POSITIVE: award wins, chart success, comeback, milestone, fan events, collaboration, praise
- NEGATIVE: controversy, scandal, criticism, member departure, conflict, health crisis, allegations
- NEUTRAL: general news, interviews, schedules, announcements, no strong positive/negative tone"""

SENTIMENT_BATCH_SIZE = 20


def process_sentiment_batch(batch_size: int = SENTIMENT_BATCH_SIZE) -> int:
    """
    sentiment가 NULL인 PROCESSED 기사를 Gemini 1회 호출로 감성 분류합니다.
    완료된 기사 수를 반환합니다.
    """
    from sqlalchemy import select

    from core.config import check_gemini_kill_switch, record_gemini_usage
    from core.db import get_db
    from database.models import Article, ProcessStatus

    with get_db() as session:
        rows = list(
            session.scalars(
                select(Article)
                .where(Article.process_status == ProcessStatus.PROCESSED)
                .where(Article.sentiment.is_(None))
                .where(Article.title_ko.isnot(None))
                .order_by(Article.published_at.desc().nullslast())
                .limit(batch_size)
            )
        )
        article_data = [
            {"id": a.id, "title": a.title_ko or "", "content": (a.content_ko or "")[:300]}
            for a in rows
        ]

    if not article_data:
        logger.debug("감성 분류할 기사 없음")
        return 0

    logger.info("감성 분류 시작 | %d개 기사 → Gemini 1회 호출", len(article_data))

    try:
        check_gemini_kill_switch()

        articles_json = json.dumps(article_data, ensure_ascii=False)
        prompt = _SENTIMENT_PROMPT.format(articles_json=articles_json)
        model = _get_model()
        response = model.generate_content(prompt)

        usage = getattr(response, "usage_metadata", None)
        total_tokens = getattr(usage, "total_token_count", 0)
        if total_tokens:
            try:
                record_gemini_usage(total_tokens)
            except Exception:
                pass

        results = json.loads(response.text.strip())
        if isinstance(results, dict):
            results = [results]

        count = 0
        for r in results:
            article_id = r.get("id")
            sentiment = r.get("sentiment")
            if not article_id or sentiment not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
                continue
            try:
                from core.db import get_db
                with get_db() as session:
                    art = session.get(Article, article_id)
                    if art and art.sentiment is None:
                        art.sentiment = sentiment
                        session.commit()
                        count += 1
            except Exception as exc:
                logger.warning("감성 저장 실패 | id=%d: %s", article_id, exc)

        logger.info("감성 분류 완료 | 처리=%d / 대상=%d", count, len(article_data))
        return count

    except Exception as exc:
        logger.exception("감성 분류 Gemini 호출 실패: %s", exc)
        return 0
