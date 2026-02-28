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
MIN_CONTENT_LEN = 300  # RSS 본문 최소 길이 (이보다 짧으면 전체 페이지 스크래핑 대상)
THUMBNAIL_BACKFILL_DAYS = 20   # 썸네일 백필 대상: 최근 N일치 기사
THUMBNAIL_BACKFILL_BATCH = 30  # 썸네일 백필 1회 처리 기사 수

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

    _t_api = time.perf_counter()
    response = model.generate_content(prompt)
    _t_api_ms = round((time.perf_counter() - _t_api) * 1000)

    usage = getattr(response, "usage_metadata", None)
    total_tokens = getattr(usage, "total_token_count", 0)
    prompt_tokens = getattr(usage, "prompt_token_count", 0)
    completion_tokens = getattr(usage, "candidates_token_count", 0)
    if total_tokens:
        try:
            record_gemini_usage(total_tokens)
        except Exception:
            pass

    logger.info(
        "gemini_batch_call | n=%d t_api=%dms tokens=%d (prompt=%d completion=%d)",
        len(articles), _t_api_ms, total_tokens, prompt_tokens, completion_tokens,
    )

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

    _t_batch = time.perf_counter()
    logger.info("배치 AI 처리 시작 | %d개 → Gemini 1회 호출", len(ids))

    try:
        # title_ko 없는 기사는 Gemini 건너뜀
        valid_articles = [a for a in articles if a.title_ko]
        skip_ids = [a.id for a in articles if not a.title_ko]

        _t_gemini = time.perf_counter()
        results = _call_gemini_batch(valid_articles) if valid_articles else []
        _t_gemini_ms = round((time.perf_counter() - _t_gemini) * 1000)

        _t_apply = time.perf_counter()
        status = _apply_results({a.id: a for a in articles}, results)
        _t_apply_ms = round((time.perf_counter() - _t_apply) * 1000)

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
        _t_total_ms = round((time.perf_counter() - _t_batch) * 1000)
        logger.info(
            "배치 AI 처리 완료 | 성공=%d/%d | t_gemini=%dms t_apply=%dms t_total=%dms",
            done, len(ids), _t_gemini_ms, _t_apply_ms, _t_total_ms,
        )
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


def queue_fullscrape_for_new_entities(article_ids: list[int]) -> int:
    """
    신규 아티스트/그룹이 발견된 기사 중 본문이 짧은 기사(RSS 수집)에 대해
    전체 페이지 스크래핑을 job_queue에 등록합니다.

    조건: content_ko가 None 이거나 MIN_CONTENT_LEN(300자) 미만
    처리:
      1. summary_ko/en, hashtags_en 초기화 → 전체 본문 기반 재생성 유도
      2. EntityMapping 삭제 → 전체 본문으로 엔티티 재추출 유도
      3. scrape 잡을 job_queue에 등록

    Returns:
        등록된 잡 수
    """
    from sqlalchemy import delete as sa_delete

    from core.db import get_db
    from database.models import Article, EntityMapping
    from scraper.db import create_job

    queued = 0
    for article_id in article_ids:
        try:
            with get_db() as session:
                art = session.get(Article, article_id)
                if art is None:
                    continue
                content_len = len(art.content_ko or "")
                if content_len >= MIN_CONTENT_LEN:
                    logger.debug(
                        "full_scrape_skip | article_id=%d content_len=%d (충분)",
                        article_id, content_len,
                    )
                    continue
                source_url = art.source_url

                # AI 필드 초기화 → 전체 본문으로 재생성 유도
                art.summary_ko = None
                art.summary_en = None
                art.hashtags_en = None
                session.commit()

            # EntityMapping 삭제 → 전체 본문으로 엔티티 재추출
            with get_db() as session:
                session.execute(
                    sa_delete(EntityMapping).where(EntityMapping.article_id == article_id)
                )
                session.commit()

            create_job("scrape", {"source_url": source_url, "language": "kr"})
            logger.info(
                "hybrid_fullscrape_queued | article_id=%d content_len=%d url=%s",
                article_id, content_len, source_url,
            )
            queued += 1
        except Exception as exc:
            logger.warning("hybrid_fullscrape_queue_failed | article_id=%d: %s", article_id, exc)

    if queued:
        logger.info("하이브리드 스크래핑 큐 등록 완료 | %d개 기사 (신규 엔티티)", queued)
    return queued


