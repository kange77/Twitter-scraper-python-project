"""Mutation testing for xscraper: plant one realistic bug at a time and check the suite catches it.

WARNING: it edits files under xscraper/ in place (restoring each one afterwards).
Run it on a throwaway copy, never in your working tree:

    git archive HEAD | tar -x -C /tmp/mut && cd /tmp/mut
    PYTHONPATH=$PWD python scripts/mutants.py [orig|new|all]

ORIG: the 16 mutants from the 2026-09-29 principal QA, adapted to the current code.
NEW:  34 mutants aimed at the 2026-10-07 release-gate fixes.
Each mutant is (file, search, replacement, name); the search must occur exactly once.
Results 2026-10-09 on main: 50/50 killed. A SURVIVED or NOMATCH line means a test or this
script needs updating.
"""
import subprocess, sys, os, time

ORIG = [
 ("xscraper/jobs.py", "WHERE state = 'leased' AND lease_until < ? ", "WHERE state = 'leased' AND lease_until < ? + 1e9 ", "O1 claim steals live leases"),
 ("xscraper/jobs.py", "if row[0] is not None and row[0] < now:", "if False:", "O2 next_ready_in ignores expired leases"),
 ("xscraper/jobs.py", "delay = min(300.0, 5.0 * 2 ** (attempts - 1))", "delay = 0.0", "O3 no retry backoff"),
 ("xscraper/jobs.py", "if attempts >= cfg.max_attempts:", "if attempts > cfg.max_attempts:", "O4 off-by-one max attempts"),
 ("xscraper/jobs.py", "WHERE state = 'pending'\", timeline_rows", "\", timeline_rows", "O5 timeline marks leased/failed items done"),
 ("xscraper/shared.py", "row.conn.execute(\"UPDATE rate_state SET tat = ? WHERE name = ?\", (tat, row.name))", "pass", "O6 GCRA state not shared"),
 # adapted: budget exhaustion no longer publishes a hold (it is a shared win_budget now);
 # same intent = a pause one process learns is not published to the others.
 ("xscraper/shared.py", "        super().pause(seconds)\n        self._publish(self._clock() + min(seconds, self.max_wait))", "        super().pause(seconds)\n        pass", "O7 hold not published to other processes (adapted)"),
 ("xscraper/http.py", "budget = int(remaining) - self.in_flight", "budget = int(remaining)", "O8 in-flight not discounted"),
 ("xscraper/http.py", "wait = retry_after_seconds(headers, now) if status in (429, 503) else None", "wait = retry_after_seconds(headers, now) if status == 429 else None", "O9 503 Retry-After ignored"),
 ("xscraper/crawl.py", "store.unregister(self.owner)", "pass", "O10 leases not released on exit"),
 ("xscraper/crawl.py", "if now - last_beat >= self.heartbeat:", "if False:", "O11 no heartbeat/lease renewal"),
 ("xscraper/watch.py", "if row[2] is not None:", "if True:", "O12 deleted event for never-seen tweet"),
 ("xscraper/watch.py", "merged = [new if new is not None else old for new, old in zip(counts, old_counts)]", "merged = counts", "O13 None counts overwrite known counts"),
 ("xscraper/watch.py", "store.mark_delivered(e.seq for e in events)", "pass", "O14 webhook never marks delivered"),
 ("xscraper/watch.py", "if state == \"deleted\":", "if False:", "O15 no restored event"),
 ("xscraper/metrics.py", "if queued and idle > stall_after:", "if False:", "O16 healthz never fails"),
]

