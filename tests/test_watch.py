"""Watch mode: scheduled polling, change detection, the event outbox and sinks."""
import asyncio
import json

import pytest

from xscraper import cli
from xscraper.watch import JsonlSink, WatchStore, Watcher, WebhookSink, parse_duration

from .test_crawl import FakeX, tweet


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def counted(tid, likes, text=None, user="nasa"):
    t = tweet(tid, user=user)
    t.like_count = likes
    if text is not None:
        t.text = text
    return t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    with WatchStore(tmp_path / "watch.db", clock=clock) as s:
        yield s


def run_cycle(watcher):
    return asyncio.run(watcher.cycle())


def types(events):
    return [(e.type, e.tweet_id) for e in events]


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("15m") == 900
    assert parse_duration("2h") == 7200
    assert parse_duration("1.5d") == 129600
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_new_tweets_then_nothing(store):
    x = FakeX(timelines={"nasa": [counted(1, 10), counted(2, 5)]})
    store.add_target("user", "nasa", 60)
    w = Watcher(store, x.tweet, x.user)
    assert types(run_cycle(w)) == [("new", "1"), ("new", "2")]
    assert run_cycle(w) == []  # not due again yet
    assert x.calls["@nasa"] == 1


def test_schedule_edit_and_engagement(store, clock):
    timeline = [counted(1, 100)]
    x = FakeX(timelines={"nasa": timeline})
    store.add_target("user", "nasa", 60)
    w = Watcher(store, x.tweet, x.user, engagement_change=0.5)
    run_cycle(w)
    clock.t += 61
    timeline[0] = counted(1, 120, text="fixed typo")
    assert types(run_cycle(w)) == [("edited", "1")]  # +20% is under the 50% threshold
    clock.t += 61
    timeline[0] = counted(1, 160, text="fixed typo")
    events = run_cycle(w)
    assert types(events) == [("engagement", "1")]  # measured from 100, not from 120
    assert events[0].data["change"] == {"like_count": {"from": 100, "to": 160}}
    clock.t += 61
    timeline[0] = counted(1, 170, text="fixed typo")
    assert run_cycle(w) == []  # 160 -> 170 is small; the baseline moved to 160
    assert [row[1] for row in store.history("1")] == [100, 120, 160, 170]


def test_missing_counts_keep_last_value(store, clock):
    timeline = [counted(1, 100)]
    x = FakeX(timelines={"nasa": timeline})
    store.add_target("user", "nasa", 60)
    w = Watcher(store, x.tweet, x.user)
    run_cycle(w)
    clock.t += 61
    timeline[0] = counted(1, None)
    run_cycle(w)
    assert [row[1] for row in store.history("1")] == [100]


def test_tracked_tweets_are_rechecked_for_deletion(store, clock):
    x = FakeX([counted(1, 10), counted(2, 10)], timelines={"nasa": [counted(1, 10), counted(2, 10)]})
    store.add_target("user", "nasa", 3600)
    w = Watcher(store, x.tweet, x.user, track_for=7200, recheck=600)
    run_cycle(w)
    clock.t += 601
    del x.tweets["2"]
    x.tweets["1"] = counted(1, 50)
    events = run_cycle(w)
    assert types(events) == [("deleted", "2")]
    assert events[0].data["last_text"] == "tweet 2"
    assert store.history("1")[-1][1] == 50
    clock.t += 601
    assert run_cycle(w) == []  # deleted tweets aren't re-checked or re-reported
    assert x.calls["2"] == 1
    clock.t += 7200  # tracking window over
    run_cycle(w)
    assert x.calls["1"] == 2


def test_failed_recheck_is_not_a_deletion(store, clock):
    x = FakeX([counted(1, 10)], timelines={"nasa": [counted(1, 10)]}, fail_times={"1": 1})
    store.add_target("user", "nasa", 3600)
    w = Watcher(store, x.tweet, x.user, track_for=7200, recheck=600)
    run_cycle(w)
    clock.t += 601
    assert run_cycle(w) == []
    assert store.summary()["tracked"] == {"live": 1}


