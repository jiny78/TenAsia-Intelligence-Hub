"""
scraper/worker.py — EC2 백그라운드 워커

두 가지 실행 모드:
  1. 루프 모드 (기본): python -m scraper.worker
     - pending 작업을 계속 폴링하며 처리
     - SIGTERM/SIGINT 수신 시 현재 작업 완료 후 종료

  2. 단일 실행 모드 (SSM SendCommand 트리거용):
     python -m scraper.worker --job-id <id>
     - 지정한 작업 하나만 처리하고 즉시 종료

환경 변수:
  WORKER_POLL_INTERVAL   폴링 간격 (초, 기본 10)
  WORKER_ID              워커 식별자 (기본: EC2 인스턴스 ID 또는 hostname)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from datetime import datetime
from typing import Optional

import requests

from scraper.db import (
    STATUS_FAILED,
    create_db_tables,
    get_job_by_id,
    get_pending_job,
    increment_retry,
    update_job_status,
)
from scraper.engine import ForbiddenError, TenAsiaScraper

logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("WORKER_POLL_INTERVAL", "10"))


def _get_worker_id() -> str:
    """EC2 인스턴스 ID 또는 hostname을 워커 식별자로 사용합니다."""
    env_id = os.getenv("WORKER_ID")
    if env_id:
        return env_id

    # EC2 IMDSv2 에서 인스턴스 ID 조회
    try:
        # IMDSv2: 먼저 토큰 발급
        token_resp = requests.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            timeout=2,
        )
        token = token_resp.text
        instance_id = requests.get(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
            timeout=2,
        ).text
        return instance_id
    except Exception:
        return socket.gethostname()


# ── 작업 처리 ────────────────────────────────────────────────

def _do_scrape(params: dict, job_id: Optional[int] = None) -> dict:
    """
    TenAsiaScraper 를 사용하여 실제 스크래핑을 실행합니다.

    params 예시:
        {
            "source_url": "https://tenasia.hankyung.com/article/123",  # 단일 URL
            "urls":       ["https://...", "https://..."],              # 복수 URL (우선)
            "language":   "kr",
            "platforms":  ["x", "instagram"],
            "batch_size": 10,   # 생략 시 기본값 10
        }

    Returns:
        BatchResult.to_dict() 기반 딕셔너리 (job_queue.result 에 저장됨):
            {
                "total":     10,
                "processed": 8,
                "success":   [...],
                "failed":    [...],
                "platforms": [...],
            }

    Raises:
        ForbiddenError: HTTP 403 차단 감지 — process_job 이 재시도 없이 즉시 실패 처리
        ValueError:     params 에 URL 이 없을 때
    """
    # URL 목록: params["urls"] 우선, 없으면 source_url 단건 래핑
    urls: list[str] = params.get("urls") or []
    if not urls:
        source_url = params.get("source_url", "").strip()
        if source_url:
            urls = [source_url]

    if not urls:
        raise ValueError("params 에 source_url 또는 urls 가 없습니다.")

    language   = params.get("language",   "kr")
    platforms  = params.get("platforms",  [])
    batch_size = int(params.get("batch_size", 10))
    dry_run    = bool(params.get("dry_run", False))

    logger.info(
        "스크래핑 시작 | urls=%d lang=%s job_id=%s dry_run=%s",
        len(urls), language, job_id, dry_run,
    )

    scraper = TenAsiaScraper(batch_size=batch_size)
    result  = scraper.scrape_batch(
        urls=urls, job_id=job_id, language=language, dry_run=dry_run
    )

    # fatal 실패(403 차단) 감지 → ForbiddenError 재발생으로 재시도 방지
    fatal = [f for f in result.failed if f.get("fatal")]
    if fatal:
        raise ForbiddenError(
            f"IP/UA 차단 감지 — 재시도 불필요: {fatal[0].get('url', '')}"
        )

    result_dict = result.to_dict()
    result_dict["platforms"] = platforms
    return result_dict


def _do_scrape_range(params: dict, job_id: Optional[int] = None) -> dict:
    """
    날짜 범위 스크래핑을 실행합니다. POST /scrape 의 워커 구현체입니다.

    params 예시:
        {
            "start_date": "2024-01-01",
            "end_date":   "2024-01-31",
            "language":   "kr",
            "max_pages":  10,
            "dry_run":    false,
        }
    """
    start_dt = datetime.strptime(params["start_date"], "%Y-%m-%d")
    end_dt   = datetime.strptime(params["end_date"],   "%Y-%m-%d").replace(
        hour=23, minute=59, second=59
    )
    language  = params.get("language",  "kr")
    max_pages = int(params.get("max_pages", 10))
    dry_run   = bool(params.get("dry_run", False))

    logger.info(
        "scrape_range 시작 | start=%s end=%s lang=%s max_pages=%d dry_run=%s job_id=%s",
        start_dt.date(), end_dt.date(), language, max_pages, dry_run, job_id,
    )

    scraper = TenAsiaScraper()
    result  = scraper.scrape_range(
        start_date=start_dt,
        end_date=end_dt,
        job_id=job_id,
        language=language,
        max_pages=max_pages,
        dry_run=dry_run,
    )

    return {
        "total":         getattr(result, "total",   0),
        "success_count": len(getattr(result, "success",  [])),
        "failed_count":  len(getattr(result, "failed",   [])),
        "skipped_count": len(getattr(result, "skipped",  [])),
    }


def _do_process_scraped() -> None:
    """스크래핑 완료 후 SCRAPED 기사를 AI 처리합니다. 실패해도 스크래핑 결과에 영향 없음."""
    try:
        from processor.simple_processor import process_all_scraped
        process_all_scraped()
    except Exception as exc:
        logger.warning("AI 후처리 실패 (스크래핑 결과는 정상 저장됨) | %s: %s", type(exc).__name__, exc)


def process_job(job: dict) -> None:
    """
    단일 작업을 처리하고 결과를 DB에 기록합니다.
    예외 발생 시 retry 로직을 적용합니다.
    """
    job_id   = job["id"]
    job_type = job["job_type"]
    params   = job.get("params") or {}

    logger.info("작업 처리 시작 | id=%d type=%s", job_id, job_type)

    try:
        if job_type == "scrape":
            result = _do_scrape(params, job_id=job_id)
        elif job_type == "scrape_range":
            result = _do_scrape_range(params, job_id=job_id)
        else:
            raise ValueError(f"알 수 없는 job_type: {job_type!r}")

        update_job_status(job_id, "completed", result=result)
        logger.info("작업 완료 | id=%d", job_id)

        # 스크래핑 성공 후 SCRAPED 기사를 즉시 AI 처리
        _do_process_scraped()

    except ForbiddenError as exc:
        # 403 IP/UA 차단: 재시도해도 의미 없으므로 즉시 실패 처리
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error(
            "IP/UA 차단 — 재시도 없이 실패 처리 | id=%d error=%s",
            job_id, error_msg,
        )
        update_job_status(job_id, STATUS_FAILED, error_msg=error_msg)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("작업 실패 | id=%d error=%s", job_id, error_msg)

        new_retry = increment_retry(job_id)
        max_retries = job.get("max_retries", 3)

        if new_retry >= max_retries:
            # increment_retry 내부에서 이미 'failed' 로 전환됨
            update_job_status(job_id, STATUS_FAILED, error_msg=error_msg)
            logger.warning("최대 재시도 초과, 작업 실패 처리 | id=%d retries=%d", job_id, new_retry)
        else:
            # 'pending' 으로 재귀 (increment_retry 가 이미 처리)
            logger.info("재시도 예약 | id=%d retry=%d/%d", job_id, new_retry, max_retries)


# ── 실행 모드 ────────────────────────────────────────────────

class _ShutdownFlag:
    """SIGTERM/SIGINT 를 받으면 running 을 False 로 전환합니다."""

    def __init__(self) -> None:
        self.running = True
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT,  self._handle)

    def _handle(self, signum, frame) -> None:  # noqa: ANN001
        logger.info("종료 신호 수신 (%d), 현재 작업 완료 후 종료합니다…", signum)
        self.running = False


def run_loop() -> None:
    """
    루프 모드: pending 작업을 계속 폴링하며 처리합니다.
    SIGTERM 수신 시 현재 작업 완료 후 종료합니다.
    """
    worker_id = _get_worker_id()
    flag = _ShutdownFlag()

    logger.info("워커 루프 시작 | worker_id=%s poll_interval=%ds", worker_id, POLL_INTERVAL)

    create_db_tables()  # 테이블이 없으면 생성 (멱등)

    while flag.running:
        job = get_pending_job(worker_id)

        if job is None:
            logger.debug("대기 중… (큐 비어있음)")
            time.sleep(POLL_INTERVAL)
            continue

        process_job(job)
        # 처리 직후 다음 작업 즉시 시도 (sleep 없음)

    logger.info("워커 루프 종료")


def run_single(job_id: int) -> None:
    """
    단일 실행 모드: 지정한 job_id 를 처리하고 종료합니다.
    SSM SendCommand 트리거 시 사용합니다.
    """
    worker_id = _get_worker_id()
    logger.info("단일 작업 모드 | job_id=%d worker_id=%s", job_id, worker_id)

    create_db_tables()

    job = get_job_by_id(job_id)
    if job is None:
        logger.error("job_id=%d 를 찾을 수 없습니다.", job_id)
        return

    if job["status"] != "pending":
        logger.warning("job_id=%d 상태가 pending 이 아님: %s", job_id, job["status"])
        return

    process_job(job)


# ── 진입점 ────────────────────────────────────────────────────

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="TIH Scraper Worker")
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="특정 job_id 만 처리하고 종료 (SSM SendCommand 트리거용)",
    )
    args = parser.parse_args(argv)

    _setup_logging()

    if args.job_id is not None:
        run_single(args.job_id)
    else:
        run_loop()


if __name__ == "__main__":
    main()
