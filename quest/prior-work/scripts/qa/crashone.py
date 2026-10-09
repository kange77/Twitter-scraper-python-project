import asyncio
from xscraper.jobs import JobStore, Item
from xscraper.crawl import Crawler
from xscraper.scraper import tweet_from_body
from xscraper.models import Tweet, User
store = JobStore("jc.db"); store.add([Item("tweet", str(i)) for i in range(1, 1001)])
async def ft(k):
    await asyncio.sleep(0.001)
    body = b'{"__typename": ["Tweet"], "id_str": "%s"}' % k.encode() if k == "500" else b'{"id_str":"%s","text":"x","user":{"screen_name":"a"}}' % k.encode()
    return tweet_from_body(k, body)
async def fu(k): return []
try:
    print(asyncio.run(Crawler(store, ft, fu, concurrency=16).run()))
except Exception as e:
    print("CRAWLER DIED:", type(e).__name__, e)
print(store.counts())
