"""
core/logger.py — TenAsia Intelligence Hub 구조화 로깅

아키텍처:
    structlog ──► stdlib.LoggerFactory ──► 두 개의 핸들러
                                           ├── StreamHandler     (콘솔)
                                           │     개발: 컬러 콘솔
                                           │     프로덕션: JSON
                                           └── RotatingFileHandler (파일)
                                                 항상 JSON (ELK 호환)
                                                 10 MB 초과 시 자동 교체
                                                 백업 최대 5개 유지

    ProcessorFormatter 가 각 핸들러에서 최종 렌더링을 담당합니다.
    외부 라이브러리(sqlalchemy, boto3 등) 로그도 동일한 파이프라인을 통과합니다.

Context Injection:
    모든 로그에 article_id / phase / job_id 가 자동으로 포함됩니다.
    Python contextvars 기반 — 스레드·비동기 양쪽에서 안전합니다.

ELK 호환 JSON 출력 예시:
    {
        "@timestamp":  "2026-02-25T10:00:00.000000Z",
        "level":       "INFO",
        "logger":      "scraper.worker",
        "message":     "아티클 처리 시작",
        "service":     "tih",
        "host":        "ip-10-0-1-5",
        "article_id":  42,
        "phase":       "Scraping",
        "job_id":      7,
        "worker_id":   "i-0abc123",
        "duration_ms": 312
    }

────────────────────────────────────────────────────────────────
빠른 시작:

    # 앱 시작 시 1회 호출
    from core.logger import configure_logging
    configure_logging()

    # 로거 획득
    from core.logger import get_logger, log_context, Phase
    logger = get_logger(__name__)

    # 컨텍스트 자동 주입 — with 블록 내 모든 로그에 적용
    with log_context(article_id=42, phase=Phase.SCRAPING, job_id=7):
        logger.info("스크래핑 시작")
        #  → {"message": "스크래핑 시작", "article_id": 42, "phase": "Scraping", ...}
        do_scrape(url)
        logger.info("완료", bytes=42300)   # 같은 컨텍스트 + 추가 키

    with log_context(article_id=42, phase=Phase.AI_PROCESSING):
        logger.info("Gemini 호출", model="gemini-2.0-flash")

────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

import structlog
from structlog.stdlib import ProcessorFormatter

# ─────────────────────────────────────────────────────────────
# 처리 단계 상수
# ─────────────────────────────────────────────────────────────

class Phase:
    """
    로그 컨텍스트에 사용하는 처리 단계 식별자.

    Usage:
        with log_context(phase=Phase.SCRAPING):
            logger.info("HTTP 요청")
    """
    SCRAPING      = "Scraping"       # HTTP 수집
    AI_PROCESSING = "AI Processing"  # Gemini API 호출
    DB_WRITE      = "DB Write"       # DB UPSERT
    S3_UPLOAD     = "S3 Upload"      # 이미지 업로드
    WORKER_LOOP   = "Worker Loop"    # EC2 워커 폴링 루프
    API_CALL      = "API Call"       # FastAPI 요청 처리
    INIT          = "Initialization" # 앱 초기화


# ─────────────────────────────────────────────────────────────
# 내부 상수
# ─────────────────────────────────────────────────────────────

_LOG_DIR  = Path(os.getenv("LOG_DIR", "logs"))
_HOSTNAME = socket.gethostname()
_SERVICE  = "tih"


# ─────────────────────────────────────────────────────────────
# 커스텀 structlog 프로세서
# ─────────────────────────────────────────────────────────────

def _add_service_context(
    logger: Any, method: str, event_dict: dict
) -> dict:
    """
    모든 로그에 서비스·호스트 메타를 자동 삽입합니다.

    ELK에서 멀티 서비스 로그를 구분할 때 사용합니다.
    """
    event_dict.setdefault("service", _SERVICE)
    event_dict.setdefault("host",    _HOSTNAME)
    return event_dict


def _rename_event_to_message(
    logger: Any, method: str, event_dict: dict
) -> dict:
    """
    ELK Stack 호환: structlog 의 'event' 키를 'message' 로 변경합니다.

    Elasticsearch 는 'message' 필드를 기본 전문 검색 대상으로 사용합니다.
    이 프로세서는 파일(JSON) 핸들러 전용입니다.
    """
    event_dict["message"] = event_dict.pop("event", "")
    return event_dict


# ─────────────────────────────────────────────────────────────
# 공유 프로세서 체인
# ─────────────────────────────────────────────────────────────

def _build_shared_processors() -> list:
    """
    structlog 과 stdlib 핸들러(foreign_pre_chain) 양쪽에서 공유하는 프로세서 목록.

    실행 순서:
        1. contextvars  → article_id / phase / job_id 자동 병합
        2. add_log_level → "level": "INFO" 추가
        3. add_logger_name → "logger": "scraper.worker" 추가
        4. PositionalArgFormatter → '%s' 스타일 메시지 지원
        5. TimeStamper → "@timestamp": "2026-02-25T..." (UTC ISO 8601)
        6. StackInfoRenderer → stack_info 인자 처리
        7. _add_service_context → "service": "tih", "host": "..." 주입
    """
    return [
        # ① 컨텍스트 자동 병합 (article_id, phase, job_id 등)
        structlog.contextvars.merge_contextvars,
        # ② 로그 레벨 문자열 추가
        structlog.stdlib.add_log_level,
        # ③ 모듈명(로거 이름) 추가
        structlog.stdlib.add_logger_name,
        # ④ '%s' / '%d' 스타일 포지셔널 포매팅 지원
        structlog.stdlib.PositionalArgumentsFormatter(),
        # ⑤ UTC ISO 8601 타임스탬프 (@timestamp 는 ELK 표준 필드명)
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="@timestamp"),
        # ⑥ logger.info("...", stack_info=True) 처리
        structlog.processors.StackInfoRenderer(),
        # ⑦ 서비스·호스트 메타 자동 주입
        _add_service_context,
    ]


# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────

def configure_logging(
    level:    str | None  = None,
    json_logs: bool | None = None,
    log_file:  bool        = True,
) -> None:
    """
    structlog + stdlib logging 을 통합 설정합니다.

    파이프라인:
        structlog.get_logger() 호출
            │  ← contextvars에서 article_id/phase 자동 병합
            ▼
        공유 프로세서 체인 (_build_shared_processors)
            │
            ▼ ProcessorFormatter.wrap_for_formatter
        stdlib root logger
            ├── StreamHandler   ──► ProcessorFormatter (콘솔 렌더러)
            └── RotatingFileHandler ─► ProcessorFormatter (JSON 렌더러, ELK)

    Args:
        level:     로그 레벨 (기본: LOG_LEVEL 환경변수 → "INFO")
        json_logs: 콘솔 JSON 강제 여부 (기본: production 이면 True)
        log_file:  파일 로그 활성화 (기본: True)
    """
    from core.config import settings

    log_level_str = (level or settings.LOG_LEVEL).upper()
    log_level     = getattr(logging, log_level_str, logging.INFO)
    is_production = settings.ENVIRONMENT == "production"
    use_json      = json_logs if json_logs is not None else is_production

    shared = _build_shared_processors()

    # ── ① structlog 설정 (stdlib 브릿지) ─────────────────────
    #
    #  wrap_for_formatter 가 마지막에 위치해야 합니다.
    #  이 프로세서가 event_dict 를 stdlib LogRecord 로 패키징합니다.
    #  실제 렌더링(JSON/콘솔)은 각 핸들러의 ProcessorFormatter 가 담당합니다.
    structlog.configure(
        processors=shared + [ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    # ── ② 콘솔 포매터 ────────────────────────────────────────
    #
    #  개발: ConsoleRenderer (색상 + 정렬된 키)
    #  프로덕션: JSONRenderer (CloudWatch/로그 수집기 호환)
    _console_renderer = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty() or sys.stdout.isatty(),
            sort_keys=False,
        )
    )
    console_formatter = ProcessorFormatter(
        # stdlib 라이브러리 로그(sqlalchemy 등)도 동일 체인 통과
        foreign_pre_chain=shared,
        processors=[
            ProcessorFormatter.remove_processors_meta,   # 내부 메타 제거
            structlog.processors.ExceptionRenderer(),    # 예외 → 문자열
            _console_renderer,
        ],
    )

    # ── ③ 파일 포매터 (항상 JSON, ELK Stack 호환) ────────────
    #
    #  @timestamp, level, logger, message, service, host + 컨텍스트 키
    #  'event' → 'message' 변환으로 Elasticsearch 기본 검색 호환
    file_formatter = ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(),
            _rename_event_to_message,                    # ELK: event → message
            structlog.processors.JSONRenderer(),
        ],
    )

    # ── ④ 핸들러 조립 ────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(log_level)

    all_handlers: list[logging.Handler] = [console_handler]

    if log_file:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOG_DIR / "tih.log"

        # RotatingFileHandler: 10 MB 초과 시 자동 교체, 최대 5개 백업 유지
        #   tih.log        ← 현재 로그
        #   tih.log.1      ← 직전 백업
        #   tih.log.2 ~ .5 ← 오래된 백업 (5개 초과 시 가장 오래된 것 삭제)
        file_handler = logging.handlers.RotatingFileHandler(
            filename    = str(log_path),
            maxBytes    = 10 * 1024 * 1024,  # 10 MB
            backupCount = 5,
            encoding    = "utf-8",
            delay       = True,              # 첫 로그 기록 시점에 파일 생성
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(log_level)
        all_handlers.append(file_handler)

    # ── ⑤ 루트 로거에 핸들러 등록 ───────────────────────────
    root = logging.getLogger()
    root.handlers.clear()   # 기존 핸들러 제거 (중복 방지)
    for h in all_handlers:
        root.addHandler(h)
    root.setLevel(log_level)

    # ── ⑥ 외부 라이브러리 로그 레벨 조정 ────────────────────
    #
    #  INFO 레벨 시 DB 쿼리·HTTP 헤더 등 불필요한 로그가 넘칩니다.
    _library_levels: dict[str, str] = {
        "uvicorn":            "INFO",
        "uvicorn.access":     "WARNING",
        "sqlalchemy.engine":  "DEBUG" if not is_production else "WARNING",
        "sqlalchemy.pool":    "WARNING",
        "alembic":            "INFO",
        "httpx":              "WARNING",
        "boto3":              "WARNING",
        "botocore":           "WARNING",
        "google.api_core":    "WARNING",
        "google.auth":        "WARNING",
        "urllib3":            "WARNING",
        "asyncio":            "WARNING",
        "PIL":                "WARNING",    # Pillow
        "yt_dlp":             "WARNING",
    }
    for lib_name, lib_level in _library_levels.items():
        logging.getLogger(lib_name).setLevel(
            getattr(logging, lib_level, logging.WARNING)
        )

    # ── ⑦ 초기화 완료 로그 ──────────────────────────────────
    init_log = structlog.get_logger(__name__)
    init_log.info(
        "로깅 초기화 완료",
        level    = log_level_str,
        console  = "json" if use_json else "color",
        file     = str(_LOG_DIR / "tih.log") if log_file else "disabled",
        rotation = "10MB × 5 backups" if log_file else "N/A",
        host     = _HOSTNAME,
    )


# ─────────────────────────────────────────────────────────────
# Context Injection API
# ─────────────────────────────────────────────────────────────

def bind_log_context(
    *,
    article_id: Optional[int] = None,
    phase:      Optional[str] = None,
    job_id:     Optional[int] = None,
    worker_id:  Optional[str] = None,
    **extra: Any,
) -> None:
    """
    현재 스레드/코루틴의 로그 컨텍스트를 설정합니다.

    설정된 값은 이후 모든 structlog 로그 호출에 자동으로 포함됩니다.
    기존 컨텍스트는 유지되고, 지정한 키만 추가/업데이트됩니다.

    Args:
        article_id: 처리 중인 아티클 ID (articles.id)
        phase:      처리 단계 (Phase.SCRAPING 등 Phase 상수 사용 권장)
        job_id:     작업 큐 ID (job_queue.id)
        worker_id:  EC2 워커 인스턴스 ID
        **extra:    추가 컨텍스트 (url, model, retry 등 자유 키)

    Usage:
        # 수동 설정 (루프 내에서 매 작업마다 업데이트)
        bind_log_context(article_id=42, phase=Phase.SCRAPING)
        logger.info("HTTP 요청")  # → {"article_id": 42, "phase": "Scraping", ...}
        bind_log_context(phase=Phase.AI_PROCESSING)
        logger.info("AI 호출")    # → {"article_id": 42, "phase": "AI Processing"}
    """
    ctx = {k: v for k, v in {
        "article_id": article_id,
        "phase":      phase,
        "job_id":     job_id,
        "worker_id":  worker_id,
        **extra,
    }.items() if v is not None}

    if ctx:
        structlog.contextvars.bind_contextvars(**ctx)


def clear_log_context() -> None:
    """
    현재 스레드/코루틴의 모든 로그 컨텍스트를 초기화합니다.

    Usage:
        clear_log_context()  # 작업 완료 후 컨텍스트 정리
    """
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(
    *,
    article_id: Optional[int] = None,
    phase:      Optional[str] = None,
    job_id:     Optional[int] = None,
    worker_id:  Optional[str] = None,
    **extra: Any,
) -> Generator[None, None, None]:
    """
    로그 컨텍스트를 자동으로 설정·복원하는 컨텍스트 매니저.

    동작:
        - with 블록 진입 시 컨텍스트 설정
        - with 블록 종료 시 이전 상태로 완전 복원 (예외 발생 시에도)
        - 중첩 사용 가능 — 내부 블록이 외부 블록의 컨텍스트를 덮지 않습니다

    Args:
        article_id: 처리 중인 아티클 ID
        phase:      처리 단계 (Phase 상수 사용 권장)
        job_id:     작업 큐 ID
        worker_id:  EC2 워커 인스턴스 ID
        **extra:    추가 컨텍스트

    Usage:
        # 기본 사용
        with log_context(article_id=42, phase=Phase.SCRAPING, job_id=7):
            logger.info("스크래핑 시작")          # article_id=42, phase=Scraping
            html = await fetch(url)
            logger.info("완료", bytes=len(html))  # 동일 컨텍스트 + bytes

        # 중첩 사용 — 외부 컨텍스트 자동 복원
        with log_context(job_id=7, phase=Phase.WORKER_LOOP):
            logger.info("워커 폴링")              # job_id=7, phase=Worker Loop

            with log_context(article_id=42, phase=Phase.SCRAPING):
                logger.info("스크래핑")           # job_id=7, article_id=42, phase=Scraping

            logger.info("다음 작업 대기")         # job_id=7, phase=Worker Loop (복원됨)

        # 비동기 환경에서도 동일하게 동작 (asyncio Task 격리)
        async def process_article(article_id: int, job_id: int):
            with log_context(article_id=article_id, job_id=job_id):
                logger.info("처리 시작")
                await scrape(url)
    """
    # 현재 컨텍스트 스냅샷 저장 (중첩 복원용)
    previous = structlog.contextvars.get_contextvars().copy()

    bind_log_context(
        article_id = article_id,
        phase      = phase,
        job_id     = job_id,
        worker_id  = worker_id,
        **extra,
    )
    try:
        yield
    finally:
        # 이전 상태로 완전 복원 (예외 발생 시에도 보장)
        structlog.contextvars.clear_contextvars()
        if previous:
            structlog.contextvars.bind_contextvars(**previous)


# ─────────────────────────────────────────────────────────────
# 편의 함수
# ─────────────────────────────────────────────────────────────

def get_logger(name: str = __name__) -> Any:
    """
    모듈별 structlog 로거를 반환합니다.

    Usage:
        # 모듈 상단에서 1회 선언
        logger = get_logger(__name__)

        # 이후 사용
        logger.debug("디버그", key=value)
        logger.info("처리 완료", count=5)
        logger.warning("재시도", attempt=2)
        logger.error("처리 실패", exc_info=True)
        logger.exception("예외 발생")  # exc_info=True 자동 포함
    """
    return structlog.get_logger(name)