def test_watched_tweet_target(store, clock):
    x = FakeX([counted(7, 1)])
    store.add_target("tweet", "7", 60)
    w = Watcher(store, x.tweet, x.user, track_for=0, recheck=600)
    assert types(run_cycle(w)) == [("new", "7")]
    clock.t += 61
    x.tweets.pop("7")
    assert types(run_cycle(w)) == [("deleted", "7")]
    clock.t += 61
    x.tweets["7"] = counted(7, 2)
    assert [e.type for e in run_cycle(w)] == ["restored"]
    assert x.calls["7"] == 3  # polled through its target only, never double-checked


def test_target_errors_are_recorded(store):
    x = FakeX()
    store.add_target("user", "ghost", 60)
    store.add_target("tweet", "9", 60)
    x.fail.add("9")
    run_cycle(Watcher(store, x.tweet, x.user))
    errors = {key: err for _, key, _, _, err in store.targets()}
    assert errors["ghost"] == "not found" and "403" in errors["9"]


def test_jsonl_sink(store, tmp_path):
    x = FakeX(timelines={"nasa": [counted(1, 10)]})
    store.add_target("user", "nasa", 60)
    run_cycle(Watcher(store, x.tweet, x.user, sinks=[JsonlSink(tmp_path / "ev.jsonl")]))
    rows = [json.loads(line) for line in (tmp_path / "ev.jsonl").read_text().splitlines()]
    assert rows[0]["type"] == "new" and rows[0]["tweet"]["id"] == "1"
    assert rows[0]["url"] == "https://x.com/nasa/status/1"


def test_webhook_outbox_retries_backlog(store, clock):
    sent, up = [], [False]

    def post(url, payload, timeout):
        if not up[0]:
            raise OSError("connection refused")
        sent.extend(e["seq"] for e in payload["events"])
        return 204

    timeline = [counted(1, 10)]
    x = FakeX(timelines={"nasa": timeline})
    store.add_target("user", "nasa", 60)
    w = Watcher(store, x.tweet, x.user, sinks=[WebhookSink("http://hook", post=post, batch=1)])
    run_cycle(w)
    assert sent == [] and store.summary()["undelivered"] == 1
    clock.t += 61
    timeline.append(counted(2, 1))
    up[0] = True
    run_cycle(w)
    assert sent == [1, 2]  # the backlog first, in order
    assert store.summary()["undelivered"] == 0


def test_webhook_non_2xx_is_retried(store):
    sink = WebhookSink("http://hook", post=lambda url, payload, timeout: 500)
    x = FakeX(timelines={"nasa": [counted(1, 10)]})
    store.add_target("user", "nasa", 60)
    run_cycle(Watcher(store, x.tweet, x.user, sinks=[sink]))
    assert store.summary()["undelivered"] == 1


def test_run_once_and_stop(store):
    x = FakeX(timelines={"nasa": [counted(1, 10)]})
    store.add_target("user", "nasa", 60)
    seen = []
    w = Watcher(store, x.tweet, x.user)
    asyncio.run(w.run(once=True, on_cycle=seen.append))
    assert w.cycles == 1 and len(seen[0]) == 1

    async def stop_soon():
        async def sleep(s):
            w.stop()
            await asyncio.sleep(0)
        w._sleep = sleep
        await w.run()

    asyncio.run(stop_soon())
    assert w.cycles == 2


def test_drift_is_checked_during_watch(store):
    from xscraper.metrics import DriftMonitor, Metrics
    from xscraper.models import Tweet, User

    m = Metrics()
    m.drift = DriftMonitor(window=10, threshold=0.2, min_samples=3)
    broken = [Tweet(id=str(i), text="", created_at=None, user=User(id="", screen_name="")) for i in range(5)]
    x = FakeX(timelines={"nasa": broken})
    store.add_target("user", "nasa", 60)
    run_cycle(Watcher(store, x.tweet, x.user, observer=m))
    assert "no_date" in m.drift.alerts
    assert m.snapshot()["events"] == {"new": 5}


