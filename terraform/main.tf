terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # -------------------------------------------------------
  # 원격 상태 저장소
  # 최초 배포 전 아래 리소스를 수동으로 생성해야 합니다:
  #   aws s3 mb s3://tenasia-intelligence-hub-tfstate --region ap-northeast-2
  #   aws dynamodb create-table \
  #     --table-name tenasia-intelligence-hub-tf-locks \
  #     --attribute-definitions AttributeName=LockID,AttributeType=S \
  #     --key-schema AttributeName=LockID,KeyType=HASH \
  #     --billing-mode PAY_PER_REQUEST \
  #     --region ap-northeast-2
  # -------------------------------------------------------
  backend "s3" {
    bucket         = "tenasia-intelligence-hub-tfstate"
    key            = "terraform.tfstate"
    region         = "ap-northeast-2"
    encrypt        = true
    dynamodb_table = "tenasia-intelligence-hub-tf-locks"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "TenAsia-Intelligence-Hub"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

# =============================================================
# 비용 관리 — AWS Budgets
# =============================================================
#
# 알림 구조:
#   실제(ACTUAL)    50% → 조기 경보
#   실제(ACTUAL)    80% → 주의
#   실제(ACTUAL)   100% → 예산 초과 (즉시 대응 필요)
#   예측(FORECASTED) 80% → 사전 경보 (아직 초과 전이지만 초과 예상)
#   예측(FORECASTED)100% → 이달 내 초과 확실
#
# ※ AWS Budget 알림 이메일은 SNS 구독 확인 없이 직접 발송됩니다.
# =============================================================

resource "aws_budgets_budget" "monthly" {
  name         = "tih-monthly-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_amount_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # ── 실제 사용량 알림 ─────────────────────────────────────
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # ── 예측 사용량 알림 (초과 전 사전 경보) ─────────────────
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# =============================================================
# Kill Switch — SSM Parameter Store
#
# Gemini API 사용량이 임계치를 초과하면 앱이 이 파라미터를 읽어
# API 호출을 자동으로 중단(Kill Switch)합니다.
#
# 파라미터:
#   /tih/gemini/kill_switch        "false" | "true"
#   /tih/gemini/monthly_tokens     현재 월 누적 토큰 수 (정수 문자열)
#
# 값 수동 변경:
#   # Kill Switch ON  (즉시 중단)
#   aws ssm put-parameter --name /tih/gemini/kill_switch \
#     --value "true" --type String --overwrite
#
#   # Kill Switch OFF (재개)
#   aws ssm put-parameter --name /tih/gemini/kill_switch \
#     --value "false" --type String --overwrite
# =============================================================

resource "aws_ssm_parameter" "gemini_kill_switch" {
  name        = "/tih/gemini/kill_switch"
  description = "Gemini API Kill Switch — true 로 설정 시 모든 API 호출 중단"
  type        = "String"
  value       = "false"

  lifecycle {
    ignore_changes = [value]  # 앱이 런타임에 값을 변경해도 Terraform이 덮어쓰지 않음
  }
}

resource "aws_ssm_parameter" "gemini_monthly_tokens" {
  name        = "/tih/gemini/monthly_tokens"
  description = "이번 달 Gemini API 누적 토큰 사용량"
  type        = "String"
  value       = "0"

  lifecycle {
    ignore_changes = [value]
  }
}