def process_all_with_retry() -> int:
    """
    ERROR 기사를 SCRAPED으로 리셋한 뒤 전체 처리 + 엔티티 추출을 실행합니다.
    신규 엔티티 발견 시 얇은 본문 기사에 대해 전체 스크래핑 잡을 큐에 등록합니다.
    Worker 루프에서 호출합니다.
    """
    reset_error_to_scraped()
    n = process_all_scraped()
    # 번역 완료 후 엔티티 추출 전체 실행 (실패해도 번역 결과는 유지)
    try:
        all_new_entity_article_ids: list[int] = []
        while True:
            entity_count, new_entity_article_ids = process_entity_extraction()
            all_new_entity_article_ids.extend(new_entity_article_ids)
            if entity_count == 0:
                break
        # 신규 엔티티 발견 → 본문 짧은 기사 전체 스크래핑 예약 (하이브리드)
        if all_new_entity_article_ids:
            queue_fullscrape_for_new_entities(all_new_entity_article_ids)
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
        "activity_status_hint": "ACTIVE" | "HIATUS" | "DISBANDED" | "SOLO_ONLY" | null,
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
- activity_status_hint: ONLY set this for GROUP type when the article clearly announces a status change:
    ACTIVE    = comeback announced / new album / tour / award win / actively performing
    HIATUS    = activity suspension / hiatus / long break announced (활동 잠정 중단, 공백기)
    DISBANDED = official disbandment announced (해체, 공식 해체)
    SOLO_ONLY = all members going solo while group not officially disbanded
    null      = no clear status change announced (most articles — use null by default)
  Do NOT set activity_status_hint based on past events or vague mentions. Only set when the article's main topic IS the status change.
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


def _save_entity_results(results: list[dict]) -> tuple[int, list[int]]:
    """
    Gemini 엔티티 추출 결과를 artist/group/entity_mapping 테이블에 저장합니다.

    Returns:
        (count, new_entity_article_ids) — 신규 아티스트/그룹이 생성된 기사 ID 목록
    """
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
    new_entity_article_ids: list[int] = []  # 신규 엔티티 생성된 기사 ID
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
            activity_status_hint = (ent.get("activity_status_hint") or "").strip().upper() or None

            if not name_ko or confidence < 0.7:
                continue

            # 엄격한 연관성 조건: 제목 포함 또는 주어로 4회 이상 언급
            if not in_title and subject_count < 4:
                logger.debug(
                    "엔티티 필터링 | article_id=%d name_ko=%s in_title=%s subject_count=%d",
                    article_id, name_ko, in_title, subject_count,
                )
                continue

            # activity_status_hint 유효성 검사
            _valid_statuses = {"ACTIVE", "HIATUS", "DISBANDED", "SOLO_ONLY"}
            if activity_status_hint and activity_status_hint not in _valid_statuses:
                activity_status_hint = None

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
                            # 신규 그룹 → 하이브리드 스크래핑 대상 추가
                            new_entity_article_ids.append(article_id)
                            logger.info("새 그룹 생성 | name_ko=%s article_id=%d → 전체 스크래핑 예약", name_ko, article_id)
                        elif name_en and not group.name_en:
                            group.name_en = name_en
                        # 활동 내용 있으면 bio_ko 보완
                        if has_activity_content and activity_summary_ko and not group.bio_ko:
                            group.bio_ko = activity_summary_ko
                        # 기사에서 명확한 활동 상태 변화 감지 시 업데이트 (기존 값 덮어씀)
                        if activity_status_hint:
                            try:
                                new_status = ActivityStatus(activity_status_hint)
                                if group.activity_status != new_status:
                                    logger.info(
                                        "그룹 활동상태 업데이트 | %s: %s → %s (article_id=%d)",
                                        name_ko,
                                        group.activity_status,
                                        new_status,
                                        article_id,
                                    )
                                    group.activity_status = new_status
                            except ValueError:
                                pass
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
                            # 신규 아티스트 → 하이브리드 스크래핑 대상 추가
                            new_entity_article_ids.append(article_id)
                            logger.info("새 아티스트 생성 | name_ko=%s article_id=%d → 전체 스크래핑 예약", name_ko, article_id)
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

    return count, list(set(new_entity_article_ids))


