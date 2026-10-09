# Quality yardstick for the `xscraper tweet` batch flow

Written for this Quest before any code was changed. A change to the flow is
"good enough" when every line below holds. Each line names how it is checked.

| # | Property | How it is checked |
|---|---|---|
| Q1 | **One bad item never costs the rest of the batch.** A payload the parser can't read becomes a reported failure for that ID; every other ID is still fetched and written. | `quest/checks.py`: `ids_lost == 0` with poison IDs in the batch, both HTTP engines |
| Q2 | **Failures are named, not hidden.** Each failed ID appears on stderr as `failed: <id>: <reason>`, with no traceback, and the exit code is non-zero. | `checks.py`: `failures_reported == poison_ids`, `traceback == false`, `exit == 1` |
| Q3 | **Reruns converge.** Running the same batch twice gives the same result, with no new loss. | `checks.py`: `poison_*_rerun` equals `poison_*` |
| Q4 | **No change for clean input.** A batch without bad payloads produces the same output, exit code and request count as before, within timing noise. | `checks.py`: `clean_*` before vs after; full `pytest` suite |
| Q5 | **Bugs outside the payload stay loud.** Programming errors in HTTP, scheduling or output code are not turned into per-item failures. | Code review of the exception boundary; a test that a non-shape error still propagates |
| Q6 | **Every fix comes with a test that fails without it.** | Tests run against the base commit (must fail; `4585f8f` for this Quest) and the branch (must pass) |
| Q7 | **The diff stays in one flow.** Only the tweet-by-ID path changes; HTTP, rate-limit, storage and timeline code are untouched. | `git diff --stat <base> -- xscraper` (base `4585f8f` for this Quest) |
| Q8 | **Someone else can change it next.** The boundary is in one place, documented where a maintainer will look. | Handoff exercise in `handoff.md` |
