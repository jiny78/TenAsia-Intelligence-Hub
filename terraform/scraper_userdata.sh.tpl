#!/bin/bash
# =============================================================
# TenAsia Intelligence Hub — EC2 Scraper 초기화 스크립트
# Amazon Linux 2023 ARM64 (t4g.micro)
# =============================================================
set -euo pipefail

INSTANCE_NAME="${instance_name}"
AWS_REGION="${aws_region}"
APP_DIR="/app"
SERVICE_NAME="tih-scraper"

echo "[$(date)] 초기화 시작: $INSTANCE_NAME"

# ── 시스템 패키지 ─────────────────────────────────────────────
dnf update -y
dnf install -y \
    python3.11 python3.11-pip \
    git \
    gcc python3.11-devel libpq-devel \
    ffmpeg \
    jq

# Python 3.11을 기본으로 설정
alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
alternatives --set python3 /usr/bin/python3.11
ln -sf /usr/bin/python3.11 /usr/local/bin/python
ln -sf /usr/bin/pip3.11   /usr/local/bin/pip

# ── SSM Agent 확인 (AL2023 기본 설치) ─────────────────────────
systemctl enable amazon-ssm-agent
systemctl start  amazon-ssm-agent

# ── 앱 디렉토리 구성 ──────────────────────────────────────────
mkdir -p $APP_DIR
cd $APP_DIR

# GitHub에서 코드 클론 (첫 부팅 시)
# 실제 레포 URL은 SSM deploy 단계에서 git pull로 최신화됩니다
# git clone https://github.com/YOUR_ORG/TenAsia-Intelligence-Hub.git .

# 의존성 설치 스크립트 (배포 시 SSM이 실행)
cat > /usr/local/bin/deploy-scraper.sh << 'DEPLOY_EOF'
#!/bin/bash
set -euo pipefail
APP_DIR="/app"
SERVICE_NAME="tih-scraper"

echo "[deploy] 코드 최신화 중..."
cd $APP_DIR
git pull origin main

echo "[deploy] 의존성 설치 중..."
pip install --quiet -r requirements.txt

echo "[deploy] 서비스 재시작 중..."
systemctl restart $SERVICE_NAME

echo "[deploy] 완료: $(date)"
DEPLOY_EOF
chmod +x /usr/local/bin/deploy-scraper.sh

# ── systemd 서비스 등록 ───────────────────────────────────────
cat > /etc/systemd/system/$SERVICE_NAME.service << SERVICE_EOF
[Unit]
Description=TenAsia Intelligence Hub — Background Scraper Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=$APP_DIR
ExecStart=/usr/local/bin/python -m scraper.worker
Restart=always
RestartSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# 환경 변수 — Secrets Manager에서 로드 (core/config.py가 처리)
Environment="ENVIRONMENT=production"
Environment="AWS_REGION=$AWS_REGION"
Environment="PYTHONUNBUFFERED=1"

# 리소스 제한
TimeoutStartSec=60
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE_EOF

systemctl daemon-reload
# 코드가 배포된 후 GitHub Actions에서 systemctl enable + start 실행
# systemctl enable $SERVICE_NAME
# systemctl start  $SERVICE_NAME

echo "[$(date)] 초기화 완료. 코드 배포 후 서비스가 시작됩니다."
