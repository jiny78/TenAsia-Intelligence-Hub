"""
processor/cleaner.py — Gemini 기반 아티클 데이터 정제기

정제 파이프라인:
  1. HTML 클리닝    : 불필요한 태그/스크립트 제거, 텍스트 추출
  2. Gemini 추출    : global_priority 에 따라 전체/최소 추출 분기
  3. Pydantic 검증  : ArticleExtracted 모델로 유효성 검증 + 정규화
  4. 썸네일 처리    : S3 업로드 (image_utils.process_thumbnail)
  5. DB 저장        : scraper.db.upsert_article

사용법:
    cleaner = ArticleCleaner()
    record = cleaner.process(raw_article)
    # record.id, record.title_ko, record.thumbnail_url ...
"""

from __future__ import annotations

import re
import structlog
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import ValidationError

from processor.models import ArticleExtracted, ArticleRecord, RawArticle
from scraper.gemini_engine import GeminiEngine
from scraper.image_utils import process_thumbnail
from scraper.db import upsert_article, get_article_by_url

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# HTML 클리너
# ─────────────────────────────────────────────────────────────

# 제거 대상 태그
_REMOVE_TAGS = {
    "script", "style", "noscript", "iframe",
    "nav", "footer", "header", "aside", "menu",
    "form", "button", "input", "select",
    "svg", "canvas", "video", "audio",
    "ins", "ad", "advertisement",
}

# 유지 대상 속성 (나머지 모두 제거)
_KEEP_ATTRS: dict[str, list[str]] = {
    "a":   ["href"],
    "img": ["src", "alt"],
}


