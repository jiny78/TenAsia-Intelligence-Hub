"""
scraper/engine.py — 안정적 스크래핑 엔진 (Throttling & Queue)

설계 원칙:
    Throttling (2중 레이어):
        1층 — DomainThrottle (scraper/throttle.py)
              도메인별 최소 간격 + RPM 슬라이딩 윈도우 (Thread-safe)
        2층 — Human Delay (self.delay = random.uniform(2.0, 5.0))
              도메인 간격 외에 추가되는 랜덤 지연 — 봇 패턴 탐지 회피

    배치 처리:
        scrape_batch() 가 URL 목록을 batch_size 개씩 처리하고
        성공 시마다 DB 에 즉시 SCRAPED 커밋 (실패해도 배치 계속 진행)

    이미지 제외:
        <img>, <figure>, <picture>, <video>, <audio>, <iframe> 등 미디어 태그
        및 <script>, <style>, <nav>, <aside> 제거
        텍스트 제목·본문·메타데이터(날짜·기자명)만 추출
        ※ OG 메타태그(og:image)는 thumbnail_url 로 별도 수집 (인라인 이미지 아님)

    에러 핸들링:
        HTTP 403 → ForbiddenError (즉시 배치 중단, 재시도 없음)
        HTTP 429 → RateLimitError (Retry-After 준수 후 지수 백오프, 최대 3회)
        HTTP 5xx / 네트워크 오류 → ScraperError (지수 백오프 재시도)
        파싱 실패 → ParseError (해당 URL 건너뜀, 배치 계속)

기간 필터링:
    scrape_range(start_date, end_date) 으로 특정 날짜 구간의 기사만 수집합니다.
    RSS 피드에서 날짜를 먼저 가져와 필터링하고, RSS 범위 초과 시 목록 페이지를
    페이지네이션합니다. scrape_batch() 내부에서 parsed published_at 으로 이중 확인합니다.
    CLI: python -m scraper.engine scrape-range --start 2026-02-01 --end 2026-02-25

RSS / 최신 감지:
    check_latest() 가 RSS 피드(또는 목록 첫 페이지)를 스캔하여 DB의 최신 published_at
    보다 이후 기사를 감지하고, 자동으로 job_queue 에 일괄 등록합니다.
    CLI: python -m scraper.engine check-latest [--no-queue]

상태 기반 중복 체크:
    scrape_batch() 호출 전 DB 를 일괄 조회하여 URL 을 분류합니다:
        PROCESSED  → 스킵 (skip_processed=True, 기본값)
        ERROR      → 재시도 (retry_error=True, 기본값)
        SCRAPED    → 스킵 (이미 수집 완료, 처리 대기 중)
        PENDING / MANUAL_REVIEW → 스킵
        DB에 없음  → 신규 수집 대상
    스킵된 URL 은 BatchResult.skipped 에 이유와 함께 기록됩니다.

공개 클래스:
    BaseScraper      — 공통 Throttle·Backoff·배치 로직 (abstract)
    TenAsiaScraper   — tenasia.hankyung.com 특화 파서

공개 예외:
    ScraperError     — 기본 스크래퍼 예외
    ForbiddenError   — HTTP 403 (IP/UA 차단, 즉시 중단)
    RateLimitError   — HTTP 429 (요청 과다, 지수 백오프)
    ParseError       — HTML 파싱 실패

사용 예:
    from scraper.engine import TenAsiaScraper

    scraper = TenAsiaScraper(batch_size=10)

    # 기본 배치
    result = scraper.scrape_batch(urls=[...], job_id=42)

    # 날짜 범위 수집
    from datetime import datetime, timezone
    result = scraper.scrape_range(
        start_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 2, 25, tzinfo=timezone.utc),
    )

    # 최신 기사 감지 + 자동 큐 등록
    check = scraper.check_latest(language="kr", auto_queue=True)
    print(check.new_count)  # 새로 큐에 추가된 기사 수
"""

from __future__ import annotations

import abc
import argparse
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime as _rfc2822_parse
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
import structlog
from bs4 import BeautifulSoup, Tag

from scraper.db import (
    create_job,
    get_articles_status_by_urls,
    get_latest_published_at,
    upsert_article,
    upsert_article_image,
)
from scraper.throttle import get_session


# ─────────────────────────────────────────────────────────────
# 모듈 레벨 유틸
# ─────────────────────────────────────────────────────────────

