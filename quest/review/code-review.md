# Code review: the agent's first fix, rejected and corrected

**Reviewed:** commit `c004558` "Fail one tweet, not the batch, on a malformed embed payload" (full diff: [agent-v1.diff](agent-v1.diff)).
**Author:** a Claude sub-agent working alone in an isolated git worktree, given only [directive-v1.md](../directive-v1.md).
**Reviewer:** the Claude thread agent working for Karimi, checking against [yardstick.md](../yardstick.md). Karimi, as the accountable engineer, still has to read and sign off on this review (see "Limitations" in `directive.md`).
**Outcome:** I rejected one behaviour and corrected it in `4181442`. I kept the rest.

## What the agent got right
- It kept to the file boundaries in the directive: `parse.py` and `scraper.py`, plus tests. No HTTP, CLI or storage changes.
- It didn't catch bare `Exception` in the batch loops, and it put the boundary in `tweet_from_body`, which both engines share.
- It showed its new tests fail on `main`, and the full suite passed (178 tests).
- It flagged a tension in its own report: "a payload like `{"__typename": ["Tweet"], "id_str": "1"}` now parses as a normal tweet, because criterion 1 requires that." That was the right thing to raise.

## Rejected: malformed payloads were stored as blank tweets
The agent's `parse.py` change treated a non-string `__typename` as "an ordinary tweet type", which is what my directive v1 asked for in acceptance criterion 1. The result:

```
$ python quest/checks.py --src <agent worktree>        # results/agent-v1.json
poison_sync: exit 0, tweets_written 1000, failures_reported 0
>>> tweet_from_body("900", b'{"__typename": ["Tweet"], "id_str": "900"}')
Tweet(id='900', text='', created_at=None, user=User(id='', screen_name='', ...), ...)
```

The batch no longer crashed, so it looked fixed. But the three malformed payloads were written to the output as tweets with empty text, no author and no date. The exit code was 0 and nothing was reported.

**Risk if I had accepted it:**
- **Silent data corruption.** Downstream analysis (sentiment, dedupe, counts per author) would include blank records with nothing marking them as bad. A crash is visible; a blank row in a 100k-row export isn't.
- **A dead tweet can turn into a fake live one.** If X ever sends a tombstone with a non-string `__typename` (for example `["TweetTombstone"]`), the agent's version would store a blank "live" tweet for a deleted one. The tombstone check silently stops working.
- **Exit code 0 says "all good"** to cron or CI when it isn't. That breaks yardstick Q2.
- **The tests hid it.** To make the CLI tests fail on `main` under criterion 1, the agent served the bad ID a payload with *no* `id_str`, so the existing "not a tweet object" check caught it. So the tests didn't exercise the shape the QA fuzzer actually found (`__typename` list *with* an `id_str`). They passed for the wrong reason.

**Root cause:** the mistake started in my directive, not the agent's code. Criterion 1 picked "treat as ordinary tweet" to avoid a crash without asking what the user receives instead. The agent followed it faithfully and flagged the consequence. I only caught it because `checks.py` counts *reported failures*, not just written rows.

## Corrected (commit `4181442`)
| Change | Why |
|---|---|
| A non-string `__typename` raises `ParseError("unexpected __typename …")` | We can't tell a tweet from a tombstone, so we fail the ID visibly rather than guess |
| `tweet_from_body` re-raises existing `ParseError`s with the tweet ID in front | Library callers get the ID in the message, not only the CLI's `failed: <ref>:` prefix |
| `ValueError` and `ArithmeticError` added to the converted shape errors (`LookupError` replaces `KeyError` and `IndexError`) | The batch loops don't catch a plain `ValueError` either; same class of escape |
| Tests use the real fuzzed shape (`__typename` list or dict **with** `id_str`), plus `["TweetTombstone"]` | They now fail on both `main` *and* the agent's version (5 failures there), so they guard against both regressions |
| New test: a `RuntimeError` from the parser still propagates | Yardstick Q5. Only data-shape errors become per-item failures |

After the correction: 1,000 IDs with 3 bad ones gives 997 written, 3 named failures, exit 1, on both engines ([results/after.json](../results/after.json)).

## Smaller points I left as they are
- **The CLI line repeats the ID** (`failed: 500: tweet 500: unexpected __typename …`). That's redundant but harmless, and the library message needs the ID. Not worth more code.
- **Some bugs now look like data errors.** A genuine programming bug *inside the parser* that raises `TypeError` will show up as a per-tweet `ParseError` instead of a crash. That's a deliberate trade-off (see [decision-record.md](../decision-record.md)): the parser's job is reading untrusted data, and the original exception stays chained in `__cause__` for debugging.
