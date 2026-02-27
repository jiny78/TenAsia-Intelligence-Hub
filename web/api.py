"""
web/api.py — FastAPI 내부 API 서버 (포트 8000)

App Runner 컨테이너 내부에서 uvicorn 으로 구동됩니다.
Streamlit(포트 8501) → http://localhost:8000 으로 호출합니다.

엔드포인트:
  GET    /health                      헬스체크 (DB 연결·Gemini API·디스크 용량)
  GET    /articles                    기사 목록 조회 (언어·번역 누락·처리 상태 필터)
  PATCH  /articles/{id}               기사 영문 번역 수동 수정 (title_en, summary_en)
  GET    /glossary                    Glossary 목록 (category·검색어 필터)
  POST   /glossary                    Glossary 용어 등록
  PUT    /glossary/{id}               Glossary 용어 수정
  DELETE /glossary/{id}               Glossary 용어 삭제
  GET    /artists                     아티스트 목록 (이름 검색)
  PATCH  /artists/{id}/priority       아티스트 번역 우선순위 변경
  GET    /reports/cost/today          오늘 Gemini 토큰 사용량 + 절감 비용 추정
  POST   /jobs                        작업 큐에 새 작업 추가
  GET    /jobs/{job_id}               작업 상세 조회
  GET    /jobs                        최근 작업 목록
  DELETE /jobs/{job_id}               작업 취소 (pending 만)
  GET    /jobs/stats                  상태별 통계
  POST   /trigger/ssm                 SSM SendCommand 로 EC2 스크래퍼 즉시 실행

  [Phase 5-B] Automation Monitor
  GET    /automation/summary          자율 처리 24h 통계 요약
  GET    /automation/feed             자율 결정 타임라인 (auto_resolution_logs)
  GET    /automation/conflicts        미해결 ConflictFlag 목록
  PATCH  /automation/conflicts/{id}   ConflictFlag 해결·기각
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from datetime import date, datetime
from typing import Any, Literal, Optional

import boto3
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from scraper.db import (
    cancel_job,
    create_job,
    get_job_by_id,
    get_queue_stats,
    get_recent_jobs,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="TenAsia Intelligence Hub — Internal API",
    version="1.0.0",
    docs_url="/docs",      # Swagger UI (개발용)
    redoc_url=None,
)

# Next.js 프록시(/api/*) 경유로 브라우저 요청이 들어오므로 origins=["*"]
# 직접 접근 시에도 내부 관리 도구용이므로 전체 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files (로컬 썸네일 서빙) ─────────────────────────
# 컨테이너 내 /app/static 또는 프로젝트 루트의 static/ 폴더를
# /static 엔드포인트로 서빙합니다.
# 예: http://localhost:8000/static/thumbnails/abc.jpg
_STATIC_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
os.makedirs(_STATIC_ROOT, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_ROOT), name="static")

# ── 공개 API 라우터 (소비자 사이트용) ──────────────────────────
from web.public_api import public_router  # noqa: E402
app.include_router(public_router)


# ── 요청/응답 스키마 ─────────────────────────────────────────

class CreateJobRequest(BaseModel):
    source_url:  str       = Field(..., description="스크래핑할 기사 URL")
    language:    str       = Field("kr", description="언어 코드 (kr / en)")
    platforms:   list[str] = Field(default_factory=list, description="배포 플랫폼 목록")
    priority:    int       = Field(5, ge=1, le=10, description="우선순위 (높을수록 먼저)")
    max_retries: int       = Field(3, ge=0, le=10)
    dry_run:     bool      = Field(
        False,
        description=(
            "True 면 HTTP 스크래핑·파싱은 수행하되 DB 에 저장하지 않음 (테스트 모드). "
            "결과는 [DRY RUN] 태그로 로그에 출력됩니다."
        ),
    )


class SsmTriggerRequest(BaseModel):
    job_id:    Optional[int] = Field(None, description="특정 job_id 지정 (없으면 루프 재시작)")
    comment:   str           = Field("", description="트리거 이유 (로그용)")


class ArticlePatchRequest(BaseModel):
    """기사 영문 번역 수동 수정 요청."""

    title_en:   Optional[str] = Field(None, description="수정할 영문 제목 (빈 문자열 → NULL 저장)")
    summary_en: Optional[str] = Field(None, description="수정할 영문 요약 (빈 문자열 → NULL 저장)")


class GlossaryCreateRequest(BaseModel):
    """Glossary 용어 등록 요청."""

    term_ko:     str           = Field(..., max_length=300, description="한국어 원어")
    term_en:     Optional[str] = Field(None, max_length=300, description="영어 공식 표기")
    category:    str           = Field(..., description="ARTIST / AGENCY / EVENT")
    description: Optional[str] = Field(None, description="추가 설명 (동명이인 구분 등)")

    @field_validator("category")
    @classmethod
    def _check_cat(cls, v: str) -> str:
        upper = v.upper()
        if upper not in {"ARTIST", "AGENCY", "EVENT"}:
            raise ValueError("category 는 ARTIST / AGENCY / EVENT 중 하나여야 합니다.")
        return upper


class GlossaryUpdateRequest(BaseModel):
    """Glossary 용어 수정 요청. None 인 필드는 변경하지 않습니다."""

    term_ko:     Optional[str] = Field(None, max_length=300)
    term_en:     Optional[str] = Field(None, max_length=300)
    category:    Optional[str] = Field(None)
    description: Optional[str] = Field(None)

    @field_validator("category")
    @classmethod
    def _check_cat(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        upper = v.upper()
        if upper not in {"ARTIST", "AGENCY", "EVENT"}:
            raise ValueError("category 는 ARTIST / AGENCY / EVENT 중 하나여야 합니다.")
        return upper


class ArtistPriorityRequest(BaseModel):
    """아티스트 번역 우선순위 업데이트 요청."""

    global_priority: Optional[int] = Field(
        None,
        description="번역 우선순위 (1=전체번역, 2=요약만, 3=번역제외, null=미분류)",
    )

    @field_validator("global_priority")
    @classmethod
    def _check_priority(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (1, 2, 3):
            raise ValueError("global_priority 는 1, 2, 3, 또는 null 이어야 합니다.")
        return v


class ScrapeRequest(BaseModel):
    """[Phase 5] 날짜 범위 스크래핑 요청."""

    start_date: str = Field(..., description="시작일 (YYYY-MM-DD)")
    end_date:   str = Field(..., description="종료일 (YYYY-MM-DD)")
    language:   str = Field("kr", description="언어 코드 (kr / en / jp)")
    max_pages:  int = Field(10, ge=1, le=200, description="수집 최대 페이지 수")
    dry_run:    bool = Field(False, description="드라이 런 모드 (DB 저장 없음)")


class ScrapeRSSRequest(BaseModel):
    """RSS 피드 즉시 수집 요청 (개별 페이지 fetch 없음, ~50배 빠름)."""

    language:   str            = Field("kr", description="언어 코드 (kr / en / jp)")
    start_date: Optional[str]  = Field(None, description="시작일 YYYY-MM-DD (없으면 RSS 전체)")
    end_date:   Optional[str]  = Field(None, description="종료일 YYYY-MM-DD")


class ConflictResolveRequest(BaseModel):
    """[Phase 5-B] ConflictFlag 해결·기각 요청."""

    action:      Literal["RESOLVED", "DISMISSED"] = Field(
        ..., description="처리 결과: RESOLVED(해결) 또는 DISMISSED(기각)"
    )
    resolved_by: str = Field(..., max_length=100, description="처리자 이름/ID")


# ── 스크래핑 태스크 상태 (모듈 레벨, 단일 인스턴스용) ────────────────
# { task_id: {"status": ..., "created_at": ..., "result": ..., "error": ...} }
_scrape_tasks: dict[str, dict[str, Any]] = {}
_MAX_TASK_HISTORY = 50  # 메모리 보호용 최대 보관 태스크 수


# ── 헬퍼 ─────────────────────────────────────────────────────

def _ssm_client():
    return boto3.client("ssm", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))


def _scraper_instance_id() -> str:
    instance_id = os.getenv("EC2_SCRAPER_INSTANCE_ID", "")
    if not instance_id:
        raise HTTPException(
            status_code=503,
            detail="EC2_SCRAPER_INSTANCE_ID 환경 변수가 설정되지 않았습니다.",
        )
    return instance_id


def _run_scrape_bg(task_id: str, req: ScrapeRequest) -> None:
    """
    [Phase 5] BackgroundTasks 에서 실행되는 날짜 범위 스크래핑 함수.

    scraper/engine.py 의 TenAsiaScraper.scrape_range() 를 직접 호출합니다.
    결과는 _scrape_tasks[task_id] 에 기록됩니다.
    """
    _scrape_tasks[task_id]["status"]     = "running"
    _scrape_tasks[task_id]["started_at"] = datetime.now().isoformat()

    try:
        from scraper.engine import TenAsiaScraper

        start_dt = datetime.strptime(req.start_date, "%Y-%m-%d")
        end_dt   = datetime.strptime(req.end_date,   "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )

        scraper = TenAsiaScraper()
        result  = scraper.scrape_range(
            start_date     = start_dt,
            end_date       = end_dt,
            language       = req.language,
            max_pages      = req.max_pages,
            dry_run        = req.dry_run,
        )

        _scrape_tasks[task_id].update({
            "status":       "completed",
            "completed_at": datetime.now().isoformat(),
            "result": {
                "total":         getattr(result, "total",   0),
                "success_count": len(getattr(result, "success",  [])),
                "failed_count":  len(getattr(result, "failed",   [])),
                "skipped_count": len(getattr(result, "skipped",  [])),
            },
        })
        logger.info(
            "스크래핑 완료 | task_id=%s total=%d success=%d failed=%d",
            task_id,
            getattr(result, "total", 0),
            len(getattr(result, "success", [])),
            len(getattr(result, "failed",  [])),
        )

    except Exception as exc:
        logger.exception("스크래핑 백그라운드 실패 | task_id=%s error=%s", task_id, exc)
        _scrape_tasks[task_id].update({
            "status":       "failed",
            "error":        f"{type(exc).__name__}: {exc}",
            "completed_at": datetime.now().isoformat(),
        })

    finally:
        # 오래된 태스크 정리
        if len(_scrape_tasks) > _MAX_TASK_HISTORY:
            oldest_key = min(
                _scrape_tasks,
                key=lambda k: _scrape_tasks[k].get("created_at", ""),
            )
            _scrape_tasks.pop(oldest_key, None)


def _get_db_status() -> dict[str, Any]:
    """
    [Phase 5] DB 에서 articles / artists 통계를 조회합니다.
    오류 발생 시 빈 dict 를 반환하여 /status 엔드포인트가 중단되지 않게 합니다.
    """
    try:
        from core.config import settings
        conn = psycopg2.connect(settings.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 기사 상태별 카운트
                cur.execute("""
                    SELECT process_status, COUNT(*) AS cnt
                    FROM   articles
                    GROUP  BY process_status
                """)
                art_rows = {r["process_status"]: int(r["cnt"]) for r in cur.fetchall()}

                # 오늘 수집된 기사 수 (한국 시간 기준)
                cur.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM   articles
                    WHERE  created_at >= CURRENT_DATE
                """)
                today_count = int(cur.fetchone()["cnt"])

                # 아티스트 통계
                cur.execute("""
                    SELECT COUNT(*)                                AS total,
                           SUM(CASE WHEN is_verified THEN 1 ELSE 0 END) AS verified
                    FROM   artists
                """)
                art_stat = cur.fetchone()

        finally:
            conn.close()

        return {
            "articles": {
                **art_rows,
                "total": sum(art_rows.values()),
                "today": today_count,
            },
            "artists": {
                "total":    int(art_stat["total"]    or 0),
                "verified": int(art_stat["verified"] or 0),
            },
        }

    except Exception as exc:
        logger.error("DB 통계 조회 실패: %s", exc)
        return {}


