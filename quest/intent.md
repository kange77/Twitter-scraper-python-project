# intent.md: Why this problem?

**Candidate:** Karimi · **Repository:** [kange77/Twitter-scraper-python-project](https://github.com/kange77/Twitter-scraper-python-project) (my own, MIT) · **Flow in scope:** `xscraper tweet ID...` (fetch tweets by ID in a batch)

## What pre-existed and what is new
- **Pre-existing:** the `xscraper` package (built earlier with AI help, merged as PRs #1–#4), and two QA reports written on 2026-09-29, before this Quest: `review/xscraper-review.md` (senior review) and `review/principal-qa.md` (principal QA). They are the source of the candidate problems below. The defects were not planted for this Quest; they were found by those reviews.
- **New for this Quest:** the comparison and scores below, the baseline and after measurements (`quest/checks.py`, `quest/results/`), the yardstick, the agent directives, the fix, its tests, the review record and the handoff material.

## Problems considered
All four are real, reproduced defects on `main` @4585f8f. "Measured" means `quest/checks.py --candidates` printed it against a local synthetic mock ([results/before.json](results/before.json)).

| | Problem | Flow | Baseline (measured) |
|---|---|---|---|
| **A** | One malformed embed payload aborts a whole `tweet` batch (senior review finding 1, applied to `tweet`) | `tweet` | 1,000-ID batch with 3 bad IDs: **499 written, 498 good tweets never delivered**, ~16–20 requests fetched and thrown away, bad ID not named, Python traceback, exit 1. A rerun fails in exactly the same place. Same on the async and sync engines. |
| **B** | An `x-rate-limit-reset` sent in milliseconds hangs the client forever (principal QA P2) | every network command | `tweet 901 10 11`: 1 request sent, then silent until killed at 20 s (the QA report saw the same at 200 s). |
| **C** | Re-fetching a tweet by ID erases retweet and quote counts in the SQLite store (principal QA P9) | `user` then `tweet -o store.db` | `retweet_count, quote_count` go from `40, 2` to `null, null`. |
| **D** | `--format jsonl` without `-o` is silently ignored (senior review finding 10) | `tweet`, `user` | Output is the human format, so `xscraper tweet … --format jsonl \| jq` breaks. |

## Criteria and scores
Scored 1 (poor) to 5 (best) by me. These are judgments, not measurements.

| Criterion (weight) | Meaning of 5 | A | B | C | D |
|---|---|---|---|---|---|
| User impact (40%) | Loses data or blocks the user's job | 5 | 4 | 3 | 2 |
| Likelihood with real X (20%) | Plausible on the endpoints the tool depends on | 4 | 2 | 4 | 5 |
| Operating cost removed (20%) | Removes wasted requests, reruns or manual work | 4 | 3 | 2 | 1 |
| Maintenance effort (20%) | Small, local, low-risk change that's easy to test | 5 | 2 | 3 | 5 |
| **Weighted** | | **4.6** | **3.0** | **3.0** | **3.0** |

Why each score:
- **A, impact 5:** the batch silently stops being a batch. Everything after the bad ID is lost, the user isn't told which ID was bad, and the only way through is to find and delete that ID by hand.
- **A, likelihood 4:** the tool depends on an undocumented embed endpoint whose payload shape X changes without notice, and the parser is the part most exposed to that. The QA fuzzer found 84 escapes in 30,000 mutated payloads, all from this one line. That rate describes mutated test data, not X's real traffic, which has never been sampled in this project (no live access from the cloud environment). So likelihood is a judgment.
- **A, operating cost 4:** every retry re-fetches what the failed run already had. At the default `--rate 1`, re-fetching the 499 tweets before the bad ID costs about 8 minutes of rate budget per retry. *(Estimate: 499 requests ÷ 1 request/s; not measured against X.)*
- **A, maintenance 5:** the faulty line and the shared parse entry point (`tweet_from_body`) are both used by the sync and the async engines, so one small fix covers both.
- **B:** a real hang, but it needs a server or proxy to send the wrong unit, and the only evidence is a synthetic header. The fix lives in the rate gate that every request passes through, and needs a policy decision about what horizon is sane, so it's riskier to change in a short exercise.
- **C:** silent data-quality loss, but fixing it means deciding merge semantics for every field of the store, which touches two flows.
- **D:** cheap to fix, but it's an annoyance with an obvious workaround (`-o file.jsonl`).

## Why A ranked first
It has the largest measured user impact (half the batch lost, unfinishable on rerun), it sits on the part of the system most exposed to change that X controls, and it can be fixed in one place that both engines share, with a test for each shape. It's the best ratio of risk removed to code changed.

## Affected users
People who run `xscraper tweet` over lists of IDs, especially large `-i ids.txt` batches under a low `--rate`, and anything that schedules those runs (cron, CI). They are the people who lose the most when a run stops halfway. *(No real users were interviewed; this is the tool's intended audience as described in its README.)*

## Intended value
- A bad payload costs one ID, not the rest of the batch.
- The bad ID is named on stderr, so the user can act on it.
- Reruns finish instead of failing at the same place.
- Clean batches don't change.

## Non-goals (what I will not change)
- **B, C and D.** They're recorded above with baselines for a later change.
- **The `user` (timeline) flow**, including the charset crash from principal QA P1. It's the same class of bug on another flow.
- **The crawl, watch and multi-process work** on the unmerged branch `claude/project-thread-jx0q4f`. Its poison-pill and lease bugs are a larger job.
- **Catching every exception per item.** Programming errors in HTTP, scheduling or output code must stay loud.
- **The seven duplicated `except (HttpError, ParseError…)` lists** across `scraper.py`, `aio.py` and `cli.py`. The fix makes them unnecessary to touch; unifying them is a separate refactor.
- **Live validation against X.** X hosts are blocked from this environment. Every number here comes from a local mock.
