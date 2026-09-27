"""Throughput benchmark for xscraper's fetch, parse and export paths.

Starts ``mock_x.py`` locally for each scenario, so no traffic reaches X:

    python benchmarks/bench_fetch.py            # full matrix
    python benchmarks/bench_fetch.py --quick    # smaller batches

Runs against whichever ``xscraper`` is importable, so pointing PYTHONPATH at
an older checkout gives before/after numbers from the same script.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from xscraper import scraper as sc
from xscraper.http import HttpClient
from xscraper.models import Tweet, User
from xscraper.storage import export

try:
    from xscraper.aio import AsyncHttpClient, AsyncScraper
except ImportError:  # older checkout, or aiohttp missing
    AsyncScraper = None

MOCK = Path(__file__).with_name("mock_x.py")
UNLIMITED = 1e9


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Mock:
    def __init__(self, *args: str):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, str(MOCK), "--port", str(self.port), *args])
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)
        sc.TWEET_ENDPOINT = f"http://127.0.0.1:{self.port}/tweet-result"

    def stats(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/__stats") as r:
            return json.load(r)

    def __enter__(self) -> "Mock":
        return self

    def __exit__(self, *exc) -> None:
        self.proc.terminate()
        self.proc.wait()


def ids(n: int) -> list[str]:
    return [str(1800000000000000000 + i) for i in range(n)]


def sync_client(workers: int, rate: float, retries: int = 5) -> HttpClient:
    kw = dict(rate=rate, burst=workers, retries=retries, backoff=0.5)
    try:
        return HttpClient(pool_size=workers, **kw)
    except TypeError:  # before pool_size existed
        return HttpClient(**kw)


def run_sync(n: int, workers: int, rate: float = UNLIMITED) -> tuple[float, int]:
    s = sc.Scraper(sync_client(workers, rate), workers=workers)
    start = time.perf_counter()
    got = s.tweets(ids(n), return_exceptions=True)
    return time.perf_counter() - start, sum(isinstance(t, Tweet) for t in got)


def run_async(n: int, concurrency: int, rate: float = UNLIMITED) -> tuple[float, int]:
    async def go():
        async with AsyncHttpClient(rate=rate, burst=concurrency, concurrency=concurrency,
                                   backoff=0.5) as client:
            start = time.perf_counter()
            got = await AsyncScraper(client).tweets(ids(n), return_exceptions=True)
            return time.perf_counter() - start, sum(isinstance(t, Tweet) for t in got)
    return asyncio.run(go())


def row(label: str, n: int, elapsed: float, ok: int, extra: str = "") -> None:
    print(f"  {label:<28} {n / elapsed:>9,.0f} tweets/s  {elapsed:6.2f}s  ok {ok}/{n}  {extra}", flush=True)


def fetch_scenarios(quick: bool) -> None:
    n = 2000 if quick else 5000
    print(f"\nFetch, no added latency ({n} tweets)")
    with Mock():
        for w in (4, 16, 64):
            row(f"sync  workers={w}", n, *run_sync(n, w))
        if AsyncScraper:
            for c in (64, 256):
                row(f"async concurrency={c}", n, *run_async(n, c))

    n = 1000 if quick else 3000
    print(f"\nFetch, 50 ms simulated network latency ({n} tweets)")
    with Mock("--latency", "0.05"):
        for w in (4, 16, 64):
            row(f"sync  workers={w}", n, *run_sync(n, w))
        if AsyncScraper:
            for c in (64, 256):
                row(f"async concurrency={c}", n, *run_async(n, c))


def rate_limit_scenario(quick: bool) -> None:
    n, limit, window = (600, 200, 2) if quick else (1500, 300, 3)
    print(f"\nServer rate limit: {limit} requests per {window}s window, 429s otherwise ({n} tweets)")
    runs = [("sync  workers=32", lambda: run_sync(n, 32))]
    if AsyncScraper:
        runs.append(("async concurrency=64", lambda: run_async(n, 64)))
    for label, fn in runs:
        with Mock("--limit", str(limit), "--window", str(window)) as m:
            elapsed, ok = fn()
            stats = m.stats()
            row(label, n, elapsed, ok, f"429s received: {stats['limited']}")


def export_scenario(quick: bool) -> None:
    n = 20000 if quick else 50000
    user = User(id="1", screen_name="NASA", name="NASA")
    tweets = [Tweet(id=str(i), text=f"tweet number {i} #Artemis", created_at="2024-06-10T14:00:00Z",
                    user=user, hashtags=["Artemis"], like_count=i) for i in range(n)]
    print(f"\nExport ({n} tweets)")
    with tempfile.TemporaryDirectory() as d:
        for ext in ("jsonl", "csv", "db"):
            start = time.perf_counter()
            export(tweets, f"{d}/out.{ext}")
            first = time.perf_counter() - start
            extra = ""
            if ext == "db":  # a second scrape into the same store: all updates
                start = time.perf_counter()
                export(tweets, f"{d}/out.{ext}")
                extra = f"re-upsert {n / (time.perf_counter() - start):,.0f} tweets/s"
            row(ext, n, first, n, extra)


def main() -> None:
    p = argparse.ArgumentParser(description="xscraper throughput benchmark")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--only", choices=("fetch", "ratelimit", "export"))
    args = p.parse_args()
    logging.disable(logging.WARNING)  # retry logs would drown the table
    import xscraper
    print(f"xscraper from {Path(xscraper.__file__).parent}; async engine: {'yes' if AsyncScraper else 'no'}")
    if args.only in (None, "fetch"):
        fetch_scenarios(args.quick)
    if args.only in (None, "ratelimit"):
        rate_limit_scenario(args.quick)
    if args.only in (None, "export"):
        export_scenario(args.quick)


if __name__ == "__main__":
    main()
