# =============================================================
# EC2 Background Scraper — t4g.micro (Private Subnet)
#
#  역할: 무한 루프 스크래핑 워커. 긴 실행 시간 / 비용 효율 목적.
#  배치: Private Subnet (RDS 직접 접근 + NAT 경유 인터넷 스크래핑)
#  관리: SSM Session Manager (SSH 키 없음)
#  코드 배포: GitHub Actions → SSM RunCommand → git pull + restart
#
#  월 비용 (ap-northeast-2 기준):
#    t4g.micro On-Demand ≈ $0.0104/hr → $7.5/월
#    t4g.micro Savings Plan 1yr no-upfront ≈ $4.6/월
# =============================================================

# ─────────────────────────────────────────────────────────────
# IAM Role — Scraper EC2 전용
# ─────────────────────────────────────────────────────────────
resource "aws_iam_role" "scraper" {
  name        = "${local.name}-scraper-role"
  description = "EC2 Scraper — SSM + S3 + Secrets Manager + SSM Kill Switch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# SSM Session Manager + RunCommand 수신
resource "aws_iam_role_policy_attachment" "scraper_ssm_core" {
  role       = aws_iam_role.scraper.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# S3 썸네일 버킷 (스크래퍼가 이미지 업로드)
resource "aws_iam_role_policy_attachment" "scraper_s3" {
  role       = aws_iam_role.scraper.name
  policy_arn = aws_iam_policy.s3_thumbnails.arn
}

# Secrets Manager — API 키 / DB URL 읽기
resource "aws_iam_role_policy_attachment" "scraper_secrets" {
  role       = aws_iam_role.scraper.name
  policy_arn = aws_iam_policy.secrets_read.arn
}

# SSM Kill Switch 파라미터 읽기·쓰기
resource "aws_iam_role_policy_attachment" "scraper_ssm_params" {
  role       = aws_iam_role.scraper.name
  policy_arn = aws_iam_policy.ssm_kill_switch.arn
}

resource "aws_iam_instance_profile" "scraper" {
  name = "${local.name}-scraper-profile"
  role = aws_iam_role.scraper.name
}

# ─────────────────────────────────────────────────────────────
# EC2 Scraper Instance
# ─────────────────────────────────────────────────────────────
resource "aws_instance" "scraper" {
  ami                    = data.aws_ami.al2023_arm64.id  # bastion.tf에 정의
  instance_type          = "t4g.micro"
  subnet_id              = aws_subnet.private[0].id
  iam_instance_profile   = aws_iam_instance_profile.scraper.name
  vpc_security_group_ids = [aws_security_group.scraper.id]

  # SSH 키 없음 — SSM RunCommand만 사용
  key_name = null

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20   # 스크래핑 임시 파일 공간
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"   # IMDSv2 강제
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(templatefile("${path.module}/scraper_userdata.sh.tpl", {
    aws_region    = var.aws_region
    instance_name = "${local.name}-scraper"
  }))

  tags = { Name = "${local.name}-scraper" }

  # 의존: NAT GW (인터넷 접근), 인스턴스 프로필
  depends_on = [
    aws_nat_gateway.main,
    aws_iam_instance_profile.scraper,
  ]
}
