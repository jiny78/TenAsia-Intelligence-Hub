"""
web/worker_main.py — App Runner 워커 서비스 진입점

App Runner는 HTTP 서버를 요구합니다.
- 메인 스레드: 스크래퍼 워커 루프 (SIGTERM 처리 포함)
- 데몬 스레드: FastAPI 헬스체크 서버 (포트 8000)
"""
from __future__ import annotations

import logging
import threading

import uvicorn
from fastapi import FastAPI

from scraper.worker import _setup_logging, run_loop

_setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="TenAsia Scraper Worker")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "scraper-worker"}


def main() -> None:
    # 헬스체크 HTTP 서버를 데몬 스레드로 실행
    # (메인 스레드 종료 시 자동으로 함께 종료됨)
    health_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
        daemon=True,
        name="health-server",
    )
    health_thread.start()
    logger.info("헬스체크 서버 시작 (포트 8000)")

    # 워커 루프를 메인 스레드에서 실행 (signal handling 지원)
    logger.info("스크래퍼 워커 루프 시작")
    run_loop()
    logger.info("워커 종료")


if __name__ == "__main__":
    main()
