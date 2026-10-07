# xscraper: principal QA review

Reviewed 2026-09-29 on branch `claude/project-thread-jx0q4f` at `7009594` (crawl jobs, metrics, multi-process, watch). This review builds on the senior review in `review/xscraper-review.md` and doesn't repeat its findings; references like "S1" point to that report's finding numbers. I reproduced every finding below myself against a new hostile mock server (`review/principal-qa/hostile_mock.py`). Live X is blocked here, so nothing was checked against real X.

## Verdict: not release-ready

The earlier review found the bugs you would find by reading the code. This pass asked a different question: **what happens when the environment misbehaves, and would the test suite notice?** The answers:

* **The suite is well built but blind to failure modes.** It has 221 tests and 92% line+branch coverage, and it stays stable under random order and CPU saturation. But **6 of 16 deliberately planted bugs in the crash-recovery, retry and watch code went undetected**. Lease renewal can be deleted outright and every test still passes.
* **The poison-pill class is wider than the parser.** One bad `Content-Type` header crashes async runs, and the one-line `parse.py` fix proposed in S1 won't touch it. The same class of crash also kills **watch mode** permanently, and with `--processes` it takes down processes one at a time **while the parent still exits 0**.
* **One header value can hang the client forever.** If `x-rate-limit-reset` arrives in milliseconds, the run goes silent indefinitely. No log line, no timeout, no exit.
* **Crawl results aren't reproducible.** A single transient 503 changes which tweets a depth-limited crawl collects. Raising `--depth` on an existing job silently does nothing.
* **Watch mode's delivery guarantees are weaker than documented.** The JSONL sink loses events for good if a write fails. Two watchers on one state file deliver every webhook event twice.

Release gate: fix P1–P5 and S1–S4, then add the missing test layers (section 3) so these bugs can't come back unnoticed.

---

## 1. New findings (all reproduced)

| # | Finding | Severity | Repro |
|---|---|---|---|
| P1 | Bad charset header crashes async `user`/`crawl`/`watch`; the crawl job becomes unfinishable | critical | `xs.py user badcharset` |
| P2 | `x-rate-limit-reset` in milliseconds hangs the client forever, with no log output | high | `xs.py tweet 901 10 --workers 1` |
| P3 | The poison-pill crash also kills watch mode, for every target, on every restart | high | `watch_faults.py` |
| P4 | `--processes`: helper crashes are ignored; the parent exits 0 with work unfinished | high | `mp_exit.py` |
| P5 | Crawl coverage depends on timing: depth is set by whichever path arrives first | high | `depth_race.py` |
| P6 | Raising `--depth` on an existing job is a silent no-op | medium | `depth_raise.py` |
| P7 | The JSONL event sink is at-most-once: a failed write loses events permanently | medium | `watch_faults.py` |
| P8 | Two watchers on one state file double-deliver every webhook event | medium | `webhook_dupes.py` |
| P9 | The tweet store is last-write-wins: refetching by ID erases retweet and quote counts | medium | `store_clobber.py` |
| P10 | Local clock skew against X breaks the rate budget (429s, or 10× over-waits) | low–medium | `clock_skew.py` |

All scripts are in `review/principal-qa/`. Start the mock with `python hostile_mock.py` (port 8790), then run scripts from the repo venv. `xs.py` is the CLI pointed at the mock.

### P1. A bad charset header is a transport-level poison pill (critical)
The async client decodes timeline pages with the server-supplied charset (`xscraper/aio.py:55`, `self.content.decode(self.encoding, …)`). An unknown charset raises `LookupError`. That isn't an `HttpError`, `ParseError` or `ValueError`, so it gets past `Crawler._work` (`crawl.py:80`), `AsyncScraper.user_timelines` (`aio.py:262`) and `cli.main` (`cli.py:767`).
```
$ xs.py user nasa badcharset --http async      -> Traceback … LookupError: unknown encoding: x-bogus  (@nasa's results lost too)
$ xs.py crawl cs.db @nasa @badcharset 2 3 4    -> LookupError, exit 1; job: pending 2, done 6
$ xs.py crawl cs.db                             -> LookupError again (unfinishable)
$ xs.py user badcharset --http sync             -> handled ("no tweets returned"): requests falls back
```
**Why it matters beyond S1:** S1 proposed fixing `parse.py:186`. That fix leaves this crash in place. It's the second independent route to the same failure, and the fuzzer can't reach it because it mutates payloads, not headers. Any CDN, proxy or captive portal that mislabels a page triggers it.
**Fix:** decode with `codecs.lookup` inside a try, falling back to utf-8. More importantly, adopt S1's general fix: `_work` and `Watcher._bounded` should convert every non-cancellation exception into an item error, so it counts toward `max_attempts` and gets dead-lettered.

