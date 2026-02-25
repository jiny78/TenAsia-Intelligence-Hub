"""
scraper/throttle.py — HTTP 요청 스로틀링

도메인별 요청 속도를 제한하여 웹사이트 차단을 방지합니다.

지원 방식:
  1. 도메인별 최소 간격 (min_interval) — 기본 1.0초
  2. 도메인별 최대 RPM 제한 (max_rpm)
  3. 429 / 503 응답 시 Exponential Backoff 자동 재시도

사용법:
    throttle = DomainThrottle()

    # 요청 전 대기 (자동)
    with throttle.acquire("https://tenasia.hankyung.com/article/123"):
        resp = requests.get(url)

    # 세션에 자동 통합 (requests.Session)
    session = throttle.make_session()
    resp = session.get("https://tenasia.hankyung.com/article/123")
"""

from __future__ import annotations

import time
import threading
import structlog
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# 도메인별 기본 설정
# ─────────────────────────────────────────────────────────────

# 도메인 → (최소 간격 초, 최대 RPM)
_DOMAIN_RULES: dict[str, tuple[float, int]] = {
    "tenasia.hankyung.com": (1.0,  30),
    "naver.com":            (0.5,  60),
    "entertain.naver.com":  (0.5,  60),
    "news.naver.com":       (0.5,  60),
    "daum.net":             (0.8,  40),
    "youtube.com":          (1.0,  30),
    "instagram.com":        (2.0,  15),
    "twitter.com":          (2.0,  15),
    "x.com":                (2.0,  15),
    # 기본값: 키 없으면 아래 DEFAULT 사용
}

_DEFAULT_INTERVAL = 1.0   # 초
_DEFAULT_MAX_RPM  = 30

# HTTP 재시도 설정
_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
_MAX_RETRIES        = 3
_BACKOFF_FACTOR     = 2.0   # 대기: 0, 2, 4, 8, ...초


# ─────────────────────────────────────────────────────────────
# DomainThrottle
# ─────────────────────────────────────────────────────────────

class DomainThrottle:
    """
    도메인별 요청 속도 제한기.
    Thread-safe: 여러 스레드에서 동시에 사용 가능.
    """

    def __init__(
        self,
        default_interval: float = _DEFAULT_INTERVAL,
        default_max_rpm:  int   = _DEFAULT_MAX_RPM,
        rules: Optional[dict[str, tuple[float, int]]] = None,
    ) -> None:
        self._default_interval = default_interval
        self._default_max_rpm  = default_max_rpm
        self._rules            = rules or _DOMAIN_RULES

        # 도메인 → 마지막 요청 시각
        self._last_request:  dict[str, float]        = defaultdict(float)
        # 도메인 → 최근 60초 요청 타임스탬프 deque
        self._timestamps:    dict[str, deque[float]] = defaultdict(deque)
        # 도메인별 Lock
        self._locks:         dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._global_lock    = threading.Lock()

    def _get_lock(self, domain: str) -> threading.Lock:
        with self._global_lock:
            if domain not in self._locks:
                self._locks[domain] = threading.Lock()
            return self._locks[domain]

    def _get_rules(self, domain: str) -> tuple[float, int]:
        """도메인에 해당하는 (간격, RPM) 반환. 서브도메인 포함 매칭."""
        for key, rule in self._rules.items():
            if domain == key or domain.endswith(f".{key}"):
                return rule
        return self._default_interval, self._default_max_rpm

    @staticmethod
    def _extract_domain(url: str) -> str:
        return urlparse(url).netloc.lower()

    def wait(self, url: str) -> None:
        """
        주어진 URL 의 도메인에 대해 속도 제한을 적용합니다.
        필요 시 블로킹 대기합니다.
        """
        domain = self._extract_domain(url)
        lock   = self._get_lock(domain)
        min_interval, max_rpm = self._get_rules(domain)

        with lock:
            now = time.monotonic()

            # ── 간격 제한 ────────────────────────────────────
            elapsed = now - self._last_request[domain]
            if elapsed < min_interval:
                wait = min_interval - elapsed
                logger.debug(
                    "도메인 간격 대기",
                    domain=domain,
                    wait_sec=round(wait, 2),
                )
                time.sleep(wait)
                now = time.monotonic()

            # ── RPM 제한 (슬라이딩 윈도우) ───────────────────
            ts = self._timestamps[domain]
            while ts and now - ts[0] >= 60.0:
                ts.popleft()

            if len(ts) >= max_rpm:
                wait = 60.0 - (now - ts[0]) + 0.1
                logger.debug(
                    "도메인 RPM 한도 대기",
                    domain=domain,
                    rpm=max_rpm,
                    wait_sec=round(wait, 2),
                )
                time.sleep(max(wait, 0.0))
                now = time.monotonic()
                # 만료 타임스탬프 재정리
                while ts and now - ts[0] >= 60.0:
                    ts.popleft()

            ts.append(now)
            self._last_request[domain] = now

    @contextmanager
    def acquire(self, url: str):
        """
        컨텍스트 매니저로 사용하면 요청 전 자동 대기합니다.

        Usage:
            with throttle.acquire(url):
                resp = requests.get(url)
        """
        self.wait(url)
        yield

    def make_session(
        self,
        user_agent: str = "Mozilla/5.0 (compatible; TIH-Bot/1.0; +https://github.com/tih)",
        timeout: int = 15,
    ) -> "ThrottledSession":
        """
        스로틀링이 통합된 requests.Session 을 반환합니다.

        Usage:
            session = throttle.make_session()
            resp = session.get("https://tenasia.hankyung.com/article/123")
        """
        return ThrottledSession(throttle=self, user_agent=user_agent, timeout=timeout)

    def stats(self) -> dict[str, dict]:
        """현재 도메인별 요청 통계를 반환합니다."""
        now = time.monotonic()
        result = {}
        for domain, ts in self._timestamps.items():
            recent = [t for t in ts if now - t < 60.0]
            result[domain] = {
                "requests_last_60s": len(recent),
                "last_request_ago":  round(now - self._last_request[domain], 1),
            }
        return result


