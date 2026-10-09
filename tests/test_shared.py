"""Rate limits shared across processes through the job file."""
import threading

import pytest

from xscraper.jobs import JobStore
from xscraper.shared import SharedRateGate, SharedRateLimiter


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "job.db"
    JobStore(p).close()
    return p


def test_limiter_spaces_requests_across_instances(path):
    clock = Clock()
    a = SharedRateLimiter(path, rate=10, burst=1, clock=clock)
    b = SharedRateLimiter(path, rate=10, burst=1, clock=clock)
    waits = [a.reserve(), b.reserve(), a.reserve(), b.reserve()]
    # One budget: the 4 requests get slots 0.1 s apart, whichever process asks.
    assert waits == pytest.approx([0.0, 0.1, 0.2, 0.3])
    clock.t += 10  # idle: no credit beyond the burst accumulates
    assert a.reserve() == 0.0 and b.reserve() == pytest.approx(0.1)


def test_limiter_burst(path):
    clock = Clock()
    lim = SharedRateLimiter(path, rate=10, burst=5, clock=clock)
    other = SharedRateLimiter(path, rate=10, burst=5, clock=clock)
    waits = [lim.reserve() for _ in range(5)] + [other.reserve()]
    assert waits[:5] == [0.0] * 5
    assert waits[5] == pytest.approx(0.1, abs=1e-6)


def test_limiter_total_rate_under_threads(path):
    clock = Clock()
    limiters = [SharedRateLimiter(path, rate=100, burst=10, clock=clock) for _ in range(4)]
    waits = []
    lock = threading.Lock()

    def run(lim):
        for _ in range(50):
            w = lim.reserve()
            with lock:
                waits.append(w)

    threads = [threading.Thread(target=run, args=(lim,)) for lim in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 200 requests at 100/s with a burst of 10 need about 1.9 s of slots.
    assert 1.85 <= max(waits) <= 2.0


def test_limiter_rejects_bad_rate(path):
    with pytest.raises(ValueError):
        SharedRateLimiter(path, rate=0)


def test_gate_pause_reaches_other_processes(path):
    clock = Clock()
    a = SharedRateGate(path, clock=clock, refresh=0)
    b = SharedRateGate(path, clock=clock, refresh=0)
    assert b.enter() == 0.0
    b.leave(200, {})
    a.leave(429, {"Retry-After": "30"})  # a is told to back off
    assert b.enter() == pytest.approx(30.0)
    clock.t += 31
    assert b.enter() == 0.0


def test_gate_spent_window_reaches_other_processes(path):
    clock = Clock()
    a = SharedRateGate(path, clock=clock, refresh=0)
    b = SharedRateGate(path, clock=clock, refresh=0)
    assert a.enter() == 0.0
    a.leave(200, {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(int(clock.t + 60))})
    assert a.enter() == pytest.approx(60.0)  # a sees the empty budget and publishes it
    assert b.enter() == pytest.approx(60.0)


def test_gate_hold_is_cached(path):
    clock = Clock()
    a = SharedRateGate(path, clock=clock, refresh=5)
    b = SharedRateGate(path, clock=clock, refresh=5)
    assert b.enter() == 0.0
    a.pause(30)
    assert b.enter() == 0.0  # b hasn't re-read the shared hold yet
    clock.t += 5
    assert b.enter() == pytest.approx(25.0)


def test_cli_crawl_with_several_processes(tmp_path, monkeypatch):
    """Two processes split one job; every tweet is fetched exactly once."""
    import json
    from collections import Counter
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    from xscraper import cli
    from xscraper import scraper as sync_scraper

    hits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            tid = parse_qs(urlsplit(self.path).query)["id"][0]
            hits[tid] += 1
            body = json.dumps({"__typename": "Tweet", "id_str": tid, "text": f"t{tid}",
                               "created_at": "2024-06-10T14:00:00.000Z", "favorite_count": 1,
                               "user": {"id_str": "1", "screen_name": "nasa"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(sync_scraper, "TWEET_ENDPOINT", base + "/tweet-result")
    ids = [str(1000 + i) for i in range(300)]
    try:
        assert cli.main(["crawl", str(tmp_path / "job.db"), *ids, "--processes", "2", "--http", "sync",
                         "--rate", "100000", "--workers", "8", "--progress", "0"]) == 0
    finally:
        server.shutdown()
    assert set(hits) == set(ids) and set(hits.values()) == {1}
    with JobStore(tmp_path / "job.db") as job:
        assert job.counts()["done"] == 300 and len(job.tweets) == 300


def test_window_budget_is_shared_between_processes(tmp_path):
    # Senior review S3: each process used to spend the whole
    # x-rate-limit-remaining for itself, so N processes drew 429s.
    now = [1000.0]
    a = SharedRateGate(tmp_path / "job.db", clock=lambda: now[0])
    b = SharedRateGate(tmp_path / "job.db", clock=lambda: now[0])
    assert a.enter() == 0
    a.leave(200, {"x-rate-limit-remaining": "3", "x-rate-limit-reset": "1010"})
    assert [a.enter(), b.enter(), b.enter()] == [0, 0, 0]
    assert a.enter() == pytest.approx(10) and b.enter() == pytest.approx(10)
    now[0] = 1010  # new window: budget unknown until a response says
    assert b.enter() == 0
    a.close(), b.close()


def test_old_job_files_get_the_window_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE rate_state (name TEXT PRIMARY KEY, tat REAL NOT NULL DEFAULT 0, "
                "hold_until REAL NOT NULL DEFAULT 0)")
    con.commit(), con.close()
    gate = SharedRateGate(path)
    assert gate.enter() == 0
    gate.close()
