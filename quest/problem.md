# problem.md: the problem, who it hurts, and the evidence

Optional Quest document. [intent.md](intent.md) explains *why I chose* this problem over three others. This one is the problem on its own: who it hurts, what the evidence is, and where the edges are.

## Why a Twitter scraper?
- **It had to be my own code, and it couldn't be my day job.** The brief asks for a repository I own, with no confidential employer material. I work on core digital lending at a bank, and that code, its data and its incidents can't leave the bank. xscraper is mine: public, MIT-licensed, and built before this Quest, which the brief explicitly allows.
- **It has the same failure modes I deal with at work, at a size that fits one flow.** Batch jobs, an upstream service I don't control, rate limits, retries and partial failure are what performance engineering on a lending platform is about. xscraper has all of them in a few thousand lines. The defect here is a classic one: one bad response from upstream takes down a whole batch.
- **Its problems are real and already documented.** I didn't plant a bug for the exercise. Two independent reviews on 2026-09-29 had already found and reproduced these defects, so I could compare real problems with real baselines (see [intent.md](intent.md)).
- **The dependency is honestly hostile.** X's embed endpoints are undocumented and change shape without notice. That makes "what happens when the payload isn't what we expect?" a real engineering question, not a contrived one.
- **It's safe to run and to share.** The data is public tweets. Every measurement runs against a local mock, and the few live checks used public endpoints at 1 request per second, with no login.

## The problem in one sentence
When X's embed endpoint returns a tweet payload the parser doesn't expect, `xscraper tweet` doesn't fail that one tweet. It **crashes the whole batch**, loses every tweet after it, doesn't say which ID caused it, and **dies at the same place on every rerun**.

## Who it hurts
- **People running large ID batches,** for example `xscraper tweet -i ids.txt -o out.jsonl` with thousands of IDs at a low `--rate`. They lose the most, because the work after the bad ID is thrown away.
- **Anything scheduled:** cron jobs, CI pipelines, data refreshes. A stopped run looks like a generic crash with a traceback. Nothing tells the operator which ID to remove, so the job fails again tomorrow.
- **Whoever consumes the output.** They get half a dataset and an exit code of 1, with no list of what's missing.

*I didn't interview anyone. These are the users the tool is built for, as its README describes them, and the failure modes come from running it.*

## Evidence

| What | How I know | Measured or not |
|---|---|---|
| A list or dict `__typename` crashes `parse_tweet_result` with `TypeError` | The 2026-09-29 QA fuzzer: 84 escapes in 30,000 mutated payloads, all from this one line ([prior-work/xscraper-review.md](prior-work/xscraper-review.md), finding 1) | Measured, on fuzzed data |
| In a 1,000-ID batch with 3 bad payloads, 498 of 997 good tweets are lost | `quest/checks.py` against `main` @4585f8f, both engines ([results/before.json](results/before.json)) | Measured, synthetic mock |
| The bad ID isn't named, and the run ends with a traceback | Same runs | Measured |
| Every rerun dies at the same ID | 5 repeat runs per version on 2026-10-09: identical every time ([results/repeat-5x-2026-10-09.json](results/repeat-5x-2026-10-09.json)) | Measured |
| Each rerun wastes about 8 minutes of rate budget at `--rate 1` | 499 requests ÷ 1 request per second | **Estimate** (arithmetic) |
| How often X actually sends this shape | A 213-request live crawl from my machine on 2026-10-07 parsed every real response cleanly | **Unknown.** Never seen live |

**What the evidence does and doesn't support:** the damage per occurrence is measured and severe. The *frequency* isn't. I'm treating it as a real risk because the endpoint is undocumented and X changes its payloads without notice. But I don't claim it happens often, and I don't claim any team-wide impact from a local test.

## Scope
- **In:** the `xscraper tweet` batch flow, fetching tweets by ID through `tweet_from_body` → `parse_tweet_result`, on both the async and the sync engines.
- **Out:** everything else. That includes the other three candidates (B: the rate-gate hang; C: the store erasing counts; D: `--format` without `-o`), the `user` timeline flow and its own crash class, the crawl, watch and multi-process work, and live validation of the bad shape. The full list and the reasons are in [intent.md](intent.md), under "What I deliberately did not change".

## What "solved" means
- A bad payload costs one ID.
- That ID is named on stderr.
- Every other tweet is written.
- The exit code is 1.
- Clean batches are unchanged.

All five are checked mechanically by [checks.py](checks.py) and enforced in CI by `tests/test_yardstick.py`. The results are in [directive.md](directive.md) §C.
