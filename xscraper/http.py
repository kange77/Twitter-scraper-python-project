"""HTTP layer: token-bucket rate limiting, retries with jittered exponential
backoff that honours Retry-After / x-rate-limit-reset, and proxy rotation."""
from __future__ import annotations

import email.utils
import itertools
import logging
import random
import threading
import time
from typing import Callable, Optional, Sequence

import requests

log = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
)
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NotFound(HttpError):
    pass


class RateLimiter:
    """Thread-safe token bucket: ``rate`` requests/second, bursts of ``burst``."""

    def __init__(self, rate: float, burst: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            self._sleep(wait)


def retry_after_seconds(resp: requests.Response, now: Optional[float] = None) -> Optional[float]:
    """Server-requested wait from Retry-After or x-rate-limit-reset, if any."""
    now = time.time() if now is None else now
    value = resp.headers.get("Retry-After")
    if value:
        if value.strip().isdigit():
            return float(value)
        try:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            pass
    reset = resp.headers.get("x-rate-limit-reset")
    if reset and reset.strip().isdigit():
        return max(0.0, float(reset) - now)
    return None


class HttpClient:
    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 3,
        retries: int = 5,
        backoff: float = 1.0,
        max_backoff: float = 120.0,
        timeout: float = 20.0,
        proxies: Sequence[str] = (),
        cookies: Optional[str] = None,
        user_agent: Optional[str] = None,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
        })
        if cookies:
            self.session.headers["Cookie"] = cookies
        self.limiter = RateLimiter(rate, burst, sleep=sleep)
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.timeout = timeout
        self._proxies = itertools.cycle(proxies) if proxies else None
        self._proxy_lock = threading.Lock()
        self._sleep = sleep

    def _next_proxy(self) -> Optional[dict]:
        if not self._proxies:
            return None
        with self._proxy_lock:
            proxy = next(self._proxies)
        return {"http": proxy, "https": proxy}

    def _delay(self, attempt: int, resp: Optional[requests.Response]) -> float:
        server = retry_after_seconds(resp) if resp is not None else None
        if server is not None:
            return min(server + random.uniform(0, 1), self.max_backoff)
        # "Full jitter" exponential backoff.
        return random.uniform(0, min(self.max_backoff, self.backoff * 2 ** attempt))

    def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> requests.Response:
        last_error = "no attempts made"
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            resp = None
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout, proxies=self._next_proxy())
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 404:
                    raise NotFound(f"404 Not Found: {url}", 404)
                if resp.status_code not in RETRY_STATUSES:
                    raise HttpError(f"HTTP {resp.status_code} for {url}", resp.status_code)
                last_error = f"HTTP {resp.status_code}"
            if attempt == self.retries:
                break
            delay = self._delay(attempt, resp)
            log.warning("%s on %s; retry %d/%d in %.1fs", last_error, url, attempt + 1, self.retries, delay)
            self._sleep(delay)
        attempts = self.retries + 1
        raise HttpError(f"giving up on {url} after {attempts} attempt{'s' if attempts > 1 else ''} "
                        f"({last_error})",
                        resp.status_code if resp is not None else None)
