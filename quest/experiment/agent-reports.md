# Agent reports, as received (2026-10-08)

Each implementing agent's final report is reproduced as it was returned to the thread agent. Formatting may have been tidied, but the substance is unchanged. Scores come from [scores.txt](scores.txt), not from these reports. The two contaminated runs are kept for the record and were **not scored**.

## Directive v1

### v1 run 1: commit `904b379`, scored FAIL
> The fix is committed as **904b379** in /…/exp/v1-1 (not pushed). **Tests:** `180 passed in 3.68s` (cwd and PYTHONPATH set to the worktree, `xscraper.__file__` resolves into the worktree). With the two source files put back to `main`, all 9 new test cases fail (`9 failed, 171 passed`). **Files changed:** `xscraper/parse.py`, `xscraper/scraper.py`, `tests/test_parse.py`, `tests/test_scraper.py`, `tests/test_cli.py`, `tests/test_aio.py`.
> - `parse.py`: `parse_tweet_result` now counts `__typename` as "unavailable" only when it is a string in `_UNAVAILABLE_TYPES`. A list or dict `__typename` no longer raises TypeError; it is parsed as an ordinary tweet (criterion 1).
> - `scraper.py`: `tweet_from_body` passes an existing `ParseError` through unchanged. If parsing raises TypeError, AttributeError, KeyError, IndexError or ValueError, it re-raises it as `ParseError("tweet <id>: unexpected payload shape (...)")`, chained to the original (criterion 2). I did not change any batch loop, `aio.py`, `http.py` or the CLI, and nothing catches bare `Exception`.
> - Tests: list and dict `__typename` cases in `test_parse`; a `tweet_from_body` test that patches the parser to raise TypeError and checks the ID and the chained cause; CLI batches with a poisoned ID in the middle for both the sync and async engines, checking `failed: 13:`, that both good tweets are written, and exit code 1.
>
> **Ambiguities and risks:** (1) Criteria 1 and 3 pull against each other. After fix 1, `{"__typename": ["Tweet"], "id_str": ...}` parses successfully, so it is not reported as failed. The CLI tests therefore use `{"__typename": ["Tweet"]}` with no ID, which crashed on `main` and now ends up as `ParseError` ("not a tweet object"). (2) The set of errors that count as "data shape" is my choice; including ValueError and AttributeError means a real programming bug of those types inside the parser would be reported per ID. (3) The directive points the reviewer to `quest/yardstick.md` and `quest/checks.py`, but neither file exists in this worktree.

### v1 run 2: commit `37372ce`, scored FAIL
> I committed the fix as `37372ce` in the v1-2 worktree (not pushed). The full suite gives `180 passed in 3.76s`. With `xscraper/` reverted to main, the new tests fail: `9 failed, 172 passed`.
> - `parse.py`: `parse_tweet_result` only checks `__typename` against the unavailable types when it is a string. A list or dict `__typename` is now parsed as an ordinary tweet instead of raising TypeError (criterion 1).
> - `scraper.py`: `tweet_from_body` wraps only the `parse_tweet_result` call. It catches a new `_SHAPE_ERRORS = (TypeError, AttributeError, KeyError, IndexError)` and re-raises `ParseError("tweet <id>: unexpected payload shape (...)")` with the original chained. Anything else, including RuntimeError, still crashes.
> - Tests: list and dict `__typename`; each of the four shape errors wrapped with the ID and the cause; a 5-ID CLI batch on both engines in which ID 2 has a list `__typename` **and is written**, and ID 3 raises a shape error and is reported as `failed: 3: ...`.
>
> **Ambiguities and risks:** (1) Criteria 1 and 3 conflict. Once criterion 1 is fixed, the defect's own payload parses fine, so to exercise the failure path the CLI tests make the parser raise TypeError on a marked payload by monkeypatching it. (2) Programming errors can now be hidden. (3) I left ValueError out to keep the net narrow. (4) I dropped a "RuntimeError still crashes" test because it passed on main. (5) `quest/yardstick.md` and `quest/checks.py` aren't in my worktree.

### v1 run 3: commit `1fff2d8`, CONTAMINATED and not scored
Its report described a `_s(data.get("__typename"))` change. The commit actually contained final-directive code ("unexpected __typename", "unreadable response"), because another agent's `git stash pop` reached this worktree. Discarded; see the rerun below.

### v1 run 3 (rerun): commit `2d884a4`, scored FAIL
> The fix is committed as **2d884a4** in worktree v1-3-r. Full suite with the fix: `181 passed in 3.68s`. With main's `parse.py` and `scraper.py` restored in a temp copy, it's `10 failed, 171 passed`; the 10 failures are exactly the new tests.
> - `parse.py`: the "unavailable" check now runs only when `__typename` is a string. A list or dict `__typename` no longer raises `TypeError`; it is parsed as an ordinary tweet (criterion 1).
> - `scraper.py`: only the `parse_tweet_result(data)` call is wrapped. An existing `ParseError` is re-raised unchanged. `TypeError`, `AttributeError`, `KeyError`, `IndexError` and `ValueError` become `ParseError("tweet <id>: unexpected payload shape (...)")`, chained.
> - Tests: list and dict `__typename` with an ID (parses as a tweet) and without one (`ParseError`); `tweet_from_body` wrapping each shape error; sync and async CLI batches with a poisoned ID in the middle.
>
> **Ambiguities and risks:** (1) Criteria 1 and 3 pull against each other. After fix 1, a list `__typename` with a valid `id_str` succeeds, so the CLI poison payloads have no usable ID and fail as "not a tweet object". These tests prove the batch survives, but not the wrapping path end to end. (2) The wrapper can hide real bugs. (3) `ParseError`s from the parser itself still don't name the ID.

