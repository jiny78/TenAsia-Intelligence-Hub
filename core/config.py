"""
core/config.py — TenAsia Intelligence Hub 통합 설정

시크릿 로드 우선순위:
  1. AWS Secrets Manager  (ENVIRONMENT=production 일 때)
  2. 환경 변수 / .env 파일 (로컬 개발)

사용법:
    from core.config import settings

    key = settings.GEMINI_API_KEY
    url = settings.DATABASE_URL
    print(settings.is_production)

─────────────────────────────────────────────────────────────────
[Gemini API Kill Switch 아키텍처 가이드]

 문제: Google Gemini API는 자체 월별 비용 한도 기능이 없습니다.
       API 키를 탈취당하거나 루프 버그가 발생하면 요금이 폭증할 수 있습니다.

 해결 방법: SSM Parameter Store 기반 Kill Switch

 ┌──────────────────────────────────────────────────────────────┐
 │  SSM Parameter Store (Terraform으로 생성)                    │
 │    /tih/gemini/kill_switch     "false" | "true"              │
 │    /tih/gemini/monthly_tokens  현재 월 누적 토큰 수 (문자열) │
 └──────────────────────────────────────────────────────────────┘
         ↑ 읽기/쓰기                       ↑ 읽기/쓰기
 ┌───────────────┐               ┌──────────────────────┐
 │  앱 (App      │               │  수동 관리자 개입     │
 │  Runner)      │               │  aws ssm put-param.. │
 │  engine.py    │               └──────────────────────┘
 └───────────────┘

 자동 동작 흐름:
   1. engine.py 가 Gemini API 호출 전 check_gemini_kill_switch() 실행
   2. kill_switch == "true"  → GeminiKillSwitchError 발생, 프로세스 중단
   3. API 호출 성공 후 record_gemini_usage(token_count) 실행
   4. 누적 토큰이 GEMINI_MONTHLY_TOKEN_LIMIT 초과
      → kill_switch 를 "true" 로 자동 설정 + 로그 경고
   5. 매월 1일 EventBridge Rule → Lambda 가 monthly_tokens 를 "0" 으로 리셋
      → kill_switch 를 "false" 로 자동 복구

 Google Cloud 할당량 설정 (하드 리밋, 이중 방어):
   1. https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/quotas
   2. "GenerateContent requests per day" → 원하는 값으로 Override
   3. 이 설정은 앱 로직과 독립적으로 Google 서버에서 직접 차단

 토큰 → 비용 환산 기준 (2025년 기준, 변동 가능):
   gemini-2.0-flash  입력: $0.075/1M tokens  출력: $0.30/1M tokens
   gemini-1.5-flash  입력: $0.075/1M tokens  출력: $0.30/1M tokens
   예) 월 $5 예산 → 약 5,000,000 입력 토큰 또는 1,666,666 출력 토큰

 월별 리셋 Lambda (EventBridge Rule 예시):
   이벤트 패턴: cron(0 0 1 * ? *)  # 매월 1일 자정 UTC
   Lambda 코드:
     import boto3
     def handler(event, context):
         ssm = boto3.client('ssm', region_name='ap-northeast-2')
         ssm.put_parameter(Name='/tih/gemini/monthly_tokens',
                           Value='0', Overwrite=True)
         ssm.put_parameter(Name='/tih/gemini/kill_switch',
                           Value='false', Overwrite=True)

 수동 Kill Switch 조작 (CLI):
   # 즉시 중단
   aws ssm put-parameter --name /tih/gemini/kill_switch \\
     --value "true" --type String --overwrite --region ap-northeast-2

   # 재개
   aws ssm put-parameter --name /tih/gemini/kill_switch \\
     --value "false" --type String --overwrite --region ap-northeast-2

   # 현재 사용량 확인
   aws ssm get-parameter --name /tih/gemini/monthly_tokens \\
     --region ap-northeast-2 --query Parameter.Value --output text
─────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# -------------------------------------------------------
# Secrets Manager 헬퍼
# -------------------------------------------------------

def _fetch_secret(secret_id: str, region: str) -> dict[str, Any]:
    """Secrets Manager 에서 JSON 시크릿을 가져옵니다. 실패 시 빈 딕셔너리 반환."""
    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
        return json.loads(raw)
    except ImportError:
        logger.debug("boto3 미설치 — 환경 변수로 대체합니다.")
        return {}
    except Exception as exc:
        logger.debug("Secrets Manager 조회 실패 [%s]: %s", secret_id, exc)
        return {}


def _load_secrets(region: str) -> dict[str, Any]:
    """프로젝트 시크릿 2종을 일괄 로드합니다."""
    combined: dict[str, Any] = {}
    for key in ("GEMINI_API_KEY", "DATABASE_URL"):
        combined.update(_fetch_secret(f"tih/{key}", region))
    return combined


# -------------------------------------------------------
# 설정 데이터클래스
# -------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    # ── 민감 정보 ────────────────────────────────────────
    GEMINI_API_KEY: str
    DATABASE_URL: str

    # ── AWS ──────────────────────────────────────────────
    AWS_REGION: str      = "ap-northeast-2"
    S3_BUCKET_NAME: str  = "tenasia-thumbnails"

    # ── 배포 환경 ─────────────────────────────────────────
    ENVIRONMENT: str = "development"

    # ── 모델 ──────────────────────────────────────────────
    ARTICLE_MODEL: str  = "gemini-2.0-flash"
    VIDEO_MODEL: str    = "gemini-1.5-flash"
    FALLBACK_MODEL: str = "gemini-1.5-flash"

    # ── API 동작 ──────────────────────────────────────────
    MAX_RETRIES: int    = 3
    API_TIMEOUT: int    = 120   # 초
    BASE_WAIT_TIME: int = 2     # 지수 백오프 기본 대기(초)

    # ── 비디오 처리 ───────────────────────────────────────
    MAX_FRAMES: int      = 10
    FRAME_INTERVAL: int  = 5    # 초
    MAX_VIDEO_LENGTH: int = 300  # 초

    # ── SNS 플랫폼 문자 제한 ──────────────────────────────
    PLATFORM_LIMITS: dict = field(default_factory=lambda: {
        "x":         {"max_chars": 280,  "recommended_min": 140, "recommended_max": 200},
        "instagram": {"max_chars": 2200, "recommended_min": 500, "hashtag_count": 10},
        "threads":   {"max_chars": 500,  "recommended_chars": 300},
    })

    # ── 언어 ──────────────────────────────────────────────
    SUPPORTED_LANGUAGES: list = field(default_factory=lambda: ["kr", "en"])

    # ── 품질 검증 ─────────────────────────────────────────
    QUALITY_CHECKPOINTS: list = field(default_factory=lambda: [
        "팩트 체크: 기사 본문의 정보와 100% 일치하는가?",
        "품격 유지: 브랜드 이미지에 맞는 고급스러운 어휘를 사용했는가?",
        "자연스러운 현지화: 번역투가 아닌 현지 인플루언서의 말투인가?",
    ])

    # ── 로깅 ──────────────────────────────────────────────
    LOG_LEVEL: str  = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # ── Gemini Kill Switch ─────────────────────────────────
    # SSM Parameter Store 경로 (Terraform main.tf 에서 생성)
    GEMINI_KILL_SWITCH_SSM:    str = "/tih/gemini/kill_switch"
    GEMINI_MONTHLY_TOKENS_SSM: str = "/tih/gemini/monthly_tokens"
    # 월 토큰 한도 초과 시 kill_switch 를 자동 활성화합니다.
    # gemini-2.0-flash 기준 2,000,000 토큰 ≈ 약 $0.75 (입력 토큰 기준)
    # 예산에 맞게 조정:  $5→5_000_000 / $10→10_000_000 / $20→20_000_000
    GEMINI_MONTHLY_TOKEN_LIMIT: int = 2_000_000

    # ── 편의 프로퍼티 ─────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def s3_base_url(self) -> str:
        return f"https://{self.S3_BUCKET_NAME}.s3.{self.AWS_REGION}.amazonaws.com"


# -------------------------------------------------------
# 싱글톤 팩토리
# -------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Settings 싱글톤을 반환합니다.

    - production: Secrets Manager 우선 → 환경 변수 fallback
    - 그 외: 환경 변수 / .env 만 사용
    """
    env    = os.getenv("ENVIRONMENT", "development")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    secrets: dict[str, Any] = {}
    if env == "production":
        logger.info("Secrets Manager에서 시크릿 로드 중...")
        secrets = _load_secrets(region)

    def resolve(key: str) -> str:
        """환경 변수 → Secrets Manager 순으로 값 탐색"""
        return os.getenv(key) or secrets.get(key, "")

    return Settings(
        GEMINI_API_KEY = resolve("GEMINI_API_KEY") or resolve("GOOGLE_API_KEY"),
        DATABASE_URL   = resolve("DATABASE_URL"),
        AWS_REGION     = region,
        S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "tenasia-thumbnails"),
        ENVIRONMENT    = env,
    )


