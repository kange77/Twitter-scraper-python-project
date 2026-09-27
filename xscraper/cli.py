"""Command-line interface: ``xscraper {tweet,thread,user,analyze,bench}``."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import random
import sys
import time
from collections import Counter
from typing import Optional, Sequence
from urllib.parse import urlparse

from . import __version__
from .analysis import ENGINES, Analyzer, near_duplicate_groups
from .http import HttpClient, HttpError
from .models import Tweet
from .parse import ParseError
from .crawl import Crawler
from .metrics import AdaptiveLimit, Metrics
from .shared import SharedRateGate, SharedRateLimiter
from .jobs import TWEET, USER, Item, JobStore, parse_follow
from . import scraper as _scraper_mod
from .scraper import Scraper, parse_screen_name, parse_tweet_id
from .storage import FORMATS, TweetWriter, export, load

# Tweets are analysed and written in chunks of this size while a batch streams in.
CHUNK = 500


def _have_aiohttp() -> bool:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _add_network_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("network")
    g.add_argument("--rate", type=float, default=1.0, help="max requests per second (default 1)")
    g.add_argument("--retries", type=int, default=5, help="retries per request (default 5)")
    g.add_argument("--timeout", type=float, default=20.0, help="request timeout in seconds")
    g.add_argument("--workers", type=int, default=4,
                   help="requests in flight at once for batches (default 4; the --rate cap still applies)")
    g.add_argument("--http", choices=("auto", "async", "sync"), default="auto",
                   help="HTTP engine: async needs aiohttp (pip install xscraper[fast]); "
                        "auto uses it when installed")
    g.add_argument("--proxy", action="append", default=[], metavar="URL",
                   help="proxy URL; repeat to rotate through several")
    g.add_argument("--cookies", default=os.environ.get("XSCRAPER_COOKIES"),
                   help="Cookie header to send (default: $XSCRAPER_COOKIES)")
    g.add_argument("--lang", default="en", help="language for the embed endpoint")


def _add_output_args(p: argparse.ArgumentParser, analyze_flag: bool = True) -> None:
    g = p.add_argument_group("output")
    g.add_argument("-o", "--output", help="write to a .json/.jsonl/.csv/.db file instead of printing")
    g.add_argument("--format", choices=FORMATS, help="override the format inferred from --output")
    if analyze_flag:
        g.add_argument("--analyze", action="store_true",
                       help="add entities, sentiment and SimHash to each tweet")
    g.add_argument("--engine", choices=ENGINES, default="auto", help="analysis engine (default auto)")
    g.add_argument("--dedupe", type=int, nargs="?", const=3, metavar="BITS",
                   help="drop near-duplicate tweets (SimHash distance <= BITS, default 3)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xscraper",
        description="Scrape public tweets from X and analyse them with a WebAssembly core.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tweet", help="fetch tweets by ID or URL")
    p.add_argument("tweets", nargs="*", metavar="ID_OR_URL")
    p.add_argument("-i", "--input", dest="input_file", metavar="FILE",
                   help="read IDs/URLs from FILE, one per line ('-' for stdin; # starts a comment)")
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("thread", help="fetch the reply chain leading to a tweet")
    p.add_argument("tweet", metavar="ID_OR_URL")
    p.add_argument("--max-depth", type=int, default=50)
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("user", help="fetch recent tweets from one or more profiles")
    p.add_argument("users", nargs="*", metavar="SCREEN_NAME")
    p.add_argument("-i", "--input", dest="input_file", metavar="FILE",
                   help="read screen names from FILE, one per line ('-' for stdin)")
    p.add_argument("--no-retweets", action="store_true")
    p.add_argument("--limit", type=int, help="keep at most N tweets per user")
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("crawl", help="run a resumable crawl job stored in a SQLite file",
                       description="Fetch seeds and, optionally, the tweets they reply to, quote or "
                                   "retweet. Progress lives in JOB, so an interrupted crawl resumes "
                                   "where it stopped, and several crawl processes can share one JOB.")
    p.add_argument("job", metavar="JOB.db", help="job file (created if missing)")
    p.add_argument("seeds", nargs="*", metavar="SEED",
                   help="tweet ID/URL, or @name / profile URL for a profile's timeline")
    p.add_argument("-i", "--input", dest="input_file", metavar="FILE",
                   help="read seeds from FILE, one per line ('-' for stdin)")
    p.add_argument("--follow", help="links to follow: parents,quotes,retweets | all | none "
                                    "(default: none, or the job's stored setting)")
    p.add_argument("--depth", type=int, help="how many links away from a seed to go (default 0, or stored)")
    p.add_argument("--max-attempts", type=int,
                   help="attempts per item before it is parked as failed (default 3, or stored)")
    p.add_argument("--max-items", type=int, help="stop after this many items (the rest stay queued)")
    p.add_argument("--no-run", action="store_true", help="only add the seeds and settings")
    g = p.add_argument_group("monitoring")
    g.add_argument("--progress", type=float, default=10.0, metavar="SECONDS",
                   help="print a progress line this often (default 10; 0 to disable)")
    g.add_argument("--stats-file", metavar="FILE", help="write run statistics as JSON to FILE (kept current)")
    g.add_argument("--metrics-port", type=int, metavar="PORT",
                   help="serve Prometheus /metrics, JSON /stats and /healthz on 127.0.0.1:PORT")
    g.add_argument("--no-adaptive", action="store_true",
                   help="keep --workers requests in flight even when the server signals overload")
    p.add_argument("--processes", type=int, default=1, metavar="N",
                   help="crawler processes to run on this job (default 1). They share the job's "
                        "rate budget, so this adds CPU for parsing and storing, not requests/s")
    _add_network_args(p)
    # The job remembers its rate; --rate changes it for every process on the job.
    p.set_defaults(rate=None)

    p = sub.add_parser("job", help="inspect or manage a crawl job")
    p.add_argument("action", choices=("status", "retry", "export"),
                   help="status: progress and workers; retry: requeue failed items; "
                        "export: write the job's tweets to -o")
    p.add_argument("job", metavar="JOB.db")
    p.add_argument("-o", "--output", help="export destination (.json/.jsonl/.csv/.db)")
    p.add_argument("--format", choices=FORMATS)

    p = sub.add_parser("analyze", help="analyse tweets saved as .json/.jsonl/.db")
    p.add_argument("input")
    p.add_argument("--top", type=int, default=10, help="entries per leaderboard")
    _add_output_args(p, analyze_flag=False)

    p = sub.add_parser("bench", help="benchmark the WASM engine against pure Python")
    p.add_argument("-n", type=int, default=20000, help="number of synthetic tweets")
    return parser


def _client_kwargs(args) -> dict:
    # Burst no bigger than one second's worth of requests, so a large --workers
    # doesn't open with a spike above --rate.
    burst = max(1, min(args.workers, math.ceil(args.rate)))
    return dict(rate=args.rate, burst=burst, retries=args.retries,
                timeout=args.timeout, proxies=args.proxy, cookies=args.cookies)


def _client(args) -> HttpClient:
    return HttpClient(pool_size=args.workers, **_client_kwargs(args))


def _use_async(args) -> bool:
    if args.http == "sync":
        return False
    if _have_aiohttp():
        return True
    if args.http == "async":
        raise RuntimeError("--http async needs aiohttp: pip install 'xscraper[fast]'")
    return False


def _refs(args, positional: list[str]) -> list[str]:
    refs = list(positional)
    if args.input_file:
        f = sys.stdin if args.input_file == "-" else open(args.input_file, encoding="utf-8")
        with f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    refs.append(line)
    if not refs:
        raise ValueError("nothing to fetch: pass values on the command line or with --input")
    return refs


def _dedupe(tweets: list[Tweet], bits: int) -> list[Tweet]:
    groups = near_duplicate_groups([t.analysis["simhash"] for t in tweets], bits)
    drop = {i for g in groups for i in sorted(g)[1:]}
    if drop:
        logging.getLogger("xscraper").info("dropped %d near-duplicate tweets", len(drop))
    return [t for i, t in enumerate(tweets) if i not in drop]


def _finish(tweets: list[Tweet], args, analyze: bool) -> None:
    sink = _Sink(args, analyze)
    sink.add_many(tweets)
    sink.close()


class _Sink:
    """Analyses and writes tweets in chunks as a batch streams in.

    With --dedupe everything is buffered, since near-duplicates can be
    anywhere in the batch.
    """

    def __init__(self, args, analyze: bool):
        self.args = args
        self.analyzer = None
        if analyze or args.dedupe is not None:
            self.analyzer = Analyzer(args.engine)
            logging.getLogger("xscraper").info("analysis engine: %s", self.analyzer.engine_name)
        self.buffer: list[Tweet] = []
        self.writer = TweetWriter(args.output, args.format) if args.output else None
        self.stream = args.dedupe is None
        self.count = 0

    def add(self, tweet: Tweet) -> None:
        self.buffer.append(tweet)
        if self.stream and len(self.buffer) >= CHUNK:
            self._flush(self.buffer)
            self.buffer = []

    def add_many(self, tweets: list[Tweet]) -> None:
        for t in tweets:
            self.add(t)

    def _flush(self, tweets: list[Tweet]) -> None:
        if self.analyzer and tweets:
            self.analyzer.annotate(tweets)
        if not self.stream:
            tweets = _dedupe(tweets, self.args.dedupe)
        self.count += len(tweets)
        if self.writer:
            self.writer.write(tweets)
        else:
            for t in tweets:
                print_tweet(t)

    def close(self) -> None:
        try:
            self._flush(self.buffer)
            self.buffer = []
        finally:
            if self.writer:
                self.writer.close()
        if self.writer:
            if self.writer.fmt == "sqlite":
                print(f"stored {self.writer.written} new tweets in {self.args.output} "
                      f"({self.count} fetched)", file=sys.stderr)
            else:
                print(f"wrote {self.writer.written} tweets to {self.args.output}", file=sys.stderr)


def _count(n: Optional[int]) -> str:
    return "-" if n is None else f"{n:,}"


def print_tweet(t: Tweet) -> None:
    rt = "RT " if t.is_retweet else ""
    print(f"{t.created_at or '?'}  {rt}@{t.user.screen_name}  {t.url}")
    for line in t.text.splitlines() or [""]:
        print(f"    {line}")
    stats = (f"    ♥ {_count(t.like_count)}  ↻ {_count(t.retweet_count)}  "
             f"💬 {_count(t.reply_count)}  ❝ {_count(t.quote_count)}")
    if t.media:
        stats += f"  🖼 {len(t.media)}"
    if t.analysis:
        stats += f"  sentiment {t.analysis['sentiment']:+.2f}"
    print(stats)
    print()


def cmd_tweet(args) -> int:
    refs = _refs(args, args.tweets)
    ids = [parse_tweet_id(r) for r in refs]  # reject bad input before fetching anything
    sink = _Sink(args, args.analyze)
    got = failures = 0

    def handle(ref: str, result) -> None:
        nonlocal got, failures
        if isinstance(result, Tweet):
            sink.add(result)
            got += 1
        elif result is None:
            print(f"not available (deleted, private or withheld): {ref}", file=sys.stderr)
        else:
            print(f"failed: {ref}: {result}", file=sys.stderr)
            failures += 1

    try:
        if _use_async(args):
            from .aio import AsyncHttpClient, AsyncScraper

            async def run() -> None:
                async with AsyncHttpClient(concurrency=args.workers, **_client_kwargs(args)) as client:
                    results = AsyncScraper(client, lang=args.lang).iter_tweets(ids, return_exceptions=True)
                    i = 0
                    async for result in results:
                        handle(refs[i], result)
                        i += 1

            asyncio.run(run())
        else:
            scraper = Scraper(_client(args), lang=args.lang, workers=args.workers)
            for ref, result in zip(refs, scraper.iter_tweets(ids, return_exceptions=True)):
                handle(ref, result)
    finally:
        sink.close()
    return 0 if got and not failures else 1


def cmd_thread(args) -> int:
    tweets = Scraper(_client(args), lang=args.lang).thread(args.tweet, args.max_depth)
    _finish(tweets, args, args.analyze)
    return 0 if tweets else 1


def cmd_user(args) -> int:
    names = _refs(args, args.users)
    include_retweets = not args.no_retweets
    if _use_async(args):
        from .aio import AsyncHttpClient, AsyncScraper

        async def run() -> list:
            async with AsyncHttpClient(concurrency=args.workers, **_client_kwargs(args)) as client:
                return await AsyncScraper(client, lang=args.lang).user_timelines(names, include_retweets)

        results = asyncio.run(run())
    else:
        results = Scraper(_client(args), lang=args.lang, workers=args.workers).user_timelines(
            names, include_retweets)
    tweets: list[Tweet] = []
    failures = 0
    for name, got in zip(names, results):
        if isinstance(got, Exception):
            print(f"@{name}: {got}", file=sys.stderr)
            failures += 1
            continue
        if not got:
            print(f"@{name}: no tweets returned (X serves this widget inconsistently; "
                  "try again later or pass --cookies)", file=sys.stderr)
        tweets.extend(got[:args.limit] if args.limit else got)
    _finish(tweets, args, args.analyze)
    return 1 if failures else 0


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def cmd_analyze(args) -> int:
    tweets = load(args.input)
    analyzer = Analyzer(args.engine)
    start = time.perf_counter()
    analyzer.annotate(tweets)
    elapsed = time.perf_counter() - start
    results = [t.analysis for t in tweets]
    n = len(tweets)

    print(f"{n:,} tweets analysed with the {analyzer.engine_name} engine in {elapsed * 1000:.1f} ms")
    if not n:
        return 0
    scores = [r["sentiment"] for r in results]
    pos = sum(s > 0.05 for s in scores)
    neg = sum(s < -0.05 for s in scores)
    print(f"sentiment: mean {sum(scores) / n:+.3f} | positive {pos / n:.0%} | "
          f"neutral {(n - pos - neg) / n:.0%} | negative {neg / n:.0%}")

    groups = near_duplicate_groups([r["simhash"] for r in results], 3 if args.dedupe is None else args.dedupe)
    print(f"near-duplicate groups: {len(groups)} ({sum(len(g) - 1 for g in groups)} redundant tweets)")

    boards = {
        "hashtags": Counter(f"#{h}" for r in results for h in {x.lower() for x in r["hashtags"]}),
        "mentions": Counter(f"@{m}" for r in results for m in {x.lower() for x in r["mentions"]}),
        "cashtags": Counter(f"${c}" for r in results for c in r["cashtags"]),
        "domains": Counter(d for t in tweets for d in {_domain(u) for u in t.urls or t.analysis["urls"]}),
    }
    for title, counter in boards.items():
        if counter:
            top = ", ".join(f"{k} ({v})" for k, v in counter.most_common(args.top))
            print(f"top {title}: {top}")

    ranked = sorted(tweets, key=lambda t: t.analysis["sentiment"])
    for label, t, show in (("most negative", ranked[0], ranked[0].analysis["sentiment"] < 0),
                           ("most positive", ranked[-1], ranked[-1].analysis["sentiment"] > 0)):
        if show:
            print(f"{label} ({t.analysis['sentiment']:+.2f}): {t.text[:140]!r}")

    if args.output:
        if args.dedupe is not None:
            tweets = _dedupe(tweets, args.dedupe)
        export(tweets, args.output, args.format)
        print(f"wrote annotated tweets to {args.output}", file=sys.stderr)
    return 0


def _synthetic_corpus(n: int) -> list[str]:
    rng = random.Random(42)
    words = ("the launch was amazing but the stream kept failing and support was not helpful "
             "honestly great team really proud of everyone rocket moon mars data model release "
             "bug fix thanks love hate terrible wow").split()
    extras = ["#Artemis", "#AI", "@NASA", "@SpaceX", "$TSLA", "$NVDA",
              "https://example.com/article", "https://t.co/AbCdEf123"]
    return [" ".join(rng.choice(words) for _ in range(rng.randint(8, 40)))
            + " " + " ".join(rng.sample(extras, rng.randint(0, 3))) for _ in range(n)]


def cmd_bench(args) -> int:
    corpus = _synthetic_corpus(args.n)
    timings = {}
    outputs = {}
    for name in ("python", "wasm"):
        try:
            analyzer = Analyzer(name)
        except RuntimeError as exc:
            print(f"{name}: unavailable ({exc})")
            continue
        start = time.perf_counter()
        outputs[name] = analyzer.analyze(corpus)
        timings[name] = time.perf_counter() - start
        print(f"{name:>6}: {timings[name] * 1000:8.1f} ms  ({args.n / timings[name]:,.0f} tweets/s)")
    if len(timings) == 2:
        print(f"speed-up: {timings['python'] / timings['wasm']:.1f}x; "
              f"results identical: {outputs['python'] == outputs['wasm']}")
    return 0


def _seed(value: str) -> Item:
    """Numeric IDs and status URLs are tweets; @names, bare names and profile URLs are profiles."""
    v = value.strip()
    try:
        return Item(TWEET, parse_tweet_id(v))
    except ValueError:
        return Item(USER, parse_screen_name(v))


def _share_limits(client, args) -> None:
    """Swap the client's rate limiter and gate for ones every process on the job shares."""
    kw = _client_kwargs(args)
    client.limiter = SharedRateLimiter(args.job, kw["rate"], kw["burst"])
    client.gate = SharedRateGate(args.job)


