"""HTTP layer: token-bucket rate limiting, retries with jittered exponential
backoff that honours Retry-After / x-rate-limit-reset, a shared pause when the
server says the rate limit is used up, and proxy rotation.

The pieces that don't touch the network (``RateLimiter``, ``RateGate``,
``classify``) are shared with the asyncio client in ``xscraper.aio``."""
from __future__ import annotations

import email.utils
import itertools
import logging
import random
import threading
import time
from typing import Callable, Optional, Sequence

import requests
from requests.adapters import HTTPAdapter

try:  # optional: several times faster than the stdlib for the payloads we parse
    import orjson

    def loads(data):
        return orjson.loads(data)
except ImportError:  # pragma: no cover - exercised when orjson isn't installed
    import json

    def loads(data):
        return json.loads(data)

log = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
)
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Spread (seconds) added to rate-limit pauses so waiting workers resume staggered.
GATE_JITTER = 0.25


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

    def reserve(self) -> float:
        """Take a token now and return how long to wait before using it.

        Reservations let concurrent callers (threads or coroutines) queue up
        for evenly spaced slots instead of polling.
        """
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            self._tokens -= 1
            return 0.0 if self._tokens >= 0 else -self._tokens / self.rate

    def acquire(self) -> None:
        wait = self.reserve()
        if wait > 0:
            self._sleep(wait)


class RateGate:
    """Request budget shared by every worker on a client, driven by the server.

    X reports ``x-rate-limit-remaining`` and ``x-rate-limit-reset`` on each
    response. The gate turns them into a budget for the current window,
    discounting requests already in flight, and holds new requests until the
    reset once it runs out, instead of sending them into certain 429s. A 429
    or 503 with Retry-After / x-rate-limit-reset pauses every worker until
    then, not just the one that got it.

    Usage: ``enter()`` before sending (returns seconds to wait, or 0 and
    takes a slot), then ``leave(status, headers)`` when the request ends.
    """

    def __init__(self, clock: Callable[[], float] = time.time, max_wait: float = 900.0):
        self._clock = clock
        self.max_wait = max_wait
        self._until = 0.0
        self._reset: Optional[float] = None
        self._budget: Optional[int] = None
        self.in_flight = 0
        self._lock = threading.Lock()

    def _wait(self, now: float) -> float:
        if self._until > now:
            return self._until - now
        if self._reset is not None and now >= self._reset:
            self._reset = self._budget = None  # new window; budget unknown until a response says
        if self._budget is not None and self._budget <= 0:
            return self._reset - now
        return 0.0

    def wait_time(self) -> float:
        with self._lock:
            return self._wait(self._clock())

    def enter(self) -> float:
        with self._lock:
            wait = self._wait(self._clock())
            if wait > 0:
                return wait
            self.in_flight += 1
            if self._budget is not None:
                self._budget -= 1
            return 0.0

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, self._clock() + min(seconds, self.max_wait))

    def leave(self, status: Optional[int] = None, headers=None) -> Optional[float]:
        """Record a finished request; returns the server-requested wait, if any."""
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            if headers is None:
                return None
            now = self._clock()
            remaining = (headers.get("x-rate-limit-remaining") or "").strip()
            reset = (headers.get("x-rate-limit-reset") or "").strip()
            if remaining.isdigit() and reset.isdigit() and float(reset) > now:
                reset_at = float(reset)
                # Requests still in flight will draw on what's left.
                budget = int(remaining) - self.in_flight
                if self._reset is None or reset_at > self._reset:
                    self._reset, self._budget = reset_at, budget
                elif reset_at == self._reset:
                    self._budget = budget if self._budget is None else min(self._budget, budget)
                # reset_at < self._reset: a late answer from the previous window.
        wait = retry_after_seconds(headers, now) if status in (429, 503) else None
        if wait is not None:
            self.pause(wait)
        return wait


