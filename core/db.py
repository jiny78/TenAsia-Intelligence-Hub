"""
core/db.py — SQLAlchemy 데이터베이스 연결 관리

세션 사용법:
    # 컨텍스트 매니저 (권장)
    from core.db import get_db
    with get_db() as db:
        db.add(article)

    # FastAPI Dependency Injection
    from core.db import get_db_dep
    def route(db: Session = Depends(get_db_dep)):
        ...

    # 일회성 조회 (읽기 전용)
    from core.db import engine
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))

연결 풀 설정:
    pool_size=5        동시 연결 수 (기본)
    max_overflow=10    풀 초과 시 추가 허용 연결
    pool_pre_ping=True 연결 유효성 사전 확인 (Serverless DB 재연결)
    pool_recycle=1800  30분 후 연결 재생성 (RDS 유휴 타임아웃 대응)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 엔진 생성 (싱글턴 — 앱 프로세스당 1개)
# ─────────────────────────────────────────────────────────────

def _make_engine():
    from core.config import settings

    echo = settings.ENVIRONMENT == "development"   # 개발 시 SQL 로그 출력

    eng = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,       # SELECT 1 로 연결 유효성 확인
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,        # 30분 후 연결 재생성
        echo=echo,
        connect_args={
            "connect_timeout": 10,
            "application_name": "tih-app",
        },
    )

    # 연결 이벤트: 타임존 고정
    @event.listens_for(eng, "connect")
    def _set_timezone(dbapi_conn, connection_record):
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")

    return eng


# 모듈 임포트 시점에 바로 생성하지 않고, 첫 사용 시 생성
_engine = None
_SessionLocal = None


def _get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine()
        _SessionLocal = sessionmaker(
            bind=_engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,   # 커밋 후 객체 재조회 방지
        )
        logger.info("SQLAlchemy 엔진 초기화 완료")
    return _engine


@property
def engine():
    return _get_engine()


# ─────────────────────────────────────────────────────────────
# 세션 컨텍스트 매니저
# ─────────────────────────────────────────────────────────────

@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    SQLAlchemy 세션 컨텍스트 매니저.

    성공 시 커밋, 예외 시 롤백, 항상 닫음.

    Usage:
        with get_db() as db:
            db.add(SomeModel(field="value"))
        # ← 자동 커밋
    """
    _get_engine()   # 엔진 초기화 보장
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_dep() -> Generator[Session, None, None]:
    """
    FastAPI Dependency Injection 용 세션 제너레이터.

    Usage:
        from fastapi import Depends
        def endpoint(db: Session = Depends(get_db_dep)):
            ...
    """
    _get_engine()
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────────────────────

def ping_db() -> bool:
    """DB 연결 가능 여부를 확인합니다. True 반환 시 정상."""
    try:
        eng = _get_engine()
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("DB 연결 실패: %s", exc)
        return False
