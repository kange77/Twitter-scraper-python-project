"""Fake X syndication server with payloads shaped like the live endpoints."""
import json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

NASA = {"id_str": "11348282", "name": "NASA", "screen_name": "NASA", "verified": False,
        "is_blue_verified": True, "profile_image_url_https": "https://pbs.twimg.com/profile_images/1/nasa_normal.jpg",
        "followers_count": 89000000}
ALICE = {"id_str": "501", "name": "Alice", "screen_name": "alice", "is_blue_verified": False,
         "profile_image_url_https": "https://pbs.twimg.com/p/a.jpg"}
BOB = {"id_str": "502", "name": "Bob 🚀", "screen_name": "bob_rocket", "is_blue_verified": False}

def embed(id_, text, user, created, **kw):
    d = {"__typename": "Tweet", "lang": "en", "favorite_count": kw.pop("likes", 10),
         "possibly_sensitive": False, "created_at": created,
         "display_text_range": kw.pop("dtr", [0, len(text)]),
         "entities": kw.pop("entities", {"hashtags": [], "urls": [], "user_mentions": [], "symbols": []}),
         "id_str": id_, "text": text, "user": user, "edit_control": {"edit_tweet_ids": [id_],
         "editable_until_msecs": "1", "is_edit_eligible": False, "edits_remaining": "5"},
         "conversation_count": kw.pop("replies", 2), "news_action_type": "conversation",
         "isEdited": False, "isStaleEdit": False}
    d.update(kw)
    return d

