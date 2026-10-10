# directive.md: the final directive, and what happened

**Karimi** · repo [kange77/Twitter-scraper-python-project](https://github.com/kange77/Twitter-scraper-python-project) (mine, MIT) · branch [`quest`](https://github.com/kange77/Twitter-scraper-python-project/tree/quest) · PR [#5](https://github.com/kange77/Twitter-scraper-python-project/pull/5), merged into `main` 2026-10-09

**New here?** [README.md](README.md) has a 3-minute reproduction and a map of everything. Throughout, "`main`" means `main` **before** this change: commit `4585f8f`. Today's `main` already contains the fix, plus separate crawl/watch work (PRs #6–#8).

## The short version
- **What I wanted:** one unreadable response from X costs **one tweet ID**, visibly, and nothing else.
- **What I got:** in a 1,000-ID batch with 3 bad payloads, the result went from **498 good tweets lost, a traceback, and no bad ID named** to **0 lost, all 3 named, exit 1**. Clean batches are identical.
- **How:** a 17-line change in the one parse entry point both engines share.
- **The most important thing that happened** was that I rejected the coding agent's first fix. It passed every test but turned bad payloads into **blank tweets with exit 0**. The cause was my first directive, not the agent. I then tested that claim: given the old directive, **fresh agents failed 3 out of 3** times; given this one, **they passed 3 out of 3**.

This file is the **final** directive, developed from [intent.md](intent.md). The one the agent actually worked from is [directive-v1.md](directive-v1.md), and "What I changed from v1" explains the difference.

---

## 1. Objective
In `xscraper tweet` batches, a response the parser can't read must cost **that one ID**, visibly, and nothing else. Every other ID is still fetched and written, the bad ID is named on stderr, and the exit code says something failed.

## 2. Context you need
- **The path:** `cli.cmd_tweet` runs the batch on the async engine (`aio.AsyncScraper.iter_tweets`) or the thread pool (`scraper.Scraper.iter_tweets`). Both call `scraper.tweet_from_body`, which calls `parse.parse_tweet_result`.
- **Error contract:** per-ID failures travel as `HttpError` or `ParseError`. Anything else ends the batch, on purpose, so bugs stay loud.
- **The defect:** on `main`, `{"__typename": ["Tweet"], "id_str": "…"}` raises `TypeError` at `parse.py:186` (`… in _UNAVAILABLE_TYPES`). The 2026-09-29 QA fuzzing found that shape ([prior-work/xscraper-review.md](prior-work/xscraper-review.md), finding 1).
- **Payload policy (predates this work; enforced by `test_unexpected_field_types_do_not_crash`):**
  - A wrong type in an **attribute** field (user, entities, counts, media, dates, text) degrades to `None` or empty, and the tweet is kept.
  - A bad **identity** field (`id_str`, `__typename`, `tombstone`) fails the ID.
  - Don't store a guess.

## 3. Scope
- **In:** `xscraper/parse.py` (`parse_tweet_result`), `xscraper/scraper.py` (`tweet_from_body`), tests, and `quest/`.
- **Out:** everything in intent.md's non-goals: the rate gate and `http.py`, storage, the CLI, the `user` timeline path, crawl and watch, the duplicated `except` tuples, and live X.

## 4. Requirements
1. A non-string `__typename` raises `ParseError`. It is **never** stored as a tweet: it might be a tombstone.
2. `tweet_from_body` converts data-shape errors raised while parsing into `ParseError("tweet <id>: …")`, chained to the original. Those are `TypeError`, `ValueError`, `LookupError`, `AttributeError` and `ArithmeticError`. Existing `ParseError`s get the ID prefix too.
3. No `except Exception`, and no new `except` in the batch loops. Errors from outside the parser still propagate.
4. The public API stays the same. No new dependencies.

## 5. Tests and checks
- **Unit tests on the real bad shape:** `__typename` as a list or dict **with** `id_str`, plus `["TweetTombstone"]`. Also cover the ID-prefix wrapping, and show that a `RuntimeError` still propagates.
- **CLI tests on both engines:** a bad ID in the middle of a batch gives `failed: <id>: …`, every good tweet is written, and the exit code is 1.
- **Every new test fails on `main`,** except regression guards labelled as such. The `RuntimeError` test passes on `main` by design.
- **`python -m pytest -q` passes,** including `tests/test_yardstick.py`.
- **`quest/checks.py` before and after:** `ids_lost == 0`, `failures_reported == poison_ids`, no traceback, and the clean batch unchanged.

## 6. Who does what
| Who | Does | Doesn't |
|---|---|---|
| **Implementing agent** (Claude sub-agent, isolated worktree) | Implements to this directive, runs the tests, reports the diff and results, flags any conflict between requirements | push, open PRs, edit outside scope, decide policy |
| **Thread agent** (Claude, working for me) | Drafts directives, measurements, review and docs; runs the checks; corrects the agent's output | merge; its review doesn't replace mine |
| **Me, Karimi** (accountable) | Chooses the problem, decides what gets rejected, re-runs the checks, merges, records the Loom | — |

## 7. Done means
Every line of [yardstick.md](yardstick.md) holds, with the evidence linked below. The PR is green on CI and reviewed by me.

**Status (2026-10-09):** CI green, merged by me. No GitHub review was recorded before the merge.

## What I changed from v1, and why
- **Requirement 1 was reversed.** v1 said a non-string `__typename` should be "treated as an ordinary tweet type". The agent did exactly that, and malformed payloads became blank tweets with exit 0. I rejected it ([review/code-review.md](review/code-review.md)). The lesson I took: write what the user must **receive**, not just what must stop crashing.
- **Tests must use the real shape.** v1 didn't say so, and the agent's CLI tests dropped `id_str` so they would pass.
- **More errors converted:** `ValueError` and `ArithmeticError`, the ID prefix on existing `ParseError`s, and the "other errors propagate" test.
- **The payload policy went into the context** after the handoff demo hit it ([review/handoff-demo.md](review/handoff-demo.md)).
- **After the 2026-10-08 experiment:** all three agents given this directive flagged that "fails on `main`" contradicts the required guard test. Requirement 5 now exempts labelled guards, and the yardstick runs in CI as `tests/test_yardstick.py`.

---

# Appendix: results and handoff

> **When and where:** the fix, the checks and the first documents were produced on 2026-10-06, in a cloud session with no access to X. Later work is dated where it appears: a live check on 2026-10-07, the experiment and CI gate on 2026-10-08, repeat runs on 2026-10-09, all on my machine. **Every number below comes from a local synthetic mock** unless it says live. None of it describes real X traffic or team-wide impact.

## A. Where everything is
| What | Link |
|---|---|
| Runnable repository | [branch `quest`](https://github.com/kange77/Twitter-scraper-python-project/tree/quest) |
| **The focused diff** (`parse.py` +6/−1, `scraper.py` +11/−1; tests +72) | [compare `8d25838...4181442`](https://github.com/kange77/Twitter-scraper-python-project/compare/8d25838...4181442), or `git diff 8d25838 4181442 -- xscraper tests` |
| Why this problem | [intent.md](intent.md) |
| Yardstick, and directive v1 as the agent got it | [yardstick.md](yardstick.md) · [directive-v1.md](directive-v1.md) |
| Before/after check script and raw results | [checks.py](checks.py) · [before](results/before.json) · [agent v1](results/agent-v1.json) · [after](results/after.json) · [5x repeat](results/repeat-5x-2026-10-09.json) |
| **Code review: the rejected AI output** | [review/code-review.md](review/code-review.md), with the agent's raw diff in [review/agent-v1.diff](review/agent-v1.diff) |
| Decision record (options and trade-offs) | [decision-record.md](decision-record.md) |
| Quality metrics, review checklist and handoff note | [handoff.md](handoff.md) |
| Handoff demonstration | [review/handoff-demo.md](review/handoff-demo.md) |
| **Directive experiment** (v1 vs final, 3 fresh agents each) | [experiment/README.md](experiment/README.md) |
| **The yardstick as a CI gate** | [tests/test_yardstick.py](https://github.com/kange77/Twitter-scraper-python-project/blob/quest/tests/test_yardstick.py) |
| Prior work (the 2026-09-29 reviews and their scripts, unchanged) | [prior-work/](prior-work/README.md) |
| Loom outline · optional notes | [loom-script.md](loom-script.md) · [agents.md](agents.md) · [memory.md](memory.md) |

**The commits tell the story in order:**
1. `8d25838`: yardstick, directive v1 and check script (thread agent).
2. `c004558`: the fix **exactly as the implementing agent wrote it**.
3. `4181442`: **my review correction**.
4. The documentation commits.

## B. Reproduce it yourself
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
python -m venv .venv && . .venv/bin/activate
git checkout quest && pip install -e ".[dev]"
python -m pytest -q                                   # 183 passed (178 + 5 yardstick gate tests)
git worktree add ../before 4585f8f                    # main before this change
python quest/checks.py --src ../before --candidates   # before: 498 lost, traceback
python quest/checks.py --src . --candidates           # after:  0 lost, 3 named failures, exit 1
```
`checks.py` starts its own mock on a free local port and runs the real CLI in a subprocess. It prints which `xscraper` it imported, so a run can't quietly measure the wrong code. That happened once during this work; see §E.

## C. Results
1,000 IDs, 3 of them malformed (500, 700, 900). Identical on `--http async` and `--http sync`.

| Check | Before (`main`) | Agent v1 (rejected) | After |
|---|---|---|---|
| Good tweets lost (of 997) | 498 | 0 | **0** |
| Bad IDs named | 0, traceback instead | 0 | **3**, e.g. `failed: 500: tweet 500: unexpected __typename in tweet-result payload: list` |
| Malformed payloads written as blank tweets | 0 | **3** | 0 |
| Exit code | 1 (crash) | **0** | 1 |
| Rerun | same crash | same | same, complete |
| Clean 400-ID batch (written / exit / requests) | 400 / 0 / 400 | same | same |
| `pytest` | 171 passed | 178 passed | **178 passed** (183 with the gate) |
| New tests failing on `main` / on agent v1 | — | — | 6 / 5 |
| Candidates B, C, D (out of scope) | hang, counts erased, flag ignored | — | unchanged, as intended |

**It's repeatable.** On 2026-10-09 I re-ran `checks.py` 5 times per version on a fresh clone: 20 poisoned batches and 10 clean batches each. Every outcome was identical every time. The only thing that varies is `main`'s request count (514–525 on async, 516–517 on sync), because requests are still in flight when it crashes.

**Measured or estimated:**
- Everything in the table is measured.
- "About 8 minutes of rate budget per rerun at `--rate 1`" is arithmetic (499 requests at 1 per second).
- "30–60 minutes for the handoff exercise" is a guess.
- How often X really sends this payload is **unknown**.

## C2. Did the directive change cause the fix? I tested it (2026-10-08)
- **Setup:** three fresh agents got directive v1, and three got this one. Each worked alone in its own worktree on `main`, and none saw the answer.
- **Scoring:** mechanical: `checks.py`, hidden reference tests and a scope check. I calibrated the scorer first on three known answers: `main`, `c004558` and `4181442`.
- **Directive v1: 0 of 3 passed** (0 of 4, counting the original run). Every one wrote the 3 bad payloads as blank tweets with exit 0, **and every one had a green test suite of its own**. All three had even flagged the contradiction in their reports, and then implemented it anyway.
- **This directive: 3 of 3 passed.**
- **What it doesn't prove:** 3 runs per arm is a small sample, and this directive states the required behaviour outright. So it shows these instructions are *sufficient*, not that an agent would find the right policy alone.
- **One incident:** two runs were contaminated, because `git stash` is shared by every worktree of a repo. I caught it by comparing each diff with its agent's own report, then threw both runs away and reran them. Details and every agent's report: [experiment/](experiment/README.md).

**The gate.** `tests/test_yardstick.py` now checks yardstick lines Q1–Q4 on every CI run. It passes here, and it fails on `main` with "Q1: a bad payload cost 498 good tweets" and on any v1-style fix with "Q2: 3 malformed payloads were written as tweets". The mistake I caught by hand can't get back in quietly.

## D. Handoff
- **For whoever changes this next:** [handoff.md](handoff.md) has the flow map, the rules, the review checklist and an exercise.
- **How it's been tested:** so far, only by the AI that wrote it ([review/handoff-demo.md](review/handoff-demo.md)). The first attempt failed an existing test. That exposed a policy the note didn't mention, so the note was fixed, and the second attempt passed.
- **Not done yet:** a person doing it cold. I'll update that file with what actually happens.

## E. How I used AI, and what I corrected
**Who did what:**
- **The implementing agent** (a Claude sub-agent) wrote commit `c004558` alone, from directive v1.
- **The thread agent** (Claude, working for me) drafted the comparison, `checks.py`, both directives, the correction `4181442` and these documents.
- **I decided:**
  - the repository and the scope;
  - **problem A over B, C and D**;
  - **to reject the agent's first fix.** Blank tweets with exit 0 are unacceptable; a payload we can't read must be a named failure.
- **I confirmed** the scores and weights in intent.md as my own.
- **I directed the verification,** from 2026-10-07 to 10-09 on my machine, through Claude Code:
  - the suite and `checks.py` re-run on my machine (the results matched);
  - a live check against X;
  - the directive experiment and the CI gate, which I approved when the thread agent proposed them;
  - three independent cold reviews of this submission against the brief.

  The session log isn't published; what it produced is in this branch.
- **I own the merge and the Loom.**

**The corrections:**
1. **The blank-tweet fix, rejected. This one matters most.** It turned a crash into silent bad data, and the tombstone check could no longer be trusted. Its root cause was directive v1, which Claude drafted and I issued, so it's mine to own. See [review/code-review.md](review/code-review.md).
2. **Tests that passed for the wrong reason.** The agent's CLI tests used a convenient shape (no `id_str`). They were replaced with the real one.
3. **The thread agent's own measurement mistake.** The first `checks.py` measured the wrong checkout, because `python -c` puts the current directory first on `sys.path`. It was caught because the "agent" numbers matched `main` exactly, which they shouldn't have. It now runs from the measured checkout and prints what it imported.
4. **A trigger ID in the wrong place.** Candidate B's trigger (901) sat inside the 1..1000 batch, so once the crash was fixed, the batch hung on it. It moved to 5000. That also showed B is the next thing that can stop this flow.

## F. Effort
- **Agent time (measured):** about 25 minutes on 2026-10-06, docs included. The implementing agent ran for 2 min 51 s of that.
- **My time:** about 6–10 hours across the week. **That's my estimate; I didn't track it.** It went on deciding, directing and checking agents, and on reading what they produced. The Loom isn't included.
- **Why the agent time is short:** the candidates and their reproductions already existed from the 2026-09-29 reviews, and AI did most of the drafting. My time went where I think a lead's should: on judgment and verification.

## G. Limits, plainly
- **The bad payload has never been seen live.** The shape came from fuzzing. From my machine on 2026-10-07, the live embed endpoint worked, and a 213-request crawl outside this Quest parsed every real response without error. How often X sends this shape is unknown.
- **Local only:** 5 runs per version on one machine. I'm not claiming team-wide or production impact.
- **No person has done the handoff yet** (§D).
- **AI reviewed AI.** The code reviewer was from the same system as the implementer. My decisions are listed in §E. A GitHub review on PR #5 is still to come.
- **Some bugs now look like data errors.** A genuine `TypeError` bug *inside the parser* shows up as a per-tweet failure, not a crash. That's the trade-off in [decision-record.md](decision-record.md), and it's what's merged. The original exception is kept in `__cause__`.
- **Still open:** B (the rate-gate hang, fixed later outside this Quest), C (store counts), D (`--format` without `-o`), and the same crash class in the `user` flow.
