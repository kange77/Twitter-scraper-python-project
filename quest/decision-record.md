# Decision record: where a malformed tweet payload stops

**Status:** accepted, in this branch · **Date:** 2026-10-06 · **Decider:** Karimi (prepared with an AI agent; see `directive.md`)

## Context
`xscraper tweet` fetches IDs in a batch. Both the async and the thread-pool engines call `tweet_from_body`, which calls `parse_tweet_result`. The batch loops treat `HttpError` and `ParseError` as "this ID failed" and carry on. Any other exception ends the batch. On `main`, an embed payload whose `__typename` is a list or dict raises `TypeError`, so one bad response loses everything after it (measured: 498 of 997 good tweets lost in a 1,000-ID batch, and every rerun fails at the same place).

## Options considered
| | Option | For | Against |
|---|---|---|---|
| 1 | **Guard the one line** (`isinstance(typename, str)`) | Smallest diff | Fixes this shape only. The next unexpected shape crashes the batch again. Also has to decide what a bad `__typename` *means*. |
| 2 | **Catch `Exception` per item** in the four batch loops (`scraper.py` and `aio.py`) | Nothing can ever abort a batch | Four places to change. Hides real bugs in the HTTP client, rate gate or output code as thousands of identical per-item failures, each still spending a request. Breaks yardstick Q5. |
| 3 | **Convert data-shape errors to `ParseError` at the parse entry point** (`tweet_from_body`), plus make a bad `__typename` an explicit `ParseError` | One place, shared by both engines. Reuses the existing failure path, messages and exit codes. HTTP and scheduling bugs stay loud. | A `TypeError` caused by a bug *in the parser itself* now looks like bad data. |
| 4 | **Validate the payload with a schema** (pydantic or jsonschema) before parsing | Strongest guarantee; documents the shape | New dependency. The real shape has never been captured live, so the schema would encode guesses. Much larger diff than one flow warrants. |

## Decision
**Option 3, with option 1's guard made strict.** A non-string `__typename` raises `ParseError`; it isn't treated as a tweet. `tweet_from_body` turns `TypeError`, `ValueError`, `LookupError`, `AttributeError` and `ArithmeticError` from parsing into a `ParseError` that names the tweet ID and chains the original exception.

## How this fits the existing parser policy
The parser already had a deliberate rule, enforced by `test_unexpected_field_types_do_not_crash`: a wrong type in an *attribute* field degrades to `None` or empty and the tweet is kept. `__typename` is different. It's an *identity* field that decides whether the record is a live tweet or a tombstone, so a bad value fails the ID instead of degrading. The handoff demo made this distinction explicit (`review/handoff-demo.md`).

## Trade-offs accepted
- **Parser bugs look like data errors.** A programming bug inside the parser is reported per tweet instead of crashing. That's acceptable because the parser's whole job is reading untrusted data, the original exception is kept in `__cause__`, and a systematic bug shows up as every ID failing with the same message.
- **A bad `__typename` is a failure, not a best guess.** The tweet behind a malformed type is lost for this run even if the rest of the payload was fine. A visible failure that the user can retry beats a blank or possibly-deleted tweet stored as live. I rejected the "treat it as an ordinary tweet" variant in review; see `review/code-review.md`.
- **The timeline (`user`) path keeps its own handling.** `parse_timeline_page` already skips bad entries for `ParseError`; extending the boundary there is a separate change.

## Consequences
- Adding a new kind of payload check means raising `ParseError` inside `parse.py`. No batch loop needs to change.
- The seven duplicated `except (HttpError, ParseError…)` tuples remain. They're now less dangerous, but unifying them is still worth a later refactor.
- The rate-gate hang (intent.md candidate B) becomes the next thing that can stop a batch on this flow. `checks.py` first hit it while I was measuring the agent's version.
