# intent.md: why this problem?

**Karimi** · repo [kange77/Twitter-scraper-python-project](https://github.com/kange77/Twitter-scraper-python-project) (mine, MIT) · flow in scope: **`xscraper tweet ID...`** (fetch tweets by ID in a batch)

## The short version
One malformed response from X used to kill a whole `xscraper tweet` batch. In a 1,000-ID batch with 3 bad payloads, **498 good tweets were never delivered**, the bad ID wasn't named, and every rerun died in exactly the same place. I compared four real defects. This one scored highest on user impact, and it could be fixed in a single place that both HTTP engines share. So I chose it, and I deliberately left the other three alone.

## What already existed, and what I did for this Quest
- **Already existed:**
  - `xscraper` itself, which I built earlier with AI help (PRs #1–#4).
  - Two QA reviews of it from 2026-09-29, a senior review and a principal QA, kept unchanged in [prior-work/](prior-work/README.md) with their scripts.
  - **I didn't plant any defect.** All four candidates below come from those reviews.
- **New for this Quest:**
  - this comparison and its scores;
  - the before and after measurements ([checks.py](checks.py), [results/](results/));
  - the yardstick, the agent directives, the fix and its tests;
  - the code review, the decision record and the handoff material.

## The four problems I compared
All four were reproduced on `main` before the change (`4585f8f`). "Measured" means [checks.py](checks.py) printed it against a local **synthetic** mock of X ([results/before.json](results/before.json)).

| | Problem | Flow | Baseline (measured) |
|---|---|---|---|
| **A** | One malformed embed payload aborts the whole batch (senior review, finding 1) | `tweet` | 1,000 IDs, 3 bad: **499 written, 498 good tweets lost**, bad ID not named, traceback, exit 1. 15–26 requests fetched and thrown away. **Every rerun fails at the same ID.** Same on both engines. |
| **B** | An `x-rate-limit-reset` sent in milliseconds hangs the client forever (principal QA P2) | every network command | 1 request sent, then silence until killed at 20 s (QA saw the same at 200 s) |
| **C** | Re-fetching a tweet by ID erases its retweet and quote counts in the store (P9) | `user` then `tweet -o store.db` | counts go from `40, 2` to `null, null` |
| **D** | `--format jsonl` without `-o` is silently ignored (senior review, finding 10) | `tweet`, `user` | human format on stdout, so `… --format jsonl \| jq` breaks |

## How I scored them
1 is poor and 5 is best. These are **my judgments, not measurements**. Claude drafted a first pass, and I confirmed the scores and weights as my own.

| Criterion (weight) | 5 means | A | B | C | D |
|---|---|---|---|---|---|
| User impact (40%) | loses data or blocks the user's job | 5 | 4 | 3 | 2 |
| Likelihood with real X (20%) | plausible on the endpoints we depend on | 4 | 2 | 4 | 5 |
| Operating cost removed (20%) | removes wasted requests, reruns or manual work | 4 | 3 | 2 | 1 |
| Maintenance effort (20%) | small, local, low-risk, easy to test | 5 | 2 | 3 | 5 |
| **Weighted** | | **4.6** | **3.0** | **3.0** | **3.0** |

**Why A scores the way it does:**
- **Impact 5.** The batch silently stops being a batch. Everything after the bad ID is lost, nobody is told which ID caused it, and the only way through is to find and delete that ID by hand.
- **Likelihood 4.** We depend on an undocumented endpoint whose payload X changes without notice, and the parser is the most exposed part.
  - The QA fuzzer found 84 escapes in 30,000 mutated payloads, all from this one line. That's a rate for mutated test data, not for X.
  - **What's measured:** once a bad payload is in a batch, the cost recurs on every rerun. I reran it 5 times on 2026-10-09 and lost the same 498 tweets each time.
  - **What isn't:** how often X actually sends that shape. A 213-request live crawl from my machine on 2026-10-07 parsed every real response cleanly, so the frequency is **unknown**. This score is my judgment.
- **Operating cost 4.** Every retry re-fetches what the failed run already had: about **8 minutes of rate budget per retry** at the default `--rate 1`. *That's arithmetic (499 requests at 1 per second), not a measurement.*
- **Maintenance 5.** The faulty line and the shared parse entry point (`tweet_from_body`) serve both the sync and the async engines, so one small, testable fix covers both.

**Why not the others:**
- **B** is a real hang. But it needs a misbehaving server or proxy, the only evidence is a synthetic header, and the fix belongs in the rate gate every request passes through. That's too risky to change in a short exercise.
- **C** is silent data loss. Fixing it means deciding merge rules for every field in the store, across two flows.
- **D** is cheap to fix, but it's an annoyance with an obvious workaround (`-o file.jsonl`).

## Why A won
It had the largest measured damage: half the batch lost, and unfinishable on rerun. It sits in the part of the system most exposed to changes X controls. And it can be fixed in one shared place, with a test for each shape. **It removes the most risk for the least code.**

## Who it affects
- People who run `xscraper tweet` over lists of IDs, especially big `-i ids.txt` batches at a low `--rate`.
- Anything that schedules those runs, like cron or CI.

They lose the most when a run stops halfway. *I didn't interview users. This is the tool's intended audience as its README describes it.*

## What success looks like
- A bad payload costs **one ID**, not the rest of the batch.
- The bad ID is **named** on stderr, so the user can act on it.
- Reruns **finish** instead of dying in the same place.
- **Clean batches don't change** at all.

## What I deliberately did not change
- **B, C and D.** They're recorded here with baselines for later. *(B was fixed for every command afterwards, in separate release-gate work outside this Quest.)*
- **The `user` timeline flow,** including its own charset crash (P1). It's the same class of bug in another flow.
- **The crawl, watch and multi-process work.** It was unmerged at the time, and its poison-pill and lease bugs were a bigger job. It was fixed and merged separately (PRs #6–#8), and none of it is part of this submission.
- **Catching every exception per item.** Bugs in the HTTP, scheduling or output code must stay loud.
- **The seven duplicated `except (HttpError, ParseError…)` lists.** The fix makes touching them unnecessary; unifying them is a refactor for another day.
- **Live validation of the bad shape.** X was unreachable from the environment where I did the Quest, so every number here comes from a local mock. The live check I ran later is in [directive.md](directive.md) §G.
