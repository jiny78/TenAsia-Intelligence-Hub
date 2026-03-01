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

from fastapi import APIRouter, Body, HTTPException, Query

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/public", tags=["public"])


# ─────────────────────────────────────────────────────────────
# 직렬화 헬퍼
# ─────────────────────────────────────────────────────────────

def _article_summary(a: Any) -> dict:
    """기사 목록용 요약 딕셔너리 (content_ko 제외)."""
    from core.config import settings
    s3_base = settings.s3_base_url

    extra_images: list[dict] = []
    for img in (getattr(a, "images", None) or []):
        if img.is_representative:
            continue
        url = (
            f"{s3_base}/{img.thumbnail_path}"
            if img.thumbnail_path
            else img.original_url
        )
        if url:
            extra_images.append({"url": url})
        if len(extra_images) >= 10:
            break

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
        "extra_images":    extra_images,
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
        # 기사 썸네일 우선, 없으면 artists.photo_url DB 컬럼 fallback
        "photo_url":        photo_url or getattr(a, "photo_url", None),
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
        # 기사 썸네일 우선, 없으면 groups.photo_url DB 컬럼 fallback
        "photo_url":         photo_url or getattr(g, "photo_url", None),
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
    limit:         int           = Query(20, ge=1, le=100),
    offset:        int           = Query(0,  ge=0),
    artist_id:     Optional[int] = Query(None, description="특정 아티스트 관련 기사"),
    group_id:      Optional[int] = Query(None, description="특정 그룹 관련 기사"),
    language:      Optional[str] = Query(None, description="언어 코드 (kr/en/jp)"),
    q:             Optional[str] = Query(None, description="제목 검색어"),
    has_thumbnail: Optional[bool] = Query(None, description="썸네일 있는 기사만"),
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
            from sqlalchemy.orm import selectinload
            stmt = (
                select(Article)
                .options(selectinload(Article.images))
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

            if has_thumbnail is True:
                stmt = stmt.where(Article.thumbnail_url.isnot(None))

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


# ─────────────────────────────────────────────────────────────
# 관리 (Admin) — 그룹 상태·엔티티 매핑 수동 편집
# ─────────────────────────────────────────────────────────────

@public_router.patch("/groups/{group_id}")
def update_group_status(
    group_id: int,
    activity_status: Optional[str] = Body(None, embed=True, description="ACTIVE/HIATUS/DISBANDED/SOLO_ONLY"),
    bio_ko: Optional[str] = Body(None, embed=True),
    bio_en: Optional[str] = Body(None, embed=True),
) -> dict:
    """그룹 활동 상태 및 소개글 수동 수정."""
    try:
        from core.db import get_db
        from database.models import ActivityStatus, Group

        valid_statuses = {s.value for s in ActivityStatus}
        if activity_status and activity_status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"유효하지 않은 상태: {activity_status}. 허용: {valid_statuses}")

        with get_db() as session:
            group = session.get(Group, group_id)
            if group is None:
                raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")
            if activity_status:
                group.activity_status = ActivityStatus(activity_status)
            if bio_ko is not None:
                group.bio_ko = bio_ko or None
            if bio_en is not None:
                group.bio_en = bio_en or None
            session.commit()
            return _group_dict(group)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("그룹 상태 수정 실패 id=%d: %s", group_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.delete("/groups/{group_id}", status_code=200)
def delete_group(group_id: int) -> dict:
    """그룹 삭제 (관리자용). 관련 entity_mappings·멤버십도 cascade 삭제."""
    try:
        from core.db import get_db
        from database.models import Group

        with get_db() as session:
            group = session.get(Group, group_id)
            if group is None:
                raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")
            name = group.name_ko
            session.delete(group)
            session.commit()
            logger.info("그룹 삭제 | id=%d name=%s", group_id, name)
            return {"deleted": group_id, "name": name}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("그룹 삭제 실패 id=%d: %s", group_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.delete("/artists/{artist_id}", status_code=200)
def delete_artist(artist_id: int) -> dict:
    """아티스트 삭제 (관리자용). 관련 entity_mappings·멤버십도 cascade 삭제."""
    try:
        from core.db import get_db
        from database.models import Artist

        with get_db() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise HTTPException(status_code=404, detail="아티스트를 찾을 수 없습니다.")
            name = artist.name_ko
            session.delete(artist)
            session.commit()
            logger.info("아티스트 삭제 | id=%d name=%s", artist_id, name)
            return {"deleted": artist_id, "name": name}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("아티스트 삭제 실패 id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.patch("/artists/{artist_id}")
def update_artist(
    artist_id: int,
    bio_ko: Optional[str] = Body(None, embed=True),
    bio_en: Optional[str] = Body(None, embed=True),
) -> dict:
    """아티스트 소개글 수동 수정."""
    try:
        from core.db import get_db
        from database.models import Artist

        with get_db() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise HTTPException(status_code=404, detail="아티스트를 찾을 수 없습니다.")
            if bio_ko is not None:
                artist.bio_ko = bio_ko or None
            if bio_en is not None:
                artist.bio_en = bio_en or None
            session.commit()
            return _artist_dict(artist)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("아티스트 수정 실패 id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.get("/entity-mappings")
def list_entity_mappings(
    article_id: Optional[int] = Query(None),
    artist_id:  Optional[int] = Query(None),
    group_id:   Optional[int] = Query(None),
    q:          Optional[str] = Query(None, description="아티스트명/그룹명/기사제목 검색"),
    limit:      int           = Query(50, ge=1, le=200),
    offset:     int           = Query(0, ge=0),
) -> dict:
    """엔티티 매핑 목록 조회 (관리자용). {items, total} 반환."""
    try:
        from core.db import get_db
        from database.models import Article, Artist, EntityMapping, Group
        from sqlalchemy import func, or_, select
        from sqlalchemy.orm import joinedload

        with get_db() as session:
            # 기본 필터 목록 구성
            # sentinel(EVENT, confidence=0.0)은 파이프라인 재추출 방지용 — 목록에서 제외
            from database.models import EntityType as _EntityType
            base_filters = [
                ~((EntityMapping.entity_type == _EntityType.EVENT) &
                  (EntityMapping.confidence_score == 0.0))
            ]
            if article_id is not None:
                base_filters.append(EntityMapping.article_id == article_id)
            if artist_id is not None:
                base_filters.append(EntityMapping.artist_id == artist_id)
            if group_id is not None:
                base_filters.append(EntityMapping.group_id == group_id)

            # 이름 검색 시 outerjoin으로 매칭 ID 먼저 수집
            if q:
                like = f"%{q}%"
                id_stmt = (
                    select(EntityMapping.id)
                    .outerjoin(Artist, EntityMapping.artist_id == Artist.id)
                    .outerjoin(Group, EntityMapping.group_id == Group.id)
                    .outerjoin(Article, EntityMapping.article_id == Article.id)
                    .where(
                        or_(
                            Artist.name_ko.ilike(like),
                            Artist.stage_name_ko.ilike(like),
                            Group.name_ko.ilike(like),
                            Article.title_ko.ilike(like),
                        )
                    )
                )
                for f in base_filters:
                    id_stmt = id_stmt.where(f)
                matching_ids = session.execute(id_stmt).scalars().all()
                total = len(matching_ids)

                if not matching_ids:
                    return {"items": [], "total": 0}

                stmt = (
                    select(EntityMapping)
                    .options(
                        joinedload(EntityMapping.article),
                        joinedload(EntityMapping.artist),
                        joinedload(EntityMapping.group),
                    )
                    .where(EntityMapping.id.in_(matching_ids))
                    .order_by(EntityMapping.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            else:
                # COUNT 쿼리
                count_stmt = select(func.count()).select_from(EntityMapping)
                for f in base_filters:
                    count_stmt = count_stmt.where(f)
                total = session.scalar(count_stmt) or 0

                stmt = (
                    select(EntityMapping)
                    .options(
                        joinedload(EntityMapping.article),
                        joinedload(EntityMapping.artist),
                        joinedload(EntityMapping.group),
                    )
                    .order_by(EntityMapping.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                for f in base_filters:
                    stmt = stmt.where(f)

            rows = session.execute(stmt).scalars().all()
            return {
                "items": [
                    {
                        "id":               m.id,
                        "article_id":       m.article_id,
                        "article_title_ko": m.article.title_ko if m.article else None,
                        "article_url":      m.article.source_url if m.article else None,
                        "entity_type":      m.entity_type.value if m.entity_type else None,
                        "artist_id":        m.artist_id,
                        "artist_name_ko":   m.artist.name_ko if m.artist else None,
                        "group_id":         m.group_id,
                        "group_name_ko":    m.group.name_ko if m.group else None,
                        "confidence_score": m.confidence_score,
                    }
                    for m in rows
                ],
                "total": total,
            }

    except Exception as exc:
        logger.exception("엔티티 매핑 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.delete("/entity-mappings/{mapping_id}", status_code=200)
def delete_entity_mapping(mapping_id: int) -> dict:
    """엔티티 매핑 삭제 (관리자용).

    삭제 후 해당 기사에 남은 매핑이 없으면 sentinel(EVENT, confidence=0.0)을 삽입.
    → 파이프라인이 '매핑 없는 기사'로 인식해 재추출·재생성하는 것을 방지.
    """
    try:
        from sqlalchemy import select
        from core.db import get_db
        from database.models import EntityMapping, EntityType

        with get_db() as session:
            mapping = session.get(EntityMapping, mapping_id)
            if mapping is None:
                raise HTTPException(status_code=404, detail="매핑을 찾을 수 없습니다.")

            article_id = mapping.article_id
            session.delete(mapping)
            session.flush()  # DELETE 반영 후 remaining 확인

            # 남은 매핑 확인 (sentinel 포함)
            remaining = session.scalars(
                select(EntityMapping).where(EntityMapping.article_id == article_id).limit(1)
            ).first()

            if remaining is None:
                # 파이프라인 재추출 방지용 sentinel 삽입
                session.add(EntityMapping(
                    article_id=article_id,
                    entity_type=EntityType.EVENT,
                    confidence_score=0.0,
                ))
                logger.info("매핑 삭제 후 sentinel 삽입 | article_id=%d", article_id)

            session.commit()
            return {"deleted": mapping_id}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("엔티티 매핑 삭제 실패 id=%d: %s", mapping_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.post("/entity-mappings", status_code=201)
def create_entity_mapping(
    article_id:       int            = Body(..., embed=True),
    artist_id:        Optional[int]  = Body(None, embed=True),
    group_id:         Optional[int]  = Body(None, embed=True),
    confidence_score: float          = Body(1.0, embed=True),
) -> dict:
    """엔티티 매핑 수동 추가 (관리자용)."""
    try:
        from core.db import get_db
        from database.models import EntityMapping, EntityType
        from sqlalchemy import select

        if artist_id is None and group_id is None:
            raise HTTPException(status_code=400, detail="artist_id 또는 group_id 중 하나는 필수입니다.")

        entity_type = EntityType.ARTIST if artist_id else EntityType.GROUP

        with get_db() as session:
            # 중복 확인
            existing_stmt = select(EntityMapping).where(
                EntityMapping.article_id == article_id
            )
            if artist_id:
                existing_stmt = existing_stmt.where(EntityMapping.artist_id == artist_id)
            if group_id:
                existing_stmt = existing_stmt.where(EntityMapping.group_id == group_id)
            if session.execute(existing_stmt).scalar_one_or_none():
                raise HTTPException(status_code=409, detail="이미 존재하는 매핑입니다.")

            mapping = EntityMapping(
                article_id=article_id,
                entity_type=entity_type,
                artist_id=artist_id,
                group_id=group_id,
                confidence_score=min(max(confidence_score, 0.0), 1.0),
            )
            session.add(mapping)
            session.commit()
            return {"created": mapping.id, "article_id": article_id, "artist_id": artist_id, "group_id": group_id}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("엔티티 매핑 추가 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────
# 보강 데이터 초기화 (잘못된 Gemini 보강 데이터 리셋)
# ─────────────────────────────────────────────────────────────

_ENRICHED_GROUP_FIELDS = [
    "name_en", "debut_date", "label_ko", "label_en",
    "fandom_name_ko", "fandom_name_en", "gender", "bio_ko", "bio_en",
]
_ENRICHED_ARTIST_FIELDS = [
    "name_en", "stage_name_ko", "stage_name_en", "birth_date",
    "nationality_ko", "nationality_en", "mbti", "blood_type",
    "height_cm", "weight_kg", "gender", "bio_ko", "bio_en",
]


@public_router.post("/groups/{group_id}/reset-enrichment", status_code=200)
def reset_group_enrichment(
    group_id: int,
    fields: Optional[list[str]] = Body(None, embed=True,
        description="초기화할 필드 목록. 미입력 시 전체 초기화"),
) -> dict:
    """
    그룹의 Gemini 보강 데이터를 초기화합니다.
    enriched_at을 NULL로 리셋해 다음 보강 실행 시 재처리됩니다.
    """
    try:
        from core.db import get_db
        from database.models import Group

        with get_db() as session:
            group = session.get(Group, group_id)
            if group is None:
                raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")

            target_fields = fields if fields else _ENRICHED_GROUP_FIELDS
            cleared = []
            for f in target_fields:
                if f in _ENRICHED_GROUP_FIELDS and hasattr(group, f):
                    setattr(group, f, None)
                    cleared.append(f)

            group.enriched_at = None  # 다음 보강 실행 시 재처리
            session.commit()
            return {"group_id": group_id, "cleared_fields": cleared, "enriched_at_reset": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("그룹 보강 초기화 실패 id=%d: %s", group_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@public_router.post("/artists/{artist_id}/reset-enrichment", status_code=200)
def reset_artist_enrichment(
    artist_id: int,
    fields: Optional[list[str]] = Body(None, embed=True,
        description="초기화할 필드 목록. 미입력 시 전체 초기화"),
) -> dict:
    """
    아티스트의 Gemini 보강 데이터를 초기화합니다.
    enriched_at을 NULL로 리셋해 다음 보강 실행 시 재처리됩니다.
    """
    try:
        from core.db import get_db
        from database.models import Artist

        with get_db() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise HTTPException(status_code=404, detail="아티스트를 찾을 수 없습니다.")

            target_fields = fields if fields else _ENRICHED_ARTIST_FIELDS
            cleared = []
            for f in target_fields:
                if f in _ENRICHED_ARTIST_FIELDS and hasattr(artist, f):
                    setattr(artist, f, None)
                    cleared.append(f)

            artist.enriched_at = None
            session.commit()
            return {"artist_id": artist_id, "cleared_fields": cleared, "enriched_at_reset": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("아티스트 보강 초기화 실패 id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────
# 프로필 보강 (Gemini 기반 자동 데이터 수집)
# ─────────────────────────────────────────────────────────────

@public_router.post("/enrich-profiles")
def enrich_profiles(
    target:     str = Body("all",  embed=True, description="'all' | 'artists' | 'groups'"),
    batch_size: int = Body(10,     embed=True, description="한 번에 처리할 수"),
) -> dict:
    """
    Gemini 지식 기반으로 비어있는 아티스트/그룹 프로필을 자동 보강합니다.
    이미 값이 있는 필드는 덮어쓰지 않습니다.
    """
    try:
        from processor.profile_enricher import enrich_artists, enrich_groups

        artists_count = 0
        groups_count  = 0

        if target in ("all", "artists"):
            artists_count = enrich_artists(batch_size=batch_size)
        if target in ("all", "groups"):
            groups_count = enrich_groups(batch_size=batch_size)

        return {
            "enriched_artists": artists_count,
            "enriched_groups":  groups_count,
            "total":            artists_count + groups_count,
        }

    except Exception as exc:
        logger.exception("프로필 보강 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
