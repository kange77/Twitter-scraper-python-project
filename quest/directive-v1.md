# Directive v1: instructions handed to the AI coding agent

> Earlier version, kept for the record. This is the exact text given to the
> implementing agent (a Claude sub-agent working in an isolated git worktree)
> before any fix existed. The final version is `quest/directive.md`.

## Context
`xscraper` is a Python scraper for X's public syndication (embed) endpoints.
`xscraper tweet ID...` fetches tweets by ID in a batch, through either an
asyncio client (`xscraper/aio.py`) or a thread pool (`xscraper/scraper.py`).
Both call `tweet_from_body` (`xscraper/scraper.py`) and then
`parse_tweet_result` (`xscraper/parse.py`). Per-item failures are expected to
surface as `HttpError` or `ParseError`; the batch loops catch only those and
report the ID as failed.

Defect (reproduced on `main` @4585f8f): an embed payload whose `__typename` is
a list or dict raises `TypeError` at `parse.py:186` (`data.get("__typename") in
_UNAVAILABLE_TYPES`). The TypeError escapes the per-item handlers, so a
1,000-ID batch stops at the first bad payload: 498 good tweets are never
delivered, the bad ID is not named, the user sees a traceback, and a rerun dies
at the same place.

## Task
Make a payload the parser can't understand fail **that one ID** with a
`ParseError`, so the existing batch handling reports it and carries on.

## Boundaries
- Change only `xscraper/parse.py` and `xscraper/scraper.py` (plus tests).
- Do not touch `http.py`, `aio.py`'s HTTP client, the CLI, storage, the
  timeline (`user`) path or the rate gate.
- Do not catch bare `Exception` or `BaseException` in batch loops. Programming
  errors outside the payload must still crash loudly.
- No new dependencies. Keep the public API (`tweet_from_body`,
  `parse_tweet_result`, `ParseError`) unchanged.

## Acceptance criteria
1. `parse_tweet_result({"__typename": ["Tweet"], "id_str": "1"})` no longer
   raises TypeError; a non-string `__typename` is treated as an ordinary tweet
   type (not as "unavailable").
2. `tweet_from_body` converts any unexpected data-shape exception raised while
   parsing into `ParseError` that names the tweet ID, chaining the original.
3. In `xscraper tweet` batches (sync and async), a bad payload is reported as
   `failed: <id>: ...`, every other ID is written, exit code is 1.
4. Clean batches behave exactly as before.

## Tests required
- Unit: the poison shapes (list and dict `__typename`) in `tests/test_parse.py`.
- Unit: `tweet_from_body` wraps a shape error into `ParseError` with the ID.
- CLI: a batch with one poisoned ID in the middle completes, in both engines.
- Each new test must fail on `main`.
- `python -m pytest -q` must pass.

## Review responsibilities
- The agent: implements, runs the full suite, reports the diff and test output.
  It does not push, open PRs, or edit files outside the boundaries.
- The reviewer (the accountable human, Karimi, assisted by the thread agent):
  reviews the diff against `quest/yardstick.md`, runs `quest/checks.py` before
  and after, and rejects anything that widens the exception net beyond data
  shape errors.