# ── 기사 헬퍼 ────────────────────────────────────────────────

def _article_to_dict(a: Any) -> dict[str, Any]:
    """Article ORM 객체를 JSON 직렬화 가능한 dict 로 변환합니다."""
    # S3 처리 썸네일 & 로컬 정적 URL: eager-load 된 images 에서 representative 이미지를 선택
    thumbnail_s3_url:    str | None = None
    thumbnail_local_url: str | None = None
    for img in (getattr(a, "images", None) or []):
        if img.is_representative and img.thumbnail_path:
            from core.config import settings
            thumbnail_s3_url    = f"{settings.s3_base_url}/{img.thumbnail_path}"
            # 로컬 Docker 환경에서 S3 없이 직접 접근 가능한 URL
            thumbnail_local_url = f"/static/{img.thumbnail_path}"
            break

    return {
        "id":                   a.id,
        "source_url":           a.source_url,
        "language":             a.language,
        "process_status":       a.process_status.value if a.process_status else None,
        "title_ko":             a.title_ko,
        "title_en":             a.title_en,
        "summary_ko":           a.summary_ko,
        "summary_en":           a.summary_en,
        "author":               a.author,
        "artist_name_ko":       a.artist_name_ko,
        "artist_name_en":       a.artist_name_en,
        "hashtags_ko":          list(a.hashtags_ko or []),
        "hashtags_en":          list(a.hashtags_en or []),
        "thumbnail_url":        a.thumbnail_url,
        "thumbnail_s3_url":     thumbnail_s3_url,
        "thumbnail_local_url":  thumbnail_local_url,
        "published_at":         a.published_at.isoformat() if a.published_at else None,
        "created_at":           a.created_at.isoformat()   if a.created_at   else None,
        "updated_at":           a.updated_at.isoformat()   if a.updated_at   else None,
    }