### P2. One header value hangs the client forever (high)
`RateGate.leave` (`http.py:146-153`) trusts any `x-rate-limit-reset` later than now. With `remaining: 0` and a reset in milliseconds (≈56 years away), `_wait` returns a huge value. `_enter_gate` then sleeps `min(wait, max_backoff)` in a loop, and the gate re-arms on every pass, so the `max_wait=900` cap never ends the wait.
```
$ timeout 200 xs.py tweet 901 10 11 12 --workers 1 --http async   -> exit 124 after 200 s; server saw 1 request
$ timeout 200 xs.py tweet 901 10 11 12 --workers 1 --http sync    -> exit 124 after 200 s; server saw 1 request
```
Nothing is logged at default verbosity. In a crawl the shared gate publishes a job-wide hold, so **every process on the job stalls**. `/healthz` turns 503 after 300 s of no progress, so a crawl under a liveness probe would at least restart. `tweet`, `user` and `watch` have no watchdog at all.
**Fix:** reject reset values more than about 1 hour (or 2 × max_wait) past now, and log them as drift. Put an absolute ceiling on the total time any single request may spend waiting at the gate. Log every gate wait over 30 s at WARNING.

### P3. The poison pill kills watch mode too (high)
`Watcher._bounded` catches only `HttpError`, `ParseError` and `ValueError` (`watch.py:403`), and `asyncio.gather` re-raises anything else. One poisoned tweet among the targets aborts the cycle before any result is recorded.
```
$ xs.py watch w.db 10 11 900 --once   -> exit 1, TypeError: unhashable type: 'list'
$ xs.py watch w.db --once             -> exit 1 again
events recorded: 0 | targets with last_run set: 0 of 3
```
Healthy targets 10 and 11 are never polled, and the watch can't recover without manually removing the poisoned target. For a monitoring product, this is a total outage caused by a single bad tweet.

### P4. Multi-process runs hide crashes and cascade the poison (high)
`cmd_crawl` calls `h.join()` on the helper processes but never reads `exitcode` (`cli.py:549-553`), and the exit status looks only at `failed` (`cli.py:570`). When a helper dies, its `finally` releases the poisoned item, and the next process claims it and dies too. Six trials with 600 seeds, one poison item and `--processes 3`:
```
trial 0: exit 1; 2 tracebacks; all processes died, parent crashed
trial 1: exit 0; 2 tracebacks; pending 1 … failed 0
trials 2-5: exit 0; 1 traceback; pending 1 … failed 0
```
In 5 of 6 runs the command reported success, printed a normal summary and left work unfinished. A scheduler or CI job would never notice.
**Fix:** exit non-zero when any helper's `exitcode != 0`, or when `pending + leased > 0` at exit without `--max-items`. Print which processes died.

### P5. Crawl coverage depends on timing (high, correctness)
`add` and `complete` insert discovered links with `INSERT OR IGNORE` (`jobs.py:304-306`), so an item's depth is whatever the **first** path to reach it said, not the shortest path. Retries, concurrency and multiple processes all change which path wins. Graph: A→C→D→E, and B quotes D, with `--depth 2 --follow all`:
```
no failure:         A 0, B 0, C 1, D 1, E 2           (E collected)
B fails once (503): A 0, B 0, C 1, D 2                (E never queued, even after B succeeds)
```
Two runs of the same job can collect different datasets, and a flaky network systematically shrinks coverage. Nothing in the output reveals it.
**Fix:** on conflict, lower the depth (`ON CONFLICT DO UPDATE SET depth = MIN(depth, excluded.depth)`). When a done item's depth drops, re-expand its stored links from the saved tweet rather than refetching it.

