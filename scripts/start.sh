#!/bin/bash
# scripts/start.sh — App Runner 컨테이너 멀티 프로세스 진입점
#
# 프로세스 구성:
#   1. uvicorn  → web/api.py (포트 8000, FastAPI 내부 API)
#   2. streamlit → web/app.py (포트 8501, 외부 공개 UI)
#
# App Runner 헬스체크 경로: /_stcore/health (Streamlit 기본)

set -euo pipefail

# ── 로그 유틸 ────────────────────────────────────────────────
log() { echo "[start.sh] $(date -u +'%H:%M:%S') $*"; }

# ── DB 테이블 초기화 ─────────────────────────────────────────
log "DB 테이블 초기화 중..."
python - <<'PYEOF'
from scraper.db import create_db_tables
create_db_tables()
print("[start.sh] job_queue 테이블 초기화 완료")
PYEOF

# ── FastAPI (uvicorn) 백그라운드 실행 ────────────────────────
log "FastAPI 서버 시작 (포트 8000)..."
uvicorn web.api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --log-level info \
    --no-access-log &
UVICORN_PID=$!
log "uvicorn PID=$UVICORN_PID"

# uvicorn 준비 대기 (최대 10초)
for i in $(seq 1 10); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        log "FastAPI 준비 완료"
        break
    fi
    log "FastAPI 대기 중... ($i/10)"
    sleep 1
done

# ── Streamlit 포그라운드 실행 ────────────────────────────────
log "Streamlit 시작 (포트 8501)..."
exec streamlit run web/app.py \
    --server.port=8501 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.enableCORS=false \
    --server.enableXsrfProtection=false

# exec 이 컨테이너를 장악하므로 이후 코드는 실행되지 않음.
# Streamlit 이 종료되면 컨테이너도 종료되고,
# 백그라운드 uvicorn 은 커널에 의해 정리됩니다.
