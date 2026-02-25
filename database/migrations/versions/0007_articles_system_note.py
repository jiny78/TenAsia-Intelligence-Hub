"""articles 테이블에 system_note 컬럼 추가

변경 요약:

  1. articles — system_note TEXT 컬럼 추가
       용도: AI 처리 중 발생한 모호함·불확실 사유를 기록합니다.
       MANUAL_REVIEW 전환 시 Gemini가 판단한 이유를 저장합니다.

       예시:
         "MANUAL_REVIEW 사유: '지수' 탐지 신뢰도 낮음(0.72/0.80); 동명이인 가능성: 블랙핑크 지수 vs 기타"

       설계 원칙:
         - NULL = 자동 처리 완료(사유 없음) 또는 미처리
         - 값 있음 = 검수자가 확인해야 할 사유
         - 검수 완료 후 NULL로 초기화하거나 그대로 보존 (운영 정책에 따라 결정)

  2. idx_articles_system_note_partial 부분 인덱스 추가
       WHERE system_note IS NOT NULL
       → 검수 큐 UI에서 미해결 노트가 있는 기사만 빠르게 조회

Revision ID: 0007
Revises: 0006
Create Date: 2026-02-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. system_note 컬럼 추가 ──────────────────────────────
    op.execute("""
        ALTER TABLE articles
            ADD COLUMN IF NOT EXISTS system_note TEXT
    """)

    # ── 2. 부분 인덱스: system_note 가 있는 기사만 ────────────
    #    검수 큐에서 "미해결 노트" 기사 필터링 시 사용
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_system_note_partial
            ON articles (created_at DESC)
            WHERE system_note IS NOT NULL
    """)


# ─────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────

def downgrade() -> None:

    # 부분 인덱스 삭제
    op.execute("DROP INDEX IF EXISTS idx_articles_system_note_partial")

    # 컬럼 삭제
    op.drop_column("articles", "system_note")
