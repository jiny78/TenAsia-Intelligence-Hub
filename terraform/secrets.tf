# -------------------------------------------------------
# AWS Secrets Manager
# 실제 값은 배포 후 아래 명령으로 직접 주입합니다
# (Terraform 상태 파일에 시크릿 값이 남지 않도록 ignore_changes 사용)
# -------------------------------------------------------

# GEMINI_API_KEY
resource "aws_secretsmanager_secret" "gemini_api_key" {
  name                    = "tih/GEMINI_API_KEY"
  description             = "Google Gemini API 키"
  recovery_window_in_days = 7
  tags                    = { Name = "tih-gemini-api-key" }
}

resource "aws_secretsmanager_secret_version" "gemini_api_key" {
  secret_id     = aws_secretsmanager_secret.gemini_api_key.id
  secret_string = jsonencode({ GEMINI_API_KEY = "REPLACE_ME" })

  lifecycle {
    ignore_changes = [secret_string]  # Terraform이 실제 값을 덮어쓰지 않음
  }
}

# DATABASE_URL
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "tih/DATABASE_URL"
  description             = "PostgreSQL 연결 URL"
  recovery_window_in_days = 7
  tags                    = { Name = "tih-database-url" }
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id

  # RDS 엔드포인트가 결정되면 자동 구성 (최초 1회)
  secret_string = jsonencode({
    DATABASE_URL = "postgresql://${var.db_username}:${var.db_password}@${aws_db_instance.main.endpoint}/${var.db_name}"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }

  depends_on = [aws_db_instance.main]
}

# -------------------------------------------------------
# 배포 후 값 주입 방법 (CLI)
#
# Gemini API Key:
#   aws secretsmanager put-secret-value \
#     --secret-id tih/GEMINI_API_KEY \
#     --secret-string '{"GEMINI_API_KEY":"실제-키-값"}' \
#     --region ap-northeast-2
#
# Database URL (자동 구성되지만 수동 변경 시):
#   aws secretsmanager put-secret-value \
#     --secret-id tih/DATABASE_URL \
#     --secret-string '{"DATABASE_URL":"postgresql://user:pass@host:5432/db"}' \
#     --region ap-northeast-2
# -------------------------------------------------------
