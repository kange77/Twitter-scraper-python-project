import subprocess, sys
M = [
 ("xscraper/jobs.py", "WHERE state = 'leased' AND lease_until < ? ", "WHERE state = 'leased' AND lease_until < ? + 1e9 ", "claim steals live leases"),
 ("xscraper/jobs.py", "if row[0] is not None and row[0] < now:", "if False:", "next_ready_in ignores expired leases"),
 ("xscraper/jobs.py", "delay = min(300.0, 5.0 * 2 ** (attempts - 1))", "delay = 0.0", "no retry backoff"),
 ("xscraper/jobs.py", "if attempts >= cfg.max_attempts:", "if attempts > cfg.max_attempts:", "off-by-one max attempts"),
 ("xscraper/jobs.py", "WHERE state = 'pending'\", timeline_rows", "\", timeline_rows", "timeline marks leased/failed items done"),
 ("xscraper/shared.py", "row.conn.execute(\"UPDATE rate_state SET tat = ? WHERE name = ?\", (tat, row.name))", "pass", "GCRA state not shared"),
 ("xscraper/shared.py", "self._publish(now + min(wait, self.max_wait))", "pass", "hold not published on budget exhaustion"),
 ("xscraper/http.py", "budget = int(remaining) - self.in_flight", "budget = int(remaining)", "in-flight not discounted"),
 ("xscraper/http.py", "wait = retry_after_seconds(headers, now) if status in (429, 503) else None", "wait = retry_after_seconds(headers, now) if status == 429 else None", "503 Retry-After ignored"),
 ("xscraper/crawl.py", "store.unregister(self.owner)", "pass", "leases not released on exit"),
 ("xscraper/crawl.py", "if now - last_beat >= self.heartbeat:", "if False:", "no heartbeat/lease renewal"),
 ("xscraper/watch.py", "if row[2] is not None:", "if True:", "deleted event for never-seen tweet"),
 ("xscraper/watch.py", "merged = [new if new is not None else old for new, old in zip(counts, old_counts)]", "merged = counts", "None counts overwrite known counts"),
 ("xscraper/watch.py", "store.mark_delivered(e.seq for e in events)", "pass", "webhook never marks delivered"),
 ("xscraper/watch.py", "if state == \"deleted\":", "if False:", "no restored event"),
 ("xscraper/metrics.py", "if queued and idle > stall_after:", "if False:", "healthz never fails"),
]
for f, a, b, name in M:
    src = open(f).read()
    assert src.count(a) >= 1, (f, a)
    open(f, "w").write(src.replace(a, b, 1))
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:randomly"], capture_output=True, text=True, timeout=300)
        res = "KILLED " if r.returncode else "SURVIVED"
    except subprocess.TimeoutExpired:
        res = "KILLED (timeout)"
    finally:
        open(f, "w").write(src)
    print(f"{res:9} {name}")
