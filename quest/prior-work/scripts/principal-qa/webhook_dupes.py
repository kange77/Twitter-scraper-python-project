"""Two watch processes on one state file (overlapping cron runs, a restart while the old one drains,
two systemd units) both deliver the same outbox rows: nothing claims an event before POSTing it."""
import json, os, subprocess, sys, tempfile, urllib.request
X = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xs.py")
st = os.path.join(tempfile.mkdtemp(), "w.db")
subprocess.run([sys.executable, X, "watch", st, *map(str, range(30, 40)), "--once", "--every", "60s", "--rate", "50"],
               capture_output=True)
urllib.request.urlopen("http://127.0.0.1:8790/__stats?reset=1").read()
cmd = [sys.executable, X, "watch", st, "--once", "--webhook", "http://127.0.0.1:8790/hook", "--rate", "50"]
ps = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
print("exit codes:", [p.wait() for p in ps])
hook = json.load(urllib.request.urlopen("http://127.0.0.1:8790/__stats"))["hook"]
print(f"webhook received {len(hook)} events, {len(set(hook))} distinct seqs")