NEW = [
 ("xscraper/crawl.py", "        except Exception as exc:\n            # Anything else", "        except ZeroDivisionError as exc:\n            # Anything else", "N1 crawl _work: no catch-all boundary"),
 ("xscraper/watch.py", "                except Exception as exc:\n                    # One target", "                except ZeroDivisionError as exc:\n                    # One target", "N2 watch _bounded: no catch-all boundary"),
 ("xscraper/aio.py", "            codecs.lookup(self.encoding)", "            pass", "N3 aio Response.text: no charset fallback"),
 ("xscraper/http.py", "reset.isdigit() and float(reset) - now > MAX_RESET_HORIZON:", "reset.isdigit() and False:", "N4 RateGate.leave: no MAX_RESET_HORIZON check"),
 ("xscraper/http.py", "if wait is not None and wait > MAX_RESET_HORIZON:", "if False:", "N5 retry_after_seconds: no horizon check"),
 ("xscraper/http.py", "if waited >= 2 * gate.max_wait:", "if False:", "N6 gate_sleep_time: never gives up"),
 ("xscraper/http.py", "wait = min(wait, max_backoff, 2 * gate.max_wait - waited) +", "wait = min(wait, max_backoff) +", "N7 gate_sleep_time: last sleep not trimmed to cap"),
 ("xscraper/http.py", "            waited += wait", "            pass", "N8 HttpClient._enter_gate: waited not accumulated"),
 ("xscraper/aio.py", "                waited += wait", "                pass", "N9 AsyncHttpClient: waited not accumulated"),
 ("xscraper/crawl.py", "if not store.leased_elsewhere(self.owner):", "if True:", "N10 crawl run: exits while others hold leases"),
 ("xscraper/crawl.py", "if store.reap(self.stale_after):", "if False:", "N11 crawl run: no reap while waiting"),
 ("xscraper/jobs.py", "    except ProcessLookupError:\n        return False", "    except ProcessLookupError:\n        return True", "N12 _pid_alive: dead pid reported alive"),
 ("xscraper/jobs.py", "if beat < cutoff or (whost == host and pid != os.getpid() and not _pid_alive(pid)):", "if beat < cutoff:", "N13 reap: heartbeat-only (no pid check)"),
 ("xscraper/cli.py", "with _on_signal(signal.SIGTERM, crawler.stop):", "with contextlib.nullcontext():", "N14 cmd_crawl: SIGTERM not handled"),
 ("xscraper/cli.py", "return 128 + signal.SIGTERM", "return 0", "N15 cmd_crawl: stopped exits 0"),
 ("xscraper/cli.py", "if left and max_items is None:", "if False:", "N16 cmd_crawl: leftover work exits 0"),
 ("xscraper/cli.py", "return 1 if counts[\"failed\"] or crashed else 0", "return 1 if counts[\"failed\"] else 0", "N17 cmd_crawl: crashed helper exits 0"),
 ("xscraper/cli.py", "h.terminate()  # they stop", "pass  # they stop", "N18 cmd_crawl: helpers not terminated on SIGTERM"),
 ("xscraper/shared.py", "            if budget <= 0:\n                return reset - now\n            if not take:", "            if budget <= 0:\n                return 0.0\n            if not take:", "N19 _window_wait: exhausted budget doesn't wait"),
 ("xscraper/shared.py", "SET win_budget = win_budget - 1 WHERE", "SET win_budget = win_budget WHERE", "N20 SharedRateGate: budget never decremented"),
 ("xscraper/shared.py", "self._set_window(float(reset), budget)", "pass", "N21 SharedRateGate.leave: window never shared"),
 ("xscraper/shared.py", "        return self._window_wait(now, take=False)", "        return 0.0", "N22 SharedRateGate._wait ignores shared window"),
 ("xscraper/shared.py", "budget = int(remaining) - max(0, self.in_flight - 1)", "budget = int(remaining)", "N23 SharedRateGate.leave: in-flight not discounted"),
 ("xscraper/shared.py", "now < float(reset) <= now + MAX_RESET_HORIZON:", "now < float(reset):", "N24 SharedRateGate.leave: no horizon check"),
 ("xscraper/shared.py", "THEN MIN(COALESCE(win_budget, ?), ?)", "THEN MAX(COALESCE(win_budget, ?), ?)", "N25 _set_window: same window can raise budget"),
 ("xscraper/jobs.py", "elif depth < old[1]:", "elif False:", "N26 _queue: no depth relaxation"),
 ("xscraper/jobs.py", "if old[0] == \"done\" and kind == TWEET:", "if False:", "N27 _queue: no re-expansion of done items"),
 ("xscraper/jobs.py", "if depth + 1 > cfg.max_depth or not cfg.follow:", "if depth > cfg.max_depth or not cfg.follow:", "N28 _links_of: off-by-one depth"),
 ("xscraper/jobs.py", "if cfg.max_depth > old.max_depth or set(cfg.follow) - set(old.follow):", "if False:", "N29 configure: no re-expansion"),
 ("xscraper/jobs.py", "if cfg.max_depth > old.max_depth or set(cfg.follow) - set(old.follow):", "if cfg.max_depth > old.max_depth:", "N30 configure: new --follow type not re-expanded"),
 ("xscraper/jobs.py", "depth = MIN(depth, excluded.depth) WHERE", "depth = depth WHERE", "N31 timeline rows don't lower depth"),
 ("xscraper/scraper.py", "raise ParseError(f\"tweet {tweet_id}: empty response (soft block or endpoint glitch)\")", "return None", "N32 tweet_from_body: empty body = deleted"),
 ("xscraper/scraper.py", "except (TypeError, ValueError, LookupError, AttributeError, ArithmeticError) as exc:", "except ZeroDivisionError as exc:", "N33 tweet_from_body: shape errors escape"),
 ("xscraper/parse.py", "if typename is not None and not isinstance(typename, str):", "if False:", "N34 parse_tweet_result: non-str __typename accepted"),
]

which = sys.argv[1] if len(sys.argv) > 1 else "all"
M = {"orig": ORIG, "new": NEW, "all": ORIG + NEW}[which]
env = dict(os.environ, PYTHONPATH=os.getcwd())
for f, a, b, name in M:
    src = open(f).read()
    n = src.count(a)
    if n != 1:
        print(f"NOMATCH({n}) {name}", flush=True)
        continue
    t0 = time.time()
    try:
        open(f, "w").write(src.replace(a, b, 1))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                           capture_output=True, text=True, timeout=300, env=env)
        res = "KILLED" if r.returncode else "SURVIVED"
        if r.returncode:
            fail = [l for l in r.stdout.splitlines() if l.startswith("FAILED") or l.startswith("ERROR")]
            res += "  " + (fail[0][:110] if fail else f"rc={r.returncode}")
    except subprocess.TimeoutExpired:
        res = "KILLED(timeout)"
    finally:
        open(f, "w").write(src)
    print(f"{name:55} {res}  [{time.time()-t0:.0f}s]", flush=True)
