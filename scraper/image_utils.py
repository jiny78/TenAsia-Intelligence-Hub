"""
scraper/image_utils.py — 이미지 다운로드 및 S3 업로드 유틸리티

우선순위 정책:
  1. S3 업로드 시도 (최우선)
     → 성공: S3 퍼블릭 URL 반환 + 로컬 임시 파일 즉시 삭제
  2. S3 실패 (네트워크 오류, 권한 오류 등)
     → 경고 로그 기록 + 로컬 임시 파일 경로 반환 (폴백)
     → ⚠️  폴백 URL은 DB에 저장하지 말 것 (컨테이너 재시작 시 사라짐)

사용법:
    from scraper.image_utils import process_thumbnail

    s3_url = process_thumbnail(
        image_url="https://example.com/photo.jpg",
        article_id=42,
    )
    # → "https://tenasia-thumbnails.s3.ap-northeast-2.amazonaws.com/thumbnails/42/a1b2c3d4.jpg"
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Optional

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# 썸네일 최대 크기 (px)
_THUMBNAIL_MAX_SIZE = (800, 600)
# JPEG 품질
_JPEG_QUALITY = 85
# HTTP 다운로드 타임아웃 (초)
_DOWNLOAD_TIMEOUT = 15

# Content-Type → 확장자 매핑
_EXT_MAP: dict[str, str] = {
    "image/jpeg":  ".jpg",
    "image/jpg":   ".jpg",
    "image/png":   ".png",
    "image/gif":   ".gif",
    "image/webp":  ".webp",
    "image/bmp":   ".bmp",
}


# ─────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────

def _s3_client():
    from core.config import settings
    return boto3.client("s3", region_name=settings.AWS_REGION)


def _bucket() -> str:
    from core.config import settings
    return settings.S3_BUCKET_NAME


def _s3_public_url(key: str) -> str:
    """S3 퍼블릭 오브젝트 URL을 생성합니다."""
    from core.config import settings
    return (
        f"https://{_bucket()}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
    )


def _url_hash(url: str) -> str:
    """URL 기반 SHA-256 해시 앞 8자 — S3 키 중복 방지용."""
    return hashlib.sha256(url.encode()).hexdigest()[:8]


# ─────────────────────────────────────────────────────────────
# 다운로드
# ─────────────────────────────────────────────────────────────

def download_image(url: str, timeout: int = _DOWNLOAD_TIMEOUT) -> Path:
    """
    이미지 URL을 다운로드하여 시스템 임시 디렉터리에 저장합니다.

    Returns:
        Path: 저장된 임시 파일 경로

    Raises:
        requests.HTTPError:     HTTP 오류 응답 (4xx, 5xx)
        ValueError:             이미지가 아닌 응답
        requests.Timeout:       타임아웃
    """
    logger.debug("이미지 다운로드 시작 | url=%s", url)

    resp = requests.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError(
            f"이미지 응답이 아닙니다. content-type={content_type!r}, url={url}"
        )

    ext = _EXT_MAP.get(content_type, ".jpg")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        for chunk in resp.iter_content(chunk_size=8_192):
            tmp.write(chunk)

    logger.debug("다운로드 완료 | path=%s size=%d bytes", tmp_path, tmp_path.stat().st_size)
    return tmp_path


# ─────────────────────────────────────────────────────────────
# 리사이즈
# ─────────────────────────────────────────────────────────────

def resize_thumbnail(
    local_path: Path,
    max_size: tuple[int, int] = _THUMBNAIL_MAX_SIZE,
) -> Path:
    """
    이미지를 썸네일 크기로 리사이즈합니다.
    원본이 이미 작으면 그대로 반환합니다 (불필요한 재인코딩 방지).

    Returns:
        Path: 동일한 경로 (인-플레이스 수정)
    """
    try:
        with Image.open(local_path) as img:
            if img.width <= max_size[0] and img.height <= max_size[1]:
                logger.debug(
                    "리사이즈 불필요 | %dx%d ≤ %dx%d",
                    img.width, img.height, *max_size,
                )
                return local_path

            img.thumbnail(max_size, Image.LANCZOS)

            # RGBA/P → RGB 변환 (JPEG는 알파 채널 미지원)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # 원본 확장자 유지, JPEG이면 품질 적용
            save_kwargs: dict = {}
            if local_path.suffix.lower() in (".jpg", ".jpeg"):
                save_kwargs = {"quality": _JPEG_QUALITY, "optimize": True}

            img.save(local_path, **save_kwargs)
            logger.debug(
                "리사이즈 완료 | %dx%d → %dx%d",
                img.width, img.height, *img.size,
            )
    except UnidentifiedImageError:
        logger.warning("리사이즈 실패 (이미지 파일 인식 불가) | path=%s", local_path)

    return local_path


# ─────────────────────────────────────────────────────────────
# S3 업로드
# ─────────────────────────────────────────────────────────────

def upload_to_s3(local_path: Path, s3_key: str) -> str:
    """
    로컬 파일을 S3에 업로드합니다.

    버킷 정책(public GetObject)으로 퍼블릭 읽기가 허용되므로
    ACL은 별도로 설정하지 않습니다.

    Returns:
        str: S3 퍼블릭 URL

    Raises:
        ClientError:     S3 권한 오류, 버킷 없음 등
        BotoCoreError:   네트워크/설정 오류
    """
    ext_to_ct = {
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".gif":  "image/gif",
        ".webp": "image/webp",
    }
    content_type = ext_to_ct.get(local_path.suffix.lower(), "image/jpeg")

    s3 = _s3_client()
    s3.upload_file(
        Filename=str(local_path),
        Bucket=_bucket(),
        Key=s3_key,
        ExtraArgs={"ContentType": content_type},
    )

    url = _s3_public_url(s3_key)
    logger.info(
        "S3 업로드 완료 | key=%s size=%d bytes url=%s",
        s3_key,
        local_path.stat().st_size,
        url,
    )
    return url


# ─────────────────────────────────────────────────────────────
# 메인 파이프라인
# ─────────────────────────────────────────────────────────────

def process_thumbnail(
    image_url: str,
    article_id: Optional[int] = None,
    prefix: str = "thumbnails",
) -> str:
    """
    이미지 처리 전체 파이프라인:

        download_image()          → 시스템 임시 파일 저장
        resize_thumbnail()        → 최대 800×600 리사이즈
        upload_to_s3()            → S3 업로드 (최우선)
        local_path.unlink()       → 임시 파일 즉시 삭제 ✓
        return S3_PUBLIC_URL      → DB에 저장할 URL

    S3 업로드 실패 시:
        경고 로그만 기록 → 임시 파일 유지 → 빈 문자열 반환
        (호출자가 DB에 저장하지 않도록 처리해야 함)

    Args:
        image_url:   원본 이미지 URL
        article_id:  articles.id (S3 키 경로 구성용, 없으면 해시만 사용)
        prefix:      S3 키 프리픽스 (기본: "thumbnails")

    Returns:
        str: S3 퍼블릭 URL (성공) 또는 "" (S3 실패)
    """
    local_path: Optional[Path] = None

    try:
        # ── Step 1: 다운로드 ─────────────────────────────────
        local_path = download_image(image_url)

        # ── Step 2: 썸네일 리사이즈 ──────────────────────────
        local_path = resize_thumbnail(local_path)

        # ── Step 3: S3 키 생성 ───────────────────────────────
        img_hash = _url_hash(image_url)
        ext      = local_path.suffix or ".jpg"
        s3_key   = (
            f"{prefix}/{article_id}/{img_hash}{ext}"
            if article_id is not None
            else f"{prefix}/{img_hash}{ext}"
        )

        # ── Step 4: S3 업로드 (최우선) ───────────────────────
        public_url = upload_to_s3(local_path, s3_key)

        # ── Step 5: 로컬 임시 파일 즉시 삭제 ─────────────────
        local_path.unlink(missing_ok=True)
        logger.debug("임시 파일 삭제 | path=%s", local_path)

        return public_url

    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "S3 업로드 실패 | url=%s error=%s — 임시 파일 유지됨: %s",
            image_url, exc, local_path,
        )
        # S3 실패 시 빈 문자열 반환 — DB에 thumbnail_url 저장 금지
        return ""

    except (requests.RequestException, ValueError) as exc:
        logger.warning("이미지 다운로드 실패 | url=%s error=%s", image_url, exc)
        # 다운로드 실패 시 임시 파일 없음
        return ""

    except Exception as exc:
        logger.exception("process_thumbnail 예기치 않은 오류 | url=%s error=%s", image_url, exc)
        if local_path and local_path.exists():
            local_path.unlink(missing_ok=True)
        return ""


def delete_s3_thumbnail(s3_url: str) -> bool:
    """
    S3 URL로부터 오브젝트를 삭제합니다.
    아티클 삭제 시 연동 호출용.

    Returns:
        True: 삭제 성공, False: 실패 또는 S3 URL이 아님
    """
    bucket = _bucket()
    # URL에서 키 추출: https://{bucket}.s3.{region}.amazonaws.com/{key}
    marker = f".amazonaws.com/"
    if marker not in s3_url:
        logger.debug("S3 URL 아님, 삭제 건너뜀 | url=%s", s3_url)
        return False

    s3_key = s3_url.split(marker, 1)[1]

    try:
        _s3_client().delete_object(Bucket=bucket, Key=s3_key)
        logger.info("S3 오브젝트 삭제 | key=%s", s3_key)
        return True
    except (ClientError, BotoCoreError) as exc:
        logger.warning("S3 삭제 실패 | key=%s error=%s", s3_key, exc)
        return False
