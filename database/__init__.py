"""
database 패키지 — SQLAlchemy ORM 모델 및 Alembic 마이그레이션

구조:
    database/
    ├── __init__.py      ← 이 파일 (Base, 공통 임포트)
    ├── models.py        ← ORM 모델 (JobQueue, Article)
    └── migrations/      ← Alembic 마이그레이션
        ├── env.py
        ├── script.py.mako
        └── versions/
            └── 0001_initial.py

마이그레이션 명령어:
    # 마이그레이션 실행
    alembic upgrade head

    # 새 마이그레이션 자동 생성
    alembic revision --autogenerate -m "add column"

    # 되돌리기
    alembic downgrade -1
"""

from database.base import Base  # noqa: F401 (다른 모듈에서 Base import용)
