"""Run statistics, Prometheus export, adaptive concurrency and drift alarms."""
import asyncio
import json
import urllib.error
import urllib.request

from xscraper import cli
from xscraper.crawl import Crawler
from xscraper.http import HttpClient
from xscraper.jobs import TWEET, Item, JobStore, Outcome
from xscraper.metrics import AdaptiveLimit, DriftMonitor, Metrics, anomalies
from xscraper.models import Tweet, User

from .conftest import FakeResponse
from .test_crawl import FakeX, chain, tweet


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_adaptive_limit_slow_start_then_aimd():
    clock = Clock()
    lim = AdaptiveLimit(64, initial=4, clock=clock)
    for _ in range(10):
        lim.on_response(200, 0.01)
    assert lim.limit == 14  # slow start: +1 per success
    lim.on_response(429, 0.01)
    assert lim.limit == 7 and not lim.slow_start
    lim.on_response(503, 0.01)  # within the cooldown: one burst counts once
    assert lim.limit == 7
    for _ in range(7):
        lim.on_response(200, 0.01)
    assert 7.9 < lim.limit < 8.1  # congestion avoidance: about +1 per window
    clock.t += 2
    lim.on_response(None, 20.0, "TimeoutError")
    assert lim.limit < 4.1
    lim.on_response(404, 0.01)  # a client error isn't congestion
    assert lim.limit < 4.1


def test_adaptive_limit_bounds():
    clock = Clock()
    lim = AdaptiveLimit(10, initial=50, minimum=2, clock=clock)
    assert lim.limit == 10
    for _ in range(20):
        clock.t += 5
        lim.on_congestion()
    assert lim.limit == 2
    for _ in range(1000):
        lim.on_success()
    assert lim.limit == 10


def test_anomalies():
    ok = tweet(1)
    ok.like_count = 5
    assert anomalies(ok) == []
    bad = Tweet(id="2", text="", created_at=None, user=User(id="", screen_name=""))
    assert anomalies(bad) == ["no_date", "no_author", "empty_text", "no_like_count"]
    odd = Tweet(id="3", text="x", created_at="Tuesday", user=User(id="1", screen_name="a"), like_count=1)
    assert anomalies(odd) == ["unparsed_date"]


def test_drift_monitor_raises_once_and_clears(caplog):
    d = DriftMonitor(window=100, threshold=0.2, min_samples=20)
    for _ in range(19):
        assert d.observe(["no_date"]) == []  # not enough samples yet
    assert d.observe(["no_date"]) == ["no_date"]
    assert d.observe(["no_date"]) == []
    assert "payload drift" in caplog.text
    for _ in range(100):
        d.observe([])
    assert d.alerts == set()


def test_metrics_snapshot_and_prometheus():
    m = Metrics()
    for s in (0.02, 0.04, 0.2, 3.0):
        m.on_response(200, s)
    m.on_response(429, 0.01)
    m.on_response(None, 20.0, "TimeoutError")
    m.on_retry()
    m.on_wait("server", 2.5)
    m.outcomes([Outcome(Item(TWEET, "1"), [tweet(1)])],
               {"done": 1, "stored": 1, "queued": 2})
    s = m.snapshot()
    assert s["responses"] == {"200": 4, "429": 1, "TimeoutError": 1}
    assert s["items"]["done"] == 1 and s["tweets_stored"] == 1 and s["links_queued"] == 2
    assert s["request_wait_s"] == {"server": 2.5}
    assert 0.025 <= s["latency_s"]["p50"] <= 0.25
    assert s["anomalies"] == {"no_like_count": 1}
    json.dumps(s)
    text = m.prometheus()
    assert 'xscraper_responses_total{status="429"} 1' in text
    assert 'xscraper_request_duration_seconds_bucket{le="+Inf"} 6' in text
    assert "xscraper_request_duration_seconds_count 6" in text
    assert 'xscraper_items_total{outcome="done"} 1' in text
    assert "progress:" in m.progress_line()


def test_parse_errors_count_as_drift():
    m = Metrics()
    m.drift = DriftMonitor(window=10, threshold=0.2, min_samples=5)
    outcomes = [Outcome(Item(TWEET, str(i)), error="ParseError: unexpected payload") for i in range(5)]
    m.outcomes(outcomes, {"retry": 5})
    assert m.drift.alerts == {"parse_error"}


def test_sync_client_reports_to_observer():
    m = Metrics()
    responses = iter([FakeResponse(503), FakeResponse(200, b"{}")])

    class Session:
        headers = {}

        def get(self, *a, **kw):
            return next(responses)

    client = HttpClient(rate=1000, backoff=0, session=Session(), sleep=lambda s: None, observer=m)
    client.get("http://x/")
    assert m.snapshot()["responses"] == {"503": 1, "200": 1}
    assert m.retries == 1


def test_health_and_http_endpoints():
    clock = Clock()
    m = Metrics(clock=clock)
    m.frontier = {"pending": 5, "leased": 0}
    assert m.healthy()[0]
    clock.t += 1000
    ok, why = m.healthy(stall_after=300)
    assert not ok and "no progress" in why
    server = m.serve(0)
    port = server.server_address[1]
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics").read().decode()
        assert "xscraper_responses_total" in body
        stats = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats").read())
        assert stats["frontier"]["pending"] == 5
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        server.shutdown()


def test_crawler_feeds_metrics_and_heartbeat(tmp_path):
    m = Metrics()
    x = FakeX(chain(20))
    with JobStore(tmp_path / "job.db") as job:
        job.add([Item(TWEET, t) for t in x.tweets])
        calls = []
        crawler = Crawler(job, x.tweet, x.user, observer=m, heartbeat=0, flush_interval=0.01,
                          progress=lambda: calls.append(m.progress_line()), progress_interval=0)
        asyncio.run(crawler.run())
    assert m.items["done"] == 20 and m.stored == 20
    assert m.frontier["done"] == 20
    assert calls and "done" in calls[-1]


def test_adaptive_limit_caps_crawler_concurrency(tmp_path):
    lim = AdaptiveLimit(64, initial=2)
    lim.on_congestion()  # leave slow start at 1
    peak = 0
    running = 0

    async def fetch(key):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.001)
        running -= 1
        return tweet(key)

    with JobStore(tmp_path / "job.db") as job:
        job.add([Item(TWEET, str(i)) for i in range(20)])
        asyncio.run(Crawler(job, fetch, None, concurrency=64, limit=lim, flush_interval=0.01).run())
    assert peak == 1


def test_cli_crawl_writes_stats_file(tmp_path, monkeypatch):
    x = FakeX(chain(5))

    async def fake_fetchers(args, stack, observer=None):
        return x.tweet, x.user

    monkeypatch.setattr(cli, "_fetchers", fake_fetchers)
    stats = tmp_path / "stats.json"
    assert cli.main(["crawl", str(tmp_path / "job.db"), *x.tweets, "--stats-file", str(stats),
                     "--progress", "0"]) == 0
    data = json.loads(stats.read_text())
    assert data["items"]["done"] == 5 and data["frontier"]["done"] == 5
