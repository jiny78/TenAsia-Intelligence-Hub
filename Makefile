# =============================================================
# TenAsia Intelligence Hub — Makefile
#
# 로컬 개발:
#   make setup          ← 가상환경 생성 + requirements.txt 설치
#   make dev-web        ← FastAPI(8000) + Streamlit(8501) 동시 실행
#   make db-init        ← Alembic 마이그레이션 (upgrade head)
#   make test-scraper   ← 어제 날짜 1일치 dry-run 스크래핑
#
# 클라우드:
#   make cloud-setup  ← AWS 인프라 전체 셋업
#   make deploy       ← App Runner 수동 배포 트리거
#   make destroy      ← 인프라 전체 삭제 (주의)
# =============================================================

.DEFAULT_GOAL := help
TERRAFORM_DIR := terraform
SCRIPTS_DIR   := scripts

# 색상
GREEN  := \033[0;32m
YELLOW := \033[1;33m
CYAN   := \033[0;36m
NC     := \033[0m

# ── 가상환경 경로 (Windows / Unix 자동 감지) ───────────────
VENV := .venv
ifeq ($(OS),Windows_NT)
    VENV_BIN := $(VENV)/Scripts
else
    VENV_BIN := $(VENV)/bin
endif
PY  := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip

# test-scraper 기본 날짜 (어제, 덮어쓰기 가능)
SCRAPE_DATE ?= $(shell date -d "1 day ago" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)

.PHONY: help setup dev-web db-init test-scraper \
        cloud-setup bootstrap tf-init tf-apply secrets github-secrets deploy destroy clean \
        docker-dev docker-build docker-up docker-down docker-logs docker-ps docker-clean

help:
	@echo ""
	@echo "  $(GREEN)TenAsia Intelligence Hub$(NC)"
	@echo ""
	@echo "  $(CYAN)로컬 개발$(NC)"
	@echo "    make setup          가상환경 생성 및 requirements.txt 설치"
	@echo "    make dev-web        FastAPI(8000) + Streamlit(8501) 동시 실행 (개발 모드)"
	@echo "    make db-init        Alembic 마이그레이션 실행 (upgrade head)"
	@echo "    make test-scraper   어제 날짜 1일치 샘플 스크래핑 (dry-run)"
	@echo "                        └ 날짜 지정: make test-scraper SCRAPE_DATE=2025-01-15"
	@echo ""
	@echo "  $(YELLOW)클라우드 배포$(NC)"
	@echo "    make cloud-setup    전체 AWS 셋업 (인프라 → 시크릿 → GitHub 순서)"
	@echo ""
	@echo "  $(YELLOW)개별 단계$(NC)"
	@echo "    make bootstrap      Terraform 상태 저장소만 생성 (S3 + DynamoDB)"
	@echo "    make tf-init        terraform init"
	@echo "    make tf-apply       terraform plan + apply"
	@echo "    make secrets        Secrets Manager 값 재등록"
	@echo "    make github-secrets GitHub Secrets 재등록 (terraform output 필요)"
	@echo ""
	@echo "  $(YELLOW)운영$(NC)"
	@echo "    make deploy         App Runner 수동 배포 트리거"
	@echo "    make destroy        인프라 전체 삭제 (위험)"
	@echo "    make clean          로컬 Terraform 캐시 정리"
	@echo ""
	@echo "  $(CYAN)Docker$(NC)"
	@echo "    make docker-dev     개발 스택 시작 (Hot Reload, override.yml 자동 병합)"
	@echo "    make docker-build   프로덕션 이미지 빌드 (--no-cache)"
	@echo "    make docker-up      프로덕션 스택 백그라운드 시작"
	@echo "    make docker-down    모든 컨테이너 중지"
	@echo "    make docker-logs    실시간 로그 (Ctrl+C 로 종료)"
	@echo "    make docker-ps      컨테이너 상태 확인"
	@echo "    make docker-clean   컨테이너 + 볼륨 완전 삭제 (위험)"
	@echo ""

# ── 로컬 개발: 가상환경 구축 ──────────────────────────────────
setup:
	@echo "$(GREEN)[setup]$(NC) 가상환경 생성 중..."
	@python -m venv $(VENV)
	@echo "$(GREEN)[setup]$(NC) 패키지 설치 중..."
	@$(PIP) install --upgrade pip --quiet
	@$(PIP) install -r requirements.txt
	@echo ""
	@echo "  $(GREEN)완료!$(NC) 아래 명령으로 가상환경을 활성화하세요:"
	@echo "    Windows : .venv\\Scripts\\activate"
	@echo "    macOS   : source .venv/bin/activate"
	@echo ""
	@echo "  이후 .env 파일에 DATABASE_URL, GEMINI_API_KEY 를 설정한 뒤"
	@echo "  make db-init 으로 DB 스키마를 초기화하세요."

# ── 로컬 개발: FastAPI + Streamlit 동시 실행 ──────────────────
dev-web:
	@echo "$(GREEN)[dev-web]$(NC) 개발 서버 시작..."
	@echo "  FastAPI   → http://localhost:8000  (Swagger: /docs)"
	@echo "  Streamlit → http://localhost:8501"
	@echo "  종료: Ctrl+C"
	@echo ""
	@trap 'kill 0' EXIT; \
	 $(PY) -m uvicorn web.api:app \
	   --host localhost \
	   --port 8000 \
	   --reload \
	   --log-level info & \
	 $(PY) -m streamlit run web/app.py \
	   --server.port 8501 \
	   --server.address localhost \
	   --server.headless false; \
	 wait

# ── 로컬 개발: DB 마이그레이션 ────────────────────────────────
db-init:
	@echo "$(GREEN)[db-init]$(NC) Alembic 마이그레이션 실행 (upgrade head)..."
	@echo "  DATABASE_URL: $${DATABASE_URL:-(.env 파일에서 로드됩니다)}"
	@$(PY) -m alembic upgrade head
	@echo "$(GREEN)[db-init]$(NC) 마이그레이션 완료"

# ── 로컬 개발: 스크래퍼 샘플 테스트 ──────────────────────────
# 사용 예: make test-scraper SCRAPE_DATE=2025-01-15
test-scraper:
	@echo "$(GREEN)[test-scraper]$(NC) 샘플 스크래핑 시작 (날짜: $(SCRAPE_DATE), dry-run)"
	@echo "  --dry-run: HTTP 요청·파싱은 수행하되 DB에 저장하지 않음"
	@$(PY) -m scraper.engine scrape-range \
	   --start $(SCRAPE_DATE) \
	   --end   $(SCRAPE_DATE) \
	   --batch-size 5 \
	   --dry-run
	@echo "$(GREEN)[test-scraper]$(NC) 테스트 완료 (DB에 저장 없음)"

# ── 클라우드 배포 전 전체 셋업 ────────────────────────────────
cloud-setup:
	@chmod +x $(SCRIPTS_DIR)/setup.sh
	@bash $(SCRIPTS_DIR)/setup.sh

# ── Terraform 상태 저장소 생성 ────────────────────────────────
bootstrap:
	@echo "$(GREEN)[bootstrap]$(NC) Terraform 상태 저장소 생성 중..."
	@AWS_REGION=$${AWS_REGION:-ap-northeast-2}; \
	BUCKET="tenasia-intelligence-hub-tfstate"; \
	TABLE="tenasia-intelligence-hub-tf-locks"; \
	if aws s3api head-bucket --bucket "$$BUCKET" 2>/dev/null; then \
	  echo "  S3 버킷 이미 존재: $$BUCKET"; \
	else \
	  aws s3api create-bucket --bucket "$$BUCKET" --region "$$AWS_REGION" \
	    --create-bucket-configuration LocationConstraint="$$AWS_REGION" 2>/dev/null || \
	  aws s3api create-bucket --bucket "$$BUCKET" --region "$$AWS_REGION"; \
	  aws s3api put-bucket-versioning --bucket "$$BUCKET" \
	    --versioning-configuration Status=Enabled; \
	  echo "  S3 버킷 생성 완료: $$BUCKET"; \
	fi; \
	if aws dynamodb describe-table --table-name "$$TABLE" --region "$$AWS_REGION" &>/dev/null; then \
	  echo "  DynamoDB 테이블 이미 존재: $$TABLE"; \
	else \
	  aws dynamodb create-table --table-name "$$TABLE" \
	    --attribute-definitions AttributeName=LockID,AttributeType=S \
	    --key-schema AttributeName=LockID,KeyType=HASH \
	    --billing-mode PAY_PER_REQUEST --region "$$AWS_REGION"; \
	  echo "  DynamoDB 테이블 생성 완료: $$TABLE"; \
	fi

# ── Terraform ─────────────────────────────────────────────────
tf-init:
	@echo "$(GREEN)[tf-init]$(NC) terraform init..."
	@cd $(TERRAFORM_DIR) && terraform init -reconfigure

tf-apply: tf-init
	@echo "$(GREEN)[tf-apply]$(NC) terraform plan & apply..."
	@cd $(TERRAFORM_DIR) && terraform plan && terraform apply

tf-output:
	@cd $(TERRAFORM_DIR) && terraform output

# ── Secrets Manager 재등록 ────────────────────────────────────
secrets:
	@echo "$(GREEN)[secrets]$(NC) Secrets Manager 값 등록"
	@read -rsp "  GEMINI_API_KEY: " GEMINI && echo ""; \
	read -rsp "  DATABASE_URL  : " DB_URL && echo ""; \
	REGION=$${AWS_REGION:-ap-northeast-2}; \
	aws secretsmanager put-secret-value \
	  --secret-id tih/GEMINI_API_KEY \
	  --secret-string "{\"GEMINI_API_KEY\":\"$$GEMINI\"}" \
	  --region "$$REGION" && echo "  tih/GEMINI_API_KEY 등록 완료"; \
	aws secretsmanager put-secret-value \
	  --secret-id tih/DATABASE_URL \
	  --secret-string "{\"DATABASE_URL\":\"$$DB_URL\"}" \
	  --region "$$REGION" && echo "  tih/DATABASE_URL 등록 완료"

# ── GitHub Secrets 재등록 ─────────────────────────────────────
github-secrets:
	@echo "$(GREEN)[github-secrets]$(NC) GitHub Secrets 등록"
	@read -rp "  GitHub 레포 (org/repo): " REPO; \
	ROLE_ARN=$$(cd $(TERRAFORM_DIR) && terraform output -raw github_actions_role_arn); \
	SERVICE_ARN=$$(cd $(TERRAFORM_DIR) && terraform output -raw service_arn); \
	echo "$$ROLE_ARN"    | gh secret set AWS_ROLE_ARN           --repo "$$REPO" && echo "  AWS_ROLE_ARN 등록 완료"; \
	echo "$$SERVICE_ARN" | gh secret set APP_RUNNER_SERVICE_ARN --repo "$$REPO" && echo "  APP_RUNNER_SERVICE_ARN 등록 완료"

# ── 수동 배포 트리거 ──────────────────────────────────────────
deploy:
	@echo "$(GREEN)[deploy]$(NC) App Runner 배포 트리거..."
	@SERVICE_ARN=$$(cd $(TERRAFORM_DIR) && terraform output -raw service_arn); \
	REGION=$${AWS_REGION:-ap-northeast-2}; \
	aws apprunner start-deployment \
	  --service-arn "$$SERVICE_ARN" \
	  --region "$$REGION" \
	  --query 'OperationId' --output text | xargs -I{} echo "  배포 시작됨 | OperationId: {}"

# ── 정리 ──────────────────────────────────────────────────────
destroy:
	@echo "$(YELLOW)[destroy]$(NC) 인프라 전체를 삭제합니다!"
	@read -rp "  정말 삭제하시겠습니까? 'destroy'를 입력하세요: " CONFIRM; \
	if [[ "$$CONFIRM" == "destroy" ]]; then \
	  cd $(TERRAFORM_DIR) && terraform destroy; \
	else \
	  echo "취소됨."; \
	fi

clean:
	@echo "$(GREEN)[clean]$(NC) 로컬 Terraform 캐시 정리..."
	@rm -rf $(TERRAFORM_DIR)/.terraform $(TERRAFORM_DIR)/.terraform.lock.hcl $(TERRAFORM_DIR)/tfplan
	@echo "  정리 완료"

# =============================================================
# Docker Compose 명령어
# =============================================================

# ── 개발 서버 (Hot Reload) ─────────────────────────────────────
docker-dev:
	@echo "$(GREEN)[docker-dev]$(NC) 개발 환경 시작 (Hot Reload)..."
	@echo "  docker-compose.override.yml 자동 병합"
	@echo "  DB  → localhost:5432"
	@echo "  API → http://localhost:8000  (Swagger: /docs)"
	@echo "  Web → http://localhost:3000"
	@echo "  종료: Ctrl+C 후 'make docker-down'"
	@echo ""
	@if [ ! -f .env ]; then \
	  echo "$(YELLOW)  ⚠ .env 파일이 없습니다. .env.docker 를 복사하세요:$(NC)"; \
	  echo "    cp .env.docker .env"; \
	  exit 1; \
	fi
	docker compose up

# ── 프로덕션 이미지 빌드 ─────────────────────────────────────
docker-build:
	@echo "$(GREEN)[docker-build]$(NC) 프로덕션 이미지 빌드..."
	@echo "  BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose -f docker-compose.yml build --no-cache

# ── 프로덕션 서버 시작 ───────────────────────────────────────
docker-up:
	@echo "$(GREEN)[docker-up]$(NC) 프로덕션 스택 시작..."
	docker compose -f docker-compose.yml up -d
	@echo ""
	@echo "  API → http://localhost:$${API_PORT:-8000}"
	@echo "  Web → http://localhost:$${WEB_PORT:-3000}"

# ── 모든 컨테이너 중지 ──────────────────────────────────────
docker-down:
	@echo "$(GREEN)[docker-down]$(NC) 컨테이너 중지..."
	docker compose down

# ── 로그 실시간 확인 ─────────────────────────────────────────
docker-logs:
	docker compose logs -f --tail=100

# ── 컨테이너 상태 확인 ──────────────────────────────────────
docker-ps:
	docker compose ps

# ── 볼륨 포함 완전 삭제 ─────────────────────────────────────
docker-clean:
	@echo "$(YELLOW)[docker-clean]$(NC) 컨테이너 + 볼륨 모두 삭제합니다!"
	@read -rp "  계속하시겠습니까? (y/N): " CONFIRM; \
	if [[ "$$CONFIRM" == "y" || "$$CONFIRM" == "Y" ]]; then \
	  docker compose down -v --remove-orphans; \
	  echo "  완료"; \
	else \
	  echo "  취소됨"; \
	fi