# ── 헬스체크 헬퍼 ────────────────────────────────────────────

def _check_db() -> tuple[str, bool]:
    """
    SQLAlchemy로 SELECT 1 쿼리를 실행하여 DB 연결 상태와 응답 시간을 측정합니다.

    Returns:
        (status_str, is_ok)
        - "ok (12ms)"          연결 성공
        - "not configured"     DATABASE_URL 미설정 (개발 환경)
        - "error: <message>"   연결 실패
    """
    try:
        from core.config import settings
        from core.db import _get_engine
        from sqlalchemy import text as sa_text

        if not settings.DATABASE_URL:
            return "not configured", True  # 개발 환경에서는 오류로 처리하지 않음

        t0 = time.monotonic()
        eng = _get_engine()
        with eng.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return f"ok ({elapsed_ms}ms)", True

    except Exception as exc:
        logger.warning("헬스체크 DB 오류: %s", exc)
        return f"error: {type(exc).__name__}: {exc}", False


def _check_gemini() -> tuple[str, Literal["ok", "degraded", "error"]]:
    """
    Gemini API의 list_models()를 호출하여 API 키 유효성과 할당량을 확인합니다.

    Returns:
        (status_str, level)  level: "ok" | "degraded" | "error"
        - ("ok", "ok")                     정상
        - ("quota_exceeded", "degraded")   429 할당량 초과 (키는 유효, 일시적 문제)
        - ("error: invalid api key", "error")  키 무효 / 인증 실패
        - ("error: <ExcName>", "error")    기타 장애
    """
    try:
        import google.generativeai as genai
        from core.config import settings

        if not settings.GEMINI_API_KEY:
            return "error: api key not set", "error"

        genai.configure(api_key=settings.GEMINI_API_KEY)
        # list_models()는 콘텐츠 생성 없이 API 키 유효성만 확인하는 가벼운 호출
        next(iter(genai.list_models()))
        return "ok", "ok"

    except Exception as exc:
        exc_name = type(exc).__name__
        exc_msg  = str(exc).lower()

        # 429 ResourceExhausted — 할당량 초과 (키는 유효하지만 일시적으로 제한됨)
        if any(k in exc_msg for k in ("quota", "resource_exhausted", "429", "rate limit")):
            logger.warning("Gemini 할당량 초과: %s", exc)
            return "quota_exceeded", "degraded"

        # 401/403 — API 키 무효 또는 권한 없음
        if any(k in exc_msg for k in ("api key", "invalid", "permission", "unauthenticated", "401", "403")):
            logger.error("Gemini API 키 오류: %s", exc)
            return "error: invalid api key", "error"

        logger.error("Gemini 헬스체크 실패 [%s]: %s", exc_name, exc)
        return f"error: {exc_name}", "error"


def _check_disk() -> tuple[str, bool]:
    """
    로그 파일이 저장되는 디스크의 잔여 용량을 확인합니다.
    잔여 용량이 10% 미만이면 경고를 반환합니다.

    Returns:
        (status_str, is_ok)
        - "85% free"           정상
        - "warning: 8% free"   10% 미만 경고
        - "error: <message>"   디스크 조회 실패
    """
    try:
        # 로그 디렉터리가 없으면 현재 디렉터리 기준으로 측정
        check_path = "logs" if os.path.exists("logs") else "."
        usage = shutil.disk_usage(check_path)
        free_pct = usage.free / usage.total * 100
        label = f"{free_pct:.0f}% free"

        if free_pct < 10:
            logger.warning("디스크 용량 부족: %.1f%% 남음 (경로: %s)", free_pct, check_path)
            return f"warning: {label}", False
        return label, True

    except Exception as exc:
        logger.warning("디스크 용량 확인 실패: %s", exc)
        return f"error: {type(exc).__name__}", False


# ── 엔드포인트 ───────────────────────────────────────────────

