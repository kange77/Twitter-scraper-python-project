"""Crawl coverage depends on timing: an item's depth is fixed by whichever path reaches it first.
Graph: seeds A, B. A -> C -> D -> E (reply parents). B quotes D. follow=all, depth=2.
Shortest paths: D is 1 hop from B, so E (2 hops) is in scope. If B's first fetch fails once,
D is first reached via A->C at depth 2 and E (depth 3) is never queued, even after B succeeds."""
import asyncio, os, sys, tempfile
from xscraper.jobs import JobStore, Item
from xscraper.crawl import Crawler
from xscraper.http import HttpError
from xscraper.models import Tweet, User

GRAPH = {"A": dict(in_reply_to_id="C"), "B": dict(quoted_tweet_id="D"), "C": dict(in_reply_to_id="D"),
         "D": dict(in_reply_to_id="E"), "E": {}}

def run(fail_b_once: bool):
    path = os.path.join(tempfile.mkdtemp(), "j.db")
    store = JobStore(path); store.configure(follow=("parents", "quotes", "retweets"), max_depth=2)
    store.add([Item("tweet", "A"), Item("tweet", "B")])
    failed = {"B": fail_b_once}
    async def ft(k):
        await asyncio.sleep(0.01)
        if failed.get(k):
            failed[k] = False
            raise HttpError("HTTP 503 (transient)", 503)
        return Tweet(k, f"tweet {k}", None, User("1", "u"), **GRAPH[k])
    async def fu(k): return []
    asyncio.run(Crawler(store, ft, fu, concurrency=4, flush_interval=0.05).run())
    rows = dict(store.conn.execute("SELECT key, state || ' depth ' || depth FROM frontier"))
    store.close()
    return rows

print("no failure:        ", run(False))
print("B fails once (503):", run(True))
