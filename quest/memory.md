# memory.md: lasting facts about this flow

Optional Quest document. These are facts, decisions and gotchas that someone (a person or an AI agent) should know before changing the `xscraper tweet` batch flow. Each item is dated. Update an item when it stops being true.

## Facts
- **Flow (2026-10-06):** `cli.cmd_tweet` → `aio.AsyncScraper.iter_tweets` (async) or `scraper.Scraper.iter_tweets` (sync) → `scraper.tweet_from_body` → `parse.parse_tweet_result`. `tweet_from_body` is the one boundary both engines share.
- **Failure contract:**
  - A per-ID failure is `HttpError` or `ParseError`, printed as `failed: <id>: <reason>`, and the batch exits 1.
  - Any other exception ends the batch. That's by design, so bugs stay loud.
- **Baseline on `main` @4585f8f (measured, synthetic mock, 2026-10-06; reproduced 2026-10-07):**
  - In a 1,000-ID batch with 3 bad payloads, 498 good tweets were lost, with a traceback and exit 1.
  - After the fix: 0 lost, 3 named failures, exit 1.
  - The request count on `main` varies between 514 and 525 per run (async engine; sync is 516–517).
- **Test suite:**
  - `main`: 171 passed.
  - Quest branch: 178 passed; 183 with the yardstick gate (`tests/test_yardstick.py`, added 2026-10-08).
  - 6 of the new tests fail on `main`; 5 fail on the rejected agent version.
- **Live X:**
  - Unreachable from the cloud session where the Quest was done.
  - Reachable from Karimi's machine on 2026-10-07: `xscraper tweet 20` returned a real tweet.
  - That live check doesn't exercise the malformed shape, which is still synthetic (found by fuzzing).
  - The embed endpoint has no `retweet_count` or `quote_count`.

## Decisions (see [decision-record.md](decision-record.md))
- **2026-10-06:**
  - Shape errors are converted to `ParseError` at `tweet_from_body`, not caught in the loops.
  - A non-string `__typename` fails the ID; it's never stored as a tweet.
- **2026-10-06:** Rejected the agent v1 behaviour of turning bad payloads into blank tweets. See [review/code-review.md](review/code-review.md).
- **2026-10-06:** Accepted the trade-off that a `TypeError` from a bug inside the parser shows up as a per-tweet failure. The original exception stays in `__cause__`.
- **Payload policy, predating the Quest:**
  - Attribute fields degrade.
  - Identity fields (`id_str`, `__typename`, `tombstone`) fail the ID.
  - Enforced by `test_unexpected_field_types_do_not_crash`.

## Gotchas
- **`git stash` is shared by all worktrees of a repository.** Parallel agents in worktrees must not use it; it contaminated 2 of 6 experiment runs on 2026-10-08.
- **Measuring the wrong code:**
  - `python -c` puts the current directory first on `sys.path`, and the venv has an editable install.
  - Always run `checks.py --src <checkout>`; it runs from that checkout and prints `imported`. Check that path.
- **Trigger IDs:**
  - Candidate B's trigger ID must stay outside the batch range (it's 5000).
  - Otherwise the fixed batch hangs on the rate-gate bug that's out of scope.
- **Tests on convenient shapes pass for the wrong reason.** A payload without `id_str` is rejected by an older check. Use the real shape.
- **Timings are noise.** Wall time varies 1.4–2.4 s on a clean batch. Don't claim a speed-up or slow-down from one run.
- **Estimates, not measurements:**
  - "About 8 minutes of rate budget per rerun" is arithmetic (499 ÷ 1/s).
  - "30–60 minutes for the handoff exercise" is a guess.

## Commands
```bash
python -m pytest -q                                   # 183 passed on the Quest branch
python quest/checks.py --src ../xs-main --candidates  # before (git worktree add ../xs-main main)
python quest/checks.py --src . --candidates           # after
```

## Open items (not changed by the Quest)
- **Candidate B:** a reset sent in milliseconds hangs the client.
- **Candidate C:** refetching a tweet by ID erases its counts in the store.
- **Candidate D:** `--format jsonl` without `-o` is ignored.
- **The `user` flow:** it has the same crash class (charset).
- **The handoff exercise:** not yet done by another engineer.
- **Karimi's own time:** not yet recorded in `directive.md`.
- **Release-gate fixes for the crawl and watch commands (outside this Quest):** made on 2026-10-07 on branch `claude/tier45-release-gate`. They fix candidate B for every command. They aren't part of this submission.
