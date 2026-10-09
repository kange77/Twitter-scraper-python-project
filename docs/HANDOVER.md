# xscraper handover

I'm handing xscraper over. This document is everything I'd tell you over a long coffee: what the system is, how it's built, how to run it without hurting anyone (including X), what's solid, and what isn't. Where I give a number, I measured it, and I say when and on what. Where I'm guessing, I say so.

Read sections 1–3 on day one. Keep sections 6–7 open the first time you run a crawl or a watch for real.

---

## 1. The short version

- **What it is:** a Python CLI and library that scrapes **public** tweets from X's syndication (embed) endpoints. No login is needed for tweets by ID. There's a WebAssembly analytics core for sentiment, hashtags and near-duplicates.
- **What it has grown into:** besides one-shot commands (`tweet`, `thread`, `user`), it runs **resumable crawl jobs** (`crawl`, `job`) and **scheduled monitoring** (`watch`) on one machine, with several processes sharing one rate budget.
- **State on `main` (2026-10-09):**
  - All PRs #1–#9 are merged, and CI is green on Python 3.10 and 3.12.
  - **302 tests** pass locally on Python 3.13.
  - The planted-bug (mutation) check catches 50 of 50 (see §8).
- **Honest grade on my own ladder ([docs/maturity.md](maturity.md)):**
  - **Tier 4** (durable, observable crawler): solid.
  - **Tier 5** (distributed, continuously running service): **single-host only, with known gaps** (§9).
  - Nothing here is a multi-host service.
- **The ceiling is the data source, not the code.**
  - The embed endpoints give tweets by ID and a recent slice of a profile.
  - There's no search, no pagination, no replies-downward, no followers, and no retweet counts on per-tweet fetches.
  - I decided not to go beyond public, logged-out endpoints (§5).

**Your first day:**
1. Clone, install, run the tests (§6.1). Expect `302 passed`.
2. Run `xscraper tweet 20` from your machine. It should print @jack's "just setting up my twttr". If it doesn't, X has changed something; go to §7.6.
3. Read §5 (the rules I don't break) and §9 (what's still broken) before changing anything.

---

## 2. How we got here

| When | What | PR / branch |
|---|---|---|
| 2026-09-27 | First package: models, parser, sync client, CLI, WASM core | #1, #2 |
| 2026-09-27 | Retweet text, reply mentions, pinned order, batch exit codes | #3 |
| 2026-09-27 | Async engine, shared rate-limit budget, streaming export (tier 3) | #4 |
| 2026-09-27 | Crawl jobs, metrics, multi-process, watch mode (tier 4–5 attempt) | #6 (`claude/project-thread-jx0q4f`) |
| 2026-09-29 | Senior review (S1–S12) and principal QA (P1–P10): **"not release-ready"** | reports in `quest/prior-work/` |
| 2026-10-06 | Tweet-batch poison-pill fix, done as a hiring Quest | #5, #9 (`claude/quest-quality-fix-r0t0ss`; the full submission is on `quest`) |
| 2026-10-07 | Release-gate fixes for S1–S4 and P1–P6, plus fault-injection tests | #7 (`claude/tier45-release-gate`) |
| 2026-10-07 | Live run against X; empty-timeline handling; contract tests on real responses | #8 (`claude/live-contract-update`) |
| 2026-10-09 | Everything merged to `main` | — |

**How I built it.** I built all of this with Claude agents. I set the scope, wrote or approved the instructions, decided what got rejected, and had every claim re-measured before I believed it. There's more on how in §10. The 2026-09-29 reviews matter most: they're the only adversarial look this code has had, and §9 is largely what's left from them.

---

## 3. Architecture

### 3.1 Module map (`xscraper/`, about 4,000 lines)

