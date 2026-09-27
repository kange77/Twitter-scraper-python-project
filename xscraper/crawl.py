"""Run a crawl job: lease items from the frontier, fetch them, record outcomes.

The crawler keeps up to ``concurrency`` fetches in flight and writes results
back to the job in batches (every ``flush_size`` outcomes or
``flush_interval`` seconds), so a crash loses at most one batch, and those
items are simply fetched again once their lease is released. Several
crawlers, in one process or many, can work on the same job file.

Fetching is injected (``fetch_tweet`` / ``fetch_user`` coroutines), so the
same loop drives the aiohttp client, the thread-pool client through
``asyncio.to_thread``, or a fake in tests.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from .http import HttpError, NotFound
from .jobs import TWEET, Item, JobStore, Outcome, new_worker_id
from .models import Tweet
from .parse import ParseError

log = logging.getLogger(__name__)

FetchTweet = Callable[[str], Awaitable[Optional[Tweet]]]
FetchUser = Callable[[str], Awaitable[list[Tweet]]]

TOTALS = ("done", "missing", "retry", "failed", "stored", "queued")


class Crawler:
    def __init__(self, store: JobStore, fetch_tweet: FetchTweet, fetch_user: FetchUser, *,
                 concurrency: int = 64, lease: float = 300.0, flush_size: int = 500,
                 flush_interval: float = 1.0, heartbeat: float = 5.0, stale_after: float = 60.0,
                 owner: Optional[str] = None, limit=None, observer=None,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.fetch_tweet = fetch_tweet
        self.fetch_user = fetch_user
        self.concurrency = max(1, concurrency)
        self.lease = lease
        self.flush_size = flush_size
        self.flush_interval = flush_interval
        self.heartbeat = heartbeat
        self.stale_after = stale_after
        self.owner = owner or new_worker_id()
        # Optional adaptive limit: anything with a ``limit`` attribute <= concurrency.
        self.limit = limit
        # Optional observer with ``outcomes(list[Outcome], counts)`` and ``snapshot()``.
        self.observer = observer
        self._clock = clock
        self.totals = dict.fromkeys(TOTALS, 0)
        self._stopping = False

    def stop(self) -> None:
        """Finish what's in flight, hand back the rest, and return from ``run``."""
        self._stopping = True

    def _cap(self) -> int:
        if self.limit is None:
            return self.concurrency
        return max(1, min(self.concurrency, int(self.limit.limit)))

    async def _work(self, item: Item) -> Outcome:
        try:
            if item.kind == TWEET:
                tweet = await self.fetch_tweet(item.key)
                return Outcome(item, [tweet] if tweet else [], missing=tweet is None)
            return Outcome(item, list(await self.fetch_user(item.key)))
        except NotFound:
            return Outcome(item, missing=True)
        except (HttpError, ParseError, ValueError) as exc:
            return Outcome(item, error=f"{type(exc).__name__}: {exc}")

    def _flush(self, buffer: list[Outcome]) -> None:
        if not buffer:
            return
        counts = self.store.complete(buffer)
        for k, v in counts.items():
            self.totals[k] += v
        if self.observer is not None:
            self.observer.outcomes(buffer, counts)
        buffer.clear()

    def _beat(self) -> None:
        self.store.renew(self.owner, self.lease)
        stats = self.observer.snapshot() if self.observer is not None else None
        self.store.heartbeat(self.owner, stats)

    async def run(self, max_items: Optional[int] = None) -> dict:
        """Crawl until the frontier is empty (or ``max_items`` outcomes); returns totals."""
        store = self.store
        store.register(self.owner)
        reaped = store.reap(self.stale_after)
        if reaped:
            log.info("requeued %d items held by crawlers that stopped", reaped)
        ready: deque[Item] = deque()
        tasks: dict[asyncio.Task, Item] = {}
        buffer: list[Outcome] = []
        finished = 0
        last_flush = last_beat = self._clock()
        try:
            while True:
                budget = None if max_items is None else max_items - finished - len(tasks) - len(ready)
                cap = self._cap()
                if not self._stopping and len(ready) + len(tasks) < cap and (budget is None or budget > 0):
                    want = 2 * cap - len(ready) - len(tasks)
                    ready.extend(store.claim(self.owner, want if budget is None else min(want, budget),
                                             self.lease))
                while ready and len(tasks) < cap and not self._stopping:
                    item = ready.popleft()
                    tasks[asyncio.ensure_future(self._work(item))] = item
                if not tasks:
                    self._flush(buffer)
                    last_flush = self._clock()
                    if self._stopping or (max_items is not None and finished >= max_items):
                        break
                    wait = store.next_ready_in()
                    if wait is None:
                        break  # nothing queued; anything leased belongs to a live worker
                    await asyncio.sleep(min(max(wait, 0.05), self.heartbeat))
                else:
                    done, _ = await asyncio.wait(tasks, timeout=self.flush_interval,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        tasks.pop(task)
                        buffer.append(task.result())
                        finished += 1
                now = self._clock()
                if len(buffer) >= self.flush_size or now - last_flush >= self.flush_interval:
                    self._flush(buffer)
                    last_flush = now
                if now - last_beat >= self.heartbeat:
                    self._beat()
                    last_beat = now
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for task, item in tasks.items():
                if not task.cancelled() and task.exception() is None:
                    buffer.append(task.result())
            try:
                self._flush(buffer)
            finally:
                store.unregister(self.owner)  # hands back anything still leased
        return dict(self.totals)
