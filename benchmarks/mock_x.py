"""Load-test stand-in for X's syndication endpoints.

Serves a synthetic embed payload for any numeric tweet ID and a 40-tweet
profile-timeline page for any screen name, with keep-alive HTTP/1.1, an
optional simulated network latency and an optional server-side rate limit
that answers 429 with ``x-rate-limit-*`` headers the way X does.

    python benchmarks/mock_x.py --port 8766 --latency 0.05 --limit 500

It runs on asyncio so the mock itself isn't what gets measured.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

_WORDS = ("launch rocket moon mars data model release great team proud amazing stream "
          "failing support honestly thanks love wow orbit crew science").split()


def _embed(tweet_id: str) -> dict:
    n = int(tweet_id)
    words = " ".join(_WORDS[(n + i * 7) % len(_WORDS)] for i in range(12 + n % 20))
    text = f"{words} #Artemis @NASA https://t.co/abc{n % 1000}"
    return {
        "__typename": "Tweet", "lang": "en", "id_str": tweet_id, "text": text,
        "created_at": "2024-06-10T14:00:00.000Z", "favorite_count": n % 5000,
        "conversation_count": n % 90, "display_text_range": [0, len(text)],
        "entities": {"hashtags": [{"indices": [0, 0], "text": "Artemis"}],
                     "user_mentions": [{"id_str": "11348282", "screen_name": "NASA", "name": "NASA"}],
                     "urls": [{"url": f"https://t.co/abc{n % 1000}",
                               "expanded_url": "https://www.nasa.gov/artemis"}], "symbols": []},
        "user": {"id_str": "11348282", "name": "NASA", "screen_name": "NASA",
                 "is_blue_verified": True, "followers_count": 89000000,
                 "profile_image_url_https": "https://pbs.twimg.com/profile_images/1/nasa_normal.jpg"},
        "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/p.jpg",
                          "original_info": {"width": 1200, "height": 800}}],
        "edit_control": {"edit_tweet_ids": [tweet_id], "editable_until_msecs": "1",
                         "is_edit_eligible": False, "edits_remaining": "5"},
    }


def _timeline(name: str) -> bytes:
    user = {"id_str": "1", "screen_name": name, "name": name.title()}
    entries = []
    for i in range(40):
        tid = str(1800000000000000000 + i)
        text = " ".join(_WORDS[(i + k) % len(_WORDS)] for k in range(20)) + " #Artemis"
        entries.append({"type": "tweet", "entry_id": f"tweet-{tid}", "sort_index": tid, "content": {"tweet": {
            "id_str": tid, "full_text": text, "created_at": "Tue Jun 11 09:00:00 +0000 2024",
            "display_text_range": [0, len(text)], "user": user, "favorite_count": i, "retweet_count": i,
            "reply_count": i, "quote_count": 0, "lang": "en",
            "entities": {"hashtags": [{"text": "Artemis"}], "user_mentions": [], "urls": [], "symbols": []}}}})
    data = {"props": {"pageProps": {"timeline": {"entries": entries}}}}
    return ('<!DOCTYPE html><html><body><script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}</script></body></html>").encode()


class Limiter:
    """Fixed window: ``limit`` requests per ``window`` seconds, like X's 15-minute windows."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.reset = time.time() + window
        self.used = 0

    def take(self) -> tuple[bool, dict]:
        now = time.time()
        if now >= self.reset:
            self.reset, self.used = now + self.window, 0
        self.used += 1
        headers = {"x-rate-limit-limit": str(self.limit),
                   "x-rate-limit-remaining": str(max(0, self.limit - self.used)),
                   "x-rate-limit-reset": str(int(self.reset + 0.999))}
        return self.used <= self.limit, headers


class Stats:
    def __init__(self):
        self.ok = self.limited = self.connections = 0


async def serve(port: int, latency: float, limiter: Limiter | None, stats: Stats) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stats.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                target = head.split(b" ", 2)[1].decode()
                if latency:
                    await asyncio.sleep(latency)
                status, ctype, body, extra = 200, "application/json", b"", {}
                allowed = True
                if limiter and not target.startswith("/__stats"):
                    allowed, extra = limiter.take()
                url = urlsplit(target)
                if url.path == "/__stats":
                    allowed, status = True, 200
                    body = json.dumps(vars(stats)).encode()
                    if "reset" in url.query:
                        stats.ok = stats.limited = 0
                elif not allowed:
                    status, body = 429, b'{"errors":[{"code":88,"message":"Rate limit exceeded"}]}'
                    stats.limited += 1
                elif url.path == "/tweet-result":
                    tid = (parse_qs(url.query).get("id") or [""])[0]
                    if tid.isdigit():
                        body = json.dumps(_embed(tid)).encode()
                    else:
                        status = 404
                elif url.path.startswith("/srv/timeline-profile/screen-name/"):
                    body, ctype = _timeline(url.path.rsplit("/", 1)[1]), "text/html; charset=utf-8"
                else:
                    status = 404
                if status == 200 and url.path != "/__stats":
                    stats.ok += 1
                lines = [f"HTTP/1.1 {status} X", f"Content-Type: {ctype}", f"Content-Length: {len(body)}"]
                lines += [f"{k}: {v}" for k, v in extra.items()]
                writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port, backlog=1024)
    async with server:
        await server.serve_forever()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--latency", type=float, default=0.0, help="seconds added to every response")
    p.add_argument("--limit", type=int, help="requests allowed per window (default: unlimited)")
    p.add_argument("--window", type=float, default=10.0, help="rate-limit window in seconds")
    args = p.parse_args()
    limiter = Limiter(args.limit, args.window) if args.limit else None
    try:
        asyncio.run(serve(args.port, args.latency, limiter, Stats()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
