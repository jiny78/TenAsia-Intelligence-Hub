"""
core/image_utils.py — 이미지 다운로드, 썸네일 생성, S3 업로드

설계 원칙:
    1. 원본 이미지를 메모리에만 잠시 로드하고, 처리 직후 즉시 제거합니다.
       (raw bytes → BytesIO → PIL Image → WEBP BytesIO → S3 업로드 → 모든 임시 객체 del)

    2. 가로 THUMBNAIL_MAX_WIDTH(300) px 기준으로 비율을 유지해 리사이징합니다.
       이미 작은 이미지(원본 가로 ≤ 300px)는 그대로 저장합니다.

    3. 저장 포맷은 WEBP (quality 80) — 동일 화질에서 JPEG 대비 약 25~35% 용량 절감.
       Content-Type: image/webp 로 S3에 업로드하여 브라우저에서 바로 렌더링됩니다.

    4. S3 키 형식: thumbnails/{env}/{article_id}_{url_sha256[:12]}.webp
       article_id + URL 해시 조합으로 충돌 방지 및 역추적 가능.

    5. HTTP 요청 시 session 파라미터로 ThrottledSession 을 전달하면
       DomainThrottle(도메인 최소 간격 + RPM) 이 자동 적용됩니다.
       추가 Human Delay 는 호출 측(engine.py) 에서 _human_delay() 로 처리합니다.

    6. S3 클라이언트는 모듈 레벨 싱글톤으로 초기화 지연(lazy init)하여
       boto3 미설치 환경에서도 import 시 오류가 발생하지 않습니다.

    7. S3 퍼블릭 접근은 버킷 정책(Public Read on thumbnails/* prefix)으로 관리합니다.
       업로드 시 ACL 파라미터를 사용하지 않습니다.

공개 함수:
    generate_thumbnail(image_url, article_id, session=None, timeout=15) -> Optional[str]

공개 상수:
    THUMBNAIL_MAX_WIDTH   — 최대 가로 폭 px (300)
    S3_KEY_PREFIX         — S3 키 접두사 (thumbnails)

사용 예:
    from core.image_utils import generate_thumbnail
    from scraper.throttle import get_session

    session    = get_session()
    public_url = generate_thumbnail(
        image_url  = "https://example.com/img/photo.jpg",
        article_id = 42,
        session    = session,
    )
    # "https://tenasia-thumbnails.s3.ap-northeast-2.amazonaws.com/thumbnails/development/42_3a8f1c0d9e2b.webp"
    # 또는 None (실패)
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    import boto3 as _boto3_type

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 설정 상수
# ─────────────────────────────────────────────────────────────

THUMBNAIL_MAX_WIDTH: int = 300
THUMBNAIL_QUALITY: int   = 80
THUMBNAIL_FORMAT: str    = "WEBP"
THUMBNAIL_CONTENT_TYPE   = "image/webp"
THUMBNAIL_CACHE_CONTROL  = "max-age=86400"   # 1일 브라우저 캐시

S3_KEY_PREFIX: str = "thumbnails"            # S3 내 최상위 폴더

# ─────────────────────────────────────────────────────────────
# S3 클라이언트 싱글톤 (lazy init)
# ─────────────────────────────────────────────────────────────

_s3_client: Optional[object] = None          # boto3.client 인스턴스 또는 None


def _get_s3_client():
    """
    boto3 S3 클라이언트를 반환합니다. 최초 호출 시 초기화됩니다.

    Returns:
        boto3.client("s3") 인스턴스

    Raises:
        ImportError:   boto3 미설치
        Exception:     boto3 클라이언트 초기화 실패 (AWS 자격증명 오류 등)
    """
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    import boto3  # type: ignore[import]

    from core.config import settings

    _s3_client = boto3.client("s3", region_name=settings.AWS_REGION)
    log.debug(
        "S3 클라이언트 초기화 완료 | region=%s bucket=%s",
        settings.AWS_REGION, settings.S3_BUCKET_NAME,
    )
    return _s3_client


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────

def generate_thumbnail(
    image_url:  str,
    article_id: int,
    session:    Optional[requests.Session] = None,
    timeout:    int                        = 15,
) -> Optional[str]:
    """
    원본 URL 에서 이미지를 메모리에 다운로드하고 썸네일로 변환하여 S3에 업로드합니다.

    처리 순서:
        1. HTTP GET — session 이 있으면 ThrottledSession 재사용 (DomainThrottle 자동 적용)
        2. raw bytes → BytesIO → PIL.Image.open() + load()
        3. raw bytes 즉시 삭제 (메모리 해제)
        4. RGB/RGBA 변환 (WEBP 호환)
        5. 가로 THUMBNAIL_MAX_WIDTH(300px) 기준 비율 유지 리사이징
        6. 인메모리 BytesIO(webp_buf)에 WEBP 형식으로 저장
        7. PIL Image + 입력 BytesIO 즉시 닫기 + 삭제 (메모리 해제)
        8. S3 put_object() — ContentType=image/webp, CacheControl=max-age=86400
        9. webp_buf 닫기 + 삭제
       10. S3 퍼블릭 URL 반환

    Args:
        image_url:  원본 이미지 URL
        article_id: 연결된 articles.id (S3 키에 포함하여 역추적 용이)
        session:    재사용할 requests.Session.
                    scraper.throttle.get_session() 의 ThrottledSession 을 전달하면
                    DomainThrottle 이 자동 적용됩니다. None 이면 일반 requests.get 사용.
        timeout:    HTTP 요청 타임아웃 (초, 기본 15)

    Returns:
        업로드된 썸네일의 S3 퍼블릭 URL 문자열
        예: "https://tenasia-thumbnails.s3.ap-northeast-2.amazonaws.com/thumbnails/development/42_3a8f1c0d9e2b.webp"
        또는 None (다운로드 실패 / Pillow 처리 오류 / S3 업로드 실패 / 지원 불가 포맷)
    """
    # ── 의존성 사전 확인 ──────────────────────────────────────
    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore[import]
    except ImportError:
        log.error("Pillow 미설치 — 썸네일 생성 불가. `pip install Pillow` 를 실행하세요.")
        return None

    try:
        s3 = _get_s3_client()
    except ImportError:
        log.error("boto3 미설치 — S3 업로드 불가. `pip install boto3` 를 실행하세요.")
        return None
    except Exception as exc:
        log.error("S3 클라이언트 초기화 실패 | err=%r", exc)
        return None

    from core.config import settings

    # ── 1. 이미지 다운로드 ──────────────────────────────────
    try:
        requester: requests.Session | type = session if session is not None else requests
        resp = requester.get(image_url, timeout=timeout, stream=True)
        resp.raise_for_status()
        raw_bytes: bytes = resp.content
        del resp          # 응답 객체 + 커넥션 즉시 해제
    except requests.exceptions.RequestException as exc:
        log.warning("img_download_failed | url=%.80s | err=%s", image_url, exc)
        return None
    except Exception as exc:
        log.warning("img_download_unexpected | url=%.80s | err=%r", image_url, exc)
        return None

    # ── 2~9. 리사이징 → 인메모리 WEBP 생성 → S3 업로드 ────
    buf: Optional[io.BytesIO]     = None
    img                           = None
    webp_buf: Optional[io.BytesIO] = None
    try:
        buf = io.BytesIO(raw_bytes)
        del raw_bytes   # 원본 바이트 즉시 삭제

        img = Image.open(buf)
        img.load()      # 실제 픽셀 데이터 디코딩 강제 실행

        # RGBA, P(팔레트), L(그레이스케일) → RGB 변환 (WEBP 호환)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # 가로 기준 비율 유지 리사이징 (원본이 이미 작으면 스킵)
        orig_w, orig_h = img.size
        if orig_w > THUMBNAIL_MAX_WIDTH:
            new_h = max(1, round(orig_h * THUMBNAIL_MAX_WIDTH / orig_w))
            img   = img.resize((THUMBNAIL_MAX_WIDTH, new_h), Image.LANCZOS)

        # 인메모리 WEBP 버퍼 생성
        webp_buf = io.BytesIO()
        img.save(webp_buf, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY)

        # PIL Image + 입력 BytesIO 즉시 정리 (S3 업로드 전 메모리 확보)
        img.close()
        del img
        img = None
        buf.close()
        del buf
        buf = None

        # S3 키 및 업로드 설정
        url_hash = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
        s3_key   = (
            f"{S3_KEY_PREFIX}/{settings.ENVIRONMENT}"
            f"/{article_id}_{url_hash}.webp"
        )

        # ── 8. S3 업로드 ────────────────────────────────────
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Body=webp_buf.getvalue(),
            ContentType=THUMBNAIL_CONTENT_TYPE,
            CacheControl=THUMBNAIL_CACHE_CONTROL,
        )

        public_url = f"{settings.s3_base_url}/{s3_key}"
        log.debug(
            "thumbnail_uploaded | url=%s | size=%d bytes",
            public_url, webp_buf.tell() if webp_buf.seekable() else -1,
        )
        return public_url

    except (UnidentifiedImageError, OSError) as exc:
        log.warning("img_pillow_failed | url=%.80s | err=%s", image_url, exc)
        return None
    except Exception as exc:
        log.warning("img_unexpected | url=%.80s | err=%r", image_url, exc)
        return None
    finally:
        # 예외 발생 여부와 무관하게 메모리 정리
        for obj, name in ((img, "img"), (buf, "buf"), (webp_buf, "webp_buf")):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
