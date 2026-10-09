# Changelog

## Unreleased (branch `claude/tier45-release-gate`)

Release-gate fixes for the crawl, watch and multi-process work, from the
2026-09-29 senior review (S) and principal QA (P) reports. Every fix has a
regression test that fails on `claude/project-thread-jx0q4f`.

### Fixed
- **S1, P1, P3. Poison pills.** An unexpected error while fetching or
  parsing one item (a malformed payload, an unknown charset, a bug) is that
  item's failure. It counts toward `--max-attempts`, is parked as failed,
  and its traceback is logged. Before, it ended the crawl or every watch
  cycle, and the item blocked the job forever.
- **P2. Header hang.** An `x-rate-limit-reset` or `Retry-After` more than an
  hour away (typically milliseconds) is ignored and logged, and no request
  waits at the rate gate for longer than 30 minutes. Waits over 30 s are
  logged.
- **S2. Stranded leases.** A crawl doesn't finish while other workers hold
  leases. It waits for them, or reaps them: at once if the worker's process
  is gone on this host, otherwise once its heartbeat goes stale.
- **S2, P4. Exit codes and SIGTERM.** SIGTERM stops a crawl like Ctrl-C:
  in-flight work finishes, outcomes are flushed, leases are handed back,
  helpers are stopped too, and the exit code is 143. A crashed helper
  process, failed items, or items left queued or leased give exit 1.
- **S3. Shared rate window.** The server's window budget is one job-wide
  counter. On the benchmark mock (300 requests per 3 s, 4 processes), 429s
  fell from 359 to 76. That's partly fixed: the remaining overshoot is
  requests other processes have in flight when a window opens.
- **S4. False deletions.** An empty tweet-result body fails that attempt
  instead of meaning "deleted", so watch mode doesn't emit
  `deleted`/`restored` for a blank response. In `tweet` batches, an empty
  body is now reported as `failed: <id>: … empty response` with exit 1.
- **P5. Order-dependent coverage.** An item's depth is the shortest path
  found. When a finished item gets a shorter path, its links are
  re-expanded from the stored tweet.
- **QA re-verification follow-ups.** Links count from the depth an item has
  when it completes, not when it was claimed (P5 with the item in flight). A
  crashed helper left as a zombie is reaped instead of holding its leases
  until its heartbeat goes stale. A 200 carrying `{}`, `[]` or `null` is an
  error, not a deletion. A payload without text keeps the last known text
  instead of firing `edited`.
- **P6. `--depth`/`--follow` on an existing job.** Widening them re-expands
  items finished under the old settings.

### Changed (live contract, 2026-10-07)
- **Empty profile timeline pages.** To logged-out clients, X
  intermittently answers the profile widget with an empty shell:
  `hasResults` is true but `timeline.entries` is empty (seen once for @NASA
  on 2026-10-07; the same request got 20 entries 25 minutes later).
  `parse_timeline_page` raises the new `parse.EmptyTimelineShell` (a
  `ParseError`) for that page when no cookies were sent. `xscraper user`
  says so, suggests retrying or `--cookies` / `$XSCRAPER_COOKIES`, and
  exits 1. A crawl retries such a profile with back-off and parks it after
  `--max-attempts` (it used to be "done" with 0 tweets); watch records it
  as the target's error. Clients expose `logged_in`. An empty page sent
  with cookies, or one without `hasResults`, is still an empty result.

### Tests and CI
- `tests/test_contract_live.py`: 8 real tweet-result responses saved on
  2026-10-07 (`tests/fixtures/live/`: text, photos, video, media-only,
  quote, reply, two HTTP 200 tombstones) parse with the right id, author,
  date, text, media and reply/quote IDs. No parser bug found in them.
- `tests/test_faults.py`: fault injection (SIGTERM on a 2-process CLI crawl,
  slow fetches against short leases, exit-status table, shared window).
- `pytest-timeout` (60 s per test).
- Still to apply by hand: CI `timeout-minutes: 20` and Python 3.13 in the
  matrix (`.github/workflows/ci.yml`). The token used to push this branch
  can't change workflow files.

### Not fixed yet
- From the live payloads, left as is on purpose: `User.verified` is
  `verified or is_blue_verified`, so a paid checkmark counts as verified
  (changing it changes stored data; a separate field would be better);
  timeline retweets carry the wrapper's own counts (0 likes), not the
  original's; `edit_control` is not read (no model field for edits); a
  withheld (DMCA) video still gets a `video_url`.
- P7 (JSONL sink loses events on a failed write), P8 (two watchers
  double-deliver webhooks), P9 (store upsert erases counts), P10 (clock
  skew), S5 (edit detection), S8 (resume forgets `--workers`), S9 (late
  outcomes without a lease check), S10–S12.
- Watch still emits `deleted` after a single 404 or tombstone (no K-miss
  confirmation).
- No live validation of crawl or watch against X.
- Cost: with 4 processes and no rate limit, the per-request shared-window
  check makes a 20,000-seed crawl about 20% slower on the local mock
  (3.0 s → 3.6 s). One process: 5.5 s → 5.8 s.
