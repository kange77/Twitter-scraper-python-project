# Loom outline (max 5 minutes)

> A script for Karimi to record in their own words. It was drafted by an AI agent; reword it so it sounds like you. Times are targets. Have a terminal open in the repo with the venv active and `../xs-main` checked out (see directive.md §B).

## 0:00–0:40 · The problem and why it ranked first
- "This is my scraper, xscraper. I scoped the Quest to one flow: `xscraper tweet` fetching IDs in a batch."
- Show the intent.md table: four real defects, all found by reviews on 2026-09-29, before the Quest.
- "A won: one malformed payload killed the batch and lost 498 of 997 good tweets, every rerun failed in the same place, and the fix is small and in one place. B is a real hang, but it's riskier to fix in the rate gate, and the evidence is weaker."
- Say what you didn't change: B, C, D, the `user` flow, the crawl branch.

## 0:40–1:40 · Demonstrate before and after
```bash
python quest/checks.py --src ../xs-main | head -40   # poison_async: ids_lost 498, traceback true
python quest/checks.py --src .          | head -40   # ids_lost 0, failures_reported 3, exit 1
```
- Point at `failure_lines`: each bad ID is named.
- "The clean batch is identical before and after. Timing differences are noise, so I don't claim a speed-up."

## 1:40–3:00 · The most important verification: rejecting the AI's fix
- Open `quest/review/code-review.md` and `results/agent-v1.json`.
- "The coding agent's version passed all 178 tests and didn't crash. But checks.py showed 1,000 rows written and 0 failures. The three bad payloads had become blank tweets, with exit 0."
- "The root cause was my directive: I told it to treat a bad `__typename` as an ordinary tweet. That could also turn a deleted tweet into a fake live one."
- Show the small diff in `parse.py` and `scraper.py` (+17 −2 lines): a bad `__typename` is now a named failure.
- Mention that the tests now use the real shape and fail on both `main` and the agent's version.

## 3:00–3:50 · Handoff and maintainability
- Open `handoff.md`: the flow diagram, the two-level payload policy and the checklist.
- "The handoff demo failed the first time: my exercise contradicted an existing test that says attribute fields degrade. That gap is now documented. Another engineer still needs to do the exercise. I'm not claiming that's been done."

## 3:50–4:50 · How I used AI, my decisions, limitations
- AI: one agent implemented from directive v1, and another drafted the measurements, review and docs.
- What I checked: I had the suite and `checks.py` re-run on my machine, then commissioned the directive experiment (v1 0/3, final 3/3) and an independent review.
- My decisions: the repo and scope, problem A over B, C and D, the scores, and rejecting blank tweets.
- Limitations: no live X, a synthetic payload, one machine, a self-performed handoff, and an AI reviewer that you then reviewed.

## 4:50–5:00 · Close
- "Next, I'd fix the rate-gate hang, which is the next thing that can stop this same batch."
