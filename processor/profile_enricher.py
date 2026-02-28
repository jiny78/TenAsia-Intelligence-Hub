"""
processor/profile_enricher.py — Wikipedia + Gemini 기반 아티스트/그룹 프로필 자동 보강

보강 순서:
  1. Wikipedia 한국어 API 에서 실제 위키 텍스트를 가져옴 (최신, 정확)
  2. Gemini 가 위키 텍스트를 파싱해 구조화된 필드로 변환
  3. Wikipedia 페이지가 없으면 Gemini 기본 지식만 사용 (불확실 → null 반환)

보강 완료 시 enriched_at 타임스탬프 기록.
다음 실행에서는 enriched_at IS NULL 인 항목(새 프로필)만 처리.
이미 값이 있는 필드는 덮어쓰지 않음 (보완 only).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ARTIST_BATCH_SIZE = 10
GROUP_BATCH_SIZE  = 10

# ── Wikipedia 한국어 API ──────────────────────────────────────

def _fetch_wikipedia_extract(name_ko: str) -> str | None:
    """
    Wikipedia 한국어 API 에서 인물/그룹 소개글을 가져옵니다.
    페이지가 없거나 오류 시 None 반환.
    """
    try:
        params = urllib.parse.urlencode({
            "action":      "query",
            "titles":      name_ko,
            "prop":        "extracts",
            "exintro":     "1",
            "explaintext": "1",
            "redirects":   "1",
            "format":      "json",
            "utf8":        "1",
        })
        url = f"https://ko.wikipedia.org/w/api.php?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "TenAsiaBot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        pages = data.get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if pid == "-1":
                return None
            extract = page.get("extract", "").strip()
            return extract if len(extract) > 20 else None
    except Exception as exc:
        logger.debug("Wikipedia 조회 실패 | %s: %s", name_ko, exc)
        return None


# ── Gemini 프롬프트 ──────────────────────────────────────────

_ARTIST_PROMPT_WITH_WIKI = """\
You are a K-pop data extractor. Extract structured profile information from the Wikipedia text below.
Return ONLY a valid JSON object — no markdown, no explanation.

Artist name: {name_ko}
Wikipedia text:
{wiki_text}

Return this JSON object:
{{
  "verified_match": true or false (true = this Wikipedia article is definitely about this artist),
  "stage_name_ko": "Stage name in Korean or null",
  "stage_name_en": "Stage name in English/romanized or null",
  "name_en": "Full legal name in English or null",
  "gender": "MALE" | "FEMALE" | "UNKNOWN",
  "birth_date": "YYYY-MM-DD or null",
  "nationality_ko": "국적 in Korean or null",
  "nationality_en": "Nationality in English or null",
  "mbti": "MBTI type or null",
  "blood_type": "A" | "B" | "O" | "AB" | null,
  "height_cm": <integer or null>,
  "weight_kg": <integer or null>,
  "bio_ko": "1-2 sentence Korean biography based on the Wikipedia text, or null",
  "bio_en": "1-2 sentence English biography based on the Wikipedia text, or null"
}}

Rules:
- If verified_match is false, set ALL other fields to null
- Only extract facts clearly stated in the Wikipedia text — no inference
- blood_type: null if not mentioned
- weight_kg: null (privacy)"""

_ARTIST_PROMPT_NO_WIKI = """\
You are a K-pop expert. Provide profile information for this K-pop idol.
Return ONLY a valid JSON object — no markdown, no explanation.

Artist name (Korean): {name_ko}

Return this JSON object:
{{
  "verified_match": true or false (true = you are confident this is a known K-pop idol),
  "stage_name_ko": "Stage name in Korean or null",
  "stage_name_en": "Stage name in English/romanized or null",
  "name_en": "Full legal name in English or null",
  "gender": "MALE" | "FEMALE" | "UNKNOWN",
  "birth_date": "YYYY-MM-DD or null",
  "nationality_ko": "국적 in Korean or null",
  "nationality_en": "Nationality in English or null",
  "mbti": "MBTI type or null",
  "blood_type": "A" | "B" | "O" | "AB" | null,
  "height_cm": <integer or null>,
  "weight_kg": <integer or null>,
  "bio_ko": "1-2 sentence Korean biography or null",
  "bio_en": "1-2 sentence English biography or null"
}}

