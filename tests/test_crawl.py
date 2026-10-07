"""Crawl jobs: the SQLite frontier, leases, resume, link following and retries."""
import asyncio
import sqlite3
from collections import Counter

import pytest

from xscraper import cli
from xscraper.crawl import Crawler
from xscraper.http import HttpError, NotFound
from xscraper.jobs import TWEET, USER, Item, JobStore, Outcome, parse_follow
from xscraper.models import Tweet, User
from xscraper.storage import load


def tweet(tid, parent=None, quote=None, rt=None, user="nasa"):
    return Tweet(id=str(tid), text=f"tweet {tid}", created_at="2024-06-10T14:00:00Z",
                 user=User(id="1", screen_name=user), in_reply_to_id=parent and str(parent),
                 quoted_tweet_id=quote and str(quote), retweeted_tweet_id=rt and str(rt))


class FakeX:
    """Tweet graph plus failure injection; counts every fetch."""

    def __init__(self, tweets=(), timelines=None, fail=(), fail_times=None, delay=0.0):
        self.tweets = {t.id: t for t in tweets}
        self.timelines = timelines or {}
        self.fail = set(fail)                  # always fail
        self.fail_times = dict(fail_times or {})  # fail the first N times
        self.delay = delay
        self.calls = Counter()

    async def tweet(self, key):
        self.calls[key] += 1
        await asyncio.sleep(self.delay)
        if key in self.fail:
            raise HttpError("HTTP 403", 403)
        if self.fail_times.get(key, 0) > 0:
            self.fail_times[key] -= 1
            raise HttpError("HTTP 503", 503)
        return self.tweets.get(key)

    async def user(self, name):
        self.calls["@" + name] += 1
        await asyncio.sleep(self.delay)
        if name not in self.timelines:
            raise NotFound("404", 404)
        return self.timelines[name]


def crawl(store, x, **kw):
    max_items = kw.pop("max_items", None)
    kw.setdefault("flush_interval", 0.01)
    crawler = Crawler(store, x.tweet, x.user, **kw)
    return asyncio.run(crawler.run(max_items=max_items))


@pytest.fixture
def job(tmp_path):
    with JobStore(tmp_path / "job.db") as store:
        yield store


def chain(n, start=100):
    """Tweets start..start+n-1, each replying to the previous one."""
    return [tweet(start + i, parent=start + i - 1 if i else None) for i in range(n)]


def test_parse_follow():
    assert parse_follow("all") == ("parents", "quotes", "retweets")
    assert parse_follow("none") == ()
    assert parse_follow("quotes, parents,quotes") == ("quotes", "parents")
    with pytest.raises(ValueError):
        parse_follow("replies")


def test_seeds_are_deduplicated(job):
    assert job.add([Item(TWEET, "1"), Item(TWEET, "2"), Item(TWEET, "1")]) == 2
    assert job.add([Item(TWEET, "2"), Item(USER, "nasa")]) == 1
    assert job.counts()["pending"] == 3


def test_follows_links_up_to_depth(job):
    x = FakeX(chain(10))
    job.configure(follow=("parents",), max_depth=3)
    job.add([Item(TWEET, "109")])
    totals = crawl(job, x)
    assert totals["done"] == 4 and totals["queued"] == 3
    assert sorted(t.id for t in job.tweets) == ["106", "107", "108", "109"]
    assert all(v == 1 for v in x.calls.values())  # nothing fetched twice


def test_links_are_fetched_once_even_when_shared(job):
    root = tweet(1)
    kids = [tweet(10 + i, parent=1, quote=1) for i in range(20)]
    x = FakeX([root, *kids])
    job.configure(follow=("parents", "quotes"), max_depth=1)
    job.add([Item(TWEET, k.id) for k in kids])
    crawl(job, x, concurrency=8)
    assert x.calls["1"] == 1
    assert len(job.tweets) == 21


def test_missing_and_failed_items(job):
    x = FakeX([tweet(1), tweet(4)], fail={"3"}, fail_times={"4": 1})
    job.configure(max_attempts=2)
    job.add([Item(TWEET, k) for k in "1234"] + [Item(USER, "ghost")])
    # Retries wait out a backoff; fake the clock forward instead of sleeping.
    job._clock = _Clock(job._clock, step=10.0)
    totals = crawl(job, x)
    c = job.counts()
    assert (c["done"], c["missing"], c["failed"]) == (2, 2, 1)
    assert totals["retry"] == 2 and totals["failed"] == 1
    assert x.calls["3"] == 2 and x.calls["4"] == 2
    (kind, key, attempts, err), = job.failures()
    assert (kind, key, attempts) == (TWEET, "3", 2) and "403" in err
    assert job.retry_failed() == 1
    assert job.counts()["pending"] == 1