# ─────────────────────────────────────────────────────────────
# ThrottledSession
# ─────────────────────────────────────────────────────────────

class ThrottledSession(requests.Session):
    """
    DomainThrottle 가 통합된 requests.Session.
    get/post 호출 시 자동으로 도메인 속도 제한을 적용합니다.
    """

    def __init__(
        self,
        throttle: DomainThrottle,
        user_agent: str = "Mozilla/5.0 (compatible; TIH-Bot/1.0)",
        timeout: int = 15,
    ) -> None:
        super().__init__()
        self._throttle = throttle
        self._timeout  = timeout

        # 기본 헤더
        self.headers.update({
            "User-Agent":      user_agent,
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        })

        # 자동 재시도 어댑터 (429, 5xx)
        retry = Retry(
            total=_MAX_RETRIES,
            backoff_factor=_BACKOFF_FACTOR,
            status_forcelist=_RETRY_STATUS_CODES,
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://",  adapter)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        # 요청 전 스로틀 대기
        self._throttle.wait(url)

        # 타임아웃 기본값 적용
        kwargs.setdefault("timeout", self._timeout)

        logger.debug("HTTP 요청", method=method.upper(), url=url)
        resp = super().request(method, url, **kwargs)

        log = logger.bind(method=method.upper(), url=url, status=resp.status_code)
        if resp.status_code == 429:
            log.warning("요청 제한(429) — 다음 요청에서 자동 대기 증가")
        elif resp.status_code >= 400:
            log.warning("HTTP 오류 응답")
        else:
            log.debug("HTTP 응답 완료")

        return resp


# ─────────────────────────────────────────────────────────────
# 전역 싱글턴 (worker 에서 공유 사용)
# ─────────────────────────────────────────────────────────────

_default_throttle: Optional[DomainThrottle] = None


def get_throttle() -> DomainThrottle:
    """기본 DomainThrottle 싱글턴을 반환합니다."""
    global _default_throttle
    if _default_throttle is None:
        _default_throttle = DomainThrottle()
    return _default_throttle


def get_session(user_agent: Optional[str] = None) -> ThrottledSession:
    """
    기본 ThrottledSession 을 반환합니다.

    Usage:
        session = get_session()
        html = session.get("https://tenasia.hankyung.com/...").text
    """
    kwargs = {}
    if user_agent:
        kwargs["user_agent"] = user_agent
    return get_throttle().make_session(**kwargs)