Rules:
- If verified_match is false (unknown idol or uncertain), set ALL other fields to null
- Only state facts you are highly confident about — use null for anything uncertain
- Do NOT confuse with similarly-named idols or groups (e.g., 누에라 ≠ 뉴이스트)
- blood_type: null if not widely known
- weight_kg: null (privacy)"""

_GROUP_PROMPT_WITH_WIKI = """\
You are a K-pop data extractor. Extract structured profile information from the Wikipedia text below.
Return ONLY a valid JSON object — no markdown, no explanation.

Group name: {name_ko}
Wikipedia text:
{wiki_text}

Return this JSON object:
{{
  "verified_match": true or false (true = this Wikipedia article is definitely about this group),
  "name_en": "Group name in English or null",
  "gender": "MALE" | "FEMALE" | "MIXED" | "UNKNOWN",
  "debut_date": "YYYY-MM-DD or null",
  "label_ko": "소속사명 in Korean or null",
  "label_en": "Label/agency name in English or null",
  "fandom_name_ko": "팬덤명 in Korean or null",
  "fandom_name_en": "Fandom name in English or null",
  "activity_status": "ACTIVE" | "HIATUS" | "DISBANDED" | "SOLO_ONLY" | null,
  "bio_ko": "1-2 sentence Korean biography based on the Wikipedia text, or null",
  "bio_en": "1-2 sentence English biography based on the Wikipedia text, or null"
}}

Rules:
- If verified_match is false, set ALL other fields to null
- Only extract facts clearly stated in the Wikipedia text — no inference
- activity_status: infer from the text (disbanded → DISBANDED, active → ACTIVE, etc.)"""

_GROUP_PROMPT_NO_WIKI = """\
You are a K-pop expert. Provide profile information for this K-pop group.
Return ONLY a valid JSON object — no markdown, no explanation.

Group name (Korean): {name_ko}

Return this JSON object:
{{
  "verified_match": true or false (true = you are confident this is a known K-pop group),
  "name_en": "Group name in English or null",
  "gender": "MALE" | "FEMALE" | "MIXED" | "UNKNOWN",
  "debut_date": "YYYY-MM-DD or null",
  "label_ko": "소속사명 in Korean or null",
  "label_en": "Label/agency name in English or null",
  "fandom_name_ko": "팬덤명 in Korean or null",
  "fandom_name_en": "Fandom name in English or null",
  "activity_status": "ACTIVE" | "HIATUS" | "DISBANDED" | "SOLO_ONLY" | null,
  "bio_ko": "1-2 sentence Korean biography or null",
  "bio_en": "1-2 sentence English biography or null"
}}

