#!/usr/bin/env bash
# =============================================================
# TenAsia Intelligence Hub — 배포 전 전체 셋업 스크립트
#
# 실행 한 번으로 아래를 모두 처리합니다:
#   1. 사전 도구 확인 (aws, terraform, gh)
#   2. 값 수집 (대화형 프롬프트)
#   3. terraform.tfvars 생성
#   4. Terraform 상태 저장소 생성 (S3 + DynamoDB)
#   5. terraform init / apply
#   6. AWS Secrets Manager 값 주입
#   7. GitHub Secrets 등록
#
# 사용법:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh
# =============================================================

set -euo pipefail

# ── 색상 ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}━━━ $* ━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TERRAFORM_DIR="$PROJECT_DIR/terraform"

# =============================================================
# STEP 0: 사전 도구 확인
# =============================================================
step "0/7  사전 도구 확인"

check_tool() {
  if ! command -v "$1" &>/dev/null; then
    error "$1 이 설치되어 있지 않습니다. 설치 후 다시 실행하세요."
  fi
  success "$1 확인됨 ($(command -v "$1"))"
}

check_tool aws
check_tool terraform
check_tool gh

# AWS 자격증명 확인
if ! aws sts get-caller-identity &>/dev/null; then
  error "AWS 자격증명이 설정되지 않았습니다.\n  aws configure 또는 환경 변수를 설정하세요."
fi
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
success "AWS 계정: $AWS_ACCOUNT_ID"

# GitHub 로그인 확인
if ! gh auth status &>/dev/null; then
  warn "GitHub CLI 로그인이 필요합니다."
  gh auth login
fi
success "GitHub 인증 확인됨"

# =============================================================
# STEP 1: 값 수집
# =============================================================
step "1/7  배포 설정 값 수집"

echo ""
echo "  설정 값을 입력하세요. [기본값]이 있으면 Enter 로 건너뜁니다."
echo ""

prompt() {
  local var_name="$1" prompt_text="$2" default="$3" secret="${4:-false}"
  local value=""
  while [[ -z "$value" ]]; do
    if [[ "$secret" == "true" ]]; then
      read -rsp "  ${prompt_text} [입력 숨김]: " value
      echo ""
    else
      read -rp "  ${prompt_text} [${default}]: " value
      value="${value:-$default}"
    fi
    [[ -z "$value" ]] && warn "값을 입력해야 합니다."
  done
  eval "$var_name='$value'"
}

prompt AWS_REGION         "AWS 리전"                              "ap-northeast-2"
prompt ENVIRONMENT        "배포 환경 (production/staging)"        "production"
prompt DB_USERNAME        "RDS 사용자 이름"                       "tih_admin"
prompt DB_NAME            "RDS 데이터베이스 이름"                 "tih"
prompt DB_PASSWORD        "RDS 비밀번호 (16자 이상 권장)"         ""   "true"
prompt GITHUB_ORG         "GitHub 조직 또는 사용자명"             ""
prompt GITHUB_REPO        "GitHub 레포지토리 이름"                "TenAsia-Intelligence-Hub"
prompt GEMINI_API_KEY     "Google Gemini API 키"                  ""   "true"
prompt BUDGET_AMOUNT      "월 예산 한도 (USD)"                    "50"
prompt ALERT_EMAIL        "비용 알림 수신 이메일"                  ""

# 관리자 접근 설정
echo ""
info "관리자 DB 접근 방식을 선택합니다."
echo "  [1] SSM Session Manager (권장 — SSH 키 불필요, 보안 우수)"
echo "  [2] Bastion Host + SSM  (SSH 키 사용, 즉각적인 접속 필요 시)"
read -rp "  선택 [1]: " ACCESS_CHOICE
ACCESS_CHOICE="${ACCESS_CHOICE:-1}"

ENABLE_BASTION="false"
ADMIN_IP_CIDR="0.0.0.0/32"
PUBLIC_KEY_PATH=""

