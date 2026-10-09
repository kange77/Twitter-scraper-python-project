"""Hostile X mock for principal QA. Normal IDs get an embed payload (tweet N replies to N//2
when N % 3, like benchmarks/mock_x.py --links). Special cases:
  tweet id 900  -> poison payload ("__typename": ["Tweet"])
  tweet id 901  -> 200 OK but x-rate-limit-remaining: 0, x-rate-limit-reset in MILLISECONDS
  timeline /badcharset -> 200 with Content-Type charset=x-bogus
  timeline /nasa -> 3 tweets whose IDs are also served by /tweet-result (embed lacks retweet_count)
  POST /hook -> webhook receiver that sleeps 1.5 s and logs every event seq it receives
Every request is counted at /__stats."""
import json, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOCK = threading.Lock(); STATS = {"requests": 0, "by_id": {}}; HOOK = []
USER = {"id_str": "11348282", "name": "NASA", "screen_name": "NASA"}

def embed(i):
    n = int(i); d = {"__typename": "Tweet", "id_str": i, "text": f"tweet {i}", "lang": "en",
        "created_at": "2024-06-10T14:00:00.000Z", "favorite_count": 7, "conversation_count": 3, "user": USER}
    if n > 1 and n % 3 and n < 900: d["in_reply_to_status_id_str"] = str(n // 2)
    return d

def v11(i, rt):
    return {"id_str": i, "full_text": f"tweet {i}", "created_at": "Tue Jun 11 09:00:00 +0000 2024", "user": USER,
            "favorite_count": 7, "retweet_count": rt, "reply_count": 3, "quote_count": 2, "lang": "en",
            "entities": {"hashtags": [], "urls": [], "user_mentions": [], "symbols": []}}

def page(entries):
    nd = {"props": {"pageProps": {"contextProvider": {"hasResults": bool(entries)}, "timeline": {"entries": entries}}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">' + json.dumps(nd) + '</script></body></html>')

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json", headers=()):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(1.5)
        with LOCK: HOOK.extend(e["seq"] for e in body["events"])
        self.send(200, "{}")
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/__stats":
            with LOCK: s = dict(STATS, hook=HOOK)
            if "reset" in q:
                with LOCK: STATS.update(requests=0, by_id={}); HOOK.clear()
            return self.send(200, json.dumps(s))
        with LOCK:
            STATS["requests"] += 1; k = (q.get("id") or [u.path])[0]; STATS["by_id"][k] = STATS["by_id"].get(k, 0) + 1
        if u.path == "/tweet-result":
            i = q["id"][0]
            if i == "900": return self.send(200, json.dumps({"__typename": ["Tweet"], "id_str": i}))
            if i == "901":
                return self.send(200, json.dumps(embed(i)), headers=[("x-rate-limit-remaining", "0"),
                                 ("x-rate-limit-reset", str(int(time.time() * 1000) + 60000))])
            return self.send(200, json.dumps(embed(i)))
        if u.path.startswith("/srv/timeline-profile/screen-name/"):
            name = u.path.rsplit("/", 1)[1].lower()
            if name == "badcharset":
                return self.send(200, page([]), "text/html; charset=x-bogus")
            ents = [{"type": "tweet", "entry_id": f"tweet-{i}", "content": {"tweet": v11(i, 40)}} for i in ("1001", "1002", "1003")]
            return self.send(200, page(ents), "text/html; charset=utf-8")
        self.send(404, "")

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8790), H).serve_forever()
