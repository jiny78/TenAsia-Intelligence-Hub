# =============================================================
# VPC Interface Endpoints — 프라이빗 AWS 서비스 접근
#
# 목적:
#   EC2 Scraper / SSM Host 가 AWS 서비스(SSM, Secrets Manager, S3)에
#   접근할 때 NAT Gateway 를 거치지 않도록 VPC 내부 경로를 제공합니다.
#
# 비용 절감:
#   NAT Gateway 데이터 처리 비용: $0.045/GB
#   Interface Endpoint:          $0.01/hr × 2 AZ = $14.4/월 (고정)
#   → 월 320 GB 이상 전송 시 Interface Endpoint 가 유리
#
# 보안 강화:
#   인터넷 경유 없이 AWS 백본 네트워크만 사용
#
# 생성 리소스:
#   ┌───────────────────────────────────┬───────────┐
#   │ 서비스                             │ 타입      │
#   ├───────────────────────────────────┼───────────┤
#   │ com.amazonaws.*.ssm               │ Interface │
#   │ com.amazonaws.*.ssmmessages       │ Interface │
#   │ com.amazonaws.*.ec2messages       │ Interface │
#   │ com.amazonaws.*.secretsmanager    │ Interface │
#   │ com.amazonaws.*.s3                │ Gateway   │
#   └───────────────────────────────────┴───────────┘
# =============================================================

# ─────────────────────────────────────────────────────────────
# 보안 그룹 — VPC Endpoint 전용
# (프라이빗 서브넷 내 EC2 에서 443 인바운드 허용)
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "vpc_endpoint" {
  name        = "${local.name}-vpc-endpoint-sg"
  description = "VPC Interface Endpoints — 프라이빗 서브넷에서 HTTPS 인바운드"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from EC2 Scraper"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    security_groups = [aws_security_group.scraper.id]
  }

  ingress {
    description = "HTTPS from SSM Host"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    security_groups = [aws_security_group.ssm_host.id]
  }

  egress {
    description = "응답 아웃바운드"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-vpc-endpoint-sg" }
}

# ─────────────────────────────────────────────────────────────
# Interface Endpoints (Private DNS 활성화)
# ─────────────────────────────────────────────────────────────

# SSM Session Manager — RunCommand 수신 (EC2 → SSM 제어 채널)
resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoint.id]
  private_dns_enabled = true   # ssm.ap-northeast-2.amazonaws.com → 내부 IP

  tags = { Name = "${local.name}-endpoint-ssm" }
}

# SSM Messages — Session Manager 데이터 채널
resource "aws_vpc_endpoint" "ssm_messages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-endpoint-ssmmessages" }
}

# EC2 Messages — SSM Agent 필수 의존
resource "aws_vpc_endpoint" "ec2_messages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ec2messages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-endpoint-ec2messages" }
}

# Secrets Manager — EC2 Scraper 가 API 키/DB URL 읽기
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-endpoint-secretsmanager" }
}

# ─────────────────────────────────────────────────────────────
# Gateway Endpoint — S3 (데이터 전송 무료)
# ─────────────────────────────────────────────────────────────

# S3 Gateway Endpoint (라우팅 테이블 기반 — 추가 비용 없음)
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"   # Gateway 타입: 무료

  # 프라이빗 서브넷 라우팅 테이블에 S3 경로 추가
  route_table_ids = [aws_route_table.private.id]

  tags = { Name = "${local.name}-endpoint-s3" }
}