# 모듈 레벨 싱글톤
settings = get_settings()


# -------------------------------------------------------
# 시작 시 필수 값 검증
# -------------------------------------------------------

def validate_settings() -> None:
    """앱 시작 시 호출하여 필수 설정이 모두 있는지 확인합니다."""
    s = get_settings()
    missing = []

    if not s.GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if s.is_production and not s.DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        raise ValueError(
            f"필수 시크릿 누락: {', '.join(missing)}\n"
            "  운영: aws secretsmanager put-secret-value --secret-id tih/<KEY> ...\n"
            "  로컬: .env 파일에 KEY=value 형식으로 추가"
        )

    logger.info(
        "설정 로드 완료 | env=%s | Gemini=%s | DB=%s",
        s.ENVIRONMENT,
        "OK" if s.GEMINI_API_KEY else "MISSING",
        "OK" if s.DATABASE_URL   else "MISSING",
    )


# -------------------------------------------------------
# Gemini API Kill Switch 구현
#
# [engine.py 적용 방법]
#
#   from core.config import check_gemini_kill_switch, record_gemini_usage
#
#   # API 호출 직전
#   check_gemini_kill_switch()          # 차단 상태면 GeminiKillSwitchError 발생
#
#   response = model.generate_content(...)
#
#   # 호출 성공 후 토큰 기록
#   used = response.usage_metadata.total_token_count
#   record_gemini_usage(used)           # 누적 초과 시 kill_switch 자동 활성화
# -------------------------------------------------------