@app.get("/health")
def health() -> JSONResponse:
    """
    시스템 건강 상태를 체크합니다.

    체크 항목:
      - DB:     SQLAlchemy SELECT 1 응답 시간 + 연결 상태
      - Gemini: list_models() 로 API 키 유효성 + 할당량 초과 여부
      - Disk:   로그 디렉터리 디스크 잔여 용량 (10% 미만 시 경고)

    status 값:
      - healthy   모든 체크 통과
      - degraded  경고 수준 문제 (디스크 부족 또는 Gemini 할당량 초과)
      - unhealthy 치명적 오류 (DB 연결 불가, Gemini API 키 오류)

    HTTP 200: healthy / degraded
    HTTP 503: unhealthy
    """
    db_status,     db_ok         = _check_db()
    gemini_status, gemini_level  = _check_gemini()
    disk_status,   disk_ok       = _check_disk()

    # DB 다운만 503(unhealthy) — Gemini 오류는 degraded(200) 로 처리해
    # App Runner 헬스체크가 Gemini 설정과 무관하게 통과하도록 합니다.
    if not db_ok:
        overall   = "unhealthy"
        http_code = 503
    elif not disk_ok or gemini_level in ("error", "degraded"):
        overall   = "degraded"
        http_code = 200
    else:
        overall   = "healthy"
        http_code = 200

    logger.info(
        "헬스체크 | status=%s db=%s gemini=%s disk=%s",
        overall, db_status, gemini_status, disk_status,
    )
    return JSONResponse(
        status_code=http_code,
        content={
            "status": overall,
            "db":     db_status,
            "gemini": gemini_status,
            "disk":   disk_status,
        },
    )


@app.post("/jobs", status_code=201)
def submit_job(req: CreateJobRequest) -> dict[str, Any]:
    """작업 큐에 스크래핑 작업을 추가합니다."""
    params = {
        "source_url": req.source_url,
        "language":   req.language,
        "platforms":  req.platforms,
        "dry_run":    req.dry_run,
    }
    job_id = create_job(
        job_type="scrape",
        params=params,
        priority=req.priority,
        max_retries=req.max_retries,
    )
    logger.info(
        "작업 생성 | job_id=%d url=%s dry_run=%s",
        job_id, req.source_url, req.dry_run,
    )
    return {"job_id": job_id, "status": "pending", "dry_run": req.dry_run}


@app.get("/jobs/stats")
def queue_stats() -> dict[str, int]:
    """상태별 작업 수를 반환합니다."""
    return get_queue_stats()


@app.get("/jobs")
def list_jobs(limit: int = 20) -> list[dict]:
    """최근 작업 목록을 반환합니다."""
    return get_recent_jobs(limit=limit)


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    """특정 작업의 상세 정보를 반환합니다."""
    job = get_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job_id={job_id} 없음")
    return job


