# =============================================================
# TenAsia Intelligence Hub — Backend Dockerfile
# Python 3.11 / FastAPI + Scraper + Processor
#
# Stages:
#   deps        → pip install (캐시 레이어 분리)
#   dev         → 소스 볼륨 마운트 + uvicorn --reload
#   production  → 소스 포함 + 멀티 워커
# =============================================================

# ─── 1. Dependency builder ────────────────────────────────────
FROM python:3.11-slim AS deps

WORKDIR /build

# 빌드에만 필요한 컴파일러 등 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --prefix=/install 로 격리된 경로에 설치 → 다음 스테이지로 COPY만 하면 됨
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── 2. Slim runtime base ─────────────────────────────────────
FROM python:3.11-slim AS base

LABEL org.opencontainers.image.title="TenAsia Intelligence Hub — API"
LABEL org.opencontainers.image.description="FastAPI backend: scraper, AI processor, REST API"

# 런타임 시스템 라이브러리만
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        # opencv-python-headless 런타임 의존
        libgl1-mesa-glx \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    # 비루트 실행 유저
    && groupadd -r appgroup \
    && useradd  -r -g appgroup -u 1000 app

# deps 스테이지에서 설치된 패키지 복사
COPY --from=deps /install /usr/local

WORKDIR /app


# ─── 3. Development (소스는 볼륨으로 마운트) ─────────────────
FROM base AS dev

# 진입점 스크립트만 이미지에 내장 (소스는 볼륨)
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "web.api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app"]


# ─── 4. Production (소스 이미지에 포함) ──────────────────────
FROM base AS production

COPY --chown=app:appgroup . .
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:$PATH

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "web.api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "info"]