def _fetchers(args, stack: contextlib.AsyncExitStack, observer=None):
    """Coroutines that fetch one tweet / one timeline with the chosen HTTP engine."""
    include_retweets = True
    if _use_async(args):
        from .aio import AsyncHttpClient, AsyncScraper

        async def open_async():
            client = await stack.enter_async_context(
                AsyncHttpClient(concurrency=args.workers, observer=observer, **_client_kwargs(args)))
            _share_limits(client, args)
            scraper = AsyncScraper(client, lang=args.lang)
            return scraper.tweet, lambda name: scraper.user_timeline(name, include_retweets)
        return open_async()

    client = _client(args)
    client.observer = observer
    _share_limits(client, args)
    scraper = Scraper(client, lang=args.lang, workers=args.workers)

    async def open_sync():
        async def tweet(key):
            return await asyncio.to_thread(scraper.tweet, key)

        async def user(name):
            return await asyncio.to_thread(scraper.user_timeline, name, include_retweets)
        return tweet, user
    return open_sync()


def cmd_crawl(args) -> int:
    seeds = [_seed(v) for v in (_refs(args, args.seeds) if args.seeds or args.input_file else [])]
    with JobStore(args.job) as store:
        cfg = store.configure(
            follow=parse_follow(args.follow) if args.follow is not None else None,
            max_depth=args.depth, max_attempts=args.max_attempts, rate=args.rate)
        args.rate = cfg.rate
        added = store.add(seeds)
        if seeds:
            print(f"queued {added} new seeds ({len(seeds) - added} already in the job)", file=sys.stderr)
        if args.no_run:
            return 0
        if not any(store.counts()[s] for s in ("pending", "leased")):
            print("nothing to crawl: the job has no queued items (add seeds, or `xscraper job retry`)",
                  file=sys.stderr)
            return 0 if store.counts()["done"] else 1
        logging.getLogger("xscraper").info(
            "crawl settings: follow=%s depth=%d max-attempts=%d rate=%g/s",
            ",".join(cfg.follow) or "none", cfg.max_depth, cfg.max_attempts, cfg.rate)
        helpers = _spawn_helpers(args)

        metrics = Metrics()
        limit = None if args.no_adaptive else AdaptiveLimit(args.workers)
        metrics.limit = limit
        server = metrics.serve(args.metrics_port) if args.metrics_port else None

        def progress() -> None:
            if args.progress:
                print(metrics.progress_line(), file=sys.stderr)
            _write_stats(args.stats_file, metrics)

        async def run() -> dict:
            async with contextlib.AsyncExitStack() as stack:
                fetch_tweet, fetch_user = await _fetchers(args, stack, metrics)
                crawler = Crawler(store, fetch_tweet, fetch_user, concurrency=args.workers, limit=limit,
                                  observer=metrics, progress=progress,
                                  progress_interval=args.progress or 5.0)
                return await crawler.run(max_items=args.max_items)

        start = time.perf_counter()
        try:
            totals = asyncio.run(run())
            for h in helpers:
                h.join()
        finally:
            for h in helpers:
                h.join(timeout=30)
            _write_stats(args.stats_file, metrics)
            if server is not None:
                server.shutdown()
        elapsed = time.perf_counter() - start
        if getattr(args, "quiet", False):
            return 0
        if helpers:
            print(f"{len(helpers) + 1} processes; totals below are this process's share", file=sys.stderr)
        items = totals["done"] + totals["missing"] + totals["failed"] + totals["retry"]
        print(f"crawled {items:,} items in {elapsed:.1f}s: {totals['done']:,} done, "
              f"{totals['missing']:,} unavailable, {totals['retry']:,} to retry, {totals['failed']:,} failed; "
              f"{totals['stored']:,} new tweets stored, {totals['queued']:,} links queued", file=sys.stderr)
        if metrics.drift.alerts:
            print("warning: payload drift suspected (" + ", ".join(sorted(metrics.drift.alerts))
                  + "); X may have changed a payload shape", file=sys.stderr)
        _print_job(store)
        counts = store.counts()
    return 1 if counts["failed"] else 0