@app.get("/articles")
def list_articles(
    translation_pending: bool = Query(
        False,
        description="True 이면 title_en 이 비어 있는 기사(번역 누락)만 반환합니다.",
    ),
    process_status: Optional[str] = Query(
        None,
        description="처리 상태 필터 (PENDING / SCRAPED / PROCESSED / MANUAL_REVIEW / ERROR)",
    ),
    limit:  int = Query(30, ge=1, le=200),
    offset: int = Query(0,  ge=0),
) -> list[dict[str, Any]]:
    """
    기사 목록을 조회합니다.

    - translation_pending=true : title_en 이 NULL 또는 빈 문자열인 기사만 반환
    - process_status           : 처리 상태별 필터 (대소문자 구분 없음)
    """
    try:
        from core.db import get_db
        from database.models import Article
        from sqlalchemy import or_, select

        with get_db() as session:
            from sqlalchemy.orm import selectinload
            q = (
                select(Article)
                .options(selectinload(Article.images))
                .order_by(Article.created_at.desc())
            )

            if translation_pending:
                q = q.where(
                    or_(Article.title_en.is_(None), Article.title_en == "")
                )

            if process_status:
                q = q.where(
                    Article.process_status == process_status.upper()
                )

            rows = session.execute(q.limit(limit).offset(offset)).scalars().all()
            return [_article_to_dict(a) for a in rows]

    except Exception as exc:
        logger.exception("기사 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=f"DB 조회 오류: {exc}") from exc


@app.patch("/articles/{article_id}")
def patch_article(article_id: int, req: ArticlePatchRequest) -> dict[str, Any]:
    """
    기사의 영문 번역(title_en, summary_en)을 수동으로 수정합니다.

    - 빈 문자열로 전송하면 NULL 로 저장합니다.
    - 필드를 요청에 포함하지 않으면 해당 필드는 변경하지 않습니다.
    """
    if req.title_en is None and req.summary_en is None:
        raise HTTPException(
            status_code=422,
            detail="수정할 필드가 없습니다. title_en 또는 summary_en 을 포함하세요.",
        )

    try:
        from core.db import get_db
        from database.models import Article

        with get_db() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise HTTPException(status_code=404, detail=f"article_id={article_id} 없음")

            updated_fields: dict[str, Any] = {}

            if req.title_en is not None:
                article.title_en = req.title_en.strip() or None
                updated_fields["title_en"] = article.title_en

            if req.summary_en is not None:
                article.summary_en = req.summary_en.strip() or None
                updated_fields["summary_en"] = article.summary_en

            # get_db() 컨텍스트 매니저가 자동 커밋

        logger.info(
            "기사 번역 수동 수정 완료 | article_id=%d fields=%s",
            article_id, list(updated_fields.keys()),
        )
        return {"article_id": article_id, "updated": True, **updated_fields}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("기사 수정 실패 | article_id=%d: %s", article_id, exc)
        raise HTTPException(status_code=500, detail=f"DB 저장 오류: {exc}") from exc


# ── Glossary CRUD ────────────────────────────────────────────

@app.get("/glossary")
def list_glossary(
    category: Optional[str] = Query(None, description="ARTIST / AGENCY / EVENT"),
    q:        Optional[str] = Query(None, description="term_ko 부분 일치 검색"),
    limit:    int            = Query(200, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Glossary 용어 목록을 반환합니다."""
    try:
        from core.db import get_db
        from database.models import Glossary
        from sqlalchemy import select

        with get_db() as session:
            stmt = select(Glossary).order_by(Glossary.category, Glossary.term_ko)
            if category:
                stmt = stmt.where(Glossary.category == category.upper())
            if q:
                stmt = stmt.where(Glossary.term_ko.ilike(f"%{q}%"))
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return [
                {
                    "id":          g.id,
                    "term_ko":     g.term_ko,
                    "term_en":     g.term_en,
                    "category":    g.category.value if g.category else None,
                    "description": g.description,
                    "created_at":  g.created_at.isoformat() if g.created_at else None,
                    "updated_at":  g.updated_at.isoformat() if g.updated_at else None,
                }
                for g in rows
            ]
    except Exception as exc:
        logger.exception("Glossary 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/glossary", status_code=201)
def create_glossary(req: GlossaryCreateRequest) -> dict[str, Any]:
    """Glossary 용어를 등록합니다. (term_ko, category) 쌍은 유니크해야 합니다."""
    try:
        from core.db import get_db
        from database.models import Glossary, GlossaryCategory

        with get_db() as session:
            entry = Glossary(
                term_ko     = req.term_ko.strip(),
                term_en     = req.term_en.strip() if req.term_en else None,
                category    = GlossaryCategory(req.category),
                description = req.description,
            )
            session.add(entry)
            session.flush()   # PK 획득
            new_id = entry.id

        logger.info("Glossary 등록 | id=%d term_ko=%s", new_id, req.term_ko)
        return {"id": new_id, "created": True}

    except Exception as exc:
        if "uq_glossary_term_category" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"이미 등록된 용어입니다: ({req.term_ko}, {req.category})",
            ) from exc
        logger.exception("Glossary 등록 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/glossary/{glossary_id}")
def update_glossary(glossary_id: int, req: GlossaryUpdateRequest) -> dict[str, Any]:
    """Glossary 용어를 수정합니다. None 인 필드는 변경하지 않습니다."""
    try:
        from core.db import get_db
        from database.models import Glossary, GlossaryCategory

        with get_db() as session:
            entry = session.get(Glossary, glossary_id)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"id={glossary_id} 없음")

            if req.term_ko     is not None: entry.term_ko     = req.term_ko.strip()
            if req.term_en     is not None: entry.term_en     = req.term_en.strip() or None
            if req.category    is not None: entry.category    = GlossaryCategory(req.category)
            if req.description is not None: entry.description = req.description or None

        logger.info("Glossary 수정 | id=%d", glossary_id)
        return {"id": glossary_id, "updated": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Glossary 수정 실패 | id=%d: %s", glossary_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/glossary/{glossary_id}", status_code=200)
def delete_glossary(glossary_id: int) -> dict[str, Any]:
    """Glossary 용어를 삭제합니다."""
    try:
        from core.db import get_db
        from database.models import Glossary

        with get_db() as session:
            entry = session.get(Glossary, glossary_id)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"id={glossary_id} 없음")
            session.delete(entry)

        logger.info("Glossary 삭제 | id=%d", glossary_id)
        return {"id": glossary_id, "deleted": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Glossary 삭제 실패 | id=%d: %s", glossary_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── 아티스트 관리 ─────────────────────────────────────────────

@app.get("/artists")
def list_artists(
    q:      Optional[str] = Query(None, description="name_ko 부분 일치 검색"),
    limit:  int           = Query(100, ge=1, le=500),
    offset: int           = Query(0,   ge=0),
) -> list[dict[str, Any]]:
    """아티스트 목록을 반환합니다."""
    try:
        from core.db import get_db
        from database.models import Artist
        from sqlalchemy import select

        with get_db() as session:
            stmt = select(Artist).order_by(Artist.name_ko)
            if q:
                stmt = stmt.where(Artist.name_ko.ilike(f"%{q}%"))
            rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
            return [
                {
                    "id":              a.id,
                    "name_ko":         a.name_ko,
                    "name_en":         a.name_en,
                    "agency":          a.agency,
                    "global_priority": a.global_priority,
                    "is_verified":     a.is_verified,
                    "debut_date":      a.debut_date.isoformat() if a.debut_date else None,
                }
                for a in rows
            ]
    except Exception as exc:
        logger.exception("아티스트 목록 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.patch("/artists/{artist_id}/priority")
def update_artist_priority(artist_id: int, req: ArtistPriorityRequest) -> dict[str, Any]:
    """아티스트의 글로벌 번역 우선순위를 변경합니다."""
    try:
        from core.db import get_db
        from database.models import Artist

        with get_db() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise HTTPException(status_code=404, detail=f"artist_id={artist_id} 없음")
            artist.global_priority = req.global_priority

        logger.info("아티스트 우선순위 변경 | artist_id=%d priority=%s", artist_id, req.global_priority)
        return {"artist_id": artist_id, "global_priority": req.global_priority, "updated": True}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("아티스트 우선순위 변경 실패 | artist_id=%d: %s", artist_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── 비용 리포트 ───────────────────────────────────────────────

# Gemini 2.0 Flash 단가 (2025년 기준, USD / 1M tokens)
_INPUT_PRICE_PER_M  = 0.075
_OUTPUT_PRICE_PER_M = 0.300


@app.get("/reports/cost/today")
def cost_report_today() -> dict[str, Any]:
    """
    오늘(UTC 00:00 기준) Gemini API 사용 현황과 Priority 로직에 의한 절감 비용을 반환합니다.

    비용 기준 (gemini-2.0-flash, 2025):
      입력: $0.075 / 1M tokens
      출력: $0.300 / 1M tokens

    절감액 추정 방법:
      - global_priority=false 기사 (번역 스킵) 수 × 기사당 평균 토큰 × 블렌드 단가
      - 블렌드 단가: 입력 70% + 출력 30% 비율 추정
    """
    try:
        from core.db import get_db
        from sqlalchemy import text as sa_text

        with get_db() as session:
            # ── 오늘 AI_PROCESS 로그 집계 ─────────────────────────
            tok = session.execute(sa_text("""
                SELECT
                    COUNT(*)                                                    AS api_calls,
                    COALESCE(SUM((details->>'prompt_tokens')::bigint),     0)  AS prompt_tokens,
                    COALESCE(SUM((details->>'completion_tokens')::bigint), 0)  AS completion_tokens,
                    COALESCE(SUM((details->>'total_tokens')::bigint),      0)  AS total_tokens,
                    COALESCE(AVG((details->>'response_time_ms')::numeric), 0)  AS avg_latency_ms
                FROM system_logs
                WHERE category = 'AI_PROCESS'
                  AND created_at >= CURRENT_DATE
                  AND details ? 'total_tokens'
            """)).fetchone()

            api_calls         = int(tok.api_calls         or 0)
            prompt_tokens     = int(tok.prompt_tokens     or 0)
            completion_tokens = int(tok.completion_tokens or 0)
            total_tokens      = int(tok.total_tokens      or 0)
            avg_latency_ms    = round(float(tok.avg_latency_ms or 0), 1)

            # ── 오늘 기사 Priority 현황 ───────────────────────────
            prio = session.execute(sa_text("""
                SELECT
                    CASE WHEN global_priority = true THEN 'translated' ELSE 'skipped' END AS bucket,
                    COUNT(*) AS cnt
                FROM articles
                WHERE created_at >= CURRENT_DATE
                  AND process_status IN ('PROCESSED', 'MANUAL_REVIEW', 'SCRAPED')
                GROUP BY bucket
            """)).fetchall()
            prio_map          = {r.bucket: int(r.cnt) for r in prio}
            translated_count  = prio_map.get("translated", 0)
            skipped_count     = prio_map.get("skipped",    0)

        # ── 실제 비용 계산 ─────────────────────────────────────
        actual_input_cost  = prompt_tokens    / 1_000_000 * _INPUT_PRICE_PER_M
        actual_output_cost = completion_tokens / 1_000_000 * _OUTPUT_PRICE_PER_M
        actual_total_cost  = actual_input_cost + actual_output_cost

        # ── 절감 비용 추정 ──────────────────────────────────────
        avg_tokens_per_call = (total_tokens / api_calls) if api_calls > 0 else 2_000
        blended_per_m       = _INPUT_PRICE_PER_M * 0.7 + _OUTPUT_PRICE_PER_M * 0.3
        saved_tokens_est    = int(skipped_count * avg_tokens_per_call)
        saved_cost_est      = saved_tokens_est / 1_000_000 * blended_per_m

        return {
            "date":                  str(date.today()),
            "pricing": {
                "model":             "gemini-2.0-flash",
                "input_usd_per_m":   _INPUT_PRICE_PER_M,
                "output_usd_per_m":  _OUTPUT_PRICE_PER_M,
            },
            "usage": {
                "api_calls":         api_calls,
                "prompt_tokens":     prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens":      total_tokens,
                "avg_latency_ms":    avg_latency_ms,
            },
            "cost": {
                "actual_input_usd":  round(actual_input_cost,  6),
                "actual_output_usd": round(actual_output_cost, 6),
                "actual_total_usd":  round(actual_total_cost,  6),
            },
            "savings": {
                "translated_articles": translated_count,
                "skipped_articles":    skipped_count,
                "avg_tokens_per_call": round(avg_tokens_per_call, 1),
                "saved_tokens_est":    saved_tokens_est,
                "saved_cost_usd_est":  round(saved_cost_est, 6),
                "total_if_no_priority_usd": round(actual_total_cost + saved_cost_est, 6),
            },
        }

    except Exception as exc:
        logger.exception("비용 리포트 조회 실패: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int) -> dict[str, Any]:
    """pending 상태인 작업을 취소합니다."""
    cancelled = cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=f"job_id={job_id} 취소 실패 (이미 실행 중이거나 존재하지 않음)",
        )
    return {"job_id": job_id, "status": "cancelled"}


@app.post("/trigger/ssm")
def trigger_ssm(req: SsmTriggerRequest) -> dict[str, Any]:
    """
    SSM SendCommand 로 EC2 스크래퍼를 즉시 실행합니다.

    - job_id 지정 시: python -m scraper.worker --job-id <id> 실행
    - job_id 없을 때: systemctl restart tih-scraper (루프 재시작)
    """
    instance_id = _scraper_instance_id()
    ssm = _ssm_client()

    if req.job_id is not None:
        command = f"cd /opt/tih && python -m scraper.worker --job-id {req.job_id}"
    else:
        command = "systemctl restart tih-scraper"

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            Comment=req.comment or "TIH App Runner trigger",
            TimeoutSeconds=60,
        )
        command_id = response["Command"]["CommandId"]
        logger.info("SSM SendCommand 전송 | command_id=%s job_id=%s", command_id, req.job_id)
        return {
            "command_id":  command_id,
            "instance_id": instance_id,
            "command":     command,
        }
    except Exception as exc:
        logger.exception("SSM SendCommand 실패: %s", exc)
        raise HTTPException(status_code=502, detail=f"SSM 오류: {exc}") from exc


@app.get("/trigger/ssm/{command_id}")
def get_ssm_result(command_id: str) -> dict[str, Any]:
    """SSM 명령 실행 결과를 조회합니다."""
    instance_id = _scraper_instance_id()
    ssm = _ssm_client()

    try:
        resp = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
        )
        return {
            "command_id":      command_id,
            "status":          resp["Status"],
            "status_details":  resp["StatusDetails"],
            "stdout":          resp.get("StandardOutputContent", ""),
            "stderr":          resp.get("StandardErrorContent", ""),
        }
    except ssm.exceptions.InvocationDoesNotExist:
        raise HTTPException(status_code=404, detail=f"command_id={command_id} 없음")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SSM 조회 오류: {exc}") from exc


@app.post("/scrape", status_code=202)
def start_scrape(req: ScrapeRequest) -> dict[str, Any]:
    """
    날짜 범위 스크래핑을 워커 큐에 추가합니다.

    즉시 job_id 를 반환하며, 실제 스크래핑은 별도 워커 서비스에서 진행됩니다.
    GET /jobs/{job_id} 로 진행 상황을 조회하세요.
    """
    job_id = create_job(
        "scrape_range",
        params={
            "start_date": req.start_date,
            "end_date":   req.end_date,
            "language":   req.language,
            "max_pages":  req.max_pages,
            "dry_run":    req.dry_run,
        },
    )
    logger.info(
        "스크래핑 큐 추가 | job_id=%d start=%s end=%s lang=%s dry_run=%s",
        job_id, req.start_date, req.end_date, req.language, req.dry_run,
    )
    return {
        "task_id":  str(job_id),
        "status":   "pending",
        "message":  "스크래핑 작업이 워커 큐에 추가되었습니다.",
        "poll_url": f"/jobs/{job_id}",
    }


@app.post("/scrape/rss", status_code=202)
def start_scrape_rss(req: ScrapeRSSRequest) -> dict[str, Any]:
    """
    RSS 피드 1회 요청으로 최신 기사를 즉시 저장합니다.
    개별 페이지 fetch 없이 RSS 메타데이터만 저장 (기존 대비 ~50배 빠름).

    날짜 범위 없이 호출하면 RSS 피드 전체 (최신 ~50개)를 수집합니다.
    """
    params: dict[str, Any] = {"language": req.language}
    if req.start_date:
        params["start_date"] = req.start_date
    if req.end_date:
        params["end_date"] = req.end_date

    job_id = create_job("scrape_rss", params, priority=8)
    logger.info("scrape_rss 큐 추가 | job_id=%d lang=%s", job_id, req.language)
    return {
        "task_id":  str(job_id),
        "status":   "pending",
        "message":  "RSS 수집 작업이 워커 큐에 추가되었습니다.",
        "poll_url": f"/jobs/{job_id}",
    }


@app.get("/scrape/{task_id}")
def get_scrape_task(task_id: str) -> dict[str, Any]:
    """스크래핑 태스크의 현재 상태를 반환합니다."""
    task = _scrape_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task_id={task_id} 없음")
    return {"task_id": task_id, **task}


@app.get("/status")
def get_status() -> dict[str, Any]:
    """
    시스템 현황을 반환합니다.

    - DB 기사·아티스트 통계
    - 작업 큐 상태별 카운트
    - 현재 실행 중인 스크래핑 태스크 목록
    """
    from scraper.db import get_queue_stats  # 이미 import 되어 있지만 명시적으로 유지

    db_stats    = _get_db_status()
    queue_stats = get_queue_stats()

    running_tasks = [
        {"task_id": tid, **info}
        for tid, info in _scrape_tasks.items()
        if info.get("status") == "running"
    ]

    return {
        "db":           db_stats,
        "queue":        queue_stats,
        "scrape_tasks": {
            "running": running_tasks,
            "total":   len(_scrape_tasks),
        },
    }


# ── [Phase 5-B] Automation Monitor ──────────────────────────────────────────


@app.get("/automation/summary")
def get_automation_summary() -> dict[str, Any]:
    """
    [Phase 5-B] 자율 처리 24h 통계 요약.

    auto_resolution_logs + conflict_flags 를 집계하여
    Automation Status Card 에 표시할 지표를 반환합니다.
    """
    try:
        from core.config import settings
        conn = psycopg2.connect(settings.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                # 24h 자율 결정 수 (type 별)
                cur.execute("""
                    SELECT resolution_type, COUNT(*) AS cnt
                    FROM   auto_resolution_logs
                    WHERE  created_at >= NOW() - INTERVAL '24 hours'
                    GROUP  BY resolution_type
                """)
                type_counts = {r["resolution_type"]: int(r["cnt"]) for r in cur.fetchall()}

                # 24h ConflictFlag 처리 수
                cur.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM   conflict_flags
                    WHERE  resolved_at >= NOW() - INTERVAL '24 hours'
                      AND  status IN ('RESOLVED', 'DISMISSED')
                """)
                resolved_24h = int(cur.fetchone()["cnt"])

                # 현재 미해결 ConflictFlag 수
                cur.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM   conflict_flags
                    WHERE  status = 'OPEN'
                """)
                open_conflicts = int(cur.fetchone()["cnt"])

                # 24h 평균 신뢰도
                cur.execute("""
                    SELECT ROUND(AVG(source_reliability)::numeric, 4) AS avg_reliability
                    FROM   auto_resolution_logs
                    WHERE  created_at >= NOW() - INTERVAL '24 hours'
                """)
                avg_row = cur.fetchone()
                avg_reliability = float(avg_row["avg_reliability"] or 0.0)

        finally:
            conn.close()

        total_24h = sum(type_counts.values())
        return {
            "period":                 "24h",
            "total_decisions":        total_24h,
            "fill_count":             type_counts.get("FILL",      0),
            "reconcile_count":        type_counts.get("RECONCILE", 0),
            "enroll_count":           type_counts.get("ENROLL",    0),
            "conflicts_resolved_24h": resolved_24h,
            "open_conflicts":         open_conflicts,
            "avg_reliability":        avg_reliability,
        }

    except Exception as exc:
        logger.error("[Phase5B] /automation/summary 오류: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/automation/feed")
def get_automation_feed(
    limit:           int          = Query(50, ge=1, le=200),
    offset:          int          = Query(0,  ge=0),
    resolution_type: Optional[str] = Query(None, description="FILL / RECONCILE / ENROLL"),
) -> list[dict[str, Any]]:
    """
    [Phase 5-B] 자율 결정 타임라인.

    auto_resolution_logs 를 최신순으로 반환합니다.
    기사 제목(title_ko)을 JOIN 하여 컨텍스트를 함께 제공합니다.
    """
    try:
        from core.config import settings
        conn = psycopg2.connect(settings.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # [보안] f-string 금지 — resolution_type 필터 유무에 따라
                # 정적 쿼리 2개로 분기하여 SQL Injection 원천 차단
                _FEED_SELECT = """
                    SELECT
                        arl.id,
                        arl.article_id,
                        a.title_ko           AS article_title_ko,
                        arl.entity_type,
                        arl.entity_id,
                        arl.field_name,
                        arl.old_value_json,
                        arl.new_value_json,
                        arl.resolution_type,
                        arl.gemini_reasoning,
                        arl.gemini_confidence,
                        arl.source_reliability,
                        arl.created_at
                    FROM  auto_resolution_logs arl
                    LEFT  JOIN articles a ON a.id = arl.article_id
                """

                if resolution_type:
                    rt = resolution_type.upper()
                    if rt not in ("FILL", "RECONCILE", "ENROLL"):
                        raise HTTPException(
                            status_code=422,
                            detail="resolution_type 은 FILL / RECONCILE / ENROLL 중 하나여야 합니다.",
                        )
                    cur.execute(
                        _FEED_SELECT
                        + "WHERE arl.resolution_type = %s "
                        + "ORDER BY arl.created_at DESC LIMIT %s OFFSET %s",
                        (rt, limit, offset),
                    )
                else:
                    cur.execute(
                        _FEED_SELECT
                        + "ORDER BY arl.created_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )

                rows = cur.fetchall()
        finally:
            conn.close()

        result = []
        for r in rows:
            result.append({
                "id":                 r["id"],
                "article_id":         r["article_id"],
                "article_title_ko":   r["article_title_ko"],
                "entity_type":        r["entity_type"],
                "entity_id":          r["entity_id"],
                "field_name":         r["field_name"],
                "old_value":          r["old_value_json"],
                "new_value":          r["new_value_json"],
                "resolution_type":    r["resolution_type"],
                "gemini_reasoning":   r["gemini_reasoning"],
                "gemini_confidence":  r["gemini_confidence"],
                "source_reliability": r["source_reliability"],
                "created_at":         r["created_at"].isoformat() if r["created_at"] else None,
            })
        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Phase5B] /automation/feed 오류: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/automation/conflicts")
def get_automation_conflicts(
    status: str = Query("OPEN", description="OPEN / RESOLVED / DISMISSED"),
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0,  ge=0),
) -> list[dict[str, Any]]:
    """
    [Phase 5-B] ConflictFlag 목록.

    운영자가 검토해야 할 미해결 모순(OPEN) 또는 처리된 이력을 반환합니다.
    충돌 심각도(conflict_score) 내림차순으로 정렬합니다.
    """
    status_upper = status.upper()
    if status_upper not in ("OPEN", "RESOLVED", "DISMISSED"):
        raise HTTPException(
            status_code=422,
            detail="status 는 OPEN / RESOLVED / DISMISSED 중 하나여야 합니다.",
        )

    try:
        from core.config import settings
        conn = psycopg2.connect(settings.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        cf.id,
                        cf.article_id,
                        a.title_ko              AS article_title_ko,
                        cf.entity_type,
                        cf.entity_id,
                        cf.field_name,
                        cf.existing_value_json,
                        cf.conflicting_value_json,
                        cf.conflict_reason,
                        cf.conflict_score,
                        cf.status,
                        cf.resolved_by,
                        cf.resolved_at,
                        cf.created_at
                    FROM  conflict_flags cf
                    LEFT  JOIN articles a ON a.id = cf.article_id
                    WHERE cf.status = %s
                    ORDER BY cf.conflict_score DESC, cf.created_at DESC
                    LIMIT  %s OFFSET %s
                """, (status_upper, limit, offset))
                rows = cur.fetchall()
        finally:
            conn.close()

        result = []
        for r in rows:
            result.append({
                "id":                  r["id"],
                "article_id":          r["article_id"],
                "article_title_ko":    r["article_title_ko"],
                "entity_type":         r["entity_type"],
                "entity_id":           r["entity_id"],
                "field_name":          r["field_name"],
                "existing_value":      r["existing_value_json"],
                "conflicting_value":   r["conflicting_value_json"],
                "conflict_reason":     r["conflict_reason"],
                "conflict_score":      r["conflict_score"],
                "status":              r["status"],
                "resolved_by":         r["resolved_by"],
                "resolved_at":         r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "created_at":          r["created_at"].isoformat() if r["created_at"] else None,
            })
        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Phase5B] /automation/conflicts 오류: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.patch("/automation/conflicts/{conflict_id}")
