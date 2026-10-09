"""Score one implementing-agent run against the Quest yardstick, mechanically.

usage: python score.py <worktree> [<worktree> ...]  -> one JSON object per run, then a table

For each worktree (an agent's commit on top of main @4585f8f):
  * own suite      - the agent's own `pytest -q` result
  * hidden tests   - the reference regression tests from claude/quest-quality-fix-r0t0ss,
                     run against the agent's code (the agent never saw them)
  * checks.py      - quest/checks.py from the Quest branch: 1,000 IDs, 3 malformed, both engines
  * scope          - files changed outside parse.py / scraper.py / tests / quest
  * catch-all      - `except Exception` or bare `except` added
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = "4585f8f"
REF = "claude/quest-quality-fix-r0t0ss"
HERE = os.path.dirname(os.path.abspath(__file__))  # as run: exp/ beside xs/ (repo clone) and quest-wt/ (this branch)
PY = os.path.join(HERE, "..", "xs", ".venv", "bin", "python")
REPO = os.path.join(HERE, "..", "xs")
CHECKS = os.path.join(HERE, "..", "quest-wt", "quest", "checks.py")
HIDDEN_FILES = ["tests/test_parse.py", "tests/test_scraper.py", "tests/test_cli.py", "tests/test_aio.py"]
HIDDEN = ["test_non_string_typename_is_parse_error", "test_tweet_from_body_wraps_shape_errors",
          "test_tweet_from_body_leaves_other_errors_alone", "test_tweet_batch_survives_poisoned_payload",
          "test_cli_async_batch_survives_poisoned_payload"]
ALLOWED = re.compile(r"^(xscraper/parse\.py|xscraper/scraper\.py|tests/.*|quest/.*)$")


def sh(args, cwd, env=None, timeout=600):
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def pytest_line(out):
    lines = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed)", l)]
    return lines[-1].strip("= ") if lines else "no result"


def score(wt):
    wt = os.path.abspath(wt)
    env = dict(os.environ, PYTHONPATH=wt)
    res = {"run": os.path.basename(wt)}
    _, log = sh(["git", "log", "--format=%h %s", f"{BASE}..HEAD"], wt)
    res["commits"] = log.strip().splitlines()
    _, names = sh(["git", "diff", "--name-only", BASE], wt)
    files = [f for f in names.split() if f]
    res["files"] = files
    res["out_of_scope"] = [f for f in files if not ALLOWED.match(f)]
    _, diff = sh(["git", "diff", BASE, "--", "xscraper"], wt)
    res["catch_all_added"] = bool(re.search(r"^\+.*except\s*(Exception|BaseException)?\s*(as \w+)?:", diff, re.M))
    _, out = sh([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], wt, env)
    res["own_suite"] = pytest_line(out)
    # Hidden reference tests, run in a throwaway copy so the agent's tree is untouched.
    tmp = tempfile.mkdtemp(prefix="hidden-")
    try:
        shutil.copytree(wt, os.path.join(tmp, "w"), ignore=shutil.ignore_patterns(".git", "__pycache__"))
        w = os.path.join(tmp, "w")
        for f in HIDDEN_FILES:
            _, src = sh(["git", "show", f"{REF}:{f}"], REPO)
            open(os.path.join(w, f), "w").write(src)
        _, out = sh([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", " or ".join(HIDDEN)] + HIDDEN_FILES,
                    w, dict(os.environ, PYTHONPATH=w))
        res["hidden_tests"] = pytest_line(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _, out = sh([PY, CHECKS, "--src", wt], HERE, timeout=900)
    try:
        rep = json.loads(out[out.index("{"):])
    except ValueError:
        res["checks"] = "checks.py failed: " + out[-300:]
        return res
    for eng in ("async", "sync"):
        p, c = rep[f"poison_{eng}"], rep[f"clean_{eng}"]
        res[eng] = {"written": p["tweets_written"], "ids_lost": p["ids_lost"],
                    "blank_written": max(0, p["tweets_written"] - (p["ids"] - p["poison_ids"])),
                    "named_failures": p["failures_reported"], "exit": p["exit"], "traceback": p["traceback"],
                    "clean": f'{c["tweets_written"]}/{c["exit"]}/{c["requests"]}'}
    return res


def verdict(r):
    """Meets the yardstick's mechanical lines: nothing lost, nothing blank, every bad ID named, exit 1."""
    if "async" not in r:
        return "NO RESULT"
    ok = all(r[e]["ids_lost"] == 0 and r[e]["blank_written"] == 0 and r[e]["named_failures"] == 3
             and r[e]["exit"] == 1 and not r[e]["traceback"] and r[e]["clean"] == "400/0/400"
             for e in ("async", "sync"))
    ok = ok and not r["out_of_scope"] and not r["catch_all_added"] and "passed" in r["hidden_tests"] \
        and "failed" not in r["hidden_tests"]
    return "PASS" if ok else "FAIL"


if __name__ == "__main__":
    rows = []
    for wt in sys.argv[1:]:
        r = score(wt)
        r["verdict"] = verdict(r)
        print(json.dumps(r), flush=True)
        rows.append(r)
    print()
    print("| run | verdict | blank tweets written (async/sync) | bad IDs named | exit | lost | hidden tests | own suite | out of scope | catch-all |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "async" not in r:
            print(f"| {r['run']} | {r['verdict']} | | | | | | {r.get('own_suite')} | | |")
            continue
        a, s = r["async"], r["sync"]
        print(f"| {r['run']} | {r['verdict']} | {a['blank_written']}/{s['blank_written']} | "
              f"{a['named_failures']}/{s['named_failures']} | {a['exit']}/{s['exit']} | {a['ids_lost']}/{s['ids_lost']} | "
              f"{r['hidden_tests']} | {r['own_suite']} | {', '.join(r['out_of_scope']) or 'none'} | "
              f"{'yes' if r['catch_all_added'] else 'no'} |")
