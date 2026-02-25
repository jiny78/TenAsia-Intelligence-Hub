# =============================================================
# VPC — 2-Tier 네트워크 설계
#
#  Public  Subnet (10.10.10.x / 11.x)  ← 웹 서버 / NAT GW / Bastion
#  Private Subnet (10.10.0.x  /  1.x)  ← RDS / SSM Host (인터넷 노출 없음)
#
#  트래픽 흐름:
#    인터넷 → IGW → Public Subnet
#    Public  → NAT GW → Private Subnet (아웃바운드만)
#    Private ← NAT GW ← 인터넷 (인바운드 차단)
#
#  보안 그룹 체계:
#    sg-apprunner-connector  앱 → RDS (5432)
#    sg-ssm-host             관리자 DB 접근 (인바운드 완전 차단)
#    sg-bastion              (선택) SSH 22, 관리자 IP 전용
#    sg-rds                  5432 인바운드: 위 3개 SG만 허용
# =============================================================

locals {
  name = "tih"
}

# ─────────────────────────────────────────────────────────────
# VPC
# ─────────────────────────────────────────────────────────────
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "${local.name}-vpc" }
}

# ─────────────────────────────────────────────────────────────
# 서브넷
# ─────────────────────────────────────────────────────────────

# [Public Tier] 웹 서버 / NAT GW / Bastion Host
resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name}-public-${var.azs[count.index]}"
    Tier = "Public"
    Use  = "Web/NAT/Bastion"
  }
}

# [Private Tier] RDS DB / SSM Host — 인터넷 직접 노출 없음
resource "aws_subnet" "private" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = var.azs[count.index]

  tags = {
    Name = "${local.name}-private-${var.azs[count.index]}"
    Tier = "Private"
    Use  = "DB/SSM"
  }
}

# ─────────────────────────────────────────────────────────────
# 인터넷 게이트웨이 & NAT Gateway
# ─────────────────────────────────────────────────────────────
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.main]
  tags       = { Name = "${local.name}-nat-eip" }
}

# NAT GW: Public Subnet에 배치 → Private의 아웃바운드 허용
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${local.name}-nat" }
  depends_on    = [aws_internet_gateway.main]
}

# ─────────────────────────────────────────────────────────────
# 라우팅 테이블
# ─────────────────────────────────────────────────────────────

# Public: IGW 경유 직접 인터넷
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private: NAT GW 경유 아웃바운드 (인터넷 인바운드 불가)
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# =============================================================
# 보안 그룹
# =============================================================

# ── SG 1. App Runner VPC Connector ───────────────────────────
#
#  역할: App Runner 관리형 서비스가 Private Subnet의 RDS에 접근
#  인바운드: 없음 (App Runner 내부 관리)
#  아웃바운드: VPC 내 5432(RDS) + 외부 443(AWS 서비스)
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "apprunner_connector" {
  name        = "${local.name}-apprunner-connector-sg"
  description = "App Runner VPC Connector — RDS 아웃바운드 전용"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "PostgreSQL → RDS (VPC 내부만)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "HTTPS → AWS 서비스 (Secrets Manager, SSM 등)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-apprunner-connector-sg" }
}

# ── SG 2. SSM Session Manager Host ───────────────────────────
#
#  역할: 관리자가 SSH 키 없이 DB에 접근하기 위한 프록시 EC2
#  핵심 보안: 인바운드 포트 완전 차단 (SSM은 아웃바운드 HTTPS만 사용)
#  아웃바운드: AWS SSM 엔드포인트 443 + RDS 5432
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "ssm_host" {
  name        = "${local.name}-ssm-host-sg"
  description = "SSM Host — 인바운드 완전 차단, SSM 아웃바운드 + DB 접근만"
  vpc_id      = aws_vpc.main.id

  # 인바운드 규칙 없음 — 보안 핵심

  egress {
    description = "SSM Agent → AWS SSM 서비스 엔드포인트"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL → RDS (VPC 내부만)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${local.name}-ssm-host-sg" }
}

# ── SG 3. Bastion Host (enable_bastion = true 일 때만 생성) ──
#
#  역할: SSH를 통한 DB 관리 (SSM 대안)
#  인바운드: 관리자 IP(var.admin_ip_cidr)에서만 22번 포트 허용
#  아웃바운드: RDS 5432 + HTTPS 443
#
#  ⚠️  평소에는 enable_bastion = false 유지 권장
#      필요할 때만 true → terraform apply 로 즉시 생성
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "bastion" {
  count       = var.enable_bastion ? 1 : 0
  name        = "${local.name}-bastion-sg"
  description = "Bastion Host — 관리자 IP(${var.admin_ip_cidr})에서만 SSH 허용"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from admin IP only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_ip_cidr]
  }

  egress {
    description = "PostgreSQL → RDS"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "HTTPS (패키지 업데이트)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-bastion-sg" }
}

# ── SG 4. EC2 Background Scraper ─────────────────────────────
#
#  역할: 장시간 스크래핑 루프 EC2 (App Runner와 분리)
#  인바운드: 없음 (SSM RunCommand 수신은 아웃바운드 HTTPS로 처리)
#  아웃바운드: RDS 5432 + 인터넷 스크래핑(80/443) + SSM 443
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "scraper" {
  name        = "${local.name}-scraper-sg"
  description = "EC2 Scraper — 인바운드 없음, 스크래핑 아웃바운드만"
  vpc_id      = aws_vpc.main.id

  # 인바운드 규칙 없음 — SSM은 아웃바운드 HTTPS만 사용

  egress {
    description = "PostgreSQL → RDS"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "HTTPS → AWS 서비스(SSM, Secrets Manager) + 웹 스크래핑"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP → 웹 스크래핑"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-scraper-sg" }
}

# ── SG 5. RDS PostgreSQL ──────────────────────────────────────
#
#  5432 인바운드: 위 3개 SG에서만 허용 (IP 기반 아님 — SG 참조 방식)
#  퍼블릭 인터넷 → RDS 직접 접근 완전 불가
# ─────────────────────────────────────────────────────────────
resource "aws_security_group" "rds" {
  name        = "${local.name}-rds-sg"
  description = "RDS — App Runner + EC2 Scraper + SSM Host + Bastion SG만 5432 허용"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL ← App Runner VPC Connector"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.apprunner_connector.id]
  }

  ingress {
    description     = "PostgreSQL ← EC2 Background Scraper"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.scraper.id]
  }

  ingress {
    description     = "PostgreSQL ← SSM Session Manager Host"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ssm_host.id]
  }

  # Bastion 활성화 시에만 추가 ingress (dynamic block)
  dynamic "ingress" {
    for_each = var.enable_bastion ? [1] : []
    content {
      description     = "PostgreSQL ← Bastion Host"
      from_port       = 5432
      to_port         = 5432
      protocol        = "tcp"
      security_groups = [aws_security_group.bastion[0].id]
    }
  }

  egress {
    description = "RDS 응답 아웃바운드"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-rds-sg" }
}
