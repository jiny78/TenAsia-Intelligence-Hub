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
    result  = scraper.scrape_batch(
        urls=["https://tenasia.hankyung.com/article/123", ...],
        job_id=42,
        language="kr",
    )
    print(result)
    # {"success": [...], "failed": [...], "total": 10, "processed": 8}
"""

from __future__ import annotations

import abc
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
import structlog
from bs4 import BeautifulSoup, Tag

from scraper.db import upsert_article
from scraper.throttle import get_session


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
class BatchResult:
    """scrape_batch() 반환 타입."""

    total:     int
    success:   list[dict[str, Any]] = field(default_factory=list)
    failed:    list[dict[str, Any]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.success) + len(self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total":     self.total,
            "processed": self.processed,
            "success":   self.success,
            "failed":    self.failed,
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
    ) -> BatchResult:
        """
        URL 목록을 배치로 스크래핑합니다.

        - 최대 batch_size 개의 URL 만 처리합니다.
        - 성공 시마다 DB 에 즉시 SCRAPED 상태로 커밋합니다.
        - ForbiddenError(403) 발생 시 배치 전체를 즉시 중단합니다.
        - 개별 URL 오류는 failed 에 기록하고 배치를 계속 진행합니다.

        Args:
            urls:            스크래핑할 URL 목록
            job_id:          연결된 job_queue.id (없으면 None)
            language:        기사 언어 코드 ('kr' / 'en' / 'jp')
            global_priority: 글로벌 아티스트 여부 (True → 영어 추출 활성화)

        Returns:
            BatchResult(total, success, failed)
        """
        batch = urls[:self.batch_size]
        result = BatchResult(total=len(urls))

        self.log.info(
            "batch_start",
            total_urls=len(urls),
            batch_size=len(batch),
            job_id=job_id,
        )

        for idx, url in enumerate(batch, start=1):
            self.log.info(
                "batch_item",
                current=idx,
                total=len(batch),
                url=url,
            )

            try:
                # 1. HTTP 요청 (Throttle + Human Delay + Backoff 포함)
                resp = self._fetch(url)

                # 2. HTML 파싱 — raw soup 을 _parse_article 에 전달
                #    (서브클래스가 og:image 수집 후 _clean_soup 를 직접 호출)
                soup = BeautifulSoup(resp.text, "html.parser")
                data = self._parse_article(url, soup)

                # 3. 공통 필드 병합
                data.setdefault("language",        language)
                data.setdefault("global_priority", global_priority)
                data["process_status"] = "SCRAPED"

                # 4. DB 즉시 커밋 (UPSERT)
                article_id = upsert_article(url, data, job_id=job_id)

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
                # 403: 배치 전체 즉시 중단 — IP 차단은 계속 시도해도 의미 없음
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
            "title_ko":     title_ko,
            "content_ko":   content_ko,
            "author":       author,
            "published_at": published_at,
            "thumbnail_url": thumbnail_url,
        }
