import asyncio
from xscraper.watch import WatchStore, Watcher
from xscraper.scraper import tweet_from_body
from xscraper.models import Tweet, User
t = Tweet("42", "hello", "2024-01-01T00:00:00Z", User("1", "a"), like_count=5)
bodies = [b'{"id_str":"42","text":"hello","user":{"screen_name":"a"},"favorite_count":5,"created_at":"2024-06-10T14:00:00.000Z"}', b"", b'{"id_str":"42","text":"hello","user":{"screen_name":"a"},"favorite_count":5,"created_at":"2024-06-10T14:00:00.000Z"}']
async def main():
    s = WatchStore("w.db"); s.add_target("tweet", "42", 0)
    it = iter(bodies)
    async def ft(k): return tweet_from_body(k, next(it))
    async def fu(k): return []
    w = Watcher(s, ft, fu)
    for i in range(3):
        ev = await w.cycle(); print(f"cycle {i}:", [e.type for e in ev])
asyncio.run(main())
