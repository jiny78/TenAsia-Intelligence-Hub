# main 브랜치 보호 규칙 설정 가이드

이 문서는 `main` 브랜치에 **직접 push를 차단**하고,
반드시 **Pull Request(PR)** 를 통해서만 코드가 병합되도록 설정하는 방법을 안내합니다.

---

## 방법 1 — GitHub 웹 UI (권장, 간편)

1. **레포지토리 페이지** → `Settings` 탭 → 왼쪽 메뉴 `Branches`
2. **"Add branch protection rule"** 클릭
3. **Branch name pattern**: `main` 입력
4. 아래 항목 체크:

   | 옵션 | 설명 |
   |------|------|
   | ✅ Require a pull request before merging | PR 없이 직접 push 차단 |
   | ✅ Require approvals: **1** | 최소 1명 리뷰 필수 |
   | ✅ Dismiss stale pull request approvals when new commits are pushed | 새 커밋 시 이전 승인 무효화 |
   | ✅ Do not allow bypassing the above settings | 관리자도 규칙 우회 불가 |
   | ✅ Restrict who can push to matching branches | 직접 push 가능 계정 제한 |
   | ✅ Allow force pushes: **비활성화** | 강제 push 차단 |
   | ✅ Allow deletions: **비활성화** | 브랜치 삭제 차단 |

5. **"Create"** 클릭

---

## 방법 2 — GitHub REST API (PowerShell, Windows)

`GITHUB_TOKEN` 환경 변수에 `repo` 권한을 가진 Personal Access Token을 설정 후 실행:

```powershell
$token = $env:GITHUB_TOKEN   # GitHub PAT (repo 권한)
$owner = "jiny78"
$repo  = "TenAsia-Intelligence-Hub"
$branch = "main"

$body = @{
    required_status_checks        = $null
    enforce_admins                 = $true
    required_pull_request_reviews = @{
        required_approving_review_count = 1
        dismiss_stale_reviews           = $true
        require_code_owner_reviews      = $false
    }
    restrictions    = $null
    allow_force_pushes = $false
    allow_deletions    = $false
    block_creations    = $false
} | ConvertTo-Json -Depth 10

$headers = @{
    Authorization = "Bearer $token"
    Accept        = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
}

Invoke-RestMethod `
    -Uri "https://api.github.com/repos/$owner/$repo/branches/$branch/protection" `
    -Method Put `
    -Headers $headers `
    -Body $body `
    -ContentType "application/json"

Write-Host "✅ main 브랜치 보호 규칙 적용 완료"
```

---

## 방법 3 — GitHub CLI (gh 설치 후)

```bash
# gh CLI 설치: https://cli.github.com/
gh auth login

gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  /repos/jiny78/TenAsia-Intelligence-Hub/branches/main/protection \
  --field required_pull_request_reviews='{"required_approving_review_count":1,"dismiss_stale_reviews":true}' \
  --field enforce_admins=true \
  --field allow_force_pushes=false \
  --field allow_deletions=false
```

---

## 로컬 pre-push 훅 (보조 안전장치)

GitHub 설정과 별개로 **로컬 환경에서도** main 직접 push를 차단합니다.
`pre-commit`을 설치하면 `.pre-commit-config.yaml`의 `no-commit-to-branch` 훅이
`main`으로의 직접 커밋을 막아줍니다.

```bash
pip install pre-commit
pre-commit install                           # commit 훅
pre-commit install --hook-type pre-push      # push 훅 (추가 보호)
```

**수동 pre-push 훅 설치** (pre-commit 없이도 동작):

```bash
cat > .git/hooks/pre-push << 'EOF'
#!/bin/bash
# main 브랜치 직접 push 차단
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ]; then
    echo "❌ main 브랜치에 직접 push는 금지되어 있습니다."
    echo "   feature/* 또는 fix/* 브랜치를 만들고 Pull Request를 통해 병합하세요."
    exit 1
fi
EOF
chmod +x .git/hooks/pre-push
```

---

## 현재 보호 규칙 확인

```powershell
$token = $env:GITHUB_TOKEN
Invoke-RestMethod `
    -Uri "https://api.github.com/repos/jiny78/TenAsia-Intelligence-Hub/branches/main/protection" `
    -Headers @{
        Authorization = "Bearer $token"
        Accept = "application/vnd.github+json"
    }
```

---

## Rulesets (GitHub Enterprise / Free 2023+)

GitHub Free 플랜도 2023년부터 **Repository Rulesets** 를 지원합니다.
`Settings` → `Rules` → `Rulesets` → `New ruleset` 으로
더 세밀한 규칙(status check 필수, signed commit 강제 등)을 설정할 수 있습니다.
