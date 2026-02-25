variable "aws_region" {
  description = "AWS 배포 리전"
  type        = string
  default     = "ap-northeast-2"
}

variable "environment" {
  description = "배포 환경"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging"], var.environment)
    error_message = "environment 는 production 또는 staging 이어야 합니다."
  }
}

variable "db_username" {
  description = "RDS 마스터 사용자 이름"
  type        = string
  default     = "tih_admin"
}

variable "db_name" {
  description = "초기 데이터베이스 이름"
  type        = string
  default     = "tih"
}

variable "db_password" {
  description = "RDS 마스터 비밀번호 (terraform.tfvars 에 설정, 절대 커밋 금지)"
  type        = string
  sensitive   = true
}

variable "vpc_cidr" {
  description = "VPC CIDR 블록"
  type        = string
  default     = "10.10.0.0/16"
}

variable "azs" {
  description = "사용할 가용 영역"
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]
}

variable "github_org" {
  description = "GitHub 조직 또는 사용자명 (OIDC 신뢰 정책에 사용)"
  type        = string
  # 예: "myorg" → repo:myorg/TenAsia-Intelligence-Hub:*
}

# ── 비용 관리 ──────────────────────────────────────────────
variable "budget_amount_usd" {
  description = "월 예산 한도 (USD). 이 금액 기준으로 50/80/100% 알림을 전송합니다."
  type        = number
  default     = 50

  validation {
    condition     = var.budget_amount_usd > 0
    error_message = "budget_amount_usd 는 0보다 커야 합니다."
  }
}

variable "alert_email" {
  description = "비용 초과 알림 수신 이메일 (AWS Budget + Gemini Kill Switch 알림 공용)"
  type        = string

  validation {
    condition     = can(regex("^[^@]+@[^@]+\\.[^@]+$", var.alert_email))
    error_message = "유효한 이메일 주소를 입력하세요."
  }
}

# ── 관리자 접근 (Bastion / SSM) ────────────────────────────

variable "admin_ip_cidr" {
  description = "Bastion Host SSH 허용 IP (CIDR). 현재 IP 확인: curl ifconfig.me"
  type        = string
  default     = "0.0.0.0/32"  # 기본값: 아무도 접근 불가 (의도적으로 막아둠)

  validation {
    condition     = can(cidrhost(var.admin_ip_cidr, 0))
    error_message = "유효한 CIDR 형식이어야 합니다. 예: 1.2.3.4/32"
  }
}

variable "enable_bastion" {
  description = "Bastion Host 생성 여부. 평소 false 유지, 필요 시 true → terraform apply"
  type        = bool
  default     = false
}

variable "public_key_path" {
  description = "Bastion Host SSH 공개키 파일 경로 (enable_bastion = true 일 때 사용)"
  type        = string
  default     = ""
  # 키 생성: ssh-keygen -t ed25519 -C "tih-bastion" -f ~/.ssh/tih-bastion
  # 이후: public_key_path = "~/.ssh/tih-bastion.pub"
}
