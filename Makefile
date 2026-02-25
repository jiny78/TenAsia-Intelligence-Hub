# =============================================================
# TenAsia Intelligence Hub — Makefile
#
# 사용법:
#   make setup        ← 최초 배포 전 전체 셋업 (이것만 실행하면 됩니다)
#   make deploy       ← 수동 배포 트리거
#   make secrets      ← Secrets Manager 값만 재등록
#   make destroy      ← 인프라 전체 삭제 (주의)
# =============================================================

.DEFAULT_GOAL := help
TERRAFORM_DIR := terraform
SCRIPTS_DIR   := scripts

# 색상
GREEN  := \033[0;32m
YELLOW := \033[1;33m
NC     := \033[0m

.PHONY: help setup bootstrap tf-init tf-apply secrets github-secrets deploy destroy clean

help:
	@echo ""
	@echo "  $(GREEN)TenAsia Intelligence Hub$(NC)"
	@echo ""
	@echo "  $(YELLOW)최초 배포$(NC)"
	@echo "    make setup          전체 셋업 (값 수집 → 인프라 → 시크릿 → GitHub 순서로 자동 처리)"
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

# ── 최초 배포 전 전체 셋업 ─────────────────────────────────────
setup:
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