def process_entity_extraction(batch_size: int = ENTITY_BATCH_SIZE) -> tuple[int, list[int]]:
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
        return 0, []

    logger.info("엔티티 추출 시작 | %d개 기사 → Gemini 1회 호출", len(article_data))

    try:
        results = _call_gemini_entity_batch(article_data)
        count, new_entity_article_ids = _save_entity_results(results)
        logger.info(
            "엔티티 추출 완료 | 처리=%d / 대상=%d | 신규엔티티=%d개 기사",
            count, len(article_data), len(new_entity_article_ids),
        )
        return count, new_entity_article_ids
    except Exception as exc:
        logger.exception("엔티티 추출 실패: %s", exc)
        return 0, []


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


# ─────────────────────────────────────────────────────────────
# 썸네일 백필 (최근 N일치 기사 원문 페이지 fetch → og:image 추출)
# ─────────────────────────────────────────────────────────────

def backfill_thumbnails_batch(
    limit: int = THUMBNAIL_BACKFILL_BATCH,
    days: int = THUMBNAIL_BACKFILL_DAYS,
) -> int:
    """
    최근 days일치 기사 중 thumbnail_url이 없는 PROCESSED 기사의 원문 페이지를
    직접 fetch하여 og:image / twitter:image를 추출하고 articles.thumbnail_url을 업데이트합니다.

    RSS 수집 시 enclosure 이미지가 없었던 기사를 사후 보완합니다.

    Returns:
        업데이트된 기사 수
    """
    import requests as _requests
    from bs4 import BeautifulSoup
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, ProcessStatus

    since = datetime.now(timezone.utc) - timedelta(days=days)

    with get_db() as session:
        rows = list(
            session.scalars(
                select(Article)
                .where(Article.process_status == ProcessStatus.PROCESSED)
                .where(Article.thumbnail_url.is_(None))
                .where(Article.published_at >= since)
                .where(Article.source_url.isnot(None))
                .order_by(Article.published_at.desc().nullslast())
                .limit(limit)
            )
        )
        article_data = [{"id": a.id, "source_url": a.source_url} for a in rows]

    if not article_data:
        logger.debug("썸네일 백필할 기사 없음 (최근 %d일)", days)
        return 0

    logger.info(
        "썸네일 백필 시작 | %d개 기사 (최근 %d일 thumbnail_url=NULL)",
        len(article_data), days,
    )

    sess = _requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    updated = 0
    for art in article_data:
        article_id = art["id"]
        url = art["source_url"]
        try:
            resp = sess.get(url, timeout=15, allow_redirects=True)
            if resp.status_code != 200:
                logger.debug(
                    "썸네일 fetch 실패 | id=%d status=%d", article_id, resp.status_code
                )
                time.sleep(0.5)
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # og:image / twitter:image 우선 순위로 추출
            thumb: str | None = None
            for meta in soup.find_all("meta"):
                prop = meta.get("property") or meta.get("name") or ""
                if prop in ("og:image", "twitter:image"):
                    thumb = (meta.get("content") or "").strip() or None
                    if thumb:
                        break

            if not thumb:
                logger.debug("og:image 없음 | id=%d url=%s", article_id, url)
                time.sleep(0.5)
                continue

            with get_db() as session:
                art_obj = session.get(Article, article_id)
                if art_obj and art_obj.thumbnail_url is None:
                    art_obj.thumbnail_url = thumb
                    session.commit()
                    updated += 1
                    logger.info(
                        "썸네일 백필 완료 | id=%d thumb=%.80s", article_id, thumb
                    )

            time.sleep(0.5)  # 서버 부하 방지

        except Exception as exc:
            logger.warning(
                "썸네일 백필 실패 | id=%d url=%s: %s", article_id, url, exc
            )
            time.sleep(0.5)

    if updated:
        logger.info(
            "썸네일 백필 배치 완료 | 업데이트=%d / 대상=%d (최근 %d일)",
            updated, len(article_data), days,
        )
    return updated


