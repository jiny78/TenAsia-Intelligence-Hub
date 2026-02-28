"""
processor/profile_enricher.py — Gemini 기반 아티스트/그룹 프로필 자동 보강

Gemini 의 K-pop 지식을 활용해 DB에 비어있는 프로필 필드를 채웁니다.
  · 아티스트: birth_date, nationality_ko, nationality_en, mbti, blood_type,
              height_cm, weight_kg, gender, stage_name_ko, stage_name_en, bio_ko, bio_en
  · 그룹     : debut_date, label_ko, label_en, fandom_name_ko, fandom_name_en,
              gender, activity_status, bio_ko, bio_en

보강 완료 시 enriched_at 타임스탬프를 기록합니다.
다음 실행에서는 enriched_at IS NULL인 항목만 처리합니다 (= 새로 생긴 프로필만).
이미 값이 있는 필드는 덮어쓰지 않습니다 (보완 only).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ARTIST_BATCH_SIZE = 10
GROUP_BATCH_SIZE  = 10

# ── 아티스트 프로필 프롬프트 ───────────────────────────────────────

_ARTIST_PROFILE_PROMPT = """\
You are a K-pop expert with comprehensive knowledge of K-pop idols.
Given the following list of K-pop idol names (Korean), provide their profile information.
Return ONLY a JSON array — no markdown, no explanation.

Idol names (Korean):
{names_json}

Return this JSON array (one object per name, same order):
[
  {{
    "name_ko": "Korean name as given",
    "stage_name_ko": "Stage name in Korean or null",
    "stage_name_en": "Stage name in English/romanized or null",
    "name_en": "Full legal name in English or null",
    "gender": "MALE" | "FEMALE" | "UNKNOWN",
    "birth_date": "YYYY-MM-DD or null",
    "nationality_ko": "국적 in Korean (e.g. 대한민국) or null",
    "nationality_en": "Nationality in English (e.g. South Korean) or null",
    "mbti": "MBTI type (e.g. ENFP) or null",
    "blood_type": "A" | "B" | "O" | "AB" | null,
    "height_cm": <integer cm or null>,
    "weight_kg": <integer kg or null>,
    "bio_ko": "1-2 sentence Korean biography or null",
    "bio_en": "1-2 sentence English biography or null"
  }}
]

Rules:
- Only return verified facts you are confident about — use null for uncertain data
- blood_type: use null if unknown, never guess
- birth_date: use full YYYY-MM-DD format; if only year known, use YYYY-01-01
- weight_kg: use null (often not disclosed, respect privacy)
- If the name is not a known K-pop idol, return all fields as null except name_ko"""

# ── 그룹 프로필 프롬프트 ──────────────────────────────────────────

_GROUP_PROFILE_PROMPT = """\
You are a K-pop expert with comprehensive knowledge of K-pop groups.
Given the following list of K-pop group names (Korean), provide their profile information.
Return ONLY a JSON array — no markdown, no explanation.

Group names (Korean):
{names_json}