def retry_after_seconds(resp, now: Optional[float] = None, reset_only: bool = False) -> Optional[float]:
    """Server-requested wait from Retry-After or x-rate-limit-reset, if any.

    ``resp`` may be a response or just its (case-insensitive) headers.
    """
    headers = getattr(resp, "headers", resp)
    now = time.time() if now is None else now
    value = None if reset_only else headers.get("Retry-After")
    if value:
        if value.strip().isdigit():
            return float(value)
        try:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            pass
    reset = headers.get("x-rate-limit-reset")
    if reset and reset.strip().isdigit():
        return max(0.0, float(reset) - now)
    return None


def classify(status: int, url: str) -> Optional[str]:
    """None for success, an error string for a retryable status; raises otherwise."""
    if status < 400:
        return None
    if status == 404:
        raise NotFound(f"404 Not Found: {url}", 404)
    if status not in RETRY_STATUSES:
        raise HttpError(f"HTTP {status} for {url}", status)
    return f"HTTP {status}"


def backoff_delay(attempt: int, backoff: float, max_backoff: float) -> float:
    """"Full jitter" exponential backoff."""
    return random.uniform(0, min(max_backoff, backoff * 2 ** attempt))


def give_up(url: str, retries: int, last_error: str, status: Optional[int]) -> HttpError:
    attempts = retries + 1
    return HttpError(f"giving up on {url} after {attempts} attempt{'s' if attempts > 1 else ''} "
                     f"({last_error})", status)


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
        clock: Callable[[], float] = time.time,
        pool_size: int = 10,
        observer=None,
    ):
        if session is None:
            session = requests.Session()
            # urllib3 keeps 10 connections per host by default; with more
            # workers than that, extra connections are opened and thrown away
            # on every request instead of being reused.
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(10, pool_size))
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self.session = session
        self.session.headers.update({
            "User-Agent": user_agent or random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
        })
        if cookies:
            self.session.headers["Cookie"] = cookies
        self.limiter = RateLimiter(rate, burst, sleep=sleep)
        self.gate = RateGate(clock=clock)
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.timeout = timeout
        self._proxies = itertools.cycle(proxies) if proxies else None
        self._proxy_lock = threading.Lock()
        self._sleep = sleep
        # Optional: gets on_response(status, seconds, error), on_retry(), on_wait(kind, seconds).
        self.observer = observer

    def _next_proxy(self) -> Optional[dict]:
        if not self._proxies:
            return None
        with self._proxy_lock:
            proxy = next(self._proxies)
        return {"http": proxy, "https": proxy}

    def _enter_gate(self) -> None:
        # Jitter so paused workers don't all fire at the same instant.
        while (wait := self.gate.enter()) > 0:
            wait = min(wait, self.max_backoff) + random.uniform(0, GATE_JITTER)
            if self.observer is not None:
                self.observer.on_wait("server", wait)
            self._sleep(wait)

    def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> requests.Response:
        last_error = "no attempts made"
        obs = self.observer
        for attempt in range(self.retries + 1):
            wait = self.limiter.reserve()
            if wait > 0:
                if obs is not None:
                    obs.on_wait("rate_limit", wait)
                self._sleep(wait)
            self._enter_gate()
            resp = None
            server_wait = None
            started = time.monotonic()
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout, proxies=self._next_proxy())
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.gate.leave()
                last_error = f"{type(exc).__name__}: {exc}"
                if obs is not None:
                    obs.on_response(None, time.monotonic() - started, type(exc).__name__)
            except BaseException:
                self.gate.leave()
                raise
            else:
                if obs is not None:
                    obs.on_response(resp.status_code, time.monotonic() - started)
                server_wait = self.gate.leave(resp.status_code, resp.headers)
                error = classify(resp.status_code, url)
                if error is None:
                    return resp
                last_error = error
            if attempt == self.retries:
                break
            if obs is not None:
                obs.on_retry()
            if server_wait is not None:
                # The gate now holds every request, this one included, until the reset.
                log.warning("%s on %s; pausing requests for %.1fs (retry %d/%d)", last_error, url,
                            server_wait, attempt + 1, self.retries)
                continue
            delay = backoff_delay(attempt, self.backoff, self.max_backoff)
            log.warning("%s on %s; retry %d/%d in %.1fs", last_error, url, attempt + 1, self.retries, delay)
            self._sleep(delay)
        raise give_up(url, self.retries, last_error, resp.status_code if resp is not None else None)
