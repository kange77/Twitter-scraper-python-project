# handoff.md: the `xscraper tweet` batch flow

This note is for the next engineer who changes how `xscraper tweet` handles a batch of IDs. With it you should be able to understand the flow, change it safely, and prove you didn't break it, without needing me. It covers the Quest change only. For the whole system, see [docs/HANDOVER.md](https://github.com/kange77/Twitter-scraper-python-project/blob/docs/handover/docs/HANDOVER.md).

## The short version
- **What this flow promises:** a response we can't read costs **that one ID**. The ID is named on stderr, every other tweet is still written, and the exit code is 1.
- **The one place it's enforced:** `scraper.tweet_from_body`, the parse entry point both engines share. Payload checks live in `parse.py`. The batch loops don't change.
- **How you know you didn't break it:**
  - `python -m pytest -q` (183 tests), which includes the yardstick gate `tests/test_yardstick.py`;
  - `python quest/checks.py --src .`, which runs the real CLI on 1,000 IDs, 3 of them poisoned.
- **Time to get productive:** about 30–60 minutes, including setup. *That's my estimate; nobody has timed it yet.*

---

## 1. The numbers this flow has to keep
Measured locally against a **synthetic** mock: 1,000 IDs, 3 malformed (500, 700, 900), `--rate 5000 --workers 16 --retries 0`. One run per version on 2026-10-06, then 5 per version on 2026-10-09, with identical outcomes. Raw data: [before](results/before.json) · [agent v1](results/agent-v1.json) · [after](results/after.json) · [5x repeat](results/repeat-5x-2026-10-09.json).

| Metric | Before (`4585f8f`) | Agent v1 (rejected) | Now |
|---|---|---|---|
| Good tweets lost (of 997) | **498** | 0 | **0** |
| Bad IDs named on stderr | 0 (traceback) | 0 | **3** |
| Malformed payloads written as blank tweets | 0 | **3** | **0** |
| Exit code | 1 (uncaught `TypeError`) | **0** | **1** (reported failures) |
| Requests for the batch | 514–525 (dies partway) | 1,000 | 1,000 |
| Rerun | dies at the same ID | same | completes |
| Async vs sync engine | same | same | same |
| Clean 400-ID batch: written / exit / requests | 400 / 0 / 400 | 400 / 0 / 400 | 400 / 0 / 400 |
| Tests | 171 passed | 178 passed | 183 passed (178 + 5 gate tests) |

**How to read these honestly:**
- They show the behaviour changed. They say nothing about how often X sends a malformed payload. That has never been seen live: a 213-request live crawl from my machine on 2026-10-07 parsed everything cleanly.
- Wall time isn't a result here. The clean batch varies 1.4–2.4 s run to run. The poisoned batch now takes longer only because it now finishes.
- "About 8 minutes of rate budget per rerun" is arithmetic (499 requests at 1 per second), not a measurement.

## 2. How the flow works
```
cli.cmd_tweet ─┬─ async: aio.AsyncScraper.iter_tweets → _tweet_or_exc ─┐
               └─ sync:  scraper.Scraper.iter_tweets → fetch ───────────┤
                                                                        ▼
   client.get(TWEET_ENDPOINT)     → HttpError (retried, then per-ID failure) / NotFound → None
   scraper.tweet_from_body(id, body)
       empty body / not JSON     → ParseError
       parse.parse_tweet_result  → None for tombstone / unavailable types
                                 → ParseError for a non-string __typename   (identity field: fail the ID)
                                 → Tweet otherwise                          (attribute fields degrade)
       ★ shape errors raised while parsing (TypeError, ValueError, LookupError,
         AttributeError, ArithmeticError) → ParseError("tweet <id>: …"), original in __cause__
   batch loop: HttpError | ParseError → "failed: <id>: <reason>", keep going, exit 1 at the end
               anything else          → propagates and ends the run (on purpose)
```

