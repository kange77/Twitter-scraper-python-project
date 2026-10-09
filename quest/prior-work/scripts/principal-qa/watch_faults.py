"""Watch-mode fault injection through the real CLI against hostile_mock.py.
1) One poison tweet (id 900) among watched targets kills every cycle, for every target, on every restart.
2) The JSON Lines sink is at-most-once: events are committed to the state file before the sink
   writes them, so a failed write (here: /dev/full, i.e. disk full) loses them for good."""
import os, subprocess, sys, tempfile
X = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xs.py")
d = tempfile.mkdtemp()
def sh(*a):
    r = subprocess.run([sys.executable, X, "watch", *a, "--once", "--every", "60s", "--rate", "50"],
                       capture_output=True, text=True)
    last = (r.stderr.strip().splitlines() or [""])[-1]
    print(f"$ xscraper watch {' '.join(os.path.basename(x) for x in a)} --once -> exit {r.returncode}; "
          f"stdout {len(r.stdout.splitlines())} lines; stderr tail: {last[:90]}")
    return r
st = os.path.join(d, "w.db")
print("-- 1) poison pill in watch mode")
sh(st, "10", "11", "900")
sh(st)                                   # restart: dies again, 10 and 11 never get polled
import sqlite3; c = sqlite3.connect(st)
print("   events recorded:", c.execute("select count(*) from events").fetchone()[0],
      "| targets with last_run set:", c.execute("select count(*) from targets where last_run is not null").fetchone()[0], "of 3")
print("-- 2) JSONL sink loses events when the write fails")
st2 = os.path.join(d, "w2.db"); ev = os.path.join(d, "events.jsonl")
sh(st2, "20", "21", "--events", "/dev/full")
c2 = sqlite3.connect(st2); print("   events in state file:", c2.execute("select count(*) from events").fetchone()[0])
sh(st2, "--events", ev)
print("   events in", os.path.basename(ev), ":", sum(1 for _ in open(ev)) if os.path.exists(ev) else 0)