if [[ "$ACCESS_CHOICE" == "2" ]]; then
  ENABLE_BASTION="true"
  # 현재 IP 자동 감지
  DETECTED_IP=$(curl -s --connect-timeout 5 ifconfig.me || curl -s --connect-timeout 5 ipinfo.io/ip || echo "")
  if [[ -n "$DETECTED_IP" ]]; then
    info "현재 IP 감지됨: $DETECTED_IP"
    read -rp "  관리자 IP CIDR [${DETECTED_IP}/32]: " ADMIN_IP_INPUT
    ADMIN_IP_CIDR="${ADMIN_IP_INPUT:-${DETECTED_IP}/32}"
  else
    prompt ADMIN_IP_CIDR "관리자 IP CIDR (예: 1.2.3.4/32)" "0.0.0.0/32"
  fi
  read -rp "  SSH 공개키 경로 [~/.ssh/tih-bastion.pub]: " KEY_PATH_INPUT
  PUBLIC_KEY_PATH="${KEY_PATH_INPUT:-~/.ssh/tih-bastion.pub}"
  # SSH 키가 없으면 생성
  KEY_FILE="${PUBLIC_KEY_PATH%.pub}"
  if [[ ! -f "$PUBLIC_KEY_PATH" ]]; then
    warn "SSH 키가 없습니다. 새로 생성합니다: $KEY_FILE"
    ssh-keygen -t ed25519 -C "tih-bastion" -f "$KEY_FILE" -N ""
    success "SSH 키 생성 완료: $KEY_FILE"
  fi
fi

echo ""
info "입력 완료. 아래 내용으로 진행합니다:"
echo "  리전       : $AWS_REGION"
echo "  환경       : $ENVIRONMENT"
echo "  DB 사용자  : $DB_USERNAME"
echo "  DB 이름    : $DB_NAME"
echo "  GitHub     : ${GITHUB_ORG}/${GITHUB_REPO}"
echo "  월 예산    : \$$BUDGET_AMOUNT"
echo "  알림 이메일: $ALERT_EMAIL"
echo "  DB 접근    : SSM Session Manager$( [[ "$ENABLE_BASTION" == "true" ]] && echo " + Bastion Host ($ADMIN_IP_CIDR)" || echo " (권장)")"
echo ""
read -rp "계속 진행하시겠습니까? (y/N): " CONFIRM
[[ "${CONFIRM,,}" != "y" ]] && echo "취소됨." && exit 0

# =============================================================
# STEP 2: terraform.tfvars 생성
# =============================================================
step "2/7  terraform.tfvars 생성"

TFVARS_FILE="$TERRAFORM_DIR/terraform.tfvars"

cat > "$TFVARS_FILE" <<EOF
aws_region  = "${AWS_REGION}"
environment = "${ENVIRONMENT}"
db_username = "${DB_USERNAME}"
db_name     = "${DB_NAME}"
db_password = "${DB_PASSWORD}"
github_org  = "${GITHUB_ORG}"

budget_amount_usd = ${BUDGET_AMOUNT}
alert_email       = "${ALERT_EMAIL}"

enable_bastion  = ${ENABLE_BASTION}
admin_ip_cidr   = "${ADMIN_IP_CIDR}"
public_key_path = "${PUBLIC_KEY_PATH}"
EOF

success "terraform.tfvars 생성 완료: $TFVARS_FILE"

# =============================================================
# STEP 3: Terraform 상태 저장소 생성 (S3 + DynamoDB)
# =============================================================
step "3/7  Terraform 상태 저장소 생성"

TF_STATE_BUCKET="tenasia-intelligence-hub-tfstate"
TF_LOCK_TABLE="tenasia-intelligence-hub-tf-locks"

# S3 버킷
if aws s3api head-bucket --bucket "$TF_STATE_BUCKET" 2>/dev/null; then
  info "S3 버킷 이미 존재: $TF_STATE_BUCKET"
else
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "$TF_STATE_BUCKET" \
      --region "$AWS_REGION"
  else
    aws s3api create-bucket \
      --bucket "$TF_STATE_BUCKET" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  aws s3api put-bucket-versioning \
    --bucket "$TF_STATE_BUCKET" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption \
    --bucket "$TF_STATE_BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  success "S3 버킷 생성 완료: $TF_STATE_BUCKET"
fi

