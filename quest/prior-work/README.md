# Prior work (pre-existing, not part of the Quest change)

These two reports **predate the Quest** and are kept **unchanged**. They're the source of the four candidate problems in [intent.md](../intent.md), which shows the defects were found by independent reviews, not planted for this exercise.

| File | What it is | Written |
|---|---|---|
| [xscraper-review.md](xscraper-review.md) | Senior review and independent QA (finding 1 is this Quest's defect) | 2026-09-29 |
| [principal-qa.md](principal-qa.md) | Principal QA review (the source of candidates B and C) | 2026-09-29 |

**Their reproduction scripts aren't included.** Most of them target the crawl, watch and multi-process code, which isn't part of this flow or this branch. The Quest's defect is reproduced by the maintained check [`quest/checks.py`](../checks.py), whose before and after results are in [`results/`](../results/). The original scripts are in this repository's history (commit `b6904d2`, `quest/prior-work/scripts/`).
