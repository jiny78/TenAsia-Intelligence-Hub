# -------------------------------------------------------
# RDS PostgreSQL 15  |  db.t4g.micro  |  퍼블릭 액세스 없음
# -------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name        = "${local.name}-db-subnet-group"
  description = "프라이빗 서브넷 — RDS 전용"
  subnet_ids  = aws_subnet.private[*].id
  tags        = { Name = "${local.name}-db-subnet-group" }
}

resource "aws_db_parameter_group" "pg15" {
  name        = "${local.name}-pg15"
  family      = "postgres15"
  description = "PostgreSQL 15 파라미터"

  parameter { name = "log_connections";    value = "1" }
  parameter { name = "log_disconnections"; value = "1" }

  tags = { Name = "${local.name}-pg15-params" }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name}-db"

  # 엔진
  engine         = "postgres"
  engine_version = "15.10"
  instance_class = "db.t4g.micro"

  # 스토리지
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  # 접속 정보
  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  # 네트워크 — 퍼블릭 액세스 명시적 차단
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  parameter_group_name = aws_db_parameter_group.pg15.name

  # 백업
  backup_retention_period   = 7
  backup_window             = "03:00-04:00"
  maintenance_window        = "Mon:04:00-Mon:05:00"
  copy_tags_to_snapshot     = true
  auto_minor_version_upgrade = true

  # 운영 환경: 삭제 보호 ON
  deletion_protection = var.environment == "production"
  skip_final_snapshot = var.environment != "production"
  final_snapshot_identifier = var.environment == "production" ? "${local.name}-final-snapshot" : null

  # Enhanced Monitoring (60초 간격)
  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_monitoring.arn

  # t4g.micro 는 Performance Insights 미지원
  performance_insights_enabled = false

  tags = { Name = "${local.name}-db" }
}

# Enhanced Monitoring Role
resource "aws_iam_role" "rds_monitoring" {
  name = "${local.name}-rds-monitoring-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
