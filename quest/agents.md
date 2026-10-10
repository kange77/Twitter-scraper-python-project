# agents.md: how AI agents were used on this Quest

Optional Quest document. It records the agents' context, rules and roles, and how they worked with Karimi. It covers the Quest change only: the `xscraper tweet` batch flow on branch `quest` (merged into `main` as PR #5 on 2026-10-09).

## Roles

| Role | Who | Context it had | Allowed | Not allowed |
|---|---|---|---|---|
| **Accountable engineer** | Karimi | Everything | Chooses the repo and scope, confirms the scores, reviews the diff, re-runs checks, merges, records the Loom | — |
| **Thread agent** | Claude (Opus), in the session that worked for Karimi | Repo, the 2026-09-29 review reports, all Quest docs | Write the yardstick, both directives, `checks.py`, measurements, review, decision record, handoff docs; correct the implementing agent's output | Merge; decide policy alone; its review doesn't replace Karimi's |
| **Implementing agent** | Claude sub-agent in an isolated git worktree | [directive-v1.md](directive-v1.md) and the code, nothing else | Implement, run the suite, report the diff and test output, flag conflicts in the directive | Push, open PRs, edit outside `parse.py`, `scraper.py` and tests |

The implementing agent got only the directive on purpose, to test whether the directive was enough on its own. It ran for 2 min 51 s and produced commit `c004558`.

## Rules every agent followed
1. **Scope:** only the `tweet` flow. The rate gate, storage, CLI, the `user` flow and the crawl/watch branch are out of scope (see [intent.md](intent.md) non-goals).
2. **Exception boundary:** don't catch `Exception` or use a bare `except` in the batch loops. New payload checks raise `ParseError` from `parse.py`.
3. **Payload policy:** wrong *attribute* fields degrade to `None`. Bad *identity* fields (`id_str`, `__typename`, `tombstone`) fail the ID.
4. **Tests:**
   - Every change has a test that fails on `main`.
   - Tests use the real bad shape (for example `__typename` as a list *with* `id_str`), not a convenient one.
   - Both engines are covered (`--http async` and `--http sync`).
5. **Evidence:**
   - Run `quest/checks.py` before and after, and keep the JSON in `results/`.
   - Label data as synthetic, mark estimates as estimates, and claim nothing about team-wide impact.
6. **No network to X** from the cloud session. All numbers come from a local synthetic mock.
7. **No pushes or PRs** by the implementing agent.

## How the work flowed
| Step | Who | Output | Commit |
|---|---|---|---|
| 1. Comparison, baseline, yardstick, directive v1, check script | Thread agent | `intent.md` draft, `yardstick.md`, `directive-v1.md`, `checks.py`, `results/before.json` | `8d25838` |
| 2. Implementation from directive v1 | Implementing agent | Fix plus tests, unedited | `c004558` |
| 3. Review against the yardstick | Thread agent (review and correction); **Karimi** (the decision) | Found that "treat a bad `__typename` as an ordinary tweet" wrote blank tweets with exit 0. Karimi decided to reject it; the thread agent wrote the correction and replaced the tests with ones using the real shape | `4181442` |
| 4. Final directive, decision record, handoff note, self-performed handoff demo, Loom outline | Thread agent | The remaining `quest/` docs | `ef4a485` |
| 5. Decisions, verification, review | **Karimi** | Chose A; required the rejection of agent v1's blank tweets (10-06). Had the checks re-run on his own machine (10-07). Commissioned the directive experiment and CI gate (10-08) and three independent cold reviews (10-09). Confirmed the intent scores (10-09). The Loom is recorded separately. | `8f26c44`, `dc10852` |

## Corrections and their causes
| What went wrong | Caught by | Cause | Fix |
|---|---|---|---|
| Malformed payloads stored as blank tweets, exit 0 | `checks.py` counting *reported failures*, not just rows written | **Directive v1, not the agent:** criterion 1 told it to do this | Requirement reversed in [directive.md](directive.md); see [review/code-review.md](review/code-review.md) |
| The agent's tests passed for the wrong reason | Review | Directive v1 didn't require the real shape | Rule 4 above |
| `checks.py` measured the wrong checkout | The agent's numbers matched `main` exactly | `python -c` puts the current directory first on `sys.path` | Run from the measured checkout and print the imported path |
| The batch hung on the trigger ID for candidate B | `checks.py` timing out | Trigger ID 901 was inside the 1..1000 batch | Moved to 5000 |
| The handoff exercise contradicted an existing test | Self-performed handoff, attempt 1 | The note didn't state the payload policy | Rule 3 added to `handoff.md` |

**Lesson:** the costliest error was in the instructions, not the generated code. The agent even flagged the consequence in its report. Directives now state what the user must *receive*, not only what must stop crashing.

## Testing the directive itself (2026-10-08)
The lesson above was tested with 3 fresh agents per directive version, scored mechanically.
- **Directive v1: 0 of 3 passed.** All three flagged the conflict, then wrote blank tweets with a green suite.
- **Final directive: 3 of 3 passed.**
- All three final-directive agents flagged one more conflict in the final directive, and it's now fixed.
- A shared `git stash` contaminated 2 runs. They were caught, discarded and rerun.

See [experiment/README.md](experiment/README.md). The yardstick now runs on every CI build as `tests/test_yardstick.py`, so a v1-style fix can't pass CI.

## Briefing a new agent on this flow
Give it [handoff.md](handoff.md) (the flow diagram, rules and checklist) and [memory.md](memory.md) (facts and gotchas). Also give it a task with:
- the file boundaries;
- an acceptance criterion phrased as *observable output* (for example "the bad ID appears on stderr as `failed: <id>: …`, exit 1, nothing blank written");
- the requirement to run `checks.py` and the full suite before reporting.

Review its output against the checklist in `handoff.md` before accepting it.

## Outside this Quest
The same practices (one worktree per agent, parallel QA agents, mutation testing as the acceptance gate, and re-measuring agent claims) were used on 2026-10-07 for work outside this flow. It's in public branches [`claude/tier45-release-gate`](https://github.com/kange77/Twitter-scraper-python-project/tree/claude/tier45-release-gate) and [`claude/live-contract-update`](https://github.com/kange77/Twitter-scraper-python-project/tree/claude/live-contract-update), and none of it is part of this submission.

## Limitations
- The reviewer of the agent's work was another Claude agent, from the same system. Karimi's own review is still required.
- No second human has done the handoff exercise.
- Agent run times above are measured. Anything about human effort is an estimate.