Return this JSON array (one object per name, same order):
[
  {{
    "name_ko": "Korean name as given",
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
]

Rules:
- Only return verified facts you are confident about — use null for uncertain data
- debut_date: YYYY-MM-DD format; if only year known, use YYYY-01-01
- activity_status values:
    ACTIVE    = currently active as a group
    HIATUS    = temporarily on hiatus / long pause
    DISBANDED = officially disbanded/broken up
    SOLO_ONLY = each member active solo but group not officially disbanded
    null      = uncertain or insufficient information
- If the name is not a known K-pop group, return all fields as null except name_ko"""


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


def _call_gemini(prompt: str) -> list[dict]:
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
    return result if isinstance(result, list) else [result]


# ── 아티스트 보강 ─────────────────────────────────────────────────

def enrich_artists(batch_size: int = ARTIST_BATCH_SIZE) -> int:
    """
    enriched_at IS NULL인 아티스트를 Gemini로 보강합니다.
    보강 완료 후 enriched_at = NOW() 기록 → 다음 실행 시 스킵됩니다.
    이미 값이 있는 필드는 덮어쓰지 않습니다.
    보강된 아티스트 수를 반환합니다.
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
        artists = [{"id": a.id, "name_ko": a.name_ko} for a in rows]

    if not artists:
        logger.debug("보강할 아티스트 없음 (모두 enriched_at 기록됨)")
        return 0

    logger.info("아티스트 프로필 보강 시작 | %d명", len(artists))

    names_json = json.dumps([a["name_ko"] for a in artists], ensure_ascii=False)
    prompt = _ARTIST_PROFILE_PROMPT.format(names_json=names_json)

    try:
        results = _call_gemini(prompt)
    except Exception as exc:
        logger.exception("Gemini 호출 실패: %s", exc)
        return 0

    result_map: dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict) and r.get("name_ko"):
            result_map[r["name_ko"]] = r

    now = datetime.now(timezone.utc)
    count = 0

    for a_info in artists:
        r = result_map.get(a_info["name_ko"])
        try:
            from core.db import get_db
            with get_db() as session:
                artist = session.get(Artist, a_info["id"])
                if artist is None:
                    continue

                changed = False

                def _set(field: str, value):
                    nonlocal changed
                    if value and not getattr(artist, field):
                        setattr(artist, field, value)
                        changed = True

                if r:
                    _set("stage_name_ko",   r.get("stage_name_ko"))
                    _set("stage_name_en",   r.get("stage_name_en"))
                    _set("name_en",         r.get("name_en"))
                    _set("birth_date",      r.get("birth_date"))
                    _set("nationality_ko",  r.get("nationality_ko"))
                    _set("nationality_en",  r.get("nationality_en"))
                    _set("mbti",            r.get("mbti"))
                    _set("blood_type",      r.get("blood_type"))
                    _set("bio_ko",          r.get("bio_ko"))
                    _set("bio_en",          r.get("bio_en"))

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

                # 보강 완료 표시 — Gemini가 모르더라도 enriched_at 기록 (재시도 방지)
                artist.enriched_at = now
                session.commit()

                if changed:
                    logger.info("아티스트 보강 ✓ | %s", artist.name_ko)
                    count += 1
                else:
                    logger.debug("아티스트 보강 스킵 (Gemini 정보 없음) | %s", artist.name_ko)

        except Exception as exc:
            logger.warning("아티스트 보강 저장 실패 | id=%d: %s", a_info["id"], exc)

    logger.info("아티스트 프로필 보강 완료 | 보강=%d / 대상=%d", count, len(artists))
    return count


# ── 그룹 보강 ─────────────────────────────────────────────────────

def enrich_groups(batch_size: int = GROUP_BATCH_SIZE) -> int:
    """
    enriched_at IS NULL인 그룹을 Gemini로 보강합니다.
    보강 완료 후 enriched_at = NOW() 기록 → 다음 실행 시 스킵됩니다.
    이미 값이 있는 필드는 덮어쓰지 않습니다.
    보강된 그룹 수를 반환합니다.
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
        groups = [{"id": g.id, "name_ko": g.name_ko} for g in rows]

    if not groups:
        logger.debug("보강할 그룹 없음 (모두 enriched_at 기록됨)")
        return 0

    logger.info("그룹 프로필 보강 시작 | %d개", len(groups))

    names_json = json.dumps([g["name_ko"] for g in groups], ensure_ascii=False)
    prompt = _GROUP_PROFILE_PROMPT.format(names_json=names_json)

    try:
        results = _call_gemini(prompt)
    except Exception as exc:
        logger.exception("Gemini 호출 실패: %s", exc)
        return 0

    result_map: dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict) and r.get("name_ko"):
            result_map[r["name_ko"]] = r

    now = datetime.now(timezone.utc)
    count = 0

    for g_info in groups:
        r = result_map.get(g_info["name_ko"])
        try:
            from core.db import get_db
            with get_db() as session:
                group = session.get(Group, g_info["id"])
                if group is None:
                    continue

                changed = False

                def _set(field: str, value):
                    nonlocal changed
                    if value and not getattr(group, field):
                        setattr(group, field, value)
                        changed = True

                if r:
                    _set("name_en",         r.get("name_en"))
                    _set("debut_date",      r.get("debut_date"))
                    _set("label_ko",        r.get("label_ko"))
                    _set("label_en",        r.get("label_en"))
                    _set("fandom_name_ko",  r.get("fandom_name_ko"))
                    _set("fandom_name_en",  r.get("fandom_name_en"))
                    _set("bio_ko",          r.get("bio_ko"))
                    _set("bio_en",          r.get("bio_en"))

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

                # 보강 완료 표시 — Gemini가 모르더라도 enriched_at 기록 (재시도 방지)
                group.enriched_at = now
                session.commit()

                if changed:
                    logger.info("그룹 보강 ✓ | %s", group.name_ko)
                    count += 1
                else:
                    logger.debug("그룹 보강 스킵 (Gemini 정보 없음) | %s", group.name_ko)

        except Exception as exc:
            logger.warning("그룹 보강 저장 실패 | id=%d: %s", g_info["id"], exc)

    logger.info("그룹 프로필 보강 완료 | 보강=%d / 대상=%d", count, len(groups))
    return count


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
