# Handoff demo

This file records every attempt at the exercise in [handoff.md](../handoff.md) §5, and what each one changed in the note.

| Attempt | Who | Result |
|---|---|---|
| 1 | Claude (the agent that wrote the note) | Failed an existing test, which exposed a gap in the note |
| 2 | Claude, after fixing the note | Passed (181 tests) |
| 3 | **A person, cold** | **Not done yet.** Recorded below when it happens |

**Why attempts 1 and 2 are weak evidence:** the agent already knew the code, so they mainly test whether the *note* is complete, not whether a newcomer can follow it. They ran on a throwaway branch (`handoff-demo`, deleted afterwards). In attempts 1 and 2 below, "I" means Claude.

## Attempt 1: original exercise, "`user` not an object → fail the ID" (20:22:20–20:22:25 UTC)
- I followed the note: a check in `parse_tweet_result` and a parametrized test.
- **Result: 1 failed, 180 passed.** The failing test was the pre-existing `tests/test_parse.py::test_unexpected_field_types_do_not_crash[user-value2]`. Its docstring reads: "X changes payloads without notice; wrong types degrade, never crash."
- **What this showed:** the exercise I had written contradicted a deliberate, tested policy that the note never mentioned. My rule "when in doubt, fail the ID" was too broad: for attribute fields, the project's existing choice is to degrade. A newcomer following the note would have hit the same wall, and might have "fixed" it by deleting the old test.
- **Good news:** the existing suite caught the conflict at once. That's the safety net working.

## What I changed in the handoff material as a result
1. `handoff.md` rule 3 now spells out the two-level policy: attribute fields degrade, identity fields (`id_str`, `__typename`, `tombstone`) fail the ID.
2. `decision-record.md` explains why the `__typename` fix fails the ID while `user` doesn't.
3. The exercise was replaced with one that is consistent with the policy (non-numeric `id_str`).

## Attempt 2: revised exercise, "non-numeric `id_str` → fail the ID" (20:22:49–20:22:54 UTC)
- A 3-line check in `parse_tweet_result` after `parse_tweet`, plus a 3-case parametrized test. Diff: [handoff-demo-attempt2.diff](handoff-demo-attempt2.diff).
- **Result: 181 passed.** `tweet_from_body` raised `ParseError: tweet …: tweet-result payload has a non-numeric id: 'abc'`, which the batch reports as `failed: <id>: …`.
- I did not merge it. It's the exercise's reference answer, not part of the focused change.

## Timing caveat
The timestamps are the agent's tool runs, a few seconds each. They aren't a fair estimate of how long a person would take. A realistic guess for an engineer new to the repo is 30–60 minutes including setup. That's an **estimate, not observed**.

## Attempt 3: a person, cold
**Status: not done yet.** When it happens, this section will record who did it, how long it took (start, setup done, change found, finished), where they hesitated or got stuck, what in `handoff.md` was missing or wrong and what I changed in response, and their final `pytest` line and diff. Nothing will be written here that didn't happen.
