# Quest submission: start here

**Branch `quest`** holds the whole Quest: the focused fix to the `xscraper tweet` batch flow, its tests, and every document and piece of evidence. It's based on `main` before the change (`4585f8f`). The same work was merged into `main` as PR #5 on 2026-10-09, alongside unrelated crawl/watch work (PRs #6–#8) that isn't part of this Quest.

**Required items**
1. [intent.md](intent.md): why this problem.
2. [directive.md](directive.md): the final instructions, plus a results and handoff appendix that links everything else.
3. The Loom video, submitted separately.

**Reproduce in 3 minutes**
```bash
git clone https://github.com/kange77/Twitter-scraper-python-project && cd Twitter-scraper-python-project
git checkout quest && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m pytest -q                                   # 183 passed, including the yardstick gate
git worktree add ../before 4585f8f
python quest/checks.py --src ../before                # before: 498 good tweets lost, traceback
python quest/checks.py --src .                        # after: 0 lost, 3 bad IDs named, exit 1
```

**Map**
| Folder or file | What |
|---|---|
| [yardstick.md](yardstick.md), [directive-v1.md](directive-v1.md) | Quality yardstick; the first directive given to the agent |
| [review/](review/) | Code review of the rejected agent output, and the handoff demo |
| [decision-record.md](decision-record.md) | Options, decision, trade-offs |
| [handoff.md](handoff.md) | Metrics, flow map, rules, review checklist, handoff exercise |
| [results/](results/) | Before / agent-v1 / after JSON, and the 5x repeatability summary |
| [experiment/](experiment/) | Directive v1 vs final, 3 fresh agents each, scored mechanically |
| [problem.md](problem.md), [agents.md](agents.md), [claude.md](claude.md), [memory.md](memory.md) | Optional: the problem and its evidence; agent roles and rules; Claude setup and verification workflow; lasting facts and gotchas |
| [`../CLAUDE.md`](../CLAUDE.md) | The project rules Claude Code loads in every session |
| [prior-work/](prior-work/) | Pre-existing 2026-09-29 reviews and their scripts (unchanged) |
| [`tests/test_yardstick.py`](../tests/test_yardstick.py) | The yardstick as a CI gate |