class GeminiKillSwitchError(RuntimeError):
    """Kill Switch 활성화 상태에서 Gemini API 호출 시 발생합니다."""
    pass


def _ssm_get(param_name: str, region: str) -> Optional[str]:
    """SSM Parameter Store 에서 값을 읽습니다. 실패 시 None 반환."""
    try:
        import boto3
        client = boto3.client("ssm", region_name=region)
        return client.get_parameter(Name=param_name)["Parameter"]["Value"]
    except Exception as exc:
        logger.debug("SSM get 실패 [%s]: %s", param_name, exc)
        return None


def _ssm_put(param_name: str, value: str, region: str) -> bool:
    """SSM Parameter Store 에 값을 씁니다. 성공 시 True 반환."""
    try:
        import boto3
        client = boto3.client("ssm", region_name=region)
        client.put_parameter(Name=param_name, Value=value, Overwrite=True)
        return True
    except Exception as exc:
        logger.warning("SSM put 실패 [%s]: %s", param_name, exc)
        return False


def check_gemini_kill_switch() -> None:
    """
    Gemini API 호출 전 Kill Switch 상태를 확인합니다.

    - kill_switch == "true"  → GeminiKillSwitchError 발생 (API 호출 중단)
    - kill_switch == "false" → 정상 통과
    - SSM 조회 실패 (로컬 개발) → 경고만 출력하고 통과

    사용 예시:
        check_gemini_kill_switch()
        response = model.generate_content(prompt)
    """
    s = get_settings()

    # 로컬/개발 환경에서는 Kill Switch 비활성화
    if not s.is_production:
        return

    flag = _ssm_get(s.GEMINI_KILL_SWITCH_SSM, s.AWS_REGION)

    if flag is None:
        logger.warning(
            "Kill Switch SSM 파라미터를 읽을 수 없습니다 [%s]. "
            "IAM 권한(ssm:GetParameter)을 확인하세요. 이번 호출은 허용합니다.",
            s.GEMINI_KILL_SWITCH_SSM,
        )
        return

    if flag.strip().lower() == "true":
        raise GeminiKillSwitchError(
            "Gemini API Kill Switch 가 활성화되어 있습니다.\n"
            f"  월 토큰 한도({s.GEMINI_MONTHLY_TOKEN_LIMIT:,}) 초과 또는 수동 설정.\n"
            "  재개: aws ssm put-parameter --name /tih/gemini/kill_switch "
            "--value 'false' --type String --overwrite"
        )


