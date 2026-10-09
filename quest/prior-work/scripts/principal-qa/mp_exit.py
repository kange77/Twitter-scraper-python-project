"""--processes N: a helper process that crashes is never noticed. The parent exits 0 and prints
a normal summary while the helper's leased items sit stranded. Poison item: tweet 900."""
import os, re, subprocess, sys, tempfile
X = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xs.py")
for trial in range(6):
    d = tempfile.mkdtemp(); job = os.path.join(d, "j.db")
    seeds = [str(i) for i in range(2000, 2600)] + ["900"]
    r = subprocess.run([sys.executable, X, "crawl", job, *seeds, "--rate", "400", "--workers", "16",
                        "--processes", "3", "--progress", "0"], capture_output=True, text=True, timeout=300)
    died = r.stderr.count("TypeError: unhashable")
    status = [l for l in r.stderr.splitlines() if "pending" in l][-1:] or ["(no summary: parent crashed)"]
    print(f"trial {trial}: exit {r.returncode}; tracebacks {died}; {status[0].strip()[:110]}")
