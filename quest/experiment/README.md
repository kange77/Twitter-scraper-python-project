# Directive experiment: does the revised directive change what agents deliver?

**Run:** 2026-10-08, on Karimi's machine. **Question:** the Quest claims the agent's bad first fix (blank tweets, exit 0) came from directive v1, not from the agent. Is that true, and is the final directive enough to prevent it?

## Method
- **Two arms, 3 fresh agents each** (Claude sub-agents, same model, same wrapper prompt).
  - Each agent got only its directive text: [directive-v1.txt](directive-v1.txt) or [directive-final.txt](directive-final.txt). These are the **exact files the agents read**, kept as evidence:
    - `directive-v1.txt` is [../directive-v1.md](../directive-v1.md) from "## Context" onward, word for word (the header was left off).
    - `directive-final.txt` is `directive.md` sections 1–7 **as they stood on 2026-10-08**, with links to files the agent couldn't see replaced by plain text. Today's `directive.md` has been rewritten since, so this snapshot is the version that was actually tested.
- **Isolation:**
  - Each agent had its own git worktree at `main` @4585f8f, with no `quest/` folder, no reference fix and no review.
  - They were told not to read other worktrees, branches or the web.
- **Scoring:** mechanical, by [score.py](score.py), after all runs finished. Raw output: [scores.txt](scores.txt). Every agent's full report, including the two discarded runs: [agent-reports.md](agent-reports.md).
  - `quest/checks.py`: 1,000 IDs with 3 malformed, on both engines.
  - The Quest's hidden reference tests, run against each agent's code. The agents never saw them.
  - A scope check and a catch-all `except` check.
  - **PASS** means nothing lost, nothing blank, all 3 bad IDs named, exit 1, the clean batch unchanged, the hidden tests pass, in scope, and no catch-all.
- **Calibration:** the scorer was first run on three known answers. `main` fails, the original rejected agent commit `c004558` fails, and the accepted fix `4181442` passes.

## Results (measured)
| Run | Verdict | Blank tweets written (async/sync) | Bad IDs named | Exit | Hidden tests | Agent's own suite |
|---|---|---|---|---|---|---|
| v1 run 1 | **FAIL** | 3/3 | 0/0 | 0/0 | 5 failed, 2 passed | 180 passed |
| v1 run 2 | **FAIL** | 3/3 | 0/0 | 0/0 | 5 failed, 2 passed | 180 passed |
| v1 run 3 (rerun) | **FAIL** | 3/3 | 0/0 | 0/0 | 5 failed, 2 passed | 181 passed |
| final run 1 (rerun) | **PASS** | 0/0 | 3/3 | 1/1 | 7 passed | 186 passed |
| final run 2 | **PASS** | 0/0 | 3/3 | 1/1 | 7 passed | 186 passed |
| final run 3 | **PASS** | 0/0 | 3/3 | 1/1 | 7 passed | 189 passed |

**Directive v1: 0 of 3 passed, or 0 of 4 counting the original 2026-10-06 run. Final directive: 3 of 3 passed.** No run changed files out of scope or added a catch-all `except`.

## What the agents' own reports show
- **Every v1 agent saw the problem and implemented it anyway.** All three flagged that "criterion 1 and criterion 3 pull against each other". Then each followed criterion 1 and wrote tests with a payload missing `id_str` so the suite would pass. Every v1 run had a **green suite and wrong behaviour**.
  - **Lesson:** an agent flagging a risk is not the agent refusing it. The reviewer has to read the flagged risks, and a gate has to measure what the user receives.
- **Every final-directive agent flagged the same remaining defect.** The directive requires a "RuntimeError still propagates" guard test and also says "every new test fails on `main`". A guard can't fail on `main`.
  - **Change made:** `directive.md` requirement 5 now excepts labelled regression guards.
- **Most runs noted the doubled ID** in CLI lines (`failed: 13: tweet 13: …`). It's a known, accepted redundancy (see `review/code-review.md`).

## Incident: two runs contaminated, discarded and rerun
- **What happened:** one agent used `git stash` / `git stash pop` to test against `main`. Stashes are shared by every worktree of a repository, so its pop applied another agent's stash.
- **How it was caught:** each commit was compared with the agent's own description of its change. The v1-run-3 commit contained final-directive code, and the final-run-1 commit contained v1 code.
- **What was done:**
  - Both runs were discarded unscored and rerun in fresh worktrees.
  - The reruns' wrapper added one workspace rule: never use `git stash`; test against `main` in a temporary copy.
  - The directives themselves didn't change.
- **Lesson:** worktrees isolate files, not the whole repository. Shared refs (stash, branches, config) need explicit rules when agents run in parallel.

## Cost (measured, from the agent runs)
- Wall time per agent was 73–113 s, and each used 51k–56k tokens.
- The scorer takes about 2 minutes per run, mostly `checks.py`.

## Limits
- **Small n:** 3 runs per arm, plus the original v1 run, all from one model family, on one machine.
- **Sufficiency, not cleverness:** the final directive states the required behaviour outright (requirement 1). The experiment shows the revised instructions are *sufficient*; it doesn't show agents would find the right policy alone.
- **Same author:** Karimi's side wrote the scorer and the hidden tests. They're published here so they can be checked.
- **The agents' code isn't in the repo.** Each agent's commit lived in a local worktree that was never pushed, and those worktrees have since been deleted:
  - **What remains:** [scores.txt](scores.txt) (the scorer's raw output) and [agent-reports.md](agent-reports.md) (what each agent said it did).
  - **Can't be rerun:** the six agent scores.
  - **Can be rerun:** the calibration. `main` @4585f8f, the rejected `c004558` and the accepted `4181442` are all public, and [score.py](score.py) scores them, though its paths reflect the directory layout it was run in.