class _Clock:
    """Each call moves time forward, so backoff delays pass instantly."""

    def __init__(self, base, step):
        self.base, self.step, self.offset = base, step, 0.0

    def __call__(self):
        self.offset += self.step
        return self.base() + self.offset


def test_resume_after_stopping(tmp_path):
    x = FakeX(chain(30))
    with JobStore(tmp_path / "job.db") as job:
        job.add([Item(TWEET, t) for t in x.tweets])
        crawl(job, x, max_items=10, concurrency=4)
        assert job.counts()["done"] == 10
        assert job.counts()["leased"] == 0  # unfinished claims were handed back
    with JobStore(tmp_path / "job.db") as job:  # a new process
        crawl(job, x)
        assert job.counts()["done"] == 30
    assert all(v == 1 for v in x.calls.values())


def test_crashed_worker_items_are_requeued(job):
    x = FakeX(chain(5))
    job.add([Item(TWEET, t) for t in x.tweets])
    job.register("dead-worker")
    assert len(job.claim("dead-worker", 5)) == 5
    job.conn.execute("UPDATE workers SET heartbeat = 0 WHERE id = 'dead-worker'")
    crawl(job, x)
    assert job.counts()["done"] == 5
    assert [w["id"] for w in job.workers()] == []


def test_expired_leases_are_reclaimed(job):
    x = FakeX(chain(3))
    job.add([Item(TWEET, t) for t in x.tweets])
    job.claim("someone", 3, lease=-1)  # already expired, owner never registered
    crawl(job, x)
    assert job.counts()["done"] == 3


def test_live_workers_keep_their_leases(job):
    x = FakeX(chain(4))
    job.add([Item(TWEET, t) for t in x.tweets])
    job.register("busy")
    job.claim("busy", 2)
    crawl(job, x, max_items=2)
    assert job.counts() == {"pending": 0, "leased": 2, "done": 2, "missing": 0, "failed": 0}
    assert sum(x.calls.values()) == 2


def test_crawl_does_not_finish_while_another_worker_holds_leases(job):
    # S2: a resumed crawl used to exit 0 as soon as nothing was pending,
    # stranding items leased to a worker that had died moments before.
    x = FakeX(chain(4))
    job.add([Item(TWEET, t) for t in x.tweets])
    job.register("went-quiet")
    job.claim("went-quiet", 2)
    crawl(job, x, heartbeat=0.05, stale_after=0.3)
    assert job.counts()["done"] == 4 and job.counts()["leased"] == 0


def test_dead_local_process_is_reaped_without_waiting(job):
    import subprocess
    import sys
    import time
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # its pid is now (almost certainly) free
    x = FakeX(chain(3))
    job.add([Item(TWEET, t) for t in x.tweets])
    import socket
    owner = f"{socket.gethostname()}:{p.pid}:dead00"  # same shape as new_worker_id()
    job.register(owner)
    job.conn.execute("UPDATE workers SET pid = ? WHERE id = ?", (p.pid, owner))
    job.claim(owner, 3)
    start = time.monotonic()
    crawl(job, x)  # default stale_after is 60 s
    assert job.counts()["done"] == 3
    assert time.monotonic() - start < 5


def test_two_crawlers_share_one_job(tmp_path):
    x = FakeX(chain(300), delay=0.001)
    path = tmp_path / "job.db"
    with JobStore(path) as a:
        a.add([Item(TWEET, t) for t in x.tweets])

    async def both():
        with JobStore(path) as a, JobStore(path) as b:
            return await asyncio.gather(
                Crawler(a, x.tweet, x.user, concurrency=16, flush_interval=0.01, flush_size=20).run(),
                Crawler(b, x.tweet, x.user, concurrency=16, flush_interval=0.01, flush_size=20).run())

    ta, tb = asyncio.run(both())
    assert ta["done"] > 0 and tb["done"] > 0
    assert ta["done"] + tb["done"] == 300
    assert set(x.calls.values()) == {1}
    with JobStore(path) as job:
        assert job.counts()["done"] == 300


