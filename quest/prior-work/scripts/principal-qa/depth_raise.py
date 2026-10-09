"""Raising --depth on an existing job does nothing for items already done:
links are only computed when an item completes, so the frontier never grows."""
import sys, subprocess, os, tempfile
X = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xs.py")
d = tempfile.mkdtemp(); job = os.path.join(d, "j.db")
def sh(*a):
    r = subprocess.run([sys.executable, X, "crawl", job, *a, "--rate", "50", "--progress", "0"], capture_output=True, text=True)
    print(f"$ xscraper crawl job.db {' '.join(a)}  (exit {r.returncode})"); print("  " + r.stderr.strip().splitlines()[-1])
sh("40", "--follow", "parents", "--depth", "1")    # 40 -> 20 (depth 1); 20 -> 10 would be depth 2
sh("--depth", "3")                                  # expect 10 and 5 to be queued now
