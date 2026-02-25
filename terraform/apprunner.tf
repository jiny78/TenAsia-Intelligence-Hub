# -------------------------------------------------------
# App Runner — 컨테이너 앱 서비스
# -------------------------------------------------------

# VPC 커넥터: App Runner → 프라이빗 RDS 통신
resource "aws_apprunner_vpc_connector" "main" {
  vpc_connector_name = "${local.name}-vpc-connector"
  subnets            = aws_subnet.private[*].id
  security_groups    = [aws_security_group.apprunner_connector.id]
  tags               = { Name = "${local.name}-vpc-connector" }
}

resource "aws_apprunner_service" "main" {
  service_name = "tenasia-intelligence-hub"

  source_configuration {
    authentication_configuration {
      access_role_arn = aws_iam_role.apprunner_access.arn
    }

    image_repository {
      image_identifier      = "${aws_ecr_repository.app.repository_url}:latest"
      image_repository_type = "ECR"

      image_configuration {
        port = "8501"

        # 비민감 환경 변수
        runtime_environment_variables = {
          ENVIRONMENT              = var.environment
          AWS_REGION               = var.aws_region
          S3_BUCKET_NAME           = aws_s3_bucket.thumbnails.bucket
          EC2_SCRAPER_INSTANCE_ID  = aws_instance.scraper.id   # SSM SendCommand 타겟
          FASTAPI_INTERNAL_PORT    = "8000"
        }

        # 민감 정보 — Secrets Manager에서 직접 주입
        runtime_environment_secrets = {
          GEMINI_API_KEY = "${aws_secretsmanager_secret.gemini_api_key.arn}:GEMINI_API_KEY::"
          DATABASE_URL   = "${aws_secretsmanager_secret.database_url.arn}:DATABASE_URL::"
        }
      }
    }

    # ECR 새 이미지 push 시 자동 배포
    auto_deployments_enabled = true
  }

  instance_configuration {
    cpu               = "1024"  # 1 vCPU
    memory            = "2048"  # 2 GB
    instance_role_arn = aws_iam_role.app.arn
  }

  network_configuration {
    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = aws_apprunner_vpc_connector.main.arn
    }
    ingress_configuration {
      is_publicly_accessible = true
    }
  }

  health_check_configuration {
    protocol            = "HTTP"
    path                = "/_stcore/health"
    interval            = 20
    timeout             = 5
    healthy_threshold   = 1
    unhealthy_threshold = 5
  }

  tags = { Name = "tenasia-intelligence-hub-service" }

  depends_on = [
    aws_iam_role_policy_attachment.app_s3,
    aws_iam_role_policy_attachment.app_secrets,
    aws_iam_role_policy_attachment.apprunner_ecr,
  ]
}