def _helper_main(args) -> None:
    """Entry point of an extra crawler process started by --processes."""
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2),
                        format="%(levelname)s %(name)s: %(message)s")
    # Carry over endpoint overrides (tests and benchmarks point these at a mock).
    _scraper_mod.TWEET_ENDPOINT, _scraper_mod.TIMELINE_ENDPOINT = args.endpoints
    try:
        cmd_crawl(args)
    except KeyboardInterrupt:
        pass


def _spawn_helpers(args) -> list:
    if args.processes <= 1:
        return []
    import copy
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")  # no inherited threads, event loops or connections
    helper_args = copy.copy(args)
    helper_args.seeds, helper_args.input_file = [], None  # the parent already queued them
    helper_args.follow = helper_args.depth = helper_args.max_attempts = None
    helper_args.processes, helper_args.progress = 1, 0
    helper_args.stats_file = helper_args.metrics_port = None
    helper_args.quiet = True
    helper_args.endpoints = (_scraper_mod.TWEET_ENDPOINT, _scraper_mod.TIMELINE_ENDPOINT)
    procs = [ctx.Process(target=_helper_main, args=(helper_args,), name=f"xscraper-crawl-{i + 1}")
             for i in range(args.processes - 1)]
    for p in procs:
        p.start()
    return procs


