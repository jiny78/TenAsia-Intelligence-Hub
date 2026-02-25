# =============================================================
# 관리자 DB 접근 — 두 가지 방식
#
#  [권장] SSM Session Manager Host  (항상 실행 / 인바운드 포트 없음)
#  [선택] Bastion Host               (enable_bastion = true 일 때만)
#
# ─────────────────────────────────────────────────────────────
# [SSM 방식 — DB 접속 절차]
#
#  사전 준비: AWS CLI + Session Manager Plugin 설치
#    https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
#
#  1. RDS 엔드포인트 확인
#     RDS_HOST=$(cd terraform && terraform output -raw rds_endpoint | cut -d: -f1)
#
#  2. SSM 포트 포워딩 터널 열기 (백그라운드)
#     aws ssm start-session \
#       --target $(terraform output -raw ssm_host_instance_id) \
#       --document-name AWS-StartPortForwardingSessionToRemoteHost \
#       --parameters "{\"host\":[\"$RDS_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"15432\"]}" \
#       --region ap-northeast-2
#
#  3. 새 터미널에서 psql 연결
#     psql -h localhost -p 15432 -U tih_admin -d tih
#
# ─────────────────────────────────────────────────────────────
# [Bastion 방식 — DB 접속 절차]
#
#  1. SSH 터널 열기
#     ssh -i ~/.ssh/tih-bastion \
#         -L 15432:<RDS_ENDPOINT>:5432 \
#         ec2-user@<BASTION_PUBLIC_IP> -N &
#
#  2. psql 연결
#     psql -h localhost -p 15432 -U tih_admin -d tih
# =============================================================

# ─────────────────────────────────────────────────────────────
# 공통: 최신 Amazon Linux 2023 ARM64 AMI
# ─────────────────────────────────────────────────────────────
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# =============================================================
# [권장] SSM Session Manager Host
#
#  위치: Private Subnet (인터넷 직접 노출 없음)
#  접근: SSM Agent 아웃바운드 HTTPS → NAT GW → AWS SSM 서비스
#  인스턴스: t4g.nano (≈ $3.7/월)  SSH 키: 없음
# =============================================================

resource "aws_iam_role" "ssm_host" {
  name        = "${local.name}-ssm-host-role"
  description = "SSM Session Manager Host — AmazonSSMManagedInstanceCore만 부여"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# SSM 연결에 필요한 최소 권한 (StartSession, TerminateSession, SendCommand 포함)
resource "aws_iam_role_policy_attachment" "ssm_host_core" {
  role       = aws_iam_role.ssm_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ssm_host" {
  name = "${local.name}-ssm-host-profile"
  role = aws_iam_role.ssm_host.name
}

resource "aws_instance" "ssm_host" {
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = "t4g.nano"
  subnet_id              = aws_subnet.private[0].id
  iam_instance_profile   = aws_iam_instance_profile.ssm_host.name
  vpc_security_group_ids = [aws_security_group.ssm_host.id]

  # SSH 키 없음 — SSM 전용 접근 (보안 강화)
  key_name = null

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    delete_on_termination = true
    encrypted             = true
  }

  # IMDSv2 강제 (SSRF 방어)
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOF
    #!/bin/bash
    # Amazon Linux 2023은 SSM Agent가 기본 설치됨
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
  EOF
  )

  tags = { Name = "${local.name}-ssm-host" }

  depends_on = [aws_nat_gateway.main]  # NAT GW 없으면 SSM 연결 불가
}

# =============================================================
# [선택] Bastion Host — enable_bastion = true 일 때만 생성
#
#  위치: Public Subnet (퍼블릭 IP 자동 할당)
#  접근: SSH 22번 포트, var.admin_ip_cidr 에서만
#  인스턴스: t4g.nano (사용 시에만 켜두는 것을 권장)
# =============================================================

# SSH 공개키 등록 (public_key_path 가 설정된 경우)
resource "aws_key_pair" "bastion" {
  count      = var.enable_bastion && var.public_key_path != "" ? 1 : 0
  key_name   = "${local.name}-bastion-key"
  public_key = file(var.public_key_path)
  tags       = { Name = "${local.name}-bastion-key" }
}

resource "aws_instance" "bastion" {
  count                       = var.enable_bastion ? 1 : 0
  ami                         = data.aws_ami.al2023_arm64.id
  instance_type               = "t4g.nano"
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.bastion[0].id]
  associate_public_ip_address = true

  key_name = (
    var.enable_bastion && var.public_key_path != ""
    ? aws_key_pair.bastion[0].key_name
    : null
  )

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = { Name = "${local.name}-bastion" }
}
