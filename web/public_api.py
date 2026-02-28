"""
web/public_api.py — 소비자 공개 API 라우터

소비자 사이트(tenasia-fan 등)에서 사용하는 읽기 전용 공개 엔드포인트.
PROCESSED 상태의 기사만 노출하며, 내부 운영 필드(*_source_article_id 등)는 제외합니다.

엔드포인트 목록:
  GET  /public/articles                 기사 목록 (PROCESSED)
  GET  /public/articles/{id}            기사 상세 (content_ko 포함)
  GET  /public/artists                  아티스트 목록
  GET  /public/artists/{id}             아티스트 프로필
  GET  /public/artists/{id}/articles    아티스트 관련 기사
  GET  /public/groups                   그룹 목록
  GET  /public/groups/{id}              그룹 프로필 + 멤버
  GET  /public/groups/{id}/articles     그룹 관련 기사
  GET  /public/search                   통합 검색 (기사+아티스트+그룹)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/public", tags=["public"])


# ─────────────────────────────────────────────────────────────
# 직렬화 헬퍼
# ─────────────────────────────────────────────────────────────

def _article_summary(a: Any) -> dict:
    """기사 목록용 요약 딕셔너리 (content_ko 제외)."""
    return {
        "id":              a.id,
        "title_ko":        a.title_ko,
        "title_en":        a.title_en,
        "summary_ko":      a.summary_ko,
        "summary_en":      a.summary_en,
        "author":          a.author,
        "published_at":    a.published_at.isoformat() if a.published_at else None,
        "artist_name_ko":  a.artist_name_ko,
        "artist_name_en":  a.artist_name_en,
        "hashtags_ko":     a.hashtags_ko or [],
        "hashtags_en":     a.hashtags_en or [],
        "thumbnail_url":   a.thumbnail_url,
        "source_url":      a.source_url,
        "language":        a.language,
        "sentiment":       a.sentiment,
    }


def _article_detail(a: Any) -> dict:
    """기사 상세용 딕셔너리 (content_ko 포함)."""
    d = _article_summary(a)
    d["content_ko"] = a.content_ko
    return d


def _artist_dict(a: Any, photo_url: Optional[str] = None) -> dict:
    """아티스트 공개 프로필 딕셔너리 (내부 FK 제외)."""
    return {
        "id":               a.id,
        "name_ko":          a.name_ko,
        "name_en":          a.name_en,
        "stage_name_ko":    a.stage_name_ko,
        "stage_name_en":    a.stage_name_en,
        "gender":           a.gender.value if a.gender else None,
        "birth_date":       a.birth_date.isoformat() if a.birth_date else None,
        "nationality_ko":   a.nationality_ko,
        "nationality_en":   a.nationality_en,
        "mbti":             a.mbti,
        "blood_type":       a.blood_type,
        "height_cm":        a.height_cm,
        "weight_kg":        a.weight_kg,
        "bio_ko":           a.bio_ko,
        "bio_en":           a.bio_en,
        "is_verified":      a.is_verified,
        "global_priority":  a.global_priority,
        "photo_url":        photo_url,
    }


def _group_dict(g: Any, photo_url: Optional[str] = None) -> dict:
    """그룹 공개 프로필 딕셔너리."""
    return {
        "id":                g.id,
        "name_ko":           g.name_ko,
        "name_en":           g.name_en,
        "gender":            g.gender.value if g.gender else None,
        "debut_date":        g.debut_date.isoformat() if g.debut_date else None,
        "label_ko":          g.label_ko,
        "label_en":          g.label_en,
        "fandom_name_ko":    g.fandom_name_ko,
        "fandom_name_en":    g.fandom_name_en,
        "activity_status":   g.activity_status.value if g.activity_status else None,
        "bio_ko":            g.bio_ko,
        "bio_en":            g.bio_en,
        "is_verified":       g.is_verified,
        "global_priority":   g.global_priority,
        "photo_url":         photo_url,
    }


def _member_dict(mo: Any) -> dict:
    """MemberOf 멤버 딕셔너리 (아티스트 기본 정보 포함)."""
    return {
        "artist_id":    mo.artist_id,
        "name_ko":      mo.artist.name_ko if mo.artist else None,
        "name_en":      mo.artist.name_en if mo.artist else None,
        "stage_name_ko": mo.artist.stage_name_ko if mo.artist else None,
        "stage_name_en": mo.artist.stage_name_en if mo.artist else None,
        "roles":        mo.roles or [],
        "started_on":   mo.started_on.isoformat() if mo.started_on else None,
        "ended_on":     mo.ended_on.isoformat() if mo.ended_on else None,
        "is_sub_unit":  mo.is_sub_unit,
    }


# ─────────────────────────────────────────────────────────────
# 기사 (Articles)
# ─────────────────────────────────────────────────────────────

@public_router.get("/articles")
def list_articles(
    limit:     int           = Query(20, ge=1, le=100),
    offset:    int           = Query(0,  ge=0),
    artist_id: Optional[int] = Query(None, description="특정 아티스트 관련 기사"),
    group_id:  Optional[int] = Query(None, description="특정 그룹 관련 기사"),
    language:  Optional[str] = Query(None, description="언어 코드 (kr/en/jp)"),
    q:         Optional[str] = Query(None, description="제목 검색어"),
) -> list[dict]:
    """
    소비자용 기사 목록.
    PROCESSED 상태의 기사만 반환합니다.
    """
    try:
        from core.db import get_db
        from database.models import Article, EntityMapping
        from sqlalchemy import select

        with get_db() as session:
            stmt = (
                select(Article)
                .where(Article.process_status == "PROCESSED")
                .order_by(Article.published_at.desc())
            )

            if artist_id is not None:
                stmt = stmt.join(
                    EntityMapping,
                    (EntityMapping.article_id == Article.id)
                    & (EntityMapping.artist_id == artist_id),
                ).distinct()

            if group_id is not None:
                stmt = stmt.join(
                    EntityMapping,
                    (EntityMapping.article_id == Article.id)
                    & (EntityMapping.group_id == group_id),
                ).distinct()

            if language:
                stmt = stmt.where(Article.language == language)

            if q:
                like = f"%{q}%"
                stmt = stmt.where(
                    Article.title_ko.ilike(like) | Article.title_en.ilike(like)
                )

            rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
            return [_article_summary(a) for a in rows]

    except Exception as exc:
        logger.exception("공개 기사 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/articles/{article_id}")
def get_article(article_id: int) -> dict:
    """기사 상세 (content_ko 포함)."""
    try:
        from core.db import get_db
        from database.models import Article

        with get_db() as session:
            article = session.get(Article, article_id)
            if article is None or article.process_status != "PROCESSED":
                raise HTTPException(status_code=404, detail="기사를 찾을 수 없습니다.")
            return _article_detail(article)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("공개 기사 상세 조회 실패 id=%d: %s", article_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────
# 아티스트 (Artists)
# ─────────────────────────────────────────────────────────────

@public_router.get("/artists")
def list_artists(
    q:               Optional[str] = Query(None, description="이름 검색 (한/영)"),
    limit:           int           = Query(50, ge=1, le=200),
    offset:          int           = Query(0,  ge=0),
    global_priority: Optional[int] = Query(None, description="번역 우선순위 (1/2/3)"),
) -> list[dict]:
    """아티스트 목록."""
    try:
        from core.db import get_db
        from database.models import Artist
        from sqlalchemy import or_, select

        with get_db() as session:
            stmt = select(Artist).order_by(Artist.name_ko)

            if q:
                like = f"%{q}%"
                stmt = stmt.where(
                    or_(Artist.name_ko.ilike(like), Artist.name_en.ilike(like))
                )
            if global_priority is not None:
                stmt = stmt.where(Artist.global_priority == global_priority)

            rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()

            # 아티스트별 photo_url: 해당 아티스트가 주인공인 기사 우선, 없으면 관련 기사
            photo_map: dict[int, str] = {}
            artist_ids = [a.id for a in rows]
            if artist_ids:
                from database.models import Article, EntityMapping
                # 1순위: article.artist_name_ko = artist.name_ko (이 아티스트가 주인공)
                primary_rows = session.execute(
                    select(EntityMapping.artist_id, Article.thumbnail_url)
                    .join(Article, Article.id == EntityMapping.article_id)
                    .join(Artist, Artist.id == EntityMapping.artist_id)
                    .where(
                        EntityMapping.artist_id.in_(artist_ids),
                        EntityMapping.artist_id.isnot(None),
                        Article.thumbnail_url.isnot(None),
                        Article.artist_name_ko == Artist.name_ko,
                    )
                    .order_by(EntityMapping.artist_id, Article.published_at.desc())
                ).all()
                for aid, url in primary_rows:
                    if aid not in photo_map:
                        photo_map[aid] = url

                # 2순위: 관련 기사 아무거나 (fallback)
                missing_ids = [aid for aid in artist_ids if aid not in photo_map]
                if missing_ids:
                    fallback_rows = session.execute(
                        select(EntityMapping.artist_id, Article.thumbnail_url)
                        .join(Article, Article.id == EntityMapping.article_id)
                        .where(
                            EntityMapping.artist_id.in_(missing_ids),
                            EntityMapping.artist_id.isnot(None),
                            Article.thumbnail_url.isnot(None),
                        )
                        .order_by(EntityMapping.artist_id, Article.published_at.desc())
                    ).all()
                    for aid, url in fallback_rows:
                        if aid not in photo_map:
                            photo_map[aid] = url

            return [_artist_dict(a, photo_url=photo_map.get(a.id)) for a in rows]

    except Exception as exc:
        logger.exception("공개 아티스트 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/artists/{artist_id}")
def get_artist(artist_id: int) -> dict:
    """아티스트 프로필 (소속 그룹 포함)."""
    try:
        from core.db import get_db
        from database.models import Artist, MemberOf
        from sqlalchemy import select

        with get_db() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise HTTPException(status_code=404, detail="아티스트를 찾을 수 없습니다.")

            # photo_url: 아티스트 이름이 주인공인 기사 우선, fallback은 관련 기사
            from database.models import Article, EntityMapping
            photo_url: Optional[str] = session.execute(
                select(Article.thumbnail_url)
                .join(EntityMapping, EntityMapping.article_id == Article.id)
                .where(
                    EntityMapping.artist_id == artist_id,
                    Article.thumbnail_url.isnot(None),
                    Article.artist_name_ko == artist.name_ko,
                )
                .order_by(Article.published_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if not photo_url:
                photo_url = session.execute(
                    select(Article.thumbnail_url)
                    .join(EntityMapping, EntityMapping.article_id == Article.id)
                    .where(
                        EntityMapping.artist_id == artist_id,
                        Article.thumbnail_url.isnot(None),
                    )
                    .order_by(Article.published_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

            result = _artist_dict(artist, photo_url=photo_url)

            # 소속 그룹 목록
            mo_rows = (
                session.execute(
                    select(MemberOf)
                    .where(MemberOf.artist_id == artist_id)
                    .order_by(MemberOf.started_on.desc())
                )
                .scalars()
                .all()
            )
            result["groups"] = [
                {
                    "group_id":    mo.group_id,
                    "name_ko":     mo.group.name_ko if mo.group else None,
                    "name_en":     mo.group.name_en if mo.group else None,
                    "roles":       mo.roles or [],
                    "started_on":  mo.started_on.isoformat() if mo.started_on else None,
                    "ended_on":    mo.ended_on.isoformat() if mo.ended_on else None,
                }
                for mo in mo_rows
            ]
            return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("공개 아티스트 상세 조회 실패 id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/artists/{artist_id}/articles")
def get_artist_articles(
    artist_id: int,
    limit:  int = Query(20, ge=1, le=100),
    offset: int = Query(0,  ge=0),
) -> list[dict]:
    """아티스트 관련 기사 목록."""
    try:
        from core.db import get_db
        from database.models import Article, Artist, EntityMapping
        from sqlalchemy import select

        with get_db() as session:
            if session.get(Artist, artist_id) is None:
                raise HTTPException(status_code=404, detail="아티스트를 찾을 수 없습니다.")

            stmt = (
                select(Article)
                .join(
                    EntityMapping,
                    (EntityMapping.article_id == Article.id)
                    & (EntityMapping.artist_id == artist_id),
                )
                .where(Article.process_status == "PROCESSED")
                .order_by(Article.published_at.desc())
                .limit(limit)
                .offset(offset)
                .distinct()
            )
            rows = session.execute(stmt).scalars().all()
            return [_article_summary(a) for a in rows]

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("아티스트 기사 조회 실패 artist_id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────
# 그룹 (Groups)
# ─────────────────────────────────────────────────────────────

@public_router.get("/groups")
def list_groups(
    q:      Optional[str] = Query(None, description="그룹명 검색 (한/영)"),
    limit:  int           = Query(50, ge=1, le=200),
    offset: int           = Query(0,  ge=0),
) -> list[dict]:
    """그룹 목록."""
    try:
        from core.db import get_db
        from database.models import Group
        from sqlalchemy import or_, select

        with get_db() as session:
            stmt = select(Group).order_by(Group.name_ko)

            if q:
                like = f"%{q}%"
                stmt = stmt.where(
                    or_(Group.name_ko.ilike(like), Group.name_en.ilike(like))
                )

            rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()

            # 그룹별 최신 기사 썸네일을 photo_url 로 사용
            group_photo_map: dict[int, str] = {}
            group_ids = [g.id for g in rows]
            if group_ids:
                from database.models import EntityMapping, Article
                gphoto_rows = session.execute(
                    select(EntityMapping.group_id, Article.thumbnail_url)
                    .join(Article, Article.id == EntityMapping.article_id)
                    .where(
                        EntityMapping.group_id.in_(group_ids),
                        EntityMapping.group_id.isnot(None),
                        Article.thumbnail_url.isnot(None),
                    )
                    .order_by(EntityMapping.group_id, Article.published_at.desc())
                ).all()
                for gid, url in gphoto_rows:
                    if gid not in group_photo_map:
                        group_photo_map[gid] = url

            return [_group_dict(g, photo_url=group_photo_map.get(g.id)) for g in rows]

    except Exception as exc:
        logger.exception("공개 그룹 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/groups/{group_id}")
def get_group(group_id: int) -> dict:
    """그룹 프로필 + 멤버 목록."""
    try:
        from core.db import get_db
        from database.models import Group, MemberOf
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload

        with get_db() as session:
            group = session.get(Group, group_id)
            if group is None:
                raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")

            # 최신 기사 썸네일을 photo_url 로
            from database.models import EntityMapping, Article
            gphoto_row = session.execute(
                select(Article.thumbnail_url)
                .join(EntityMapping, EntityMapping.article_id == Article.id)
                .where(
                    EntityMapping.group_id == group_id,
                    Article.thumbnail_url.isnot(None),
                )
                .order_by(Article.published_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            result = _group_dict(group, photo_url=gphoto_row)

            # 멤버 목록 (Artist joinedload)
            mo_rows = (
                session.execute(
                    select(MemberOf)
                    .options(joinedload(MemberOf.artist))
                    .where(MemberOf.group_id == group_id)
                    .order_by(MemberOf.started_on.asc())
                )
                .scalars()
                .all()
            )
            result["members"] = [_member_dict(mo) for mo in mo_rows]
            return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("공개 그룹 상세 조회 실패 id=%d: %s", group_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/groups/{group_id}/articles")
def get_group_articles(
    group_id: int,
    limit:  int = Query(20, ge=1, le=100),
    offset: int = Query(0,  ge=0),
) -> list[dict]:
    """그룹 관련 기사 목록."""
    try:
        from core.db import get_db
        from database.models import Article, EntityMapping, Group
        from sqlalchemy import select

        with get_db() as session:
            if session.get(Group, group_id) is None:
                raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")

            stmt = (
                select(Article)
                .join(
                    EntityMapping,
                    (EntityMapping.article_id == Article.id)
                    & (EntityMapping.group_id == group_id),
                )
                .where(Article.process_status == "PROCESSED")
                .order_by(Article.published_at.desc())
                .limit(limit)
                .offset(offset)
                .distinct()
            )
            rows = session.execute(stmt).scalars().all()
            return [_article_summary(a) for a in rows]

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("그룹 기사 조회 실패 group_id=%d: %s", group_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────
# 통합 검색
# ─────────────────────────────────────────────────────────────

@public_router.get("/search")
def search(
    q:     str = Query(..., min_length=1, description="검색어"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """
    기사·아티스트·그룹 통합 검색.
    제목/이름에 대해 부분 일치 검색합니다.
    """
    try:
        from core.db import get_db
        from database.models import Article, Artist, Group
        from sqlalchemy import or_, select

        like = f"%{q}%"

        with get_db() as session:
            # 기사 검색
            article_stmt = (
                select(Article)
                .where(
                    Article.process_status == "PROCESSED",
                    or_(
                        Article.title_ko.ilike(like),
                        Article.title_en.ilike(like),
                        Article.artist_name_ko.ilike(like),
                        Article.artist_name_en.ilike(like),
                    ),
                )
                .order_by(Article.published_at.desc())
                .limit(limit)
            )
            articles = session.execute(article_stmt).scalars().all()

            # 아티스트 검색
            artist_stmt = (
                select(Artist)
                .where(
                    or_(Artist.name_ko.ilike(like), Artist.name_en.ilike(like))
                )
                .limit(10)
            )
            artists = session.execute(artist_stmt).scalars().all()

            # 그룹 검색
            group_stmt = (
                select(Group)
                .where(or_(Group.name_ko.ilike(like), Group.name_en.ilike(like)))
                .limit(10)
            )
            groups = session.execute(group_stmt).scalars().all()

            # 검색 결과에도 photo_url 포함
            from database.models import EntityMapping, Article as ArticleModel
            s_artist_ids = [a.id for a in artists]
            s_group_ids  = [g.id for g in groups]
            s_artist_photo: dict[int, str] = {}
            s_group_photo:  dict[int, str] = {}
            if s_artist_ids:
                for aid, url in session.execute(
                    select(EntityMapping.artist_id, ArticleModel.thumbnail_url)
                    .join(ArticleModel, ArticleModel.id == EntityMapping.article_id)
                    .where(EntityMapping.artist_id.in_(s_artist_ids), ArticleModel.thumbnail_url.isnot(None))
                    .order_by(EntityMapping.artist_id, ArticleModel.published_at.desc())
                ).all():
                    if aid not in s_artist_photo:
                        s_artist_photo[aid] = url
            if s_group_ids:
                for gid, url in session.execute(
                    select(EntityMapping.group_id, ArticleModel.thumbnail_url)
                    .join(ArticleModel, ArticleModel.id == EntityMapping.article_id)
                    .where(EntityMapping.group_id.in_(s_group_ids), ArticleModel.thumbnail_url.isnot(None))
                    .order_by(EntityMapping.group_id, ArticleModel.published_at.desc())
                ).all():
                    if gid not in s_group_photo:
                        s_group_photo[gid] = url

            return {
                "query":    q,
                "articles": [_article_summary(a) for a in articles],
                "artists":  [_artist_dict(a, photo_url=s_artist_photo.get(a.id)) for a in artists],
                "groups":   [_group_dict(g, photo_url=s_group_photo.get(g.id)) for g in groups],
            }

    except Exception as exc:
        logger.exception("통합 검색 실패 q=%r: %s", q, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
