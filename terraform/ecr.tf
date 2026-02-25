# -------------------------------------------------------
# ECR — Docker 이미지 레지스트리
# -------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "tenasia-intelligence-hub"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = "tenasia-intelligence-hub-ecr" }
}

# 최신 10개 이미지만 유지 (비용 절감)
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "최신 10개 이미지만 보관"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
