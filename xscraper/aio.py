"""asyncio client and scraper for high-throughput batches (needs ``aiohttp``).

One event loop drives hundreds of keep-alive connections, which removes the
thread-per-request overhead of ``requests``: on the benchmark mock it moves
the single-process ceiling from roughly 700 to several thousand tweets per
second. Rate limiting, retries, the shared rate-limit pause and proxy
rotation behave exactly as in :class:`xscraper.http.HttpClient`.

    import asyncio
    from xscraper.aio import AsyncHttpClient, AsyncScraper

    async def main():
        async with AsyncHttpClient(rate=50, concurrency=64) as client:
            scraper = AsyncScraper(client)
            async for tweet in scraper.iter_tweets(open("ids.txt"), return_exceptions=True):
                ...

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import codecs
import itertools
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Optional, Sequence

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy

from .http import (USER_AGENTS, HttpError, NotFound, RateGate, RateLimiter, backoff_delay, classify, gate_sleep_time,
                   give_up)
from .models import Tweet
from .parse import ParseError
from . import scraper as _sync
from .scraper import parse_screen_name, parse_tweet_id, timeline_from_page, tweet_from_body, tweet_params

log = logging.getLogger(__name__)


@dataclass
class Response:
    """The parts of a response the scraper uses, read fully before the connection is reused."""
    status_code: int
    content: bytes
    headers: CIMultiDictProxy
    encoding: str = "utf-8"
    server_wait: Optional[float] = None  # set when the server asked us to back off

    @property
    def text(self) -> str:
        # The charset comes from the server's Content-Type; a mislabelled page
        # (proxy, CDN, captive portal) must not raise LookupError here.
        try:
            codecs.lookup(self.encoding)
        except LookupError:
            log.warning("unknown charset %r in response; decoding as utf-8", self.encoding)
            return self.content.decode("utf-8", errors="replace")
        return self.content.decode(self.encoding, errors="replace")


class AsyncHttpClient:
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
        concurrency: int = 64,
        session: Optional[aiohttp.ClientSession] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        observer=None,
    ):
        self.observer = observer  # see HttpClient
        self.headers = {"User-Agent": user_agent or random.choice(USER_AGENTS),
                        "Accept-Language": "en-US,en;q=0.9"}
        if cookies:
            self.headers["Cookie"] = cookies
        self.concurrency = max(1, concurrency)
        self.limiter = RateLimiter(rate, burst)
        self.gate = RateGate(clock=clock)
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.timeout = timeout
        self._proxies = itertools.cycle(proxies) if proxies else None
        self._sleep = sleep
        self._session = session
        self._owns_session = session is None
        self._slots: Optional[asyncio.Semaphore] = None

    @property
    def logged_in(self) -> bool:
        """True when requests carry a Cookie header (see ``parse.EmptyTimelineShell``)."""
        return "Cookie" in self.headers

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=self.concurrency, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector, headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "AsyncHttpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _fetch(self, url: str, params: Optional[dict], headers: Optional[dict]) -> Response:
        # The semaphore is created lazily so it binds to the running loop.
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.concurrency)
        async with self._slots:
            # Checked after getting a slot, so requests queued behind a
            # rate-limit signal see it before they are sent.
            obs = self.observer
            waited = 0.0
            while (wait := self.gate.enter()) > 0:
                wait = gate_sleep_time(self.gate, wait, waited, self.max_backoff, url)
                if obs is not None:
                    obs.on_wait("server", wait)
                await self._sleep(wait)
                waited += wait
            proxy = next(self._proxies) if self._proxies else None
            started = time.monotonic()
            try:
                async with self.session.get(url, params=params, headers=headers, proxy=proxy) as r:
                    body = await r.read()
                    resp = Response(r.status, body, CIMultiDictProxy(CIMultiDict(r.headers)),
                                    r.charset or "utf-8")
            except BaseException as exc:
                self.gate.leave()
                if obs is not None and isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
                    obs.on_response(None, time.monotonic() - started, type(exc).__name__)
                raise
            if obs is not None:
                obs.on_response(resp.status_code, time.monotonic() - started)
            resp.server_wait = self.gate.leave(resp.status_code, resp.headers)
            return resp

    async def get(self, url: str, params: Optional[dict] = None,
                  headers: Optional[dict] = None) -> Response:
        last_error = "no attempts made"
        resp: Optional[Response] = None
        for attempt in range(self.retries + 1):
            wait = self.limiter.reserve()
            if wait > 0:
                if self.observer is not None:
                    self.observer.on_wait("rate_limit", wait)
                await self._sleep(wait)
            resp = None
            server_wait = None
            try:
                resp = await self._fetch(url, params, headers)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                server_wait = resp.server_wait
                error = classify(resp.status_code, url)
                if error is None:
                    return resp
                last_error = error
            if attempt == self.retries:
                break
            if self.observer is not None:
                self.observer.on_retry()
            if server_wait is not None:
                log.warning("%s on %s; pausing requests for %.1fs (retry %d/%d)", last_error, url,
                            server_wait, attempt + 1, self.retries)
                continue
            delay = backoff_delay(attempt, self.backoff, self.max_backoff)
            log.warning("%s on %s; retry %d/%d in %.1fs", last_error, url, attempt + 1, self.retries, delay)
            await self._sleep(delay)
        raise give_up(url, self.retries, last_error, resp.status_code if resp is not None else None)


class AsyncScraper:
    """Async counterpart of :class:`xscraper.scraper.Scraper`."""

    def __init__(self, client: Optional[AsyncHttpClient] = None, lang: str = "en"):
        self.client = client or AsyncHttpClient()
        self.lang = lang

    async def tweet(self, tweet: str | int) -> Optional[Tweet]:
        tweet_id = parse_tweet_id(tweet)
        try:
            resp = await self.client.get(_sync.TWEET_ENDPOINT, params=tweet_params(tweet_id, self.lang))
        except NotFound:
            return None
        return tweet_from_body(tweet_id, resp.content)

    async def _tweet_or_exc(self, tweet_id: str, return_exceptions: bool):
        try:
            return await self.tweet(tweet_id)
        except (HttpError, ParseError) as exc:
            if not return_exceptions:
                raise
            return exc

    async def iter_tweets(self, tweets: Iterable[str | int], return_exceptions: bool = False,
                          window: Optional[int] = None) -> AsyncIterator:
        """Yield results in input order as they arrive (None for missing tweets).

        The client's ``concurrency`` bounds requests in flight; ``window``
        (default ``4 * concurrency``) bounds how far ahead of the slowest
        outstanding tweet fetching may run, so memory stays flat and
        ``tweets`` may be a lazy iterable such as an open file.
        """
        window = max(1, window or 4 * self.client.concurrency)
        pending: deque = deque()
        try:
            for t in tweets:
                pending.append(asyncio.ensure_future(
                    self._tweet_or_exc(parse_tweet_id(t), return_exceptions)))
                if len(pending) >= window:
                    yield await pending.popleft()
            while pending:
                yield await pending.popleft()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def tweets(self, tweets: Iterable[str | int], return_exceptions: bool = False) -> list:
        ids = [parse_tweet_id(t) for t in tweets]
        return [r async for r in self.iter_tweets(ids, return_exceptions)]

    async def thread(self, tweet: str | int, max_depth: int = 50) -> list[Tweet]:
        """The reply chain leading up to (and including) a tweet, oldest first."""
        chain: list[Tweet] = []
        seen: set[str] = set()
        next_id: Optional[str] = parse_tweet_id(tweet)
        while next_id and next_id not in seen and len(chain) < max_depth:
            seen.add(next_id)
            try:
                current = await self.tweet(next_id)
            except (HttpError, ParseError) as exc:
                if not chain:
                    raise
                log.warning("stopping thread at %s: %s", next_id, exc)
                break
            if current is None:
                break
            chain.append(current)
            next_id = current.in_reply_to_id
        return list(reversed(chain))

    async def user_timeline(self, screen_name: str, include_retweets: bool = True) -> list[Tweet]:
        name = parse_screen_name(screen_name)
        resp = await self.client.get(_sync.TIMELINE_ENDPOINT.format(name), params={"showReplies": "true"})
        return timeline_from_page(resp.text, include_retweets, getattr(self.client, "logged_in", False))

    async def user_timelines(self, screen_names: Iterable[str], include_retweets: bool = True) -> list:
        """Each profile's tweets (or the exception it raised), in input order."""
        async def fetch(name: str):
            try:
                return await self.user_timeline(name, include_retweets)
            except (HttpError, ParseError, ValueError) as exc:
                return exc

        return list(await asyncio.gather(*(fetch(n) for n in screen_names)))
