# claude.md: how Claude was set up, what it could do, and how I checked it

Optional Quest document. [agents.md](agents.md) covers the agents' roles and rules. This one covers the configuration, the tools, and the **verification workflow**: how I kept AI output from reaching the repo without being measured first.

## Setup
| | |
|---|---|
| **2026-10-06** (the fix itself) | Claude Code in a cloud session. No access to X; everything ran against local mocks. |
| **2026-10-07 to 10-10** (checks, experiment, reviews, docs) | Claude Code in VS Code on my own Linux laptop. Live X was reachable from here. |
| **Model** | Claude Opus 5.5, for the main session and every sub-agent (see the commit trailers) |
| **Project rules** | [`CLAUDE.md`](../CLAUDE.md) at the repo root, which Claude Code loads into every session. Each rule there exists because something went wrong without it (table below). |
| **Memory** | Claude Code's per-project memory held the Quest context across sessions: that it's for an AI lead role, the submission branch, and the official scoring rubric. |

## Tools Claude used, and their limits
| Tool | Used for | Boundary |
|---|---|---|
| Shell and file edits | Code, tests, `checks.py`, docs | Only inside the repo and a scratch directory |
| **Sub-agents** | The implementing agent; QA, experiment and reviewer agents | Each got a written brief, its own git worktree and its own ports. **Sub-agents never pushed.** |
| git worktrees | Isolating parallel agents, and the "before" and "agent v1" checkouts | No `git stash` (see below) |
| GitHub CLI (`gh`) | Pushing branches, reading CI and PR state, editing the PR description | Pushed only from the main session, after I approved. The token can't change workflow files, so CI-config changes are applied by hand. |
| Local mocks | All before/after numbers | Synthetic data, and labelled as such |
| Live X | One-tweet checks, one 213-request crawl, screenshots | Public, logged-out endpoints only, at 1 request per second; no cookies, proxies or workarounds |

**Sub-agent runs, for scale:**
- **10-06:** 1 implementing agent.
- **10-07:** 3 QA agents (fault injection, mutation testing, adversarial review), then a live-crawl agent and a code-update agent.
- **10-08:** 6 experiment agents, plus 2 reruns.
- **10-09:** 3 independent reviewers.

That's about 17 runs. Two of the QA agents hit the account's usage limit and stopped early. Their partial results were recovered from disk, and the missing checks were finished by hand.

## The verification workflow
I trusted nothing an agent said until something mechanical agreed with it.

1. **Measure the right code.** `checks.py` prints which `xscraper` it imported. Every run uses `PYTHONPATH=<checkout>`.
2. **Measure what the user gets, not the test count.** `checks.py` counts tweets written, good tweets lost, failures *named* and the exit code, on both engines.
3. **Every new test must fail on the base commit**, and use the real bad shape.
4. **Hidden tests.** The experiment scored agents against reference tests they never saw. The scorer was first calibrated on three known answers (`main` fails, agent v1 fails, the accepted fix passes).
5. **Repeatability.** 5 runs per version, with identical outcomes, before I quote a number.
6. **Mutation testing** for code with leases, retries or delivery semantics: plant 50 realistic bugs and require the suite to catch all 50 (on `main`; `scripts/mutants.py`).
7. **Re-measure agent claims.**
   - A QA agent's "6 mutants survive" was true for the commit it tested and stale for the branch head.
   - A live-crawl agent's run finished early, so the SIGTERM path it was meant to exercise never ran. It said so, but that was easy to miss.
8. **Compare each diff with the agent's own description.** That's how the shared-stash contamination was caught.
9. **Independent cold reviews.** Three reviewer agents scored this submission from a fresh clone and the brief only, without access to the working session. Their findings were fixed, or are listed as open.
10. **A CI gate,** so a mistake caught by hand stays caught: `tests/test_yardstick.py` fails on `main` and on any fix like agent v1's.

## Where the setup failed, and the rule that came out of it
| What happened | Rule now (in `CLAUDE.md` or the agent briefs) |
|---|---|
| The first `checks.py` measured the wrong checkout (`python -c` and an editable install) | Set `PYTHONPATH`; read the imported path |
| A green test suite hid blank tweets written with exit 0 | Judge by what's written; `checks.py` and the CI gate |
| An agent's tests used a convenient payload shape and passed for the wrong reason | Tests must use the real bad shape |
| `git stash` is shared across worktrees, and it contaminated 2 of 6 experiment runs | Never stash in a worktree; test against the base in a temporary copy |
| A benchmark script without a `__main__` guard crashed every `--processes` helper (old code hid this with exit 0) | Guard wrapper scripts; trust the new exit codes |
| `pkill -f <pattern>` killed the shell running it | Don't pattern-kill from the same shell |
| Agents hit the account usage limit mid-task | Recover partial results from disk; finish the gap by hand; don't relaunch blindly |
| The GitHub token lacked `workflow` scope | CI changes are kept as a patch and applied manually |

## What stayed mine
The decisions, and accountability for them:
- choosing the problem;
- rejecting the agent's first fix;
- approving the experiment and the CI gate;
- what gets pushed and merged;
- the Loom.

Claude drafted most of the text and code; [directive.md](directive.md) §E says exactly who did what.
