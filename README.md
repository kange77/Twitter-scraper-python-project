# xscraper: X/Twitter scraper with a WebAssembly analytics core

`xscraper` fetches public tweets from X (Twitter) and analyses them with a Rust core compiled to **WebAssembly**. The core runs in-process through [wasmtime](https://pypi.org/project/wasmtime/).

- **No login or API keys.** It uses X's syndication endpoints, the ones behind embedded tweets and profile widgets, so it doesn't break every time x.com's frontend changes.
- **WASM analytics, 15–20× faster than pure Python.** The core extracts hashtags, mentions, cashtags and URLs, scores sentiment, and computes SimHash fingerprints for near-duplicate detection.
- **Works without WASM too.** A pure-Python port gives identical results. It is fuzz-tested against the WASM build, so installing wasmtime is optional. Both engines share one generated Unicode table, so hashtags in scripts with combining marks (Hindi, Tamil, Thai, …) come out whole.
- **Resilient networking.** Requests go through a token-bucket rate limiter. Failed requests are retried with jittered exponential backoff, which honours `Retry-After` and `x-rate-limit-reset`. You can rotate through several proxies and fetch in parallel.
- **Exports** to JSON, JSON Lines, CSV and an **incremental SQLite store**. Repeated scrapes against the same store only add new tweets.

```
$ xscraper bench
python:   8796.9 ms  (2,274 tweets/s)
  wasm:    520.3 ms  (38,440 tweets/s)
speed-up: 16.9x; results identical: True
```

## Install

```bash
pip install ".[wasm]"      # with the WebAssembly engine (recommended)
pip install .              # pure-Python engine only
```

The compiled `xscraper_core.wasm` (about 100 KB) ships in the package, so you don't need Rust unless you change the core.

## Command line

```bash
# One or more tweets, by ID or URL (fetched concurrently)
xscraper tweet https://x.com/NASA/status/1834231234567890123 20

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
| `--workers` | `4` | Concurrent fetches when you ask for many tweets |
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

Each `Tweet` has `id`, `text` (with t.co links expanded), `created_at` (ISO 8601 UTC), `user`, like, retweet, reply and quote counts, `hashtags`, `mentions`, `urls`, and `media` (photo URLs plus the highest-bitrate MP4 for videos). It also records reply, quote and retweet links.

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

Respect X's Terms of Service, robots rules and applicable law. Keep request rates modest (the default is 1 request per second). This project is provided as-is, without warranty.

## License

MIT
