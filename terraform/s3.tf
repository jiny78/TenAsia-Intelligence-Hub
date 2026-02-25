# -------------------------------------------------------
# S3 Bucket: tenasia-thumbnails
# 퍼블릭 읽기(GetObject)만 허용, 쓰기는 IAM Role 만 가능
# -------------------------------------------------------

resource "aws_s3_bucket" "thumbnails" {
  bucket = "tenasia-thumbnails"
  tags   = { Name = "tenasia-thumbnails", Purpose = "썸네일 퍼블릭 호스팅" }
}

# 버전 관리 (삭제 복구용)
resource "aws_s3_bucket_versioning" "thumbnails" {
  bucket = aws_s3_bucket.thumbnails.id
  versioning_configuration { status = "Enabled" }
}

# 서버 측 암호화
resource "aws_s3_bucket_server_side_encryption_configuration" "thumbnails" {
  bucket = aws_s3_bucket.thumbnails.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# 퍼블릭 액세스 제어
# - ACL 기반 접근은 차단
# - 버킷 정책(Policy) 기반 퍼블릭 읽기는 허용
resource "aws_s3_bucket_public_access_block" "thumbnails" {
  bucket                  = aws_s3_bucket.thumbnails.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false  # 정책 기반 접근 허용
  restrict_public_buckets = false
}

# 퍼블릭 읽기 정책 (GetObject 전용)
resource "aws_s3_bucket_policy" "thumbnails_public_read" {
  bucket     = aws_s3_bucket.thumbnails.id
  depends_on = [aws_s3_bucket_public_access_block.thumbnails]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.thumbnails.arn}/*"
      }
    ]
  })
}

# CORS (웹 브라우저 직접 접근 허용)
resource "aws_s3_bucket_cors_configuration" "thumbnails" {
  bucket = aws_s3_bucket.thumbnails.id
  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    max_age_seconds = 3600
  }
}

# 수명 주기 정책
resource "aws_s3_bucket_lifecycle_configuration" "thumbnails" {
  bucket = aws_s3_bucket.thumbnails.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    noncurrent_version_expiration { noncurrent_days = 30 }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