**Where to look:**
| Need to… | File, function |
|---|---|
| add or change a payload check | `xscraper/parse.py`: `parse_tweet_result` (by-ID only) or `parse_tweet` (**also used by the timeline**) |
| change what counts as a per-ID failure | `xscraper/scraper.py`: `tweet_from_body`. Think twice (rule 2) |
| see how failures are printed and counted | `xscraper/cli.py`: `cmd_tweet` |
| prove the batch still behaves | `quest/checks.py`, `tests/test_yardstick.py` |

## 3. Rules for changing it
1. **A new payload check raises `ParseError` from `parse.py`.** Don't add `except` clauses to the batch loops.
2. **Never widen `tweet_from_body`'s `except` beyond data-shape errors, and never catch `Exception` in a loop.** Bugs in HTTP, scheduling or output code must stay loud. `test_tweet_from_body_leaves_other_errors_alone` guards this.
3. **Know the two-level payload policy.** It's the thing most likely to trip you up.
   - **Attribute fields** (`user`, `entities`, counts, media, dates, `text`) **degrade**: a wrong type becomes `None` or empty, and the tweet is kept. `test_unexpected_field_types_do_not_crash` enforces this, and it predates my change.
   - **Identity fields** (`id_str`, `__typename`, `tombstone`) decide whether the record is a live tweet at all, so a bad value **fails the ID**.
   - Never store a guess.
4. **Changes that only make sense for tweets fetched by ID go in `parse_tweet_result`, not `parse_tweet`.** The timeline also uses `parse_tweet`.
5. **Every change ships with a test that fails without it, using the real bad shape.** A payload missing `id_str` is rejected by an older check, so a test built on it passes for the wrong reason. The coding agent did exactly that once.
6. **Run `checks.py` before and after, and look at what was written, not just the counts.** That's how the blank-tweet fix was caught: it passed every test.

## 4. Review checklist (for any change to this flow)
- [ ] `python -m pytest -q` is green, including `tests/test_yardstick.py`
- [ ] `checks.py`: `ids_lost == 0`, and `failures_reported == poison_ids`, with no traceback and exit 1
- [ ] Nothing malformed is written as a real tweet. **Open the output file and check.**
- [ ] The clean batch is unchanged: same written, exit and requests
- [ ] No `except Exception` or bare `except` added; HTTP and output bugs still raise
- [ ] New tests fail on the commit you started from, and use the real bad shape
- [ ] Both engines covered (`--http async` and `--http sync`)
- [ ] The diff stays in this flow: `git diff --stat <base> -- xscraper` shows only `parse.py` and/or `scraper.py`

## 5. Handoff exercise (about 30–60 minutes)
**The bug:** an embed payload whose `id_str` isn't numeric (for example `"abc"`) is stored as a tweet with that ID and a broken URL. I checked this on 2026-10-10: `tweet_from_body` returns a `Tweet` with id `'abc'` and URL `https://x.com/nasa/status/abc`.

**Your task:** make that payload fail the ID instead, in the `tweet` flow only, following the rules and checklist above.

**Setup:**
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
git switch quest
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m pytest -q                      # expect: 183 passed
python quest/checks.py --src .           # expect poison_*: ids_lost 0, failures_reported 3
```

**Rules of the exercise:**
- Use this note and the code.
- **Don't open** `review/handoff-demo*.md` or `review/handoff-demo-attempt2.diff`; they contain the answer.
- Time yourself, and write down every place you hesitated. That's the feedback this exercise exists to collect.

**You're done when:**
- the full suite is green;
- your new test fails without your change;
- `checks.py` is unchanged;
- the batch loops are untouched.

The reference solution is small: a check in `parse_tweet_result`, plus one parametrized test.

**Then tell me:**
- how long it took;
- where you got stuck;
- what in this note was missing, wrong or confusing.

I'll fix the note and record what you found in [review/handoff-demo.md](review/handoff-demo.md).

## 6. How this note has been tested so far
- **So far, only by the AI that wrote it** ([review/handoff-demo.md](review/handoff-demo.md)).
- **Its first attempt failed an existing test.** The exercise I'd originally set contradicted the payload policy, which the note didn't mention yet. That's why rule 3 exists.
- **Its second attempt passed.**
- **Not yet done:** a person doing it cold. Until that happens, treat the 30–60 minute estimate as a guess.