def _ensure_tz(dt: datetime) -> datetime:
    """naive datetime 을 UTC timezone-aware 로 변환합니다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _cli_parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """
    CLI 날짜 문자열 → UTC timezone-aware datetime.

    지원 형식:
        YYYY-MM-DD            → 00:00:00 UTC (또는 23:59:59 UTC if end_of_day)
        YYYY-MM-DDTHH:MM:SS   → 지정 시각 UTC
    """
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if end_of_day and fmt == "%Y-%m-%d":
                from datetime import time as _t
                dt = datetime.combine(dt.date(), _t(23, 59, 59))
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"날짜 형식 오류 (YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM:SS): {value!r}"
    )


# ─────────────────────────────────────────────────────────────
# 예외 계층
# ─────────────────────────────────────────────────────────────

class ScraperError(Exception):
    """기본 스크래퍼 예외 — HTTP 오류·네트워크 오류·재시도 한계 초과"""


class ForbiddenError(ScraperError):
    """
    HTTP 403 Forbidden.
    IP 차단 또는 User-Agent 차단 의심.
    배치 전체를 즉시 중단합니다 — 재시도해도 차단이 지속될 가능성이 높습니다.
    """


class RateLimitError(ScraperError):
    """
    HTTP 429 Too Many Requests.
    지수 백오프 후에도 429가 지속될 때 발생합니다.
    scrape_batch() 는 이 예외를 잡아 해당 URL 을 failed 로 기록하고 계속 진행합니다.
    """


class ParseError(ScraperError):
    """
    HTML 파싱 실패 (필수 필드 추출 불가).
    제목이 없거나 콘텐츠 구조가 예상과 완전히 다를 때 발생합니다.
    해당 URL 을 건너뛰고 배치는 계속 진행합니다.
    """


# ─────────────────────────────────────────────────────────────
# 배치 결과 컨테이너
# ─────────────────────────────────────────────────────────────

@dataclass
class RSSEntry:
    """RSS / 목록 페이지에서 수집한 기사 메타데이터."""

    url:          str
    title:        str                   = ""
    published_at: Optional[datetime]    = None


@dataclass
class BatchResult:
    """scrape_batch() 반환 타입."""

    total:     int
    success:   list[dict[str, Any]] = field(default_factory=list)
    failed:    list[dict[str, Any]] = field(default_factory=list)
    skipped:   list[dict[str, Any]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.success) + len(self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total":     self.total,
            "processed": self.processed,
            "success":   self.success,
            "failed":    self.failed,
            "skipped":   self.skipped,
        }


@dataclass
class CheckResult:
    """check_latest() 반환 타입."""

    new_count:    int
    queued_urls:  list[str]          = field(default_factory=list)
    job_id:       Optional[int]      = None
    latest_db:    Optional[datetime] = None
    latest_feed:  Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "new_count":   self.new_count,
            "queued_urls": self.queued_urls,
            "job_id":      self.job_id,
            "latest_db":   self.latest_db.isoformat() if self.latest_db else None,
            "latest_feed": self.latest_feed.isoformat() if self.latest_feed else None,
        }


# ─────────────────────────────────────────────────────────────
# BaseScraper
# ─────────────────────────────────────────────────────────────

class BaseScraper(abc.ABC):
    """
    스크래퍼 공통 기반 클래스 (abstract).

    서브클래스 구현 의무:
        _parse_article(url, soup) → dict
            BeautifulSoup 에서 title_ko, content_ko, author, published_at 등을 추출해
            반환합니다. thumbnail_url 은 og:image 에서만 수집하고 인라인 <img> 는 무시해야
            합니다. _clean_soup() 호출 전에 OG 메타를 수집하세요.

    Throttling 2중 레이어:
        1층 — ThrottledSession (scraper/throttle.py): 도메인 최소 간격 + RPM
        2층 — _human_delay(): random.uniform(delay_min, delay_max) 추가 대기
              self.delay 는 매 호출마다 갱신됩니다.

    에러 처리:
        403 → ForbiddenError 즉시 raise (재시도 없음)
        429 → Retry-After 준수 후 지수 백오프 (최대 max_retries 회)
        5xx / 네트워크 → 지수 백오프 재시도
    """

    # ── 미디어 태그 (완전 제거 대상) ──────────────────────────
    _MEDIA_TAGS: frozenset[str] = frozenset({
        "img", "figure", "picture",
        "video", "audio", "source", "track",
        "iframe", "embed", "object",
        "canvas", "svg",
    })

    # ── 노이즈 태그 (레이아웃·부수 콘텐츠, 완전 제거) ─────────
    _NOISE_TAGS: frozenset[str] = frozenset({
        "script", "style", "noscript",
        "nav", "header", "footer",
        "aside", "form", "button", "select",
        "input", "textarea",
        "advertisement", "ins",        # 광고
    })

    # ── 한국 뉴스 보일러플레이트 패턴 ────────────────────────
    _BOILERPLATE_PATTERNS: list[str] = [
        r"무단\s*전재\s*(?:및\s*)?재배포\s*금지",
        r"저작권자\s*[©ⓒ(c)]*\s*[\w가-힣\s]+,?\s*무단",
        r"Copyright\s*[©ⓒ]?\s*[\w\s]+\.\s*All\s+Rights\s+Reserved",
        r"기사\s*제보\s*:\s*[\w@.\-]+",
        r"\[[\w가-힣\s]+\s*기자\]",        # [홍길동 기자] 형태 말미 태그 (본문 외)
    ]

    # ── 기본 HTTP 헤더 ────────────────────────────────────────
    _BASE_HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "DNT":             "1",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(
        self,
        delay_min:   float = 2.0,
        delay_max:   float = 5.0,
        max_retries: int   = 3,
        timeout:     int   = 15,
        batch_size:  int   = 10,
    ) -> None:
        """
        Args:
            delay_min:   Human delay 하한 (초). 기본 2.0
            delay_max:   Human delay 상한 (초). 기본 5.0
            max_retries: 429/5xx 재시도 최대 횟수. 기본 3
            timeout:     HTTP 요청 타임아웃 (초). 기본 15
            batch_size:  한 번에 처리할 최대 URL 수. 기본 10
        """
        self.delay_min   = delay_min
        self.delay_max   = delay_max
        self.max_retries = max_retries
        self.timeout     = timeout
        self.batch_size  = batch_size

        # self.delay: 마지막으로 적용된 human delay 값 (매 요청마다 갱신)
        self.delay: float = random.uniform(delay_min, delay_max)

        # ThrottledSession: 도메인 최소 간격 + RPM + urllib3 Retry
        self._session: requests.Session = get_session(
            user_agent=self._BASE_HEADERS["User-Agent"]
        )
        self._session.headers.update(self._BASE_HEADERS)

        self.log = structlog.get_logger(self.__class__.__name__)

    # ─────────────────────────────────────────────────────────
    # Throttling
    # ─────────────────────────────────────────────────────────

    def _human_delay(self) -> None:
        """
        인간 행동 모방 추가 지연.

        DomainThrottle 의 최소 간격(1초)에 더해 random.uniform(delay_min, delay_max)
        초의 랜덤 대기를 추가합니다. self.delay 는 매 호출마다 새로 생성됩니다.
        """
        self.delay = random.uniform(self.delay_min, self.delay_max)
        self.log.debug("human_delay", wait_sec=round(self.delay, 2))
        time.sleep(self.delay)

    # ─────────────────────────────────────────────────────────
    # HTTP 요청 (Fetch + 에러 핸들링)
    # ─────────────────────────────────────────────────────────

    def _backoff(self, attempt: int, base: float = 2.0) -> None:
        """
        지수 백오프 (Exponential Backoff with Jitter).

        대기 시간 = base × 2^attempt + uniform(0, 1)
            attempt=0 → ~2s
            attempt=1 → ~4s
            attempt=2 → ~8s
        """
        wait = base * (2 ** attempt) + random.uniform(0.0, 1.0)
        self.log.warning("backoff", attempt=attempt, wait_sec=round(wait, 2))
        time.sleep(wait)

    def _fetch(self, url: str) -> requests.Response:
        """
        HTTP GET with 에러 감지 및 지수 백오프.

        동작 순서 (매 시도):
            1. _human_delay() — random 2~5 초 추가 대기
            2. ThrottledSession.get() — 도메인 간격 + RPM 제어 (자동)
            3. 403 감지 → ForbiddenError (즉시 raise, 재시도 없음)
            4. 429 감지 → Retry-After 준수 후 재시도
            5. 5xx / 네트워크 오류 → 지수 백오프 후 재시도

        Returns:
            성공한 requests.Response

        Raises:
            ForbiddenError: HTTP 403
            RateLimitError: 429 가 max_retries 회 이상 지속
            ScraperError:   그 외 HTTP 오류 또는 재시도 한계 초과
        """
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):

            # 첫 번째 이후 시도: 지수 백오프 먼저 적용
            if attempt > 0:
                self._backoff(attempt - 1)

            # Human delay (매 시도마다 새로운 랜덤 값)
            self._human_delay()

            try:
                self.log.info(
                    "fetch",
                    url=url,
                    attempt=attempt,
                    delay=round(self.delay, 2),
                )
                resp = self._session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                )

            except requests.exceptions.ConnectionError as exc:
                self.log.warning("connection_error", url=url, attempt=attempt, error=str(exc))
                last_exc = exc
                continue  # 재시도

            except requests.exceptions.Timeout as exc:
                self.log.warning("timeout", url=url, attempt=attempt, timeout=self.timeout)
                last_exc = exc
                continue

            # ── 응답 코드별 분기 ──────────────────────────────

            # 403: IP / User-Agent 차단 → 즉시 중단 (재시도 불필요)
            if resp.status_code == 403:
                self.log.error(
                    "forbidden",
                    url=url,
                    status=403,
                    hint="IP 또는 User-Agent 차단 의심 — 프록시/UA 변경 필요",
                )
                raise ForbiddenError(
                    f"HTTP 403 Forbidden — IP/UA 차단 의심: {url}"
                )

            # 429: Too Many Requests → Retry-After 준수 후 재시도
            if resp.status_code == 429:
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"HTTP 429 Too Many Requests — 재시도 한계({self.max_retries}회) 초과: {url}"
                    )
                retry_after = int(resp.headers.get("Retry-After", 30))
                jitter       = random.uniform(1.0, 5.0)
                total_wait   = retry_after + jitter
                self.log.warning(
                    "rate_limited",
                    url=url,
                    attempt=attempt,
                    retry_after=retry_after,
                    total_wait=round(total_wait, 1),
                )
                time.sleep(total_wait)
                continue

            # 기타 HTTP 오류 (5xx 등)
            if not resp.ok:
                self.log.warning(
                    "http_error",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt,
                )
                last_exc = requests.exceptions.HTTPError(
                    f"HTTP {resp.status_code}", response=resp
                )
                continue  # 재시도

            # 성공
            self.log.info(
                "fetch_ok",
                url=url,
                status=resp.status_code,
                bytes=len(resp.content),
            )
            return resp

        # 모든 재시도 소진
        raise ScraperError(
            f"최대 재시도({self.max_retries}회) 초과: {url}"
        ) from last_exc

    # ─────────────────────────────────────────────────────────
    # HTML 정제 (이미지·노이즈 제거)
    # ─────────────────────────────────────────────────────────

    @classmethod
    def _clean_soup(cls, soup: BeautifulSoup) -> BeautifulSoup:
        """
        미디어 태그와 레이아웃 노이즈 완전 제거.

        제거 대상:
            미디어 : img, figure, picture, video, audio, iframe, embed, ...
            노이즈 : script, style, nav, header, footer, aside, form, ...

        ※ 이 메서드 호출 전에 og:image 등 메타데이터를 먼저 수집하세요.
        """
        for tag_name in cls._MEDIA_TAGS | cls._NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        return soup

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """
        텍스트 후처리:
            - 연속 공백 → 단일 공백
            - 연속 줄바꿈 3개 이상 → 2개
            - 한국 뉴스 보일러플레이트 제거
        """
        text = re.sub(r"[ \t]+",  " ",    text)
        text = re.sub(r"\n{3,}",  "\n\n", text)
        for pattern in cls._BOILERPLATE_PATTERNS:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        return text.strip()

    # ─────────────────────────────────────────────────────────
    # 날짜 파싱 유틸
    # ─────────────────────────────────────────────────────────

    _DATE_FORMATS: list[str] = [
        "%Y-%m-%dT%H:%M:%S%z",   # ISO 8601 with tz
        "%Y-%m-%dT%H:%M:%SZ",    # ISO 8601 UTC
        "%Y-%m-%dT%H:%M:%S",     # ISO 8601 no tz
        "%Y-%m-%d %H:%M:%S",     # Standard datetime
        "%Y-%m-%d %H:%M",        # Without seconds
        "%Y.%m.%d %H:%M:%S",     # Dot-separated with seconds
        "%Y.%m.%d %H:%M",        # Dot-separated
        "%Y.%m.%d",              # Date only dot
        "%Y-%m-%d",              # Date only dash
        "%Y/%m/%d %H:%M",        # Slash-separated
    ]

    @classmethod
    def _parse_datetime(cls, value: str) -> Optional[datetime]:
        """다양한 날짜 문자열을 datetime 으로 파싱합니다."""
        if not value:
            return None
        value = value.strip()

        # 한국 날짜 형식 전처리: "2024년 01월 15일" → "2024-01-15"
        value = re.sub(
            r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일",
            lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
            value,
        )

        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        # dateutil fallback
        try:
            from dateutil import parser as dateutil_parser  # optional dependency
            return dateutil_parser.parse(value)
        except (ImportError, ValueError, OverflowError):
            pass

        return None

    # ─────────────────────────────────────────────────────────
    # 상태 기반 중복 체크
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _classify_urls(
        urls:            list[str],
        skip_processed:  bool = True,
        retry_error:     bool = True,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """
        URL 목록을 DB 의 process_status 에 따라 분류합니다.

        분류 규칙:
            DB에 없음      → 신규 (수집 대상)
            PROCESSED      → skip_processed=True 면 스킵
            ERROR          → retry_error=True 면 재시도 (수집 대상)
            SCRAPED        → 이미 수집 완료, 스킵
            PENDING        → 이미 큐에 있음, 스킵
            MANUAL_REVIEW  → 검수 대기 중, 스킵

        Returns:
            (to_scrape, skipped_records)
            to_scrape       : 실제 스크래핑할 URL 목록
            skipped_records : 스킵된 URL 와 reason 딕셔너리 목록
        """
        statuses = get_articles_status_by_urls(urls)
        to_scrape: list[str]           = []
        skipped:   list[dict[str, Any]] = []

        for url in urls:
            status = statuses.get(url)          # None = 신규

            if status is None:
                to_scrape.append(url)
            elif status == "PROCESSED" and skip_processed:
                skipped.append({"url": url, "reason": "already_processed"})
            elif status == "ERROR" and retry_error:
                to_scrape.append(url)           # ERROR → 재시도
            elif status in ("SCRAPED", "PENDING", "MANUAL_REVIEW"):
                skipped.append({"url": url, "reason": f"status_{status.lower()}"})
            else:
                # skip_processed=False 또는 retry_error=False — 강제 수집
                to_scrape.append(url)

        return to_scrape, skipped

    # ─────────────────────────────────────────────────────────
    # 후처리 훅 (서브클래스 선택적 재정의)
    # ─────────────────────────────────────────────────────────

    def _on_article_saved(self, article_id: int, data: dict[str, Any]) -> None:
        """
        DB 저장 성공 직후 호출되는 후처리 훅.

        기본 구현은 아무것도 하지 않습니다.
        서브클래스에서 재정의하여 이미지 처리, 태그 추출 등 추가 작업을 수행합니다.

        Args:
            article_id: 방금 저장된 articles.id
            data:       _parse_article() + 공통 필드가 병합된 전체 데이터 딕셔너리
        """

    # ─────────────────────────────────────────────────────────
    # 추상 메서드 (서브클래스 구현 필수)
    # ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    def _parse_article(self, url: str, soup: BeautifulSoup) -> dict[str, Any]:
        """
        URL + BeautifulSoup → 아티클 데이터 딕셔너리.

        구현 지침:
            1. og:image 등 메타데이터를 먼저 수집한 뒤 _clean_soup(soup) 호출
            2. 제목·본문·저자·날짜를 추출하여 딕셔너리로 반환
            3. 필수 필드(title_ko)가 없으면 ParseError 를 raise 할 것

        반환 키 (모두 Optional, title_ko 만 필수):
            title_ko       : 한국어 제목 (필수)
            content_ko     : 본문 텍스트 (이미지 제외, 순수 텍스트)
            author         : 기자명
            published_at   : 발행 datetime
            thumbnail_url  : 대표 이미지 URL (og:image 에서만 수집)
        """
        ...

    # ─────────────────────────────────────────────────────────
    # 배치 처리 (핵심 루프)
    # ─────────────────────────────────────────────────────────

    def scrape_batch(
        self,
        urls:            list[str],
        job_id:          Optional[int]  = None,
        language:        str            = "kr",
        global_priority: bool           = False,
        skip_processed:  bool           = True,
        retry_error:     bool           = True,
        date_after:      Optional[datetime] = None,
        date_before:     Optional[datetime] = None,
        dry_run:         bool           = False,
    ) -> BatchResult:
        """
        URL 목록을 배치로 스크래핑합니다.

        - 최대 batch_size 개의 URL 만 처리합니다.
        - 호출 전 DB 에서 상태를 일괄 조회하여 중복 처리를 방지합니다.
        - 성공 시마다 DB 에 즉시 SCRAPED 상태로 커밋합니다.
        - ForbiddenError(403) 발생 시 배치 전체를 즉시 중단합니다.
        - 개별 URL 오류는 failed 에 기록하고 배치를 계속 진행합니다.

        Args:
            urls:            스크래핑할 URL 목록
            job_id:          연결된 job_queue.id (없으면 None)
            language:        기사 언어 코드 ('kr' / 'en' / 'jp')
            global_priority: 글로벌 아티스트 여부 (True → 영어 추출 활성화)
            skip_processed:  True 면 PROCESSED 기사 스킵 (기본 True)
            retry_error:     True 면 ERROR 기사 재시도 (기본 True)
            date_after:      이 날짜 이전 기사는 스킵 (포함 경계, None=필터 없음)
            date_before:     이 날짜 이후 기사는 스킵 (포함 경계, None=필터 없음)
            dry_run:         True 면 HTTP 요청·파싱은 수행하되 DB 에 커밋하지 않음.
                             [DRY RUN] 태그로 결과를 로그에 출력합니다.

        Returns:
            BatchResult(total, success, failed, skipped)
        """
        batch = urls[:self.batch_size]

        # ── 상태 기반 중복 체크 ────────────────────────────────
        to_scrape, status_skipped = self._classify_urls(
            batch,
            skip_processed=skip_processed,
            retry_error=retry_error,
        )
        result = BatchResult(total=len(urls), skipped=list(status_skipped))

        if status_skipped:
            reason_counts: dict[str, int] = {}
            for s in status_skipped:
                reason_counts[s["reason"]] = reason_counts.get(s["reason"], 0) + 1
            self.log.info("batch_status_skip", counts=reason_counts)

        if not to_scrape:
            self.log.info("batch_all_skipped", total_skipped=len(status_skipped))
            return result

        # ── 날짜 경계 UTC 변환 ─────────────────────────────────
        _after  = _ensure_tz(date_after)  if date_after  else None
        _before = _ensure_tz(date_before) if date_before else None

        self.log.info(
            "batch_start",
            total_urls=len(urls),
            to_scrape=len(to_scrape),
            skipped=len(status_skipped),
            job_id=job_id,
            dry_run=dry_run,
        )
        if dry_run:
            self.log.info("[DRY RUN] DB 커밋 없이 스크래핑·파싱만 수행합니다.")

        for idx, url in enumerate(to_scrape, start=1):
            self.log.info(
                "batch_item",
                current=idx,
                total=len(to_scrape),
                url=url,
            )

            try:
                # 1. HTTP 요청 (Throttle + Human Delay + Backoff 포함)
                resp = self._fetch(url)

                # 2. HTML 파싱 — raw soup 을 _parse_article 에 전달
                soup = BeautifulSoup(resp.text, "html.parser")
                data = self._parse_article(url, soup)

                # 3. 날짜 범위 필터 ──────────────────────────────
                if _after or _before:
                    pub = data.get("published_at")
                    if pub is not None:
                        pa = _ensure_tz(pub)
                        if _after and pa < _after:
                            result.skipped.append({
                                "url":          url,
                                "reason":       "before_date_range",
                                "published_at": pa.isoformat(),
                            })
                            self.log.debug("date_skip_early", url=url, pa=pa.isoformat())
                            continue
                        if _before and pa > _before:
                            result.skipped.append({
                                "url":          url,
                                "reason":       "after_date_range",
                                "published_at": pa.isoformat(),
                            })
                            self.log.debug("date_skip_late", url=url, pa=pa.isoformat())
                            continue

                # 4. 공통 필드 병합
                data.setdefault("language",        language)
                data.setdefault("global_priority", global_priority)
                data["process_status"] = "SCRAPED"

                if dry_run:
                    # [DRY RUN] DB 커밋 없이 수집 결과만 로그 출력
                    result.success.append({
                        "url":          url,
                        "article_id":   None,
                        "title_ko":     str(data.get("title_ko", ""))[:60],
                        "published_at": str(data.get("published_at") or ""),
                        "dry_run":      True,
                    })
                    self.log.info(
                        "[DRY RUN] scraped_preview",
                        url=url,
                        title=str(data.get("title_ko", ""))[:50],
                        content_len=len(data.get("content_ko") or ""),
                        published_at=str(data.get("published_at") or ""),
                    )
                else:
                    # 5. DB 즉시 커밋 (UPSERT)
                    article_id = upsert_article(url, data, job_id=job_id)

                    # 6. 후처리 훅 (이미지 저장 등 — 서브클래스에서 구현)
                    self._on_article_saved(article_id, data)

                    result.success.append({
                        "url":        url,
                        "article_id": article_id,
                        "title_ko":   str(data.get("title_ko", ""))[:60],
                    })
                    self.log.info(
                        "scraped_ok",
                        url=url,
                        article_id=article_id,
                        title=str(data.get("title_ko", ""))[:50],
                        content_len=len(data.get("content_ko") or ""),
                    )

            except ForbiddenError as exc:
                # 403: 배치 전체 즉시 중단
                self.log.error(
                    "forbidden_abort",
                    url=url,
                    processed_before_abort=len(result.success),
                    msg=str(exc),
                )
                result.failed.append({
                    "url":   url,
                    "error": "forbidden",
                    "fatal": True,
                })
                break

            except (RateLimitError, ParseError, ScraperError) as exc:
                self.log.warning(
                    "article_failed",
                    url=url,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                result.failed.append({"url": url, "error": str(exc)})

            except Exception as exc:
                self.log.error(
                    "unexpected_error",
                    url=url,
                    error_type=type(exc).__name__,
                    error=repr(exc),
                )
                result.failed.append({"url": url, "error": repr(exc)})

        self.log.info(
            "batch_done",
            success=len(result.success),
            failed=len(result.failed),
            skipped=len(result.skipped),
            total_processed=result.processed,
        )
        return result


# ─────────────────────────────────────────────────────────────
# TenAsiaScraper
# ─────────────────────────────────────────────────────────────

class TenAsiaScraper(BaseScraper):
    """
    tenasia.hankyung.com 특화 파서.

    추출 전략 (우선순위 순):
        1. JSON-LD (application/ld+json) — 가장 신뢰도 높음
        2. Open Graph / Twitter Card 메타태그
        3. CSS 셀렉터 (도메인 특화 클래스명 기반)
        4. 공통 HTML 폴백 (h1, time[datetime] 등)

    이미지 처리:
        - <img> 포함 모든 미디어 태그는 _clean_soup() 로 완전 제거
        - thumbnail_url 은 og:image 메타태그에서만 수집 (인라인 이미지 아님)
    """

    # TenAsia 도메인 특화 셀렉터 (우선순위 순)
    _TITLE_SELECTORS: list[str] = [
        "h1.article-title",
        "h1.headline",
        "h1[itemprop='headline']",
        ".article_title h1",
        ".news_tit",
        "h1",
    ]

    _CONTENT_SELECTORS: list[str] = [
        "div.article-body",
        "div.article_view",
        "div#article_body",
        "div#articleBody",
        "section.article-content",
        "div[itemprop='articleBody']",
        "div.news_cnt_detail_wrap",
        "div.article_txt",
    ]

    _AUTHOR_SELECTORS: list[str] = [
        "[itemprop='author'] [itemprop='name']",
        ".reporter_name",
        ".article_info .name",
        "span.reporter",
        "em.reporter",
        ".byline .name",
        "meta[name='author']",
    ]

    _DATE_SELECTORS: list[str] = [
        "time[datetime]",
        "[itemprop='datePublished']",
        "meta[property='article:published_time']",
        ".article_date time",
        ".date",
        "span.date_info",
    ]

    # ── JSON-LD 추출 ──────────────────────────────────────────

    @staticmethod
    def _extract_ld_json(soup: BeautifulSoup) -> dict[str, Any]:
        """
        <script type="application/ld+json"> 에서 NewsArticle 데이터 추출.
        여러 블록이 있으면 @type == NewsArticle | Article 인 것을 우선합니다.
        """
        candidates: list[dict] = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    candidates.extend(data)
                elif isinstance(data, dict):
                    candidates.append(data)
            except (json.JSONDecodeError, TypeError):
                continue

        for candidate in candidates:
            dtype = candidate.get("@type", "")
            if isinstance(dtype, list):
                dtype = " ".join(dtype)
            if "article" in dtype.lower() or "newsarticle" in dtype.lower():
                return candidate
        # NewsArticle 이 없으면 첫 번째 딕셔너리 반환
        return candidates[0] if candidates else {}

    # ── OG / Twitter 메타태그 추출 ────────────────────────────

    @staticmethod
    def _extract_og_meta(soup: BeautifulSoup) -> dict[str, str]:
        """og:title, og:description, og:image, article:published_time 수집."""
        meta: dict[str, str] = {}
        for tag in soup.find_all("meta"):
            prop  = tag.get("property") or tag.get("name") or ""
            content = tag.get("content", "").strip()
            if content:
                meta[prop] = content
        return meta

    # ── 필드별 추출 헬퍼 ─────────────────────────────────────

    @classmethod
    def _extract_title(cls, soup: BeautifulSoup, ld: dict, og: dict) -> Optional[str]:
        # 1. JSON-LD
        if headline := (ld.get("headline") or ld.get("name")):
            return str(headline).strip()
        # 2. OG
        if og_title := og.get("og:title"):
            return og_title.strip()
        # 3. CSS 셀렉터
        for selector in cls._TITLE_SELECTORS:
            if tag := soup.select_one(selector):
                text = tag.get_text(strip=True)
                if text:
                    return text
        # 4. <title> 폴백 (사이트명 제거)
        if title_tag := soup.find("title"):
            title = title_tag.get_text(strip=True)
            # " | 사이트명" 패턴 제거
            title = re.split(r"\s*[|·—]\s*", title)[0].strip()
            if title:
                return title
        return None

    @classmethod
    def _extract_author(cls, soup: BeautifulSoup, ld: dict, og: dict) -> Optional[str]:
        # 1. JSON-LD
        if author_data := ld.get("author"):
            if isinstance(author_data, dict):
                return str(author_data.get("name", "")).strip() or None
            if isinstance(author_data, list) and author_data:
                return str(author_data[0].get("name", "")).strip() or None
        # 2. meta[name="author"]
        if author := og.get("author"):
            return author.strip()
        # 3. CSS 셀렉터
        for selector in cls._AUTHOR_SELECTORS:
            tag = soup.select_one(selector)
            if tag is None:
                continue
            # <meta> 는 content 속성 사용
            value = tag.get("content") if tag.name == "meta" else tag.get_text(strip=True)
            if value:
                # "홍길동 기자" → "홍길동" (기자 suffix 제거)
                value = re.sub(r"\s*기자$", "", str(value)).strip()
                if value:
                    return value
        return None

    @classmethod
    def _extract_published_at(
        cls, soup: BeautifulSoup, ld: dict, og: dict
    ) -> Optional[datetime]:
        # 1. JSON-LD
        for key in ("datePublished", "dateCreated"):
            if raw := ld.get(key):
                dt = cls._parse_datetime(str(raw))
                if dt:
                    return dt
        # 2. OG / Twitter
        for key in ("article:published_time", "article:published_date", "pubdate"):
            if raw := og.get(key):
                dt = cls._parse_datetime(raw)
                if dt:
                    return dt
        # 3. CSS 셀렉터
        for selector in cls._DATE_SELECTORS:
            tag = soup.select_one(selector)
            if tag is None:
                continue
            raw = (
                tag.get("datetime")
                or tag.get("content")
                or tag.get_text(strip=True)
            )
            if raw:
                dt = cls._parse_datetime(str(raw))
                if dt:
                    return dt
        return None

    @classmethod
    def _extract_content(cls, soup: BeautifulSoup, ld: dict) -> Optional[str]:
        # 1. JSON-LD articleBody
        if body := ld.get("articleBody"):
            cleaned = cls._clean_text(str(body))
            if len(cleaned) >= 50:
                return cleaned

        # 2. CSS 셀렉터로 본문 컨테이너 탐색
        container: Optional[Tag] = None
        for selector in cls._CONTENT_SELECTORS:
            container = soup.select_one(selector)
            if container:
                break

        # 3. 컨테이너를 못 찾으면 <article> 폴백
        if container is None:
            container = soup.find("article")

        if container is None:
            return None

        # 단락 구조 보존: <p> 태그 텍스트를 개행으로 결합
        paragraphs: list[str] = []
        for p in container.find_all(["p", "div"], recursive=False):
            # 중첩 div는 직계 자식만 → 재귀 제한으로 과다 중복 방지
            text = p.get_text(separator=" ", strip=True)
            if text and len(text) >= 15:   # 짧은 UI 텍스트 필터링
                paragraphs.append(text)

        if not paragraphs:
            # <p> 없으면 전체 텍스트 추출
            raw = container.get_text(separator="\n", strip=True)
            return cls._clean_text(raw) or None

        return cls._clean_text("\n\n".join(paragraphs)) or None

    @staticmethod
    def _extract_thumbnail(og: dict) -> Optional[str]:
        """og:image 에서만 thumbnail_url 수집 — 인라인 <img> 아님."""
        return og.get("og:image") or og.get("twitter:image") or None

    @staticmethod
    def _extract_image_urls(soup: BeautifulSoup) -> list[tuple[str, Optional[str]]]:
        """
        HTML에서 모든 <img> 태그의 이미지 URL 과 alt 텍스트를 수집합니다.

        반드시 _clean_soup() 호출 **전** 에 실행해야 합니다.
        _clean_soup() 가 <img> 태그를 모두 제거하기 때문입니다.

        수집 속성 우선순위:
            src → data-src → data-lazy-src → data-original
            (lazy loading / 지연 로딩 속성 지원)

        필터링:
            - URL 이 http(s):// 로 시작하지 않으면 제외 (data URI, 상대 경로 제외)
            - 중복 URL 제거 (첫 번째 occurrence 유지)

        Returns:
            [(url, alt_text), ...] — alt_text 는 없으면 None
        """
        results: list[tuple[str, Optional[str]]] = []
        seen: set[str] = set()

        for img_tag in soup.find_all("img"):
            url = ""
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                candidate = img_tag.get(attr, "").strip()
                if candidate:
                    url = candidate
                    break

            if not url or not url.startswith("http"):
                continue
            if url in seen:
                continue

            seen.add(url)
            alt = img_tag.get("alt", "").strip() or None
            results.append((url, alt))

        return results

    # ── 핵심 파싱 메서드 (BaseScraper 구현) ──────────────────

    def _parse_article(self, url: str, soup: BeautifulSoup) -> dict[str, Any]:
        """
        URL + BeautifulSoup → articles 테이블 데이터 딕셔너리.

        처리 순서:
            1. JSON-LD 구조화 데이터 추출 (가장 신뢰도 높음)
            2. OG / Twitter 메타태그 추출
            3. thumbnail_url 수집 (og:image — 인라인 이미지 아님)
            4. _clean_soup(): 미디어·노이즈 태그 완전 제거
            5. 제목·본문·저자·날짜 추출 (LD-JSON → OG → CSS 우선순위)

        Raises:
            ParseError: title_ko 를 추출할 수 없을 때
        """
        # 1. 구조화 데이터 우선 수집 (soup 변경 전)
        ld = self._extract_ld_json(soup)
        og = self._extract_og_meta(soup)

        # 2. thumbnail_url — og:image 에서만, 인라인 <img> 아님
        thumbnail_url = self._extract_thumbnail(og)

        # 2b. 본문 인라인 이미지 URL 수집 (_clean_soup 전 — 이후 <img> 전부 제거됨)
        image_urls = self._extract_image_urls(soup)

        # 3. 미디어·노이즈 제거 (이후 soup 은 텍스트만 남음)
        self._clean_soup(soup)

        # 4. 필드 추출
        title_ko     = self._extract_title(soup, ld, og)
        content_ko   = self._extract_content(soup, ld)
        author       = self._extract_author(soup, ld, og)
        published_at = self._extract_published_at(soup, ld, og)

        # title 은 필수 — 없으면 파싱 실패
        if not title_ko:
            raise ParseError(
                f"제목을 추출할 수 없습니다 (HTML 구조가 예상과 다름): {url}"
            )

        self.log.debug(
            "parsed",
            url=url,
            title=title_ko[:50],
            content_len=len(content_ko or ""),
            has_author=author is not None,
            has_date=published_at is not None,
        )

        return {
            "title_ko":      title_ko,
            "content_ko":    content_ko,
            "author":        author,
            "published_at":  published_at,
            "thumbnail_url": thumbnail_url,
            # 본문 인라인 이미지 (url, alt_text) 튜플 목록.
            # _process_article_images() 에서 소비됩니다.
            # article_images 테이블 저장 + 썸네일 생성에 사용됩니다.
            "image_urls":    image_urls,
        }

    # ── 이미지 후처리 ─────────────────────────────────────────

    def _on_article_saved(self, article_id: int, data: dict[str, Any]) -> None:
        """
        DB 저장 후 이미지 처리 훅 (BaseScraper 재정의).

        data["image_urls"] 의 인라인 이미지와 data["thumbnail_url"] (og:image) 을
        article_images 테이블에 저장하고 썸네일을 생성합니다.
        """
        image_urls   = data.get("image_urls") or []
        og_thumbnail = data.get("thumbnail_url")

        if not image_urls and not og_thumbnail:
            return

        self._process_article_images(
            article_id=article_id,
            image_urls=image_urls,
            og_thumbnail=og_thumbnail,
        )

    def _process_article_images(
        self,
        article_id:  int,
        image_urls:  list[tuple[str, Optional[str]]],
        og_thumbnail: Optional[str] = None,
    ) -> None:
        """
        수집된 이미지 URL 을 article_images 테이블에 저장하고 썸네일을 생성합니다.

        처리 순서:
            1. og:image (is_representative=True) 를 먼저 처리합니다.
            2. 본문 <img> URL 을 순서대로 처리합니다.
               og:image 와 동일한 URL 은 중복 처리하지 않습니다.

        Throttling:
            이미지 다운로드 전 _human_delay() 를 호출하여 스크래퍼와 동일한
            2-레이어 쓰로틀링을 적용합니다:
                1층 — ThrottledSession (self._session): DomainThrottle 자동 적용
                2층 — _human_delay():  random 2~5초 추가 대기 (봇 패턴 회피)

        Args:
            article_id:   소속 articles.id
            image_urls:   _extract_image_urls() 의 [(url, alt_text)] 목록
            og_thumbnail: og:image URL (대표 이미지, None 허용)
        """
        from core.image_utils import generate_thumbnail

        # 처리 대상: og:image 선두(representative), 이후 본문 이미지 순
        og_set: set[str] = {og_thumbnail} if og_thumbnail else set()

        # [(url, alt_text, is_representative)]
        to_process: list[tuple[str, Optional[str], bool]] = []

        if og_thumbnail:
            to_process.append((og_thumbnail, None, True))

        for img_url, alt_text in image_urls:
            if img_url not in og_set:
                to_process.append((img_url, alt_text, False))

        if not to_process:
            return

        self.log.info(
            "img_batch_start",
            article_id=article_id,
            count=len(to_process),
        )

        for img_url, alt_text, is_rep in to_process:
            try:
                # ── Throttling: 스크래퍼와 동일한 2-레이어 적용 ──────
                # Layer 1: self._session (ThrottledSession) — DomainThrottle 자동
                # Layer 2: _human_delay() — random 2~5초 추가 지연
                self._human_delay()

                thumb_path = generate_thumbnail(
                    image_url=img_url,
                    article_id=article_id,
                    session=self._session,   # ThrottledSession 재사용
                )

                upsert_article_image(
                    article_id=article_id,
                    original_url=img_url,
                    thumbnail_path=thumb_path,
                    is_representative=is_rep,
                    alt_text=alt_text,
                )

                self.log.info(
                    "img_saved",
                    article_id=article_id,
                    url=img_url[:70],
                    thumb=thumb_path,
                    representative=is_rep,
                )

            except Exception as exc:
                self.log.warning(
                    "img_failed",
                    article_id=article_id,
                    url=img_url[:70],
                    error=str(exc),
                )

        self.log.info(
            "img_batch_done",
            article_id=article_id,
            processed=len(to_process),
        )

    # ── RSS / 목록 페이지 ─────────────────────────────────────

    # RSS 피드 URL — 환경·운영 정책에 따라 서브클래스에서 재정의 가능
    _RSS_URL:       str = "https://tenasia.hankyung.com/rss/allnews.rss"
    _LIST_BASE_URL: str = "https://tenasia.hankyung.com"
    _LIST_PATH:     str = "/all"

    # 목록 페이지에서 기사 링크를 찾기 위한 CSS 셀렉터 (우선순위 순)
    _LIST_PAGE_SELECTORS: list[str] = [
        "article.news-item a",
        ".article-list a",
        ".news_list li a",
        "ul.list_news li a",
        ".content_list .item a",
        "div.list_area a",
    ]

    @classmethod
    def _parse_rss_date(cls, raw: str) -> Optional[datetime]:
        """
        RFC 2822 pubDate (RSS 2.0) → datetime.
        파싱 실패 시 _parse_datetime() 폴백.
        """
        if not raw:
            return None
        try:
            return _rfc2822_parse(raw)
        except (TypeError, ValueError):
            return cls._parse_datetime(raw)

    @classmethod
    def _parse_rss_xml(cls, xml_text: str) -> list[RSSEntry]:
        """
        RSS 2.0 / Atom XML 을 파싱합니다.

        - 네임스페이스 선언을 제거한 뒤 파싱해 네임스페이스 prefix 의존성을 없앱니다.
        - root.iter() 로 깊이에 무관하게 <item> / <entry> 를 수집합니다.
        """
        try:
            cleaned = re.sub(r'\s+xmlns(?::\w+)?="[^"]+"', "", xml_text, flags=re.I)
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return []

        entries: list[RSSEntry] = []

        # RSS 2.0 — <item> 요소
        for item in root.iter("item"):
            url   = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if url:
                entries.append(RSSEntry(
                    url=url,
                    title=title,
                    published_at=cls._parse_rss_date(pub),
                ))

        # Atom — <entry> 요소 (RSS 가 없을 때)
        if not entries:
            for entry in root.iter("entry"):
                link_tag = entry.find("link")
                url = (
                    link_tag.get("href", "") if link_tag is not None else ""
                ).strip()
                if not url:
                    url = (entry.findtext("link") or "").strip()
                title = (entry.findtext("title") or "").strip()
                pub   = (
                    entry.findtext("published")
                    or entry.findtext("updated")
                    or ""
                ).strip()
                if url:
                    entries.append(RSSEntry(
                        url=url,
                        title=title,
                        published_at=cls._parse_datetime(pub),
                    ))

        return entries

    def _fetch_rss(self) -> list[RSSEntry]:
        """
        RSS 피드를 취득하여 파싱합니다.
        Human delay 없이 ThrottledSession 으로 직접 GET 합니다.
        실패 시 빈 리스트를 반환합니다.
        """
        if not self._RSS_URL:
            return []
        try:
            resp = self._session.get(self._RSS_URL, timeout=self.timeout)
            if not resp.ok:
                self.log.warning(
                    "rss_fetch_failed",
                    url=self._RSS_URL,
                    status=resp.status_code,
                )
                return []
            entries = self._parse_rss_xml(resp.text)
            self.log.info("rss_fetched", count=len(entries), url=self._RSS_URL)
            return entries
        except Exception as exc:
            self.log.warning("rss_error", url=self._RSS_URL, error=str(exc))
            return []

    def _fetch_list_page(self, page: int = 1) -> list[RSSEntry]:
        """
        기사 목록 페이지에서 URL + 날짜를 추출합니다 (RSS 폴백).

        - _LIST_PAGE_SELECTORS 의 셀렉터를 순서대로 시도합니다.
        - 날짜는 링크 주변 <time datetime> 또는 .date / .time 클래스에서 탐색합니다.
        - 아티클 URL 패턴(`/article` 또는 8자리 이상 숫자)을 기준으로 필터링합니다.
        """
        url = f"{self._LIST_BASE_URL}{self._LIST_PATH}"
        if page > 1:
            url = f"{url}?page={page}"

        try:
            resp = self._session.get(url, timeout=self.timeout)
            if not resp.ok:
                self.log.warning("list_page_failed", url=url, status=resp.status_code)
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            entries: list[RSSEntry] = []
            seen: set[str] = set()

            def _extract_date_near(tag: Tag) -> Optional[datetime]:
                """링크 태그 주변(최대 3단계 부모)에서 날짜를 탐색합니다."""
                parent = tag.parent
                for _ in range(3):
                    if parent is None:
                        break
                    time_tag = parent.find("time")
                    if time_tag:
                        raw = time_tag.get("datetime") or time_tag.get_text(strip=True)
                        return self._parse_datetime(str(raw)) if raw else None
                    date_tag = parent.find(
                        class_=re.compile(r"\bdate\b|\btime\b", re.I)
                    )
                    if date_tag:
                        return self._parse_datetime(date_tag.get_text(strip=True))
                    parent = parent.parent
                return None

            # CSS 셀렉터로 링크 탐색
            for selector in self._LIST_PAGE_SELECTORS:
                links = soup.select(selector)
                if not links:
                    continue
                for a in links:
                    href = a.get("href", "").strip()
                    if not href:
                        continue
                    href = urljoin(self._LIST_BASE_URL, href)
                    if href in seen:
                        continue
                    if not re.search(r"/article[s]?/|\d{8,}", href):
                        continue
                    seen.add(href)
                    entries.append(RSSEntry(
                        url=href,
                        title=a.get_text(strip=True),
                        published_at=_extract_date_near(a),
                    ))
                if entries:
                    break   # 첫 번째 성공한 셀렉터 사용

            # 폴백: 아티클 패턴이 있는 모든 <a>
            if not entries:
                for a in soup.find_all(
                    "a", href=re.compile(r"/article[s]?/|\d{10,}")
                ):
                    href = urljoin(self._LIST_BASE_URL, a["href"])
                    if href not in seen:
                        seen.add(href)
                        entries.append(RSSEntry(
                            url=href,
                            title=a.get_text(strip=True),
                        ))

            self.log.info("list_page_fetched", page=page, entries=len(entries))
            return entries

        except Exception as exc:
            self.log.warning("list_page_error", url=url, error=str(exc))
            return []

    def _collect_feed_entries(
        self,
        start_date: Optional[datetime] = None,
        end_date:   Optional[datetime] = None,
        max_pages:  int                = 10,
    ) -> list[RSSEntry]:
        """
        RSS + 목록 페이지 페이지네이션으로 기사 후보를 수집합니다.

        1. RSS 를 먼저 취득합니다.
        2. RSS 의 가장 오래된 항목이 start_date 보다 최신이면
           (RSS 가 범위를 커버하지 못함) 목록 페이지를 추가 탐색합니다.
        3. start_date / end_date 로 날짜 필터를 적용합니다.
           published_at 이 없는 항목은 실제 스크래핑 후 확인을 위해 포함합니다.
        """
        entries: list[RSSEntry] = []
        seen:    set[str]       = set()

        # 1. RSS
        for e in self._fetch_rss():
            if e.url not in seen:
                seen.add(e.url)
                entries.append(e)

        # 2. 목록 페이지 페이지네이션 필요 여부 판단
        _start = _ensure_tz(start_date) if start_date else None
        rss_oldest = min(
            (_ensure_tz(e.published_at) for e in entries if e.published_at),
            default=None,
        )
        need_list = _start is not None and (
            not entries                            # RSS 없음
            or (rss_oldest is not None and rss_oldest > _start)  # RSS 범위 부족
        )

        if need_list:
            for page in range(1, max_pages + 1):
                page_entries = self._fetch_list_page(page)
                if not page_entries:
                    break
                added = False
                for e in page_entries:
                    if e.url not in seen:
                        seen.add(e.url)
                        entries.append(e)
                        added = True
                # 날짜가 있는 항목이 start_date 보다 오래됐으면 중단
                dated = [e for e in page_entries if e.published_at]
                if dated:
                    page_oldest = min(_ensure_tz(e.published_at) for e in dated)
                    if page_oldest < _start:
                        break
                if not added:
                    break

        # 3. 날짜 필터
        _end = _ensure_tz(end_date) if end_date else None
        if _start or _end:
            filtered: list[RSSEntry] = []
            for e in entries:
                if e.published_at:
                    pa = _ensure_tz(e.published_at)
                    if _start and pa < _start:
                        continue
                    if _end and pa > _end:
                        continue
                # published_at 없는 항목은 포함 (scrape_batch 에서 이중 확인)
                filtered.append(e)
            entries = filtered

        self.log.info(
            "feed_entries_collected",
            total=len(entries),
            start=_start.isoformat() if _start else None,
            end=_end.isoformat()   if _end   else None,
        )
        return entries

    # ── 공개 고수준 API ───────────────────────────────────────

    def check_latest(
        self,
        language:   str  = "kr",
        auto_queue: bool = True,
    ) -> CheckResult:
        """
        DB 의 최신 published_at 이후 기사를 RSS 에서 감지하고 자동으로 큐에 추가합니다.

        동작:
            1. DB 의 MAX(published_at) 를 기준선으로 조회
            2. RSS 피드 취득 (실패 시 목록 첫 페이지 폴백)
            3. published_at > 기준선인 항목 필터링
            4. 상태 기반 중복 체크 — PROCESSED / SCRAPED 스킵
            5. auto_queue=True 면 job_queue 에 일괄 등록 (priority=7)

        Returns:
            CheckResult(new_count, queued_urls, job_id, latest_db, latest_feed)
        """
        latest_db = get_latest_published_at()
        self.log.info(
            "check_latest_start",
            latest_db=latest_db.isoformat() if latest_db else None,
        )

        # RSS 취득 → 실패 시 목록 첫 페이지 폴백
        feed = self._fetch_rss() or self._fetch_list_page(page=1)

        # 기준선 이후 기사 선별
        if latest_db:
            threshold = _ensure_tz(latest_db)
            new_entries = [
                e for e in feed
                if e.published_at and _ensure_tz(e.published_at) > threshold
            ]
            # published_at 없는 항목도 신규 후보로 포함 (DB에 없는 URL 이면 수집)
            new_entries += [e for e in feed if not e.published_at]
        else:
            new_entries = list(feed)   # DB 가 비어있으면 전체 수집

        # 상태 기반 중복 체크
        candidate_urls = [e.url for e in new_entries]
        to_scrape, skipped = self._classify_urls(
            candidate_urls, skip_processed=True, retry_error=True
        )

        latest_feed: Optional[datetime] = max(
            (e.published_at for e in feed if e.published_at),
            default=None,
        )

        if not to_scrape:
            self.log.info(
                "check_latest_nothing_new",
                feed_count=len(feed),
                already_skipped=len(skipped),
            )
            return CheckResult(
                new_count=0,
                latest_db=latest_db,
                latest_feed=latest_feed,
            )

        self.log.info(
            "check_latest_new_found",
            new_count=len(to_scrape),
            skipped=len(skipped),
            latest_feed=latest_feed.isoformat() if latest_feed else None,
        )

        queued_job_id: Optional[int] = None
        if auto_queue:
            queued_job_id = create_job(
                "scrape",
                {
                    "urls":       to_scrape,
                    "language":   language,
                    "batch_size": len(to_scrape),
                },
                priority=7,     # 최신 기사는 높은 우선순위
            )
            self.log.info(
                "check_latest_queued",
                job_id=queued_job_id,
                url_count=len(to_scrape),
            )

        return CheckResult(
            new_count=len(to_scrape),
            queued_urls=to_scrape,
            job_id=queued_job_id,
            latest_db=latest_db,
            latest_feed=latest_feed,
        )

    def scrape_range(
        self,
        start_date:     datetime,
        end_date:       datetime,
        job_id:         Optional[int] = None,
        language:       str           = "kr",
        max_pages:      int           = 10,
        skip_processed: bool          = True,
        dry_run:        bool          = False,
    ) -> BatchResult:
        """
        특정 날짜 범위 [start_date, end_date] 의 기사만 수집합니다.

        동작:
            1. RSS + 목록 페이지 페이지네이션으로 후보 URL 을 수집
            2. 상태 기반 중복 체크
            3. batch_size 단위로 scrape_batch() 를 반복 호출
               scrape_batch 내부에서 parsed published_at 으로 날짜를 이중 확인

        Args:
            start_date:     수집 시작 날짜 (포함). naive 면 UTC 로 처리.
            end_date:       수집 종료 날짜 (포함). naive 면 UTC 로 처리.
            job_id:         연결된 job_queue.id
            language:       기사 언어 코드
            max_pages:      RSS 범위 부족 시 목록 페이지 최대 탐색 수
            skip_processed: PROCESSED 기사 스킵 여부 (기본 True)

        Returns:
            BatchResult — 전체 구간의 누적 결과
        """
        _start = _ensure_tz(start_date)
        _end   = _ensure_tz(end_date)

        self.log.info(
            "scrape_range_start",
            start=_start.isoformat(),
            end=_end.isoformat(),
        )

        # 1. 후보 URL 수집 (RSS + 필요 시 목록 페이지)
        feed_entries = self._collect_feed_entries(
            start_date=_start,
            end_date=_end,
            max_pages=max_pages,
        )
        if not feed_entries:
            self.log.warning(
                "scrape_range_no_candidates",
                start=_start.isoformat(),
                end=_end.isoformat(),
            )
            return BatchResult(total=0)

        all_urls = [e.url for e in feed_entries]
        self.log.info("scrape_range_candidates", count=len(all_urls))

        # 2. batch_size 단위로 반복 처리 (누적 결과 병합)
        combined  = BatchResult(total=len(all_urls))
        remaining = list(all_urls)

        while remaining:
            chunk      = remaining[:self.batch_size]
            remaining  = remaining[self.batch_size:]

            partial = self.scrape_batch(
                urls=chunk,
                job_id=job_id,
                language=language,
                skip_processed=skip_processed,
                retry_error=True,
                date_after=_start,
                date_before=_end,
                dry_run=dry_run,
            )
            combined.success.extend(partial.success)
            combined.failed.extend(partial.failed)
            combined.skipped.extend(partial.skipped)

            # 403 차단이면 전체 중단
            if any(f.get("fatal") for f in partial.failed):
                self.log.error("scrape_range_abort_forbidden")
                break

        self.log.info(
            "scrape_range_done",
            success=len(combined.success),
            failed=len(combined.failed),
            skipped=len(combined.skipped),
        )
        return combined


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def main() -> None:
    """
    CLI 진입점.

    사용 예:
        # 날짜 범위 수집
        python -m scraper.engine scrape-range --start 2026-02-01 --end 2026-02-25
        python -m scraper.engine scrape-range --start 2026-02-01 --end 2026-02-25 \\
            --batch-size 20 --max-pages 5 --force

        # 최신 기사 감지 및 큐 등록
        python -m scraper.engine check-latest
        python -m scraper.engine check-latest --no-queue   # 감지만, 큐 등록 안 함
        python -m scraper.engine check-latest --language en
    """
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="python -m scraper.engine",
        description="TenAsia 스크래퍼 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── scrape-range ──────────────────────────────────────────
    rng = sub.add_parser(
        "scrape-range",
        help="특정 날짜 범위의 기사를 수집합니다.",
    )
    rng.add_argument(
        "--start", required=True,
        help="수집 시작 날짜 (YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM:SS, UTC 기준)",
    )
    rng.add_argument(
        "--end", required=True,
        help="수집 종료 날짜 (YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM:SS, UTC 기준)",
    )
    rng.add_argument("--batch-size", type=int, default=10,
                     help="한 번에 처리할 최대 URL 수 (기본 10)")
    rng.add_argument("--max-pages", type=int, default=10,
                     help="RSS 범위 부족 시 목록 페이지 최대 탐색 수 (기본 10)")
    rng.add_argument("--language", default="kr",
                     help="기사 언어 코드 (기본 kr)")
    rng.add_argument("--job-id", type=int, default=None,
                     help="연결된 job_queue.id")
    rng.add_argument("--force", action="store_true",
                     help="PROCESSED 기사도 재수집 (skip_processed=False)")
    rng.add_argument("--dry-run", action="store_true",
                     help="HTTP 요청·파싱은 수행하되 DB 에 저장하지 않음 (테스트 모드)")

    # ── check-latest ──────────────────────────────────────────
    chk = sub.add_parser(
        "check-latest",
        help="DB 보다 새로운 기사를 감지하고 job_queue 에 등록합니다.",
    )
    chk.add_argument("--no-queue", action="store_true",
                     help="큐 등록 없이 감지만 합니다.")
    chk.add_argument("--language", default="kr",
                     help="기사 언어 코드 (기본 kr)")

    args = parser.parse_args()

    scraper = TenAsiaScraper()

    if args.command == "scrape-range":
        scraper.batch_size = args.batch_size
        start = _cli_parse_date(args.start)
        end   = _cli_parse_date(args.end, end_of_day=True)
        result = scraper.scrape_range(
            start_date=start,
            end_date=end,
            job_id=args.job_id,
            language=args.language,
            max_pages=args.max_pages,
            skip_processed=not args.force,
            dry_run=args.dry_run,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))

    elif args.command == "check-latest":
        check_result = scraper.check_latest(
            language=args.language,
            auto_queue=not args.no_queue,
        )
        print(json.dumps(check_result.to_dict(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
