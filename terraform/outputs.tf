output "service_url" {
  description = "App Runner 서비스 URL"
  value       = "https://${aws_apprunner_service.main.service_url}"
}

output "service_arn" {
  description = "App Runner 서비스 ARN → GitHub Secret: APP_RUNNER_SERVICE_ARN"
  value       = aws_apprunner_service.main.arn
}

output "ecr_repository_url" {
  description = "ECR 레포지토리 URL → GitHub Secret: ECR_REPOSITORY_URL"
  value       = aws_ecr_repository.app.repository_url
}

output "github_actions_role_arn" {
  description = "GitHub Actions OIDC Role ARN → GitHub Secret: AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}

output "rds_endpoint" {
  description = "RDS 엔드포인트 (프라이빗 — 직접 접근 불가)"
  value       = aws_db_instance.main.endpoint
  sensitive   = true
}

output "s3_public_url" {
  description = "S3 썸네일 퍼블릭 베이스 URL"
  value       = "https://${aws_s3_bucket.thumbnails.bucket}.s3.ap-northeast-2.amazonaws.com"
}

output "gemini_secret_arn" {
  description = "Gemini API Key 시크릿 ARN"
  value       = aws_secretsmanager_secret.gemini_api_key.arn
}

# ── 관리자 DB 접근 정보 ──────────────────────────────────────

output "ssm_host_instance_id" {
  description = "SSM Session Manager Host EC2 Instance ID (포트 포워딩에 사용)"
  value       = aws_instance.ssm_host.id
}

output "ssm_db_tunnel_command" {
  description = "RDS 접속용 SSM 포트 포워딩 명령어 (복사 후 바로 실행 가능)"
  value = join(" ", [
    "aws ssm start-session",
    "--target ${aws_instance.ssm_host.id}",
    "--document-name AWS-StartPortForwardingSessionToRemoteHost",
    "--parameters '{\"host\":[\"${split(":", aws_db_instance.main.endpoint)[0]}\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"15432\"]}'",
    "--region ${var.aws_region}",
    "# 연결 후: psql -h localhost -p 15432 -U ${var.db_username} -d ${var.db_name}"
  ])
}

output "bastion_public_ip" {
  description = "Bastion Host 퍼블릭 IP (enable_bastion = true 일 때만 값 있음)"
  value       = var.enable_bastion ? aws_instance.bastion[0].public_ip : "Bastion 비활성화 상태 (enable_bastion = false)"
}

output "bastion_ssh_command" {
  description = "Bastion SSH 터널 명령어 (enable_bastion = true 일 때만 유효)"
  value = var.enable_bastion ? join(" ", [
    "ssh -i ~/.ssh/tih-bastion",
    "-L 15432:${split(":", aws_db_instance.main.endpoint)[0]}:5432",
    "ec2-user@${aws_instance.bastion[0].public_ip} -N",
    "# 연결 후: psql -h localhost -p 15432 -U ${var.db_username} -d ${var.db_name}"
  ]) : "Bastion 비활성화 상태 (enable_bastion = false)"
}

output "scraper_instance_id" {
  description = "EC2 Scraper 인스턴스 ID → GitHub Secret: EC2_SCRAPER_INSTANCE_ID"
  value       = aws_instance.scraper.id
}

output "scraper_deploy_command" {
  description = "스크래퍼 코드 EC2 배포 명령어 (SSM RunCommand)"
  value = join(" ", [
    "aws ssm send-command",
    "--instance-ids ${aws_instance.scraper.id}",
    "--document-name AWS-RunShellScript",
    "--parameters 'commands=[\"/usr/local/bin/deploy-scraper.sh\"]'",
    "--region ${var.aws_region}",
    "--output text --query 'Command.CommandId'"
  ])
}

output "network_summary" {
  description = "네트워크 구성 요약"
  value = {
    vpc_cidr         = var.vpc_cidr
    public_subnets   = aws_subnet.public[*].cidr_block
    private_subnets  = aws_subnet.private[*].cidr_block
    rds_accessible_from = [
      "App Runner VPC Connector (sg: ${aws_security_group.apprunner_connector.id})",
      "SSM Host (sg: ${aws_security_group.ssm_host.id})",
    ]
    bastion_enabled  = var.enable_bastion
  }
}
