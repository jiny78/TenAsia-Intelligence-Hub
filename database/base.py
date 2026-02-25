"""database/base.py — SQLAlchemy 선언적 Base (순환 임포트 방지용 분리)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 Base 클래스."""
    pass
