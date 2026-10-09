"""The SQLite store is last-write-wins on the whole tweet: fetching a tweet by ID after seeing it in a
profile timeline replaces its counts with the embed payload's, which has no retweet/quote counts.
Same path in crawl jobs and watch state files (they share TweetStore.upsert)."""
import json, os, sqlite3, subprocess, sys, tempfile
X = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xs.py")
db = os.path.join(tempfile.mkdtemp(), "s.db")
def counts():
    d = json.loads(sqlite3.connect(db).execute("select data from tweets where id='1001'").fetchone()[0])
    return {k: d[k] for k in ("like_count", "retweet_count", "reply_count", "quote_count")}
subprocess.run([sys.executable, X, "user", "nasa", "-o", db, "--rate", "50"], capture_output=True)
print("after `user nasa -o s.db`:  ", counts())
subprocess.run([sys.executable, X, "tweet", "1001", "-o", db, "--rate", "50"], capture_output=True)
print("after `tweet 1001 -o s.db`: ", counts())