Rules:
- If verified_match is false (unknown group or uncertain), set ALL other fields to null
- Only state facts you are highly confident about — use null for anything uncertain
- Do NOT confuse with similarly-named groups (e.g., 누에라 ≠ 뉴이스트)
- activity_status: null if unsure about current status"""


def _get_model():
    try:
        import google.generativeai as genai  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("google-generativeai 미설치") from exc

    from core.config import settings
    genai.configure(api_key=settings.GEMINI_API_KEY)
    return genai.GenerativeModel(
        settings.GEMINI_MODEL,
        generation_config=genai.GenerationConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )


def _call_gemini_single(prompt: str) -> dict:
    """단일 엔티티 보강용 Gemini 호출 — JSON 객체 반환."""
    from core.config import check_gemini_kill_switch, record_gemini_usage
    check_gemini_kill_switch()

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
    return result if isinstance(result, dict) else {}


# ── 아티스트 보강 ─────────────────────────────────────────────────

def enrich_artists(batch_size: int = ARTIST_BATCH_SIZE, overwrite_bio: bool = False) -> int:
    """
    enriched_at IS NULL 인 아티스트를 Wikipedia + Gemini 로 보강합니다.
    보강 완료 후 enriched_at = NOW() 기록.
    overwrite_bio=True: bio_ko/bio_en도 기존 값 덮어씀 (재보강 시 사용).
    """
    from sqlalchemy import select
    from core.db import get_db
    from database.models import Artist

    with get_db() as session:
        rows = list(
            session.scalars(
                select(Artist)
                .where(Artist.enriched_at.is_(None))
                .order_by(Artist.global_priority.desc().nullslast(), Artist.id)
                .limit(batch_size)
            )
        )
        artists = [{"id": a.id, "name_ko": a.name_ko, "stage_name_ko": a.stage_name_ko} for a in rows]

    if not artists:
        logger.debug("보강할 아티스트 없음")
        return 0

    logger.info("아티스트 프로필 보강 시작 | %d명", len(artists))

    now = datetime.now(timezone.utc)
    count = 0

    for a_info in artists:
        name_ko = a_info["name_ko"]
        try:
            # 1. Wikipedia 조회 (stage_name_ko 우선, 없으면 name_ko)
            stage_name = a_info.get("stage_name_ko")
            wiki_text = None
            if stage_name and stage_name != name_ko:
                wiki_text = _fetch_wikipedia_extract(stage_name)
            if not wiki_text:
                wiki_text = _fetch_wikipedia_extract(name_ko)

            # 2. Gemini 호출 (Wikipedia 텍스트 유무에 따라 프롬프트 분기)
            if wiki_text:
                prompt = _ARTIST_PROMPT_WITH_WIKI.format(
                    name_ko=name_ko,
                    wiki_text=wiki_text[:3000],
                )
                logger.debug("아티스트 Wikipedia 활용 | %s (%d자)", name_ko, len(wiki_text))
            else:
                prompt = _ARTIST_PROMPT_NO_WIKI.format(name_ko=name_ko)

            r = _call_gemini_single(prompt)

            # verified_match = false → 전체 null (이름 혼동 방지)
            if not r.get("verified_match"):
                logger.info("아티스트 보강 건너뜀 (미검증) | %s", name_ko)
                _mark_enriched(Artist, a_info["id"], now)
                continue

            from core.db import get_db
            with get_db() as session:
                artist = session.get(Artist, a_info["id"])
                if artist is None:
                    continue

                changed = _apply_artist_fields(artist, r, overwrite_bio=overwrite_bio)
                artist.enriched_at = now
                session.commit()

            if changed:
                src = "Wikipedia" if wiki_text else "Gemini"
                logger.info("아티스트 보강 ✓ [%s] | %s", src, name_ko)
                count += 1
            else:
                logger.debug("아티스트 보강 변경 없음 | %s", name_ko)

        except Exception as exc:
            logger.warning("아티스트 보강 실패 | %s: %s", name_ko, exc)
            try:
                _mark_enriched(Artist, a_info["id"], now)
            except Exception:
                pass

    logger.info("아티스트 보강 완료 | 보강=%d / 대상=%d", count, len(artists))
    return count


def _apply_artist_fields(artist, r: dict, overwrite_bio: bool = False) -> bool:
    """아티스트 필드 적용. changed 여부 반환."""
    changed = False

    def _set(field: str, value):
        nonlocal changed
        if value and not getattr(artist, field):
            setattr(artist, field, value)
            changed = True

    def _set_bio(field: str, value):
        nonlocal changed
        if value and (not getattr(artist, field) or overwrite_bio):
            setattr(artist, field, value)
            changed = True

    _set("stage_name_ko",  r.get("stage_name_ko"))
    _set("stage_name_en",  r.get("stage_name_en"))
    _set("name_en",        r.get("name_en"))
    _set("birth_date",     r.get("birth_date"))
    _set("nationality_ko", r.get("nationality_ko"))
    _set("nationality_en", r.get("nationality_en"))
    _set("mbti",           r.get("mbti"))
    _set("blood_type",     r.get("blood_type"))
    _set_bio("bio_ko",     r.get("bio_ko"))
    _set_bio("bio_en",     r.get("bio_en"))

    if r.get("height_cm") and not artist.height_cm:
        try:
            artist.height_cm = int(r["height_cm"])
            changed = True
        except (ValueError, TypeError):
            pass

    if r.get("weight_kg") and not artist.weight_kg:
        try:
            artist.weight_kg = int(r["weight_kg"])
            changed = True
        except (ValueError, TypeError):
            pass

    gender_val = r.get("gender")
    if gender_val and not artist.gender:
        from database.models import ArtistGender
        try:
            artist.gender = ArtistGender(gender_val)
            changed = True
        except ValueError:
            pass

    return changed


# ── 그룹 보강 ─────────────────────────────────────────────────────

def enrich_groups(batch_size: int = GROUP_BATCH_SIZE, overwrite_bio: bool = False) -> int:
    """
    enriched_at IS NULL 인 그룹을 Wikipedia + Gemini 로 보강합니다.
    보강 완료 후 enriched_at = NOW() 기록.
    overwrite_bio=True: bio_ko/bio_en도 기존 값 덮어씀 (재보강 시 사용).
    """
    from sqlalchemy import select
    from core.db import get_db
    from database.models import Group

    with get_db() as session:
        rows = list(
            session.scalars(
                select(Group)
                .where(Group.enriched_at.is_(None))
                .order_by(Group.global_priority.desc().nullslast(), Group.id)
                .limit(batch_size)
            )
        )
        groups = [{"id": g.id, "name_ko": g.name_ko, "name_en": g.name_en} for g in rows]

    if not groups:
        logger.debug("보강할 그룹 없음")
        return 0

    logger.info("그룹 프로필 보강 시작 | %d개", len(groups))

    now = datetime.now(timezone.utc)
    count = 0

    for g_info in groups:
        name_ko = g_info["name_ko"]
        name_en = g_info.get("name_en")
        try:
            # Wikipedia 조회: name_ko 우선 → name_en 폴백 (영문명으로 한국어 Wikipedia 검색)
            wiki_text = _fetch_wikipedia_extract(name_ko)
            if not wiki_text and name_en:
                wiki_text = _fetch_wikipedia_extract(name_en)

            if wiki_text:
                prompt = _GROUP_PROMPT_WITH_WIKI.format(
                    name_ko=name_ko,
                    wiki_text=wiki_text[:3000],
                )
                logger.debug("그룹 Wikipedia 활용 | %s (%d자)", name_ko, len(wiki_text))
            else:
                prompt = _GROUP_PROMPT_NO_WIKI.format(name_ko=name_ko)

            r = _call_gemini_single(prompt)

            if not r.get("verified_match"):
                logger.info("그룹 보강 건너뜀 (미검증) | %s", name_ko)
                _mark_enriched(Group, g_info["id"], now)
                continue

            from core.db import get_db
            with get_db() as session:
                group = session.get(Group, g_info["id"])
                if group is None:
                    continue

                changed = _apply_group_fields(group, r, overwrite_bio=overwrite_bio)
                group.enriched_at = now
                session.commit()

            if changed:
                src = "Wikipedia" if wiki_text else "Gemini"
                logger.info("그룹 보강 ✓ [%s] | %s", src, name_ko)
                count += 1
            else:
                logger.debug("그룹 보강 변경 없음 | %s", name_ko)

        except Exception as exc:
            logger.warning("그룹 보강 실패 | %s: %s", name_ko, exc)
            try:
                _mark_enriched(Group, g_info["id"], now)
            except Exception:
                pass

    logger.info("그룹 보강 완료 | 보강=%d / 대상=%d", count, len(groups))
    return count


def _apply_group_fields(group, r: dict, overwrite_bio: bool = False) -> bool:
    """그룹 필드 적용. changed 여부 반환."""
    changed = False

    def _set(field: str, value):
        nonlocal changed
        if value and not getattr(group, field):
            setattr(group, field, value)
            changed = True

    def _set_bio(field: str, value):
        nonlocal changed
        if value and (not getattr(group, field) or overwrite_bio):
            setattr(group, field, value)
            changed = True

    _set("name_en",        r.get("name_en"))
    _set("debut_date",     r.get("debut_date"))
    _set("label_ko",       r.get("label_ko"))
    _set("label_en",       r.get("label_en"))
    _set("fandom_name_ko", r.get("fandom_name_ko"))
    _set("fandom_name_en", r.get("fandom_name_en"))
    _set_bio("bio_ko",     r.get("bio_ko"))
    _set_bio("bio_en",     r.get("bio_en"))

    gender_val = r.get("gender")
    if gender_val and not group.gender:
        from database.models import ArtistGender
        try:
            group.gender = ArtistGender(gender_val)
            changed = True
        except ValueError:
            pass

    status_val = r.get("activity_status")
    if status_val and group.activity_status is None:
        from database.models import ActivityStatus
        try:
            group.activity_status = ActivityStatus(status_val)
            changed = True
        except ValueError:
            pass

    return changed


def _mark_enriched(model_cls, entity_id: int, now: datetime) -> None:
    """enriched_at 만 업데이트 (보강 없이 스킵 처리)."""
    from core.db import get_db
    with get_db() as session:
        obj = session.get(model_cls, entity_id)
        if obj:
            obj.enriched_at = now
            session.commit()


def re_enrich_sparse(limit: int = 200) -> dict[str, int]:
    """
    핵심 필드가 비어있는 아티스트/그룹을 Wikipedia + Gemini로 재보강합니다.
    - 그룹: bio_ko IS NULL OR label_ko IS NULL OR debut_date IS NULL
    - 아티스트: bio_ko IS NULL OR birth_date IS NULL OR name_en IS NULL
    enriched_at을 NULL로 리셋한 뒤 enrich_artists/enrich_groups를 호출합니다.
    bio_ko/bio_en은 기존 값이 있어도 Wikipedia 기반으로 덮어씁니다.
    """
    from sqlalchemy import or_, select
    from core.db import get_db
    from database.models import Artist, Group

    with get_db() as session:
        sparse_artists = list(
            session.scalars(
                select(Artist)
                .where(Artist.enriched_at.isnot(None))
                .where(
                    or_(
                        Artist.bio_ko.is_(None),
                        Artist.birth_date.is_(None),
                        Artist.name_en.is_(None),
                    )
                )
                .order_by(Artist.global_priority.desc().nullslast(), Artist.id)
                .limit(limit)
            )
        )
        for a in sparse_artists:
            a.enriched_at = None
        artist_count = len(sparse_artists)

        sparse_groups = list(
            session.scalars(
                select(Group)
                .where(Group.enriched_at.isnot(None))
                .where(
                    or_(
                        Group.bio_ko.is_(None),
                        Group.label_ko.is_(None),
                        Group.debut_date.is_(None),
                    )
                )
                .order_by(Group.global_priority.desc().nullslast(), Group.id)
                .limit(limit)
            )
        )
        for g in sparse_groups:
            g.enriched_at = None
        group_count = len(sparse_groups)

        session.commit()

    logger.info("재보강 대상 리셋 | 아티스트=%d 그룹=%d", artist_count, group_count)

    enriched_artists = enrich_artists(batch_size=max(artist_count, 1), overwrite_bio=True)
    enriched_groups  = enrich_groups(batch_size=max(group_count, 1),   overwrite_bio=True)

    logger.info("재보강 완료 | 아티스트=%d 그룹=%d", enriched_artists, enriched_groups)
    return {"artists": enriched_artists, "groups": enriched_groups}


def enrich_all_profiles() -> dict[str, int]:
    """아티스트와 그룹 전체를 보강합니다. {artists: N, groups: N} 반환."""
    artist_total = 0
    while True:
        n = enrich_artists()
        artist_total += n
        if n == 0:
            break

    group_total = 0
    while True:
        n = enrich_groups()
        group_total += n
        if n == 0:
            break

    logger.info("전체 프로필 보강 완료 | 아티스트=%d 그룹=%d", artist_total, group_total)
    return {"artists": artist_total, "groups": group_total}