## Final directive

### final run 1: commit `9e3978a`, CONTAMINATED and not scored
Its report described raising `ParseError("unexpected __typename")` with a `_DATA_SHAPE_ERRORS` constant. The commit actually contained v1-run-3's `_s(...)` code, via the shared stash. Discarded; see the rerun below.

### final run 1 (rerun): commit `7b43ec0`, scored PASS
> I committed the fix as `7b43ec0` in final-1-r. A tweet-result response with a bad `__typename` now fails only that one ID: the other tweets are still written, the bad ID is named, and the exit code is 1. Full suite: `186 passed in 3.62s`.
> - `parse_tweet_result`: a `__typename` that is present and not a string raises `ParseError("unexpected __typename: <type>")`, before the tombstone check, so `["TweetTombstone"]` is never stored as a tweet.
> - `tweet_from_body`: an existing `ParseError` is re-raised as `ParseError("tweet <id>: …")`, chained, and caught first because `ParseError` subclasses `ValueError`. `TypeError`, `ValueError`, `LookupError`, `AttributeError` and `ArithmeticError` become `ParseError("tweet <id>: unexpected payload shape (<Type>: …)")`, chained. Anything else, such as a `RuntimeError`, still propagates. The batch loops and the CLI are untouched, and there is no `except Exception`.
> - Tests: list, dict, `["TweetTombstone"]` and int with `id_str`; ID prefix, wrapping and `RuntimeError` propagation; CLI on both engines checking `failed: 13: tweet 13: unexpected __typename`, both good tweets in the `-o` file, and exit 1. In a temp copy with main's sources, 14 of the 15 new tests fail.
>
> **Things to flag:** (1) The `RuntimeError` propagation test passes on main, which conflicts with "every new test fails on main". I kept it as a separate guard. (2) `__typename: null` is treated like a missing field; this is a policy call. (3) CLI error lines repeat the ID. (4) intent.md and `quest/` aren't in the worktree.

### final run 2: commit `41b920d`, scored PASS
> Commit: 41b920d. `186 passed in 3.55s`.
> - `parse_tweet_result`: if `__typename` is present but is not a string, it raises `ParseError("unexpected __typename: <type>")`. This covers lists and dicts, including `["TweetTombstone"]`, so a bad typename is never stored as a tweet. Attribute fields still degrade as before.
> - `tweet_from_body`: a `ParseError` is re-raised as `ParseError("tweet <id>: …")`. TypeError, ValueError, LookupError, AttributeError and ArithmeticError become `ParseError("tweet <id>: unreadable response (Type: msg)")`, both chained. Anything else still propagates.
> - Tests: the real bad shapes with `id_str`, wrapping of each error class with chaining, RuntimeError still propagating, and one CLI test per engine checking `failed: 123: tweet 123: …`, both good tweets, exit 1 and no traceback. Against main's source, 14 of the 15 new tests fail.
>
> **Conflicts and risks:** (1) "Every new test fails on main" can't hold for the required "RuntimeError still propagates" test. (2) `quest/` isn't in this worktree. (3) The "not JSON" ParseError already has the ID prefix, so it isn't prefixed twice. (4) **Workspace incident:** I ran `git stash` / `git stash pop` to test against main. Stash refs are shared across worktrees, and the pop restored another worktree's stashed `xscraper/` changes. I put that content back and reapplied my own edits. Treat the shared stash as possibly contaminated.

### final run 3: commit `06ebe20`, scored PASS
> Commit: 06ebe20. `189 passed in 4.16s`.
> - `parse_tweet_result`: a `__typename` that is set but isn't a string raises `ParseError("unexpected __typename: list")`, before the tombstone check (req 1). Following the identity-field policy in section 2, an `id_str` that is not a string or int also raises `ParseError`.
> - `tweet_from_body`: any `ParseError` is re-raised as `ParseError("tweet <id>: …")`, chained; `TypeError`, `ValueError`, `LookupError`, `AttributeError` and `ArithmeticError` become `ParseError("tweet <id>: unreadable payload (Type: msg)")`, chained. Any other exception still propagates. There's no `except Exception`.
> - Tests: `__typename` as `["Tweet"]`, `["TweetTombstone"]`, a dict or an int, each with `id_str`; bad `id_str` types; ID prefix, wrapping, `RuntimeError` propagating; sync CLI with workers 1 and 4, plus an async CLI test.
>
> **Conflicts, ambiguities and risks:** (1) The RuntimeError-propagates test passes on `main`. The other 17 new tests fail on main. (2) `quest/` isn't in this worktree. (3) The `id_str` check goes slightly beyond the numbered requirements, from the section 2 policy. A non-dict `tombstone` value still returns None. (4) The ID shows twice in CLI output.