def clean_html(html: str) -> tuple[str, Optional[str]]:
    """
    HTML을 정제하여 (본문 텍스트, 대표 이미지 URL) 을 반환합니다.

    Returns:
        (clean_text, thumbnail_url_or_None)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 불필요한 태그 제거
    for tag in soup.find_all(_REMOVE_TAGS):
        tag.decompose()

    # 광고 클래스/ID 제거
    ad_patterns = re.compile(
        r"(advertisement|ad-|banner|popup|modal|cookie|subscribe)", re.I
    )
    for tag in soup.find_all(class_=ad_patterns):
        tag.decompose()
    for tag in soup.find_all(id=ad_patterns):
        tag.decompose()

    # 대표 이미지 추출 (og:image 우선)
    thumbnail_url: Optional[str] = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        thumbnail_url = og_img["content"]
    else:
        first_img = soup.find("img", src=True)
        if first_img:
            src = first_img.get("src", "")
            if src.startswith("http"):
                thumbnail_url = src

    # 텍스트 추출 및 공백 정리
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip(), thumbnail_url


# ─────────────────────────────────────────────────────────────
# 아티클 정제기
# ─────────────────────────────────────────────────────────────

class ArticleCleaner:
    """
    스크래핑된 원시 아티클을 정제하여 DB에 저장합니다.

    Args:
        engine: GeminiEngine 인스턴스 (없으면 자동 생성)
    """

    def __init__(self, engine: Optional[GeminiEngine] = None) -> None:
        self._engine = engine or GeminiEngine()

    # ── 내부 단계 ─────────────────────────────────────────────

    def _extract(
        self,
        html: str,
        global_priority: bool,
    ) -> dict:
        """Gemini 추출 후 딕셔너리 반환."""
        return self._engine.extract_article(html, global_priority=global_priority)

    @staticmethod
    def _validate(data: dict) -> ArticleExtracted:
        """
        Pydantic 유효성 검증.
        ValidationError 발생 시 detail 로그 후 재발생.
        """
        try:
            return ArticleExtracted(**data)
        except ValidationError as exc:
            logger.warning(
                "Pydantic 유효성 검증 실패",
                errors=exc.errors(),
            )
            raise

    @staticmethod
    def _upload_thumbnail(
        raw_url: Optional[str],
        article_id: int,
    ) -> Optional[str]:
        """S3 업로드. 실패 시 None 반환 (로그만)."""
        if not raw_url:
            return None
        url = process_thumbnail(raw_url, article_id=article_id)
        return url if url else None

    # ── 공개 API ──────────────────────────────────────────────

    def process(
        self,
        raw: RawArticle,
        job_id: Optional[int] = None,
    ) -> ArticleRecord:
        """
        원시 아티클을 정제하여 DB에 저장하고 레코드를 반환합니다.

        Args:
            raw:    RawArticle (source_url, html, language, global_priority)
            job_id: 연결할 job_queue.id (선택)

        Returns:
            ArticleRecord (DB에 저장된 아티클)

        Raises:
            GeminiKillSwitchError: 월 토큰 한도 초과
            ValidationError:       Pydantic 검증 실패
        """
        log = logger.bind(url=str(raw.source_url), global_priority=raw.global_priority)

        # ── 1. HTML 클리닝 ──────────────────────────────────
        log.debug("HTML 클리닝 시작")
        clean_text, raw_thumbnail_url = clean_html(raw.html)
        log.debug("HTML 클리닝 완료", text_len=len(clean_text))

        # ── 2. Gemini 추출 ──────────────────────────────────
        log.info("Gemini 추출 시작")
        extracted_data = self._extract(clean_text, raw.global_priority)

        # ── 3. Pydantic 검증 ────────────────────────────────
        extracted = self._validate(extracted_data)

        # ── 4. DB upsert (썸네일 URL 없이 먼저 저장 → id 확보) ──
        article_id = upsert_article(
            source_url=str(raw.source_url),
            data={
                **extracted.model_dump(),
                "language": raw.language,
            },
            job_id=job_id,
        )
        log.info("아티클 DB 저장 완료", article_id=article_id)

        # ── 5. 썸네일 S3 업로드 (id 확보 후) ────────────────
        s3_url = self._upload_thumbnail(raw_thumbnail_url, article_id)
        if s3_url:
            # S3 URL 업데이트
            upsert_article(
                source_url=str(raw.source_url),
                data={"thumbnail_url": s3_url},
                job_id=job_id,
            )
            extracted = extracted.model_copy(update={"thumbnail_url": s3_url})
            log.info("썸네일 S3 업로드 완료", s3_url=s3_url)
        else:
            log.debug("썸네일 업로드 건너뜀 (URL 없음 또는 실패)")

        # ── 6. ArticleRecord 반환 ────────────────────────────
        import datetime
        now = datetime.datetime.utcnow()
        return ArticleRecord(
            id=article_id,
            source_url=str(raw.source_url),
            language=raw.language,
            job_id=job_id,
            created_at=now,
            updated_at=now,
            **extracted.model_dump(),
        )

    def process_url(
        self,
        source_url: str,
        language: str = "kr",
        global_priority: bool = False,
        job_id: Optional[int] = None,
    ) -> Optional[ArticleRecord]:
        """
        URL에서 직접 아티클을 정제합니다 (HTTP 다운로드 포함).

        이미 처리된 URL이면 DB에서 기존 레코드 반환 (중복 방지).
        """
        # 중복 확인
        existing = get_article_by_url(source_url)
        if existing and existing.get("title_ko"):
            logger.info("이미 처리된 URL — DB 레코드 반환", url=source_url)
            import datetime
            return ArticleRecord(
                **{k: v for k, v in existing.items()
                   if k in ArticleRecord.model_fields},
            )

        # HTTP 다운로드
        import requests
        try:
            resp = requests.get(source_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; TIH-Bot/1.0)",
            })
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.error("URL 다운로드 실패", url=source_url, error=str(exc))
            return None

        raw = RawArticle(
            source_url=source_url,
            html=html,
            language=language,
            global_priority=global_priority,
        )
        return self.process(raw, job_id=job_id)