# ─────────────────────────────────────────────────────────────
# 아티스트/그룹 photo_url 백필 (article 썸네일 → artists/groups.photo_url)
# ─────────────────────────────────────────────────────────────

def backfill_artist_photos(limit: int = 100) -> tuple[int, int]:
    """
    photo_url이 없는 아티스트/그룹에 대해 entity_mappings로 연결된 기사 썸네일을
    artists.photo_url / groups.photo_url에 직접 저장합니다.

    - 1순위: article.artist_name_ko == artist.name_ko (주인공 기사)
    - 2순위: entity_mappings로 연결된 기사 아무거나 (fallback)

    Returns:
        (artist_updated, group_updated) 업데이트된 수
    """
    from sqlalchemy import select

    from core.db import get_db
    from database.models import Article, Artist, EntityMapping, EntityType, Group

    artist_updated = 0
    group_updated = 0

    # ── 아티스트 ──────────────────────────────────────────────
    with get_db() as session:
        artists = list(
            session.scalars(
                select(Artist)
                .where(Artist.photo_url.is_(None))
                .order_by(Artist.id)
                .limit(limit)
            )
        )

    for artist in artists:
        try:
            with get_db() as session:
                # 1순위: 주인공 기사 (artist_name_ko 일치)
                thumb = session.execute(
                    select(Article.thumbnail_url)
                    .join(EntityMapping, EntityMapping.article_id == Article.id)
                    .where(
                        EntityMapping.artist_id == artist.id,
                        EntityMapping.entity_type == EntityType.ARTIST,
                        Article.thumbnail_url.isnot(None),
                        Article.artist_name_ko == artist.name_ko,
                    )
                    .order_by(Article.published_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

                # 2순위: 관련 기사 아무거나
                if not thumb:
                    thumb = session.execute(
                        select(Article.thumbnail_url)
                        .join(EntityMapping, EntityMapping.article_id == Article.id)
                        .where(
                            EntityMapping.artist_id == artist.id,
                            EntityMapping.entity_type == EntityType.ARTIST,
                            Article.thumbnail_url.isnot(None),
                        )
                        .order_by(Article.published_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()

                if thumb:
                    art_obj = session.get(Artist, artist.id)
                    if art_obj and not art_obj.photo_url:
                        art_obj.photo_url = thumb
                        session.commit()
                        artist_updated += 1
                        logger.info("아티스트 photo_url 백필 | id=%d name=%s", artist.id, artist.name_ko)
        except Exception as exc:
            logger.warning("아티스트 photo_url 백필 실패 | id=%d: %s", artist.id, exc)

    # ── 그룹 ─────────────────────────────────────────────────
    with get_db() as session:
        groups = list(
            session.scalars(
                select(Group)
                .where(Group.photo_url.is_(None))
                .order_by(Group.id)
                .limit(limit)
            )
        )

    for group in groups:
        try:
            with get_db() as session:
                thumb = session.execute(
                    select(Article.thumbnail_url)
                    .join(EntityMapping, EntityMapping.article_id == Article.id)
                    .where(
                        EntityMapping.group_id == group.id,
                        EntityMapping.entity_type == EntityType.GROUP,
                        Article.thumbnail_url.isnot(None),
                    )
                    .order_by(Article.published_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

                if thumb:
                    grp_obj = session.get(Group, group.id)
                    if grp_obj and not grp_obj.photo_url:
                        grp_obj.photo_url = thumb
                        session.commit()
                        group_updated += 1
                        logger.info("그룹 photo_url 백필 | id=%d name=%s", group.id, group.name_ko)
        except Exception as exc:
            logger.warning("그룹 photo_url 백필 실패 | id=%d: %s", group.id, exc)

    if artist_updated or group_updated:
        logger.info(
            "아티스트/그룹 photo_url 백필 완료 | 아티스트=%d 그룹=%d",
            artist_updated, group_updated,
        )
    return artist_updated, group_updated