def resolve_conflict(
    conflict_id: int,
    body: ConflictResolveRequest,
) -> dict[str, Any]:
    """
    [Phase 5-B] ConflictFlag 해결·기각.

    OPEN 상태인 conflict_flags 레코드를 RESOLVED 또는 DISMISSED 로 전환합니다.
    이미 처리된 건은 409 Conflict 를 반환합니다.
    """
    try:
        from core.config import settings
        conn = psycopg2.connect(settings.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 존재·상태 확인
                cur.execute(
                    "SELECT id, status FROM conflict_flags WHERE id = %s",
                    (conflict_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"conflict_id={conflict_id} 없음",
                    )
                if row["status"] != "OPEN":
                    raise HTTPException(
                        status_code=409,
                        detail=f"이미 처리된 ConflictFlag 입니다 (status={row['status']})",
                    )

                # 상태 업데이트
                cur.execute("""
                    UPDATE conflict_flags
                    SET    status      = %s,
                           resolved_by = %s,
                           resolved_at = NOW()
                    WHERE  id = %s
                    RETURNING id, status, resolved_by, resolved_at
                """, (body.action, body.resolved_by, conflict_id))
                updated = cur.fetchone()
                conn.commit()

        finally:
            conn.close()

        return {
            "id":          updated["id"],
            "status":      updated["status"],
            "resolved_by": updated["resolved_by"],
            "resolved_at": updated["resolved_at"].isoformat() if updated["resolved_at"] else None,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Phase5B] /automation/conflicts/%d 오류: %s", conflict_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