def record_gemini_usage(token_count: int) -> None:
    """
    API 호출 후 토큰 사용량을 SSM 에 누적하고, 한도 초과 시 Kill Switch 를 활성화합니다.

    Args:
        token_count: 이번 API 호출에서 사용된 총 토큰 수
                     (response.usage_metadata.total_token_count)

    사용 예시:
        response = model.generate_content(prompt)
        record_gemini_usage(response.usage_metadata.total_token_count)
    """
    s = get_settings()

    if not s.is_production:
        return  # 로컬 환경에서는 추적하지 않음

    # 현재 누적 토큰 읽기
    current_str = _ssm_get(s.GEMINI_MONTHLY_TOKENS_SSM, s.AWS_REGION) or "0"
    try:
        current_total = int(current_str) + token_count
    except ValueError:
        current_total = token_count

    # 누적값 저장
    _ssm_put(s.GEMINI_MONTHLY_TOKENS_SSM, str(current_total), s.AWS_REGION)

    logger.debug(
        "Gemini 토큰 사용량 기록 | 이번 호출: %d | 월 누적: %d / %d",
        token_count,
        current_total,
        s.GEMINI_MONTHLY_TOKEN_LIMIT,
    )

    # 한도 초과 시 Kill Switch 자동 활성화
    if current_total >= s.GEMINI_MONTHLY_TOKEN_LIMIT:
        _ssm_put(s.GEMINI_KILL_SWITCH_SSM, "true", s.AWS_REGION)
        logger.error(
            "⚠️  Gemini 월 토큰 한도 초과! Kill Switch 활성화됨.\n"
            "  누적: %d tokens / 한도: %d tokens\n"
            "  모든 Gemini API 호출이 중단됩니다.\n"
            "  재개하려면: aws ssm put-parameter "
            "--name /tih/gemini/kill_switch --value 'false' --type String --overwrite",
            current_total,
            s.GEMINI_MONTHLY_TOKEN_LIMIT,
        )


def get_gemini_usage_status() -> dict:
    """현재 Gemini API 사용 현황을 딕셔너리로 반환합니다."""
    s = get_settings()
    region = s.AWS_REGION

    kill_switch  = _ssm_get(s.GEMINI_KILL_SWITCH_SSM,    region) or "unknown"
    monthly_used = _ssm_get(s.GEMINI_MONTHLY_TOKENS_SSM, region) or "0"

    try:
        used_int = int(monthly_used)
    except ValueError:
        used_int = 0

    return {
        "kill_switch_active": kill_switch.strip().lower() == "true",
        "monthly_tokens_used": used_int,
        "monthly_token_limit": s.GEMINI_MONTHLY_TOKEN_LIMIT,
        "usage_percent": round(used_int / s.GEMINI_MONTHLY_TOKEN_LIMIT * 100, 1),
    }
