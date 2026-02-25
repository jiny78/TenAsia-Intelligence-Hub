"""
core/logger.py — structlog 기반 구조화 로깅 설정

특징:
  - 개발 환경: 컬러 콘솔 출력 (가독성 우선)
  - 프로덕션 환경: JSON 형식 출력 (CloudWatch / 로그 수집기 호환)
  - 모든 로그에 타임스탬프, 레벨, 모듈명 자동 포함
  - request_id / job_id 등 컨텍스트 바인딩 지원

사용법:
    # 앱 시작 시 1회 호출
    from core.logger import configure_logging
    configure_logging()

    # 이후 어디서든
    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("작업 시작", job_id=42, url="https://...")
    logger.error("처리 실패", error=str(exc), retry=1)

    # 컨텍스트 바인딩 (요청 범위)
    bound_log = logger.bind(request_id="abc123")
    bound_log.info("처리 중")
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from pathlib import Path

import structlog

# 로그 디렉터리 (컨테이너 내 /app/logs 또는 로컬 ./logs)
_LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))

# ─────────────────────────────────────────────────────────────
# 공유 프로세서 체인
# ─────────────────────────────────────────────────────────────

_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,           # contextvars 바인딩 병합
    structlog.stdlib.add_logger_name,                  # 모듈명 추가
    structlog.stdlib.add_log_level,                    # 로그 레벨 추가
    structlog.stdlib.PositionalArgumentsFormatter(),   # % 포매팅 지원
    structlog.processors.TimeStamper(fmt="iso"),       # ISO 8601 타임스탬프
    structlog.processors.StackInfoRenderer(),          # 스택 정보 렌더링
]


def configure_logging(
    level: str | None = None,
    json_logs: bool | None = None,
    log_file: bool = True,
) -> None:
    """
    structlog + stdlib logging 을 통합 설정합니다.

    Args:
        level:     로그 레벨 문자열 (기본: 환경변수 LOG_LEVEL → "INFO")
        json_logs: True=JSON, False=컬러콘솔 (기본: 프로덕션이면 True)
        log_file:  파일 로그 활성화 여부 (logs/tih.log)
    """
    from core.config import settings

    log_level_str = level or settings.LOG_LEVEL
    log_level     = getattr(logging, log_level_str.upper(), logging.INFO)

    is_production = settings.ENVIRONMENT == "production"
    use_json      = json_logs if json_logs is not None else is_production

    # ── 표준 라이브러리 logging 설정 ──────────────────────────
    handlers: dict = {
        "console": {
            "class":     "logging.StreamHandler",
            "stream":    "ext://sys.stdout",
            "formatter": "plain",
        },
    }

    formatters: dict = {
        "plain": {"format": "%(message)s"},
    }

    if log_file:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class":       "logging.handlers.RotatingFileHandler",
            "filename":    str(_LOG_DIR / "tih.log"),
            "maxBytes":    10 * 1024 * 1024,   # 10 MB
            "backupCount": 5,
            "formatter":   "plain",
            "encoding":    "utf-8",
        }

    logging.config.dictConfig({
        "version":                  1,
        "disable_existing_loggers": False,
        "formatters":               formatters,
        "handlers":                 handlers,
        "root": {
            "level":    log_level_str.upper(),
            "handlers": list(handlers.keys()),
        },
        "loggers": {
            # 외부 라이브러리 로그 레벨 조정
            "uvicorn":         {"level": "INFO",    "propagate": True},
            "uvicorn.access":  {"level": "WARNING", "propagate": True},
            "sqlalchemy.engine": {
                "level":     "DEBUG" if not is_production else "WARNING",
                "propagate": True,
            },
            "httpx":    {"level": "WARNING", "propagate": True},
            "boto3":    {"level": "WARNING", "propagate": True},
            "botocore": {"level": "WARNING", "propagate": True},
        },
    })

    # ── structlog 렌더러 결정 ────────────────────────────────
    if use_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.processors.ExceptionRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # structlog → stdlib bridge (외부 라이브러리 로그도 structlog 처리)
    structlog.stdlib.recreate_defaults(log_level=log_level)

    log = structlog.get_logger(__name__)
    log.info(
        "로깅 초기화 완료",
        level=log_level_str.upper(),
        format="json" if use_json else "console",
        log_file=str(_LOG_DIR / "tih.log") if log_file else "disabled",
    )


def get_logger(name: str = __name__):
    """
    모듈별 structlog 로거를 반환합니다.

    Usage:
        logger = get_logger(__name__)
        logger.info("처리 완료", count=5)
    """
    return structlog.get_logger(name)
