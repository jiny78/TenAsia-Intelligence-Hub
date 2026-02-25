# =======================================================
# IAM 연결 구조 요약
#
#  ┌─────────────────────────────────────────────────────┐
#  │              App Runner (aws_iam_role.app)           │
#  │                                                     │
#  │  → S3 (tenasia-thumbnails)    : 썸네일 읽기·쓰기    │
#  │  → Secrets Manager            : API키·DB URL 읽기   │
#  │     tih/GEMINI_API_KEY                              │
#  │     tih/DATABASE_URL                                │
#  │  → SSM Parameter Store        : Kill Switch 읽기·쓰기│
#  │     /tih/gemini/kill_switch                         │
#  │     /tih/gemini/monthly_tokens                      │
#  │  → SSM SendCommand            : EC2 Scraper 원격 실행│
#  │     조건: Tag[Name]=tih-scraper                     │
#  └─────────────────────────────────────────────────────┘
#
#  ┌─────────────────────────────────────────────────────┐
#  │         EC2 Scraper (aws_iam_role.scraper)           │
#  │                    ← ec2_scraper.tf 에 정의          │
#  │                                                     │
#  │  → AmazonSSMManagedInstanceCore : SSM 에이전트       │
#  │  → S3 (tenasia-thumbnails)      : 이미지 업로드      │
#  │  → Secrets Manager              : 위와 동일          │
#  │  → SSM Parameter Store          : Kill Switch        │
#  └─────────────────────────────────────────────────────┘
#
#  ┌─────────────────────────────────────────────────────┐
#  │  App Runner → RDS 네트워크 경로                      │
#  │                                                     │
#  │  App Runner ──[VPC Connector]──► sg-apprunner       │
#  │                                    │ egress 5432    │
#  │                                    ▼                │
#  │                             sg-rds (ingress 5432    │
#  │                             from sg-apprunner only) │
#  │                                    │                │
#  │                                    ▼                │
#  │                          RDS PostgreSQL 15           │
#  │                          (Private Subnet, no public) │
#  └─────────────────────────────────────────────────────┘
#
#  ┌─────────────────────────────────────────────────────┐
#  │  App Runner → Secrets Manager 경로                  │
#  │                                                     │
#  │  방법 1 (기본): App Runner 제어 플레인이 직접 주입   │
#  │    runtime_environment_secrets 설정 시              │
#  │    컨테이너 시작 전 App Runner 가 자동으로 주입      │
#  │    → IAM Role.app 에 secretsmanager:GetSecretValue  │
#  │                                                     │
#  │  방법 2 (boto3 직접 호출): core/config.py            │
#  │    EC2 → VPC Endpoint (vpc_endpoints.tf)            │
#  │         → Secrets Manager (인터넷 불필요)           │
#  └─────────────────────────────────────────────────────┘
# =======================================================

# -------------------------------------------------------
# 1. App Runner 인스턴스 Role
#    Principal: tasks.apprunner.amazonaws.com 전용
#    (EC2 Scraper 는 별도 aws_iam_role.scraper 사용)
# -------------------------------------------------------

resource "aws_iam_role" "app" {
  name        = "${local.name}-app-role"
  description = "App Runner 인스턴스 Role — S3 + Secrets Manager + SSM Kill Switch + SendCommand"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "tasks.apprunner.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# S3 접근 정책 (tenasia-thumbnails 만)
resource "aws_iam_policy" "s3_thumbnails" {
  name        = "${local.name}-s3-thumbnails"
  description = "tenasia-thumbnails 버킷 읽기·쓰기·삭제"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BucketAccess"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.thumbnails.arn
      },
      {
        Sid    = "ObjectAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.thumbnails.arn}/*"
      }
    ]
  })
}

# Secrets Manager 접근 정책 (지정 시크릿 읽기만)
#
# 적용 대상:
#   - aws_iam_role.app    (App Runner 런타임 — boto3 직접 호출 및 제어 플레인 주입)
#   - aws_iam_role.scraper (EC2 Scraper — boto3 직접 호출 via VPC Endpoint)
#
# 최소 권한 원칙:
#   - Resource 를 시크릿 ARN 으로 명시적 제한 (와일드카드 불허)
#   - GetSecretValue + DescribeSecret 만 허용 (CreateSecret/PutSecretValue 불허)
resource "aws_iam_policy" "secrets_read" {
  name        = "${local.name}-secrets-read"
  description = "GEMINI_API_KEY / DATABASE_URL 시크릿 읽기 전용 (App Runner + EC2 Scraper 공용)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "GetSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          aws_secretsmanager_secret.gemini_api_key.arn,
          aws_secretsmanager_secret.database_url.arn
        ]
      }
    ]
  })
}

# SSM Parameter Store 접근 정책 (Kill Switch 읽기·쓰기)
resource "aws_iam_policy" "ssm_kill_switch" {
  name        = "${local.name}-ssm-kill-switch"
  description = "Gemini Kill Switch SSM 파라미터 읽기·쓰기"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KillSwitchReadWrite"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter"
        ]
        Resource = [
          aws_ssm_parameter.gemini_kill_switch.arn,
          aws_ssm_parameter.gemini_monthly_tokens.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app_s3" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.s3_thumbnails.arn
}

resource "aws_iam_role_policy_attachment" "app_secrets" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.secrets_read.arn
}

resource "aws_iam_role_policy_attachment" "app_ssm" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.ssm_kill_switch.arn
}

# App Runner → EC2 Scraper 원격 실행 (SSM SendCommand)
resource "aws_iam_policy" "ssm_send_command" {
  name        = "${local.name}-ssm-send-command"
  description = "App Runner가 EC2 Scraper에 SSM RunCommand를 보낼 수 있는 권한"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SendCommandToScraper"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",  # 명령 실행 결과 조회
          "ssm:ListCommandInvocations"
        ]
        Resource = [
          "arn:aws:ec2:${var.aws_region}:*:instance/*",
          "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"
        ]
        Condition = {
          StringEquals = {
            "ssm:resourceTag/Name" = "${local.name}-scraper"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app_send_command" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.ssm_send_command.arn
}

# -------------------------------------------------------
# 2. App Runner Access Role (ECR 이미지 Pull 전용)
# -------------------------------------------------------

resource "aws_iam_role" "apprunner_access" {
  name        = "${local.name}-apprunner-access-role"
  description = "App Runner → ECR Pull 전용"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "build.apprunner.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apprunner_ecr" {
  role       = aws_iam_role.apprunner_access.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

# -------------------------------------------------------
# 3. GitHub Actions OIDC Role (비밀번호 없는 CI/CD 인증)
# -------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions" {
  name        = "${local.name}-github-actions-role"
  description = "GitHub Actions CI/CD — ECR Push + App Runner 배포"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/TenAsia-Intelligence-Hub:*"
        }
      }
    }]
  })
}

resource "aws_iam_policy" "github_deploy" {
  name        = "${local.name}-github-deploy"
  description = "ECR Push + App Runner StartDeployment"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage"
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "AppRunnerDeploy"
        Effect = "Allow"
        Action = ["apprunner:StartDeployment", "apprunner:DescribeService"]
        Resource = aws_apprunner_service.main.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_deploy" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.github_deploy.arn
}
