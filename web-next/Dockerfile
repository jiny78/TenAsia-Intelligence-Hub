# =============================================================
# TenAsia Intelligence Hub — Frontend Dockerfile
# Node.js 20 / Next.js 14 (App Router)
#
# Stages:
#   deps        → npm ci (캐시 레이어 분리)
#   dev         → next dev (소스 볼륨 마운트 + Hot Reload)
#   builder     → next build (standalone output)
#   production  → 최소 런타임 이미지
# =============================================================

# ─── 1. Dependency installer ──────────────────────────────────
FROM node:20-alpine AS deps

WORKDIR /app

# package-lock.json 이 있으면 npm ci, 없으면 npm install
COPY package.json package-lock.json* ./
RUN npm ci --frozen-lockfile


# ─── 2. Development (Hot Reload) ──────────────────────────────
FROM node:20-alpine AS dev

WORKDIR /app

# node_modules 는 deps 스테이지에서 가져옴
# 실제 소스코드는 docker-compose.override.yml 의 볼륨으로 마운트
COPY --from=deps /app/node_modules ./node_modules

# 비루트 실행
USER node

EXPOSE 3000
ENV PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=development

CMD ["npm", "run", "dev"]


# ─── 3. Builder ────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build-time env var (Next.js public 변수는 빌드 시 번들에 삽입됨)
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NODE_ENV=production \
    # 텔레메트리 비활성화
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build


# ─── 4. Production (standalone output) ────────────────────────
FROM node:20-alpine AS production

WORKDIR /app

RUN addgroup -S nodejs && adduser -S nextjs -G nodejs

# standalone 빌드 결과물 복사
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static     ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public           ./public

USER nextjs

EXPOSE 3000
ENV PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_OPTIONS="--max-old-space-size=1800"

# App Runner has its own health check; this is for local docker run
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -qO- http://localhost:3000/healthz || exit 1

# Use shell form so HOSTNAME is explicitly set at runtime,
# overriding any value injected by the container runtime (e.g. AWS Fargate).
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]
