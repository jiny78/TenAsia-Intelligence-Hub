"""
database/migrations/env.py — Alembic 실행 환경 설정

DB URL 로드 순서:
  1. DATABASE_URL 환경변수
  2. .env 파일 (python-dotenv)
  3. alembic.ini sqlalchemy.url (비어있으면 에러)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from logging.config import fileConfig

# 프로젝트 루트(/app)를 Python 경로에 추가
sys.path.insert(0, str(Path(__file__).parents[2]))

from alembic import context
from sqlalchemy import engine_from_config, pool

# .env 파일 자동 로드 (로컬 개발용)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────
# Alembic Config 객체
# ─────────────────────────────────────────────────────────────
config = context.config

# stdlib 로깅 설정 적용
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ─────────────────────────────────────────────────────────────
# DB URL 설정 (환경변수 우선)
# ─────────────────────────────────────────────────────────────

def get_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 환경변수가 설정되지 않았습니다.\n"
            ".env 파일 또는 환경변수를 확인해주세요."
        )
    # SQLAlchemy 2.x: postgres:// → postgresql://
    return url.replace("postgres://", "postgresql://", 1)


config.set_main_option("sqlalchemy.url", get_url())

# ─────────────────────────────────────────────────────────────
# 메타데이터 (autogenerate 용)
# ─────────────────────────────────────────────────────────────

# 모든 모델을 임포트해야 autogenerate 가 올바르게 작동합니다.
from database.base import Base        # noqa: E402
import database.models                # noqa: E402, F401  ← 모델 등록

target_metadata = Base.metadata

# ─────────────────────────────────────────────────────────────
# 마이그레이션 실행
# ─────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """오프라인 모드: SQL 스크립트만 생성 (실제 DB 연결 없음)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,           # 컬럼 타입 변경 감지
        compare_server_default=True, # 서버 기본값 변경 감지
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """온라인 모드: 실제 DB에 연결하여 마이그레이션 실행."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,     # 마이그레이션용 — 연결 풀 불필요
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # PostgreSQL 스키마 지정 (기본: public)
            # include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
