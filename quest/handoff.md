# Quality metrics and handoff note: `xscraper tweet` batch flow

## Metrics (measured locally, synthetic mock, one machine)
From `python quest/checks.py --src <checkout>` with 1,000 IDs, 3 of them malformed (IDs 500, 700, 900), `--rate 5000 --workers 16 --retries 0`, on Python 3.13 in a 4-core cloud container. Raw output: [before.json](results/before.json), [after.json](results/after.json), [agent-v1.json](results/agent-v1.json).

| Metric | main @4585f8f | Agent v1 (rejected) | This branch |
|---|---|---|---|
| Good tweets lost (of 997) | **498** | 0 | **0** |
| Bad IDs named on stderr | 0 (traceback) | 0 | **3** |
| Malformed payloads written as blank tweets | 0 | **3** | 0 |
| Exit code | 1 (uncaught `TypeError`) | **0** | 1 (reported failures) |
| Requests sent for the batch | 514–525 across runs | 1,000 | 1,000 |
| Rerun gives the same result | yes (dies at the same ID) | yes | yes |
| Same results, async and sync engines | yes | yes | yes |
| Clean 400-ID batch: written / exit / requests | 400 / 0 / 400 | 400 / 0 / 400 | 400 / 0 / 400 |
| Test suite | 171 passed | 178 passed | 178 passed |
| New tests that fail on `main` / on agent v1 | n/a | n/a | 6 / 5 |

**Reading these honestly**
- These are counts from local runs against a synthetic mock: one run per version on 2026-10-06, then five per version on 2026-10-09 with identical outcomes (only `main`'s request count varies). They show the behaviour changed. They don't say how often X sends a malformed payload, which hasn't been measured; X hosts are blocked here.
- Wall time isn't a result. The clean batch took 1.4–2.4 s in both versions, and that spread is run-to-run noise on this container. The poisoned batch takes longer after the fix only because the run now finishes (3.2 s vs 1.9 s).
- "About 8 minutes of rate budget saved per rerun at `--rate 1`" (intent.md) is arithmetic, not a measurement.
- The candidate baselines (B: 20 s timeout with 1 request, C: counts `40, 2` → `null, null`, D: human format on stdout) are unchanged after the fix, as intended. They're out of scope.

## How the flow works (enough to change it without me)
```
cli.cmd_tweet ─┬─ async: aio.AsyncScraper.iter_tweets → _tweet_or_exc ─┐
               └─ sync:  scraper.Scraper.iter_tweets → fetch ───────────┤
                                                                        ▼
            client.get(TWEET_ENDPOINT)   → HttpError / NotFound (None)
            scraper.tweet_from_body      → JSON decode → parse_tweet_result
                                           ★ the boundary: shape errors → ParseError("tweet <id>: …")
            per-item: HttpError | ParseError → "failed: <id>: <reason>", exit 1
            anything else                → propagates, ends the run (by design)
```
**Rules for changing it:**
1. Want a new payload check? Raise `ParseError` from `xscraper/parse.py`. Don't add `except` clauses to the batch loops.
2. Never widen `tweet_from_body`'s `except` beyond data-shape errors, and never catch `Exception` in a loop. `tests/test_scraper.py::test_tweet_from_body_leaves_other_errors_alone` guards this.
3. **Know the two-level payload policy.** *Attribute* fields (`user`, `entities`, counts, media, dates, `text`…) **degrade**: a wrong type becomes `None` or empty and the tweet is kept. `tests/test_parse.py::test_unexpected_field_types_do_not_crash` enforces this, and it predates the Quest. *Identity* fields (`id_str`, `__typename`, `tombstone`) decide whether the record is a live tweet at all, so a bad value **fails the ID** with `ParseError`. Don't store a guess for those (see `review/code-review.md`).
4. Every change needs a test that fails without it, and `quest/checks.py` before and after.

## Review checklist (for any change to this flow)
- [ ] A bad item fails only itself: `checks.py` shows `ids_lost == 0`
- [ ] Every failure is named: `failures_reported == poison_ids`, no traceback, exit 1
- [ ] Nothing malformed is written as a real tweet (look at the output, not just the count)
- [ ] Clean batch unchanged: same written, exit and requests
- [ ] No `except Exception` or bare `except` added; HTTP and output bugs still raise
- [ ] New tests fail on the base branch, and use the real bad shape, not a convenient one
- [ ] Both engines covered (`--http async` and `--http sync`)
- [ ] Diff stays in the flow (`git diff --stat` against the commit you started from)

## Handoff exercise
**Task:** an embed payload whose `id_str` isn't numeric (for example `"abc"`) is currently stored as a tweet with that ID and a broken URL. Make it fail that ID instead, in the `tweet` flow only, following the rules and checklist above.

**Expected solution shape:** a check in `parse_tweet_result` (not in `parse_tweet`, which the timeline also uses), one parametrized test, the full suite green, and no change to the batch loops. Roughly 5 lines of code plus a test.

**Setup:**
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
git checkout claude/quest-quality-fix-r0t0ss
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m pytest -q                       # expect: 183 passed (includes the yardstick gate)
python quest/checks.py --src .            # expect: poison_* ids_lost 0, failures_reported 3
```

**Observed result: demonstrated by the AI agent itself, not by another engineer.** No second engineer was available, so the thread agent that wrote this note performed the exercise from this note alone, on a throwaway branch. That's a weak test of the handoff, because the author already knows the code. See [handoff-demo.md](review/handoff-demo.md) for what happened. Karimi, or another engineer, doing the exercise cold and recording where they got stuck is the real test, and it hasn't happened yet.