| Module | Job | Read it when |
|---|---|---|
| `models.py` | `Tweet`, `User` dataclasses, `to_dict` / `from_dict` | always |
| `parse.py` | Turns X's JSON and HTML into `Tweet`s. **The boundary with untrusted data.** `ParseError`, `EmptyTimelineShell` | X changes a payload |
| `token.py` | The syndication `token` parameter (port of X's JS, checked against V8 fixtures) | tweet fetches start failing with 4xx |
| `http.py` | Sync client: `RateLimiter` (token bucket), `RateGate` (server window plus 429/503 pause), retries, `MAX_RESET_HORIZON` | anything rate- or retry-related |
| `aio.py` | Async client and `AsyncScraper` (aiohttp); same semantics as `http.py` | high-throughput batches |
| `scraper.py` | `Scraper` (sync), endpoints, `tweet_from_body` (**the shared parse entry point**), `timeline_from_page` | the `tweet` / `user` flows |
| `storage.py` | JSON/JSONL/CSV export, `TweetStore` (SQLite `tweets` table) | output formats |
| `jobs.py` | `JobStore`: the frontier, leases, workers, settings, depth bookkeeping | crawl correctness |
| `crawl.py` | `Crawler`: the claim → fetch → flush loop, heartbeats, waiting for other workers | crawl behaviour |
| `shared.py` | `SharedRateLimiter` (GCRA) and `SharedRateGate`: rate state shared by processes through the job file | multi-process |
| `metrics.py` | `Metrics`, `AdaptiveLimit` (AIMD), `DriftMonitor`, Prometheus/JSON/healthz server | observability |
| `watch.py` | `WatchStore`, `Watcher`, change detection, event outbox, JSONL and webhook sinks | monitoring |
| `cli.py` | Argument parsing, commands, exit codes, `--processes` helpers, SIGTERM | anything user-facing |
| `analysis.py` + `wasm-core/` | Rust-to-WASM analytics, with a pure-Python fallback that gives the same results | analytics |

### 3.2 The tweet-by-ID path (everything uses it)
```
cli.cmd_tweet / Crawler / Watcher
   └─ fetch: HttpClient.get | AsyncHttpClient.get   (rate limiter → gate → request → retries)
        └─ scraper.tweet_from_body(id, body)
             ├─ empty body / {} / [] / null      → ParseError   (retryable, NOT "deleted")
             ├─ not JSON                         → ParseError
             └─ parse.parse_tweet_result(data)
                  ├─ unavailable type / tombstone → None         ("missing")
                  ├─ non-string __typename       → ParseError   (fail the ID, never guess)
                  └─ parse_tweet(...)            → Tweet        (attribute fields degrade to None)
             shape errors (TypeError, ValueError, LookupError, AttributeError, ArithmeticError)
             raised inside parsing → ParseError("tweet <id>: …"), with the original in __cause__
```

### 3.3 The crawl loop (`crawl.py`)
- **Claim:** claim up to 2 × the concurrency cap from the frontier. Expired leases come first, then pending items, shallowest first.
- **Fetch:** run the fetches. **Any non-cancellation exception becomes that item's error.** It counts toward `--max-attempts` (default 3), backs off 5 s × 2^(attempts−1) (capped at 300 s), and is then parked as `failed`.
- **Flush:** write outcomes every 500 items or every 1 s. Completing an item stores its tweets and queues its links at depth + 1, counted from the item's **current** depth.
- **Heartbeat:** every 5 s, renew leases (300 s each) and save stats.
- **Waiting for other workers:** when nothing is pending but other workers hold leases, wait and reap. A worker is reaped when its heartbeat is over 60 s old, or at once if its PID is gone (or is a zombie) on this host.
- **Exit:** when nothing is queued or leased anywhere, or on `--max-items`, or on stop (Ctrl-C or SIGTERM). In-flight items finish and are flushed, and leases are handed back.

### 3.4 Rate limiting: four layers
1. **`--rate` token bucket** (burst 3). For `crawl` and `watch` it's a **GCRA shared through the job file** (`SharedRateLimiter`), so N processes together send at most `--rate`.
2. **Server window** (`x-rate-limit-remaining` until `x-rate-limit-reset`). In crawls it's **one job-wide budget** (`SharedRateGate`, the `rate_state` table).
3. **429/503 with Retry-After:** pauses every worker in every process until then (`hold_until`).
4. **Adaptive concurrency** (AIMD, `metrics.AdaptiveLimit`): backs off when the server signals overload. Turn it off with `--no-adaptive`.

**Safety rails** in `http.py`:
- A reset or Retry-After more than **1 h** away is treated as malformed and ignored (it's usually milliseconds sent as seconds).
- No request waits at the gate for more than **2 × 900 s**.
- Any gate wait of **≥ 30 s** is logged at WARNING.

### 3.5 Watch (`watch.py`)
- **Each cycle:**
  - Poll due targets (profiles or tweets, `--every`, minimum 60 s).
  - Optionally re-check tracked tweets by ID (`--track 48h --recheck …`).
  - Diff against `tracked`, write `snapshots`, and append to the `events` outbox.
  - Deliver to sinks.
- **Event types:** `new`, `edited`, `deleted`, `restored`, `engagement`.
- **Error isolation:** one target's error never stops the others; it's recorded as that target's `last_error`.

---

## 4. Data on disk

**A crawl job is one SQLite file** (WAL mode). It's also a normal tweet store, so `xscraper analyze job.db` and `storage.load` work on it.

| Table | Columns that matter |
|---|---|
| `frontier` | `kind` (tweet/user), `key`, `depth`, `parent`, `state` (pending/leased/done/missing/failed), `attempts`, `not_before`, `lease_owner`, `lease_until`, `last_error` |
| `workers` | `id` (`host:pid:rand`), `host`, `pid`, `heartbeat`, `stats` (JSON) |
| `meta` | stored settings: `follow`, `max_depth`, `max_attempts`, `rate` |
| `rate_state` | `tat` (GCRA), `hold_until`, `win_reset`, `win_budget`. Old job files get the last two columns on open |
| `tweets` | `id`, `screen_name`, `created_at`, `text`, `data` (full JSON), `first_seen`, `last_seen` |

**A watch state file** has `targets`, `tracked` (last text, counts, state), `snapshots` (engagement time series) and `events` (outbox; `seq` is the idempotency key, plus `delivered`).

Useful queries:
```sql
SELECT state, COUNT(*) FROM frontier GROUP BY state;
SELECT depth, state, COUNT(*) FROM frontier GROUP BY 1, 2 ORDER BY 1;
SELECT key, attempts, last_error FROM frontier WHERE state = 'failed' ORDER BY updated DESC LIMIT 20;
SELECT id, host, pid, datetime(heartbeat, 'unixepoch') FROM workers;
SELECT type, COUNT(*), SUM(delivered) FROM events GROUP BY type;   -- watch file
```

---

## 5. Rules I don't break (please don't either)

1. **Public, logged-out data only.** No account pools, no CAPTCHA solving, no fingerprint spoofing, and nothing that gets around X's access controls. `--cookies` exists for **your own** session and nothing else.
   - **One honest contradiction to know about:** the code ships `--proxy` rotation and a random browser User-Agent, which sit awkwardly with this rule (review finding S11). I haven't resolved it. If you do, the cleaner answer is to drop rotation.
2. **Be slow by default.** `--rate 1` is the default for a reason. Multi-process adds CPU, **not** requests per second. Never "fix" throughput by multiplying the budget.
3. **One exception boundary per flow, and it's narrow:**
   - **`tweet` batch:** only `HttpError` / `ParseError` are per-item. New payload checks raise `ParseError` from `parse.py`. **No `except Exception` in the batch loops.** Bugs in HTTP, output or scheduling code must stay loud.
   - **`crawl` / `watch`:** do catch any non-cancellation exception per item. That's deliberate, because they have retry and dead-letter paths, and a poison item must not block a long-running job forever (S1, P3). The traceback is logged and failed items make the exit code 1, so bugs still surface.
4. **Payload policy:**
   - **Attribute fields** (user, entities, counts, media, dates, text) **degrade** to `None` or empty.
   - **Identity fields** (`id_str`, `__typename`, `tombstone`) **fail the ID.**
   - Never store a guess as a live tweet.
   - **An empty response is never evidence of deletion.**
5. **Every fix ships with a test that fails without it**, using the **real** bad shape, not a convenient one. Agents (and people) will write tests that pass for the wrong reason; check that.
6. **Measure the right code.** The dev venv has an editable install. Always run with `PYTHONPATH=<checkout>` and check `python -c "import xscraper; print(xscraper.__file__)"` (§11).

---

## 6. Running it

### 6.1 Setup
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"   # [fast] for aiohttp+orjson only
python -m pytest -q                      # 302 passed (about 25 s)
scripts/build_wasm.sh                    # optional: rebuild the WASM core (needs Rust + wasm32 target)
```

### 6.2 Everyday commands
```bash
xscraper tweet 20 https://x.com/NASA/status/<id>            # by ID or URL
xscraper tweet -i ids.txt -o out.jsonl --rate 1 --workers 4  # big batch, streamed
xscraper thread <id>                                         # reply chain up to a tweet
xscraper user NASA SpaceX -o tweets.db                       # profiles (see §7.5)
xscraper analyze tweets.db                                   # sentiment, tags, domains, dupes
```

### 6.3 A crawl job, properly
```bash
xscraper crawl job.db -i seeds.txt --follow all --depth 2 --rate 1 --workers 4 \
    --progress 30 --stats-file stats.json --metrics-port 9108
xscraper crawl job.db                      # resume (settings are stored in the job)
xscraper crawl job.db --depth 3            # widen later: finished items are re-expanded
xscraper crawl job.db --processes 4        # more CPU on one job, same request rate
xscraper job status job.db                # progress, workers, recent failures
xscraper job retry job.db                 # requeue failed items
xscraper job export job.db -o tweets.jsonl
```
**Seeds:** tweet IDs or URLs, or `@profiles`. **Use tweet IDs for anything you care about** (§7.5).

### 6.4 A watch, properly
```bash
xscraper watch state.db @NASA --every 15m --track 48h --engagement-change 50 \
    --events events.jsonl --webhook https://example.invalid/hook --metrics-port 9109
xscraper watch state.db --status            # targets and counts
xscraper watch state.db --history <tweet_id> # one tweet's engagement over time
xscraper watch state.db --once              # poll what's due once (cron-friendly)
xscraper watch state.db @NASA --unwatch
```
**One watcher per state file.** Two will double-deliver webhooks (P8, §9).

### 6.5 Exit codes (use them in cron or CI)

| Command | 0 | 1 | Other |
|---|---|---|---|
| `crawl` | job finished, nothing failed | failed items, a helper process crashed, or items still queued/leased (unless `--max-items`) | **143**: stopped by SIGTERM (resumable) · **130**: Ctrl-C |
| `tweet`, `user`, `thread` | everything fetched | at least one ID or profile failed (each is named on stderr as `failed: …`) | 130: Ctrl-C |
| `watch` | no target has an error | at least one target's last poll failed | 130: Ctrl-C |

### 6.6 Running it as a service
- **Stopping:** SIGTERM is handled. In-flight items finish, outcomes are flushed, leases go back, helpers are stopped, and the exit code is 143. That works with `systemd` / Docker / Kubernetes defaults. Restart the same command to resume.
- **Health:** `--metrics-port` serves `/metrics` (Prometheus), `/stats` (JSON) and `/healthz`. `/healthz` returns 503 when work is queued but nothing has finished for **300 s**. Use it as a liveness probe for crawls.
- **Metrics worth alerting on:**
  - `xscraper_responses_total{status="429"}` rising;
  - `xscraper_drift_alert == 1`;
  - `xscraper_frontier_items{state="failed"}` growing;
  - `xscraper_wait_seconds_total` jumping (long gate waits).

---

## 7. Runbooks

### 7.1 A crawl stopped (crash, kill -9, reboot)
Run the same `xscraper crawl job.db`.
- **Same host:** a dead worker is reaped at once if its PID is gone, or (on Linux) is a zombie.
- **Another host, or a reused PID:** it takes up to 60 s, until the heartbeat goes stale.

The crawl won't report "finished" while another worker holds leases. Check with `job status`, then `SELECT … FROM workers`.

### 7.2 Exit 1 with "items are still queued or leased"
Something stopped early: a helper crashed, or the run was cut short. Just resume. If it repeats, look for `crawler process <pid> exited with code …` lines and for tracebacks logged as `unexpected error on tweet <id>`.

### 7.3 Lots of `failed` items
Look at `last_error` first.
- **The same error everywhere** (`ParseError: … unexpected payload shape`, `__typename`, `empty response`): X probably changed a payload, which is the drift case (§7.6).
- **Scattered 5xx or timeouts:** run `xscraper job retry job.db`, then resume.

### 7.4 429s
- **One process:** this shouldn't happen. The window budget comes from X's own headers. Check `--rate`, and whether something else is sharing your IP.
- **Several processes:** you'll see a few (§9, S3). Each one pauses every process until the reset. Use fewer processes if it matters; processes add CPU, not requests.
- **"ignoring x-rate-limit-reset … (milliseconds?)" in the log:** a proxy or X sent a malformed header, and the client ignored it rather than hanging. That's working as intended.

### 7.5 `user` / profile seeds return nothing
On 2026-10-07 the logged-out profile widget returned **0 entries** to one request and **20 entries** to the same request 25 minutes later, for @NASA. It's intermittent.
- The `user` command says so and exits 1.
- In crawl and watch it's a retryable error (`EmptyTimelineShell`), not "done, 0 tweets".
- To get timelines reliably, use `--cookies` with your own session, or seed with tweet IDs.

### 7.6 X changed something (payload drift)
**Symptoms:**
- `warning: payload drift suspected (…)` at the end of a run;
- `xscraper_drift_alert`;
- many identical `ParseError`s;
- `test_contract_live.py` failing after you refresh the fixtures.

**Steps:**
1. Save a few raw responses at 1 request/s or slower: `tweet_params(id, "en")` against `TWEET_ENDPOINT`.
2. Compare them with `tests/fixtures/live/`.
3. Fix the parser in `parse.py` following the payload policy (§5.4).
4. Add the new shape as a fixture and a contract test.

### 7.7 Watch says a tweet was deleted, then restored
- **Since 2026-10-07:** empty bodies and `{}` / `null` / `[]` are no longer read as deletions, and a payload missing `text` no longer fires `edited`.
- **Still possible:** a single real 404 or tombstone fires `deleted` immediately. There's no "K consecutive misses" rule yet (§9). Downstream consumers should treat `deleted` followed by `restored` as noise.

### 7.8 The JSONL events file is missing events
That's P7. The file sink is at-most-once: if a write fails (disk full), those events never reach the file. They are in the state file's `events` table, so recover with SQL (`SELECT … FROM events WHERE seq > <last seq in file>`). The webhook sink has an outbox and retries.

---

## 8. Tests and QA: what protects you

**`pytest -q`: 302 tests, about 25 s, 60 s timeout per test** (`pytest-timeout`).

| Layer | Where | What it guards |
|---|---|---|
| Unit / parser | `test_parse.py`, `test_scraper.py`, `test_storage.py`, `test_token.py` | payload policy, token algorithm, stores |
| Engines | `test_http.py`, `test_aio.py`, `test_cli.py` | retries, gate, rate limits, CLI exit codes, both engines |
| Crawl / watch | `test_crawl.py`, `test_shared.py`, `test_watch.py`, `test_metrics.py` | frontier, leases, depth, multi-process, events, AIMD, drift |
| **Fault injection** | `test_faults.py` | SIGTERM on a real 2-process CLI crawl, slow fetches against short leases, zombies, exit-status table, shared window, depth races |
| **Contract** | `test_contract_live.py` + `fixtures/live/` | the parser against **real** X responses saved 2026-10-07 |
| **Yardstick gate** | `test_yardstick.py` | the tweet batch on 1,000 IDs with 3 poisoned: nothing lost, nothing blank, every bad ID named, clean batch unchanged |

**Mutation testing** (`scripts/mutants.py`): 50 planted single-line bugs across leases, retries, gate, shared window, depth, exit codes, watch and parser. On 2026-10-07 the suite caught **34 of 50**. After `test_faults.py` it catches **50 of 50**, rechecked on `main` on 2026-10-09.
- **Run it on a throwaway copy, never your working tree:** it edits files in place.
- Run it before trusting any change to `jobs.py`, `crawl.py`, `shared.py`, `http.py` or `watch.py`.

**CI:** `.github/workflows/ci.yml` runs the suite on 3.10 and 3.12, with and without the optional extras. **To do:** add `timeout-minutes: 20` and 3.13 to the matrix. I couldn't push workflow changes with the token I had (§11).

---

## 9. What's still broken or missing, ranked

| # | Issue | Impact | Notes |
|---|---|---|---|
| 1 | **S3, partly fixed: multi-process 429s** | 4 processes on a 300 req / 3 s mock window: 359 → 47–76 per run (4 runs, 2026-10-07); 8 processes: 99 | The remaining overshoot is other processes' in-flight requests when a window opens. The fix is a job-wide in-flight counter that survives crashes. |
| 2 | **P8: two watchers on one state file** | every webhook event delivered twice | Add a lock on the state file; document `seq` as the idempotency key. |
| 3 | **P7: the JSONL sink is at-most-once** | events lost on a failed write | Give each sink a cursor (last `seq` written) in the state file, and replay on start. |
| 4 | **Deletion on a single 404 / tombstone** | false `deleted` alerts on a flaky response | Add a K-consecutive-misses rule. |
| 5 | **S5: edit detection** | real X edits create a **new ID** (`edit_control.edit_tweet_ids`); we re-hash text at the same ID | No edited tweet turned up in 41 live probes, so this is untested. |
| 6 | **P9: store upsert erases counts** | refetching by ID nulls the retweet/quote counts a timeline gave | Merge on upsert, keeping old values where the new one is `None`. |
| 7 | **S9: late outcomes without a lease check** | an expired worker can overwrite another's `done` | Guard `complete()` on `lease_owner`. |
| 8 | **S8: resume forgets `--workers`** | resumes at the default 4 | Store it in `meta`. |
| 9 | **P10: clock skew** | +90 s skew means straight into 429s; −600 s means over-waiting | Use the offset from the `Date` header. |
| 10 | Data-model choices | `verified` is `verified or is_blue_verified` (paid and legacy merged); timeline retweets carry the wrapper's like count (0) | These are product decisions, not bugs. |
| 11 | S11 / S12 | the proxy/User-Agent contradiction (§5); 14 ruff findings (as of the 09-29 review); webhooks delivered inline in the poll loop | |
| 12 | Version | still `2.0.0` after four new commands and new file formats | Bump to 3.0.0 when you cut a release. |

**Out of reach by design:** search, pagination, replies-downward, followers, and full timelines. They'd need logged-in or paid APIs, and that's a decision to make deliberately, not a patch.

---

## 10. How I worked (and how I'd like you to)

I built this with AI coding agents. What made it work was the process, not the model:

- **Write the directive as observable outcomes.** For the tweet-batch fix, my first directive said "treat a bad `__typename` as an ordinary tweet". The agent did exactly that, and malformed payloads became **blank tweets with exit 0**, with a green test suite.
  - I tested this afterwards with fresh agents: the old directive went **0 of 3**, and the revised one, which states what the user must receive, went **3 of 3** (`quest/experiment/`).
  - Every agent on the old directive flagged the conflict and implemented it anyway. **An agent flagging a risk is not an agent refusing it.**
- **Measure what the user gets, not what the tests say.** `quest/checks.py` counts tweets written and failures *named*, not just rows. That's how the blank-tweet fix got caught.
- **Re-measure agent claims.** A QA agent reported "6 mutants survive"; that was true for the commit it tested, but stale for the branch head. A live-crawl agent's run finished early, so the SIGTERM path it was meant to exercise never ran. It said so, but that's easy to miss in a long report. Check every claim against the current code.
- **Isolate parallel agents, all the way.** One git worktree per agent, separate ports, and **no `git stash`**: stashes are shared by every worktree of a repo, and that contaminated 2 of 6 runs once.
- **Fault injection and mutation testing are the acceptance gate for anything with leases, retries or delivery semantics.** The original suite had 92% coverage and still missed every lease-renewal and retry bug.

---

## 11. Gotchas that cost me time

- **Measuring the wrong code:** `python -c` puts the current directory first on `sys.path`, and the venv has an editable install of another checkout. Always set `PYTHONPATH` and print `xscraper.__file__`.
- **`--processes` needs a `__main__` guard** in any wrapper script (spawn start method). Without it, every helper crashes at start-up. The current code reports that as exit 1; the old code hid it behind exit 0.
- **`pkill -f <pattern>` can kill the shell running it** if the pattern appears in its own command line.
- **GitHub token scope:** the `gh` token used here lacks `workflow`, so CI-file changes get rejected. Run `gh auth refresh -s workflow` first.
- **The live endpoints, as observed 2026-10-07:**
  - `tweet-result` sends **no** `x-rate-limit-*` headers and is CDN-cached (`max-age=60`).
  - The timeline sends `x-rate-limit-limit: 30`.
  - **Deleted and withheld tweets come back as HTTP 200 tombstones, not 404.**
- **The live baseline, 2026-10-07:** 167 tweet-ID seeds (from Wikipedia's public pages), `--follow all --depth 2 --rate 1`, gave 213 requests, every one HTTP 200, 193 tweets and 20 unavailable, in 228 s.
- **Mock throughput, 2026-10-07:**
  - 20,000 seeds with no rate limit: 5.8 s on 1 process, 3.6 s on 4.
  - The shared-window check costs about 20% with 4 processes, compared with before it existed.
  - These are mock numbers; real X is bounded by `--rate`.
- **Timings on the mock are noisy:** up to about 1 s run to run on a 2 s batch. Don't claim a speed-up from one run.

---

## 12. Where things are

| What | Where |
|---|---|
| Code, tests, CI | `main` on GitHub (`kange77/Twitter-scraper-python-project`) |
| The review reports and their repro scripts | `quest/prior-work/` |
| The tweet-batch fix with full evidence (directive, review, experiment) | `quest/` (start at `quest/README.md`) |
| Change history, with what's fixed and not | `CHANGELOG.md` |
| Maturity ladder | `docs/maturity.md` |
| Mutation script | `scripts/mutants.py` |
| Pending CI change | §8. Apply it by hand once the token has `workflow` scope. |

If something in here turns out to be wrong, fix the doc in the same PR as the code. That's the rule I tried to keep.

— Karimi

<sub>Written with Claude from the commit history, test runs and QA reports of this project. The numbers were re-run on 2026-10-09 unless a different date is given.</sub>