def _write_stats(path: Optional[str], metrics: Metrics) -> None:
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(metrics.snapshot(), f, indent=2)
    os.replace(tmp, path)  # readers never see a half-written file


def _print_job(store: JobStore) -> None:
    c = store.counts()
    total = sum(c.values())
    print(f"job {store.path}: {total:,} items | " + " | ".join(f"{s} {c[s]:,}" for s in c)
          + f" | {len(store.tweets):,} tweets stored", file=sys.stderr)


def cmd_job(args) -> int:
    if not os.path.isfile(args.job):
        raise ValueError(f"no such job: {args.job}")
    with JobStore(args.job) as store:
        if args.action == "retry":
            print(f"requeued {store.retry_failed():,} failed items", file=sys.stderr)
            return 0
        if args.action == "export":
            if not args.output:
                raise ValueError("job export needs -o FILE")
            with TweetWriter(args.output, args.format) as writer:
                batch: list[Tweet] = []
                for t in store.tweets:
                    batch.append(t)
                    if len(batch) >= CHUNK:
                        writer.write(batch)
                        batch = []
                writer.write(batch)
            print(f"wrote {writer.written:,} tweets to {args.output}", file=sys.stderr)
            return 0
        cfg = store.config
        print(f"settings: follow={','.join(cfg.follow) or 'none'} depth={cfg.max_depth} "
              f"max-attempts={cfg.max_attempts}")
        c = store.counts()
        print("items: " + ", ".join(f"{s} {c[s]:,}" for s in c) + f"; tweets stored: {len(store.tweets):,}")
        now = time.time()
        for w in store.workers():
            line = f"worker {w['id']}: last heartbeat {now - w['heartbeat']:.0f}s ago"
            st = w["stats"]
            if st:
                line += (f", {st['items'].get('done', 0):,} done, {st['items_per_s']:,.0f} items/s, "
                         f"{st['responses'].get('429', 0)} × 429")
                if st.get("drift_alerts"):
                    line += ", DRIFT: " + ",".join(st["drift_alerts"])
            print(line)
        failures = store.failures(10)
        if failures:
            print("recent failures:")
            for kind, key, attempts, err in failures:
                print(f"  {kind} {key} ({attempts} attempts): {err}")
    return 0


COMMANDS = {"tweet": cmd_tweet, "thread": cmd_thread, "user": cmd_user, "crawl": cmd_crawl,
            "job": cmd_job, "analyze": cmd_analyze, "bench": cmd_bench}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2),
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return COMMANDS[args.command](args)
    except (HttpError, ParseError, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
