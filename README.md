# xscraper: X/Twitter scraper with a WebAssembly analytics core

`xscraper` fetches public tweets from X (Twitter) and analyses them with a Rust core compiled to **WebAssembly**. The core runs in-process through [wasmtime](https://pypi.org/project/wasmtime/).

- **No login or API keys.** It uses X's syndication endpoints, the ones behind embedded tweets and profile widgets, so it doesn't break every time x.com's frontend changes.
- **WASM analytics, 15–20× faster than pure Python.** The core extracts hashtags, mentions, cashtags and URLs, scores sentiment, and computes SimHash fingerprints for near-duplicate detection.
- **Works without WASM too.** A pure-Python port gives identical results. It is fuzz-tested against the WASM build, so installing wasmtime is optional. Both engines share one generated Unicode table, so hashtags in scripts with combining marks (Hindi, Tamil, Thai, …) come out whole.
- **High-throughput fetching.** With the `fast` extra, batches run on an asyncio client (aiohttp) that keeps hundreds of connections alive from one process: about 4,000 tweets/s against the local benchmark mock, versus about 700/s for the thread-pool path. Results stream to disk as they arrive, so memory stays flat for batches of any size.
- **Resilient networking that follows the server's rate-limit signals.** Requests go through a token-bucket rate limiter. Every worker shares one budget built from `x-rate-limit-remaining` / `x-rate-limit-reset`, so when the window runs out new requests wait for the reset instead of drawing 429s, and a 429's `Retry-After` pauses all workers, not just the one that got it. Failed requests are retried with jittered exponential backoff. You can rotate through several proxies.
- **Exports** to JSON, JSON Lines, CSV and an **incremental SQLite store**. Repeated scrapes against the same store only add new tweets.

```
$ xscraper bench
python:   8796.9 ms  (2,274 tweets/s)
  wasm:    520.3 ms  (38,440 tweets/s)
speed-up: 16.9x; results identical: True
```

## Install

```bash
pip install ".[wasm,fast]" # WebAssembly engine + async HTTP and orjson (recommended)
pip install ".[wasm]"      # WebAssembly engine, thread-pool HTTP
pip install .              # pure-Python engine only
```

The compiled `xscraper_core.wasm` (about 100 KB) ships in the package, so you don't need Rust unless you change the core.

## Command line

```bash
# One or more tweets, by ID or URL (fetched concurrently)
xscraper tweet https://x.com/NASA/status/1834231234567890123 20

# Large batches: IDs from a file (or - for stdin), streamed to JSON Lines.
# Throughput is --rate (requests/s) as long as --workers covers the latency.
xscraper tweet -i ids.txt --rate 20 --workers 64 -o tweets.jsonl

# The reply chain leading up to a tweet, oldest first
xscraper thread https://x.com/someone/status/123456789

# Recent tweets from profiles, with analysis and near-duplicate removal
xscraper user NASA SpaceX --analyze --dedupe -o tweets.jsonl

# Keep adding to a SQLite store; each run reports how many tweets were new
xscraper user NASA -o archive.db

# Analyse what you've collected: sentiment, top hashtags, mentions, cashtags,
# domains, and near-duplicate groups
xscraper analyze tweets.jsonl --engine wasm -o annotated.csv

# Benchmark the WASM engine against pure Python
xscraper bench -n 50000
```

Network options (available on `tweet`, `thread` and `user`):

| Option | Default | Meaning |
|---|---|---|
| `--rate` | `1.0` | Maximum requests per second |
| `--retries` | `5` | Retries per request on 429, 5xx, timeouts and connection errors |
| `--workers` | `4` | Requests in flight at once for batches and multiple profiles |
| `--http` | `auto` | `async` (needs the `fast` extra), `sync` (thread pool), or `auto`: async when aiohttp is installed |
| `--proxy URL` | none | Proxy to use; repeat the option to rotate through several |
| `--cookies` | `$XSCRAPER_COOKIES` | Cookie header to send, for when the profile widget comes back empty |
| `--timeout` | `20` | Request timeout in seconds |

The old `python "twitter scraper.py" <username>` command still works and runs `xscraper user`.

## Python API

```python
from xscraper import Analyzer, HttpClient, Scraper, export, near_duplicate_groups

scraper = Scraper(HttpClient(rate=2, proxies=["http://proxy:8080"]))
tweets = scraper.user_timeline("NASA")
tweets += [t for t in scraper.tweets(["1834231234567890123", "20"]) if t]

analyzer = Analyzer()            # "auto": WASM if wasmtime is installed, else Python
analyzer.annotate(tweets)        # sets tweet.analysis
for t in tweets:
    print(t.analysis["sentiment"], t.analysis["hashtags"], t.text[:80])

dupes = near_duplicate_groups([t.analysis["simhash"] for t in tweets], max_distance=3)
export(tweets, "tweets.db")      # also .json / .jsonl / .csv
```

For big batches from Python, the async API streams results in input order while keeping a bounded number of requests in flight:

```python
import asyncio
from xscraper.aio import AsyncHttpClient, AsyncScraper
from xscraper.storage import TweetWriter

async def main():
    async with AsyncHttpClient(rate=20, concurrency=64) as client:
        scraper = AsyncScraper(client)
        with TweetWriter("tweets.jsonl") as out, open("ids.txt") as ids:
            async for t in scraper.iter_tweets(ids, return_exceptions=True):
                if t is not None and not isinstance(t, Exception):
                    out.write([t])

asyncio.run(main())
```

`Scraper.iter_tweets` is the thread-pool equivalent.

Each `Tweet` has `id`, `text` (with t.co links expanded), `created_at` (ISO 8601 UTC), `user`, like, retweet, reply and quote counts, `hashtags`, `mentions`, `urls`, and `media` (photo URLs plus the highest-bitrate MP4 for videos). It also records reply, quote and retweet links.

## Throughput

`benchmarks/bench_fetch.py` runs the fetch, rate-limit and export paths against `benchmarks/mock_x.py`, a local stand-in for the syndication endpoints, so no traffic reaches X. Point `PYTHONPATH` at another checkout to compare versions with the same script. On a 4-core container:

| Scenario | Thread pool (before) | Async client |
|---|---|---|
| No added latency, 5,000 tweets | ~800 tweets/s | ~4,200 tweets/s |
| 50 ms latency, 4 workers (default) | 75 tweets/s | n/a |
| 50 ms latency, 64 in flight | ~670 tweets/s | ~1,200 tweets/s |
| 50 ms latency, 256 in flight | n/a | ~3,400 tweets/s |
| Server limit 300 per 3 s window | 128 × 429 | 0 × 429 |

Exports got faster too: 50,000 tweets to JSON Lines went from ~31,000 to ~55,000 tweets/s, and to SQLite from ~21,000 to ~61,000 tweets/s (batched upserts).

Against X itself the ceiling is the rate limit, not the client: set `--rate` to what you are allowed and raise `--workers` until it covers the network latency (rate × latency, plus headroom).

## How the WASM core works

```
wasm-core/src/lib.rs  ──cargo build --target wasm32-unknown-unknown──▶  xscraper_core.wasm
                                                                              │
xscraper/analysis/wasm_engine.py  ── wasmtime ── alloc / analyze / dealloc ───┘
```

- **Batched ABI.** Texts are packed into one length-prefixed buffer (`[u32 len][utf-8]…`) and analysed in a single call. The results come back as one JSON array, so the cost of crossing the boundary is paid once per batch rather than once per tweet. Batches are capped at 4 MB to keep guest memory bounded.
- **Compiled-module cache.** Compiled machine code is serialised to `~/.cache/xscraper/`, keyed by the WASM hash and the wasmtime version, so later runs skip compilation.
- **Thread-safe.** The wasmtime store is guarded by a lock, so one `Analyzer` can be shared across threads.
- **Algorithms:**
  - Twitter-style entity rules: word boundaries, the 15-character limit on @handles, cashtags of up to 6 letters, and URL trailing-punctuation trimming that keeps balanced parentheses.
  - An AFINN-style lexicon with negation and booster handling, normalised VADER-style to [-1, 1].
  - A 64-bit SimHash over word unigrams and bigrams (FNV-1a). Near-duplicate search uses banded LSH, which finds all pairs within *k* bits without comparing every pair to every other pair.

To rebuild after changing the Rust code:

```bash
scripts/build_wasm.sh        # runs cargo tests, builds, copies the .wasm into the package
pytest                       # includes WASM ↔ Python parity tests
```

The character table both engines use is generated by `scripts/gen_unicode_tables.py`; rerun it (with the newest Python you have) and then `scripts/build_wasm.sh` to move to a newer Unicode version. The only remaining WASM/Python difference is lowercasing of a few dozen letters newer than your Python's Unicode data.

If you change the algorithm in `lib.rs`, make the same change in `xscraper/analysis/python_engine.py` (and in `lexicon.py` if you edit the word tables). The parity and lexicon tests fail if the two drift apart.

## Limitations

- **Coverage.** The syndication endpoints only serve public data. The profile widget returns a recent slice of a timeline, not the full history. X changes and restricts these endpoints from time to time. When the widget comes back empty, passing your own logged-in cookies via `--cookies` sometimes helps. Fetching individual tweets by ID is the most reliable mode.
- **Retweet counts.** The per-tweet embed endpoint doesn't report retweet counts, so `retweet_count` is `None` for tweets fetched that way.
- **Sentiment.** The sentiment model is a small English lexicon: fast and transparent, but not a replacement for a trained model.
- **Test data.** The test fixtures are modelled on the syndication payload formats. They are not live captures, so the first thing to check if scraping stops working is whether X has changed a payload shape (see `xscraper/parse.py`).

## Legal

Respect X's Terms of Service, robots rules and applicable law. Keep request rates modest (the default is 1 request per second) and within what X allows you; the client follows the server's rate-limit headers but it's your `--rate` that sets the pace. This project is provided as-is, without warranty.

## License

MIT