# DynamoDB 테이블
if aws dynamodb describe-table --table-name "$TF_LOCK_TABLE" --region "$AWS_REGION" &>/dev/null; then
  info "DynamoDB 테이블 이미 존재: $TF_LOCK_TABLE"
else
  aws dynamodb create-table \
    --table-name "$TF_LOCK_TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION"
  success "DynamoDB 테이블 생성 완료: $TF_LOCK_TABLE"
fi

# =============================================================
# STEP 4: Terraform init / apply
# =============================================================
step "4/7  Terraform init & apply"

cd "$TERRAFORM_DIR"

terraform init -reconfigure
success "terraform init 완료"

terraform plan -out=tfplan
echo ""
read -rp "위 플랜으로 apply 하시겠습니까? (y/N): " TF_CONFIRM
[[ "${TF_CONFIRM,,}" != "y" ]] && warn "apply 취소됨. 이후 단계도 건너뜁니다." && exit 0

terraform apply tfplan
rm -f tfplan
success "terraform apply 완료"

# Terraform output 값 추출
SERVICE_ARN=$(terraform output -raw service_arn)
GITHUB_ROLE_ARN=$(terraform output -raw github_actions_role_arn)
ECR_URL=$(terraform output -raw ecr_repository_url)
SERVICE_URL=$(terraform output -raw service_url)
SCRAPER_INSTANCE_ID=$(terraform output -raw scraper_instance_id)

success "App Runner ARN      : $SERVICE_ARN"
success "GitHub Role ARN     : $GITHUB_ROLE_ARN"
success "ECR URL             : $ECR_URL"
success "서비스 URL           : $SERVICE_URL"
success "EC2 Scraper ID      : $SCRAPER_INSTANCE_ID"

cd "$PROJECT_DIR"

# =============================================================
# STEP 5: AWS Secrets Manager 값 주입
# =============================================================
step "5/7  AWS Secrets Manager 값 주입"

put_secret() {
  local secret_id="$1" json_value="$2"
  aws secretsmanager put-secret-value \
    --secret-id "$secret_id" \
    --secret-string "$json_value" \
    --region "$AWS_REGION"
  success "시크릿 등록: $secret_id"
}

# RDS 엔드포인트 조회 (Terraform output에서 sensitive라 별도 조회)
RDS_ENDPOINT=$(cd "$TERRAFORM_DIR" && terraform output -raw rds_endpoint)
DATABASE_URL="postgresql://${DB_USERNAME}:${DB_PASSWORD}@${RDS_ENDPOINT}/${DB_NAME}"

put_secret "tih/GEMINI_API_KEY" "{\"GEMINI_API_KEY\":\"${GEMINI_API_KEY}\"}"
put_secret "tih/DATABASE_URL"   "{\"DATABASE_URL\":\"${DATABASE_URL}\"}"

# =============================================================
# STEP 6: GitHub Secrets 등록
# =============================================================
step "6/7  GitHub Secrets 등록"

FULL_REPO="${GITHUB_ORG}/${GITHUB_REPO}"

set_github_secret() {
  local name="$1" value="$2"
  echo "$value" | gh secret set "$name" --repo "$FULL_REPO"
  success "GitHub Secret 등록: $name"
}

set_github_secret "AWS_ROLE_ARN"              "$GITHUB_ROLE_ARN"
set_github_secret "APP_RUNNER_SERVICE_ARN"   "$SERVICE_ARN"
set_github_secret "ECR_REPOSITORY_URL"       "$ECR_URL"
set_github_secret "EC2_SCRAPER_INSTANCE_ID"  "$SCRAPER_INSTANCE_ID"

# =============================================================
# STEP 7: 완료 요약
# =============================================================
step "7/7  셋업 완료"

echo ""
echo -e "${GREEN}${BOLD}모든 등록이 완료됐습니다!${NC}"
echo ""
echo "  서비스 URL      : ${SERVICE_URL}"
echo "  ECR 레포지토리  : ${ECR_URL}"
echo ""
echo "  다음 단계:"
echo "    git push origin main  →  GitHub Actions 가 자동으로 빌드/배포합니다"
echo ""
