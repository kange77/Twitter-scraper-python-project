"""Command-line interface: ``xscraper {tweet,thread,user,analyze,bench}``."""
from __future__ import annotations

import argparse
import logging
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
from .scraper import Scraper
from .storage import FORMATS, detect_format, export, load


def _add_network_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("network")
    g.add_argument("--rate", type=float, default=1.0, help="max requests per second (default 1)")
    g.add_argument("--retries", type=int, default=5, help="retries per request (default 5)")
    g.add_argument("--timeout", type=float, default=20.0, help="request timeout in seconds")
    g.add_argument("--workers", type=int, default=4, help="concurrent fetches for many tweets")
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
    p.add_argument("tweets", nargs="+", metavar="ID_OR_URL")
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("thread", help="fetch the reply chain leading to a tweet")
    p.add_argument("tweet", metavar="ID_OR_URL")
    p.add_argument("--max-depth", type=int, default=50)
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("user", help="fetch recent tweets from one or more profiles")
    p.add_argument("users", nargs="+", metavar="SCREEN_NAME")
    p.add_argument("--no-retweets", action="store_true")
    p.add_argument("--limit", type=int, help="keep at most N tweets per user")
    _add_network_args(p)
    _add_output_args(p)

    p = sub.add_parser("analyze", help="analyse tweets saved as .json/.jsonl/.db")
    p.add_argument("input")
    p.add_argument("--top", type=int, default=10, help="entries per leaderboard")
    _add_output_args(p, analyze_flag=False)

    p = sub.add_parser("bench", help="benchmark the WASM engine against pure Python")
    p.add_argument("-n", type=int, default=20000, help="number of synthetic tweets")
    return parser


def _client(args) -> HttpClient:
    return HttpClient(rate=args.rate, burst=max(1, args.workers), retries=args.retries,
                      timeout=args.timeout, proxies=args.proxy, cookies=args.cookies)


def _dedupe(tweets: list[Tweet], bits: int) -> list[Tweet]:
    groups = near_duplicate_groups([t.analysis["simhash"] for t in tweets], bits)
    drop = {i for g in groups for i in sorted(g)[1:]}
    if drop:
        logging.getLogger("xscraper").info("dropped %d near-duplicate tweets", len(drop))
    return [t for i, t in enumerate(tweets) if i not in drop]


def _finish(tweets: list[Tweet], args, analyze: bool) -> None:
    if analyze or args.dedupe is not None:
        analyzer = Analyzer(args.engine)
        logging.getLogger("xscraper").info("analysis engine: %s", analyzer.engine_name)
        analyzer.annotate(tweets)
        if args.dedupe is not None:
            tweets = _dedupe(tweets, args.dedupe)
    if args.output:
        n = export(tweets, args.output, args.format)
        if detect_format(args.output, args.format) == "sqlite":
            print(f"stored {n} new tweets in {args.output} ({len(tweets)} fetched)", file=sys.stderr)
        else:
            print(f"wrote {n} tweets to {args.output}", file=sys.stderr)
    else:
        for t in tweets:
            print_tweet(t)


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
    scraper = Scraper(_client(args), lang=args.lang, workers=args.workers)
    results = scraper.tweets(args.tweets, return_exceptions=True)
    tweets = []
    for ref, result in zip(args.tweets, results):
        if isinstance(result, Tweet):
            tweets.append(result)
        elif result is None:
            print(f"not available (deleted, private or withheld): {ref}", file=sys.stderr)
        else:
            print(f"failed: {ref}: {result}", file=sys.stderr)
    _finish(tweets, args, args.analyze)
    return 0 if tweets else 1


def cmd_thread(args) -> int:
    tweets = Scraper(_client(args), lang=args.lang).thread(args.tweet, args.max_depth)
    _finish(tweets, args, args.analyze)
    return 0 if tweets else 1


def cmd_user(args) -> int:
    scraper = Scraper(_client(args), lang=args.lang)
    tweets: list[Tweet] = []
    failures = 0
    for name in args.users:
        try:
            got = scraper.user_timeline(name, include_retweets=not args.no_retweets)
        except (HttpError, ParseError) as exc:
            print(f"@{name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if not got:
            print(f"@{name}: no tweets returned (X serves this widget inconsistently; "
                  "try again later or pass --cookies)", file=sys.stderr)
        tweets.extend(got[:args.limit] if args.limit else got)
    _finish(tweets, args, args.analyze)
    return 1 if failures == len(args.users) else 0


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


COMMANDS = {"tweet": cmd_tweet, "thread": cmd_thread, "user": cmd_user,
            "analyze": cmd_analyze, "bench": cmd_bench}


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
