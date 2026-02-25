"""
core/image_utils.py — 이미지 다운로드 및 썸네일 생성

설계 원칙:
    1. 원본 이미지를 메모리에만 잠시 로드하고, 처리 직후 즉시 제거합니다.
       (raw bytes → BytesIO → PIL Image → 파일 저장 → 모든 임시 객체 del)

    2. 가로 THUMBNAIL_MAX_WIDTH(300) px 기준으로 비율을 유지해 리사이징합니다.
       이미 작은 이미지(원본 가로 ≤ 300px)는 그대로 저장합니다.

    3. 저장 포맷은 WEBP (quality 80) — 동일 화질에서 JPEG 대비 약 25~35% 용량 절감.

    4. 파일명: {article_id}_{url_sha256[:12]}.webp
       article_id + URL 해시 조합으로 충돌 방지 및 역추적 가능.

    5. HTTP 요청 시 session 파라미터로 ThrottledSession 을 전달하면
       DomainThrottle(도메인 최소 간격 + RPM) 이 자동 적용됩니다.
       추가 Human Delay 는 호출 측(engine.py) 에서 _human_delay() 로 처리합니다.

공개 함수:
    generate_thumbnail(image_url, article_id, session=None, timeout=15) -> Optional[str]

공개 상수:
    THUMBNAIL_DIR         — 저장 루트 디렉토리 (static/thumbnails)
    THUMBNAIL_MAX_WIDTH   — 최대 가로 폭 px (300)

사용 예:
    from core.image_utils import generate_thumbnail
    from scraper.throttle import get_session

    session   = get_session()
    thumb_path = generate_thumbnail(
        image_url  = "https://example.com/img/photo.jpg",
        article_id = 42,
        session    = session,
    )
    # "static/thumbnails/42_3a8f1c0d9e2b.webp"  또는  None (실패)
"""

from __future__ import annotations

import hashlib
import io
import logging
import pathlib
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 설정 상수
# ─────────────────────────────────────────────────────────────

# core/image_utils.py 위치 기준으로 프로젝트 루트를 찾습니다.
# 작업 디렉토리와 무관하게 항상 올바른 경로를 가리킵니다.
_PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent

THUMBNAIL_DIR: pathlib.Path = _PROJECT_ROOT / "static" / "thumbnails"
THUMBNAIL_MAX_WIDTH: int    = 300
THUMBNAIL_FORMAT: str       = "WEBP"
THUMBNAIL_QUALITY: int      = 80


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
    원본 URL 에서 이미지를 메모리에 다운로드하고 썸네일로 변환합니다.

    처리 순서:
        1. HTTP GET — session 이 있으면 ThrottledSession 재사용 (DomainThrottle 자동 적용)
        2. raw bytes → BytesIO → PIL.Image.open() + load()
        3. raw bytes 즉시 삭제 (메모리 해제)
        4. RGB/RGBA 변환 (WEBP 호환)
        5. 가로 THUMBNAIL_MAX_WIDTH(300px) 기준 비율 유지 리사이징
        6. THUMBNAIL_DIR 에 WEBP 형식으로 저장
        7. PIL Image + BytesIO 즉시 닫기 + 삭제

    Args:
        image_url:  원본 이미지 URL
        article_id: 연결된 articles.id (파일명에 포함하여 역추적 용이)
        session:    재사용할 requests.Session.
                    scraper.throttle.get_session() 의 ThrottledSession 을 전달하면
                    DomainThrottle 이 자동 적용됩니다. None 이면 일반 requests.get 사용.
        timeout:    HTTP 요청 타임아웃 (초, 기본 15)

    Returns:
        저장된 썸네일의 프로젝트 루트 상대 경로 문자열
        예: "static/thumbnails/42_3a8f1c0d9e2b.webp"
        또는 None (다운로드 실패 / Pillow 처리 오류 / 지원 불가 포맷)

    Side effects:
        - THUMBNAIL_DIR 가 없으면 자동 생성 (mkdir -p)
        - 같은 article_id + url 조합이면 파일을 덮어씁니다 (멱등)
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        log.error(
            "Pillow 미설치 — 썸네일 생성 불가. `pip install Pillow` 를 실행하세요."
        )
        return None

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

    # ── 2~7. 리사이징 및 저장 ──────────────────────────────
    buf: Optional[io.BytesIO] = None
    img = None
    try:
        buf = io.BytesIO(raw_bytes)
        del raw_bytes   # 원본 바이트 즉시 삭제

        img = Image.open(buf)
        img.load()      # 실제 픽셀 데이터 디코딩 강제 실행

        # RGBA, P(팔레트), L(그레이스케일) → RGB 변환 (WEBP/JPEG 호환)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        # 가로 기준 비율 유지 리사이징 (원본이 이미 작으면 스킵)
        orig_w, orig_h = img.size
        if orig_w > THUMBNAIL_MAX_WIDTH:
            new_h = max(1, round(orig_h * THUMBNAIL_MAX_WIDTH / orig_w))
            img   = img.resize((THUMBNAIL_MAX_WIDTH, new_h), Image.LANCZOS)

        # 저장 경로 결정
        THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
        url_hash  = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
        filename  = f"{article_id}_{url_hash}.webp"
        save_path = THUMBNAIL_DIR / filename

        img.save(save_path, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY)

        thumb_str = str(save_path.relative_to(_PROJECT_ROOT))
        log.debug(
            "thumbnail_saved | path=%s | size=%dx%d",
            thumb_str, *img.size,
        )
        return thumb_str

    except (UnidentifiedImageError, OSError) as exc:
        log.warning("img_pillow_failed | url=%.80s | err=%s", image_url, exc)
        return None
    except Exception as exc:
        log.warning("img_unexpected | url=%.80s | err=%r", image_url, exc)
        return None
    finally:
        # 예외 발생 여부와 무관하게 메모리 정리
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
            del img
        if buf is not None:
            try:
                buf.close()
            except Exception:
                pass
            del buf
