"""
web/api.py — FastAPI 내부 API 서버 (포트 8000)

App Runner 컨테이너 내부에서 uvicorn 으로 구동됩니다.
Streamlit(포트 8501) → http://localhost:8000 으로 호출합니다.

엔드포인트:
  POST   /jobs                 작업 큐에 새 작업 추가
  GET    /jobs/{job_id}        작업 상세 조회
  GET    /jobs                 최근 작업 목록
  DELETE /jobs/{job_id}        작업 취소 (pending 만)
  GET    /jobs/stats           상태별 통계
  POST   /trigger/ssm          SSM SendCommand 로 EC2 스크래퍼 즉시 실행
  GET    /health               헬스체크
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scraper.db import (
    cancel_job,
    create_job,
    get_job_by_id,
    get_queue_stats,
    get_recent_jobs,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="TenAsia Intelligence Hub — Internal API",
    version="1.0.0",
    docs_url="/docs",      # Swagger UI (개발용)
    redoc_url=None,
)

# App Runner 내부에서만 사용하므로 CORS는 localhost만 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 요청/응답 스키마 ─────────────────────────────────────────

class CreateJobRequest(BaseModel):
    source_url: str       = Field(..., description="스크래핑할 기사 URL")
    language:   str       = Field("kr", description="언어 코드 (kr / en)")
    platforms:  list[str] = Field(default_factory=list, description="배포 플랫폼 목록")
    priority:   int       = Field(5, ge=1, le=10, description="우선순위 (높을수록 먼저)")
    max_retries: int      = Field(3, ge=0, le=10)


class SsmTriggerRequest(BaseModel):
    job_id:    Optional[int] = Field(None, description="특정 job_id 지정 (없으면 루프 재시작)")
    comment:   str           = Field("", description="트리거 이유 (로그용)")


# ── 헬퍼 ─────────────────────────────────────────────────────

def _ssm_client():
    return boto3.client("ssm", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))


def _scraper_instance_id() -> str:
    instance_id = os.getenv("EC2_SCRAPER_INSTANCE_ID", "")
    if not instance_id:
        raise HTTPException(
            status_code=503,
            detail="EC2_SCRAPER_INSTANCE_ID 환경 변수가 설정되지 않았습니다.",
        )
    return instance_id


# ── 엔드포인트 ───────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=201)
def submit_job(req: CreateJobRequest) -> dict[str, Any]:
    """작업 큐에 스크래핑 작업을 추가합니다."""
    params = {
        "source_url": req.source_url,
        "language":   req.language,
        "platforms":  req.platforms,
    }
    job_id = create_job(
        job_type="scrape",
        params=params,
        priority=req.priority,
        max_retries=req.max_retries,
    )
    logger.info("작업 생성 | job_id=%d url=%s", job_id, req.source_url)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/stats")
def queue_stats() -> dict[str, int]:
    """상태별 작업 수를 반환합니다."""
    return get_queue_stats()


@app.get("/jobs")
def list_jobs(limit: int = 20) -> list[dict]:
    """최근 작업 목록을 반환합니다."""
    return get_recent_jobs(limit=limit)


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    """특정 작업의 상세 정보를 반환합니다."""
    job = get_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job_id={job_id} 없음")
    return job


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int) -> dict[str, Any]:
    """pending 상태인 작업을 취소합니다."""
    cancelled = cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=f"job_id={job_id} 취소 실패 (이미 실행 중이거나 존재하지 않음)",
        )
    return {"job_id": job_id, "status": "cancelled"}


@app.post("/trigger/ssm")
def trigger_ssm(req: SsmTriggerRequest) -> dict[str, Any]:
    """
    SSM SendCommand 로 EC2 스크래퍼를 즉시 실행합니다.

    - job_id 지정 시: python -m scraper.worker --job-id <id> 실행
    - job_id 없을 때: systemctl restart tih-scraper (루프 재시작)
    """
    instance_id = _scraper_instance_id()
    ssm = _ssm_client()

    if req.job_id is not None:
        command = f"cd /opt/tih && python -m scraper.worker --job-id {req.job_id}"
    else:
        command = "systemctl restart tih-scraper"

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            Comment=req.comment or "TIH App Runner trigger",
            TimeoutSeconds=60,
        )
        command_id = response["Command"]["CommandId"]
        logger.info("SSM SendCommand 전송 | command_id=%s job_id=%s", command_id, req.job_id)
        return {
            "command_id":  command_id,
            "instance_id": instance_id,
            "command":     command,
        }
    except Exception as exc:
        logger.exception("SSM SendCommand 실패: %s", exc)
        raise HTTPException(status_code=502, detail=f"SSM 오류: {exc}") from exc


@app.get("/trigger/ssm/{command_id}")
def get_ssm_result(command_id: str) -> dict[str, Any]:
    """SSM 명령 실행 결과를 조회합니다."""
    instance_id = _scraper_instance_id()
    ssm = _ssm_client()

    try:
        resp = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
        )
        return {
            "command_id":      command_id,
            "status":          resp["Status"],
            "status_details":  resp["StatusDetails"],
            "stdout":          resp.get("StandardOutputContent", ""),
            "stderr":          resp.get("StandardErrorContent", ""),
        }
    except ssm.exceptions.InvocationDoesNotExist:
        raise HTTPException(status_code=404, detail=f"command_id={command_id} 없음")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SSM 조회 오류: {exc}") from exc