T_ROOT = embed("1800000000000000001", "Launch day for Artemis &amp; friends! #Artemis https://t.co/abc123",
               NASA, "2024-06-10T14:00:00.000Z", likes=52000, replies=900,
               entities={"hashtags": [{"indices": [37, 45], "text": "Artemis"}],
                         "urls": [{"display_url": "nasa.gov/artemis", "expanded_url": "https://www.nasa.gov/artemis",
                                   "indices": [46, 69], "url": "https://t.co/abc123"}],
                         "user_mentions": [], "symbols": [],
                         "media": [{"display_url": "pic.x.com/xyz", "expanded_url": "https://x.com/NASA/status/1800000000000000001/video/1",
                                    "indices": [70, 93], "url": "https://t.co/media1"}]},
               mediaDetails=[{"display_url": "pic.x.com/xyz", "type": "video",
                              "media_url_https": "https://pbs.twimg.com/amplify_video_thumb/1/img/a.jpg",
                              "original_info": {"height": 1080, "width": 1920},
                              "url": "https://t.co/media1",
                              "video_info": {"aspect_ratio": [16, 9], "duration_millis": 30000, "variants": [
                                  {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                                  {"bitrate": 256000, "content_type": "video/mp4", "url": "https://video.twimg.com/480.mp4"},
                                  {"bitrate": 2176000, "content_type": "video/mp4", "url": "https://video.twimg.com/1080.mp4"}]}}],
               video={"aspectRatio": [16, 9], "durationMs": 30000, "variants": []})
T_ROOT["text"] += " https://t.co/media1"
# Reply: live payloads keep the leading @mention in text; display_text_range skips it.
T_REPLY = embed("1800000000000000002", "@NASA Go Artemis! 🚀", ALICE, "2024-06-10T14:05:00.000Z",
                dtr=[6, 19], in_reply_to_screen_name="NASA", in_reply_to_status_id_str=T_ROOT["id_str"],
                in_reply_to_user_id_str=NASA["id_str"],
                entities={"hashtags": [], "urls": [], "symbols": [],
                          "user_mentions": [{"id_str": "11348282", "indices": [0, 5], "name": "NASA", "screen_name": "NASA"}]},
                parent=T_ROOT)
T_REPLY2 = embed("1800000000000000003", "@alice @NASA same!!", BOB, "2024-06-10T14:06:00.000Z",
                 dtr=[13, 19], in_reply_to_status_id_str=T_REPLY["id_str"], parent=T_REPLY,
                 quoted_tweet=T_ROOT)
TOMB = {"__typename": "TweetTombstone", "tombstone": {"text": {"text": "This Post was deleted by the Post author. Learn more",
        "entities": [], "rtl": False}}}
UNAVAILABLE = {"__typename": "TweetUnavailable", "reason": "Protected"}
TWEETS = {t["id_str"]: t for t in (T_ROOT, T_REPLY, T_REPLY2)}

def v11(id_, text, user, created, **kw):
    d = {"created_at": created, "id_str": id_, "full_text": text, "display_text_range": [0, len(text)],
         "entities": {"hashtags": [], "symbols": [], "user_mentions": [], "urls": []},
         "user": user, "favorite_count": 100, "retweet_count": 20, "reply_count": 5, "quote_count": 1,
         "lang": "en", "conversation_id_str": id_, "permalink": f"/{user['screen_name']}/status/{id_}"}
    d.update(kw)
    return d

ORIG_LONG = ("Webb just captured the most detailed image yet of the Pillars of Creation, showing newly "
             "formed stars glowing inside clouds of gas and dust 6,500 light-years away.")
TIMELINE = [
    v11("1799999999999999990", "Pinned: welcome to NASA on X", NASA, "Mon Jan 01 12:00:00 +0000 2024"),
    v11("1800000000000000010", "Liftoff! 🚀 #Artemis #NASA", NASA, "Tue Jun 11 09:00:00 +0000 2024",
        entities={"hashtags": [{"text": "Artemis", "indices": [11, 19]}, {"text": "NASA", "indices": [20, 25]}],
                  "symbols": [], "user_mentions": [], "urls": [],
                  "media": [{"url": "https://t.co/pic9", "media_url_https": "https://pbs.twimg.com/media/p.jpg",
                             "type": "photo", "original_info": {"width": 1200, "height": 800}}]},
        extended_entities={"media": [{"url": "https://t.co/pic9", "media_url_https": "https://pbs.twimg.com/media/p.jpg",
                                      "type": "photo", "original_info": {"width": 1200, "height": 800}}]}),
    v11("1800000000000000011", "RT @NASAWebb: " + ORIG_LONG[:125] + "…", NASA, "Tue Jun 11 10:00:00 +0000 2024",
        retweet_count=4000, favorite_count=0,
        retweeted_status=v11("1799000000000000000", ORIG_LONG,
                             {"id_str": "9", "screen_name": "NASAWebb", "name": "NASA Webb Telescope"},
                             "Mon Jun 10 10:00:00 +0000 2024", retweet_count=4000, favorite_count=20000)),
]

def page(entries):
    nd = {"props": {"pageProps": {"contextProvider": {"features": {}, "scribeData": {}, "hasResults": bool(entries)},
          "lang": "en", "timeline": {"entries": entries}, "latest_tweet_id": "1800000000000000011",
          "headerProps": {"screenName": "NASA"}}, "__N_SSP": True}, "page": "/timeline-profile/screen-name/[screenName]",
          "query": {"screenName": "NASA"}, "buildId": "abc", "isFallback": False, "gssp": True}
    return ('<!DOCTYPE html><html><head><meta charSet="utf-8"/></head><body><div id="__next"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>')

ENTRIES = [{"type": "tweet", "entry_id": f"tweet-{t['id_str']}", "sort_index": t["id_str"],
            "content": {"tweet": t}} for t in TIMELINE]
ENTRIES.insert(1, {"type": "timeline_cursor", "entry_id": "cursor-top", "content": {"value": "x"}})
hits = {}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json;charset=utf-8", headers=()):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        key = u.path + "?" + (q.get("id") or [""])[0]; hits[key] = hits.get(key, 0) + 1
        if u.path == "/tweet-result":
            i = q["id"][0]
            if not q.get("token"): return self.send(400, "{}")
            if i in TWEETS: return self.send(200, json.dumps(TWEETS[i]))
            if i == "404": return self.send(404, "")
            if i == "111": return self.send(200, json.dumps(TOMB))
            if i == "222": return self.send(200, json.dumps(UNAVAILABLE))
            if i == "333": return self.send(200, "")
            if i == "444": return self.send(200, "<html><body>Something went wrong</body></html>", "text/html")
            if i == "555":  # rate limited once, then fine
                if hits[key] == 1:
                    return self.send(429, '{"errors":[{"code":88}]}', headers=[("x-rate-limit-reset", str(int(time.time()) + 2))])
                return self.send(200, json.dumps(dict(T_ROOT, id_str="555")))
            if i == "666": return self.send(403, "")
            return self.send(404, "")
        if u.path.startswith("/srv/timeline-profile/screen-name/"):
            name = u.path.rsplit("/", 1)[1].lower()
            if name == "nasa": return self.send(200, page(ENTRIES), "text/html; charset=utf-8")
            if name == "emptyuser": return self.send(200, page([]), "text/html; charset=utf-8")
            if name == "blocked": return self.send(200, "<html><body>Rate limit exceeded</body></html>", "text/html")
            return self.send(404, "")
        self.send(404, "")

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
