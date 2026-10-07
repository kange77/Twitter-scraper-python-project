"""Fault injection: the guarantees the crash-recovery, retry and watch code make.

Each test here pins a behaviour that a planted bug (mutation testing,
review/principal-qa) slipped past the rest of the suite: lease renewal,
retry backoff, exit codes, SIGTERM, the shared rate window and depth
bookkeeping.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import FakeResponse
from xscraper import cli
from xscraper.crawl import Crawler
from xscraper.http import HttpError, RateGate
from xscraper.jobs import TWEET, USER, Item, JobStore, Outcome
from xscraper.shared import SharedRateGate
from xscraper.watch import WatchStore, Watcher

from .test_crawl import GRAPH, FakeX, _depths, _finish, chain, crawl, tweet
from .test_http import client, limits


@pytest.fixture
def job(tmp_path):
    with JobStore(tmp_path / "job.db") as store:
        yield store


# -- frontier ---------------------------------------------------------------

def test_expired_lease_makes_work_ready(job):
    job.add([Item(TWEET, "1")])
    job.claim("gone", 1, lease=-1)
    assert job.next_ready_in() == 0


def test_failed_attempt_backs_off(job):
    job.add([Item(TWEET, "1")])
    before = time.time()
    job.complete([Outcome(Item(TWEET, "1"), error="HTTP 503")])
    (not_before,) = job.conn.execute("SELECT not_before FROM frontier").fetchone()
    assert not_before >= before + 5


def test_timeline_does_not_overwrite_leased_or_failed_items(job):
    job.add([Item(TWEET, "1"), Item(TWEET, "2"), Item(USER, "nasa")])
    job.claim("other", 1)  # tweet 1 (shallowest first, all depth 0: rowid order)
    job.conn.execute("UPDATE frontier SET state = 'failed' WHERE key = '2'")
    job.complete([Outcome(Item(USER, "nasa"), [tweet(1), tweet(2), tweet(3)])])
    states = dict(job.conn.execute("SELECT key, state FROM frontier"))
    assert states == {"1": "leased", "2": "failed", "3": "done", "nasa": "done"}


def test_timeline_lowers_the_depth_of_a_queued_tweet(job):
    job.add([Item(TWEET, "7", depth=2), Item(USER, "nasa")])
    job.complete([Outcome(Item(USER, "nasa"), [tweet(7)])])
    assert _depths(job)["7"] == 0


def test_links_stop_at_max_depth(job):
    job.configure(follow=("parents",), max_depth=1)
    _finish(job, "C", 1)
    assert job._links_of("C", 1, 0.0) == [] and "D" not in _depths(job)


def test_new_follow_type_expands_finished_items(job):
    job.configure(follow=("parents",), max_depth=2)
    job.add([Item(TWEET, "B")])
    _finish(job, "B", 0)  # B quotes D, but quotes aren't followed yet
    assert "D" not in _depths(job)
    job.configure(follow=("parents", "quotes"))
    assert job.expanded == 1 and _depths(job)["D"] == 1


def test_leases_are_renewed_while_a_fetch_is_slow(job):
    # Without heartbeat renewal the 0.3 s leases expire mid-fetch and the
    # crawler claims (and fetches) its own items again.
    x = FakeX(chain(2), delay=0.6)
    job.add([Item(TWEET, t) for t in x.tweets])
    crawl(job, x, lease=0.3, heartbeat=0.05, concurrency=4)
    assert job.counts()["done"] == 2 and set(x.calls.values()) == {1}


# -- exit status --------------------------------------------------------------

CLEAN = {"pending": 0, "leased": 0, "done": 5, "missing": 0, "failed": 0}


@pytest.mark.parametrize("counts, stopped, crashed, max_items, code, says", [
    (CLEAN, False, [], None, 0, ""),
    ({**CLEAN, "failed": 1}, False, [], None, 1, ""),
    (CLEAN, False, [(4242, 1)], None, 1, "process 4242 exited with code 1"),
    ({**CLEAN, "pending": 2, "leased": 1}, False, [], None, 1, "3 items are still queued or leased"),
    ({**CLEAN, "pending": 2}, False, [], 10, 0, ""),  # --max-items stops early on purpose
    ({**CLEAN, "pending": 2}, True, [], None, 143, "stopped by signal with 2 items left"),
])
def test_crawl_exit_status(capsys, counts, stopped, crashed, max_items, code, says):
    assert cli._crawl_exit(counts, stopped, crashed, max_items, "job.db") == code
    assert says in capsys.readouterr().err


def _slow_server(delay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(delay)
            tid = parse_qs(urlsplit(self.path).query)["id"][0]
            body = json.dumps({"__typename": "Tweet", "id_str": tid, "text": f"t{tid}",
                               "user": {"id_str": "1", "screen_name": "nasa"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


RUNNER = ("import sys\nfrom xscraper import scraper, cli\n"
          "scraper.TWEET_ENDPOINT = sys.argv[1] + '/tweet-result'\n"
          "sys.exit(cli.main(sys.argv[2:]))\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_stops_every_process_and_hands_work_back(tmp_path):
    # Senior review S2 / principal QA P4: SIGTERM used to kill the crawl with
    # leases held and outcomes unflushed. Now the parent and its helpers stop
    # gracefully, nothing stays leased, and the exit code says "interrupted".
    server = _slow_server(0.05)
    path = tmp_path / "job.db"
    ids = [str(10_000 + i) for i in range(3000)]  # ~40 s of work at this pace
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    try:
        p = subprocess.Popen([sys.executable, "-c", RUNNER, f"http://127.0.0.1:{server.server_address[1]}",
                              "crawl", str(path), *ids, "--processes", "2", "--http", "sync",
                              "--rate", "100000", "--workers", "4", "--progress", "0"],
                             env=env, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # wait until both processes are fetching
            time.sleep(0.2)
            try:
                with JobStore(path) as j:
                    if len(j.workers()) >= 2 and j.counts()["done"] > 0:
                        break
            except Exception:
                pass
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=20)  # helpers must stop too, not finish the job
    finally:
        server.shutdown()
    assert p.returncode == 143, err
    assert "stopped by signal" in err
    with JobStore(path) as j:
        c = j.counts()
        assert c["leased"] == 0 and c["pending"] > 0 and c["done"] > 0
        assert j.workers() == []


# -- rate gate ----------------------------------------------------------------

def test_503_retry_after_pauses_everyone():
    gate = RateGate(clock=lambda: 1000.0)
    gate.enter()
    assert gate.leave(503, {"Retry-After": "20"}) == 20
    assert gate.enter() == pytest.approx(20)


def test_gate_cap_is_exact():
    c, sleeps = client([FakeResponse(200, b"never sent")], max_backoff=1000)
    c.gate.enter = lambda: 700.0
    with pytest.raises(HttpError):
        c.get("https://x")
    assert sum(sleeps) <= 2 * c.gate.max_wait + 3 * 0.25


def test_async_gate_cap():
    pytest.importorskip("aiohttp")
    from xscraper.aio import AsyncHttpClient
    slept = []

    async def sleep(s):
        slept.append(s)

    async def go():
        async with AsyncHttpClient(rate=1000, sleep=sleep) as c:
            c.gate.enter = lambda: 600.0
            with pytest.raises(HttpError, match="rate-limit window"):
                await c.get("http://127.0.0.1:9/never")
    asyncio.run(go())
    assert 2 * 900 <= sum(slept) <= 2 * 900 + 30 * 0.25


@pytest.fixture
def gates(tmp_path):
    now = [1000.0]
    a = SharedRateGate(tmp_path / "job.db", clock=lambda: now[0])
    b = SharedRateGate(tmp_path / "job.db", clock=lambda: now[0])
    yield a, b, now
    a.close(), b.close()


def test_shared_window_shows_in_wait_time(gates):
    a, b, _ = gates
    a.enter()
    a.leave(200, limits(0, 1010))
    assert b.wait_time() == pytest.approx(10)


def test_shared_budget_discounts_this_process_in_flight(gates):
    a, b, _ = gates
    for _ in range(3):
        assert a.enter() == 0
    a.leave(200, limits(3, 1010))  # two of ours are still in flight: 1 left
    assert b.enter() == 0
    assert b.enter() == pytest.approx(10)


def test_shared_window_ignores_reset_in_milliseconds(gates):
    a, b, _ = gates
    a.enter()
    a.leave(200, limits(0, 1000 * 1000 + 60_000))
    assert b.enter() == 0


def test_shared_window_budget_only_goes_down(gates):
    a, b, _ = gates
    a.enter()
    a.leave(200, limits(1, 1010))
    assert b.enter() == 0  # spends the last unit
    b.leave(200, limits(50, 1010))  # same window, stale higher count: must not refill it
    assert a.enter() == pytest.approx(10)


# -- watch --------------------------------------------------------------------

def test_never_seen_tweet_is_not_reported_deleted(tmp_path):
    with WatchStore(tmp_path / "w.db") as store:
        store.add_target("tweet", "404", 60)
        events = asyncio.run(Watcher(store, FakeX().tweet, FakeX().user, track_for=3600).cycle())
        assert events == []


def test_links_use_the_depth_lowered_while_the_item_was_in_flight(job):
    # QA re-verification of P5: D is leased at depth 2 (via A-C) when B lowers
    # it to 1; completing D must queue E at depth 2, not drop it at 3.
    job.configure(follow=("parents", "quotes"), max_depth=2)
    job.add([Item(TWEET, "A"), Item(TWEET, "B")])
    _finish(job, "A", 0)
    _finish(job, "C", 1)
    (d,) = [i for i in job.claim("w", 10) if i.key == "D"]
    assert d.depth == 2
    _finish(job, "B", 0)  # the shorter path lands while D is in flight
    job.complete([Outcome(d, [GRAPH["D"]])])
    assert _depths(job).get("E") == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_zombie_worker_is_reaped(job):
    # QA re-verification: a crashed helper stays a zombie until joined, and
    # kill(pid, 0) succeeds on zombies, so its leases sat idle for 60 s.
    import socket
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # exited but not waited for: a zombie
        try:
            with open(f"/proc/{p.pid}/stat", "rb") as f:
                if f.read().rsplit(b")", 1)[1].split()[0] == b"Z":
                    break
        except OSError:
            break
        time.sleep(0.05)
    owner = f"{socket.gethostname()}:{p.pid}:zombie"
    job.register(owner)
    job.conn.execute("UPDATE workers SET pid = ? WHERE id = ?", (p.pid, owner))
    job.add([Item(TWEET, "1")])
    job.claim(owner, 1)
    try:
        assert job.reap(stale_after=60) == 1
    finally:
        p.wait()
