# xscraper: senior review and independent QA

Reviewed 2026-09-29. Scope: `main` (which already includes the throughput work, merged as PR #4) plus the unmerged tier-5 branch `claude/project-thread-jx0q4f` (4 commits: crawl jobs, metrics, multi-process, watch). All numbers below come from runs in this thread against `benchmarks/mock_x.py` and `/mnt/project-files/mock-x/server.py`. I didn't reuse any earlier claims. X hosts are blocked in this environment, so **nothing here was checked against live X**. Reproduction scripts are in `review/qa/`.

## Verdict

The engineering around the fetcher is competent. The rate cap is exact, AIMD works, the parser is tolerant, and there are 221 tests at 93% line coverage. But the "tier 5" label doesn't survive a hostile QA pass:

* The crawler **dies permanently on a single malformed payload**. It **silently strands work after a crash** and exits 0 anyway. And **with more than one process it draws 429s** despite claiming a shared budget. Those are exactly the tier-4/5 properties the ladder says it has.
* Watch mode **emits false `deleted` events** on one blank response and **can't detect real X edits** at all.
* The whole stack sits on the syndication/embed endpoints: recent-slice profile widgets, no search, no pagination, no replies-downward, no followers. It has **never been validated live** in this project. Every test and benchmark runs against mocks written by the same author as the parser, so they confirm the author's assumptions, not X's behaviour.
* The maturity ladder was written by the same work that graded itself against it.

My grade: **solid tier 3, tier 4 with blocking bugs, tier 5 not yet earned.** The data-source ceiling is closer to tier 2 than to the commercial tools the ladder cites.

## Findings, most severe first

### 1. One malformed payload kills the crawl, and the job can never finish (critical, confirmed)
`parse_tweet_result` does `data.get("__typename") in _UNAVAILABLE_TYPES` (`xscraper/parse.py:186`). If `__typename` is a list or dict, that raises `TypeError`. `Crawler._work` only catches `HttpError, ParseError, ValueError` (`xscraper/crawl.py:80`). The TypeError propagates out of `task.result()` and tears down `Crawler.run`.

```
$ python review/qa/crashone.py      # 1000 items, item 500 has "__typename": ["Tweet"]
CRAWLER DIED: TypeError unhashable type: 'list'
{'pending': 489, 'leased': 0, 'done': 511, 'missing': 0, 'failed': 0}
```
The poison item goes back to `pending` with `attempts` unchanged, so every restart dies on it again. It's never parked as failed, so this is a textbook poison pill. Fuzzing 30,000 mutated payloads found 84 such escapes, all from this one line. Everything else in the parser held.
**Fix:** guard with `isinstance(..., str)`. More importantly, make `_work` turn *any* non-cancellation exception into an `Outcome(error=...)`, so the retry/dead-letter path handles bugs as well as network errors.

### 2. Crash recovery strands leased items and still reports success (high, confirmed)
Test: a crawl of 2,001 seeds with `--follow all --depth 3`, killed with `kill -9` after 4 s, then resumed immediately.
```
before resume: pending 1,442, leased 233, done 651
after resume:  pending 0, leased 233, done 3,314     <- resume "finished", exit 0
```
`next_ready_in` (`xscraper/jobs.py:328-341`) ignores leases that haven't expired, and `Crawler.run` breaks when it returns None (`xscraper/crawl.py:131-133`). The dead worker's 233 items stay invisible until its heartbeat is 60 s stale (`reap` only runs at start) or the 300 s lease expires. A rerun 22 s later did pick them up.
**SIGTERM** (what systemd, Docker and Kubernetes send) isn't handled at all. The process dies with 143, leaving 327 items leased and discarding the unflushed outcome buffer.
**Fix:** handle SIGTERM like Ctrl-C (`loop.add_signal_handler` → `crawler.stop()`). When pending is empty but leases exist, wait and `reap` instead of exiting. Exit non-zero, or print a warning, when leased items remain at exit.

### 3. Multi-process crawls draw 429s: the server budget isn't actually shared (high, confirmed)
Mock limit: 300 requests per 3 s window, 3,001 seeds, `--rate 1000 --workers 128`:

| processes | 429s | wall |
|---|---|---|
| 1 | **0** | 38.6 s |
| 4 | **323** (≈11% of requests) | 39.9 s |

`SharedRateGate` shares only the `hold_until` pause (`xscraper/shared.py:506-546`). The window budget `remaining - in_flight` stays per process (`xscraper/http.py:218`), so each of N processes spends the whole `x-rate-limit-remaining` for itself. Against real X, repeated 429s get an IP or account restricted, which is the opposite of what the README promises ("one job-wide rate budget").
**Fix:** keep the window budget (reset time plus remaining minus job-wide in-flight) in `rate_state`, and decrement it in the same `BEGIN IMMEDIATE` transaction that hands out GCRA slots.

### 4. Watch mode reports a deletion on one blank response (high, confirmed)
`tweet_from_body` returns `None` for an empty 200 body (`xscraper/scraper.py:326-327`), and `Watcher.cycle` treats `None` as missing (`xscraper/watch.py:416`, `:434`). There's no confirmation step:
```
$ python review/qa/watchflap.py     # bodies: tweet, "", tweet
cycle 0: ['new']
cycle 1: ['deleted']
cycle 2: ['restored']
```
Those events go to webhooks. A soft block, a CDN hiccup or a widget glitch becomes a false deletion alert. That's the worst possible failure for a monitoring product.
**Fix:** treat an empty body as a retryable error. Only accept 404 or a tombstone as proof, and require K consecutive misses before emitting `deleted`.

### 5. Edit detection can't work on real X (high, by code and payload shape)
On X an edit creates a **new tweet ID**. The old ID keeps serving the old text, and the chain is in `edit_control.edit_tweet_ids`, which the project's own mock includes (`mock-x/server.py:18`). xscraper ignores `edit_control` and `isEdited`, and detects edits by re-hashing the text at the same ID (`xscraper/watch.py:226`). So a real edit never fires `edited`. Meanwhile, formatting differences between the timeline payload (`full_text`) and the embed payload (`text`) for the same ID *can* fire it spuriously. I inferred that last part, because the mocks don't share tweets between endpoints.
**Fix:** parse `edit_control.edit_tweet_ids`, and emit `edited` when the latest ID in the chain changes. Then fetch the new ID.

### 6. The data source caps what "tier 5" can mean (high, strategic)
Everything goes through `cdn.syndication.twimg.com/tweet-result` and the profile-timeline widget (`xscraper/scraper.py:289-290`).
* There's no search, no pagination, no followers or following, and no replies-to-a-tweet. `thread` and `--follow parents` only walk **up** a conversation, so `crawl --depth N` explores ancestors and quotes, never the reply tree.
* The profile widget is a recent slice, and the code already warns it's served "inconsistently".
* According to the project's own mock, the embed payload has no `retweet_count` or `quote_count` (the CLI prints `↻ -`). So engagement history for tweets watched by ID is only likes and replies.

Distributed leases, Prometheus metrics and webhooks are infrastructure around a narrow pipe. Competing tools (twscrape, commercial listening) win on *coverage*, and this design can't close that gap without touching the boundary the project deliberately set (logged-in GraphQL, accounts). That's a product decision for you to make, but the tier label shouldn't hide it.

### 7. No live validation anywhere (high, process)
X hosts are blocked in the cloud environment, and every test and benchmark uses mocks written alongside the parser. The token algorithm is checked against V8 fixtures, which is good, but nothing proves that X still serves these endpoints unauthenticated, with these shapes, at these rates. **Before building more, run one live smoke test on your own machine** (via Remote Control, or by widening the environment's network allowlist) and save real responses as fixtures.

### 8. Resume forgets `--workers` (medium, confirmed)
`JobConfig` stores follow, depth, attempts and rate, but not concurrency (`xscraper/jobs.py:92-96`). The crash test's first run used `--workers 64` at 200 req/s. The plain `xscraper crawl job.db` resume ran at the default 4 workers: **74 items/s** at 50 ms latency.

### 9. Late outcomes overwrite state without a lease check (medium, by code)
`complete()` updates by `(kind, key)` with no `lease_owner` or state guard (`xscraper/jobs.py:293-298`). A worker whose lease expired can flip an item that another worker already finished as `done` back to `pending` or `failed`. Its docstring calls this idempotent, but it's only idempotent for successes.

### 10. CLI output ergonomics (medium, confirmed)
* `--format jsonl` without `-o` is silently ignored and prints the human format, so `xscraper tweet … --format jsonl | jq` breaks.
* `-o -` writes a file literally named `-` instead of stdout.

### 11. Docs contradict themselves on evasion (low)
`docs/maturity.md` says proxy rotation is out of scope. `README.md:9,108` and `http.py:100-106,312` ship proxy rotation and a random browser User-Agent. Pick one position and document it.

### 12. Smaller items (low)
* `ruff --select E,F,B,W` reports 14 findings, mostly `zip()` without `strict=` in `watch.py`.
* `Watcher` delivers webhooks inline in the poll loop. A slow receiver (10 s timeout per batch) delays polling.
* The crawler does synchronous SQLite writes on the event loop. That's fine at 4 processes (below), but it's the first ceiling you'll hit.
* The coordinator note said the throughput branch was unmerged. It's actually merged as PR #4, and only the tier-5 branch is open.

## What held up (credit where due)

| Check | Result |
|---|---|
| Test suite (`python -m pytest`, all extras) | 221 passed in 7 s. Line coverage 93% (jobs 98%, watch 97%, cli 84%) |
| Edge payloads on the project mock (404, tombstone, protected, empty, HTML, 429-then-OK, 403, empty/blocked/missing profile) | All handled with correct messages and exit codes. The 429 was waited out using `x-rate-limit-reset` |
| Rate cap with 4 processes at `--rate 100` | Exactly 100 req/s job-wide |
| Rate cap accuracy, 1 process, `--rate 1000/3000` | 990 and 2,789 req/s (`crawl`); 997 and 2,929 req/s (`tweet`) |
| Single-process 429 avoidance, 300 req/3 s server limit | 0 × 429 |
| AIMD under overload (503 above 32 in flight, 256 workers) | 296 × 503 in 17.1 s, against 4,450 × 503 in 40.3 s with `--no-adaptive` |
| Multi-process scaling, 60k items, no rate cap | 1 proc 19.9 s, 2 procs 11.1 s, 4 procs 8.5 s. No duplicates or losses (60,001 done) |
| Parser fuzz (30,000 random type mutations) | 29,916 handled cleanly; the 84 escapes are finding 1 |
| Recovery after the dead worker went stale (rerun 22 s later) | All 233 stranded items fetched; job completed with 0 leased |

## Recommended order of work

1. Findings 1–4. They're small, local fixes, each with a regression test that matches the QA scripts here.
2. One live smoke test plus real-response fixtures (finding 7) before any more features.
3. Replace text-hash edits with `edit_control` (finding 5).
4. Decide the coverage question (finding 6) explicitly. Either accept "public embed data only" and market it that way, or scope a logged-in data source.
