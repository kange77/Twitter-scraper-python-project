# problem.md: the problem, who it hurts, and the evidence

Optional Quest document. [intent.md](intent.md) explains *why I chose* this problem over three others. This one is the problem on its own: who it hurts, what the evidence is, and where the edges are.

## Why this scraper exists: the problem behind the problem
**X made its data expensive.**
- Free API access ended in 2023.
- Reading tweets through the official API now costs real money. Since February 2026, new developers pay per use, at a reported **$0.005 per post read**, so a million tweets costs about **$5,000**.
- The old fixed Pro tier was **$5,000 a month**.
- Enterprise access has been reported at around **$42,000 a month** (a 2023 figure; it's now a custom contract).

For a researcher, a small team or someone like me building tools, that prices out ordinary uses: following a few accounts, archiving a thread, analysing a set of public tweets. *(Pricing as reported publicly, checked 2026-10-10. Sources disagree on some caps and dates, so treat these as orders of magnitude.)*

**So I engineered around it with what's already public.** Every website that embeds a tweet gets it from X's public **syndication (embed) endpoints**. No login, no API key, and X applies its own rate limits. xscraper reads tweets from those same endpoints, slowly (1 request per second by default) and only public data. Whether a particular use fits X's terms is the user's responsibility, as the README's Legal section says. The tool doesn't get around logins, paywalls or rate limits.

**That choice creates the engineering risk this Quest is about.** The embed endpoints are **undocumented**, and X changes their payload shape without notice. A paid API comes with a contract. A scraper on public endpoints has none, so **the scraper's reliability depends entirely on how it handles responses it doesn't expect.** When it handles them badly, one odd payload takes down a whole batch, which is the defect below.

**Why I used it for the Quest, and not my day job.** The brief wants code I own and no confidential employer material. My work is performance engineering on core digital lending at a bank, and that can't leave the bank. xscraper is mine (public, MIT), it predates the Quest, and it has the same failure modes I work on every day: batch jobs, an upstream service I don't control, rate limits, retries and partial failure. Its defects were found by two independent reviews on 2026-09-29, not planted for this exercise.

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

## Sources for the pricing (checked 2026-10-10)
- [X API Pricing 2026: Pay-Per-Use Rates, Limits, Costs (Outstand)](https://www.outstand.so/blog/x-api-pricing)
- [X (Twitter) API Pricing in 2026: All Tiers (Postproxy)](https://postproxy.dev/blog/x-api-pricing-2026/)
- [X (Twitter) API Pricing 2026: Rates, Tiers & Real Costs (Sorsa)](https://api.sorsa.io/blog/twitter-api-pricing-2026)
- [X (Twitter) API in 2026: Credit Pricing + 3 Cheaper Routes (SocialCrawl)](https://www.socialcrawl.dev/blog/x-twitter-api-2026)

These are third-party summaries, not X's own pricing page, and they disagree on some details. Confirm on X's developer site before relying on a number.
