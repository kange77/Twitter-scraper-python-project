# CLAUDE.md

Instructions for Claude Code working in this repository. They come from mistakes that actually happened here; see `quest/claude.md` for the reasons.

## Setup and the one command that must stay green
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m pytest -q          # includes tests/test_yardstick.py (the tweet-batch quality gate)
```

## Measure the code you think you're measuring
- Run with `PYTHONPATH=<checkout>`, with the working directory set to that checkout. Before trusting any result, check `python -c "import xscraper; print(xscraper.__file__)"`. An editable install of another checkout once made a "before/after" comparison measure the same code twice.
- `quest/checks.py --src <checkout>` prints which `xscraper` it imported. Read that line.
- The "before" state of the Quest change is commit `4585f8f`, not `main`, which already contains the fix.

## Rules for the tweet flow (`parse.py`, `scraper.py`)
- A new payload check raises `ParseError` from `parse.py`. Never add `except Exception` (or a bare `except`) to a batch loop.
- **Payload policy:** wrong *attribute* fields (user, entities, counts, media, dates, text) degrade to `None` or empty. A bad *identity* field (`id_str`, `__typename`, `tombstone`) fails the ID. Never store a guess, and never treat an empty response as a deletion.
- Checks that only apply to tweets fetched by ID go in `parse_tweet_result`, not `parse_tweet` (the timeline uses that too).
- Every fix needs a test that fails without it, **using the real bad shape**. A payload missing `id_str` is rejected by an older check, so a test built on it passes for the wrong reason.
- Judge a change by what it writes, not by green tests. Run `quest/checks.py` and look at the output: a fix once passed every test while writing malformed payloads as blank tweets.

## Scope of this branch
This `quest` branch is the tweet-batch fix on top of `4585f8f`. The crawl, watch and multi-process code (with `tests/test_faults.py` and `scripts/mutants.py`) is on `main`. Its rules are in `CLAUDE.md` and `docs/HANDOVER.md` on the `docs/handover` branch (not yet merged).

## Working with parallel agents
- One git worktree per agent, separate local ports, and agents don't push.
- **Never use `git stash`** in a worktree. Stashes are shared by every worktree of the repo; it contaminated 2 of 6 experiment runs once.
- Check each agent's claim against the current code before repeating it. Compare the diff with the agent's own description of it.

## Live X
- Only public, logged-out endpoints, at `--rate 1` or slower. No account pools, no CAPTCHA solving, no getting around access controls.
- Prefer the local mocks (`quest/checks.py`, `benchmarks/mock_x.py`).
- Never commit credentials, cookies or scraped datasets.