### P6. Raising `--depth` on a job does nothing (medium)
`JobConfig` accepts a new depth, but links are computed only when an item completes, so done items never re-expand:
```
$ crawl job.db 40 --follow parents --depth 1   -> 2 items done
$ crawl job.db --depth 3                        -> "nothing to crawl: the job has no queued items", exit 0
```
The README presents the stored settings as editable per run. Either re-expand frontier items whose depth now allows it, or reject the change with a clear message.

### P7. The JSONL sink loses events on a write failure (medium)
`observe` commits events to the `events` table, and only afterwards does `JsonlSink.send` append them (`watch.py:326-329`). Nothing tracks what reached the file. Test using `/dev/full` as a stand-in for a full disk:
```
$ watch w2.db 20 21 --events /dev/full --once   -> exit 2, "No space left on device"; 2 events in the state file
$ watch w2.db --events events.jsonl --once      -> exit 0; events.jsonl has 0 events
```
A `kill -9` between the commit and the write loses events the same way. The webhook sink has an outbox; the file sink doesn't.
**Fix:** give each sink a cursor (last seq written) stored in the state file, and replay from it on startup.

### P8. Concurrent watchers double-deliver webhooks (medium)
`WebhookSink.deliver` reads undelivered rows, POSTs them, and only then marks them delivered (`watch.py:356-368`). Nothing claims a row before sending it, and nothing stops two watchers from running on the same state file. Overlapping cron `--once` runs, a restart while the old process is still draining, or two systemd units can all cause this.
```
2 × `watch w.db --once --webhook …` (receiver takes 1.5 s)  -> webhook received 20 events, 10 distinct seqs
```
Receivers can dedupe on `seq` if they know to, but the README doesn't say so. Both watchers also poll every target, doubling request cost.
**Fix:** take an exclusive lock on the state file (a `BEGIN EXCLUSIVE` lease row or `fcntl`), refuse to start a second watcher, and document `seq` as the idempotency key.

### P9. The store is last-write-wins on the whole tweet (medium, data quality)
`TweetStore.upsert` replaces `data` wholesale (`storage.py:199-202`). The embed endpoint carries no retweet or quote counts, so a refetch by ID erases what the timeline had reported:
```
after `user nasa -o s.db`:   retweet_count 40,   quote_count 2
after `tweet 1001 -o s.db`:  retweet_count None, quote_count None
```
The same thing happens in watch mode whenever profile tweets are re-checked by ID (`--track`). The `tracked` table merges counts, but `tweets`, which is what `analyze` and `job export` read, doesn't. The `screen_name` column also isn't updated on conflict, so renamed accounts keep their old name in the index.
**Fix:** merge on upsert, keeping the old value where the new payload has `None`, and update `screen_name`.

### P10. Clock skew against X (low–medium)
The gate compares X's reset time with the local wall clock (`http.py:146`):
```
local clock  +90s vs X: next request waits    0s (correct 60s): sent straight into a 429
local clock -600s vs X: next request waits  660s (correct 60s)
```
Containers and laptops drift. **Fix:** estimate the server offset from the `Date` response header and apply it.

---

## 2. What the test suite does and doesn't protect

| Check | Result |
|---|---|
| `python -m pytest` with branch coverage | 221 passed, **92%** line+branch |
| 5 runs in random order (`pytest-randomly`) | all green; no hidden order dependence |
| 15 repeats of the aio/shared/metrics/watch/crawl tests with every core busy | 825/825 passed; timing tests are robust |
| CI on the branch head `7009594` | green (3.10 and 3.12, WASM built from source, with and without extras) |
| Wheel from `python -m build`, installed in a clean venv | installs; the `.wasm` ships; the CLI and the pure-Python fallback work |

**Mutation test** (`review/principal-qa/mutants.py`). I planted 16 realistic single-line bugs, one at a time:

| Survived: no test failed | Killed |
|---|---|
| Lease renewal / heartbeat removed entirely | Claim steals live leases |
| `next_ready_in` ignores expired leases | Off-by-one on max attempts |
| Retry backoff set to 0 | GCRA state not shared |
| Timeline rows overwrite leased/failed items | Hold not published on exhaustion |
| 503 `Retry-After` ignored | In-flight requests not discounted |
| `deleted` emitted for never-seen tweets | Leases not released on exit |
| | Missing counts overwrite known counts |
| | Webhook never marks delivered (**caught only by hanging**) |
| | No `restored` event; `/healthz` never fails |

The survivors cluster in exactly the parts that sell "tier 4/5": leases, retries and resume. Those are the areas where the senior review and this one found real bugs, and the suite would pass a regression in any of them.

**Gaps in the test strategy:**
1. **No fault-injection layer.** Nothing kills a process mid-crawl, sends SIGTERM, fills the disk, or feeds hostile headers. S2, P1, P2, P4 and P7 would all have been caught by one small "chaos" suite that drives the CLI against a hostile mock.
2. **No per-test timeout.** The webhook mutant made the suite hang instead of fail. On GitHub Actions a hang like that burns the default 6-hour job limit. Add `pytest-timeout` (for example 60 s) and `timeout-minutes` on the CI job.
3. **Fuzzing isn't in the suite.** The 30k-payload fuzz from the senior review lives in a scratch script. A Hypothesis property test over `parse_tweet_result` and `timeline_from_page` would keep S1 fixed. Header fuzzing (charset, reset, Retry-After) is missing entirely.
4. **Invariants are never asserted.** Tests check examples, not properties. The missing ones are: an item is never lost (`done + missing + failed = all` at a clean exit); a crawl's result set doesn't depend on fetch order or transient failures (P5); each event is delivered once per sink (P7, P8); and N processes never exceed the server budget (S3).
5. **No contract tests from real payloads** (the senior review's S7). Every fixture was written by the same author as the parser.
6. **No lint or type check in CI.** Ruff reports 14 findings (per the senior review), and nothing gates them. Python 3.13 isn't in the matrix.

## 3. Release readiness

| Area | State |
|---|---|
| Crawl correctness | ✗ S1, S2, S9, P1, P4, P5, P6 |
| Rate-limit safety | ✗ S3, P2, P10 |
| Watch / eventing | ✗ S4, S5, P3, P7, P8 |
| Data quality | ✗ P9 |
| Tests as a safety net | ✗ 6/16 mutants survive, no fault injection, no timeouts |
| CI, packaging | ✓ green, wheel installs clean |
| Versioning | ✗ still `2.0.0` after +2,968 lines and 4 new commands; no changelog |
| Live validation | ✗ never done (S7) |

**Exit criteria I'd hold the release to:**
1. S1–S4 and P1–P5 fixed. Each fix ships with a regression test that reproduces it: the scripts here convert directly.
2. A fault-injection test module (hostile mock + CLI) runs in CI, covering kill -9 and resume, SIGTERM, a poisoned payload, a poisoned header, a disk-full sink, and two watchers.
3. The 6 surviving mutants are killed. Re-running `mutants.py` gives 16/16.
4. `pytest-timeout` and a CI `timeout-minutes` are in place, plus ruff in CI.
5. One live smoke run on your own machine, with the responses saved as fixtures.
6. The version is bumped to 3.0.0 (new commands and job/state file formats), with a changelog that states the at-least-once delivery semantics and the single-watcher rule.

## 4. Suggested fix order
1. **One exception boundary** in `_work` and `_bounded`: turn any non-cancellation exception into an item error. This closes S1, P1 and P3 in one change and blunts P4.
2. **Header sanity**: cap the reset horizon (P2), correct for the server's clock offset (P10), and share the window budget (S3).
3. **Exit codes that tell the truth**: helper exit codes, leftover leases or pending items, and SIGTERM handling (P4, S2).
4. **Frontier depth relaxation** and re-expansion (P5, P6).
5. **Sink cursors and a watcher lock** (P7, P8), then upsert merging (P9).
6. **Test layers** from section 2, in parallel with the above.
