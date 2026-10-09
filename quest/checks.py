"""Before/after checks for the Quest change: one bad payload in a `tweet` batch.

Runs the real `xscraper tweet` CLI in a subprocess against a local, SYNTHETIC
mock of the embed endpoint, so no traffic reaches X. Point PYTHONPATH (or
--src) at the checkout to measure:

    python quest/checks.py --src /path/to/checkout-at-4585f8f   > before.json   # main before the fix
    python quest/checks.py --src .                         > after.json

The mock serves a normal embed payload for every numeric ID, except IDs in
``POISON``, which get a payload whose ``__typename`` is a list or a dict.
That shape is the one the 2026-09-29 QA fuzzing found escaping the parser
(review/xscraper-review.md, finding 1); it is synthetic, not a captured X
response. ``--candidates`` also measures the baselines of the other problems
considered in intent.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

POISON = {"500": ["Tweet"], "700": {"name": "Tweet"}, "900": ["Tweet"]}
USER = {"id_str": "11348282", "name": "NASA", "screen_name": "NASA"}


def _embed(i: str) -> dict:
    return {"__typename": "Tweet", "id_str": i, "text": f"tweet {i}", "lang": "en",
            "created_at": "2024-06-10T14:00:00.000Z", "favorite_count": 7,
            "conversation_count": 3, "user": USER}


def _timeline_page() -> str:
    def v11(i):
        return {"id_str": i, "full_text": f"tweet {i}", "created_at": "Tue Jun 11 09:00:00 +0000 2024",
                "user": USER, "favorite_count": 7, "retweet_count": 40, "reply_count": 3,
                "quote_count": 2, "lang": "en",
                "entities": {"hashtags": [], "urls": [], "user_mentions": [], "symbols": []}}
    entries = [{"type": "tweet", "entry_id": f"tweet-{i}", "content": {"tweet": v11(i)}}
               for i in ("1001", "1002", "1003")]
    nd = {"props": {"pageProps": {"timeline": {"entries": entries}}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(nd) + "</script></body></html>")


class Mock:
    def __init__(self):
        self.requests = 0
        self.lock = threading.Lock()
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json", headers=()):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                with mock.lock:
                    mock.requests += 1
                if u.path == "/tweet-result":
                    i = q["id"][0]
                    if i in POISON:
                        return self.send(200, json.dumps({"__typename": POISON[i], "id_str": i}))
                    if i == "5000":  # candidate B (outside the 1..1000 batch range): reset sent in milliseconds instead of seconds
                        return self.send(200, json.dumps(_embed(i)), headers=[
                            ("x-rate-limit-remaining", "0"),
                            ("x-rate-limit-reset", str(int(time.time() * 1000) + 60000))])
                    return self.send(200, json.dumps(_embed(i)))
                if u.path.startswith("/srv/timeline-profile/screen-name/"):
                    return self.send(200, _timeline_page(), "text/html; charset=utf-8")
                self.send(404, "")

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # clients that abort mid-batch reset their connections; that's expected

        self.server = Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self) -> None:
        with self.lock:
            self.requests = 0


_RUNNER = (
    "import sys\n"
    "from xscraper import scraper, cli\n"
    "base = sys.argv[1]\n"
    "scraper.TWEET_ENDPOINT = base + '/tweet-result'\n"
    "scraper.TIMELINE_ENDPOINT = base + '/srv/timeline-profile/screen-name/{}'\n"
    "sys.exit(cli.main(sys.argv[2:]))\n"
)


def run_cli(mock: Mock, src: str, args: list[str], timeout: float = 120) -> dict:
    mock.reset()
    src = os.path.abspath(src)
    env = dict(os.environ, PYTHONPATH=src)
    start = time.perf_counter()
    try:
        p = subprocess.run([sys.executable, "-c", _RUNNER, f"http://127.0.0.1:{mock.port}", *args],
                           env=env, cwd=src,  # `python -c` puts the cwd first on sys.path
                           capture_output=True, text=True, timeout=timeout)
        code, err, out = p.returncode, p.stderr, p.stdout
    except subprocess.TimeoutExpired as exc:
        code, err, out = "timeout", (exc.stderr or b"").decode(), (exc.stdout or b"").decode()
    return {"exit": code, "seconds": round(time.perf_counter() - start, 2),
            "requests": mock.requests, "stderr": err, "stdout": out}


def batch(mock: Mock, src: str, http: str, n: int, tmp: str) -> dict:
    out = os.path.join(tmp, f"batch_{http}.jsonl")
    if os.path.exists(out):
        os.remove(out)
    ids = [str(i) for i in range(1, n + 1)]
    r = run_cli(mock, src, ["tweet", *ids, "--http", http, "--rate", "5000", "--workers", "16",
                            "--retries", "0", "-o", out])
    written = 0
    if os.path.exists(out):
        with open(out) as f:
            written = sum(1 for _ in f)
    poison = sum(1 for i in ids if i in POISON)
    err = r["stderr"]
    return {
        "ids": n, "poison_ids": poison, "exit": r["exit"], "seconds": r["seconds"],
        "requests": r["requests"], "tweets_written": written,
        "ids_lost": n - poison - written,  # good tweets the run never delivered
        "failures_reported": err.count("failed: "),
        "failure_lines": [line for line in err.splitlines() if line.startswith("failed: ")][:5],
        "traceback": "Traceback" in err,
        "stderr_tail": err.strip().splitlines()[-1:] if err.strip() else [],
    }


def candidates(mock: Mock, src: str, tmp: str) -> dict:
    res = {}
    # B: one x-rate-limit-reset in milliseconds (principal QA P2).
    r = run_cli(mock, src, ["tweet", "5000", "10", "11", "--workers", "1", "--http", "sync"], timeout=20)
    res["B_reset_in_ms"] = {"exit": r["exit"], "seconds": r["seconds"], "requests": r["requests"],
                            "tweets_printed": r["stdout"].count("/status/")}
    # C: refetching by ID erases counts the timeline had stored (principal QA P9).
    db = os.path.join(tmp, "store.db")
    run_cli(mock, src, ["user", "nasa", "--http", "sync", "-o", db])
    before = _counts(db)
    run_cli(mock, src, ["tweet", "1001", "--http", "sync", "-o", db])
    res["C_store_clobber"] = {"retweet_quote_after_user": before, "after_tweet_refetch": _counts(db)}
    # D: --format jsonl without -o is ignored (senior review 10).
    r = run_cli(mock, src, ["tweet", "10", "--http", "sync", "--format", "jsonl"])
    first = r["stdout"].lstrip()[:1]
    res["D_format_without_output"] = {"stdout_is_json": first == "{", "first_char": first}
    return res


def _counts(db: str):
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT data FROM tweets WHERE id = '1001'").fetchone()
    finally:
        con.close()
    d = json.loads(row[0]) if row else {}
    return [d.get("retweet_count"), d.get("quote_count")]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--src", default=".", help="checkout to measure (put on PYTHONPATH)")
    p.add_argument("-n", type=int, default=1000, help="IDs per batch (default 1000)")
    p.add_argument("--candidates", action="store_true", help="also measure the other candidate problems")
    args = p.parse_args()
    mock = Mock()
    with tempfile.TemporaryDirectory() as tmp:
        version = subprocess.run([sys.executable, "-c", "import xscraper; print(xscraper.__file__)"],
                                 env=dict(os.environ, PYTHONPATH=os.path.abspath(args.src)),
                                 cwd=os.path.abspath(args.src), capture_output=True, text=True).stdout.strip()
        report = {"src": os.path.abspath(args.src), "imported": version, "python": sys.version.split()[0],
                  "synthetic_mock": True, "poison_ids": sorted(POISON, key=int)}
        for http in ("async", "sync"):
            report[f"clean_{http}"] = batch(mock, args.src, http, 400, tmp)  # IDs 1..400 hold no poison
            report[f"poison_{http}"] = batch(mock, args.src, http, args.n, tmp)
            report[f"poison_{http}_rerun"] = batch(mock, args.src, http, args.n, tmp)
        if args.candidates:
            report["candidates"] = candidates(mock, args.src, tmp)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
