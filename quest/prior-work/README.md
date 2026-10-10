# Prior work (pre-existing, not part of the Quest change)

Everything in this folder **predates the Quest**. The two reports were added **unchanged** with the Quest documents on 2026-10-06. The scripts, the mock server and the maturity ladder were copied **unchanged** from Karimi's local project folder on 2026-10-09, so the evidence that `intent.md` relies on can be checked.

| File | What it is | Written |
|---|---|---|
| [xscraper-review.md](xscraper-review.md) | Senior review and independent QA (source of candidate A and finding 1, S1–S12) | 2026-09-29 |
| [principal-qa.md](principal-qa.md) | Principal QA review (source of candidates B and C; P1–P10) | 2026-09-29 |
| [scripts/qa/](scripts/qa/) | Reproduction scripts for the senior review (`crashone.py`, `fuzz.py`, `watchflap.py`, benchmarks) | 2026-09-29 |
| [scripts/principal-qa/](scripts/principal-qa/) | Reproduction scripts for the principal QA (`hostile_mock.py`, `mutants.py`, `store_clobber.py`, …) | 2026-09-29 |
| [scripts/mock-x/](scripts/mock-x/) | The mock X server the reviews ran against. The senior review cites it as `/mnt/project-files/mock-x/server.py` | 2026-09-27 |
| [maturity-ladder.md](maturity-ladder.md) | The scraper maturity ladder, context for the reviews' "tier" grading | 2026-09-27 |

**Running them:** they're kept as written. Several hard-code paths from the environment they were written in, such as `/tmp/claude-0/venv`, `/home/user/Twitter-scraper-python-project` and `/mnt/project-files`, and many target the crawl/watch code (`claude/project-thread-jx0q4f`), not this Quest's flow. To rerun one, point those paths at your checkout. The Quest's own, maintained check is [`quest/checks.py`](../checks.py).