def test_user_seed_stores_timeline_and_follows_its_links(job):
    timeline = [tweet(50, rt=40), tweet(51, parent=50), tweet(52, quote=41)]
    x = FakeX([tweet(40), tweet(41)], timelines={"nasa": timeline})
    job.configure(follow=("parents", "quotes", "retweets"), max_depth=1)
    job.add([Item(USER, "nasa")])
    crawl(job, x)
    assert sorted(t.id for t in job.tweets) == ["40", "41", "50", "51", "52"]
    assert x.calls["50"] == 0  # already on the timeline
    assert x.calls["40"] == x.calls["41"] == 1


def test_job_file_is_a_tweet_store(tmp_path):
    path = tmp_path / "job.db"
    with JobStore(path) as job:
        job.add([Item(TWEET, "1")])
        crawl(job, FakeX([tweet(1)]))
    assert [t.id for t in load(path)] == ["1"]


def test_outcomes_are_idempotent(job):
    job.add([Item(TWEET, "1")])
    item, = job.claim("w", 1)
    job.complete([Outcome(item, [tweet(1)])])
    assert job.complete([Outcome(item, [tweet(1)])])["stored"] == 0
    assert len(job.tweets) == 1


def test_not_a_job_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite at all" * 100)
    with pytest.raises(ValueError):
        JobStore(bad)


def test_cli_seed_parsing():
    assert cli._seed("20") == Item(TWEET, "20")
    assert cli._seed("https://x.com/NASA/status/123") == Item(TWEET, "123")
    assert cli._seed("@NASA") == Item(USER, "NASA")
    assert cli._seed("https://x.com/NASA") == Item(USER, "NASA")
    with pytest.raises(ValueError):
        cli._seed("not a name!")


def test_cli_job_status_and_export(tmp_path, capsys):
    path = tmp_path / "job.db"
    with JobStore(path) as job:
        job.add([Item(TWEET, "1"), Item(TWEET, "2")])
        crawl(job, FakeX([tweet(1)]))
    assert cli.main(["job", "status", str(path)]) == 0
    out = capsys.readouterr().out
    assert "done 1" in out and "missing 1" in out
    assert cli.main(["job", "export", str(path), "-o", str(tmp_path / "out.jsonl")]) == 0
    assert [t.id for t in load(tmp_path / "out.jsonl")] == ["1"]
    assert cli.main(["job", "status", str(tmp_path / "nope.db")]) == 2


def test_cli_crawl_no_run_stores_settings(tmp_path):
    path = tmp_path / "job.db"
    assert cli.main(["crawl", str(path), "20", "@NASA", "--follow", "quotes", "--depth", "2",
                     "--no-run"]) == 0
    with JobStore(path) as job:
        assert job.config.follow == ("quotes",) and job.config.max_depth == 2
        assert job.counts()["pending"] == 2
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


class PoisonX(FakeX):
    """Raises an exception nobody anticipated for some keys (S1/P1 class)."""

    def __init__(self, *a, poison=(), exc=TypeError("unhashable type: 'list'"), **kw):
        super().__init__(*a, **kw)
        self.poison, self.exc = set(poison), exc

    async def tweet(self, key):
        if key in self.poison:
            self.calls[key] += 1
            raise self.exc
        return await super().tweet(key)

    async def user(self, name):
        if name in self.poison:
            self.calls["@" + name] += 1
            raise self.exc
        return await super().user(name)


@pytest.mark.parametrize("exc", [TypeError("unhashable type: 'list'"), LookupError("unknown encoding: x-bogus"),
                                 KeyError("data")])
def test_unexpected_error_fails_one_item_not_the_crawl(job, exc):
    # Senior review S1 / principal QA P1: one poisoned item used to end the
    # run and stay at the head of the queue, so the job could never finish.
    x = PoisonX(chain(6), poison={"103"}, exc=exc,
                timelines={"nasa": [tweet(900)]})
    x.poison.add("badcharset")
    job.configure(max_attempts=1)
    job.add([Item(TWEET, t) for t in x.tweets] + [Item(USER, "nasa"), Item(USER, "badcharset")])
    totals = crawl(job, x)
    counts = job.counts()
    assert counts["failed"] == 2 and counts["done"] == 5 + 1 + 1  # 5 good tweets + profile + its tweet
    assert counts["pending"] == counts["leased"] == 0
    assert x.calls["103"] == 1  # parked after max_attempts instead of re-queued
    failures = {key: err for _, key, _, err in job.failures()}
    assert type(exc).__name__ in failures["103"]
    assert totals["failed"] == 2
