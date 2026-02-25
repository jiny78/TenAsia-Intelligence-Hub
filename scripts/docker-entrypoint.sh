#!/usr/bin/env bash
# =============================================================
# TenAsia Intelligence Hub — Backend Docker Entrypoint
#
# 역할:
#   1. PostgreSQL 준비 대기 (최대 60초)
#   2. Alembic 마이그레이션 실행 (upgrade head)
#   3. CMD 로 전달된 명령 실행 (uvicorn)
#
# 환경 변수:
#   DATABASE_URL   — PostgreSQL 접속 URL (필수)
#   SKIP_MIGRATE   — "true" 이면 마이그레이션 건너뜀 (테스트용)
# =============================================================

set -euo pipefail

log() { printf '\033[36m[entrypoint]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[entrypoint] ERROR:\033[0m %s\n' "$*" >&2; }

# ── 1. PostgreSQL 준비 대기 ───────────────────────────────────
if [[ -n "${DATABASE_URL:-}" ]]; then
    log "PostgreSQL 준비 대기 중 (최대 60초)..."

    python3 - <<'PYEOF'
import os, sys, time
import psycopg2

url = os.environ["DATABASE_URL"]

for attempt in range(1, 31):          # 2초 × 30회 = 최대 60초
    try:
        conn = psycopg2.connect(url)
        conn.close()
        print(f"[entrypoint] PostgreSQL 준비 완료 (시도 {attempt}회)", flush=True)
        sys.exit(0)
    except psycopg2.OperationalError as exc:
        print(f"[entrypoint] 대기 중 ({attempt}/30): {exc}", flush=True)
        time.sleep(2)

print("[entrypoint] PostgreSQL 60초 내 응답 없음 — 강제 종료", flush=True)
sys.exit(1)
PYEOF

else
    log "DATABASE_URL 미설정 → DB 대기 건너뜀 (개발 모드)"
fi

# ── 2. Alembic 마이그레이션 ───────────────────────────────────
if [[ "${SKIP_MIGRATE:-false}" == "true" ]]; then
    log "SKIP_MIGRATE=true → 마이그레이션 건너뜀"
else
    log "Alembic 마이그레이션 실행 (upgrade head)..."
    alembic upgrade head
    log "마이그레이션 완료."
fi

# ── 3. 서버 시작 ──────────────────────────────────────────────
log "서버 시작: $*"
exec "$@"
