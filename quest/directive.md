# directive.md: final working instructions and results

**Candidate:** Karimi · **Repo:** [kange77/Twitter-scraper-python-project](https://github.com/kange77/Twitter-scraper-python-project) (public, MIT, my own) · **Branch:** [`claude/quest-quality-fix-r0t0ss`](https://github.com/kange77/Twitter-scraper-python-project/tree/claude/quest-quality-fix-r0t0ss) · **Draft PR:** [#5](https://github.com/kange77/Twitter-scraper-python-project/pull/5)

This is the final version of the instructions given to the AI coding agent, developed from [intent.md](intent.md). The first version, which the agent actually worked from, is [directive-v1.md](directive-v1.md). What changed between the two is under "Changes from v1" below.

---

## 1. Objective
In `xscraper tweet` batches, a response the parser can't read must cost **that one ID**, visibly, and nothing else. The other IDs are still fetched and written, the bad ID is named, and the exit code says something failed.

## 2. Context
- `cli.cmd_tweet` runs a batch on the async engine (`aio.AsyncScraper.iter_tweets`) or the thread pool (`scraper.Scraper.iter_tweets`). Both call `scraper.tweet_from_body`, which calls `parse.parse_tweet_result`.
- Per-ID failures travel as `HttpError` or `ParseError`. Any other exception ends the batch.
- On `main`, `{"__typename": ["Tweet"], "id_str": "…"}` raises `TypeError` at `parse.py:186`. That shape was found by the 2026-09-29 QA fuzzing ([prior-work/xscraper-review.md](prior-work/xscraper-review.md), finding 1).
- **Payload policy:** a wrong type in an *attribute* field degrades to `None` or empty and the tweet is kept (this predates the Quest and is enforced by `test_unexpected_field_types_do_not_crash`). A bad *identity* field (`id_str`, `__typename`, `tombstone`) fails the ID.

## 3. Scope
**In:** `xscraper/parse.py` (`parse_tweet_result`), `xscraper/scraper.py` (`tweet_from_body`), tests, and `quest/`.
**Out:** everything listed under non-goals in intent.md. That covers the rate gate and `http.py`, storage, the CLI, the `user`/timeline path, the crawl and watch branch, the duplicated `except` tuples, and live X.

## 4. Requirements
1. A non-string `__typename` raises `ParseError`. It is **never** stored as a tweet, because it might be a tombstone.
2. `tweet_from_body` converts data-shape errors (`TypeError`, `ValueError`, `LookupError`, `AttributeError`, `ArithmeticError`) raised during parsing into `ParseError("tweet <id>: …")`, and chains the original. Existing `ParseError`s get the ID prefix too.
3. No `except Exception` and no new `except` in the batch loops. Errors from outside the parser still propagate.
4. Public API unchanged. No new dependencies.

## 5. Tests and checks required
- Unit tests on the **real** bad shape (`__typename` list or dict *with* `id_str`, plus `["TweetTombstone"]`), the ID-prefix wrapping, and that a `RuntimeError` still propagates.
- CLI tests on both engines: a bad ID in the middle of a batch gives `failed: <id>: …`, every good tweet written, exit 1.
- Every new test fails on `main`, except regression guards labelled as such (the "`RuntimeError` still propagates" test passes on `main` by design). `python -m pytest -q` passes, including `tests/test_yardstick.py`.
- `quest/checks.py` before and after: `ids_lost == 0`, `failures_reported == poison_ids`, no traceback, and clean batches unchanged.

## 6. Roles and review responsibilities
| Who | Does | Does not |
|---|---|---|
| Implementing agent (Claude sub-agent, isolated worktree) | Implements to this directive, runs tests, reports the diff and test output, flags any conflict between requirements | Push, open PRs, edit outside scope, decide policy |
| Thread agent (Claude, working for Karimi) | Wrote the directives, measurements, review and docs. Ran the checks and corrected the agent's output | Merge. Its review doesn't replace Karimi's |
| **Karimi (accountable engineer)** | Reads the diff and the review, re-runs the checks, decides to merge, records the Loom, does or arranges the handoff exercise | — |

## 7. Completion criteria
All lines of [yardstick.md](yardstick.md) hold, with evidence linked in the appendix. The draft PR is green on CI and reviewed by Karimi.

## Changes from v1
- **After the 2026-10-08 experiment:**
  - Requirement 5 now excepts labelled regression guards from "fails on `main`". All three agents given the final directive flagged that conflict ([experiment/README.md](experiment/README.md)).
  - The yardstick is enforced by `tests/test_yardstick.py`.
- **Requirement 1 reversed.** v1 said a non-string `__typename` should be "treated as an ordinary tweet type". The agent did that faithfully, and malformed payloads became blank tweets with exit 0. I rejected it ([review/code-review.md](review/code-review.md)).
- **Tests must use the real shape.** v1 didn't say so, and the agent's CLI tests dropped `id_str` to pass.
- **Added** `ValueError` and `ArithmeticError` to the converted errors, the ID prefix on existing `ParseError`s, and the "other errors propagate" test.
- **Added** the payload policy to the context, after the handoff demo hit it ([review/handoff-demo.md](review/handoff-demo.md)).

---

# Appendix: results and handoff

> The fix, checks and documents were produced in one working session on 2026-10-06, in a cloud container with no access to X. Later additions are dated where they appear: the live check (2026-10-07), and the directive experiment and yardstick gate (2026-10-08, on Karimi's machine). **Every number comes from a local synthetic mock**, and none describes real X traffic or team-wide impact.

## A. Artifacts
| Artifact | Link |
|---|---|
| Runnable repository (branch) | https://github.com/kange77/Twitter-scraper-python-project/tree/claude/quest-quality-fix-r0t0ss |
| Focused diff (2 source files, +17/−2 lines; tests +72, plus the 51-line yardstick gate added 2026-10-08) | [Draft PR #5, "Files changed"](https://github.com/kange77/Twitter-scraper-python-project/pull/5/files), or `git diff main -- xscraper tests` |
| Why this problem (alternatives, scores, baseline, non-goals) | [intent.md](intent.md) |
| Quality yardstick | [yardstick.md](yardstick.md) |
| Directive v1, as given to the agent | [directive-v1.md](directive-v1.md) |
| Automated before/after check script | [checks.py](checks.py) |
| Results: before / agent v1 / after | [before.json](results/before.json) · [agent-v1.json](results/agent-v1.json) · [after.json](results/after.json) |
| Code-review example (rejected AI output) | [review/code-review.md](review/code-review.md), with the agent's raw diff in [review/agent-v1.diff](review/agent-v1.diff) |
| Decision record | [decision-record.md](decision-record.md) |
| Quality metrics, review checklist and handoff note | [handoff.md](handoff.md) |
| Handoff demonstration (self-performed) | [review/handoff-demo.md](review/handoff-demo.md) |
| Pre-existing evidence (2026-09-29, unchanged copies) | [prior-work/xscraper-review.md](prior-work/xscraper-review.md) · [prior-work/principal-qa.md](prior-work/principal-qa.md) |
| Loom outline | [loom-script.md](loom-script.md) |
| **Directive experiment** (v1 vs final, 3 fresh agents each, scored mechanically) | [experiment/README.md](experiment/README.md) |
| **Yardstick as a CI gate** (runs in the existing `pytest` job) | [tests/test_yardstick.py](https://github.com/kange77/Twitter-scraper-python-project/blob/claude/quest-quality-fix-r0t0ss/tests/test_yardstick.py) |
| Agent roles, rules and collaboration (optional) | [agents.md](agents.md) |
| Lasting facts, decisions and gotchas for the flow (optional) | [memory.md](memory.md) |

Commits on the branch, in order: `8d25838` yardstick, directive v1 and check script (thread agent) → `c004558` the fix as the **implementing agent** wrote it, unedited → `4181442` **review correction** → the docs commit after it.

## B. Reproduce
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
python -m venv .venv && . .venv/bin/activate
git checkout claude/quest-quality-fix-r0t0ss && pip install -e ".[dev]"
python -m pytest -q                                   # 183 passed (178 + 5 yardstick gate tests)
git worktree add ../xs-main main                      # the "before" code
python quest/checks.py --src ../xs-main --candidates  # before: ids_lost 498, traceback
python quest/checks.py --src . --candidates           # after:  ids_lost 0, 3 named failures
python quest/checks.py --src <dir with c004558>       # optional: the rejected agent version
```
`checks.py` starts its own mock on a free local port and runs the real CLI in a subprocess, with that checkout first on `sys.path`. It prints which `xscraper` it imported, so a run can't silently measure the wrong code. (That happened once during this work; see section E.)

## C. Checks and actual results
1,000 IDs, of which 3 are malformed (500, 700, 900). Results are identical on `--http async` and `--http sync`.

| Check | Before (main) | Agent v1 (rejected) | After |
|---|---|---|---|
| Good tweets lost (of 997) | 498 | 0 | **0** |
| Bad IDs named | 0, traceback instead | 0 | **3** (`failed: 500: tweet 500: unexpected __typename in tweet-result payload: list`) |
| Blank tweets written | 0 | **3** | 0 |
| Exit code | 1 (crash) | **0** | 1 |
| Rerun result | same crash | same | same, complete |
| Clean 400-ID batch | 400 / exit 0 / 400 req | same | same |
| `pytest` | 171 passed | 178 passed | **178 passed** (183 with the yardstick gate added 2026-10-08) |
| New tests failing on main / on agent v1 | — | — | 6 / 5 |
| Candidates B, C, D (out of scope) | hang, counts erased, flag ignored | — | unchanged, as intended |

**Repeatability (measured 2026-10-09, fresh clone).**
- `checks.py` was re-run **5 times per version**: 20 poisoned batches and 10 clean batches each. Every outcome was identical in every run:
  - **before:** 499 written, 498 lost, a traceback, exit 1;
  - **after:** 997 written, 0 lost, 3 named failures, exit 1, no traceback;
  - **clean batches:** 400 / exit 0 / 400 requests.
- Only `main`'s request count varies (514–525 on async, 516–517 on sync), because requests are still in flight when it crashes.
- Raw summary: [results/repeat-5x-2026-10-09.json](results/repeat-5x-2026-10-09.json).

**Measured vs estimated.** Every row above is measured locally: one run per version on 2026-10-06, and five per version on 2026-10-09 with identical outcomes. "About 8 minutes of rate budget saved per rerun at `--rate 1`" is arithmetic (499 requests ÷ 1/s). The 30–60 minutes for a newcomer to do the handoff exercise is a guess. How often X actually sends such payloads is **unknown**.

## C2. Did the directive change cause the fix? (experiment, 2026-10-08)
- **Directive v1: 0 of 3 fresh agents passed** (0 of 4 counting the original run). Every one wrote the 3 malformed payloads as blank tweets with exit 0, and every one had a green suite of its own.
- **Final directive: 3 of 3 passed:** 0 lost, 0 blank, 3 named, exit 1, and the hidden reference tests pass.
- **Scoring:** mechanical, by `checks.py`, the hidden tests and a scope check. The scorer was calibrated first on `main`, `c004558` and `4181442`.
- **Small n:** 3 per arm. The final directive states the required behaviour outright, so this shows the instructions are sufficient, not that agents would find the policy alone.
- **Incident:** two of the six runs were contaminated by a shared `git stash`. They were caught by comparing each diff with its agent's report, then discarded and rerun.
- Details: [experiment/README.md](experiment/README.md).

**Gate:** `tests/test_yardstick.py` runs yardstick lines Q1–Q4 on every CI run. It passes on this branch, and it fails on `main` ("Q1: a bad payload cost 498 good tweets") and on every v1-style fix ("Q2: 3 malformed payloads were written as tweets").

## D. Handoff
- Context to change the code, rules, review checklist and exercise: [handoff.md](handoff.md).
- **Observed handoff: performed by the AI agent itself, not by another engineer.** The first attempt failed an existing test, which exposed a policy the note didn't mention. I fixed the note and the exercise, and the second attempt passed (181 tests). Details: [review/handoff-demo.md](review/handoff-demo.md). No external feedback was collected.

## E. AI contribution and corrections
- **Who did what.** In this section, "I" means the thread agent (Claude).
  - **Implementing agent (Claude sub-agent):** wrote commit `c004558` alone, from directive v1.
  - **Thread agent (Claude):** drafted the comparison, `checks.py`, both directives, the correction `4181442`, and the documents.
  - **Karimi decided** (2026-10-06):
    - the repository and scope;
    - **problem A over B, C and D**;
    - **to reject agent v1's output.** Blank tweets with exit 0 are unacceptable, and a malformed payload must be a named failure.
  - **Karimi confirmed** the intent.md scores and weights as his own judgment (2026-10-09).
  - **Karimi directed verification**, shown in the session logs for 2026-10-07 to 10-09:
    - had the full suite and `checks.py` re-run on his own machine (2026-10-07; results matched);
    - had a live check against X run (2026-10-07);
    - approved and commissioned the directive experiment and the yardstick CI gate the thread agent proposed (2026-10-08);
    - commissioned an independent cold review of the submission against the brief (2026-10-09).
  - **Karimi is accountable** for the merge and the Loom.
- **Correction 1 (most important).** I rejected the agent's "treat a bad `__typename` as an ordinary tweet". It turned crashes into silent blank tweets with exit 0, and the tombstone check could no longer be trusted. The root cause was my own v1 directive. Details: [review/code-review.md](review/code-review.md).
- **Correction 2.** The agent's CLI tests used a convenient shape (no `id_str`) instead of the real one, so they passed for the wrong reason. I replaced them with the real shape.
- **Correction 3 (my own mistake).** My first `checks.py` ran `python -c` from the repo directory, which put that checkout first on `sys.path`. So the "agent" run actually measured this checkout. I caught it because the agent's numbers matched `main` exactly, which they shouldn't have. The fix: run from the measured checkout and print the imported path.
- **Correction 4 (my own mistake).** The candidate-B trigger ID (901) was inside the 1..1000 batch range, so after the crash was fixed the batch hung on it. The trigger moved to ID 5000. This also showed that B is the next thing that can stop a batch on this flow.

## F. Actual effort
- **Agent time (measured):** about 25 minutes of wall-clock in this session (20:00 to about 20:25 UTC on 2026-10-06, docs included). The implementing sub-agent ran for 2 minutes 51 seconds of that.
- **Karimi's own time:** about 6–10 hours, Karimi's estimate (not tracked): reviewing, deciding, directing and checking agents, and reading the documents. The Loom isn't included.
- The brief suggests 6–8 hours, and Karimi's time is in that range. Agent time was short because the defect candidates and their reproductions came from the pre-existing 2026-09-29 reviews, and an AI did most of the drafting. Karimi's time went into deciding, directing and checking.

## G. Limitations
- **No live validation of the fix itself.** X was unreachable from the environment where the Quest was done, so the bad payload shape is synthetic (fuzzed), not captured from X.
  - **Update 2026-10-07:** from Karimi's machine, the live embed endpoint works (`xscraper tweet 20`). A later 213-request crawl outside the Quest's scope parsed every real response without error.
  - **Still unseen live:** no malformed `__typename` turned up, so how often X sends one remains unknown.
- **Local only.** Five runs per version on one machine; no claim about team-wide or production impact.
- **The handoff wasn't done by another person.**
- **Same-author review.** The reviewer was an AI from the same system as the implementer. Karimi's human review is still required.
- **Some bugs look like data errors.** A `TypeError` from a real bug inside the parser now shows as a per-tweet failure (trade-off in [decision-record.md](decision-record.md)).
- **Still open:** candidates B (rate-gate hang), C (store clobber) and D (`--format` without `-o`) are reproduced but not fixed, and so is the same crash class in the `user` flow.