def test_logged_out_profile_is_a_target_error(store, shell_html):
    from tests.conftest import FakeClient, FakeResponse
    from xscraper.scraper import Scraper
    scraper = Scraper(FakeClient(lambda url, params: FakeResponse(200, shell_html)))

    async def user(name):
        return scraper.user_timeline(name)

    store.add_target("user", "NASA", 60)
    assert run_cycle(Watcher(store, FakeX().tweet, user)) == []
    [(_, _, _, _, err)] = store.targets()
    assert err.startswith("EmptyTimelineShell:") and "--cookies" in err


def test_cli_watch_flow(tmp_path, monkeypatch, capsys):
    x = FakeX([counted(5, 1)], timelines={"NASA": [counted(1, 10)]})

    async def fake_fetchers(args, stack, observer=None):
        return x.tweet, x.user

    monkeypatch.setattr(cli, "_fetchers", fake_fetchers)
    state = str(tmp_path / "w.db")
    assert cli.main(["watch", state, "@NASA", "5", "--once", "--events", str(tmp_path / "e.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "new" in out and "@nasa 1" in out
    assert cli.main(["watch", state, "--status"]) == 0
    assert "user NASA: every 900s" in capsys.readouterr().out
    assert cli.main(["watch", state, "--history", "1"]) == 0
    assert "10" in capsys.readouterr().out
    assert cli.main(["watch", state, "@NASA", "--unwatch"]) == 0
    with WatchStore(state) as s:
        assert [t[1] for t in s.targets()] == ["5"]
    assert cli.main(["watch", state, "@NASA", "--every", "10s"]) == 2


def test_unexpected_error_on_one_target_does_not_stop_the_others(store):
    # Principal QA P3: one poisoned tweet aborted every cycle, so no target
    # was ever polled again.
    from .test_crawl import PoisonX
    x = PoisonX([tweet(10), tweet(11)], poison={"900"}, timelines={"nasa": [counted(1, 10)]})
    for kind, key in (("tweet", "10"), ("tweet", "900"), ("tweet", "11"), ("user", "nasa")):
        store.add_target(kind, key, 60)
    events = run_cycle(Watcher(store, x.tweet, x.user))
    assert sorted(t for _, t in types(events)) == ["1", "10", "11"]
    errors = {key: err for _, key, _, _, err in store.targets()}
    assert "TypeError" in errors["900"] and errors["10"] is None and errors["nasa"] is None


def test_blank_response_is_not_a_deletion(store, clock):
    # Senior review S4: bodies tweet, "", tweet used to emit new, deleted, restored.
    from xscraper.scraper import tweet_from_body
    from .test_crawl import FakeX
    import json as _json
    bodies = [_json.dumps({"__typename": "Tweet", "id_str": "7", "text": "hi",
                           "user": {"id_str": "1", "screen_name": "nasa"}}).encode(), b"", b"  "]

    async def fetch(key):
        return tweet_from_body(key, bodies.pop(0))

    store.add_target("tweet", "7", 60)
    w = Watcher(store, fetch, FakeX().user)
    assert types(run_cycle(w)) == [("new", "7")]
    for _ in range(2):
        clock.t += 61
        assert run_cycle(w) == []
    errors = {key: err for _, key, _, _, err in store.targets()}
    assert "empty response" in errors["7"]


def test_payload_without_text_is_not_an_edit(store, clock):
    # QA re-verification: a payload with an id but no text came back as
    # text "" and fired `edited` twice (to "" and back).
    x = FakeX([counted(5, 1, text="hello")])
    store.add_target("tweet", "5", 60)
    w = Watcher(store, x.tweet, x.user)
    assert types(run_cycle(w)) == [("new", "5")]
    x.tweets["5"] = counted(5, 1, text="")
    clock.t += 61
    assert run_cycle(w) == []
    x.tweets["5"] = counted(5, 1, text="hello")
    clock.t += 61
    assert run_cycle(w) == []
